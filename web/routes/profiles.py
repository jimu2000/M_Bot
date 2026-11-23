#!/usr/bin/env python3
"""账号（profile）列表路由。

  - GET /api/profiles：返回已配置的账号列表，供前端 profile 下拉动态填充。
    每个 profile 就是一个账号 + 区域（与 R-Bot 的模型一致：账号/区域切换
    就是切换 profile，没有独立的区域选择器）。

返回形状：{"ok": true, "default": "DEFAULT",
          "profiles": [{name, region, regionLabel, regionCount,
                        displayName}, ...]}。
regionLabel 是区域中文名（新加坡/圣何塞/芝加哥），未列出的区域返回原代码。
regionCount 是该账号订阅的区域数（查 iam region-subscription list，
缓存 1 小时；查失败时为 null）；displayName 是下拉显示名（主区中文名，
多区账号后缀“（N 个区）”，与机器人切换菜单一致）。
"""

import time

from fastapi import APIRouter, Depends

from web import config
from web.deps import get_current_user, resolve_profile

router = APIRouter()

# 订阅区域数缓存：region-subscription 很少变，缓存 1 小时，避免
# 每次打开页面都对每个账号调一次 OCI。
_REGION_COUNT_TTL = 3600
_region_counts = {}
_region_counts_at = 0


def get_region_counts():
    """返回 {profile: 订阅区域数}；单个账号查失败时跳过（不显示区数）。

    结果缓存 1 小时；缓存命中时直接返回副本。
    """
    global _region_counts, _region_counts_at
    now = time.time()
    if _region_counts and now - _region_counts_at < _REGION_COUNT_TTL:
        return dict(_region_counts)
    counts = {}
    for name in config.PROFILES:
        try:
            cli, compartment_id = resolve_profile(name)
            result = cli.run(["iam", "region-subscription", "list",
                              "--tenancy-id", compartment_id])
            data = result.get("data") if isinstance(result, dict) else None
            counts[name] = len(data) if isinstance(data, list) else 0
        except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
            continue
    _region_counts = counts
    _region_counts_at = now
    return dict(counts)


@router.get("/api/profiles")
def list_profiles(username: str = Depends(get_current_user)):
    """列出已配置的账号（profile）：名字、区域、区域中文名、订阅区数、显示名。"""
    counts = get_region_counts()
    profiles = [
        {
            "name": name,
            "region": cfg["region"],
            "regionLabel": config.get_region_label(cfg["region"]),
            "regionCount": counts.get(name),
            "displayName": config.profile_display_name(
                name, counts.get(name)),
        }
        for name, cfg in config.PROFILES.items()
    ]
    return {"ok": True, "default": config.DEFAULT_PROFILE,
            "profiles": profiles}
