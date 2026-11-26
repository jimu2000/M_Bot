#!/usr/bin/env python3
"""FastAPI 依赖与统一错误响应。

  - get_current_user：读 session cookie，查 SessionStore，未登录抛 401。
  - get_oci_client / resolve_profile：按 profile 与区域取 OCIClient 与
    compartment；区域不传时用该 profile 的主区，传了则校验订阅。
  - get_profile_regions / get_region_counts：各账号订阅的区域列表与区数
    （查 iam region-subscription list，缓存 1 小时），供 /api/profiles
    与区域校验共用。
  - error_response / oci_error_response：统一返回 {"ok": false, "error": ...}，
    OCIError 按 client.py 的错误分类映射到合适状态码。
"""

import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from oci.client import (
    AuthError,
    NotFoundError,
    OCIClient,
    OCIError,
    OutOfCapacityError,
    QuotaExceededError,
    RateLimitError,
)
from web import config
from web.auth import SESSIONS

# OCI exception class -> HTTP status code (per client.py's error classification): not found / upstream OCI auth failure (a panel config problem) / OCI rate limit / quota shortfall or conflict with current resource state / no capacity in the region, service temporarily unavailable / other OCI call failures. OCI 异常类 -> HTTP 状态码（按 client.py 的错误分类）：资源不存在 / 上游 OCI 认证失败（面板自身配置问题）/ OCI 限流 / 配额不足，与当前资源状态冲突 / 机房无容量，服务暂时不可用 / 其他 OCI 调用失败。
_OCI_STATUS = {
    NotFoundError: 404,
    AuthError: 502,
    RateLimitError: 429,
    QuotaExceededError: 409,
    OutOfCapacityError: 503,
    OCIError: 502,
}


def get_current_user(request: Request):
    """依赖：当前登录用户。读 session cookie，查 SessionStore。

    未登录（无 cookie / token 无效 / 已过期）抛 401。
    """
    token = request.cookies.get(config.SESSION_COOKIE)
    username = SESSIONS.get(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    return username


# Region-subscription cache: region-subscription rarely changes, so cache it for 1 hour to avoid calling OCI once per account on every page open. 订阅区域缓存：region-subscription 很少变，缓存 1 小时，避免每次打开页面都对每个账号调一次 OCI。
_REGION_TTL = 3600
_profile_regions = {}
_profile_regions_at = 0


def get_profile_regions():
    """返回 {profile: [订阅区域代码, ...]}；单个账号查失败时跳过。

    结果缓存 1 小时；缓存命中时直接返回副本（调用方不要改原 dict）。
    与机器人端 tgbot.handlers_extra.get_profile_regions 同源（查
    iam region-subscription list），只不过这里只留区域代码。
    """
    global _profile_regions, _profile_regions_at
    now = time.time()
    if _profile_regions and now - _profile_regions_at < _REGION_TTL:
        return {k: list(v) for k, v in _profile_regions.items()}
    regions = {}
    for name in config.PROFILES:
        try:
            cfg = config.get_profile_config(name)
            cli = OCIClient(profile=name, region=cfg["region"])
            result = cli.run(["iam", "region-subscription", "list",
                              "--tenancy-id", cfg["compartment_id"]])
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, list):
                codes = [item.get("region-name") for item in data
                         if isinstance(item, dict)
                         and item.get("region-name")]
                regions[name] = codes
        except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
            continue
    _profile_regions = regions
    _profile_regions_at = now
    return {k: list(v) for k, v in regions.items()}


def get_region_counts():
    """返回 {profile: 订阅区域数}；由 get_profile_regions 派生，共享缓存。"""
    return {name: len(codes)
            for name, codes in get_profile_regions().items()}


def effective_region(profile=None, region=None):
    """返回实际生效的区域：显式传了 region 就用它，否则用 profile 的主区。"""
    if region:
        return region
    return config.get_profile_config(profile)["region"]


def get_oci_client(profile=None, region=None):
    """返回指定 profile 与区域的 OCIClient；未知 profile 抛 ValueError。

    region 不传时用该 profile 的主区；传了则校验必须在该账号的订阅
    区域里（订阅列表查不到时放行，与机器人端一致）。

    客户端按 (profile, region) 缓存复用（第 2 轮性能修复）：新建
    OCIClient 本身很便宜，但第一次调 SDK 时要建 service client，
    SDK 会重读私钥文件并解析（load_pem_private_key，约 0.08 秒/次
    调用；每次强制刷新约 5 次调用，合计约 0.39 秒）还要建 TLS
    连接、加载 CA；每请求新建客户端等于每次强制刷新都重付这笔账。
    复用后私钥只解析一次、TLS 连接池也跨请求复用。
    代理仍每次调用时重解析（见 OCIClient._sdk_service_client），
    改代理无需重启。配置变更（删账号）时调
    clear_oci_client_cache() 清掉，见 web/routes/profiles.py。
    与机器人端 tgbot/config.py 的 get_oci_client 缓存同源。
    """
    cfg = config.get_profile_config(profile)
    name = profile or config.DEFAULT_PROFILE
    if region:
        subs = get_profile_regions().get(name) or []
        if subs and region not in subs:
            raise ValueError(f"账号 {name} 未订阅该区域：{region}")
    key = (name, region or cfg["region"])
    with _OCI_CLIENTS_LOCK:
        client = _OCI_CLIENTS.get(key)
        if client is None:
            client = OCIClient(profile=name, region=key[1])
            _OCI_CLIENTS[key] = client
        return client


# OCIClient 缓存：按 (profile, region) 复用，避免每请求重建 service
# client 并重读私钥。见 get_oci_client。
_OCI_CLIENTS = {}
_OCI_CLIENTS_LOCK = threading.Lock()


def clear_oci_client_cache(profile=None):
    """清 OCIClient 缓存：不传 profile 时全清；传了只清该账号。

    账号删除后调用（见 web/routes/profiles.py），避免删掉的账号
    的旧客户端被复用；新账号要重启面板才生效，重启即清空缓存。
    """
    with _OCI_CLIENTS_LOCK:
        if profile is None:
            _OCI_CLIENTS.clear()
        else:
            for key in [k for k in _OCI_CLIENTS if k[0] == profile]:
                del _OCI_CLIENTS[key]


def resolve_profile(profile=None, region=None):
    """返回 (OCIClient, compartment_id)；未知 profile 或未订阅区域抛 ValueError。"""
    cli = get_oci_client(profile, region)
    return cli, config.get_profile_config(profile)["compartment_id"]


def oci_status(exc):
    """按 OCI 异常类型返回 HTTP 状态码（未分类的 OCIError 返回 502）。"""
    for exc_class, status in _OCI_STATUS.items():
        if isinstance(exc, exc_class):
            return status
    return 502


def error_response(message, status_code=400, extra=None):
    """统一错误响应：{"ok": false, "error": "..."}。

    extra 是可选的附加字段字典（原样并进响应体，如备份已满时的
    full/count/limit，前端据此决定弹什么弹窗）。
    """
    content = {"ok": False, "error": message}
    if extra:
        content.update(extra)
    return JSONResponse(status_code=status_code, content=content)


def oci_error_response(exc):
    """把 OCI 异常转成统一错误响应（状态码按错误分类映射）。"""
    return error_response(str(exc), status_code=oci_status(exc))
