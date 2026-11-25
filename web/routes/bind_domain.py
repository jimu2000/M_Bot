#!/usr/bin/env python3
"""实例详情“绑定域名”API（v2.01）。

  - POST /api/instances/{instance_id}/bind-domain：给实例绑域名。
    请求体 {zone_id, subdomain, overwrite=false}；subdomain 是主机名
    （www、app，或 @ 表示根域名）。后端查实例公网 IP，建一条 A 记录
    指向它；同名 A 记录已存在且指向不同 IP 时返回 409（带 existing，
    前端弹确认覆盖），overwrite=true 时更新旧记录。

全部接口需登录（Depends(get_current_user)）。

复用现有逻辑，不另起一套：
  - 二级域名校验与建记录流程：tgbot.bind_domain（与机器人端同一份）；
  - Cloudflare 凭据与客户端：tgbot.cf_config.get_cf_client，与
    web/routes/dns.py 一致；
  - 错误映射：web/routes/dns.py 的 _cf_error_response（固定文案，
    不泄露 Cloudflare API 原始错误细节）。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.client import OCIError
from oci.network import get_public_ip
from tgbot.bind_domain import bind_a_record, build_fqdn, validate_subdomain
from web.deps import (error_response, get_current_user, oci_error_response,
                      resolve_profile)
from web.routes.dns import _cf_error_response

try:
    from tgbot.cf_config import get_cf_client as get_configured_cf_client
except ImportError:  # Falls back to the pure environment-variable approach when the config is not ready. 未落地时回落到纯环境变量方式。
    get_configured_cf_client = None

try:
    from clouds.cloudflare import CloudflareError, get_client
except ImportError:  # While clouds.cloudflare is not ready, the endpoints return 502. clouds.cloudflare 尚未落地时接口报 502。
    get_client = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（约定接口：有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind


router = APIRouter()


class BindDomainBody(BaseModel):
    """绑定域名请求体：zone_id 是 Cloudflare Zone id；subdomain 是
    主机名（@ 表示根域名）；overwrite=true 时覆盖已存在的同名 A 记录。"""

    zone_id: str
    subdomain: str
    overwrite: bool = False


def _get_client():
    """取 Cloudflare 客户端：与 web/routes/dns.py 的 _get_client 一致。"""
    if get_configured_cf_client is not None:
        return get_configured_cf_client()
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


@router.post("/api/instances/{instance_id}/bind-domain")
def bind_domain(instance_id: str,
                body: BindDomainBody,
                profile: str = Query(default=None),
                region: str = Query(default=None),
                username: str = Depends(get_current_user)):
    """给实例绑定域名：建 A 记录指向实例公网 IP。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        public_ip = get_public_ip(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    if not public_ip:
        return error_response("该实例没有公网 IP，无法绑定域名",
                              status_code=400)
    ok, sub = validate_subdomain(body.subdomain)
    if not ok:
        return error_response(
            "二级域名格式不对：只允许字母、数字、-、_、.，或单独一个 @",
            status_code=400)
    try:
        client = _get_client()
        zone = client.get_zone(body.zone_id)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    zone_name = (zone or {}).get("name")
    if not zone_name:
        return error_response("Zone 不存在", status_code=404)
    fqdn = build_fqdn(sub, zone_name)
    try:
        action, record = bind_a_record(client, body.zone_id, fqdn,
                                       public_ip,
                                       overwrite=body.overwrite)
    except CloudflareError as exc:
        return _cf_error_response(exc)
    if action == "exists_conflict":
        return error_response(
            f"{fqdn} 已有一条 A 记录指向 {record.get('content')}，"
            "确认覆盖后可更新",
            status_code=409,
            extra={"exists": True, "fqdn": fqdn, "existing": record,
                   "zone_name": zone_name})
    return {"ok": True, "instance_id": instance_id, "fqdn": fqdn,
            "ip": public_ip, "action": action, "record": record,
            "zone_name": zone_name}
