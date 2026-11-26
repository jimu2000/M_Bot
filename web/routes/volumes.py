#!/usr/bin/env python3
"""磁盘路由（启动盘）：列表 / 详情 / 扩容 / 删除 / 孤儿盘 / 改 VPU。

  - GET /api/volumes：启动盘列表（面板形状，见 format_volume；
    含挂载关系与挂载实例名）。
  - GET /api/volumes/orphans：孤儿启动盘（AVAILABLE 且无挂载关系，
    同样返回 面板形状）。
  - GET /api/volumes/{id}：单个启动盘详情。
  - POST /api/volumes/{id}/resize：body {size_in_gbs}，必须大于当前容量。
  - POST /api/volumes/{id}/vpus：body {vpus_per_gb}，只能是 10/20/30。
  - DELETE /api/volumes/{id}：删除启动盘（只能删未附加的盘）。

注意：/orphans 路由必须定义在 /{volume_id} 之前，否则 "orphans"
会被当成 volume_id 匹配。
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.client import OCIError
from oci.instance import slim_instances
from oci.volume import (
    delete_boot_volume,
    get_boot_volume,
    list_boot_volume_attachments,
    resize_boot_volume,
    simplify_boot_volumes,
    update_boot_volume_vpus,
)
from web import config
from web import demo_data
from web.oci_cache import cached_raw_boot_volumes, cached_raw_instances
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.quota import clear_quota_cache
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()

# ================= 配置区（改这里） =================
# Boot volume list server cache TTL (seconds): each list call runs 3 OCI CLI calls (1 list + attachment relations + instance names), about 5s serially; repeat visits within 30s return the cache, and a manual refresh (refresh=1) bypasses it. 启动盘列表服务端缓存 TTL（秒）：列表接口每次要跑 3 次 OCI CLI（1 次 list + 挂载关系 + 实例名），串行约 5 秒；30 秒内重复访问直接返回缓存，手动刷新（refresh=1）绕过缓存。
VOLUMES_CACHE_TTL = 30
# =====================================================

# Boot volume list cache: (profile, region, "list"/"orphans") -> {"expires": monotonic-clock expiry, "payload": response body}. Memory only, never written to disk. 启动盘列表缓存：(profile, region, "list"/"orphans") -> {"expires": 单调时钟过期点, "payload": 响应体}。只放内存不写盘。
_VOLUMES_CACHE = {}
_VOLUMES_CACHE_LOCK = threading.Lock()


def clear_volumes_cache(profile=None):
    """清启动盘列表缓存：指定 profile 时只清该账号，否则全清。

    写操作（扩容/改 VPU/删除）成功后调用，避免列表显示旧数据；
    单元测试在 setUp 里调用，避免缓存跨测试串数据。
    """
    with _VOLUMES_CACHE_LOCK:
        if profile is None:
            _VOLUMES_CACHE.clear()
        else:
            for key in [k for k in _VOLUMES_CACHE if k[0] == profile]:
                del _VOLUMES_CACHE[key]


# Attachment states counted as "attached" (same rule as list_orphan_boot_volumes). 视为“已挂载”的挂载状态（与 list_orphan_boot_volumes 的判断一致）。
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


def _instance_name_map(cli, compartment_id, refresh=False):
    """返回 {实例 OCID: 显示名}；查询失败时返回空字典（降级）。

    实例列表走 Web 层共享缓存（与实例列表/配额端点同一份数据），
    refresh=true 时绕过缓存。
    """
    try:
        instances = slim_instances(
            cached_raw_instances(cli, compartment_id, refresh=refresh))
    except OCIError:
        return {}
    return {inst.get("id"): inst.get("display-name")
            for inst in instances}


def format_volume(volume, attachment=None, instance_name=None):
    """把启动盘条目转成 面板形状（阶段 5 工作包 E）。

    新形状（camelCase 字段）：
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
        "id": volume.get("id"),
        "display-name": volume.get("display-name"),
        "size-in-gbs": volume.get("size-in-gbs"),
        "vpus-per-gb": volume.get("vpus-per-gb"),
        "lifecycle-state": volume.get("lifecycle-state"),
        "availability-domain": volume.get("availability-domain"),
        "time-created": volume.get("time-created"),
    }


def _format_volumes(cli, compartment_id, volumes, refresh=False):
    """给一批启动盘补挂载关系与实例名，返回 面板形状列表。

    挂载关系与实例名是两个独立的 OCI 查询，并行跑（各 1 次 CLI，
    约省一半时间）；两个查询各自降级，失败不影响对方。
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        att_future = pool.submit(_attachment_map, cli, compartment_id,
                                 volumes)
        names_future = pool.submit(_instance_name_map, cli,
                                   compartment_id, refresh=refresh)
        attachments = att_future.result()
        names = names_future.result()
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


def _get_volumes_payload(cli, compartment_id, profile_name, refresh):
    """取启动盘列表 payload（带 30 秒服务端缓存）。

    列表与孤儿盘两个接口共用：孤儿盘就是列表里 AVAILABLE 且未挂载
    的盘，直接过滤，不再单独跑一遍 list + 挂载查询（省 3 次 CLI）。
    """
    cache_key = (profile_name, cli.region, "list")
    if not refresh:
        with _VOLUMES_CACHE_LOCK:
            entry = _VOLUMES_CACHE.get(cache_key)
        if entry and time.monotonic() < entry["expires"]:
            return entry["payload"]
    volumes = simplify_boot_volumes(
        cached_raw_boot_volumes(cli, compartment_id, refresh=refresh))
    volumes = _format_volumes(cli, compartment_id, volumes,
                              refresh=refresh)
    payload = {"ok": True,
               "profile": profile_name,
               "region": cli.region,
               "volumes": volumes}
    with _VOLUMES_CACHE_LOCK:
        _VOLUMES_CACHE[cache_key] = {
            "expires": time.monotonic() + VOLUMES_CACHE_TTL,
            "payload": payload,
        }
    return payload


@router.get("/api/volumes/orphans")
def orphans(profile: str = Query(default=None),
            region: str = Query(default=None),
            refresh: bool = Query(default=False),
            username: str = Depends(get_current_user)):
    """列出孤儿启动盘（可清理的、释放实例后留下的盘）。

    复用启动盘列表缓存再过滤（孤儿 = AVAILABLE 且未挂载，与
    oci.volume.list_orphan_boot_volumes 的判断一致）；refresh=true
    时绕过缓存，强制重新查询。
    """
    if demo_data.is_demo():
        # Demo mode: simulated orphan volumes, no OCI calls.
        # 演示模式：模拟孤儿盘，不调 OCI。
        try:
            return demo_data.demo_orphans_payload(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        payload = _get_volumes_payload(cli, compartment_id, profile_name,
                                       refresh)
        orphans = [v for v in payload["volumes"]
                   if v["lifecycleState"] == "AVAILABLE"
                   and not v["attached"]]
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile_name,
            "region": cli.region,
            "volumes": orphans}


@router.get("/api/volumes")
def list_all(profile: str = Query(default=None),
             region: str = Query(default=None),
             refresh: bool = Query(default=False),
             username: str = Depends(get_current_user)):
    """列出该账号指定区域下的全部启动盘（面板形状，含挂载关系）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated volumes, no OCI calls, no cache.
        # 演示模式：模拟启动盘，不调 OCI，不走缓存。
        try:
            return demo_data.demo_volumes_payload(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        return _get_volumes_payload(cli, compartment_id, profile_name,
                                    refresh)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)


@router.get("/api/volumes/{volume_id}")
def detail(volume_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           refresh: bool = Query(default=False),
           username: str = Depends(get_current_user)):
    """查单个启动盘详情（30 秒服务端缓存，与列表共用同一份缓存字典，
    写操作清缓存时一并失效；refresh=true 绕过缓存）。

    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated volume detail, no OCI call.
        # 演示模式：模拟启动盘详情，不调 OCI。
        detail = demo_data.demo_volume_detail(volume_id)
        if detail is None:
            return error_response(f"找不到启动盘：{volume_id}",
                                  status_code=404)
        return detail
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region, "detail:" + volume_id)
        if not refresh:
            with _VOLUMES_CACHE_LOCK:
                entry = _VOLUMES_CACHE.get(cache_key)
            if entry and time.monotonic() < entry["expires"]:
                return entry["payload"]
        volume = get_boot_volume(cli, volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True, "volume": volume}
    with _VOLUMES_CACHE_LOCK:
        _VOLUMES_CACHE[cache_key] = {
            "expires": time.monotonic() + VOLUMES_CACHE_TTL,
            "payload": payload,
        }
    return payload


@router.post("/api/volumes/{volume_id}/resize")
def resize(volume_id: str,
           body: ResizeBody,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """启动盘扩容：新容量必须大于当前容量（只能扩不能缩）。"""
    if demo_data.is_demo():
        # Demo mode: validate against the simulated volume, then fake
        # success without calling OCI (the returned volume carries the
        # new size; the simulated data itself is unchanged).
        # 演示模式：按模拟盘校验新容量，通过后假成功，不调 OCI
        # （返回的 volume 带新容量；模拟数据本身不变）。
        detail = demo_data.demo_volume_detail(volume_id)
        if detail is None:
            return error_response(f"找不到启动盘：{volume_id}",
                                  status_code=404)
        current_gb = detail["volume"].get("size-in-gbs")
        if current_gb is not None and body.size_in_gbs <= current_gb:
            return error_response(
                f"新容量 {body.size_in_gbs}GB 必须大于当前容量 {current_gb}GB",
                status_code=400)
        volume = dict(detail["volume"])
        volume["size-in-gbs"] = body.size_in_gbs
        return demo_data.fake_success("扩容启动盘", volume=volume)
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
    clear_volumes_cache(profile or config.DEFAULT_PROFILE)
    clear_cloud_report_cache()
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
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
    if demo_data.is_demo():
        # Demo mode: same value check as the real path, then fake success.
        # 演示模式：同样的取值校验，通过后假成功，不调 OCI。
        if body.vpus_per_gb not in (10, 20, 30):
            return error_response(
                "vpus_per_gb 只能是 10、20 或 30", status_code=400)
        detail = demo_data.demo_volume_detail(volume_id)
        if detail is None:
            return error_response(f"找不到启动盘：{volume_id}",
                                  status_code=404)
        volume = dict(detail["volume"])
        volume["vpus-per-gb"] = body.vpus_per_gb
        return demo_data.fake_success("修改启动盘 VPU", volume=volume)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        volume = update_boot_volume_vpus(cli, volume_id, body.vpus_per_gb)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_volumes_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "volume": volume}


@router.delete("/api/volumes/{volume_id}")
def delete(volume_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """删除启动盘（只能删未附加的盘；删了不可恢复）。"""
    if demo_data.is_demo():
        # Demo mode: only unattached simulated volumes can be "deleted".
        # 演示模式：只有未附加的模拟盘能“删”，已附加的拒绝（与真实规则一致）。
        try:
            payload = demo_data.demo_volumes_payload(profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        target = next((v for v in payload["volumes"]
                       if v["volumeId"] == volume_id), None)
        if target is None:
            return error_response(f"找不到启动盘：{volume_id}",
                                  status_code=404)
        if target["attached"]:
            return error_response("该启动盘仍挂载在实例上，不能删除",
                                  status_code=400)
        return demo_data.fake_success("删除启动盘", volume_id=volume_id)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        delete_boot_volume(cli, volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_volumes_cache(profile or config.DEFAULT_PROFILE)
    clear_cloud_report_cache()
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "volume_id": volume_id}
