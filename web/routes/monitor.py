#!/usr/bin/env python3
"""监控规则路由（阶段 5 工作包 E）。

  - GET /api/monitor/rules：返回 面板形状
    {monitorEnabled, autoOpenEnabled, rules}。

开关复用 Telegram 机器人侧已有的监控配置（tgbot/monitoring.py）：
  - monitorEnabled：流量守护已开启，或任一实例开了停机保活；
  - autoOpenEnabled：任一实例开了停机保活（停机自动开机）。
rules 暂为空列表（细粒度规则尚未实现，见 docs/phase5-todo.md）。
"""

from fastapi import APIRouter, Depends

from tgbot.monitoring import load_keepalive, load_traffic_guard
from web.deps import get_current_user

router = APIRouter()


def get_monitor_rules():
    """汇总监控开关（读 tgbot 侧配置，读失败时按全部关闭处理）。"""
    try:
        guard = load_traffic_guard()
    except Exception:  # noqa: BLE001 - 配置损坏时降级为全关，不报错
        guard = {}
    try:
        keepalive = load_keepalive()
    except Exception:  # noqa: BLE001 - 同上
        keepalive = {}
    if not isinstance(guard, dict):
        guard = {}
    if not isinstance(keepalive, dict):
        keepalive = {}
    auto_open = any(bool(value) for value in keepalive.values())
    monitor_enabled = bool(guard.get("enabled")) or auto_open
    return {
        "monitorEnabled": monitor_enabled,
        "autoOpenEnabled": auto_open,
        "rules": [],
    }


@router.get("/api/monitor/rules")
def monitor_rules(username: str = Depends(get_current_user)):
    """返回监控规则（面板形状；rules 暂为空列表）。"""
    return {"ok": True, **get_monitor_rules()}
