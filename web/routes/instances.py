#!/usr/bin/env python3
"""实例路由：列表 / 详情 / 启动 / 停止 / 重启 / 终止 / 批量 / 改名 / 重装 /
串行控制台 / 换 IP / IPv6 / 升降配 / 尝试修复。

  - GET /api/instances：列表（面板形状，见 format_instance；只含
    RUNNING 状态实例，每项补上公网 IP，取不到时为 None；每项另含
    缓存的 cpuModel，未检测时为 None，列表返回后后台线程自动补测）。
    各实例的公网 IP 并行取（线程池）；列表结果服务端缓存 30 秒，
    手动刷新时传 refresh=1 绕过缓存；写操作成功后清对应账号的缓存。
  - GET /api/instances/{id}：详情（含公网 IP、内网 IP、IPv6 列表、
    启动盘大小 / OCID / VPU / 状态；附加字段尽力而为，查不到时为 None；
    另含 cpu_model，无缓存且运行中时同步 SSH 检测一次）。
  - POST /api/instances/{id}/cpu-model：强制重新检测 CPU 型号。
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
    AVAILABLE 备份列表（引导卷恢复的备份选择用；include_all=true
    时返回全部未删除备份，删除历史备份用）。
  - POST /api/instances/{id}/boot-backups：给该实例当前启动盘做
    FULL 备份（免费账号满 5 个时返回 409 提示先删再创建，不自动删）。
  - POST /api/instances/{id}/restore-boot-volume：用备份替换实例的
    启动盘（body {backup_id, confirm}，confirm 必须为 true；停机换盘，
    原地恢复；后台任务跑，返回 task_id，进度在任务页看）。

profile 与 region 通过查询参数传（如 ?profile=ACCOUNT2&region=us-sanjose-1），
默认 DEFAULT 账号的主区。
"""

import logging
import threading
import time

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from oci.boot_backup import (
    check_backup_quota,
    create_boot_volume_backup,
    get_boot_volume_backup,
    list_boot_volume_backups,
)
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
    reinstall_instance,
    rename_instance,
    resize_instance,
    restart_instance,
    slim_instances,
    start_instance,
    stop_instance,
    terminate_instance,
)
from oci.network import (
    attach_public_ips_bulk,
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
from tgbot.backup_watch import start_backup_watch
from tgbot.bark import load_bark_config
from tgbot.cpu_model import (
    cached_cpu_model,
    delete_cpu_model,
    get_cpu_model,
    host_for_ip,
    load_cpu_models,
)
from tgbot.handlers_client import (
    RemoteCommandError,
    put_remote_file,
    run_remote_command,
)
from tgbot.monitoring import guard_ensure_vnstat_command
from tgbot.paid_profiles import is_paid_profile
from tgbot.watchdog import (
    WatchdogError,
    disable_watchdog,
    guard_status_label,
    install_watchdog,
    parse_probe_output,
    probe_command,
)
from web import config
from web import demo_data
from web.oci_cache import (
    cached_raw_instances,
    cached_raw_vnic_attachments,
    cached_vnic_public_ips,
)
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.quota import clear_quota_cache
from web.routes.volumes import clear_volumes_cache
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)
from web.tasks import TASKS, start_bootswap_task

router = APIRouter()

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# Instance list server cache TTL (seconds): each list call runs 1 + N OCI CLI invocations (1 list + 1 list-vnics per instance for the public IP), ~10s serially; repeat visits within 30s return the cache, and a manual refresh (refresh=1) bypasses it. 实例列表服务端缓存 TTL（秒）：列表接口每次要跑 1 + N 次 OCI CLI（1 次 list + 每台实例 1 次 list-vnics 取公网 IP），串行约 10 秒；30 秒内重复访问直接返回缓存，手动刷新（refresh=1）绕过缓存。
INSTANCE_LIST_CACHE_TTL = 30
# =====================================================

# Instance list cache: (profile, state) -> {"expires": monotonic-clock expiry, "payload": response body}. 实例列表缓存：(profile, state) -> {"expires": 单调时钟过期点, "payload": 响应体}。
# Memory only, never written to disk; the panel has a single admin, so no per-user isolation is needed. 只放内存不写盘；面板是单管理员，无需按用户隔离。
_LIST_CACHE = {}
_LIST_CACHE_LOCK = threading.Lock()

# In-flight set for background CPU-model detection: at most one detection thread per instance at a time. CPU 型号后台检测的在途集合：同一台实例同时只跑一个检测线程。
_CPU_FETCH_IN_FLIGHT = set()
_CPU_FETCH_LOCK = threading.Lock()
# Cap on concurrent CPU-model SSH detections (round 7): each detection
# is a Paramiko SSH handshake (crypto-heavy); without a cap, a list
# with N uncached instances fires N handshakes at once, right on top
# of the menu-click burst. At most 2 run at once; the rest wait.
# CPU 型号后台检测的并发上限（第 7 轮）：每次检测都是一次 Paramiko
# SSH 握手（密码学开销大）；不设上限时，N 台未缓存实例会同时起 N 个
# 握手，正好叠在点菜单的突发上。同时最多跑 2 个，其余排队等。
_CPU_FETCH_SEMAPHORE = threading.Semaphore(2)
# Cooldown between detection attempts for one instance (seconds):
# a failed attempt (host unreachable, guard key missing, SSH refused)
# used to be retried on every list call (every 30 s cache expiry),
# stacking SSH attempts onto every menu switch. Now an instance is
# not re-attempted within 60 s of its last attempt. A cached model
# still skips detection entirely (checked before this).
# 同一台实例两次检测尝试之间的冷却（秒）：失败的尝试以前每次列表
# 刷新（每 30 秒缓存过期）都会重试，SSH 握手叠在每次切菜单上；
# 现在 60 秒内不重复尝试。已有缓存型号的仍直接跳过（先检查）。
_CPU_FETCH_COOLDOWN_SECONDS = 60
_CPU_FETCH_LAST_ATTEMPT = {}


def _refresh_cpu_models_background(instances, ips, cpu_models):
    """列表返回后，后台检测未缓存 CPU 型号的运行中实例（不阻塞列表）。

    instances 与 ips 一一对应；只有 RUNNING、有公网 IP、无缓存、
    且不在检测中、且不在 60 秒冷却期内的实例才起线程。检测结果写进
    缓存文件，下次列表（缓存过期或手动刷新后）即可显示。
    并发检测最多 2 个（信号量），其余线程排队等，不会同时起 N 个
    SSH 握手冲 CPU。
    """
    now = time.monotonic()
    for inst, ip in zip(instances, ips):
        instance_id = inst.get("id")
        if not instance_id or not ip:
            continue
        if (inst.get("lifecycle-state") or "") != "RUNNING":
            continue
        if (cpu_models or {}).get(instance_id, {}).get("model"):
            continue
        with _CPU_FETCH_LOCK:
            if instance_id in _CPU_FETCH_IN_FLIGHT:
                continue
            last = _CPU_FETCH_LAST_ATTEMPT.get(instance_id)
            if (last is not None
                    and now - last < _CPU_FETCH_COOLDOWN_SECONDS):
                continue
            _CPU_FETCH_IN_FLIGHT.add(instance_id)
            _CPU_FETCH_LAST_ATTEMPT[instance_id] = now

        def _worker(ocid=instance_id, public_ip=ip):
            with _CPU_FETCH_SEMAPHORE:
                try:
                    get_cpu_model(ocid, host_for_ip(public_ip),
                                  run_remote_command)
                finally:
                    with _CPU_FETCH_LOCK:
                        _CPU_FETCH_IN_FLIGHT.discard(ocid)

        threading.Thread(target=_worker, daemon=True).start()


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


def format_instance(inst, public_ip, region, cpu_models=None):
    """把 list_instances 的条目转成 面板形状（阶段 5 工作包 E）。

    新形状（camelCase 字段）：
    {id, name, publicIp, shape, state, ocpus, memoryInGBs, region,
     timeCreated, cpuModel}。ocpus / memoryInGBs 取自 shape-config
    （list_instances 需带 include_shape_config=True，否则为 None）。
    cpuModel 是缓存的 CPU 型号（如 "AMD EPYC 7J13"），未检测时为
    None（列表只读缓存不 SSH，后台检测线程会补上）。

    过渡期双写旧字段（display-name / lifecycle-state / time-created /
    public-ip），前端已改消费新字段，新字段是契约，旧字段后续可删。
    """
    cfg = inst.get("shape-config") or {}
    state = inst.get("lifecycle-state") or ""
    cpu_entry = (cpu_models or {}).get(inst.get("id")) or {}
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
        "cpuModel": cpu_entry.get("model"),
        # Old fields (written alongside during the transition). 旧字段（过渡期双写）。
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
    if demo_data.is_demo():
        # Demo mode: return simulated instances, no OCI calls, no cache.
        # 演示模式：返回模拟实例，不调 OCI，不走缓存。
        try:
            return demo_data.demo_instances_payload(profile, region,
                                                    show_all)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
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
        raw = slim_instances(
            cached_raw_instances(cli, compartment_id, refresh=refresh),
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
        # 批量取各实例的公网 IP。挂载列表与 VNIC 公网 IP 映射走共享
        # 缓存（第 7 轮）：命中时 0 次 SDK 调用；缓存路径本身失败
        # （OCI 报错等）时回退到直调 SDK 的旧路径（其内部仍有逐台
        # 回退），列表页不因此报错。
        if filtered:
            try:
                attachments = cached_raw_vnic_attachments(
                    cli, compartment_id, refresh=refresh)
                ip_map = cached_vnic_public_ips(
                    cli, compartment_id, refresh=refresh)
                ips = attach_public_ips_bulk(
                    cli, filtered, compartment_id,
                    attachments=attachments, ip_map=ip_map)
            except Exception as exc:  # noqa: BLE001 - 回退旧路径
                logger.warning("VNIC 缓存路径失败，回退直调：%s", exc)
                ips = attach_public_ips_bulk(cli, filtered, compartment_id)
        else:
            ips = []
        cpu_models = load_cpu_models()
        instances = [format_instance(inst, ip, cli.region, cpu_models)
                     for inst, ip in zip(filtered, ips)]
        _refresh_cpu_models_background(filtered, ips, cpu_models)
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
    if demo_data.is_demo():
        # Demo mode: fake success, no OCI call, no state change.
        # 演示模式：假成功，不调 OCI、不改状态。
        return demo_data.fake_success(
            {"start": "批量启动", "stop": "批量停止",
             "restart": "批量重启", "terminate": "批量删除"}.get(
                body.action, body.action),
            action=body.action, succeeded=list(body.instance_ids),
            failed=[])
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        # Call the module-level imported function directly (do not pre-build a dict), so tests can patch it. 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
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
    clear_cloud_report_cache()
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
    if body.action == "terminate":
        # Batch terminate permanently deletes boot volumes by default, so the boot-volume list cache must be cleared too. 批量终止默认永删引导卷，启动盘列表缓存也要清。
        clear_volumes_cache(profile or config.DEFAULT_PROFILE)
        for instance_id in result["succeeded"]:
            delete_cpu_model(instance_id)
    return {"ok": True, "action": body.action,
            "succeeded": result["succeeded"], "failed": result["failed"]}


@router.get("/api/instances/{instance_id}")
def detail(instance_id: str,
           profile: str = Query(default=None),
           region: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """查单个实例详情（含公网 IP、内网 IP、IPv6 列表、启动盘大小）。

    另含 cpu_model：缓存的 CPU 型号；无缓存且实例运行中时同步
    SSH 检测一次（约 2-4 秒），检测失败为 None。
    """
    if demo_data.is_demo():
        # Demo mode: simulated instance detail, no OCI/SSH calls.
        # 演示模式：模拟实例详情，不调 OCI、不 SSH。
        detail = demo_data.demo_instance_detail(instance_id)
        if detail is None:
            return error_response(f"找不到实例：{instance_id}",
                                  status_code=404)
        return detail
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
    cpu_model = cached_cpu_model(instance_id)
    if cpu_model is None \
            and detail_data.get("lifecycle-state") == "RUNNING" \
            and public_ip:
        cpu_model = get_cpu_model(instance_id, host_for_ip(public_ip),
                                  run_remote_command)
    return {"ok": True, "instance": detail_data, "public_ip": public_ip,
            "cpu_model": cpu_model,
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
    if demo_data.is_demo():
        # Demo mode: fake success, no OCI call, no state change.
        # 演示模式：假成功，不调 OCI、不改状态。
        return demo_data.fake_success(
            {"start": "启动实例", "stop": "停止实例",
             "restart": "重启实例", "terminate": "终止实例"}.get(
                action_name, action_name),
            instance_id=instance_id, action=action_name)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        func(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    clear_cloud_report_cache()
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
    if action_name == "terminate":
        delete_cpu_model(instance_id)
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
    if demo_data.is_demo():
        # Demo mode: no real serial console.
        # 演示模式：无真实串行控制台。
        return {"ok": True, "instance_id": instance_id,
                "console": {"demo": True,
                            "message": "演示模式：无真实串行控制台"}}
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
    if demo_data.is_demo():
        # Demo mode: fake success, no OCI call, no state change.
        # 演示模式：假成功，不调 OCI、不改状态。
        return demo_data.fake_success(
            "改名",
            instance_id=instance_id,
            instance={"id": instance_id,
                      "display-name": body.name.strip()})
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
    if demo_data.is_demo():
        # Demo mode: fake success (skip the confirm-name check: the demo
        # detail is simulated, so the check would be meaningless).
        # 演示模式：假成功（跳过确认名校验：演示详情是模拟的，校验无意义）。
        detail = demo_data.demo_instance_detail(instance_id)
        return demo_data.fake_success(
            "重装系统",
            instance_id=instance_id,
            instance=(detail or {}).get("instance"))
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
    if demo_data.is_demo():
        # Demo mode: fake success, no OCI call, no state change.
        # 演示模式：假成功，不调 OCI、不改状态。
        return demo_data.fake_success(
            "终止实例",
            instance_id=instance_id, action="terminate",
            preserve_boot_volume=body.preserve_boot_volume)
    try:
        cli, _compartment_id = resolve_profile(profile, region)
        # Default preserve_boot_volume=False: terminating permanently deletes the boot volume. 默认 preserve_boot_volume=False：终止时永久删除引导卷。
        terminate_instance(cli, instance_id,
                           preserve_boot_volume=body.preserve_boot_volume)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    # Terminate permanently deletes the boot volume by default (keeping it leaves an orphan volume), so the boot-volume list cache must be cleared too. 终止默认永删引导卷（保留时变孤儿盘），启动盘列表缓存也要清。
    clear_volumes_cache(profile or config.DEFAULT_PROFILE)
    clear_cloud_report_cache()
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
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
    if demo_data.is_demo():
        # Demo mode: fake success with a fake new TEST-NET IP.
        # 演示模式：假成功，返回一个假的新 TEST-NET IP。
        detail = demo_data.demo_instance_detail(instance_id)
        old_ip = (detail or {}).get("public_ip")
        return demo_data.fake_success(
            "换 IP",
            instance_id=instance_id,
            old_ip=old_ip, new_ip="203.0.113.99")
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
    if demo_data.is_demo():
        # Demo mode: fake success with a fake documentation-range IPv6.
        # 演示模式：假成功，返回一个假的文档段 IPv6。
        return demo_data.fake_success(
            "添加 IPv6",
            instance_id=instance_id, ipv6="2001:db8::99")
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
    if demo_data.is_demo():
        # Demo mode: fake success, report the demo IPv6 addresses as deleted.
        # 演示模式：假成功，把演示 IPv6 当作已删除返回。
        detail = demo_data.demo_instance_detail(instance_id)
        deleted = (detail or {}).get("ipv6_addresses") or []
        if not deleted:
            return demo_data.fake_success(
                "删除 IPv6",
                instance_id=instance_id, deleted=[],
                message="演示模式：该实例没有 IPv6，无需删除")
        return demo_data.fake_success(
            "删除 IPv6",
            instance_id=instance_id, deleted=deleted)
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
    if demo_data.is_demo():
        # Demo mode: validate the shape against the demo data (same rules
        # as the real path), then fake success without calling OCI.
        # 演示模式：按演示数据做同样的形状校验，通过后假成功，不调 OCI。
        detail = demo_data.demo_instance_detail(instance_id)
        shape = ((detail or {}).get("instance") or {}).get("shape") or ""
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
        return demo_data.fake_success(
            "升/降配",
            instance_id=instance_id,
            ocpus=body.ocpus, memory_gb=body.memory_gb,
            message="演示模式：改规格未真实执行")
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
    # Resizing changes the instance shape, so the A1 quota usage changes too. 改规格会改变实例形状，A1 配额用量也变了。
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
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
    if demo_data.is_demo():
        # Demo mode: fake report from the simulated instance, no repair.
        # 演示模式：按模拟实例返回假报告，不做任何修复。
        detail = demo_data.demo_instance_detail(instance_id)
        state = ((detail or {}).get("instance") or {}).get(
            "lifecycle-state") or ""
        return demo_data.fake_success(
            "尝试修复",
            instance_id=instance_id,
            report={"state": state,
                    "public_ip": (detail or {}).get("public_ip"),
                    "repaired": False, "new_ip": None,
                    "checks": ["演示模式：未检查真实实例"]})
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
                 include_all: bool = Query(default=False),
                 username: str = Depends(get_current_user)):
    """该实例当前启动盘的备份列表。

    默认只返回 AVAILABLE 备份（引导卷恢复的备份选择用）；
    include_all=true 时返回全部未删除备份（删除历史备份用）。

    返回 {ok, boot_volume_id, backups}；backups 每项是精简字典
    （id / display-name / size-in-gbs / lifecycle-state / time-created）。
    默认模式下 backups 为空时另含 other_available_count（账号里其他
    启动盘的可用备份数，供前端提示用：删盘不会删备份，旧备份还在）。
    """
    if demo_data.is_demo():
        # Demo mode: simulated backups of the instance's boot volume.
        # 演示模式：模拟该实例启动盘的备份。
        detail = demo_data.demo_instance_detail(instance_id)
        if detail is None:
            return error_response(f"找不到实例：{instance_id}",
                                  status_code=404)
        boot_volume_id = detail.get("boot_volume_id")
        try:
            name, _region, _comp = demo_data.resolve_demo_profile(
                profile, region)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        payload = demo_data.demo_backups_payload(
            name, boot_volume_id=boot_volume_id)
        backups = payload["backups"]
        if not include_all:
            backups = [b for b in backups
                       if (b.get("lifecycle-state") or "").upper()
                       == "AVAILABLE"]
        result = {"ok": True, "boot_volume_id": boot_volume_id,
                  "backups": backups}
        if not include_all and not backups:
            all_backups = demo_data.demo_backups_payload(name)["backups"]
            result["other_available_count"] = sum(
                1 for b in all_backups
                if (b.get("lifecycle-state") or "").upper() == "AVAILABLE")
        return result
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
    if not include_all:
        backups = [b for b in backups
                   if (b.get("lifecycle-state") or "").upper()
                   == "AVAILABLE"]
    result = {"ok": True, "boot_volume_id": boot_volume_id,
              "backups": backups}
    if not include_all and not backups:
        # When the current disk has no usable backup, also count usable backups of other disks in the account (e.g. backups of the old disk from before a swap), so the frontend can explain clearly in the prompt and the user does not think deleting the disk also deleted the backups. A failed lookup counts as 0 and does not affect the main flow. 当前盘没可用备份时，顺便数一下账号里别的盘的可用备份（比如换盘前旧盘的备份），前端在提示里说明白，免得用户以为删盘把备份也删了。查不到就当 0，不影响主流程。
        try:
            all_backups = list_boot_volume_backups(cli, compartment_id)
            result["other_available_count"] = sum(
                1 for b in all_backups
                if (b.get("lifecycle-state") or "").upper() == "AVAILABLE")
        except Exception:  # noqa: BLE001 - 查不到旧备份也不影响主流程
            result["other_available_count"] = 0
    return result


@router.post("/api/instances/{instance_id}/boot-backups")
def create_boot_backup(instance_id: str,
                       profile: str = Query(default=None),
                       region: str = Query(default=None),
                       username: str = Depends(get_current_user)):
    """给该实例当前启动盘做 FULL 全量备份。

    付费账号直接创建；免费账号满 5 个时返回 409（不自动删最早备份，
    提示先删除再创建，前端弹出删除历史备份弹窗），响应里带
    full/count/limit；未满直接创建。
    """
    if demo_data.is_demo():
        # Demo mode: fake success with a fake backup record.
        # 演示模式：假成功，返回一条假备份记录。
        detail = demo_data.demo_instance_detail(instance_id)
        if detail is None:
            return error_response(f"找不到实例：{instance_id}",
                                  status_code=404)
        fake_backup = {
            "backupId": "ocid1.bootvolumebackup.oc1..demobackup0099",
            "displayName": "DEMO-backup-20261125-1200",
            "bootVolumeId": detail.get("boot_volume_id"),
            "sizeInGBs": detail.get("boot_volume_size_gbs"),
            "lifecycleState": "CREATING",
            "sourceType": "MANUAL",
            "type": "FULL",
            "timeCreated": "2026-01-02T12:00:00.000000+00:00",
            "imageId": None,
            "id": "ocid1.bootvolumebackup.oc1..demobackup0099",
            "display-name": "DEMO-backup-20261125-1200",
            "boot-volume-id": detail.get("boot_volume_id"),
            "size-in-gbs": detail.get("boot_volume_size_gbs"),
            "lifecycle-state": "CREATING",
            "source-type": "MANUAL",
            "time-created": "2026-01-02T12:00:00.000000+00:00",
            "image-id": None,
        }
        return demo_data.fake_success(
            "创建备份",
            backup=fake_backup, count=3, limit=5)
    try:
        cli, compartment_id = resolve_profile(profile, region)
        prof = profile or config.DEFAULT_PROFILE
        attachment = get_boot_volume_attachment(cli, compartment_id,
                                                instance_id)
        if attachment is None:
            return error_response("找不到该实例当前挂载的启动盘",
                                  status_code=400)
        paid = is_paid_profile(prof)
        count = limit = 0
        if not paid:
            count, limit, full, _oldest = check_backup_quota(
                cli, compartment_id)
            if full:
                return error_response(
                    f"备份已满 {count}/{limit} 个（免费额度），"
                    "请先删除旧备份再创建。",
                    status_code=409,
                    extra={"full": True, "count": count, "limit": limit})
        backup = create_boot_volume_backup(
            cli, attachment.get("boot-volume-id"))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    # After a backup is created successfully, start a background thread: once the backup is AVAILABLE, notify via both channels (same as the bot-side creation point; silently skipped when chat_id is unavailable). 备份创建成功后起后台线程：等备份 AVAILABLE 后双通道通知（与机器人端创建点同款；chat_id 拿不到时静默跳过）。
    chat_id = _notify_chat_id()
    if chat_id is not None:
        try:
            detail = get_instance(cli, instance_id)
            if isinstance(detail, dict):
                detail = detail.get("data") or detail
            inst_name = (detail or {}).get("display-name") or instance_id[-4:]
        except Exception:  # noqa: BLE001 - 查不到实例名就用 id 尾缀
            inst_name = instance_id[-4:]
        start_backup_watch(cli, backup["id"], inst_name,
                           backup.get("display-name") or "?",
                           bot_config.TELEGRAM_BOT_TOKEN, chat_id)
    result = {"ok": True, "backup": backup}
    if not paid:
        result["count"] = count + 1
        result["limit"] = limit
    return result


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
    if demo_data.is_demo():
        # Demo mode: fake success with a fake task id.
        # 演示模式：假成功，返回一个假任务 id。
        return demo_data.fake_success(
            "恢复引导卷",
            instance_id=instance_id, task_id="demo-restore-1",
            message="演示模式：引导卷恢复未真实执行，进度请在任务页查看")
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
    # A swap task creates a new boot volume and deletes the old one (the 30s cache heals itself; clear it once at startup). 换盘任务会新建一块启动盘并删旧盘（30 秒缓存自愈，启动时先清一次）。
    clear_volumes_cache(profile or config.DEFAULT_PROFILE)
    clear_quota_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "instance_id": instance_id, "task_id": task_id,
            "message": "引导卷恢复已开始，进度请在任务页查看"}


class WatchdogBody(BaseModel):
    """看门狗开关请求体：enabled=true 开启，false 关闭。"""

    enabled: bool = True


def _watchdog_host(cli, instance_id):
    """看门狗 SSH 主机记录（实例公网 IP + 专用密钥）。

    未配 MBOT_GUARD_SSH_KEY 或实例无公网 IP 时返回 None。
    """
    return host_for_ip(_safe_public_ip(cli, instance_id))


@router.get("/api/instances/{instance_id}/watchdog")
def watchdog_status(instance_id: str,
                    profile: str = Query(default=None),
                    region: str = Query(default=None),
                    username: str = Depends(get_current_user)):
    """查一台实例的流量看门狗状态（与机器人菜单 19 同款）。

    返回 {ok, watchdog: {installed, auto, enabled, mode, tripped,
    limited}, label}；SSH 连不上或未配密钥时返回 ok + watchdog=None
    与原因，不报错（前端只展示）。
    """
    if demo_data.is_demo():
        # Demo mode: no watchdog on simulated instances.
        # 演示模式：模拟实例上没有看门狗。
        return {"ok": True, "watchdog": None,
                "label": "演示模式：看门狗未安装"}
    try:
        cli, _compartment_id = resolve_profile(profile, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    host = _watchdog_host(cli, instance_id)
    if host is None:
        return {"ok": True, "watchdog": None,
                "label": "看门狗：未配置 SSH 密钥或无公网 IP"}
    try:
        _code, out, _err = run_remote_command(host, probe_command())
    except RemoteCommandError as exc:
        return {"ok": True, "watchdog": None,
                "label": f"看门狗：未知（{exc}）"}
    status = parse_probe_output(out)
    return {"ok": True, "watchdog": status,
            "label": guard_status_label(status)}


@router.post("/api/instances/{instance_id}/cpu-model")
def refresh_cpu_model(instance_id: str,
                      profile: str = Query(default=None),
                      region: str = Query(default=None),
                      username: str = Depends(get_current_user)):
    """强制重新检测一台实例的 CPU 型号（详情弹窗“重新检测”按钮用）。

    返回 {ok, cpu_model, message}；SSH 失败时 cpu_model 为 None
    并附原因（不报错，前端只展示）。检测成功后清列表缓存，
    下次打开列表即显示新型号。
    """
    if demo_data.is_demo():
        # Demo mode: return the simulated CPU model, no SSH detection.
        # 演示模式：返回模拟 CPU 型号，不 SSH 检测。
        detail = demo_data.demo_instance_detail(instance_id)
        model = (detail or {}).get("cpu_model")
        if model is None:
            return {"ok": True, "cpu_model": None,
                    "message": "演示模式：找不到该实例"}
        return {"ok": True, "cpu_model": model,
                "message": f"演示模式：CPU 型号：{model}"}
    try:
        cli, _compartment_id = resolve_profile(profile, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    host = _watchdog_host(cli, instance_id)
    if host is None:
        return {"ok": True, "cpu_model": None,
                "message": "该实例无公网 IP 或未配置 SSH 密钥，无法检测"}
    model = get_cpu_model(instance_id, host, run_remote_command,
                          force=True)
    if model is None:
        return {"ok": True, "cpu_model": None,
                "message": "检测失败：SSH 连不上该实例（守护密钥可能未装）"}
    clear_instance_list_cache(profile or config.DEFAULT_PROFILE)
    return {"ok": True, "cpu_model": model,
            "message": f"CPU 型号：{model}"}


@router.post("/api/instances/{instance_id}/watchdog")
def watchdog_toggle(instance_id: str,
                    body: WatchdogBody,
                    profile: str = Query(default=None),
                    region: str = Query(default=None),
                    username: str = Depends(get_current_user)):
    """开关一台实例的流量看门狗（与机器人菜单 19 同款逻辑）。

    enabled=true：安装并开启（传脚本 + 写 Bark 配置 + 挂 cron +
    测试推送）；false：摘 cron + 解除熔断/限速。安装可能耗时几分钟
    （新机要装 vnstat），前端请显示等待提示。
    """
    if demo_data.is_demo():
        # Demo mode: fake success, no SSH, no script install.
        # 演示模式：假成功，不 SSH、不装脚本。
        return demo_data.fake_success(
            "开启看门狗" if body.enabled else "关闭看门狗",
            instance_id=instance_id,
            action="enabled" if body.enabled else "disabled",
            message=("演示模式：看门狗已开启（假动作）"
                     if body.enabled else "演示模式：看门狗已关闭（假动作）"))
    try:
        cli, _compartment_id = resolve_profile(profile, region)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    host = _watchdog_host(cli, instance_id)
    if host is None:
        return error_response("该实例无公网 IP 或未配置 SSH 密钥",
                              status_code=400)
    try:
        if body.enabled:
            result = install_watchdog(host, load_bark_config(),
                                      run_remote_command, put_remote_file,
                                      guard_ensure_vnstat_command())
        else:
            result = disable_watchdog(host, run_remote_command)
    except WatchdogError as exc:
        return error_response(str(exc), status_code=400)
    except RemoteCommandError as exc:
        return error_response(str(exc), status_code=502)
    messages = {
        "installed": "看门狗已安装并开启",
        "upgraded": "看门狗已升级到自动识别端口版并开启",
        "enabled": "看门狗已重新开启",
        "already": "看门狗已经是开启状态",
        "disabled": "看门狗已关闭（cron 已摘，熔断/限速已解除）",
        "already_off": "看门狗已经是关闭状态",
    }
    message = messages.get(result.get("action"), "操作完成")
    if result.get("action") in ("installed", "upgraded", "enabled") \
            and not result.get("test_ok"):
        message += "（测试推送没发出去，请检查 Bark 配置）"
    return {"ok": True, "instance_id": instance_id,
            "action": result.get("action"), "message": message}
