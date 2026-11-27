#!/usr/bin/env python3
"""IAM 用户管理路由（网页版，Bot 菜单 8 的对应功能）。

  - GET /api/iam/users?profile=X：列出该账号租户下的 IAM 用户。
  - POST /api/iam/users：新增普通用户 {profile, name, email, description?}。
  - POST /api/iam/users/bot：创建专用 Bot API 用户 {profile, email?}：
    api_user + Group_for_Api_used + Api_User_Policy，幂等复用已存在资源；
    策略只有实例/卷/虚拟网络三项（2026-11-27 用户决定：不给 manage users，
    Bot 用户不能改别人密码；IAM 用户管理只走管理员 profile）；
    成功后自动生成 RSA API Key，私钥只在本次响应里返回一次（前端提供下载，
    刷新后不再可查）。
  - DELETE /api/iam/users/{user_id}?profile=X：删除用户。两重保护与机器人
    端一致：禁删当前 profile 正在用的 API 用户；禁删 Administrators 组里
    最后一个管理员（OCI 本身不拦，删了租户就没人能管）。
  - GET /api/iam/users/{user_id}/keys?profile=X：列出某用户的 API Key。
  - POST /api/iam/users/{user_id}/keys：给用户新增 API Key {profile}：
    生成 RSA 密钥并上传公钥，私钥只在本次响应里返回一次。
  - DELETE /api/iam/users/{user_id}/keys/{fingerprint}?profile=X：删除
    API Key；禁删当前 profile 正在签名使用的密钥。
  - POST /api/iam/users/{user_id}/reset-password：重置控制台临时密码
    {profile}；临时密码只在本次响应里返回一次。

全部接口需登录（Depends(get_current_user)）。IAM 是租户级服务，固定用
该 profile 的主区（get_oci_client(profile) 默认主区）。tenancy OCID 从
OCI 配置文件读（get_profile_tenancy_id），读不到时回落 compartment_id，
与机器人端 _get_iam_tenancy_id 一致。

私钥与临时密码绝不写日志、不进响应以外的任何存储；前端拿到后立即提供
下载/复制，页面刷新即失效。
"""

import json
import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.iam import (
    build_oci_config,
    create_api_key,
    create_bot_permission_user,
    create_or_reset_ui_password,
    create_user,
    delete_api_key,
    delete_user,
    get_profile_api_fingerprint,
    get_profile_tenancy_id,
    get_profile_user_id,
    is_last_admin,
    list_api_keys,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    get_oci_client,
    oci_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _translate_oci_message(code, msg):
    """把常见的 OCI 错误信息翻成中文（其余原文返回）。"""
    if code == "NotAuthorizedOrNotFound":
        return "权限不足：当前 API 用户没有管理 IAM 用户的权限"
    if "description size must be between 1 and 400" in msg:
        return "用户描述不能为空，长度需在 1–400 字符之间"
    return msg


def _oci_short_message(exc):
    """从 OCI 异常文本里抽出最短的可用信息。

    CLI 失败时异常文本里常带整段 ServiceError JSON（含 client_version、
    opc-request-id、request_endpoint 等），直接展示给用户太长。
    只取 JSON 里的 message 字段并翻成中文；解析不到就返回原文本
    （截断到 200 字）。
    """
    text = str(exc)
    start = text.find("{")
    if start >= 0:
        try:
            data, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(data, dict):
                code = (data.get("code") or "").strip()
                msg = (data.get("message") or "").strip()
                if msg:
                    return _translate_oci_message(code, msg)
        except ValueError:
            pass
    return text[:200]

# Bot 专用 API 用户的默认身份（与机器人端菜单 8 一致）。
BOT_USER_NAME = "api_user"
BOT_USER_EMAIL = "api@example.com"
BOT_GROUP_NAME = "Group_for_Api_used"
BOT_POLICY_NAME = "Api_User_Policy"


def _iam_cli(profile):
    """指定 profile 的 IAM 客户端（主区）与 tenancy OCID。"""
    cli = get_oci_client(profile)
    tenancy_id = get_profile_tenancy_id(profile)
    if not tenancy_id:
        tenancy_id = config.get_profile_config(profile)["compartment_id"]
    return cli, tenancy_id


def _is_current_api_user(profile, user_id):
    """目标用户是不是该 profile 正在用的 API 用户。"""
    if not user_id:
        return False
    current = get_profile_user_id(profile)
    return current is not None and current == user_id


def _is_current_api_key(profile, user_id, fingerprint):
    """目标 API Key 是否正是该 profile 正在签名使用的密钥。"""
    if not user_id or not fingerprint:
        return False
    if not _is_current_api_user(profile, user_id):
        return False
    current_fp = get_profile_api_fingerprint(profile)
    return bool(current_fp and current_fp == fingerprint.strip().lower())


class CreateUserBody(BaseModel):
    profile: str = ""
    name: str = ""
    email: str = ""
    description: str = ""


class CreateBotUserBody(BaseModel):
    profile: str = ""
    email: str = ""


class ProfileBody(BaseModel):
    profile: str = ""


@router.get("/api/iam/users")
def list_iam_users(profile: str = Query(""),
                   _user: str = Depends(get_current_user)):
    """列出 IAM 用户。"""
    try:
        cli, tenancy_id = _iam_cli(profile)
        users = cli.sdk_list_users(tenancy_id) or []
    except ValueError as exc:
        return error_response(str(exc) or "未知账号", 400)
    except Exception as exc:  # noqa: BLE001 - OCI 异常按类型映射状态码
        return error_response(f"列出用户失败：{exc}",
                              oci_status(exc))
    return {"ok": True, "profile": profile or "DEFAULT",
            "current_api_user_id": get_profile_user_id(profile or "DEFAULT"),
            "users": users}


@router.post("/api/iam/users")
def create_iam_user(body: CreateUserBody,
                    _user: str = Depends(get_current_user)):
    """新增普通用户。"""
    name = (body.name or "").strip()
    email = (body.email or "").strip()
    if not name or not email:
        return error_response("用户名与邮箱不能为空", 400)
    try:
        cli, tenancy_id = _iam_cli(body.profile)
        user = create_user(cli, tenancy_id, name, email,
                           body.description or None)
    except ValueError as exc:
        return error_response(str(exc) or "创建用户失败", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"创建用户失败：{_oci_short_message(exc)}",
                              oci_status(exc))
    return {"ok": True, "user": user}


@router.post("/api/iam/users/bot")
def create_bot_user(body: CreateBotUserBody,
                    _user: str = Depends(get_current_user)):
    """创建专用 Bot API 用户并生成 RSA API Key（私钥只返回一次）。"""
    email = (body.email or "").strip() or BOT_USER_EMAIL
    try:
        cli, tenancy_id = _iam_cli(body.profile)
        result = create_bot_permission_user(
            cli, tenancy_id, BOT_GROUP_NAME, BOT_USER_NAME,
            email, BOT_POLICY_NAME)
        key_data = create_api_key(cli, result["user_id"])
    except ValueError as exc:
        return error_response(str(exc) or "创建 Bot 用户失败", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"创建 Bot 用户失败：{exc}",
                              oci_status(exc))
    # 私钥只在本次响应里返回一次，不写日志。配置文件随私钥一起返回，
    # 用户可直接粘贴到云账户页“添加租户”（机器人端创建时也会发这段）。
    pem = key_data.get("private_key_pem")
    if isinstance(pem, bytes):
        pem = pem.decode("utf-8")
    oci_config = build_oci_config(
        BOT_USER_NAME, result.get("user_id"),
        key_data.get("fingerprint"), tenancy_id,
        getattr(cli, "region", None))
    return {"ok": True, "user_id": result.get("user_id"),
            "user_name": BOT_USER_NAME,
            "group_created": result.get("group_created"),
            "policy_created": result.get("policy_created"),
            "key": {"fingerprint": key_data.get("fingerprint"),
                    "lifecycle_state": key_data.get("lifecycle_state")},
            "private_key_pem": pem, "oci_config": oci_config}


@router.delete("/api/iam/users/{user_id}")
def delete_iam_user(user_id: str, profile: str = Query(""),
                    _user: str = Depends(get_current_user)):
    """删除用户（禁删当前 API 用户与最后一个管理员）。"""
    try:
        cli, tenancy_id = _iam_cli(profile)
        if _is_current_api_user(profile, user_id):
            return error_response(
                "不能删除该用户：它是当前账号正在使用的 API 用户，"
                "删了机器人会立刻失联。请先换账号或换 API 密钥。", 400)
        if is_last_admin(cli, tenancy_id, user_id):
            return error_response(
                "不能删除该用户：它是租户里最后一个管理员 "
                "（Administrators 组只剩它一人），删了整个租户就没人能管了。"
                "请先把另一个用户加进 Administrators 组，再删除它。", 400)
        delete_user(cli, user_id)
    except ValueError as exc:
        return error_response(str(exc) or "删除用户失败", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"删除用户失败：{_oci_short_message(exc)}",
                              oci_status(exc))
    return {"ok": True}


@router.get("/api/iam/users/{user_id}/keys")
def list_user_keys(user_id: str, profile: str = Query(""),
                   _user: str = Depends(get_current_user)):
    """列出某用户的 API Key。"""
    try:
        cli, _tenancy_id = _iam_cli(profile)
        keys = list_api_keys(cli, user_id) or []
    except ValueError as exc:
        return error_response(str(exc) or "未知账号", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"列出 API Key 失败：{exc}",
                              oci_status(exc))
    return {"ok": True, "keys": keys}


@router.post("/api/iam/users/{user_id}/keys")
def create_user_key(user_id: str, body: ProfileBody,
                    _user: str = Depends(get_current_user)):
    """给用户新增 API Key（私钥与配置文件只返回一次）。"""
    try:
        cli, tenancy_id = _iam_cli(body.profile)
        key_data = create_api_key(cli, user_id)
    except ValueError as exc:
        return error_response(str(exc) or "新增 API Key 失败", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"新增 API Key 失败：{exc}",
                              oci_status(exc))
    if isinstance(key_data, tuple):
        key, pem = key_data[0], key_data[1]
    else:
        key = {"fingerprint": key_data.get("fingerprint"),
               "lifecycle_state": key_data.get("lifecycle_state")}
        pem = key_data.get("private_key_pem")
    if isinstance(pem, bytes):
        pem = pem.decode("utf-8")
    # 配置文件随私钥一起返回：段名用 IAM 用户名，找不到时用 user OCID。
    profile_name = user_id
    try:
        users = cli.sdk_list_users(tenancy_id) or []
        target = next((u for u in users if u.get("id") == user_id), None)
        if target and target.get("name"):
            profile_name = target["name"]
    except Exception:  # noqa: BLE001 - 查不到名字不影响出配置
        pass
    oci_config = build_oci_config(
        profile_name, user_id, key.get("fingerprint"), tenancy_id,
        getattr(cli, "region", None))
    return {"ok": True, "key": key, "private_key_pem": pem,
            "oci_config": oci_config}


@router.delete("/api/iam/users/{user_id}/keys/{fingerprint}")
def delete_user_key(user_id: str, fingerprint: str,
                    profile: str = Query(""),
                    _user: str = Depends(get_current_user)):
    """删除 API Key（禁删当前 profile 正在用的密钥）。"""
    try:
        cli, _tenancy_id = _iam_cli(profile)
        if _is_current_api_key(profile, user_id, fingerprint):
            return error_response(
                "不能删除该 API Key：它是当前账号正在签名使用的密钥，"
                "删了机器人会立刻失联。请先换 API 密钥。", 400)
        delete_api_key(cli, user_id, fingerprint)
    except ValueError as exc:
        return error_response(str(exc) or "删除 API Key 失败", 400)
    except Exception as exc:  # noqa: BLE001
        return error_response(f"删除 API Key 失败：{exc}",
                              oci_status(exc))
    return {"ok": True}


@router.post("/api/iam/users/{user_id}/reset-password")
def reset_user_password(user_id: str, body: ProfileBody,
                        _user: str = Depends(get_current_user)):
    """重置控制台临时密码（只返回一次）。

    管理员对列表中任意用户发起重置；是否允许由 OCI IAM 实际策略、
    身份域与用户能力决定，失败时把 OCI 错误映射为友好提示。
    """
    try:
        cli, tenancy_id = _iam_cli(body.profile)
        # 不在面板层按 API 用户身份或 capabilities 隐藏/拦截重置。
        # 管理员可以对列表中的任意用户发起重置；最终权限与限制由 OCI 返回。
        password = create_or_reset_ui_password(cli, user_id)
    except ValueError as exc:
        return error_response(str(exc) or "重置密码失败", 400)
    except Exception as exc:  # noqa: BLE001
        # OCI 官方支持用户通过 API 重置自己的控制台密码；若目标就是
        # 当前 profile 的签名用户，不应误导成“不能重置自己”。
        if (_is_current_api_user(body.profile, user_id)
                and ("NotAuthorizedOrNotFound" in str(exc)
                     or "PATCH request failed" in str(exc))):
            message = (
                "当前 API 用户重置自己的控制台密码也被 OCI 拒绝。OCI 文档"
                "支持用户通过 API 重置自身密码，因此这不是面板主动拦截。"
                "请检查该用户是否启用了控制台登录、是否属于 Identity Domains，"
                "以及当前 profile 是否指向正确租户和主区域。若为 Identity "
                "Domains 用户，请在身份域用户管理中发起密码重置邮件；若为经典 "
                "IAM 用户，请在 OCI 控制台的用户资料中检查登录能力后重试。"
                "面板不会尝试绕过 OCI 的身份验证或授权。"
            )
            return error_response(message, oci_status(exc))
        return error_response(f"重置密码失败：{exc}",
                              oci_status(exc))
    return {"ok": True, "password": password}
