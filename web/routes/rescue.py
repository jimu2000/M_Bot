#!/usr/bin/env python3
"""救机路由（v2.01）：前置检查 / 安全恢复 / 无盘救机 / 孤儿盘 /
Netboot 向导 / 控制台连接管理。

  - GET /api/rescue/preflight?instance_id&action[&needed_gb][&volume_id]
  - POST /api/rescue/safe-restore：body {instance_id, backup_id,
    confirm, allow_delete_backup_source?}
  - GET /api/rescue/diskless/{instance_id}：无盘救机方案
  - POST /api/rescue/diskless/{instance_id}/rebuild：body {backup_id,
    display_name?, shape?, ocpus?, memory_gb?, availability_domain?,
    subnet_id?, ssh_keys_file?, assign_public_ip?, confirm}
  - GET /api/rescue/orphans：孤儿盘（实测规则）
  - POST /api/rescue/orphans/{volume_id}/delete?confirm=true
    [&allow_backup_source=true]：删除孤儿盘腾额度
  - POST /api/rescue/volumes/{volume_id}/delete：带备份源盘检查的删盘
  - GET /api/rescue/netboot-guide?instance_id[&region][&tftp_server]
  - GET /api/rescue/console?instance_id：列出控制台连接
  - POST /api/rescue/console：body {instance_id, ssh_public_key_file?,
    confirm}，创建连接（RSA 密钥，ed25519 会被拒）
  - DELETE /api/rescue/console/{connection_id}：删除连接

注意：/console、/orphans、/netboot-guide、/preflight 这些固定段路由
必须定义在 /{volume_id} / /{instance_id} 参数路由之前。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.boot_backup import get_boot_volume_backup
from oci.client import OCIError
from oci.rescue import (
    PREFLIGHT_ACTIONS,
    check_delete_volume_safety,
    delete_boot_volume_guarded,
    list_rescue_orphans,
    netboot_guide,
    plan_diskless_rescue,
    preflight_check,
    rebuild_diskless_from_backup,
    safe_restore,
    total_rescue_orphan_gb,
)
from oci.rescue import (
    create_console as rescue_create_console,
)
from oci.rescue import (
    delete_console as rescue_delete_console,
)
from oci.rescue import (
    list_consoles as rescue_list_consoles,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.routes.boot_backups import _resolve_restore_shape
from web.routes.instances import clear_instance_list_cache
from web.routes.quota import clear_quota_cache
from web.routes.volumes import clear_volumes_cache
from web.tasks import PLAN_DEFAULTS

router = APIRouter()

_AD_PLACEHOLDER = "xxxx:AP-SINGAPORE-1-AD-1"
_SUBNET_PLACEHOLDER = "ocid1.subnet.oc1..xxxxxxxx"
_SSH_KEYS_PLACEHOLDER = "/path/to/keys.pub"


def _format_orphan(orphan):
    """孤儿盘条目转面板形状（camelCase 为主，双写旧字段）。"""
    return {
        "volumeId": orphan.get("id"),
        "displayName": orphan.get("display-name"),
        "sizeInGBs": orphan.get("size-in-gbs"),
        "lifecycleState": orphan.get("lifecycle-state"),
        "availabilityDomain": orphan.get("availability-domain"),
        "sourceBackupId": orphan.get("source-backup-id"),
        "id": orphan.get("id"),
        "display-name": orphan.get("display-name"),
        "size-in-gbs": orphan.get("size-in-gbs"),
        "lifecycle-state": orphan.get("lifecycle-state"),
        "availability-domain": orphan.get("availability-domain"),
    }


class SafeRestoreBody(BaseModel):
    """安全恢复请求体。"""

    instance_id: str
    backup_id: str
    confirm: bool = False
    allow_delete_backup_source: bool = False


class RebuildBody(BaseModel):
    """无盘救机（从备份新建实例）请求体。"""

    backup_id: str
    display_name: str = None
    shape: str = None
    ocpus: int = None
    memory_gb: int = None
    availability_domain: str = None
    subnet_id: str = None
    ssh_keys_file: str = None
    assign_public_ip: bool = True
    confirm: bool = False


class DeleteVolumeBody(BaseModel):
    """带安全检查的删盘请求体。"""

    confirm: bool = False
    allow_backup_source: bool = False


class CreateConsoleBody(BaseModel):
    """创建串行控制台连接请求体。"""

    instance_id: str
    ssh_public_key_file: str = None
    confirm: bool = False


@router.get("/api/rescue/preflight")
def preflight(instance_id: str = Query(default=None),
              action: str = Query(default=None),
              needed_gb: int = Query(default=None),
              volume_id: str = Query(default=None),
              profile: str = Query(default=None),
              region: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """救机前置检查：能不能做、缺什么、风险是什么。"""
    if not instance_id:
        return error_response("缺少 instance_id", status_code=400)
    if action not in PREFLIGHT_ACTIONS:
        return error_response(
            f"未知操作：{action}，可选：{'、'.join(PREFLIGHT_ACTIONS)}",
            status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = preflight_check(cli, compartment_id, instance_id, action,
                                 needed_gb=needed_gb, volume_id=volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, **result}


@router.post("/api/rescue/safe-restore")
def safe_restore_endpoint(body: SafeRestoreBody,
                          profile: str = Query(default=None),
                          region: str = Query(default=None),
                          username: str = Depends(get_current_user)):
    """安全恢复：先建后删（额度预检 + 备份源盘检查 + 原生换盘 + 删旧盘）。"""
    if not body.confirm:
        return error_response("安全恢复需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = safe_restore(
            cli, compartment_id, body.instance_id, body.backup_id,
            allow_delete_backup_source=body.allow_delete_backup_source)
        clear_volumes_cache(profile or config.DEFAULT_PROFILE)
        clear_quota_cache(profile or config.DEFAULT_PROFILE)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, **result}


@router.get("/api/rescue/diskless/{instance_id}")
def diskless_plan(instance_id: str,
                  profile: str = Query(default=None),
                  region: str = Query(default=None),
                  username: str = Depends(get_current_user)):
    """无盘救机方案：实例无启动盘时，说明原实例无法原地救回，
    给出从备份新建实例的方案与代价。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        plan = plan_diskless_rescue(cli, compartment_id, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, **plan}


@router.post("/api/rescue/diskless/{instance_id}/rebuild")
def diskless_rebuild(instance_id: str,
                     body: RebuildBody,
                     profile: str = Query(default=None),
                     region: str = Query(default=None),
                     username: str = Depends(get_current_user)):
    """无盘救机执行：从备份新建实例（原实例不动）。"""
    if not body.confirm:
        return error_response("从备份新建实例需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        backup = get_boot_volume_backup(cli, body.backup_id)
        shape, ocpus, memory_gb = _resolve_restore_shape(
            cli, compartment_id, backup, body.shape,
            body.ocpus, body.memory_gb)
        availability_domain = (body.availability_domain
                               or PLAN_DEFAULTS["availability_domain"])
        subnet_id = body.subnet_id or PLAN_DEFAULTS["subnet_id"]
        ssh_keys_file = (body.ssh_keys_file
                         or PLAN_DEFAULTS["ssh_keys_file"])
        if availability_domain in (None, "", _AD_PLACEHOLDER):
            return error_response(
                "未配置可用域：请在请求里传 availability_domain，或在部署"
                "环境变量 MBOT_PLAN_AD 里配置。", status_code=400)
        if subnet_id in (None, "", _SUBNET_PLACEHOLDER):
            return error_response(
                "未配置子网：请在请求里传 subnet_id，或在部署环境变量 "
                "MBOT_PLAN_SUBNET_ID 里配置。", status_code=400)
        if ssh_keys_file in (None, "", _SSH_KEYS_PLACEHOLDER):
            ssh_keys_file = ""
        display_name = body.display_name or (
            f"{backup.get('display-name') or 'backup'}-rescued")
        result = rebuild_diskless_from_backup(
            cli, compartment_id, instance_id, body.backup_id,
            availability_domain=availability_domain,
            subnet_id=subnet_id,
            display_name=display_name,
            ssh_keys_file=ssh_keys_file,
            shape=shape, ocpus=ocpus, memory_gb=memory_gb,
            assign_public_ip=body.assign_public_ip)
        clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
        clear_volumes_cache(profile or config.DEFAULT_PROFILE)
        clear_quota_cache(profile or config.DEFAULT_PROFILE)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "instance_id": result["instance_id"],
            "boot_volume_id": result["boot_volume_id"],
            "shape": result["shape"],
            "ocpus": result["ocpus"],
            "memory_gb": result["memory_gb"],
            "cost_note": ("新实例有新的 OCID 与新的公网 IP（旧 IP 不继承），"
                          "旧实例无法复活，终止旧实例前请确认数据已迁移。")}


@router.get("/api/rescue/orphans")
def orphans(profile: str = Query(default=None),
            region: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """孤儿盘列表（实测规则：无任何挂载记录 + 源自备份恢复）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        found = list_rescue_orphans(cli, compartment_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "orphans": [_format_orphan(o) for o in found],
            "total_gb": total_rescue_orphan_gb(found),
            "count": len(found)}


@router.post("/api/rescue/orphans/{volume_id}/delete")
def delete_orphan(volume_id: str,
                  body: DeleteVolumeBody = None,
                  profile: str = Query(default=None),
                  region: str = Query(default=None),
                  username: str = Depends(get_current_user)):
    """删除孤儿盘腾额度（须 confirm=true；是备份源盘时须显式放行）。"""
    confirm = bool(body and body.confirm)
    allow = bool(body and body.allow_backup_source)
    if not confirm:
        return error_response("删除孤儿盘需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = delete_boot_volume_guarded(
            cli, compartment_id, volume_id, confirm=True,
            allow_backup_source=allow)
        clear_volumes_cache(profile or config.DEFAULT_PROFILE)
        clear_quota_cache(profile or config.DEFAULT_PROFILE)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume_id": volume_id, **result}


@router.post("/api/rescue/volumes/{volume_id}/delete")
def delete_volume_guarded_endpoint(volume_id: str,
                                   body: DeleteVolumeBody,
                                   profile: str = Query(default=None),
                                   region: str = Query(default=None),
                                   username: str = Depends(get_current_user)):
    """带备份源盘检查的删盘（须 confirm=true；是备份源盘时须显式放行）。"""
    if not body.confirm:
        return error_response("删除启动盘需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = delete_boot_volume_guarded(
            cli, compartment_id, volume_id, confirm=True,
            allow_backup_source=body.allow_backup_source)
        clear_volumes_cache(profile or config.DEFAULT_PROFILE)
        clear_quota_cache(profile or config.DEFAULT_PROFILE)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume_id": volume_id, **result}


@router.get("/api/rescue/volumes/{volume_id}/delete-check")
def delete_check(volume_id: str,
                 profile: str = Query(default=None),
                 region: str = Query(default=None),
                 username: str = Depends(get_current_user)):
    """删盘前检查：该盘是否为任意备份的源盘。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        result = check_delete_volume_safety(cli, compartment_id, volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume_id": volume_id, **result}


@router.get("/api/rescue/netboot-guide")
def netboot_guide_endpoint(instance_id: str = Query(default=None),
                           region: str = Query(default=None),
                           tftp_server: str = Query(default=None),
                           username: str = Depends(get_current_user)):
    """Netboot 救机向导（盘在、系统砖了）：EFI 命令 / TFTP 节点 /
    netboot.xyz 菜单指引 / DD 步骤。"""
    if not instance_id:
        return error_response("缺少 instance_id", status_code=400)
    return {"ok": True,
            **netboot_guide(instance_id, region=region,
                            tftp_server=tftp_server)}


@router.get("/api/rescue/console")
def list_consoles_endpoint(instance_id: str = Query(default=None),
                           profile: str = Query(default=None),
                           region: str = Query(default=None),
                           username: str = Depends(get_current_user)):
    """列出串行控制台连接（可按实例过滤）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        connections = rescue_list_consoles(cli, compartment_id, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "connections": connections}


@router.post("/api/rescue/console")
def create_console_endpoint(body: CreateConsoleBody,
                            profile: str = Query(default=None),
                            region: str = Query(default=None),
                            username: str = Depends(get_current_user)):
    """创建串行控制台连接（须 confirm=true；只接受 RSA 公钥）。"""
    if not body.confirm:
        return error_response("创建控制台连接需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        connection = rescue_create_console(
            cli, body.instance_id, body.ssh_public_key_file)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "connection": connection}


@router.delete("/api/rescue/console/{connection_id}")
def delete_console_endpoint(connection_id: str,
                            profile: str = Query(default=None),
                            region: str = Query(default=None),
                            username: str = Depends(get_current_user)):
    """删除串行控制台连接。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        rescue_delete_console(cli, connection_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "connection_id": connection_id}
