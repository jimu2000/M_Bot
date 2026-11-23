#!/usr/bin/env python3
"""FastAPI 依赖与统一错误响应。

  - get_current_user：读 session cookie，查 SessionStore，未登录抛 401。
  - get_oci_client / resolve_profile：按 profile 取 OCIClient 与 compartment。
  - error_response / oci_error_response：统一返回 {"ok": false, "error": ...}，
    OCIError 按 client.py 的错误分类映射到合适状态码。
"""

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

# OCI 异常类 -> HTTP 状态码（按 client.py 的错误分类）。
_OCI_STATUS = {
    NotFoundError: 404,       # 资源不存在
    AuthError: 502,           # 上游 OCI 认证失败（面板自身配置问题）
    RateLimitError: 429,      # OCI 限流
    QuotaExceededError: 409,  # 配额不足，与当前资源状态冲突
    OutOfCapacityError: 503,  # 机房无容量，服务暂时不可用
    OCIError: 502,            # 其他 OCI 调用失败
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


def get_oci_client(profile=None):
    """返回指定 profile 的 OCIClient；未知 profile 抛 ValueError。"""
    cfg = config.get_profile_config(profile)
    return OCIClient(profile=profile or config.DEFAULT_PROFILE,
                      region=cfg["region"])


def resolve_profile(profile=None):
    """返回 (OCIClient, compartment_id)；未知 profile 抛 ValueError。"""
    cfg = config.get_profile_config(profile)
    cli = OCIClient(profile=profile or config.DEFAULT_PROFILE,
                    region=cfg["region"])
    return cli, cfg["compartment_id"]


def oci_status(exc):
    """按 OCI 异常类型返回 HTTP 状态码（未分类的 OCIError 返回 502）。"""
    for exc_class, status in _OCI_STATUS.items():
        if isinstance(exc, exc_class):
            return status
    return 502


def error_response(message, status_code=400):
    """统一错误响应：{"ok": false, "error": "..."}。"""
    return JSONResponse(status_code=status_code,
                        content={"ok": False, "error": message})


def oci_error_response(exc):
    """把 OCI 异常转成统一错误响应（状态码按错误分类映射）。"""
    return error_response(str(exc), status_code=oci_status(exc))
