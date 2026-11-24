#!/usr/bin/env python3
"""DNS 管理路由（Cloudflare DNS 记录管理）。

  - GET /api/dns/zones：Zone 列表。
  - GET /api/dns/zones/{zone_id}/records：记录列表（?type= / ?name= 过滤）。
  - POST /api/dns/zones/{zone_id}/records：创建记录。
  - PUT /api/dns/zones/{zone_id}/records/{record_id}：更新记录。
  - DELETE /api/dns/zones/{zone_id}/records/{record_id}：删除记录。
  - POST /api/dns/zones/{zone_id}/records/{record_id}/proxied：切换代理状态。
  - GET /api/dns/credentials：凭据状态（v1.26.0）。
  - POST /api/dns/credentials：手动添加凭据，校验通过才保存（v1.26.0）。
  - DELETE /api/dns/credentials：删除已保存的凭据文件（v1.26.0）。
  - POST /api/dns/credentials/extract：从服务器 1.sh 提取凭据（v1.26.0）。

全部接口需登录（Depends(get_current_user)）。

Cloudflare 客户端约定（clouds/cloudflare.py，另一工人并行编写）：
  - get_client() -> CloudflareClient；list_zones() /
    list_dns_records(zone_id, type=None, name=None) /
    create_dns_record(zone_id, type, name, content, proxied=False, ttl=1) /
    update_dns_record(zone_id, record_id, type, name, content,
                      proxied=False, ttl=1) /
    delete_dns_record(zone_id, record_id) -> bool /
    set_proxied(zone_id, record_id, proxied) -> dict。
  - CloudflareError 有 .kind 字段：config/auth/not_found/rate_limit/
    api/network。

错误映射（不泄露 Cloudflare API 原始错误细节）：
  config/auth -> 502「Cloudflare 未配置或认证失败」；
  not_found -> 404；rate_limit -> 429；其余 -> 502。
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from web.deps import error_response, get_current_user, get_oci_client

try:
    # 正式接口：clouds/cloudflare.py 由另一工人并行编写。
    from clouds.cloudflare import (  # noqa: F401  (CloudflareClient 供类型参考)
        CloudflareClient,
        CloudflareError,
        get_client,
    )
except ImportError:  # clouds.cloudflare 尚未落地时不让面板崩掉，接口报 502
    CloudflareClient = None
    get_client = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（约定接口：有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind

try:
    # 凭据管理（v1.26.0）：配置文件优先，环境变量兜底。
    from tgbot.cf_config import (
        clear_cf_config,
        extract_cf_from_host,
        get_cf_client as get_configured_cf_client,
        mask_cf_email,
        resolve_cf_credentials,
        validate_and_save,
    )
except ImportError:  # 未落地时回落到纯环境变量方式
    clear_cf_config = None
    extract_cf_from_host = None
    get_configured_cf_client = None
    mask_cf_email = None
    resolve_cf_credentials = None
    validate_and_save = None

try:
    # 提取凭据时用：按实例 OCID 查公网 IP，再拼 SSH 主机记录。
    from oci.email import is_valid_email
    from oci.network import get_public_ip
    from tgbot.cpu_model import host_for_ip
except ImportError:  # 提取接口不可用时只影响提取，其他 DNS 功能正常
    is_valid_email = None
    get_public_ip = None
    host_for_ip = None


router = APIRouter()

# 允许的记录类型（超出时 pydantic 校验返回 422）。
RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX")

# CloudflareError kind -> HTTP 状态码。
_CF_STATUS = {
    "config": 502,      # 未配置
    "auth": 502,        # 认证失败（面板自身配置问题）
    "not_found": 404,   # Zone / 记录不存在
    "rate_limit": 429,  # Cloudflare 限流
}

# kind -> 固定中文文案（不把 API 原始错误细节返回给前端）。
_CF_MESSAGE = {
    "config": "Cloudflare 未配置或认证失败",
    "auth": "Cloudflare 未配置或认证失败",
    "not_found": "DNS 记录或 Zone 不存在",
    "rate_limit": "Cloudflare 请求过于频繁，请稍后再试",
}


class DnsRecordBody(BaseModel):
    """创建 / 更新 DNS 记录的请求体。"""

    type: Literal["A", "AAAA", "CNAME", "TXT", "MX"]
    name: str
    content: str
    proxied: bool = False
    ttl: int = 1


class ProxiedBody(BaseModel):
    """代理状态切换的请求体。"""

    proxied: bool


def _get_client():
    """取 Cloudflare 客户端：配置文件优先，环境变量兜底。

    tgbot.cf_config 未就绪时回落到 clouds 的 get_client()
    （纯环境变量方式）；两者都没有时抛 config 错误。
    """
    if get_configured_cf_client is not None:
        return get_configured_cf_client()
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


def _cf_error_response(exc):
    """把 CloudflareError 转成统一错误响应（固定文案，不泄露原始细节）。"""
    kind = getattr(exc, "kind", "api")
    status = _CF_STATUS.get(kind, 502)
    message = _CF_MESSAGE.get(kind, "Cloudflare 调用失败")
    return error_response(message, status_code=status)


# ================= 凭据管理（v1.26.0） =================

class CredentialsBody(BaseModel):
    """手动添加凭据的请求体：mode=token 时传 api_token；mode=key 时传
    api_key + email。凭据值只用于当次校验与保存，绝不返回给前端。"""

    mode: Literal["token", "key"]
    api_token: Optional[str] = None
    api_key: Optional[str] = None
    email: Optional[str] = None


class ExtractBody(BaseModel):
    """从服务器提取凭据的请求体：instance_id 是 OCI 实例 OCID；
    profile / region 不传时用默认账号主区。"""

    instance_id: str
    profile: Optional[str] = None
    region: Optional[str] = None


@router.get("/api/dns/credentials")
def get_credentials(username: str = Depends(get_current_user)):
    """当前凭据状态：是否已配置、来源、方式、脱敏邮箱。

    绝不返回凭据值。source：file（机器人/面板里保存的）/
    oci_config（OCI 配置文件 ~/oci_config 里的 [cloudflare] 段）/
    env（服务器环境变量里的）/ None（都没配）。
    """
    if resolve_cf_credentials is None:
        return {"ok": True, "configured": False, "source": None,
                "auth_mode": None, "email_masked": None}
    api_token, _api_key, email, source = resolve_cf_credentials()
    return {
        "ok": True,
        "configured": source is not None,
        "source": source,
        "auth_mode": (("token" if api_token else "api_key")
                      if source else None),
        "email_masked": (mask_cf_email(email) if email else None),
    }


@router.post("/api/dns/credentials")
def save_credentials(body: CredentialsBody,
                     username: str = Depends(get_current_user)):
    """手动添加凭据：调 Cloudflare API 校验，校验通过才保存。"""
    if validate_and_save is None:
        return error_response("凭据管理模块未就绪", status_code=502)
    if body.mode == "token":
        token = (body.api_token or "").strip()
        if not token or any(ch.isspace() for ch in token):
            return error_response("Token 不能为空也不能带空格",
                                  status_code=400)
        ok, zones_total, _err = validate_and_save(token, "", "")
    else:
        key = (body.api_key or "").strip()
        email = (body.email or "").strip()
        if not key or any(ch.isspace() for ch in key):
            return error_response("Key 不能为空也不能带空格",
                                  status_code=400)
        if is_valid_email is None or not is_valid_email(email):
            return error_response("邮箱格式不对", status_code=400)
        ok, zones_total, _err = validate_and_save("", key, email)
    if not ok:
        return error_response("凭据校验没通过（无效或权限不足），没有保存",
                              status_code=502)
    return {"ok": True, "zones": zones_total, "source": "file"}


@router.delete("/api/dns/credentials")
def delete_credentials(username: str = Depends(get_current_user)):
    """删除已保存的凭据文件（只删文件，不动环境变量）。"""
    if resolve_cf_credentials is None or clear_cf_config is None:
        return error_response("凭据管理模块未就绪", status_code=502)
    _token, _key, _email, source = resolve_cf_credentials()
    if source != "file":
        if source == "oci_config":
            detail = "（当前用的是 OCI 配置文件 ~/oci_config 里的凭据）"
        else:
            detail = "（当前用的是环境变量里的凭据）"
        return error_response("没有已保存的凭据文件" + detail,
                              status_code=400)
    clear_cf_config()
    return {"ok": True}


@router.post("/api/dns/credentials/extract")
def extract_credentials(body: ExtractBody,
                        username: str = Depends(get_current_user)):
    """从一台服务器的证书脚本（1.sh）提取 CF 凭据，校验通过才保存。"""
    if extract_cf_from_host is None or validate_and_save is None:
        return error_response("凭据管理模块未就绪", status_code=502)
    if get_public_ip is None or host_for_ip is None:
        return error_response("提取功能未就绪", status_code=502)
    try:
        cli = get_oci_client(body.profile, body.region)
        public_ip = get_public_ip(cli, body.instance_id)
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转 502
        return error_response(f"查不到该实例的公网 IP：{exc}",
                              status_code=502)
    if not public_ip:
        return error_response("该实例没有公网 IP", status_code=400)
    host = host_for_ip(public_ip)
    if host is None:
        return error_response("未配置守护 SSH 密钥，面板连不上该服务器",
                              status_code=502)
    api_key, email, error = extract_cf_from_host(host)
    if error:
        return error_response(f"提取失败：{error}", status_code=502)
    ok, zones_total, _err = validate_and_save("", api_key, email)
    if not ok:
        return error_response("提取到的凭据校验没通过（可能已失效），没有保存",
                              status_code=502)
    return {"ok": True, "zones": zones_total, "source": "file",
            "email_masked": mask_cf_email(email)}


@router.get("/api/dns/zones")
def list_zones(username: str = Depends(get_current_user)):
    """Zone 列表。"""
    try:
        zones = _get_client().list_zones()
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zones": zones}


@router.get("/api/dns/zones/{zone_id}/records")
def list_records(zone_id: str,
                 type: Optional[str] = Query(default=None),
                 name: Optional[str] = Query(default=None),
                 username: str = Depends(get_current_user)):
    """某 Zone 的 DNS 记录列表（可选按 type / name 过滤）。"""
    try:
        records = _get_client().list_dns_records(zone_id, type=type,
                                                 name=name)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zone_id": zone_id, "records": records}


@router.post("/api/dns/zones/{zone_id}/records")
def create_record(zone_id: str,
                  body: DnsRecordBody,
                  username: str = Depends(get_current_user)):
    """创建一条 DNS 记录。"""
    try:
        record = _get_client().create_dns_record(
            zone_id, body.type, body.name, body.content,
            proxied=body.proxied, ttl=body.ttl)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zone_id": zone_id, "record": record}


@router.put("/api/dns/zones/{zone_id}/records/{record_id}")
def update_record(zone_id: str,
                  record_id: str,
                  body: DnsRecordBody,
                  username: str = Depends(get_current_user)):
    """更新一条 DNS 记录。"""
    try:
        record = _get_client().update_dns_record(
            zone_id, record_id, body.type, body.name, body.content,
            proxied=body.proxied, ttl=body.ttl)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zone_id": zone_id, "record": record}


@router.delete("/api/dns/zones/{zone_id}/records/{record_id}")
def delete_record(zone_id: str,
                  record_id: str,
                  username: str = Depends(get_current_user)):
    """删除一条 DNS 记录。"""
    try:
        _get_client().delete_dns_record(zone_id, record_id)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zone_id": zone_id, "record_id": record_id,
            "deleted": True}


@router.post("/api/dns/zones/{zone_id}/records/{record_id}/proxied")
def set_proxied(zone_id: str,
                record_id: str,
                body: ProxiedBody,
                username: str = Depends(get_current_user)):
    """切换一条记录的 Cloudflare 代理状态。"""
    try:
        record = _get_client().set_proxied(zone_id, record_id, body.proxied)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    return {"ok": True, "zone_id": zone_id, "record": record}
