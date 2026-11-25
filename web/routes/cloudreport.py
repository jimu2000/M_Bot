#!/usr/bin/env python3
"""云报告汇总路由（阶段 5 工作包 E）。

  - GET /api/cloud-report/summary：汇总各账号（各 profile）的实例数
    （按状态分）、磁盘总数与 GB、A1/E2 配额用量，返回 面板形状
    {groups: [{profile, region, instances, volumes, quota}],
     generatedAtEpochSec}。

某账号查询失败时该 group 带 error 字段继续汇总，不让单个账号
拖垮整份报告。

性能：各账号串行汇总要跑 12 次 OCI CLI（每账号 4 次），共 30 秒以上；
现各账号并行汇总（线程池），首次约一个账号的耗时；结果服务端缓存
60 秒，重复访问直接返回，手动刷新时传 refresh=1 绕过缓存。
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Query

from oci.client import OCIClient, OCIError
from oci.instance import list_instances
from oci.quota import get_a1_usage, get_e2_usage
from oci.volume import list_boot_volumes
from web import config
from web.deps import get_current_user

router = APIRouter()

# ================= 配置区（改这里） =================
# Cloud report server cache TTL (seconds): summarizing runs several OCI CLI calls, so repeat visits within 60s return the cache; a manual refresh (refresh=1) bypasses it. 云报告服务端缓存 TTL（秒）：汇总要跑多次 OCI CLI，60 秒内重复访问直接返回缓存；手动刷新（refresh=1）绕过缓存。
CLOUD_REPORT_CACHE_TTL = 60
# =====================================================

# Cloud report cache: a single {"expires": monotonic-clock expiry, "payload": response body}. Memory only, never written to disk; the panel has a single admin, so no per-user isolation is needed. 云报告缓存：单条 {"expires": 单调时钟过期点, "payload": 响应体}。只放内存不写盘；面板是单管理员，无需按用户隔离。
_REPORT_CACHE = {}
_REPORT_CACHE_LOCK = threading.Lock()


def clear_cloud_report_cache():
    """清云报告缓存（单元测试在 setUp 里调用，避免缓存跨测试串数据）。"""
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE.clear()

# Boot volume states that do not use free quota (same as oci.volume.total_boot_volume_gb). 不占免费额度的启动盘状态（与 oci.volume.total_boot_volume_gb 一致）。
_EXCLUDED_VOLUME_STATES = ("TERMINATING", "TERMINATED")


def summarize_profile(profile):
    """汇总单个账号（profile）的实例 / 磁盘 / 配额。

    返回 group 字典；OCI 调用失败时抛 OCIError（调用方转成 error group）。
    """
    cfg = config.get_profile_config(profile)
    cli = OCIClient(profile=profile, region=cfg["region"])
    compartment_id = cfg["compartment_id"]

    instances = list_instances(cli, compartment_id)
    by_state = {}
    for inst in instances:
        state = inst.get("lifecycle-state") or "UNKNOWN"
        by_state[state] = by_state.get(state, 0) + 1

    volumes = list_boot_volumes(cli, compartment_id)
    live = [v for v in volumes
            if v.get("lifecycle-state") not in _EXCLUDED_VOLUME_STATES]
    total_gb = sum(v.get("size-in-gbs") or 0 for v in live)

    a1 = get_a1_usage(cli, compartment_id)
    e2 = get_e2_usage(cli, compartment_id)

    return {
        "profile": profile,
        # Display name (home-region display name; the overview page uses it instead of the config key name). 显示名（主区中文名；总览页用它代替配置键名）。
        "profileLabel": config.profile_display_name(profile),
        "region": cfg["region"],
        "instances": {"total": len(instances), "byState": by_state},
        "volumes": {"count": len(live), "totalGB": total_gb},
        "quota": {
            "a1": {
                "usedOcpu": a1["ocpu_used"],
                "capOcpu": a1["ocpu_limit"],
                "usedMem": a1["memory_used_gb"],
                "capMem": a1["memory_limit_gb"],
            },
            "e2": {"count": e2["count"], "limit": e2["limit"]},
        },
    }


def _summarize_one(profile):
    """汇总单个账号，失败时返回带 error 的 group（不抛异常）。"""
    try:
        return summarize_profile(profile)
    except (OCIError, ValueError) as exc:
        return {"profile": profile,
                "profileLabel": config.profile_display_name(profile),
                "error": str(exc)}


def build_cloud_report():
    """汇总全部已配置账号，返回 {groups, generatedAtEpochSec}。

    各账号并行汇总（线程池；map 保序，groups 顺序与 PROFILES 一致）。
    """
    profiles = list(config.PROFILES)
    if not profiles:
        groups = []
    elif len(profiles) == 1:
        groups = [_summarize_one(profiles[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(profiles)) as pool:
            groups = list(pool.map(_summarize_one, profiles))
    return {"groups": groups, "generatedAtEpochSec": int(time.time())}


@router.get("/api/cloud-report/summary")
def cloud_report_summary(refresh: bool = Query(default=False),
                         username: str = Depends(get_current_user)):
    """返回各账号的云报告汇总（面板形状）。

    refresh=true：绕过服务端缓存，强制重新汇总（前端刷新按钮用）。
    """
    if not refresh:
        with _REPORT_CACHE_LOCK:
            entry = _REPORT_CACHE.get("summary")
        if entry and time.monotonic() < entry["expires"]:
            return entry["payload"]
    payload = {"ok": True, **build_cloud_report()}
    with _REPORT_CACHE_LOCK:
        _REPORT_CACHE["summary"] = {
            "expires": time.monotonic() + CLOUD_REPORT_CACHE_TTL,
            "payload": payload,
        }
    return payload
