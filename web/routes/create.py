#!/usr/bin/env python3
"""创建实例路由 + 任务路由。

  - POST /api/create：先 can_launch 预检，不通过返回 400；通过则建
    任务、后台线程跑 Grabber 抢机，返回 {task_id}。
  - GET /api/tasks：任务列表。
  - GET /api/tasks/{id}：单个任务（含日志与结果）。
  - POST /api/tasks/{id}/cancel：取消任务。

注意：A1.Flex 的 ocpus / memory_gb 透传给 Grabber，以 --shape-config
传给 launch（E2.1.Micro 是固定规格，Grabber 不传 shape-config）；
boot_volume_size_gbs / assign_public_ip / assign_ipv6 / min_delay /
max_delay / user_data 也全部透传给 Grabber（见 oci/grabber.py）。

镜像解析：前端创建表单下拉传的是架构占位符
（ocid1.image.oc1..xxxxxxxx-aarch64 / -x86），resolve_image_id()
按 web/config.py 的 IMAGE_IDS 表解析成真实镜像 OCID；部署时必须
把 IMAGE_IDS 填成真实值，否则创建会报 400 提示配置镜像。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from oci.client import OCIError
from oci.quota import can_launch, check_a1_single_instance_cap
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.tasks import TASKS, start_grab_task

router = APIRouter()

# 前端镜像下拉的架构占位符后缀 -> IMAGE_IDS 的键。
_IMAGE_PLACEHOLDER_SUFFIX = {
    "-aarch64": "aarch64",
    "-x86": "x86_64",
}


def resolve_image_id(image_id):
    """把前端传来的镜像值解析成真实镜像 OCID。

    - 架构占位符（如 ocid1.image.oc1..xxxxxxxx-aarch64）：按
      config.IMAGE_IDS 查表；表里仍是占位符时抛 ValueError 提示配置。
    - 其他值（部署时直接传的真实 OCID）：原样返回。
    """
    for suffix, arch in _IMAGE_PLACEHOLDER_SUFFIX.items():
        if image_id.endswith(suffix):
            real_id = config.IMAGE_IDS.get(arch, "")
            if not real_id or "xxxxxxxx" in real_id:
                raise ValueError(
                    f"未配置 {arch} 架构的真实镜像 OCID：请在 "
                    "web/config.py 的 IMAGE_IDS 里填入真实值后再创建。")
            return real_id
    if "xxxxxxxx" in image_id:
        raise ValueError(
            "镜像 OCID 是占位符：请在 web/config.py 的 IMAGE_IDS 里"
            "填入真实镜像 OCID 后再创建。")
    return image_id


class CreateBody(BaseModel):
    """创建实例请求体。

    boot_volume_size_gbs: 引导卷大小（GB），OCI 要求 47-200，超范围 422。
    min_delay / max_delay: 抢机延迟区间（秒），0 表示不指定（用 Grabber
        默认 25 +/- 5 秒）；max < min 时路由返回 400。
    """

    profile: str = None
    shape: str
    ocpus: int = 0
    memory_gb: int = 0
    image_id: str
    subnet_id: str
    availability_domain: str
    display_name: str
    ssh_keys_file: str
    target_cpu: str = None
    boot_volume_size_gbs: int = Field(default=47, ge=47, le=200)
    assign_public_ip: bool = True
    assign_ipv6: bool = False
    min_delay: int = Field(default=0, ge=0)
    max_delay: int = Field(default=0, ge=0)
    user_data: str = ""


@router.post("/api/create")
def create_instance(body: CreateBody,
                    username: str = Depends(get_current_user)):
    """创建实例（抢机）：配额预检通过后在后台线程跑 Grabber。"""
    if body.max_delay < body.min_delay:
        return error_response(
            f"最大延迟（{body.max_delay} 秒）不能小于最小延迟"
            f"（{body.min_delay} 秒）", status_code=400)
    if body.shape == "VM.Standard.A1.Flex":
        cap_error = check_a1_single_instance_cap(body.ocpus,
                                                 body.memory_gb)
        if cap_error:
            return error_response(cap_error, status_code=400)
    try:
        cli, compartment_id = resolve_profile(body.profile)
        ok, reason = can_launch(cli, compartment_id, body.shape,
                                ocpus=body.ocpus, memory_gb=body.memory_gb)
        if not ok:
            return error_response(f"配额预检不通过：{reason}", status_code=400)
        image_id = resolve_image_id(body.image_id)
        params = {
            "availability_domain": body.availability_domain,
            "shape": body.shape,
            "ocpus": body.ocpus,
            "memory_gb": body.memory_gb,
            "image_id": image_id,
            "subnet_id": body.subnet_id,
            "display_name": body.display_name,
            "ssh_keys_file": body.ssh_keys_file,
            "target_cpu": body.target_cpu,
            "boot_volume_size_gbs": body.boot_volume_size_gbs,
            "assign_public_ip": body.assign_public_ip,
            "assign_ipv6": body.assign_ipv6,
            "min_delay": body.min_delay,
            "max_delay": body.max_delay,
            "user_data": body.user_data,
        }
        task_id = start_grab_task(TASKS, cli, compartment_id, params)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "task_id": task_id}


@router.get("/api/tasks")
def list_tasks(username: str = Depends(get_current_user)):
    """任务列表。"""
    return {"ok": True, "tasks": TASKS.list()}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, username: str = Depends(get_current_user)):
    """单个任务详情（含日志与结果）。"""
    task = TASKS.get(task_id)
    if task is None:
        return error_response(f"任务不存在：{task_id}", status_code=404)
    return {"ok": True, "task": task}


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, username: str = Depends(get_current_user)):
    """取消任务（只对 pending/running 的任务有效）。"""
    if TASKS.get(task_id) is None:
        return error_response(f"任务不存在：{task_id}", status_code=404)
    if not TASKS.cancel(task_id):
        return error_response("任务已结束，无法取消", status_code=400)
    return {"ok": True, "task_id": task_id}
