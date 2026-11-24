#!/usr/bin/env python3
"""磁盘路由（启动盘）：列表 / 详情 / 扩容 / 删除 / 孤儿盘 / 改 VPU。

  - GET /api/volumes：启动盘列表（R-Bot 形状，见 format_volume；
    含挂载关系与挂载实例名）。
  - GET /api/volumes/orphans：孤儿启动盘（AVAILABLE 且无挂载关系，
    同样返回 R-Bot 形状）。
  - GET /api/volumes/{id}：单个启动盘详情。
  - POST /api/volumes/{id}/resize：body {size_in_gbs}，必须大于当前容量。
  - POST /api/volumes/{id}/vpus：body {vpus_per_gb}，只能是 10/20/30。
  - DELETE /api/volumes/{id}：删除启动盘（只能删未附加的盘）。

注意：/orphans 路由必须定义在 /{volume_id} 之前，否则 "orphans"
会被当成 volume_id 匹配。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.client import OCIError
from oci.instance import list_instances
from oci.volume import (
    delete_boot_volume,
    get_boot_volume,
    list_boot_volume_attachments,
    list_boot_volumes,
    list_orphan_boot_volumes,
    resize_boot_volume,
    update_boot_volume_vpus,
)
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()


# 视为“已挂载”的挂载状态（与 list_orphan_boot_volumes 的判断一致）。
_ATTACHED_STATES = ("ATTACHED", "ATTACHING")


def _attachment_map(cli, compartment_id, volumes):
    """查各 AD 的挂载关系，返回 {启动盘 OCID: 挂载记录}。

    挂载查询失败（某个 AD 查不到）时降级为空映射，不让列表整个失败；
    挂载信息缺失时前端显示未挂载（attached=false）。
    """
    mapping = {}
    ads = {v.get("availability-domain") for v in volumes
           if v.get("availability-domain")}
    for ad in ads:
        try:
            attachments = list_boot_volume_attachments(
                cli, compartment_id, ad, include_id=True)
        except OCIError:
            continue
        for att in attachments:
            mapping[att.get("boot-volume-id")] = att
    return mapping


def _instance_name_map(cli, compartment_id):
    """返回 {实例 OCID: 显示名}；查询失败时返回空字典（降级）。"""
    try:
        instances = list_instances(cli, compartment_id)
    except OCIError:
        return {}
    return {inst.get("id"): inst.get("display-name")
            for inst in instances}


def format_volume(volume, attachment=None, instance_name=None):
    """把启动盘条目转成 R-Bot 面板形状（阶段 5 工作包 E）。

    新形状（camelCase，与 R-Bot /cloud/oci/volumes 一致）：
    {volumeId, attachmentId, displayName, sizeInGBs, vpusPerGB,
     lifecycleState, instanceId, instanceName, attached,
     attachmentState, availabilityDomain, timeCreated}。
    attachment 为挂载记录（list_boot_volume_attachments 的条目），
    没有挂载关系时 attachmentId/instanceId/attachmentState 为 None，
    attached 为 False。

    过渡期双写旧字段（id / display-name / size-in-gbs / vpus-per-gb /
    lifecycle-state / availability-domain / time-created），前端已改
    消费新字段，新字段是契约，旧字段后续可删。
    """
    state = (attachment or {}).get("lifecycle-state")
    instance_id = ((attachment or {}).get("instance-id")
                   or volume.get("instance-id"))
    return {
        "volumeId": volume.get("id"),
        "attachmentId": (attachment or {}).get("id"),
        "displayName": volume.get("display-name"),
        "sizeInGBs": volume.get("size-in-gbs"),
        "vpusPerGB": volume.get("vpus-per-gb"),
        "lifecycleState": volume.get("lifecycle-state"),
        "instanceId": instance_id,
        "instanceName": instance_name,
        "attached": state in _ATTACHED_STATES,
        "attachmentState": state,
        "availabilityDomain": volume.get("availability-domain"),
        "timeCreated": volume.get("time-created"),
        # 旧字段（过渡期双写）。
        "id": volume.get("id"),
        "display-name": volume.get("display-name"),
        "size-in-gbs": volume.get("size-in-gbs"),
        "vpus-per-gb": volume.get("vpus-per-gb"),
        "lifecycle-state": volume.get("lifecycle-state"),
        "availability-domain": volume.get("availability-domain"),
        "time-created": volume.get("time-created"),
    }


def _format_volumes(cli, compartment_id, volumes):
    """给一批启动盘补挂载关系与实例名，返回 R-Bot 形状列表。"""
    attachments = _attachment_map(cli, compartment_id, volumes)
    names = _instance_name_map(cli, compartment_id)
    formatted = []
    for volume in volumes:
        att = attachments.get(volume.get("id"))
        instance_id = (att or {}).get("instance-id") or volume.get("instance-id")
        formatted.append(format_volume(volume, att, names.get(instance_id)))
    return formatted


class ResizeBody(BaseModel):
    """扩容请求体。"""

    size_in_gbs: int


class VpusBody(BaseModel):
    """改 VPU 请求体：10=默认，20=高性能，30=超高性能。"""

    vpus_per_gb: int


@router.get("/api/volumes/orphans")
def orphans(profile: str = Query(default=None),
            region: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """列出孤儿启动盘（可清理的、释放实例后留下的盘）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        volumes = list_orphan_boot_volumes(cli, compartment_id)
        volumes = _format_volumes(cli, compartment_id, volumes)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "region": cli.region,
            "volumes": volumes}


@router.get("/api/volumes")
def list_all(profile: str = Query(default=None),
             region: str = Query(default=None),
             username: str = Depends(get_current_user)):
    """列出该账号指定区域下的全部启动盘（R-Bot 形状，含挂载关系）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        volumes = list_boot_volumes(cli, compartment_id)
        volumes = _format_volumes(cli, compartment_id, volumes)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "region": cli.region,
            "volumes": volumes}


@router.get("/api/volumes/{volume_id}")
def detail(volume_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """查单个启动盘详情。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        volume = get_boot_volume(cli, volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume": volume}


@router.post("/api/volumes/{volume_id}/resize")
def resize(volume_id: str,
           body: ResizeBody,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """启动盘扩容：新容量必须大于当前容量（只能扩不能缩）。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        current = get_boot_volume(cli, volume_id)
        current_gb = (current or {}).get("size-in-gbs")
        if current_gb is not None and body.size_in_gbs <= current_gb:
            return error_response(
                f"新容量 {body.size_in_gbs}GB 必须大于当前容量 {current_gb}GB",
                status_code=400)
        volume = resize_boot_volume(cli, volume_id, body.size_in_gbs)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume": volume}


@router.post("/api/volumes/{volume_id}/vpus")
def update_vpus(volume_id: str,
                body: VpusBody,
                profile: str = Query(default=None),
                region: str = Query(default=None),
                username: str = Depends(get_current_user)):
    """修改启动盘 VPU（10=默认，20=高性能，30=超高性能）。

    无效值返回 400（见 update_boot_volume_vpus 的校验）。
    """
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        volume = update_boot_volume_vpus(cli, volume_id, body.vpus_per_gb)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume": volume}


@router.delete("/api/volumes/{volume_id}")
def delete(volume_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """删除启动盘（只能删未附加的盘；删了不可恢复）。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        delete_boot_volume(cli, volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "volume_id": volume_id}
