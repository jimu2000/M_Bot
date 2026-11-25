#!/usr/bin/env python3
"""GFW 自动换 IP 路由（菜单 16 的网页版，与机器人端共用配置）。

  - GET /api/gfw/status：Key 是否已设（脱敏显示）、已开启的实例、
    各实例上次检查结果、检查间隔。
  - POST /api/gfw/key：设置 RIPE Atlas API Key，body {key}；
    Key 只存 tgbot/data/gfw.json（0600），响应里只返回脱敏值。
  - GET /api/gfw/instances?profile=&region=：该账号指定区域的 RUNNING
    实例（带公网 IP），供页面渲染开关列表；region 不传时用主区。
  - POST /api/gfw/toggle：开关某实例的监控，body
    {instance_id, enabled, profile?, region?, name?, ip?}。
  - POST /api/gfw/check-now：后台线程立即跑一轮检查（每台约 1-2
    分钟），换 IP 时发双通道通知，完成后也发一条汇总；接口立即
    返回 {ok, started}，不等待检查完成。

与机器人端共用 tgbot/gfw.py 的配置与循环逻辑；网页端改的开关
机器人定时任务立即生效，反之亦然。
"""

import threading

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.instance import list_instances
from oci.network import attach_public_ips
from tgbot import config as bot_config
from tgbot.bark import load_bark_config
from tgbot.gfw import (
    GFW_CHECK_INTERVAL_MINUTES,
    is_gfw_configured,
    load_gfw_config,
    mask_api_key,
    run_monitor_cycle,
    set_api_key,
    set_instance_enabled,
)
from tgbot.notify import notify_dual
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()


class GfwKeyBody(BaseModel):
    key: str = ""


class GfwToggleBody(BaseModel):
    instance_id: str = ""
    enabled: bool = False
    profile: str = ""
    region: str = ""
    name: str = ""
    ip: str = ""


def _notify_chat_id():
    """通知用的 chat_id（Bark 配置里记的 > 白名单第一个用户 ID）。"""
    chat_id = load_bark_config().get("chat_id")
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    return chat_id


def _check_now_thread():
    """check-now 的后台线程体：跑一轮监控，完事发汇总通知。"""
    token = bot_config.TELEGRAM_BOT_TOKEN  # Read the variable directly: empty when unconfigured, and the thread returns silently (get_token() raises RuntimeError, which does not belong in a background thread). 直接读变量：未配置时为空，线程静默返回（get_token() 会抛 RuntimeError，不适合后台线程）。
    chat_id = _notify_chat_id()
    if not token or not chat_id:
        return
    notify = lambda title, body: notify_dual(  # noqa: E731
        token, chat_id, title, body, group="gfw-check")
    results = run_monitor_cycle(notify)
    if not results:
        notify("🛡️ GFW 检查",
               "没有可检查的实例（未设置 API Key 或未开启任何实例）。")
        return
    lines = []
    for res in results:
        name = res.get("name") or res.get("instance_id") or "?"
        status = res.get("status")
        if status == "healthy":
            lines.append(f"✅ {name}：正常（{res.get('detail')}）")
        elif status == "recovered":
            lines.append(f"🔁 {name}：换了 {res.get('changes')} 次 IP 已恢复，"
                         f"现 IP {res.get('ip')}")
        elif status == "exhausted":
            lines.append(f"⚠️ {name}：换了 {res.get('changes')} 次仍被墙，"
                         f"已暂停，下个周期继续")
        elif status == "unknown":
            lines.append(f"❓ {name}：检测无结论（{res.get('detail')}）")
        elif status == "error":
            lines.append(f"❌ {name}：{res.get('detail')}")
        else:
            lines.append(f"⏭️ {name}：{res.get('detail')}")
    notify("🛡️ GFW 检查完成", "\n".join(lines))


@router.get("/api/gfw/status")
def gfw_status(user: str = Depends(get_current_user)):
    """GFW 监控状态（Key 脱敏显示，绝不返回原文）。"""
    cfg = load_gfw_config()
    return {"ok": True,
            "configured": is_gfw_configured(cfg),
            "key_masked": mask_api_key(cfg.get("api_key")),
            "enabled": cfg.get("enabled") or {},
            "last_check": cfg.get("last_check") or {},
            "interval_minutes": GFW_CHECK_INTERVAL_MINUTES}


@router.post("/api/gfw/key")
def gfw_set_key(body: GfwKeyBody, user: str = Depends(get_current_user)):
    """设置 Atlas API Key（宽松校验：去空白后至少 8 位且不含空白）。"""
    key = (body.key or "").strip()
    if len(key) < 8 or any(ch.isspace() for ch in key):
        return error_response("API Key 格式不正确（太短或含有空白字符）。",
                              status_code=400)
    masked = set_api_key(key)
    return {"ok": True, "key_masked": masked}


@router.get("/api/gfw/instances")
def gfw_instances(profile: str = Query("DEFAULT"),
                  region: str = Query(default=None),
                  user: str = Depends(get_current_user)):
    """该账号指定区域的 RUNNING 实例（带公网 IP），供开关列表用。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        instances = attach_public_ips(
            cli, list_instances(cli, compartment_id) or [])
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001 - OCI 错误统一转响应
        return oci_error_response(exc)
    running = [{
        "id": inst.get("id"),
        "display-name": inst.get("display-name"),
        "shape": inst.get("shape"),
        "public-ip": inst.get("public-ip"),
    } for inst in instances
        if inst.get("lifecycle-state") == "RUNNING"
        and inst.get("public-ip")]
    return {"ok": True, "profile": profile, "region": cli.region,
            "instances": running}


@router.post("/api/gfw/toggle")
def gfw_toggle(body: GfwToggleBody, user: str = Depends(get_current_user)):
    """开关某实例的 GFW 监控。"""
    if not body.instance_id:
        return error_response("缺少 instance_id。", status_code=400)
    cfg = set_instance_enabled(
        body.instance_id, body.enabled,
        profile=body.profile or None, region=body.region or None,
        name=body.name or None, ip=body.ip or None)
    return {"ok": True,
            "enabled": bool((cfg.get("enabled") or {}).get(body.instance_id))}


@router.post("/api/gfw/check-now")
def gfw_check_now(user: str = Depends(get_current_user)):
    """后台立即跑一轮 GFW 检查（接口立即返回，完成后发通知）。"""
    cfg = load_gfw_config()
    if not is_gfw_configured(cfg):
        return error_response("还没设置 RIPE Atlas API Key。",
                              status_code=400)
    if not cfg.get("enabled"):
        return error_response("还没开启任何实例的监控。",
                              status_code=400)
    thread = threading.Thread(target=_check_now_thread, daemon=True)
    thread.start()
    return {"ok": True, "started": True}
