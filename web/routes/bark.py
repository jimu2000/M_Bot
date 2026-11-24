#!/usr/bin/env python3
"""Bark 通知设置路由（菜单 43 的网页版，与机器人端共用配置）。

  - GET /api/bark/status：开关状态、是否配齐、地址脱敏显示、
    Key 是否已设（32 位）。Key 与地址原文绝不返回。
  - POST /api/bark/config：设置 Bark 地址和/或 32 位加密 Key，
    body {url?, key?}；只传一个字段时只改该字段。地址须以
    http(s):// 开头，Key 须正好 32 位（与机器人端同一校验）。
  - POST /api/bark/toggle：开关推送，body {enabled}；打开时必须
    已配齐地址与 Key，否则 400。
  - POST /api/bark/test：发一条测试推送（不依赖开关），返回
    {ok, sent, error?}。
  - POST /api/bark/generate-key：随机生成一个 32 位加密 Key 并保存，
    返回 {ok, key, qr_png?, ...状态字段}。全站唯一会返回 Key 原文
    的接口——生成后用户必须把 Key 同步到 Bark App（扫码或手动填），
    页面只显示这一次；qr_png 是 Key 的二维码 PNG（data URI），
    segno 未安装时为 None。

配置存在 tgbot/data/bark.json（0600），与机器人端共用；网页端
改的配置机器人定时任务立即生效，反之亦然。
"""

import base64

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from tgbot.bark import (
    bark_configured,
    generate_bark_key,
    load_bark_config,
    make_key_qr_png,
    mask_bark_url,
    save_bark_config,
    send_bark_test,
)
from web.deps import error_response, get_current_user

router = APIRouter()


class BarkConfigBody(BaseModel):
    url: str = ""
    key: str = ""


class BarkToggleBody(BaseModel):
    enabled: bool = False


def _status_payload(cfg):
    """状态响应体（Key 只返回是否已设，地址脱敏）。"""
    return {
        "ok": True,
        "enabled": bool(cfg.get("enabled")),
        "configured": bark_configured(cfg),
        "url_masked": mask_bark_url(cfg.get("url") or ""),
        "key_set": len(cfg.get("key") or "") == 32,
    }


@router.get("/api/bark/status")
def bark_status(user: str = Depends(get_current_user)):
    """Bark 配置状态（脱敏）。"""
    return _status_payload(load_bark_config())


@router.post("/api/bark/config")
def bark_set_config(body: BarkConfigBody,
                    user: str = Depends(get_current_user)):
    """设置 Bark 地址和/或加密 Key（只传一个字段时只改该字段）。"""
    url = (body.url or "").strip()
    key = (body.key or "").strip()
    if not url and not key:
        return error_response("请至少填写 Bark 地址或加密 Key。",
                              status_code=400)
    if url and not (url.startswith("http://")
                    or url.startswith("https://")):
        return error_response("地址格式不正确：请以 http:// 或 https:// 开头。",
                              status_code=400)
    if key and len(key) != 32:
        return error_response(
            f"加密 Key 必须是 32 位（你填的是 {len(key)} 位）。",
            status_code=400)
    cfg = load_bark_config()
    if url:
        cfg["url"] = url
    if key:
        cfg["key"] = key
    save_bark_config(cfg)
    return _status_payload(cfg)


@router.post("/api/bark/toggle")
def bark_toggle(body: BarkToggleBody,
                user: str = Depends(get_current_user)):
    """开关 Bark 推送；打开时必须已配齐地址与 Key。"""
    cfg = load_bark_config()
    if body.enabled and not bark_configured(cfg):
        return error_response(
            "还没配齐：请先设置 Bark 地址和 32 位加密 Key。",
            status_code=400)
    cfg["enabled"] = bool(body.enabled)
    save_bark_config(cfg)
    return _status_payload(cfg)


@router.post("/api/bark/generate-key")
def bark_generate_key(user: str = Depends(get_current_user)):
    """随机生成一个 32 位 Bark 加密 Key 并保存，返回 Key 原文与二维码。

    全站唯一会返回 Key 原文的接口——生成后用户必须把 Key 同步到
    Bark App 的加密设置里（扫 qr_png 二维码或手动填），否则推送
    解不开；页面只显示这一次。旧 Key 生成后立即失效，其他用旧
    Key 的发送端需同步更新。
    """
    cfg = load_bark_config()
    key = generate_bark_key()
    cfg["key"] = key
    save_bark_config(cfg)
    qr_png = make_key_qr_png(key)
    payload = _status_payload(cfg)
    payload["key"] = key
    payload["qr_png"] = (
        "data:image/png;base64," + base64.b64encode(qr_png).decode("ascii")
        if qr_png else None)
    return payload


@router.post("/api/bark/test")
def bark_test(user: str = Depends(get_current_user)):
    """发一条测试推送（不依赖开关）。"""
    cfg = load_bark_config()
    if not bark_configured(cfg):
        return error_response(
            "还没配齐：请先设置 Bark 地址和 32 位加密 Key。",
            status_code=400)
    ok, err = send_bark_test()
    if not ok:
        return error_response(f"测试推送失败：{err}", status_code=502)
    return {"ok": True, "sent": True}
