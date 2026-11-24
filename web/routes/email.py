#!/usr/bin/env python3
"""Email Delivery 路由（一键搭建发件人 + SMTP 凭据）。

  - GET /api/email/senders：已批准发件人列表。
  - POST /api/email/senders：创建发件人（body {email_address}，
    创建后需点击验证邮件里的链接才能使用）。
  - GET /api/email/users：IAM 用户列表（给 SMTP 凭据选所属用户）。
  - POST /api/email/smtp-credentials：给指定用户生成 SMTP 凭据
    （body {user_id, description?}，密码只返回一次）。

安全说明（重要）：
  - SMTP 凭据的密码只在创建时返回一次：本路由直接把 OCI 的返回
    透传给前端，前端显示后立即提示用户保存；后端不存、不记日志。
  - 全部接口需登录（Depends(get_current_user)）。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.client import OCIError
from oci.email import (
    create_sender,
    create_smtp_credential,
    list_iam_users,
    list_senders,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()


class CreateSenderBody(BaseModel):
    """创建发件人请求体。"""

    email_address: str


class CreateSmtpCredentialBody(BaseModel):
    """生成 SMTP 凭据请求体。"""

    user_id: str
    description: str = "rbot-clone"


@router.get("/api/email/senders")
def list_all_senders(profile: str = Query(default=None),
                     region: str = Query(default=None),
                     username: str = Depends(get_current_user)):
    """已批准发件人列表。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        senders = list_senders(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "senders": senders}


@router.post("/api/email/senders", status_code=201)
def create_new_sender(body: CreateSenderBody,
                      profile: str = Query(default=None),
                      region: str = Query(default=None),
                      username: str = Depends(get_current_user)):
    """创建发件人（之后需点击验证邮件里的链接才能使用）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        sender = create_sender(cli, compartment_id, body.email_address)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "sender": sender}


@router.get("/api/email/users")
def list_all_users(profile: str = Query(default=None),
                   region: str = Query(default=None),
                   username: str = Depends(get_current_user)):
    """IAM 用户列表（给 SMTP 凭据选所属用户）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        users = list_iam_users(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "users": users}


@router.post("/api/email/smtp-credentials", status_code=201)
def create_new_smtp_credential(body: CreateSmtpCredentialBody,
                               profile: str = Query(default=None),
                               region: str = Query(default=None),
                               username: str = Depends(get_current_user)):
    """给指定 IAM 用户生成 SMTP 凭据。

    注意：密码只在本次响应里返回一次，关闭后无法再次查看。
    后端不保存、不记日志，前端必须明确提示用户立即保存。
    """
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        credential = create_smtp_credential(cli, body.user_id,
                                            description=body.description)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "credential": credential}
