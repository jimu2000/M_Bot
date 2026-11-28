#!/usr/bin/env python3
"""创建实例路由 + 任务路由。

  - GET /api/create/defaults：创建表单的网络默认值（可用域 / 子网 /
    SSH 公钥文件），按 profile + region 从部署配置里取（与机器人开机
    表单同一套 get_deploy_defaults：环境变量 MBOT_PLAN_* 是主区默认
    值，region_plans.json 里有该 (profile, region) 配置时用该区域
    的；region 不传时用该 profile 的主区）。SSH 公钥文件只在服务器
    上真实存在时才返回路径，没有就不填；可用域/子网未配置时返回空串。
  - POST /api/create：先 can_launch 预检，不通过返回 400；通过则建
    任务、后台线程跑 Grabber 抢机，返回 {task_id}。
  - GET /api/tasks：任务列表。
  - GET /api/tasks/{id}：单个任务（含日志与结果）。
  - POST /api/tasks/{id}/cancel：取消任务。

注意：A1.Flex 的 ocpus / memory_gb 透传给 Grabber，以 --shape-config
传给 launch（E2.1.Micro 是固定规格，Grabber 不传 shape-config）；
boot_volume_size_gbs / assign_public_ip / assign_ipv6 / min_delay /
max_delay / user_data 也全部透传给 Grabber（见 oci/grabber.py）。
ssh_keys_file 是可选项：为空时 Grabber 不传 --ssh-authorized-keys-file
（root 密码登录默认开启，建完实例仍能 root 登录）。

镜像解析：前端创建表单下拉传的是架构占位符
（ocid1.image.oc1..xxxxxxxx-aarch64 / -x86），resolve_image_id()
按 web/config.py 的 IMAGE_IDS 表解析成真实镜像 OCID；部署时必须
把 IMAGE_IDS 填成真实值，否则创建会报 400 提示配置镜像。
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from oci.client import OCIError
from oci.instance import get_image_arch
from oci.quota import can_launch, check_a1_single_instance_cap
from tgbot import config as bot_config
from tgbot.bark import load_bark_config
from tgbot.boot_form import (
    AD_PLACEHOLDER,
    SSH_KEYS_PLACEHOLDER,
    SUBNET_PLACEHOLDER,
    build_user_data,
    generate_root_password,
    get_deploy_defaults,
)
from web import config
from web import demo_data
from web.deps import (
    effective_region,
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.tasks import TASKS, start_grab_task

router = APIRouter()

# Frontend image dropdown arch placeholder suffix -> key in IMAGE_IDS. 前端镜像下拉的架构占位符后缀 -> IMAGE_IDS 的键。
_IMAGE_PLACEHOLDER_SUFFIX = {
    "-aarch64": "aarch64",
    "-x86": "x86_64",
}

# Shape -> required image architecture (only the two free shapes are supported). 形状 -> 要求的镜像架构（只支持两种免费形状）。
_SHAPE_ARCH = {
    "VM.Standard.A1.Flex": "aarch64",
    "VM.Standard.E2.1.Micro": "x86_64",
}

# Shape to suggest when the image architecture matches the other shape. 镜像架构对上另一个形状时，建议换成的形状。
_ARCH_SHAPE = {
    "aarch64": "VM.Standard.A1.Flex",
    "x86_64": "VM.Standard.E2.1.Micro",
}


def check_image_shape_match(shape, image_arch):
    """镜像架构与形状事前校验（ARM 全功能测试 Bug 6）。

    image_arch 是已知的镜像架构（占位符取自后缀，真实 OCID 由
    get_image_arch 查 display-name 推断）；推断不出（None）或形状
    不在免费形状表里时不校验、不阻塞。不匹配时抛 ValueError，
    路由转成 400 明文报错，不等抢机任务跑到 OCI 才报 InvalidParameter。
    """
    required_arch = _SHAPE_ARCH.get(shape)
    if not image_arch or not required_arch or image_arch == required_arch:
        return
    arch_label = "ARM" if image_arch == "aarch64" else "x86"
    raise ValueError(
        f"镜像架构与形状不匹配：镜像是 {image_arch}（{arch_label}），"
        f"形状 {shape} 需要 {required_arch} 镜像。"
        f"请换成 {required_arch} 镜像，或把形状改成 "
        f"{_ARCH_SHAPE[image_arch]}。")


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
    ssh_keys_file: SSH 公钥文件路径（可选项，默认空）：为空时 Grabber
        不传 --ssh-authorized-keys-file，实例只靠 root 密码登录。
    region: 工作区域（可选项）：不传时用该 profile 的主区；传了则在
        该订阅区域创建（须是该账号的订阅区域，否则 400）。
    """

    profile: str = None
    region: str = None
    shape: str
    ocpus: int = 0
    memory_gb: int = 0
    image_id: str
    subnet_id: str
    availability_domain: str
    display_name: str
    ssh_keys_file: str = ""
    target_cpu: str = None
    # Disk: None means the image default (47 GB); an explicit size must be at least 50 GB — an explicit 47 is rejected by OCI (InvalidParameter). 硬盘：None 表示用镜像默认（47GB）；显式指定最少 50GB——显式传 47 会被 OCI 拒绝（InvalidParameter）。
    # The frontend form treats 47 as the default and converts it to null when sending the request (see create.js). 前端表单 47 即默认，发请求时转成 null（见 create.js）。
    boot_volume_size_gbs: Optional[int] = Field(default=None, ge=50,
                                                le=200)
    assign_public_ip: bool = True
    assign_ipv6: bool = False
    min_delay: int = Field(default=0, ge=0)
    max_delay: int = Field(default=0, ge=0)
    user_data: str = ""
    root_login: bool = True
    root_password: str = ""
    # DD 重装：开时 user-data 是 DD 命令脚本（Debian 13），user_data
    # 原文改作 DD 完成后的初始化脚本（经 SSH 执行，不进 user-data）。
    dd: bool = False


def _notify_chat_id():
    """通知用的 chat_id：Bark 配置里记的 > 白名单第一个用户 ID。"""
    chat_id = load_bark_config().get("chat_id")
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    return chat_id


def _ad_region(availability_domain):
    """从可用域字符串里解析区域代码（如 wgwP:AP-SINGAPORE-1-AD-1 ->
    ap-singapore-1）；格式不对时返回 None（调用方不过滤）。"""
    parts = availability_domain.split(":")
    if len(parts) < 2:
        return None
    middle = parts[1]
    idx = middle.rfind("-AD-")
    if idx <= 0:
        return None
    return middle[:idx].lower()


@router.get("/api/create/defaults")
def create_defaults(profile: str = None, region: str = None,
                    username: str = Depends(get_current_user)):
    """创建表单的网络默认值：可用域 / 子网 / SSH 公钥文件。

    与机器人开机表单用同一套部署值（get_deploy_defaults）：环境变量
    MBOT_PLAN_* 是主区默认值，region_plans.json 里有该 (profile,
    region) 配置时用该区域的。region 不传时用该 profile 的主区。
    SSH 公钥文件只在服务器上真实存在时才返回路径（用户要求：有上传
    的密钥就填，没有就不填）；可用域 / 子网未配置时返回空串，前端
    留空让用户手填。
    环境变量的默认部署值属于主区：所选区域与之不符且
    region_plans.json 里又没有该 (profile, region) 配置时，返回的
    AD 与所选区域对不上——此时可用域/子网留空，避免把主区的子网
    填到别的区域导致创建失败。
    """
    if demo_data.is_demo():
        # Demo mode: simulated deploy defaults, no real config read.
        # 演示模式：模拟部署默认值，不读真实配置。
        try:
            return demo_data.demo_create_defaults(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        profile_name = profile or config.DEFAULT_PROFILE
        eff_region = effective_region(profile_name, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    deploy = get_deploy_defaults(region=eff_region, profile=profile_name)
    ad = deploy.get("availability_domain") or ""
    subnet = deploy.get("subnet_id") or ""
    if ad == AD_PLACEHOLDER:
        ad = ""
    if subnet == SUBNET_PLACEHOLDER:
        subnet = ""
    ad_region = _ad_region(ad) if ad else None
    if ad_region is not None and ad_region != eff_region:
        ad = ""
        subnet = ""
    ssh_file = deploy.get("ssh_keys_file") or ""
    if ssh_file == SSH_KEYS_PLACEHOLDER or not os.path.isfile(ssh_file):
        ssh_file = ""
    return {
        "ok": True,
        "profile": profile_name,
        "region": eff_region,
        "availability_domain": ad,
        "subnet_id": subnet,
        "ssh_keys_file": ssh_file,
        "ssh_keys_file_exists": bool(ssh_file),
    }


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
    if demo_data.is_demo():
        # Demo mode: validate the quota against the demo data, then fake
        # success with a fake task id and a fake root password (no Grabber
        # thread, no OCI call, no state change).
        # 演示模式：按模拟数据做配额预检，通过后假成功，返回假任务 id
        # 与假 root 密码（不起抢机线程，不调 OCI，不改状态）。
        try:
            ok, reason = demo_data.demo_can_launch(
                body.profile, body.region, body.shape,
                ocpus=body.ocpus, memory_gb=body.memory_gb)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        if not ok:
            return error_response(f"配额预检不通过：{reason}",
                                  status_code=400)
        return demo_data.fake_success(
            "创建实例",
            task_id="demo-grab-1",
            root_password="DEMO-0000-0000-0000")
    try:
        cli, compartment_id = resolve_profile(body.profile, body.region)
        # Availability-domain / region consistency check: the deploy values are for the home region, so without a region plan for another region the form may carry the home region's AD, and the task would then fail in Oracle with CannotParseRequest (the bot has the same problem, see tgbot/boot_form.py's deploy_region_mismatch). 可用域与所选区域一致性校验：部署值是主区的，别的区没配 region plan 时表单可能填进主区的 AD，提交后任务里 Oracle 会报 CannotParseRequest（机器人端同类问题，见 tgbot/boot_form.py 的 deploy_region_mismatch）。
        profile_name = body.profile or config.DEFAULT_PROFILE
        region = effective_region(profile_name, body.region)
        ad_region = _ad_region(body.availability_domain)
        if ad_region is not None and ad_region != region:
            raise ValueError(
                f"可用域是 {ad_region} 区的，与所选区域 {region} 不一致："
                f"{profile_name}/{region} 还没有配置开机网络，请先在 "
                "region_plans.json 里配好，或切到已配置的账号/区域再创建。")
        ok, reason = can_launch(cli, compartment_id, body.shape,
                                ocpus=body.ocpus, memory_gb=body.memory_gb)
        if not ok:
            return error_response(f"配额预检不通过：{reason}", status_code=400)
        image_id = resolve_image_id(body.image_id)
        # Image arch vs shape pre-check: the placeholder's arch comes from its suffix (no OCI call); a real OCID is looked up via image get and inferred from its display name. 镜像架构与形状事前校验：占位符的架构取自后缀（不调 OCI），真实 OCID 查 image get 从显示名推断。
        placeholder_arch = None
        for suffix, arch in _IMAGE_PLACEHOLDER_SUFFIX.items():
            if body.image_id.endswith(suffix):
                placeholder_arch = arch
                break
        check_image_shape_match(
            body.shape, placeholder_arch or get_image_arch(cli, image_id))
        # Root password: when enabled and empty, the backend generates a UUID; it is combined with the boot script into the same cloud-init (a script that is itself #cloud-config conflicts and returns 400). root 密码：开启时为空则后端生成 UUID；与开机脚本拼进同一份 cloud-init（脚本本身是 #cloud-config 时冲突，报 400）。
        root_password = None
        if body.root_login:
            root_password = body.root_password.strip() or \
                generate_root_password()
        # The script is passed through verbatim (only checked for blankness, never stripped). 脚本原文透传（只判断是否为空白，不 strip 内容）。
        script = body.user_data or None
        if script is not None and not script.strip():
            script = None
        user_data = None
        if root_password or body.dd:
            # The password and the script are combined into one cloud-init (a script that is itself #cloud-config conflicts, so build_user_data raises ValueError -> 400). 密码与脚本拼进同一份 cloud-init（脚本本身是 #cloud-config 时冲突，build_user_data 抛 ValueError -> 400）。DD 开时 user-data 是 DD 命令脚本，脚本原文改作 DD 后的初始化脚本。
            user_data = build_user_data(root_password, script,
                                        dd=bool(body.dd))
        else:
            # Without a root password, the script goes straight through to the Grabber (old behavior, no normalization). 不设 root 密码时脚本原文直通 Grabber（旧行为，不做归一化）。
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
            # DD 重装：初始化脚本原文与开关透传给抢机成功后的 DD 任务。
            "dd": bool(body.dd),
            "init_script": script,
        }
        # Dual-channel notification info (passed in memory to the thread; the password is saved to the per-instance credential store when the instance boots successfully, for later SSH / DD logins). 双通道通知信息（内存里传给线程；开机成功后密码会按实例存进凭据库，供之后 SSH / DD 登录用）。
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
    # The root password is returned with the response once (shown once in the frontend modal); the server also keeps it in the per-instance credential store for later SSH / DD logins (tgbot/instance_credentials.py), but the panel never shows it again. root 密码随响应返回一次（前端弹窗展示一次）；服务端还会把它按实例存进凭据库，供之后 SSH / DD 登录用，但面板不再展示。
    return {"ok": True, "task_id": task_id,
            "root_password": root_password}


@router.get("/api/tasks")
def list_tasks(username: str = Depends(get_current_user)):
    """任务列表。"""
    if demo_data.is_demo():
        # Demo mode: simulated task list, no real task file read.
        # 演示模式：模拟任务列表，不读真实任务文件。
        return {"ok": True, "tasks": demo_data.demo_tasks()}
    return {"ok": True, "tasks": TASKS.list()}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str, username: str = Depends(get_current_user)):
    """单个任务详情（含日志与结果）。"""
    if demo_data.is_demo():
        # Demo mode: simulated task detail.
        # 演示模式：模拟任务详情。
        task = demo_data.demo_task(task_id)
        if task is None:
            return error_response(f"任务不存在：{task_id}", status_code=404)
        return {"ok": True, "task": task}
    task = TASKS.get(task_id)
    if task is None:
        return error_response(f"任务不存在：{task_id}", status_code=404)
    return {"ok": True, "task": task}


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str, username: str = Depends(get_current_user)):
    """取消任务（只对 pending/running 的任务有效）。"""
    if demo_data.is_demo():
        # Demo mode: fake success for a simulated running task.
        # 演示模式：模拟运行中任务假取消成功。
        task = demo_data.demo_task(task_id)
        if task is None:
            return error_response(f"任务不存在：{task_id}", status_code=404)
        if task.get("status") not in ("pending", "running"):
            return error_response("任务已结束，无法取消", status_code=400)
        return demo_data.fake_success("取消任务", task_id=task_id)
    if TASKS.get(task_id) is None:
        return error_response(f"任务不存在：{task_id}", status_code=404)
    if not TASKS.cancel(task_id):
        return error_response("任务已结束，无法取消", status_code=400)
    return {"ok": True, "task_id": task_id}


@router.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, username: str = Depends(get_current_user)):
    """删除任务（只对已结束的任务有效；运行中的任务须先取消）。"""
    if demo_data.is_demo():
        # Demo mode: fake success for a simulated finished task.
        # 演示模式：模拟已结束任务假删除成功。
        task = demo_data.demo_task(task_id)
        if task is None:
            return error_response(f"任务不存在：{task_id}", status_code=404)
        if task.get("status") in ("pending", "running"):
            return error_response("任务还在运行中，请先取消", status_code=400)
        return demo_data.fake_success("删除任务", task_id=task_id)
    if TASKS.get(task_id) is None:
        return error_response(f"任务不存在：{task_id}", status_code=404)
    if not TASKS.delete(task_id):
        return error_response("任务还在运行中，请先取消", status_code=400)
    return {"ok": True, "task_id": task_id}
