#!/usr/bin/env python3
"""配额路由：免费额度汇总 / 能否再开一台 / A1 配置体检 / A1 配额。

  - GET /api/quota：A1 / E2 / 启动盘三项免费额度汇总。
  - GET /api/quota/a1：A1 配额（面板形状，cap 用免费上限 4 核/24GB）。
  - GET /api/quota/a1-health：A1 配置体检（超限检查 + 降配建议）。
  - POST /api/quota/can-launch：body {shape, ocpus, memory_gb}，
    判断能不能再开一台指定规格的实例。
"""

import threading
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.client import OCIError
from oci.quota import (
    a1_health_check,
    can_launch,
    get_a1_usage,
    get_free_tier_summary,
)
from web import config
from web import demo_data
from web.oci_cache import cached_raw_boot_volumes, cached_raw_instances
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()

# ================= 配置区（改这里） =================
# Quota server cache TTL (seconds): each quota call runs several OCI CLI calls (instance list + boot-volume list per call), several seconds serially; repeat visits within 30s return the cache, and a manual refresh (refresh=1) bypasses it. 配额服务端缓存 TTL（秒）：每次配额查询要跑多次 OCI CLI（实例列表 + 启动盘列表），串行几秒；30 秒内重复访问直接返回缓存，手动刷新（refresh=1）绕过缓存。
QUOTA_CACHE_TTL = 30
# =====================================================

# Quota cache: (profile, region, kind[, ...]) -> {"expires": monotonic-clock expiry, "payload": response body}. Memory only, never written to disk; the panel has a single admin, so no per-user isolation is needed. 配额缓存：(profile, region, 种类[, ...]) -> {"expires": 单调时钟过期点, "payload": 响应体}。只放内存不写盘；面板是单管理员，无需按用户隔离。
_QUOTA_CACHE = {}
_QUOTA_CACHE_LOCK = threading.Lock()


def clear_quota_cache(profile=None):
    """清配额缓存：指定 profile 时只清该账号，否则全清。

    配额由实例（RUNNING 数量/规格）与启动盘（总 GB）算出，实例/启动盘
    写操作（启停/终止/改规格/扩容/删盘/抢机完成/备份恢复等）成功后调用，
    避免配额页显示旧用量；单元测试在 setUp 里调用，避免缓存跨测试串数据。

    顺带清 Web 层共享 OCI 数据缓存（web/oci_cache.py）：它存的是同一批
    实例/启动盘原始列表，写操作后同样过期；只清配额响应缓存会漏掉它，
    面板仍会显示旧实例/旧磁盘。
    """
    from web.oci_cache import clear_oci_cache  # noqa: PLC0415 - 避免循环导入
    clear_oci_cache(profile)
    with _QUOTA_CACHE_LOCK:
        if profile is None:
            _QUOTA_CACHE.clear()
        else:
            for key in [k for k in _QUOTA_CACHE if k[0] == profile]:
                del _QUOTA_CACHE[key]


def _cache_get(key, refresh):
    """读缓存：refresh 时跳过；命中且未过期返回 payload，否则返回 None。"""
    if refresh:
        return None
    with _QUOTA_CACHE_LOCK:
        entry = _QUOTA_CACHE.get(key)
    if entry and time.monotonic() < entry["expires"]:
        return entry["payload"]
    return None


def _cache_put(key, payload):
    """写缓存（只缓存成功响应，错误不缓存）。"""
    with _QUOTA_CACHE_LOCK:
        _QUOTA_CACHE[key] = {
            "expires": time.monotonic() + QUOTA_CACHE_TTL,
            "payload": payload,
        }


class CanLaunchBody(BaseModel):
    """can-launch 请求体：ocpus / memory_gb 只对 A1.Flex 有意义。"""

    shape: str
    ocpus: int = 0
    memory_gb: int = 0


@router.get("/api/quota")
def quota_summary(profile: str = Query(default=None),
                  region: str = Query(default=None),
                  refresh: bool = Query(default=False),
                  username: str = Depends(get_current_user)):
    """免费额度汇总（A1 / E2 / 启动盘）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated quota, computed from the demo data.
        # 演示模式：模拟配额，按模拟数据算出。
        try:
            return demo_data.demo_quota_summary(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region, "summary")
        cached = _cache_get(cache_key, refresh)
        if cached is not None:
            return cached
        summary = get_free_tier_summary(
            cli, compartment_id,
            instances=cached_raw_instances(cli, compartment_id,
                                           refresh=refresh),
            volumes=cached_raw_boot_volumes(cli, compartment_id,
                                            refresh=refresh))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True,
               "profile": profile_name,
               # Display name (home-region display name; the quota page uses it instead of the config key name). 显示名（主区中文名；配额页用它代替配置键名）。
               "profileLabel": config.profile_display_name(profile_name),
               "summary": summary}
    _cache_put(cache_key, payload)
    return payload


def format_a1_quota(usage):
    """把 get_a1_usage 的结果转成 面板形状（阶段 5 工作包 E）。

    {capOcpu, capMem, usedOcpu, usedMem, over, overOcpu, overMem,
     instances: [{id, name, ocpus, memoryInGBs, state}]}。
    cap 用免费上限（4 核/24GB），不要把 cap 显示成当前用量；
    over 为 True 当且仅当 OCPU 或内存任一项超限；overOcpu/overMem
    是超出的量（未超限时为 0）。instances 只含 RUNNING 的 A1 实例
    （get_a1_usage 本来就只统计 RUNNING），state 恒为 "RUNNING"。
    """
    cap_ocpu = usage.get("ocpu_limit") or 0
    cap_mem = usage.get("memory_limit_gb") or 0
    used_ocpu = usage.get("ocpu_used") or 0
    used_mem = usage.get("memory_used_gb") or 0
    over_ocpu = max(0, used_ocpu - cap_ocpu)
    over_mem = max(0, used_mem - cap_mem)
    return {
        "capOcpu": cap_ocpu,
        "capMem": cap_mem,
        "usedOcpu": used_ocpu,
        "usedMem": used_mem,
        "over": over_ocpu > 0 or over_mem > 0,
        "overOcpu": over_ocpu,
        "overMem": over_mem,
        "instances": [{
            "id": inst.get("id"),
            "name": inst.get("display_name"),
            "ocpus": inst.get("ocpus"),
            "memoryInGBs": inst.get("memory_gb"),
            "state": "RUNNING",
        } for inst in usage.get("instances") or []],
    }


@router.get("/api/quota/a1")
def a1_quota(profile: str = Query(default=None),
             region: str = Query(default=None),
             refresh: bool = Query(default=False),
             username: str = Depends(get_current_user)):
    """A1 配额（面板形状：cap 为免费上限 4 核/24GB）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated A1 usage, computed from the demo data.
        # 演示模式：模拟 A1 用量，按模拟数据算出。
        try:
            return demo_data.demo_a1_quota(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region, "a1")
        cached = _cache_get(cache_key, refresh)
        if cached is not None:
            return cached
        usage = get_a1_usage(
            cli, compartment_id,
            instances=cached_raw_instances(cli, compartment_id,
                                           refresh=refresh))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True,
               "profile": profile_name,
               "a1": format_a1_quota(usage)}
    _cache_put(cache_key, payload)
    return payload


@router.get("/api/quota/a1-health")
def a1_health(profile: str = Query(default=None),
              region: str = Query(default=None),
              refresh: bool = Query(default=False),
              username: str = Depends(get_current_user)):
    """A1 配置体检：超限检查 + 降配建议 + 启动盘检查。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated health check, computed from the demo data.
        # 演示模式：模拟体检，按模拟数据算出。
        try:
            return demo_data.demo_a1_health(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region, "a1-health")
        cached = _cache_get(cache_key, refresh)
        if cached is not None:
            return cached
        result = a1_health_check(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True,
               "profile": profile_name,
               "health": result}
    _cache_put(cache_key, payload)
    return payload


@router.post("/api/quota/can-launch")
def can_launch_endpoint(body: CanLaunchBody,
                        profile: str = Query(default=None),
                        region: str = Query(default=None),
                        refresh: bool = Query(default=False),
                        username: str = Depends(get_current_user)):
    """判断能不能再开一台指定规格的实例（不通过时返回原因）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: same launch rules, evaluated against the demo data.
        # 演示模式：同一套能否开机规则，按模拟数据判断。
        try:
            ok, reason = demo_data.demo_can_launch(
                profile, region, body.shape,
                ocpus=body.ocpus, memory_gb=body.memory_gb)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        return {"ok": True, "can_launch": ok, "reason": reason}
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region, "can-launch",
                     body.shape, body.ocpus, body.memory_gb)
        cached = _cache_get(cache_key, refresh)
        if cached is not None:
            return cached
        ok, reason = can_launch(cli, compartment_id, body.shape,
                                ocpus=body.ocpus, memory_gb=body.memory_gb)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True, "can_launch": ok, "reason": reason}
    _cache_put(cache_key, payload)
    return payload
