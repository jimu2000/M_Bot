#!/usr/bin/env python3
"""实例路由：列表 / 详情 / 启动 / 停止 / 重启 / 终止 / 批量 / 改名 / 重装 /
串行控制台 / 换 IP / IPv6 / 升降配 / 尝试修复。

  - GET /api/instances：列表（面板形状，见 format_instance；只含
    RUNNING 状态实例，每项补上公网 IP，取不到时为 None）。
    各实例的公网 IP 并行取（线程池）；列表结果服务端缓存 30 秒，
    手动刷新时传 refresh=1 绕过缓存；写操作成功后清对应账号的缓存。
  - GET /api/instances/{id}：详情（含公网 IP、内网 IP、IPv6 列表、
    启动盘大小 / OCID / VPU / 状态；附加字段尽力而为，查不到时为 None）。
  - GET /api/instances/{id}/console：串行控制台连接信息。
  - POST /api/instances/batch：批量开机/关机/重启/删除
    （body {action, instance_ids, confirm}，删除必须 confirm=true）。
  - POST /api/instances/{id}/start|stop|restart。
  - POST /api/instances/{id}/terminate：body 必须含 confirm=true，
    终止时默认永久删除引导卷（terminate_instance 的默认值，不要改）；
    传 preserve_boot_volume=true 时保留引导卷（前端“删除（保留启动
    盘）”用，引导卷会占用 200GB 免费额度，之后可在启动盘页面删除）。
  - POST /api/instances/{id}/rename：body {name}，更改实例显示名。
  - POST /api/instances/{id}/reinstall：body 必须含 confirm=true 且
    confirm_name 等于实例当前显示名（防误点）；重装清空系统盘，
    实例 OCID 与 IP 保留不变。
  - POST /api/instances/{id}/change-ip：换公网 IP（body 须含
    confirm=true；先删旧临时 IP 再绑新保留 IP，见 change_public_ip；
    返回 {old_ip, new_ip}）。
  - POST /api/instances/{id}/ipv6：给主 VNIC 附加一个 IPv6，返回新地址。
  - DELETE /api/instances/{id}/ipv6：删除该实例主 VNIC 的全部 IPv6；
    无 IPv6 时返回 ok + 提示，不报错。
  - POST /api/instances/{id}/resize：升/降配 A1.Flex（body
    {ocpus, memory_gb, confirm}；E2 固定规格拒绝；A1 按免费额度校验
    上限 4 核/24GB；直接 update，OCI 自动重启实例）。
  - POST /api/instances/{id}/repair：尝试修复（检查实例状态/主 VNIC/
    公网 IP，缺失时重新附加临时公网 IP；返回修复报告）。
  - GET /api/instances/{id}/boot-backups：该实例当前启动盘的
    AVAILABLE 备份列表（引导卷恢复的备份选择用）。
  - POST /api/instances/{id}/restore-boot-volume：用备份替换实例的
    启动盘（body {backup_id, confirm}，confirm 必须为 true；停机换盘，
    原地恢复；后台任务跑，返回 task_id，进度在任务页看）。

profile 与 region 通过查询参数传（如 ?profile=ACCOUNT2&region=us-sanjose-1），
默认 DEFAULT 账号的主区。
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.boot_backup import get_boot_volume_backup, list_boot_volume_backups
from oci.boot_swap import (
    check_boot_volume_space,
    get_boot_volume_attachment,
)
from oci.client import OCIError
from oci.instance import (
    batch_restart,
    batch_start,
    batch_stop,
    batch_terminate,
    get_instance,
    get_serial_console,
    list_instances,
    reinstall_instance,
    rename_instance,
    resize_instance,
    restart_instance,
    start_instance,
    stop_instance,
    terminate_instance,
)
from oci.network import (
    change_public_ip,
    create_ipv6,
    delete_ipv6,
    get_primary_vnic,
    get_public_ip,
    list_ipv6_for_vnic,
    list_vnics,
    repair_instance_ip,
)
from oci.quota import SHAPE_A1_FLEX, SHAPE_E2_MICRO, check_a1_single_instance_cap
from oci.volume import get_boot_volume, list_boot_volume_attachments
from tgbot import config as bot_config
from tgbot.bark import load_bark_config
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.tasks import TASKS, start_bootswap_task

router = APIRouter()

# ================= 配置区（改这里） =================
# 实例列表服务端缓存 TTL（秒）：列表接口每次要跑 1 + N 次 OCI CLI
# （1 次 list + 每台实例 1 次 list-vnics 取公网 IP），串行约 10 秒；
# 30 秒内重复访问直接返回缓存，手动刷新（refresh=1）绕过缓存。
INSTANCE_LIST_CACHE_TTL = 30
# 并行取公网 IP 的线程数上限。OCIClient.run 只跑独立子进程，
# 各线程间无共享可变状态，并行安全。
MAX_IP_WORKERS = 8
# =====================================================

# 实例列表缓存：(profile, state) -> {"expires": 单调时钟过期点, "payload": 响应体}。
# 只放内存不写盘；面板是单管理员，无需按用户隔离。
_LIST_CACHE = {}
_LIST_CACHE_LOCK = threading.Lock()


def clear_instance_list_cache(profile=None):
    """清实例列表缓存：指定 profile 时只清该账号，否则全清。

    写操作（启停/重启/终止/批量/改名/重装）成功后调用，避免列表显示旧状态；
    单元测试在 setUp 里调用，避免缓存跨测试串数据。
    """
    with _LIST_CACHE_LOCK:
        if profile is None:
            _LIST_CACHE.clear()
        else:
            for key in [k for k in _LIST_CACHE if k[0] == profile]:
                del _LIST_CACHE[key]


class TerminateBody(BaseModel):
    """终止请求体：必须显式确认。

    preserve_boot_volume 默认 False（永久删除引导卷，保持原有行为）；
    传 True 时保留引导卷（前端“删除（保留启动盘）”用）。
    """

    confirm: bool = False
    preserve_boot_volume: bool = False


class BatchBody(BaseModel):
    """批量操作请求体：terminate 必须 confirm=true。"""

    action: str
    instance_ids: list = []
    confirm: bool = False


class RenameBody(BaseModel):
    """改名请求体。"""

    name: str


class ReinstallBody(BaseModel):
    """重装请求体：必须 confirm=true 且 confirm_name 等于实例当前名。

    image_id 不传时用当前镜像重装。
    """

    confirm: bool = False
    confirm_name: str = ""
    image_id: str = None


class RestoreBootVolumeBody(BaseModel):
    """引导卷恢复请求体：必须 confirm=true。"""

    backup_id: str = ""
    confirm: bool = False


def _notify_chat_id():
    """通知用的 chat_id：Bark 配置里记的 > 白名单第一个用户 ID。"""
    chat_id = load_bark_config().get("chat_id")
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    return chat_id


class ChangeIpBody(BaseModel):
    """换 IP 请求体：必须显式确认（旧 IP 立即释放，无法找回）。"""

    confirm: bool = False


class ResizeBody(BaseModel):
    """升/降配请求体：目标规格 + 显式确认（改规格时实例会重启）。"""

    ocpus: int
    memory_gb: int
    confirm: bool = False


def _unwrap_instance(data):
    """get_instance 返回的 JSON 可能包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"] or {}
    return data or {}


def _safe_public_ip(cli, instance_id):
    """取实例公网 IP，失败时返回 None（不让单台实例的 IP 查询拖垮列表）。"""
    try:
        return get_public_ip(cli, instance_id)
    except OCIError:
        return None


def _detail_extras(cli, compartment_id, instance_id, availability_domain):
    """详情页的附加字段：内网 IP / IPv6 地址列表 / 启动盘信息。

    启动盘信息含大小（GB）、OCID、VPU（vpus-per-gb）、状态
    （lifecycle-state）与挂载状态（attachment-state，供前端启动盘
    弹窗与卡片显示“已附加”）。

    全部尽力而为：任一查询失败（包括 OCI CLI 不可用）时该字段为 None，
    不影响详情主体的返回。IPv6 无地址时为空列表（与“查不到”区分）。
    """
    extras = {"private_ip": None, "ipv6_addresses": None,
              "boot_volume_size_gbs": None, "boot_volume_id": None,
              "boot_volume_vpus_per_gb": None, "boot_volume_state": None,
              "boot_volume_attachment_state": None}
    try:
        vnics = list_vnics(cli, instance_id)
        primary = next((v for v in vnics if v.get("is-primary")), None)
        if primary is not None:
            extras["private_ip"] = primary.get("private-ip")
            try:
                ipv6s = list_ipv6_for_vnic(cli, primary["id"])
                extras["ipv6_addresses"] = [v.get("ip-address")
                                            for v in ipv6s]
            except Exception:  # noqa: BLE001 - 附加字段失败不影响详情
                pass
    except Exception:  # noqa: BLE001 - 同上
        pass
    try:
        if availability_domain:
            attachments = list_boot_volume_attachments(
                cli, compartment_id, availability_domain)
            for att in attachments:
                if att.get("instance-id") == instance_id:
                    vol = get_boot_volume(cli, att["boot-volume-id"]) or {}
                    extras["boot_volume_size_gbs"] = vol.get("size-in-gbs")
                    extras["boot_volume_id"] = att.get("boot-volume-id")
                    extras["boot_volume_vpus_per_gb"] = vol.get(
                        "vpus-per-gb")
                    extras["boot_volume_state"] = vol.get("lifecycle-state")
                    extras["boot_volume_attachment_state"] = att.get(
                        "lifecycle-state")
                    break
    except Exception:  # noqa: BLE001 - 同上
        pass
    return extras


def format_instance(inst, public_ip, region):
    """把 list_instances 的条目转成 面板形状（阶段 5 工作包 E）。

    新形状（camelCase 字段）：
    {id, name, publicIp, shape, state, ocpus, memoryInGBs, region,
     timeCreated}。ocpus / memoryInGBs 取自 shape-config（list_instances
    需带 include_shape_config=True，否则为 None）。

    过渡期双写旧字段（display-name / lifecycle-state / time-created /
    public-ip），前端已改消费新字段，新字段是契约，旧字段后续可删。
    """
    cfg = inst.get("shape-config") or {}
    state = inst.get("lifecycle-state") or ""
    return {
        "id": inst.get("id"),
        "name": inst.get("display-name"),
        "publicIp": public_ip,
        "shape": inst.get("shape"),
        "state": state,
        "ocpus": cfg.get("ocpus"),
        "memoryInGBs": cfg.get("memory-in-gbs"),
        "region": region,
        "timeCreated": inst.get("time-created"),
        # 旧字段（过渡期双写）。
        "display-name": inst.get("display-name"),
        "lifecycle-state": state,
        "time-created": inst.get("time-created"),
        "public-ip": public_ip,
    }


@router.get("/api/instances")
def list_all(profile: str = Query(default=None),
             region: str = Query(default=None),
             state: str = Query(default="running"),
             refresh: bool = Query(default=False),
             username: str = Depends(get_current_user)):
    """列出该账号指定区域下的实例（面板形状，每项补公网 IP）。

    region 不传时用该 profile 的主区；传了则列该订阅区域的实例。
    state=running（默认）：只含 RUNNING 状态实例。
    state=all：含全部非 TERMINATED 实例（RUNNING/STOPPED/STOPPING 等），
    用于从面板启动已停实例；TERMINATED 的实例始终不含。
    refresh=true：绕过服务端缓存，强制重新查询（前端刷新按钮用）。
    """
    show_all = (state or "").lower() == "all"
    try:
        cli, compartment_id = resolve_profile(profile, region)
        profile_name = profile or config.DEFAULT_PROFILE
        cache_key = (profile_name, cli.region,
                     "all" if show_all else "running")
        if not refresh:
            with _LIST_CACHE_LOCK:
                entry = _LIST_CACHE.get(cache_key)
            if entry and time.monotonic() < entry["expires"]:
                return entry["payload"]
        raw = list_instances(cli, compartment_id,
                             include_shape_config=True)
        filtered = []
        for inst in raw:
            inst_state = inst.get("lifecycle-state") or ""
            if show_all:
                if inst_state == "TERMINATED":
                    continue
            elif inst_state != "RUNNING":
                continue
            filtered.append(inst)
        # 各实例的公网 IP 并行取（每台 1 次 list-vnics；map 保序，
        # 结果与 filtered 一一对应；_safe_public_ip 失败时返回 None）。
        if filtered:
            with ThreadPoolExecutor(max_workers=MAX_IP_WORKERS) as pool:
                ips = list(pool.map(
                    lambda inst: _safe_public_ip(cli, inst.get("id")),
                    filtered))
        else:
            ips = []
        instances = [format_instance(inst, ip, cli.region)
                     for inst, ip in zip(filtered, ips)]
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    payload = {"ok": True, "profile": profile_name, "region": cli.region,
               "instances": instances}
    with _LIST_CACHE_LOCK:
        _LIST_CACHE[cache_key] = {
            "expires": time.monotonic() + INSTANCE_LIST_CACHE_TTL,
            "payload": payload,
        }
    return payload


@router.post("/api/instances/batch")
def batch(body: BatchBody,
          profile: str = Query(default=None),
          region: str = Query(default=None),
          username: str = Depends(get_current_user)):
    """批量操作：action 为 start/stop/restart/terminate 之一。

    terminate 必须 confirm=true，默认永久删除各实例的引导卷。
    返回每台实例的成功/失败汇总。
    """
    if body.action not in ("start", "stop", "restart", "terminate"):
        return error_response(
            f"未知的批量动作：{body.action}"
            "（只支持 start/stop/restart/terminate）",
            status_code=400)
    if not body.instance_ids:
        return error_response("请至少选一台实例", status_code=400)
    if body.action == "terminate" and not body.confirm:
        return error_response("批量删除需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        # 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
        if body.action == "start":
            result = batch_start(cli, body.instance_ids)
        elif body.action == "stop":
            result = batch_stop(cli, body.instance_ids)
        elif body.action == "restart":
            result = batch_restart(cli, body.instance_ids)
        else:
            result = batch_terminate(cli, body.instance_ids)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "action": body.action,
            "succeeded": result["succeeded"], "failed": result["failed"]}


@router.get("/api/instances/{instance_id}")
def detail(instance_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """查单个实例详情（含公网 IP、内网 IP、IPv6 列表、启动盘大小）。"""
    try:
        cli, compartment_id = resolve_profile(profile, region)
        data = get_instance(cli, instance_id)
        detail_data = _unwrap_instance(data)
        public_ip = _safe_public_ip(cli, instance_id)
        extras = _detail_extras(cli, compartment_id, instance_id,
                                detail_data.get("availability-domain"))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance": detail_data, "public_ip": public_ip,
            "private_ip": extras["private_ip"],
            "ipv6_addresses": extras["ipv6_addresses"],
            "boot_volume_size_gbs": extras["boot_volume_size_gbs"],
            "boot_volume_id": extras["boot_volume_id"],
            "boot_volume_vpus_per_gb": extras["boot_volume_vpus_per_gb"],
            "boot_volume_state": extras["boot_volume_state"],
            "boot_volume_attachment_state":
                extras["boot_volume_attachment_state"]}


def _action(instance_id, profile, func, action_name, region=None):
    """启动/停止/重启的公共逻辑。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        func(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "action": action_name}


@router.post("/api/instances/{instance_id}/start")
def start(instance_id: str,
          profile: str = Query(default=None),
          region: str = Query(default=None),
          username: str = Depends(get_current_user)):
    """启动实例。"""
    return _action(instance_id, profile, start_instance, "start",
                   region=region)


@router.post("/api/instances/{instance_id}/stop")
def stop(instance_id: str,
         profile: str = Query(default=None),
         region: str = Query(default=None),
         username: str = Depends(get_current_user)):
    """停止实例。"""
    return _action(instance_id, profile, stop_instance, "stop",
                   region=region)


@router.post("/api/instances/{instance_id}/restart")
def restart(instance_id: str,
            profile: str = Query(default=None),
            region: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """重启实例。"""
    return _action(instance_id, profile, restart_instance, "restart",
                   region=region)


@router.get("/api/instances/{instance_id}/console")
def console(instance_id: str,
            profile: str = Query(default=None),
            region: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """获取串行控制台连接信息（含 ssh 连接命令与指纹）。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        connection = get_serial_console(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance_id": instance_id, "console": connection}


@router.post("/api/instances/{instance_id}/rename")
def rename(instance_id: str,
           body: RenameBody,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """更改实例显示名（新名字不能为空，见 rename_instance 的校验）。"""
    if not (body.name or "").strip():
        return error_response("新实例名不能为空", status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        detail = rename_instance(cli, instance_id, body.name)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "instance": detail}


@router.post("/api/instances/{instance_id}/reinstall")
def reinstall(instance_id: str,
              body: ReinstallBody,
              profile: str = Query(default=None),
              region: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """重装系统：必须 confirm=true 且 confirm_name 等于实例当前显示名。

    重装会清空系统盘数据，实例 OCID 与 IP 保留不变；
    image_id 不传时用当前镜像重装。
    """
    if not body.confirm:
        return error_response("重装系统需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        current = get_instance(cli, instance_id)
        detail = (current.get("data") if isinstance(current, dict)
                  and "data" in current else current) or {}
        current_name = detail.get("display-name")
        if body.confirm_name != current_name:
            return error_response(
                "确认名与实例当前名不符，请完整输入实例名再确认",
                status_code=400)
        result = reinstall_instance(cli, instance_id,
                                    image_id=body.image_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "instance": result}


@router.post("/api/instances/{instance_id}/terminate")
def terminate(instance_id: str,
              body: TerminateBody,
              profile: str = Query(default=None),
              region: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """终止实例（必须 confirm=true；默认永久删除引导卷，不留着扣钱）。

    preserve_boot_volume=true 时保留引导卷（前端“删除（保留启动盘）”
    用）：引导卷会变成孤儿盘，占用 200GB 免费额度，之后可在启动盘
    页面删除。
    """
    if not body.confirm:
        return error_response("终止实例需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        # 默认 preserve_boot_volume=False：终止时永久删除引导卷。
        terminate_instance(cli, instance_id,
                           preserve_boot_volume=body.preserve_boot_volume)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "action": "terminate",
            "preserve_boot_volume": body.preserve_boot_volume}


@router.post("/api/instances/{instance_id}/change-ip")
def change_ip(instance_id: str,
              body: ChangeIpBody,
              profile: str = Query(default=None),
              region: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """换公网 IP（必须 confirm=true；旧 IP 立即释放，无法找回）。

    复用 oci/network.py 的 change_public_ip：先删旧临时 IP 再创建并
    绑定新保留 IP（OCI 拒绝给已绑公网 IP 的内网 IP 再绑一个，409）。
    返回 {old_ip, new_ip}；换 IP 后指向旧 IP 的 DNS/反代需同步更新。
    """
    if not body.confirm:
        return error_response("换 IP 需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        old_ip = _safe_public_ip(cli, instance_id)
        new_ip = change_public_ip(cli, compartment_id, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id,
            "old_ip": old_ip, "new_ip": new_ip}


@router.post("/api/instances/{instance_id}/ipv6")
def add_ipv6(instance_id: str,
             profile: str = Query(default=None),
             region: str = Query(default=None),
             username: str = Depends(get_current_user)):
    """给实例主 VNIC 附加一个 IPv6，返回新地址。"""
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        vnic = get_primary_vnic(cli, instance_id)
        created = create_ipv6(cli, vnic["id"]) or {}
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id,
            "ipv6": created.get("ip-address")}


@router.delete("/api/instances/{instance_id}/ipv6")
def delete_all_ipv6(instance_id: str,
                    profile: str = Query(default=None),
                    region: str = Query(default=None),
                    username: str = Depends(get_current_user)):
    """删除实例主 VNIC 的全部 IPv6。

    无 IPv6 时返回 ok + 提示，不报错。本接口一次删全部（与面板“删除 IPv6”按钮语义一致）。
    """
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        vnic = get_primary_vnic(cli, instance_id)
        ipv6s = list_ipv6_for_vnic(cli, vnic["id"])
        if not ipv6s:
            return {"ok": True, "instance_id": instance_id, "deleted": [],
                    "message": "该实例没有 IPv6，无需删除"}
        deleted = []
        for entry in ipv6s:
            delete_ipv6(cli, entry["id"])
            deleted.append(entry.get("ip-address"))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "deleted": deleted}


@router.post("/api/instances/{instance_id}/resize")
def resize(instance_id: str,
           body: ResizeBody,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """升/降配 A1.Flex 实例（必须 confirm=true；改规格时实例会重启）。

    只支持 VM.Standard.A1.Flex（E2.1.Micro 是固定规格）；A1 按免费额度
    校验单台上限 4 核/24GB（与创建页同一校验，见
    oci/quota.py 的 check_a1_single_instance_cap）。
    """
    if not body.confirm:
        return error_response("升/降配需要确认：请求体须含 confirm=true",
                              status_code=400)
    if body.ocpus < 1 or body.memory_gb < 1:
        return error_response("OCPU 与内存都必须大于 0", status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        detail_data = _unwrap_instance(get_instance(cli, instance_id))
        shape = detail_data.get("shape") or ""
        if shape == SHAPE_E2_MICRO:
            return error_response(
                "E2.1.Micro 是固定规格（1 核 / 1GB），不支持升/降配",
                status_code=400)
        if shape != SHAPE_A1_FLEX:
            return error_response(
                f"规格 {shape} 不支持升/降配，只支持 A1.Flex",
                status_code=400)
        cap_error = check_a1_single_instance_cap(body.ocpus,
                                                 body.memory_gb)
        if cap_error:
            return error_response(cap_error, status_code=400)
        resize_instance(cli, instance_id, body.ocpus, body.memory_gb)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id,
            "ocpus": body.ocpus, "memory_gb": body.memory_gb,
            "message": "改规格已提交，实例可能会重启，请稍后查看状态"}


@router.post("/api/instances/{instance_id}/repair")
def repair(instance_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """尝试修复实例的公网 IP（保守：只检查 + 补缺失的公网 IP 附加）。

    返回修复报告 {state, public_ip, repaired, new_ip, checks}；
    公网 IP 正常时不做任何改动。
    """
    try:
        cli, compartment_id = resolve_profile(profile, region)
        report = repair_instance_ip(cli, compartment_id, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "report": report}


@router.get("/api/instances/{instance_id}/boot-backups")
def boot_backups(instance_id: str,
                 profile: str = Query(default=None),
                 region: str = Query(default=None),
                 username: str = Depends(get_current_user)):
    """该实例当前启动盘的 AVAILABLE 备份列表（引导卷恢复的备份选择用）。

    返回 {ok, boot_volume_id, backups}；backups 每项是精简字典
    （id / display-name / size-in-gbs / lifecycle-state / time-created）。
    """
    try:
        cli, compartment_id = resolve_profile(profile, region)
        attachment = get_boot_volume_attachment(cli, compartment_id,
                                                instance_id)
        if attachment is None:
            return error_response("找不到该实例当前挂载的启动盘",
                                  status_code=400)
        boot_volume_id = attachment.get("boot-volume-id")
        backups = list_boot_volume_backups(cli, compartment_id,
                                           boot_volume_id=boot_volume_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    available = [b for b in backups
                 if (b.get("lifecycle-state") or "").upper() == "AVAILABLE"]
    return {"ok": True, "boot_volume_id": boot_volume_id,
            "backups": available}


@router.post("/api/instances/{instance_id}/restore-boot-volume")
def restore_boot_volume(instance_id: str,
                        body: RestoreBootVolumeBody,
                        profile: str = Query(default=None),
                        region: str = Query(default=None),
                        username: str = Depends(get_current_user)):
    """用备份替换实例的启动盘（停机换盘，原地恢复）。

    body 必须含 confirm=true 且 backup_id。校验：实例是 RUNNING 或
    STOPPED、备份属于该实例当前启动盘且 AVAILABLE、启动盘额度够放
    新盘。校验通过后起后台任务跑换盘，返回 {ok, task_id}，进度在
    任务页看，完成/失败时双通道通知。同一实例已有换盘任务在跑时
    返回 409。
    """
    if not body.confirm:
        return error_response("请确认恢复（confirm=true）",
                              status_code=400)
    if not (body.backup_id or "").strip():
        return error_response("backup_id 不能为空", status_code=400)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        detail = get_instance(cli, instance_id)
        if isinstance(detail, dict):
            detail = detail.get("data") or detail
        name = (detail or {}).get("display-name") or instance_id[-4:]
        state = ((detail or {}).get("lifecycle-state") or "").upper()
        if state not in ("RUNNING", "STOPPED"):
            return error_response(
                f"实例当前状态是 {state or '未知'}，只有运行中或已停止的"
                "实例才能恢复引导卷", status_code=400)
        attachment = get_boot_volume_attachment(cli, compartment_id,
                                                instance_id)
        if attachment is None:
            return error_response("找不到该实例当前挂载的启动盘",
                                  status_code=400)
        backup = get_boot_volume_backup(cli, body.backup_id.strip())
        if backup.get("boot-volume-id") != attachment.get("boot-volume-id"):
            return error_response("该备份不属于这台实例的启动盘",
                                  status_code=400)
        if (backup.get("lifecycle-state") or "").upper() != "AVAILABLE":
            return error_response(
                f"备份当前状态是 {backup.get('lifecycle-state') or '未知'}，"
                "只有 AVAILABLE 状态的备份才能恢复", status_code=400)
        ok, remaining = check_boot_volume_space(
            cli, compartment_id, backup.get("size-in-gbs") or 0)
        if not ok:
            return error_response(
                f"启动盘额度不足：还剩 {remaining}GB，"
                f"新盘需要 {backup.get('size-in-gbs')}GB。"
                "请先删除不用的启动盘再恢复。", status_code=400)
        notify = {"token": bot_config.TELEGRAM_BOT_TOKEN,
                  "chat_id": _notify_chat_id()}
        task_id = start_bootswap_task(
            TASKS, cli, compartment_id, instance_id, name,
            backup["id"], backup.get("display-name") or "?",
            notify=notify)
    except ValueError as exc:
        message = str(exc)
        if "正在换盘" in message:
            return error_response(message, status_code=409)
        return error_response(message, status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "task_id": task_id,
            "message": "引导卷恢复已开始，进度请在任务页查看"}
