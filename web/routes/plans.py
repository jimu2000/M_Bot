#!/usr/bin/env python3
"""抢机计划路由（定时抢机）。

  - GET /api/plans：计划列表。
  - POST /api/plans：新增计划（body 含 cron / shape / ocpus / memory_gb，
    可选 enabled 与 Grabber 完整参数；cron 非法返回 400）。
  - DELETE /api/plans/{id}：删除计划。
  - POST /api/plans/{id}/toggle：开关计划（body {enabled}）。

增/删/开关后都会同步 APScheduler（调度器运行中时）。
全部接口需登录（Depends(get_current_user)）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web.deps import error_response, get_current_user
from web.tasks import PLANS, refresh_plan_scheduler

router = APIRouter()


class PlanBody(BaseModel):
    """新增抢机计划请求体。

    cron：标准 5 段 crontab（如 "0 2 * * *" 每天 2:00）。
    shape：VM.Standard.E2.1.Micro 或 VM.Standard.A1.Flex。
    ocpus / memory_gb：目标配置（A1 免费额度内：1-4 核 / 1-24GB）。
    其余字段可选，缺了用 web/tasks.py 的 PLAN_DEFAULTS。
    """

    cron: str
    shape: str
    ocpus: int
    memory_gb: int
    enabled: bool = True
    profile: str = None
    availability_domain: str = None
    image_id: str = None
    subnet_id: str = None
    display_name: str = None
    ssh_keys_file: str = None
    target_cpu: str = None


class ToggleBody(BaseModel):
    """开关计划请求体。"""

    enabled: bool


@router.get("/api/plans")
def list_plans(username: str = Depends(get_current_user)):
    """抢机计划列表。"""
    return {"ok": True, "plans": PLANS.list()}


@router.post("/api/plans", status_code=201)
def create_plan(body: PlanBody,
                username: str = Depends(get_current_user)):
    """新增抢机计划（cron 非法或配置超范围时返回 400）。"""
    try:
        plan = PLANS.add(
            body.cron, body.shape, body.ocpus, body.memory_gb,
            enabled=body.enabled,
            profile=body.profile,
            availability_domain=body.availability_domain,
            image_id=body.image_id,
            subnet_id=body.subnet_id,
            display_name=body.display_name,
            ssh_keys_file=body.ssh_keys_file,
            target_cpu=body.target_cpu)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    refresh_plan_scheduler()
    return {"ok": True, "plan": plan}


@router.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: str,
                username: str = Depends(get_current_user)):
    """删除抢机计划。"""
    if not PLANS.delete(plan_id):
        return error_response(f"抢机计划不存在：{plan_id}", status_code=404)
    refresh_plan_scheduler()
    return {"ok": True, "plan_id": plan_id}


@router.post("/api/plans/{plan_id}/toggle")
def toggle_plan(plan_id: str, body: ToggleBody,
                username: str = Depends(get_current_user)):
    """开关抢机计划。"""
    plan = PLANS.set_enabled(plan_id, body.enabled)
    if plan is None:
        return error_response(f"抢机计划不存在：{plan_id}", status_code=404)
    refresh_plan_scheduler()
    return {"ok": True, "plan": plan}
