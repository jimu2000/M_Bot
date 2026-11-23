#!/usr/bin/env python3
"""实例路由：列表 / 详情 / 启动 / 停止 / 重启 / 终止 / 批量 / 改名 / 重装 /
串行控制台。

  - GET /api/instances：列表（R-Bot 形状，见 format_instance；只含
    RUNNING 状态实例，每项补上公网 IP，取不到时为 None）。
  - GET /api/instances/{id}：详情（含公网 IP）。
  - GET /api/instances/{id}/console：串行控制台连接信息。
  - POST /api/instances/batch：批量开机/关机/重启/删除
    （body {action, instance_ids, confirm}，删除必须 confirm=true）。
  - POST /api/instances/{id}/start|stop|restart。
  - POST /api/instances/{id}/terminate：body 必须含 confirm=true，
    终止时默认永久删除引导卷（terminate_instance 的默认值，不要改）。
  - POST /api/instances/{id}/rename：body {name}，更改实例显示名。
  - POST /api/instances/{id}/reinstall：body 必须含 confirm=true 且
    confirm_name 等于实例当前显示名（防误点）；重装清空系统盘，
    实例 OCID 与 IP 保留不变。

profile 通过查询参数传（如 ?profile=ACCOUNT2），默认 DEFAULT。
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

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
    restart_instance,
    start_instance,
    stop_instance,
    terminate_instance,
)
from oci.network import get_public_ip
from web import config
from web.deps import (
    error_response,
    get_current_user,
    oci_error_response,
    resolve_profile,
)

router = APIRouter()


class TerminateBody(BaseModel):
    """终止请求体：必须显式确认。"""

    confirm: bool = False


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


def _safe_public_ip(cli, instance_id):
    """取实例公网 IP，失败时返回 None（不让单台实例的 IP 查询拖垮列表）。"""
    try:
        return get_public_ip(cli, instance_id)
    except OCIError:
        return None


def format_instance(inst, public_ip, region):
    """把 list_instances 的条目转成 R-Bot 面板形状（阶段 5 工作包 E）。

    新形状（camelCase，与 R-Bot /cloud/oci/instances 一致）：
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
             state: str = Query(default="running"),
             username: str = Depends(get_current_user)):
    """列出该账号下的实例（R-Bot 形状，每项补公网 IP）。

    state=running（默认）：只含 RUNNING 状态实例（与 R-Bot
    /cloud/oci/instances 一致）。
    state=all：含全部非 TERMINATED 实例（RUNNING/STOPPED/STOPPING 等），
    用于从面板启动已停实例；TERMINATED 的实例始终不含。
    """
    show_all = (state or "").lower() == "all"
    try:
        cli, compartment_id = resolve_profile(profile)
        raw = list_instances(cli, compartment_id,
                             include_shape_config=True)
        instances = []
        for inst in raw:
            inst_state = inst.get("lifecycle-state") or ""
            if show_all:
                if inst_state == "TERMINATED":
                    continue
            elif inst_state != "RUNNING":
                continue
            public_ip = _safe_public_ip(cli, inst.get("id"))
            instances.append(format_instance(inst, public_ip, cli.region))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True,
            "profile": profile or config.DEFAULT_PROFILE,
            "instances": instances}


@router.post("/api/instances/batch")
def batch(body: BatchBody,
          profile: str = Query(default=None),
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
        cli, _compartment_id = resolve_profile(profile)
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
    return {"ok": True, "action": body.action,
            "succeeded": result["succeeded"], "failed": result["failed"]}


@router.get("/api/instances/{instance_id}")
def detail(instance_id: str,
           profile: str = Query(default=None),
           username: str = Depends(get_current_user)):
    """查单个实例详情（含公网 IP）。"""
    try:
        cli, _compartment_id = resolve_profile(profile)
        data = get_instance(cli, instance_id)
        detail_data = data.get("data") if isinstance(data, dict) and "data" in data else data
        public_ip = _safe_public_ip(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance": detail_data, "public_ip": public_ip}


def _action(instance_id, profile, func, action_name):
    """启动/停止/重启的公共逻辑。"""
    try:
        cli, _compartment_id = resolve_profile(profile)
        func(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance_id": instance_id, "action": action_name}


@router.post("/api/instances/{instance_id}/start")
def start(instance_id: str,
          profile: str = Query(default=None),
          username: str = Depends(get_current_user)):
    """启动实例。"""
    return _action(instance_id, profile, start_instance, "start")


@router.post("/api/instances/{instance_id}/stop")
def stop(instance_id: str,
         profile: str = Query(default=None),
         username: str = Depends(get_current_user)):
    """停止实例。"""
    return _action(instance_id, profile, stop_instance, "stop")


@router.post("/api/instances/{instance_id}/restart")
def restart(instance_id: str,
            profile: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """重启实例。"""
    return _action(instance_id, profile, restart_instance, "restart")


@router.get("/api/instances/{instance_id}/console")
def console(instance_id: str,
            profile: str = Query(default=None),
            username: str = Depends(get_current_user)):
    """获取串行控制台连接信息（含 ssh 连接命令与指纹）。"""
    try:
        cli, _compartment_id = resolve_profile(profile)
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
           username: str = Depends(get_current_user)):
    """更改实例显示名（新名字不能为空，见 rename_instance 的校验）。"""
    if not (body.name or "").strip():
        return error_response("新实例名不能为空", status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile)
        detail = rename_instance(cli, instance_id, body.name)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance_id": instance_id, "instance": detail}


@router.post("/api/instances/{instance_id}/reinstall")
def reinstall(instance_id: str,
              body: ReinstallBody,
              profile: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """重装系统：必须 confirm=true 且 confirm_name 等于实例当前显示名。

    重装会清空系统盘数据，实例 OCID 与 IP 保留不变；
    image_id 不传时用当前镜像重装。
    """
    if not body.confirm:
        return error_response("重装系统需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile)
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
    return {"ok": True, "instance_id": instance_id, "instance": result}


@router.post("/api/instances/{instance_id}/terminate")
def terminate(instance_id: str,
              body: TerminateBody,
              profile: str = Query(default=None),
              username: str = Depends(get_current_user)):
    """终止实例（必须 confirm=true；默认永久删除引导卷，不留着扣钱）。"""
    if not body.confirm:
        return error_response("终止实例需要确认：请求体须含 confirm=true",
                              status_code=400)
    try:
        cli, _compartment_id = resolve_profile(profile)
        # 默认 preserve_boot_volume=False：终止时永久删除引导卷。
        terminate_instance(cli, instance_id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except OCIError as exc:
        return oci_error_response(exc)
    return {"ok": True, "instance_id": instance_id, "action": "terminate"}
