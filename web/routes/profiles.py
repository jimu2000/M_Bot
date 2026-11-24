#!/usr/bin/env python3
"""账号（profile）列表路由。

  - GET /api/profiles：返回已配置的账号列表，供前端 profile 下拉动态填充。
    每个 profile 就是一个账号；多区订阅的账号带 regions 列表（订阅的全部
    区域，主区排第一），前端据此显示二级区域下拉（单区账号不显示）。

返回形状：{"ok": true, "default": "DEFAULT",
          "profiles": [{name, region, regionLabel, regionCount, regions,
                        displayName}, ...]}。
regionLabel 是区域中文名（新加坡/圣何塞/芝加哥），未列出的区域返回原代码。
regionCount 是该账号订阅的区域数（查 iam region-subscription list，
缓存 1 小时；查失败时为 null）；regions 是订阅区域列表（{code, label,
isHome}，主区排第一，其余按代码排序；订阅列表查失败时只含主区）；
displayName 是下拉显示名（主区中文名，多区账号后缀“（N 个区）”，与
机器人切换菜单一致）。
"""

from fastapi import APIRouter, Depends

from web import config
from web.deps import (
    get_current_user,
    get_profile_regions,
    get_region_counts,
)

router = APIRouter()


def _ordered_regions(name, home_region, codes):
    """订阅区域列表（主区排第一，其余按代码排序）；查不到订阅列表时
    只返回主区（前端按单区处理，不显示二级菜单）。"""
    seen = {home_region}
    ordered = [home_region]
    for code in sorted(codes or []):
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    return [{"code": code,
             "label": config.get_region_label(code),
             "isHome": code == home_region} for code in ordered]


@router.get("/api/profiles")
def list_profiles(username: str = Depends(get_current_user)):
    """列出已配置的账号（profile）：名字、区域、区域中文名、订阅区数、
    订阅区域列表、显示名。"""
    counts = get_region_counts()
    regions = get_profile_regions()
    profiles = [
        {
            "name": name,
            "region": cfg["region"],
            "regionLabel": config.get_region_label(cfg["region"]),
            "regionCount": counts.get(name),
            "regions": _ordered_regions(name, cfg["region"],
                                        regions.get(name)),
            "displayName": config.profile_display_name(
                name, counts.get(name)),
        }
        for name, cfg in config.PROFILES.items()
    ]
    return {"ok": True, "default": config.DEFAULT_PROFILE,
            "profiles": profiles}
