#!/usr/bin/env python3
"""认证路由：登录 / 登出 / 当前用户 / 改密码 / 验证码 / Telegram 登录。

  - POST /api/auth/login：JSON {username, password}，锁定返回 429 + 剩余秒数，
    成功设 HttpOnly cookie mbot_session。图形验证码开启时
    （config.CAPTCHA_ENABLED）还需带 captchaId + captchaAnswer。
  - GET /api/auth/captcha：取图形验证码（未开启时返回 {enabled: false}）。
  - POST /api/auth/telegram/send-code：发 Telegram 登录验证码
    （60 秒冷却，同一 IP 冷却期内返回 429）。消息里同时带一条
    一键登录链接（/login?token=...，点开即登录，一次性、5 分钟过期）。
    Bark 已配置且开启时，验证码与链接同步推一条加密 Bark（双通道，
    Bark 失败不影响登录）。
  - POST /api/auth/telegram/verify：验 Telegram 验证码，正确则建立会话。
  - POST /api/auth/telegram/magic-login：消费一键登录 token，正确则建立会话。
  - POST /api/auth/logout：注销会话并清除 cookie。
  - GET /api/auth/me：返回当前登录用户（需登录）。
  - POST /api/auth/change-password：改管理员密码（需登录），验旧密码 +
    新密码强度校验，通过后把新哈希写进 env 文件（web/config.py 的
    ENV_FILE），返回提示“重启 Web 面板服务后生效”。

验证码（图形 / Telegram）一律一次性、5 分钟过期，绝不写日志。
"""

import os
import tempfile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from web import config
from web.auth import (
    LOGIN_GUARD,
    MAGIC_LOGIN_TOKENS,
    SESSIONS,
    TELEGRAM_CODES,
    TELEGRAM_CODE_EXPIRE_SECONDS,
    hash_password,
    verify_password,
)
from web.captcha import CAPTCHAS, generate_captcha
from web.deps import error_response, get_current_user
from web.telegram_notify import send_to_allowed_users

from tgbot.bark import send_bark

router = APIRouter()

# 新密码强度要求：至少 8 位，且同时包含字母与数字。
MIN_PASSWORD_LENGTH = 8


class LoginBody(BaseModel):
    """登录请求体（图形验证码开启时 captchaId/captchaAnswer 必填）。"""

    username: str
    password: str
    captcha_id: str = None
    captcha_answer: str = None
    remember: bool = False


class TelegramVerifyBody(BaseModel):
    """Telegram 验证码登录请求体。"""

    code_id: str
    code: str


class TelegramMagicLoginBody(BaseModel):
    """Telegram 一键登录请求体：链接里的 token。"""

    token: str


class ChangePasswordBody(BaseModel):
    """改密码请求体。"""

    old_password: str
    new_password: str


def validate_new_password(password):
    """校验新密码强度：至少 8 位，且同时包含字母与数字。

    不合要求抛 ValueError（带中文原因）。
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"新密码至少 {MIN_PASSWORD_LENGTH} 位")
    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        raise ValueError("新密码必须同时包含字母和数字")


def update_password_hash_in_env_file(new_hash, env_file=None):
    """把新密码哈希写进 env 文件的 MBOT_WEB_PASSWORD_HASH 行。

    env_file 默认取 web/config.py 的 ENV_FILE（不要写死路径）。
    保留文件里的其他行（会话密钥等）；没有该行时追加一行；
    文件不存在时新建。写操作是原子的（先写临时文件再替换），
    写完后文件权限设为 600（只允许属主读写）。
    失败时抛 OSError，调用方转成 500 响应。
    """
    path = env_file or config.ENV_FILE
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    new_line = f"MBOT_WEB_PASSWORD_HASH='{new_hash}'\n"
    found = False
    output = []
    for line in lines:
        if line.startswith("MBOT_WEB_PASSWORD_HASH="):
            output.append(new_line)
            found = True
        else:
            output.append(line)
    if not found:
        output.append(new_line)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".",
                                    prefix=".mbot-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(output)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)


def _set_session_cookie(response, username, remember=False):
    """给响应设 HttpOnly 会话 cookie（密码登录与 Telegram 登录共用）。

    remember=True（登录页勾选“记住我”）时服务端会话与 cookie
    同时延长到 REMEMBER_ME_DAYS 天，否则默认 SESSION_EXPIRE_HOURS。"""
    max_age = (int(config.REMEMBER_ME_DAYS * 24 * 3600) if remember
               else int(config.SESSION_EXPIRE_HOURS * 3600))
    token = SESSIONS.create(username, expire_seconds=max_age)
    response.set_cookie(
        config.SESSION_COOKIE, token,
        httponly=True, samesite="lax", path="/",
        max_age=max_age)
    return response


@router.get("/api/auth/captcha")
def get_captcha():
    """取图形验证码：未开启时返回 {enabled: false}；开启时返回
    {enabled: true, captchaId, image}（image 是 data URL，前端直接
    当 <img> src 用；答案存服务端，5 分钟过期，一次性）。"""
    if not config.CAPTCHA_ENABLED:
        return {"ok": True, "enabled": False}
    captcha_id, data_url = generate_captcha()
    return {"ok": True, "enabled": True,
            "captchaId": captcha_id, "image": data_url}


@router.post("/api/auth/login")
def login(body: LoginBody):
    """登录：验证用户名密码，成功后设 HttpOnly 会话 cookie。

    图形验证码开启时先验验证码（一次性，错了直接 400，不计登录失败）。
    """
    if config.CAPTCHA_ENABLED:
        if not body.captcha_id or not body.captcha_answer:
            return error_response("请填写图形验证码", status_code=400)
        if not CAPTCHAS.verify(body.captcha_id, body.captcha_answer):
            return error_response("验证码错误或已过期，请刷新验证码重试",
                                  status_code=400)
    if LOGIN_GUARD.is_locked(body.username):
        remaining = LOGIN_GUARD.remaining_lock_seconds(body.username)
        return JSONResponse(
            status_code=429,
            content={"ok": False,
                     "error": f"登录失败次数过多，已锁定，{remaining} 秒后重试",
                     "remaining_seconds": remaining})
    ok = (body.username == config.ADMIN_USERNAME
          and config.ADMIN_PASSWORD_HASH
          and verify_password(body.password, config.ADMIN_PASSWORD_HASH))
    if not ok:
        LOGIN_GUARD.record_failure(body.username)
        return error_response("用户名或密码错误", status_code=401)
    LOGIN_GUARD.record_success(body.username)
    return _set_session_cookie(
        JSONResponse({"ok": True, "username": body.username}),
        body.username, remember=body.remember)


def _client_key(request):
    """发码冷却的客户端标识：优先 X-Forwarded-For 首个 IP，否则直连 IP。"""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    client = request.client
    return client.host if client else ""


def _push_bark_login_code(code, base_url, magic_token):
    """登录码同步推一条加密 Bark（双通道，尽力而为）。

    Bark 未配置/未开启时 send_bark 直接返回失败，不发请求；
    推送失败或异常都静默吞掉，绝不影响 Telegram 登录流程。
    验证码绝不写日志。
    """
    try:
        send_bark(
            "M_Bot 面板登录",
            f"验证码：{code}，5 分钟内有效。\n"
            f"点这里直接登录：{base_url}/login?token={magic_token}")
    except Exception:  # noqa: BLE001 - Bark 是辅助通道，失败不影响登录
        pass


@router.post("/api/auth/telegram/send-code")
def telegram_send_code(request: Request):
    """发 Telegram 登录验证码：6 位数字码经 Bot API 发给白名单用户。

    同一客户端 60 秒内只能发一次（冷却期内返回 429 +
    retry_after_seconds）。机器人未配置（无 Token 或白名单为空）时
    返回 503。验证码本身绝不返回给前端、不写日志。
    """
    client_key = _client_key(request)
    can_send, remaining = TELEGRAM_CODES.can_send(client_key)
    if not can_send:
        return JSONResponse(
            status_code=429,
            content={"ok": False,
                     "error": f"发送太频繁，请 {remaining} 秒后再试",
                     "retry_after_seconds": remaining})
    code_id, code = TELEGRAM_CODES.create()
    magic_token = MAGIC_LOGIN_TOKENS.create()
    # 一键登录链接地址：优先用配置的公网地址，否则用请求自身的 Host
    # 拼（反代需透传 Host；链接点开即登录，与验证码同等效力）。
    base_url = config.PANEL_URL or str(request.base_url).rstrip("/")
    sent = send_to_allowed_users(
        f"【M_Bot 面板登录】验证码：{code}，5 分钟内有效，"
        f"请勿告诉他人。如非本人操作请忽略。\n"
        f"点这里直接登录：{base_url}/login?token={magic_token}")
    if sent <= 0:
        # 发送失败：作废这条码与链接（不进冷却，用户可立即重试）。
        TELEGRAM_CODES.revoke(code_id)
        MAGIC_LOGIN_TOKENS.revoke(magic_token)
        return error_response(
            "Telegram 机器人未配置或发送失败：请检查面板服务器的 "
            "MBOT_TELEGRAM_TOKEN 与 MBOT_ALLOWED_USERS",
            status_code=503)
    TELEGRAM_CODES.mark_sent(client_key)
    # 双通道：Bark 已配置且开启时同步推一条（含验证码与一键登录链接）。
    _push_bark_login_code(code, base_url, magic_token)
    return {"ok": True, "codeId": code_id,
            "expiresInSeconds": TELEGRAM_CODE_EXPIRE_SECONDS}


@router.post("/api/auth/telegram/verify")
def telegram_verify(body: TelegramVerifyBody):
    """验 Telegram 登录验证码：正确则建立登录会话（管理员账号）。

    验证码一次性、5 分钟过期；错了直接 401，不计密码登录失败次数。
    """
    if not TELEGRAM_CODES.verify(body.code_id, body.code):
        return error_response("验证码错误或已过期，请重新发送",
                              status_code=401)
    return _set_session_cookie(
        JSONResponse({"ok": True, "username": config.ADMIN_USERNAME}),
        config.ADMIN_USERNAME)


@router.post("/api/auth/telegram/magic-login")
def telegram_magic_login(body: TelegramMagicLoginBody):
    """一键登录：消费链接里的 token，正确则建立登录会话（管理员账号）。

    token 一次性、5 分钟过期；错了或过期直接 401。token 值绝不写日志。
    """
    if not MAGIC_LOGIN_TOKENS.consume(body.token):
        return error_response("登录链接已失效，请重新发送验证码",
                              status_code=401)
    return _set_session_cookie(
        JSONResponse({"ok": True, "username": config.ADMIN_USERNAME}),
        config.ADMIN_USERNAME)


@router.post("/api/auth/logout")
def logout(request: Request):
    """登出：注销当前会话并清除 cookie（未登录时也返回成功）。"""
    token = request.cookies.get(config.SESSION_COOKIE)
    SESSIONS.revoke(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return response


@router.get("/api/auth/me")
def me(username: str = Depends(get_current_user)):
    """返回当前登录用户。"""
    return {"ok": True, "username": username}


@router.post("/api/auth/change-password")
def change_password(body: ChangePasswordBody,
                    username: str = Depends(get_current_user)):
    """改管理员密码：验旧密码 + 新密码强度校验，通过后更新 env 文件。

    注意：运行中的面板进程读的是启动时加载的旧哈希，改密码后
    必须重启 Web 面板服务新密码才生效（响应里明确提示这一点）。
    """
    if not verify_password(body.old_password, config.ADMIN_PASSWORD_HASH):
        return error_response("旧密码不正确", status_code=401)
    try:
        validate_new_password(body.new_password)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    if body.new_password == body.old_password:
        return error_response("新密码不能与旧密码相同", status_code=400)
    new_hash = hash_password(body.new_password)
    try:
        update_password_hash_in_env_file(new_hash)
    except OSError as exc:
        return error_response(f"写入 env 文件失败：{exc}", status_code=500)
    return {"ok": True,
            "message": "密码已更新，重启 Web 面板服务后生效"}
