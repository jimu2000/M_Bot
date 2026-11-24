#!/usr/bin/env python3
"""启动卷备份路由：列表 / 配额 / 创建 / 删除 / 从备份恢复。

R-Bot 没有这个功能，这是 M_Bot 独有的。

  - GET /api/boot-backups/quota：备份额度（数量 / 上限 5 /
    是否已满 / 最早备份信息 / 是否付费账号）。付费账号不限 5 个，
    返回 paid=true、limit=null、full=false。创建备份前前端先调它。
  - GET /api/boot-backups：备份列表（R-Bot 风格 camelCase 字段，
    同时双写 kebab-case 旧字段）。
  - POST /api/boot-backups：创建 FULL 全量备份，body
    {boot_volume_id, display_name?, delete_oldest?}。付费账号直接
    创建；免费账号满了自动删最早备份再创建（用户要求 2026-11-24：
    不用等确认），响应里带 deleted_oldest；delete_oldest 参数仅
    保留兼容，不再需要。
  - POST /api/boot-backups/paid：设置当前账号付费/免费，body
    {paid: bool}，与机器人端共用 tgbot/data/paid_profiles.json。
  - POST /api/boot-backups/delete-batch：批量删除备份，body
    {backup_ids: [...]}，逐个删除并返回成功/失败清单。
  - DELETE /api/boot-backups/{id}?confirm=true：删除备份。
  - POST /api/boot-backups/{id}/restore：从备份恢复出一台新实例
    （原机不动），body {display_name?, shape?, ocpus?, memory_gb?,
    availability_domain?, subnet_id?, ssh_keys_file?,
    assign_public_ip?, confirm}。恢复前用 can_launch_batch 预检
    A1/E2 与启动盘额度（启动盘按备份实际大小算），不通过返回 400。

注意：/quota、/paid、/delete-batch 路由必须定义在 /{backup_id}
之前，否则 "quota" 会被当成 backup_id 匹配（同 volumes.py 的
/orphans）。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.boot_backup import (
    DEFAULT_RESTORE_MEMORY_GB,
    DEFAULT_RESTORE_OCPUS,
    DEFAULT_RESTORE_SHAPE,
    check_backup_quota,
    create_boot_volume_backup,
    create_boot_volume_backup_with_limit_retry,
    delete_boot_volume_backup,
    get_boot_volume_backup,
    get_source_instance_shape,
    list_boot_volume_backups,
    restore_boot_volume_backup,
)
from oci.client import OCIError
from oci.quota import (
    SHAPE_A1_FLEX,
    can_launch_batch,
    check_a1_single_instance_cap,
)
from tgbot.paid_profiles import is_paid_profile, set_paid_profile
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.routes.instances import clear_instance_list_cache
from web.tasks import PLAN_DEFAULTS

router = APIRouter()

# 部署默认值里的占位符（与 web/tasks.py 的 PLAN_DEFAULTS 一致）：
# 恢复时 AD / 子网仍是占位符说明部署没配，返回 400 提示配置。
_AD_PLACEHOLDER = "xxxx:AP-SINGAPORE-1-AD-1"
_SUBNET_PLACEHOLDER = "ocid1.subnet.oc1..xxxxxxxx"
_SSH_KEYS_PLACEHOLDER = "/path/to/keys.pub"
# 恢复表单的"自动"形状值：用备份来源实例的形状，查不到用默认 A1 1核/6GB。
_SHAPE_AUTO = "auto"


def format_backup(backup):
    """把备份条目转成 R-Bot 风格形状（camelCase 为主，双写旧字段）。

    新字段：{backupId, displayName, bootVolumeId, sizeInGBs,
    lifecycleState, sourceType, type, timeCreated, imageId}。
    旧字段（kebab-case）：id / display-name / boot-volume-id /
    size-in-gbs / lifecycle-state / source-type / type /
    time-created / image-id。
    """
    return {
        "backupId": backup.get("id"),
        "displayName": backup.get("display-name"),
        "bootVolumeId": backup.get("boot-volume-id"),
        "sizeInGBs": backup.get("size-in-gbs"),
        "lifecycleState": backup.get("lifecycle-state"),
        "sourceType": backup.get("source-type"),
        "type": backup.get("type"),
        "timeCreated": backup.get("time-created"),
        "imageId": backup.get("image-id"),
        # 旧字段（过渡期双写）。
        "id": backup.get("id"),
        "display-name": backup.get("display-name"),
        "boot-volume-id": backup.get("boot-volume-id"),
        "size-in-gbs": backup.get("size-in-gbs"),
        "lifecycle-state": backup.get("lifecycle-state"),
        "source-type": backup.get("source-type"),
        "time-created": backup.get("time-created"),
        "image-id": backup.get("image-id"),
    }


def _fmt_time(value):
    """ISO 时间转 "YYYY-MM-DD HH:MM"（显示用）；解析失败原样返回。"""
    if not value:
        return "未知时间"
    text = str(value)
    if len(text) >= 16 and text[10] == "T":
        return text[:10] + " " + text[11:16]
    return text


def _resolve_restore_shape(cli, compartment_id, backup, body_shape,
                           body_ocpus, body_memory_gb):
    """解析恢复用的形状与规格。

    body_shape 为 None / "" / "auto" 时查备份来源实例的形状，
    查不到用默认 A1.Flex 1 核 / 6GB；显式传了形状就用它，
    A1.Flex 缺 ocpus / memory_gb 时补默认 1 核 / 6GB。
    返回 (shape, ocpus, memory_gb) 三元组。
    """
    if body_shape and body_shape != _SHAPE_AUTO:
        shape = body_shape
        if shape.endswith(".Flex"):
            return (shape,
                    body_ocpus if body_ocpus is not None
                    else DEFAULT_RESTORE_OCPUS,
                    body_memory_gb if body_memory_gb is not None
                    else DEFAULT_RESTORE_MEMORY_GB)
        return (shape, None, None)
    detected = get_source_instance_shape(cli, compartment_id, backup)
    if detected:
        shape, ocpus, memory_gb = detected
        if shape.endswith(".Flex"):
            if ocpus is None:
                ocpus = DEFAULT_RESTORE_OCPUS
            if memory_gb is None:
                memory_gb = DEFAULT_RESTORE_MEMORY_GB
        return (shape, ocpus, memory_gb)
    return (DEFAULT_RESTORE_SHAPE, DEFAULT_RESTORE_OCPUS,
            DEFAULT_RESTORE_MEMORY_GB)


class CreateBackupBody(BaseModel):
    """创建备份请求体。"""

    boot_volume_id: str
    display_name: str = None
    # 仅保留兼容：免费账号满了服务端自动删最早备份再创建，
    # 不再需要前端传 delete_oldest。
    delete_oldest: bool = False


class PaidBody(BaseModel):
    """设置账号付费/免费请求体。"""

    paid: bool


class DeleteBatchBody(BaseModel):
    """批量删除备份请求体。"""

    backup_ids: list


class RestoreBody(BaseModel):
    """从备份恢复请求体。

    shape：不传或 "auto" 表示用备份来源实例的形状（查不到用默认
    A1.Flex 1 核 / 6GB）；传具体规格名则用它（A1.Flex 可配 ocpus /
    memory_gb）。availability_domain / subnet_id / ssh_keys_file
    不传时用部署默认值（环境变量 RBOT_PLAN_AD / RBOT_PLAN_SUBNET_ID /
    RBOT_PLAN_SSH_KEYS_FILE，与抢机计划共用）。
    """

    display_name: str = None
    shape: str = None
    ocpus: int = None
    memory_gb: int = None
    availability_domain: str = None
    subnet_id: str = None
    ssh_keys_file: str = None
    assign_public_ip: bool = True
    confirm: bool = False


@router.get("/api/boot-backups/quota")
def quota(profile: str = Query(default=None),
          region: str = Query(default=None),
          username: str = Depends(get_current_user)):
    """查备份额度：数量 / 上限 / 是否已满 / 最早备份 / 是否付费账号。

    付费账号不限 5 个：paid=true、limit=null、full=false。
    """
    try:
        cli, compartment_id = resolve_profile(profile, region)
        prof = profile or config.DEFAULT_PROFILE
        paid = is_paid_profile(prof)
        count, limit, full, oldest = check_backup_quota(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": prof,
            "paid": paid,
            "count": count,
            "limit": None if paid else limit,
            "full": False if paid else full,
            "oldest": ({"id": oldest.get("id"),
                        "name": oldest.get("display-name"),
                        "time_created": oldest.get("time-created")}
                       if oldest else None)}


@router.post("/api/boot-backups/paid")
def set_paid(body: PaidBody,
             profile: str = Query(default=None),
             region: str = Query(default=None),
             username: str = Depends(get_current_user)):
    """设置当前账号的付费/免费标记（与机器人端共用同一份配置）。"""
    prof = profile or config.DEFAULT_PROFILE
    try:
        resolve_profile(profile, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    set_paid_profile(prof, body.paid)
    return {"ok": True, "profile": prof, "paid": body.paid}


@router.post("/api/boot-backups/delete-batch")
def delete_batch(body: DeleteBatchBody,
                 profile: str = Query(default=None),
                 region: str = Query(default=None),
                 username: str = Depends(get_current_user)):
    """批量删除备份：逐个删除，单个失败不影响其他，返回成功/失败清单。"""
    if not body.backup_ids:
        return error_response("未选择要删除的备份", status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    deleted, failed = [], []
    for backup_id in body.backup_ids:
        try:
            delete_boot_volume_backup(cli, backup_id)
            deleted.append(backup_id)
        except OCIError as exc:
            failed.append({"id": backup_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 单个失败继续删其余
            failed.append({"id": backup_id, "error": str(exc)})
    return {"ok": True, "deleted": deleted, "failed": failed}


@router.get("/api/boot-backups")
def list_all(profile: str = Query(default=None),
             region: str = Query(default=None),
             username: str = Depends(get_current_user)):
    """列出该账号下的全部启动卷备份（按创建时间升序）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        backups = list_boot_volume_backups(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "backups": [format_backup(b) for b in backups]}


@router.post("/api/boot-backups")
def create(body: CreateBackupBody,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """创建 FULL 全量备份。

    付费账号直接创建；免费账号满了自动删最早备份再创建（用户要求
    2026-11-24：不用等确认），响应里带 deleted_oldest。
    """
    try:
        cli, compartment_id = resolve_profile(profile, region)
        prof = profile or config.DEFAULT_PROFILE
        paid = is_paid_profile(prof)
        deleted_oldest = None
        if paid:
            backup = create_boot_volume_backup(cli, body.boot_volume_id,
                                               body.display_name)
        else:
            count, limit, full, oldest = check_backup_quota(
                cli, compartment_id)
            if full:
                if oldest is None:
                    return error_response(
                        "免费备份已满，但未找到可删除的最早备份，"
                        "请手动清理。",
                        status_code=400)
                delete_boot_volume_backup(cli, oldest["id"])
                deleted_oldest = {"id": oldest.get("id"),
                                  "name": oldest.get("display-name")}
                # 删完立即创建可能撞上 OCI 配额同步延迟（LimitExceeded），
                # 用带重试的创建（实测 2026-11-24 芝加哥）。
                backup = create_boot_volume_backup_with_limit_retry(
                    cli, body.boot_volume_id, body.display_name)
            else:
                backup = create_boot_volume_backup(cli, body.boot_volume_id,
                                                   body.display_name)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    result = {"ok": True, "backup": format_backup(backup)}
    if deleted_oldest is not None:
        result["deleted_oldest"] = deleted_oldest
    return result


@router.delete("/api/boot-backups/{backup_id}")
def delete(backup_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           confirm: bool = Query(default=False),
           username: str = Depends(get_current_user)):
    """删除备份（须 confirm=true；删了不可恢复）。"""
    if not confirm:
        return error_response("删除备份需要确认：请传 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        delete_boot_volume_backup(cli, backup_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "backup_id": backup_id}


@router.post("/api/boot-backups/{backup_id}/restore")
def restore(backup_id: str,
            body: RestoreBody,
            profile: str = Query(default=None),
            region: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """从备份恢复出一台新实例（原机不动）。

    恢复前用 can_launch_batch 预检 A1/E2 与启动盘额度（启动盘按备份
    实际大小算），不通过返回 400；A1 还要过单台上限校验（4 核/24GB）。
    AD / 子网 / SSH 公钥不传时用部署默认值，部署没配则返回 400 提示。
    """
    if not body.confirm:
        return error_response("从备份恢复需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        backup = get_boot_volume_backup(cli, backup_id)
        shape, ocpus, memory_gb = _resolve_restore_shape(
            cli, compartment_id, backup, body.shape,
            body.ocpus, body.memory_gb)
        if shape == SHAPE_A1_FLEX:
            cap_error = check_a1_single_instance_cap(ocpus, memory_gb)
            if cap_error:
                return error_response(cap_error, status_code=400)
        boot_size = backup.get("size-in-gbs") or 47
        ok, reason = can_launch_batch(
            cli, compartment_id, shape, ocpus=ocpus or 0,
            memory_gb=memory_gb or 0, count=1,
            boot_volume_size_gbs=boot_size)
        if not ok:
            return error_response(f"配额预检不通过：{reason}", status_code=400)
        availability_domain = (body.availability_domain
                               or PLAN_DEFAULTS["availability_domain"])
        subnet_id = body.subnet_id or PLAN_DEFAULTS["subnet_id"]
        ssh_keys_file = (body.ssh_keys_file
                         or PLAN_DEFAULTS["ssh_keys_file"])
        if availability_domain in (None, "", _AD_PLACEHOLDER):
            return error_response(
                "未配置可用域：请在请求里传 availability_domain，或在部署"
                "环境变量 RBOT_PLAN_AD 里配置。", status_code=400)
        if subnet_id in (None, "", _SUBNET_PLACEHOLDER):
            return error_response(
                "未配置子网：请在请求里传 subnet_id，或在部署环境变量 "
                "RBOT_PLAN_SUBNET_ID 里配置。", status_code=400)
        if ssh_keys_file in (None, "", _SSH_KEYS_PLACEHOLDER):
            # 恢复出来的系统保留原机的 authorized_keys，原密钥照样能登录，
            # 所以 SSH 公钥文件不是必须的；没配就传空字符串（不传该参数）。
            ssh_keys_file = ""
        display_name = body.display_name or (
            f"{backup.get('display-name') or 'backup'}-restored")
        result = restore_boot_volume_backup(
            cli, compartment_id, backup_id,
            availability_domain=availability_domain,
            subnet_id=subnet_id,
            display_name=display_name,
            ssh_keys_file=ssh_keys_file,
            shape=shape, ocpus=ocpus, memory_gb=memory_gb,
            assign_public_ip=body.assign_public_ip)
        clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "instance_id": result["instance_id"],
            "boot_volume_id": result["boot_volume_id"],
            "shape": result["shape"],
            "ocpus": result["ocpus"],
            "memory_gb": result["memory_gb"]}
