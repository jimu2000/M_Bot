#!/usr/bin/env python3
"""账号（profile）列表路由。

  - GET /api/profiles：返回已配置的账号列表，供前端 profile 下拉动态填充。
    每个 profile 就是一个账号 + 区域（与 R-Bot 的模型一致：账号/区域切换
    就是切换 profile，没有独立的区域选择器）。

返回形状：{"ok": true, "default": "DEFAULT",
          "profiles": [{name, region, regionLabel}, ...]}。
regionLabel 是区域中文名（新加坡/圣何塞/芝加哥），未列出的区域返回原代码。
"""

from fastapi import APIRouter, Depends

from web import config
from web.deps import get_current_user

router = APIRouter()


@router.get("/api/profiles")
def list_profiles(username: str = Depends(get_current_user)):
    """列出已配置的账号（profile）：名字、区域、区域中文名。"""
    profiles = [
        {
            "name": name,
            "region": cfg["region"],
            "regionLabel": config.get_region_label(cfg["region"]),
        }
        for name, cfg in config.PROFILES.items()
    ]
    return {"ok": True, "default": config.DEFAULT_PROFILE,
            "profiles": profiles}
