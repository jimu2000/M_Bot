#!/usr/bin/env python3
"""Cloudflare API v4 客户端：DNS 记录管理（Zone / DNS record 增删改查）。

安全说明（重要）：
  - 本模块只通过环境变量或调用方显式传入的参读取 API Token / Global API Key，
    从不读取、不打印、不外传真实凭据，也不在任何服务器或文件里找密钥。
  - 配置区在文件顶端，真实 Token / Key / 邮箱一律用占位符，绝不写进代码。

认证（两种，二选一，Token 优先）：
  1. API Token（推荐）：Authorization: Bearer <token>
  2. Global API Key + Email：X-Auth-Key / X-Auth-Email

用法示例（占位符）：
    from clouds.cloudflare import get_client
    client = get_client()  # 从环境变量 CF_API_TOKEN 或 CF_API_KEY/CF_EMAIL 读
    zones = client.list_zones()
    records = client.list_dns_records("zone-id-123")
"""

import os
import socket
import ssl
from datetime import datetime, timezone

import httpx

# ================= 配置区（改这里） =================
# Cloudflare API v4 基础地址。
API_BASE_URL = "https://api.cloudflare.com/client/v4"
# 单次请求超时（秒）。
DEFAULT_TIMEOUT = 30
# 列表接口每页条数（list_zones / list_dns_records 翻页时用）。
DEFAULT_PER_PAGE = 50
# SSL 证书检查直连超时（秒，check_ssl_expiry 用，不走 Cloudflare API）。
SSL_CHECK_TIMEOUT = 10
# 环境变量名（真实凭据只放环境变量，不要写进代码）：
#   CF_API_TOKEN  - API Token（推荐，优先使用），示例占位符：cf-token-xxxxxxxx
#   CF_API_KEY    - Global API Key（与 CF_EMAIL 配合使用），示例：cf-key-xxxxxxxx
#   CF_EMAIL      - Global API Key 对应的账号邮箱，示例：user@example.com
ENV_API_TOKEN = "CF_API_TOKEN"
ENV_API_KEY = "CF_API_KEY"
ENV_EMAIL = "CF_EMAIL"
# =====================================================

# create_dns_record / update_dns_record 允许的 DNS 记录类型。
ALLOWED_RECORD_TYPES = {"A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA"}
# Cloudflare error code 中表示 zone / record 不存在的（HTTP 200 但 success=false
# 时也能按 code 判为 not_found）。7003: No route for that URI；
# 7000: No route for that URI（旧版）；7001: Method not allowed 也常伴随不存在的
# 路径；7007: Cannot find zone；7008: Cannot find record。按需补充。
NOT_FOUND_ERROR_CODES = {7000, 7003, 7007, 7008}


class CloudflareError(Exception):
    """Cloudflare 调用失败。kind 字段分类错误：

    config     - 凭据未配置（构造时两种认证都没有）
    auth       - 401 / 403，凭据无效或权限不足
    not_found  - 404，或 error code 7003/7000/7007/7008（zone/record 不存在）
    rate_limit - 429（Retry-After 只记录在 retry_after，不自动重试，调用方决定）
    api        - 其他 Cloudflare 返回 success=false
    network    - httpx 异常（连接失败、超时等）
    """

    def __init__(self, message, kind="api", retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class CloudflareClient:
    """Cloudflare API v4 的同步客户端（httpx）。"""

    def __init__(self, api_token=None, api_key=None, email=None, timeout=None):
        """参数不传时读环境变量（CF_API_TOKEN / CF_API_KEY / CF_EMAIL）。

        Token 优先；两种认证都没有时抛 CloudflareError(kind="config")。
        """
        api_token = api_token or os.environ.get(ENV_API_TOKEN)
        api_key = api_key or os.environ.get(ENV_API_KEY)
        email = email or os.environ.get(ENV_EMAIL)

        if api_token:
            self._auth_headers = {"Authorization": f"Bearer {api_token}"}
            self._auth_mode = "token"
        elif api_key and email:
            self._auth_headers = {
                "X-Auth-Key": api_key,
                "X-Auth-Email": email,
            }
            self._auth_mode = "api_key"
        else:
            raise CloudflareError(
                f"未配置 Cloudflare 凭据：请设置环境变量 {ENV_API_TOKEN}，"
                f"或同时设置 {ENV_API_KEY} 与 {ENV_EMAIL}",
                kind="config",
            )
        self.timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        self._client = None  # 惰性创建，方便测试 patch

    @property
    def auth_mode(self):
        """当前认证方式：'token' 或 'api_key'。"""
        return self._auth_mode

    # ---------- 内部 ----------

    def _get_http_client(self):
        # 注意：不用 base_url。httpx 里 "/zones" 这种绝对路径会顶掉
        # base_url 的 /client/v4 前缀，改由 _request/_list_paged 拼完整 URL。
        if self._client is None:
            self._client = httpx.Client(
                headers=self._auth_headers,
                timeout=self.timeout,
            )
        return self._client

    def _request(self, method, path, **kwargs):
        """统一请求入口：发请求、解析 success/result/errors、按状态码与
        error code 分类抛错。429 时读 Retry-After 头（只记录，不重试）。

        返回 Cloudflare 响应的 result 字段（dict 或 list）。
        """
        try:
            response = self._get_http_client().request(
                method, API_BASE_URL + path, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"网络请求失败：{exc}", kind="network") from exc
        return self._parse_response(response, path).get("result")

    @staticmethod
    def _parse_response(response, path):
        """解析 Cloudflare 响应：429/auth/not_found/api 按状态码与 error code
        分类抛错；成功时返回解析后的 payload（dict）。"""
        # 429：限流，读 Retry-After 头（只记录，不重试，调用方决定）
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise CloudflareError(
                f"Cloudflare 限流（429）{path}："
                + (f"Retry-After={retry_after}" if retry_after else "未返回 Retry-After"),
                kind="rate_limit",
                retry_after=retry_after,
            )

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        success = payload.get("success", False)
        errors = payload.get("errors") or []
        error_codes = {e.get("code") for e in errors if isinstance(e, dict)}
        error_text = "; ".join(
            e.get("message", str(e)) for e in errors if isinstance(e, dict)
        ) or response.text[:500]

        if response.status_code in (401, 403):
            raise CloudflareError(
                f"Cloudflare 认证失败（{response.status_code}）{path}：{error_text}",
                kind="auth",
            )
        if response.status_code == 404 or error_codes & NOT_FOUND_ERROR_CODES:
            raise CloudflareError(
                f"Cloudflare 资源不存在（{response.status_code}）{path}：{error_text}",
                kind="not_found",
            )
        if not success:
            raise CloudflareError(
                f"Cloudflare API 错误（{response.status_code}）{path}：{error_text}",
                kind="api",
            )
        return payload

    def _list_paged(self, path, params=None):
        """翻页取全：per_page=50，按 result_info.total_pages 翻完所有页。"""
        all_items = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"page": page, "per_page": DEFAULT_PER_PAGE})
            try:
                response = self._get_http_client().request(
                    "GET", API_BASE_URL + path, params=query)
            except httpx.HTTPError as exc:
                raise CloudflareError(
                    f"网络请求失败：{exc}", kind="network") from exc
            payload = self._parse_response(response, path)
            result = payload.get("result") or []
            all_items.extend(result)
            result_info = payload.get("result_info") or {}
            total_pages = result_info.get("total_pages") or 1
            if page >= total_pages or not result:
                break
            page += 1
        return all_items

    # ---------- Zone ----------

    def list_zones(self, name=None, status=None):
        """列出所有 Zone（自动翻页取全）。

        每项含 id / name / status / paused / name_servers。
        name/status 为过滤条件（透传给 API）。
        """
        params = {}
        if name is not None:
            params["name"] = name
        if status is not None:
            params["status"] = status
        zones = self._list_paged("/zones", params=params)
        return [
            {
                "id": z.get("id"),
                "name": z.get("name"),
                "status": z.get("status"),
                "paused": z.get("paused"),
                "name_servers": z.get("name_servers") or [],
            }
            for z in zones
        ]

    def get_zone(self, zone_id):
        """取单个 Zone 详情。返回原样 result（id/name/status/paused 等）。"""
        return self._request("GET", f"/zones/{zone_id}")

    # ---------- DNS 记录 ----------

    def list_dns_records(self, zone_id, type=None, name=None):
        """列出某 Zone 的 DNS 记录（自动翻页取全）。

        每项含 id / type / name / content / proxied / ttl / zone_id / zone_name。
        type/name 为过滤条件（透传给 API）。
        """
        params = {}
        if type is not None:
            params["type"] = type
        if name is not None:
            params["name"] = name
        records = self._list_paged(f"/zones/{zone_id}/dns_records",
                                   params=params)
        return [
            {
                "id": r.get("id"),
                "type": r.get("type"),
                "name": r.get("name"),
                "content": r.get("content"),
                "proxied": r.get("proxied"),
                "ttl": r.get("ttl"),
                "zone_id": r.get("zone_id"),
                "zone_name": r.get("zone_name"),
            }
            for r in records
        ]

    def create_dns_record(self, zone_id, type, name, content,
                          proxied=False, ttl=1):
        """新建 DNS 记录。type 必须在允许列表内，name/content 非空。

        ttl=1 表示自动（Cloudflare 默认）。
        """
        self._validate_record_fields(type, name, content)
        return self._request(
            "POST", f"/zones/{zone_id}/dns_records",
            json={"type": type, "name": name, "content": content,
                  "proxied": proxied, "ttl": ttl},
        )

    def update_dns_record(self, zone_id, record_id, type, name, content,
                          proxied=False, ttl=1):
        """全量更新 DNS 记录（PUT）。校验同 create_dns_record。"""
        self._validate_record_fields(type, name, content)
        return self._request(
            "PUT", f"/zones/{zone_id}/dns_records/{record_id}",
            json={"type": type, "name": name, "content": content,
                  "proxied": proxied, "ttl": ttl},
        )

    def delete_dns_record(self, zone_id, record_id):
        """删除 DNS 记录，成功返回 True。"""
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        return True

    def set_proxied(self, zone_id, record_id, proxied):
        """只改 proxied 字段：先 GET 读出现有记录，再 PUT 只改 proxied。"""
        existing = self._request(
            "GET", f"/zones/{zone_id}/dns_records/{record_id}")
        return self._request(
            "PUT", f"/zones/{zone_id}/dns_records/{record_id}",
            json={
                "type": existing.get("type"),
                "name": existing.get("name"),
                "content": existing.get("content"),
                "proxied": proxied,
                "ttl": existing.get("ttl", 1),
            },
        )

    @staticmethod
    def _validate_record_fields(type, name, content):
        if type not in ALLOWED_RECORD_TYPES:
            raise ValueError(
                f"不支持的 DNS 记录类型 {type!r}，"
                f"只允许：{sorted(ALLOWED_RECORD_TYPES)}")
        if not name or not str(name).strip():
            raise ValueError("name 不能为空")
        if not content or not str(content).strip():
            raise ValueError("content 不能为空")

    def close(self):
        """关闭底层 httpx 客户端（可选，进程退出前调用即可）。"""
        if self._client is not None:
            self._client.close()
            self._client = None


def get_client():
    """从环境变量构建 CloudflareClient（供 Web / Telegram / MCP 调用）。

    读 CF_API_TOKEN，缺失时读 CF_API_KEY + CF_EMAIL；都没有则抛
    CloudflareError(kind="config")。
    """
    return CloudflareClient()


# ---------- SSL 证书到期检查（不走 Cloudflare API，直连 443） ----------

def check_ssl_expiry(domain, port=443):
    """直连 domain:port 取 TLS 证书到期日（标准库 ssl，不需要凭据）。

    返回 {"domain": 域名, "expires": "YYYY-MM-DD", "days_left": 剩余天数}；
    days_left 为 0 表示今天到期，负数表示已过期 N 天。
    连接失败 / 证书无到期日时抛 CloudflareError（kind="network"/"api"），
    错误信息只带域名，不裸露 socket 细节。
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection((domain, port),
                                      timeout=SSL_CHECK_TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        raise CloudflareError(
            f"无法获取 {domain} 的 SSL 证书：连接失败",
            kind="network") from exc
    not_after = (cert or {}).get("notAfter")
    if not not_after:
        raise CloudflareError(
            f"无法获取 {domain} 的 SSL 证书：证书无到期日", kind="api")
    try:
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        raise CloudflareError(
            f"无法获取 {domain} 的 SSL 证书：到期日格式无法解析",
            kind="api") from None
    expires = expires.replace(tzinfo=timezone.utc)
    days_left = (expires.date() - datetime.now(timezone.utc).date()).days
    return {"domain": domain, "expires": expires.date().isoformat(),
            "days_left": days_left}
