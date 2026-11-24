#!/usr/bin/env python3
"""配额路由：免费额度汇总 / 能否再开一台 / A1 配置体检 / A1 配额。

  - GET /api/quota：A1 / E2 / 启动盘三项免费额度汇总。
  - GET /api/quota/a1：A1 配额（面板形状，cap 用免费上限 4 核/24GB）。
  - GET /api/quota/a1-health：A1 配置体检（超限检查 + 降配建议）。
  - POST /api/quota/can-launch：body {shape, ocpus, memory_gb}，
    判断能不能再开一台指定规格的实例。
"""

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
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()


class CanLaunchBody(BaseModel):
    """can-launch 请求体：ocpus / memory_gb 只对 A1.Flex 有意义。"""

    shape: str
    ocpus: int = 0
    memory_gb: int = 0


@router.get("/api/quota")
def quota_summary(profile: str = Query(default=None),
                  region: str = Query(default=None),
                  username: str = Depends(get_current_user)):
    """免费额度汇总（A1 / E2 / 启动盘）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        summary = get_free_tier_summary(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    profile_name = profile or config.DEFAULT_PROFILE
    return {"ok": True,
            "profile": profile_name,
            # 显示名（主区中文名；v1.15.0 新增，配额页用它代替配置键名）。
            "profileLabel": config.profile_display_name(profile_name),
            "summary": summary}


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
             username: str = Depends(get_current_user)):
    """A1 配额（面板形状：cap 为免费上限 4 核/24GB）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        usage = get_a1_usage(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "a1": format_a1_quota(usage)}


@router.get("/api/quota/a1-health")
def a1_health(profile: str = Query(default=None),
              region: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """A1 配置体检：超限检查 + 降配建议 + 启动盘检查。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = a1_health_check(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "health": result}


@router.post("/api/quota/can-launch")
def can_launch_endpoint(body: CanLaunchBody,
                        profile: str = Query(default=None),
                        region: str = Query(default=None),
                        username: str = Depends(get_current_user)):
    """判断能不能再开一台指定规格的实例（不通过时返回原因）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        ok, reason = can_launch(cli, compartment_id, body.shape,
                                ocpus=body.ocpus, memory_gb=body.memory_gb)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "can_launch": ok, "reason": reason}
