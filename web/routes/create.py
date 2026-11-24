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
from tgbot import config as bot_config
from tgbot.bark import load_bark_config
from tgbot.boot_form import build_user_data, generate_root_password
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
    root_login: 是否用 cloud-init 设 root 密码（默认开启；用户要求建完
        实例一定能 root 登录）。开启且 root_password 为空时后端生成
        UUID 密码；密码随响应返回一次（前端弹窗展示），并在抢机成功
        的双通道通知里带上，不写进任务详情。
    user_data: 开机脚本原文；与 root 密码共存时拼进同一份 cloud-init，
        脚本本身是 #cloud-config 时与密码冲突，返回 400。
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
    root_login: bool = True
    root_password: str = ""


def _notify_chat_id():
    """通知用的 chat_id：Bark 配置里记的 > 白名单第一个用户 ID。"""
    chat_id = load_bark_config().get("chat_id")
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    return chat_id


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
        # root 密码：开启时为空则后端生成 UUID；与开机脚本拼进同一份
        # cloud-init（脚本本身是 #cloud-config 时冲突，报 400）。
        root_password = None
        if body.root_login:
            root_password = body.root_password.strip() or \
                generate_root_password()
        # 脚本原文透传（只判断是否为空白，不 strip 内容）。
        script = body.user_data or None
        if script is not None and not script.strip():
            script = None
        user_data = None
        if root_password:
            # 密码与脚本拼进同一份 cloud-init（脚本本身是 #cloud-config
            # 时冲突，build_user_data 抛 ValueError -> 400）。
            user_data = build_user_data(root_password, script)
        else:
            # 不设 root 密码时脚本原文直通 Grabber（旧行为，不做归一化）。
            user_data = script
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
            "user_data": user_data,
        }
        # 双通道通知信息（只在内存里传给线程：密码不落盘，通知完即丢）。
        profile_name = body.profile or config.DEFAULT_PROFILE
        region = config.get_region(profile_name)
        notify = {
            "token": bot_config.TELEGRAM_BOT_TOKEN,
            "chat_id": _notify_chat_id(),
            "profile_label": bot_config.profile_display_name(profile_name),
            "region_label": bot_config.get_region_label(region),
            "region_code": region,
            "root_password": root_password,
        }
        task_id = start_grab_task(TASKS, cli, compartment_id, params,
                                  notify=notify)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    # root 密码随响应返回一次（前端弹窗展示一次），之后不再可查。
    return {"ok": True, "task_id": task_id,
            "root_password": root_password}


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
