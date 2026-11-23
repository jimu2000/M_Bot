#!/usr/bin/env python3
"""DNS 管理路由（Cloudflare DNS 记录管理）。

  - GET /api/dns/zones：Zone 列表。
  - GET /api/dns/zones/{zone_id}/records：记录列表（?type= / ?name= 过滤）。
  - POST /api/dns/zones/{zone_id}/records：创建记录。
  - PUT /api/dns/zones/{zone_id}/records/{record_id}：更新记录。
  - DELETE /api/dns/zones/{zone_id}/records/{record_id}：删除记录。
  - POST /api/dns/zones/{zone_id}/records/{record_id}/proxied：切换代理状态。

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

from web.deps import error_response, get_current_user

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
    """取 Cloudflare 客户端；clouds.cloudflare 未就绪时抛 config 错误。"""
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


def _cf_error_response(exc):
    """把 CloudflareError 转成统一错误响应（固定文案，不泄露原始细节）。"""
    kind = getattr(exc, "kind", "api")
    status = _CF_STATUS.get(kind, 502)
    message = _CF_MESSAGE.get(kind, "Cloudflare 调用失败")
    return error_response(message, status_code=status)


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
