#!/usr/bin/env python3
"""批量命令执行（阶段 5 工作包 E）。

  - POST /api/batch/exec：选多台已保存主机 + 一条命令，后台逐台执行，
    返回 batchId（body 须含 confirm=true 明确确认）。
  - GET /api/batch/{id}：查批量任务结果（每台的 stdout/stderr/exit_code）。
  - POST /api/batch/{id}/cancel：取消未执行的主机（正在执行的那台
    会跑完，之后的不再执行）。

安全：
  - 命令走与 Telegram /command 相同的危险命令黑名单，复用
    tgbot.handlers_client.find_dangerous_pattern，不另写一份；
    命中黑名单直接 400 拒绝。
  - 执行复用 tgbot.handlers_client.run_remote_command（Paramiko，
    密钥认证；密码认证的主机因密码不存文件会执行失败，结果里记 error）。
  - 任务在 daemon 线程里跑，取消是协作式的（执行间隙检查 stop_event）。
"""

import threading
import time
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web.deps import error_response, get_current_user
from web.routes.hosts import get_host

try:
    # Dangerous-command denylist + remote execution (shares the same implementation as Telegram /command). 危险命令黑名单 + 远程执行（与 Telegram /command 共用同一份实现）。
    from tgbot.handlers_client import (
        COMMAND_TIMEOUT_SECONDS,
        RemoteCommandError,
        find_dangerous_pattern,
        run_remote_command,
    )
except ImportError:  # pragma: no cover - tgbot 依赖缺失时的降级分支
    COMMAND_TIMEOUT_SECONDS = 30
    RemoteCommandError = Exception
    find_dangerous_pattern = None
    run_remote_command = None

router = APIRouter()

# ================= 配置区（改这里） =================
# Max hosts per batch run (so one selection of hundreds of hosts cannot drag the panel down). 单次批量最多主机数（防一次选几百台拖垮面板）。
BATCH_MAX_HOSTS = 20
# Max chars kept per host for stdout/stderr; beyond that, truncate and note it. 每台主机 stdout/stderr 各保留的最大字符数，超了截断并加提示。
BATCH_MAX_OUTPUT_CHARS = 20000
# =====================================================


class BatchExecBody(BaseModel):
    """批量执行请求体：host_ids + command，必须 confirm=true。"""

    host_ids: list = []
    command: str = ""
    confirm: bool = False


def _truncate(text, limit=BATCH_MAX_OUTPUT_CHARS):
    """输出截断：超长时截断并加提示。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（输出过长，已截断）"


class BatchRegistry:
    """批量任务注册表：内存字典 + 线程锁（重启后任务记录丢失，
    与 Web SSH 会话一样不持久化；结果查询只在本次运行内有效）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._batches = {}

    def create(self, host_ids, command):
        """新建批量任务，返回 batch 字典（含 stop_event，内部用）。"""
        batch_id = uuid.uuid4().hex[:12]
        batch = {
            "id": batch_id,
            "command": command,
            "host_ids": list(host_ids),
            "status": "running",
            "cancel_requested": False,
            "results": {},
            "created_at": time.time(),
            "stop_event": threading.Event(),
        }
        with self._lock:
            self._batches[batch_id] = batch
        return batch

    def get(self, batch_id):
        """按 id 取批量任务；不存在返回 None。"""
        with self._lock:
            return self._batches.get(batch_id)

    def cancel(self, batch_id):
        """请求取消：置 stop_event，未执行的主机将被跳过。

        返回 batch 字典；不存在返回 None。已完成/已取消的任务重复
        取消返回当前视图（幂等）。
        """
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            if batch["status"] == "running":
                batch["cancel_requested"] = True
                batch["stop_event"].set()
            return batch

    def reset(self):
        """清空全部批量任务（测试用）。"""
        with self._lock:
            self._batches.clear()


# Global singleton (shared within a single process). 全局单例（单进程内共享）。
BATCHES = BatchRegistry()


def public_view(batch):
    """批量任务的对外视图（去掉不可序列化的 stop_event）。"""
    return {
        "id": batch["id"],
        "command": batch["command"],
        "host_ids": batch["host_ids"],
        "status": batch["status"],
        "cancel_requested": batch["cancel_requested"],
        "results": batch["results"],
        "created_at": batch["created_at"],
    }


def _run_batch(batch):
    """批量任务的后台执行循环（daemon 线程里跑）。

    逐台执行：每台开始前检查 stop_event，已取消则把剩余主机
    标记为 cancelled 后退出。单台执行失败不影响其他主机。
    """
    for host_id in batch["host_ids"]:
        if batch["stop_event"].is_set():
            with BATCHES._lock:
                for pending_id in batch["host_ids"]:
                    result = batch["results"].get(pending_id)
                    if result is None:
                        batch["results"][pending_id] = {
                            "hostId": pending_id,
                            "hostName": None,
                            "status": "cancelled",
                            "exitCode": None,
                            "stdout": "",
                            "stderr": "",
                            "error": "批量任务已取消",
                        }
            break
        host = get_host(host_id)
        if host is None:
            with BATCHES._lock:
                batch["results"][host_id] = {
                    "hostId": host_id,
                    "hostName": None,
                    "status": "failed",
                    "exitCode": None,
                    "stdout": "",
                    "stderr": "",
                    "error": "主机不存在（可能已被删除）",
                }
            continue
        with BATCHES._lock:
            batch["results"][host_id] = {
                "hostId": host_id,
                "hostName": host.get("name"),
                "status": "running",
                "exitCode": None,
                "stdout": "",
                "stderr": "",
                "error": None,
            }
        try:
            exit_code, stdout, stderr = run_remote_command(
                host, batch["command"], timeout=COMMAND_TIMEOUT_SECONDS)
            result = {
                "hostId": host_id,
                "hostName": host.get("name"),
                "status": "done",
                "exitCode": exit_code,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
                "error": None,
            }
        except RemoteCommandError as exc:
            result = {
                "hostId": host_id,
                "hostName": host.get("name"),
                "status": "failed",
                "exitCode": None,
                "stdout": "",
                "stderr": "",
                "error": str(exc),
            }
        with BATCHES._lock:
            batch["results"][host_id] = result
    with BATCHES._lock:
        batch["status"] = ("cancelled" if batch["stop_event"].is_set()
                           else "done")


@router.post("/api/batch/exec", status_code=202)
def batch_exec(body: BatchExecBody,
               username: str = Depends(get_current_user)):
    """批量执行命令：后台逐台执行，返回 batchId 供查询结果。"""
    if find_dangerous_pattern is None or run_remote_command is None:
        return error_response("批量命令不可用：tgbot 模块未就绪",
                              status_code=503)
    if not body.confirm:
        return error_response("批量命令需要确认：请求体须含 confirm=true",
                              status_code=400)
    if not body.host_ids:
        return error_response("请至少选一台主机", status_code=400)
    if len(body.host_ids) > BATCH_MAX_HOSTS:
        return error_response(
            f"一次最多选 {BATCH_MAX_HOSTS} 台主机，收到 {len(body.host_ids)} 台",
            status_code=400)
    command = (body.command or "").strip()
    if not command:
        return error_response("命令不能为空", status_code=400)
    blocked = find_dangerous_pattern(command)
    if blocked:
        return error_response(f"危险命令，已拒绝执行：{blocked}",
                              status_code=400)
    missing = [hid for hid in body.host_ids if get_host(hid) is None]
    if missing:
        return error_response(
            f"主机不存在：{', '.join(missing)}", status_code=404)
    batch = BATCHES.create(body.host_ids, command)
    thread = threading.Thread(target=_run_batch, args=(batch,),
                              daemon=True)
    thread.start()
    return {"ok": True, "batchId": batch["id"], "status": "running",
            "hosts": len(body.host_ids)}


@router.get("/api/batch/{batch_id}")
def batch_get(batch_id: str, username: str = Depends(get_current_user)):
    """查批量任务结果（每台的 stdout/stderr/exit_code）。"""
    batch = BATCHES.get(batch_id)
    if batch is None:
        return error_response(f"批量任务不存在：{batch_id}", status_code=404)
    return {"ok": True, "batch": public_view(batch)}


@router.post("/api/batch/{batch_id}/cancel")
def batch_cancel(batch_id: str, username: str = Depends(get_current_user)):
    """取消批量任务：未执行的主机不再执行，正在执行的那台会跑完。"""
    batch = BATCHES.cancel(batch_id)
    if batch is None:
        return error_response(f"批量任务不存在：{batch_id}", status_code=404)
    return {"ok": True, "batch": public_view(batch)}
