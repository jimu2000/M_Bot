#!/usr/bin/env python3
"""云报告汇总路由（阶段 5 工作包 E）。

  - GET /api/cloud-report/summary：汇总各账号（各 profile）的实例数
    （按状态分）、磁盘总数与 GB、A1/E2 配额用量，返回 R-Bot 形状
    {groups: [{profile, region, instances, volumes, quota}],
     generatedAtEpochSec}。

某账号查询失败时该 group 带 error 字段继续汇总，不让单个账号
拖垮整份报告。
"""

import time

from fastapi import APIRouter, Depends

from oci.client import OCIClient, OCIError
from oci.instance import list_instances
from oci.quota import get_a1_usage, get_e2_usage
from oci.volume import list_boot_volumes
from web import config
from web.deps import get_current_user

router = APIRouter()

# 不占免费额度的启动盘状态（与 oci.volume.total_boot_volume_gb 一致）。
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


def build_cloud_report():
    """汇总全部已配置账号，返回 {groups, generatedAtEpochSec}。"""
    groups = []
    for profile in config.PROFILES:
        try:
            groups.append(summarize_profile(profile))
        except (OCIError, ValueError) as exc:
            groups.append({"profile": profile, "error": str(exc)})
    return {"groups": groups, "generatedAtEpochSec": int(time.time())}


@router.get("/api/cloud-report/summary")
def cloud_report_summary(username: str = Depends(get_current_user)):
    """返回各账号的云报告汇总（R-Bot 形状）。"""
    return {"ok": True, **build_cloud_report()}
