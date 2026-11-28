#!/usr/bin/env python3
"""bot 与网页版共用的任务存储（tasks.json）。

机器人进程与网页进程各跑一份，之前任务注册表互不可见：机器人
建的任务网页看不见，网页建的任务机器人看不见（2026-11-24 用户
实问）。本模块把任务记录存在同一份 tasks.json（web/data/ 下，
网页版原有的文件），两边读写同一份记录。

并发：两进程同时写同一文件，用 flock 锁（tasks.json.lock）
保护读-改-写；写盘用临时文件 + os.replace 原子替换。
记录字段：id / kind / status / detail / label / logs / result /
created_at / origin（"bot"/"web"）/ cancel_requested / grab_spec。
grab_spec 是抢机任务的完整规格（抢机启动时写入，服务重启后
按它恢复抢机；含 root 密码，所以任务文件每次写盘都 chmod 0600，
与 bark.json 同一先例；API 返回前用 public_record() 剥掉它）。
任务 ID 带来源前缀（bot-g1 / web-g1），两边计数器互不冲突；
旧记录没有前缀的按 web 来源处理（文件本来就是网页版的）。
取消是跨进程的：一方把 cancel_requested 置 True，另一方的
取消监听线程（start_cancel_watcher）轮询到后置本地 stop_event。
"""

import fcntl
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

from web import config as web_config

# ================= 配置区（改这里） =================
# Shared task file (MBOT_TASKS_FILE can override it; default web DATA_DIR/tasks.json). 共享任务文件（MBOT_TASKS_FILE 可覆盖，默认 web DATA_DIR/tasks.json）。
TASKS_FILENAME = "tasks.json"
# Cancel-watch poll interval (seconds). 取消监听轮询间隔（秒）。
WATCH_INTERVAL_SECONDS = 5
# Terminal states: the watch thread exits on its own when it sees one of these. 终态：监听线程看到这些状态自行退出。
TERMINAL_STATUSES = ("done", "failed", "cancelled")
# Grabber parameter allowlist to store in grab_spec (only these; other fields never enter the file). grab_spec 里要存的 Grabber 参数白名单（只留这几个，别的字段不进文件）。
GRAB_PARAM_KEYS = (
    "availability_domain", "shape", "image_id", "subnet_id",
    "display_name", "ssh_keys_file", "target_cpu", "ocpus",
    "memory_gb", "boot_volume_size_gbs", "assign_public_ip",
    "assign_ipv6", "user_data", "dd", "init_script", "min_delay",
    "max_delay", "count",
)
# dd / init_script must persist too: on restart resume the relaunched grab reads params from the spec, and _notify_grab_success needs them to start DD init. dd 与 init_script 也要存：重启恢复时重跑的抢机从规格里读 params，_notify_grab_success 靠它们起 DD 初始化；不存则重启后 DD 丢了。
# Notification-field allowlist to store in grab_spec (tokens never hit disk; they are read from env at recovery time). grab_spec 里要存的通知字段白名单（token 绝不落盘，恢复时从 env 现读）。
GRAB_NOTIFY_KEYS = (
    "chat_id", "profile_label", "region_label", "region_code",
    "root_password",
)
# =====================================================


def tasks_path():
    """共享任务文件的完整路径。"""
    override = os.environ.get("MBOT_TASKS_FILE")
    if override:
        return os.path.expanduser(override)
    return os.path.join(web_config.DATA_DIR, TASKS_FILENAME)


@contextmanager
def _locked(path):
    """对任务文件加排他锁（阻塞；临界区只做一次读/写，很短）。"""
    lock_path = path + ".lock"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _read_records_unlocked(path):
    """读文件里的记录列表（调用方需已持锁；文件不存在/损坏返回 []）。"""
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("id")]


def _write_records_unlocked(path, records):
    """原子写盘（临时文件 + os.replace；调用方需已持锁）。

    写完 chmod 0600：抢机规格里有 root 密码，文件只能本用户读，
    与 bark.json / cpu_policy.json 同一先例。
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _stamp(message):
    """给日志消息加时间前缀。"""
    return f"[{datetime.now().strftime('%H:%M:%S')}] {message}"


def _normalize(record):
    """补齐缺失字段；旧记录（无 origin）按 web 来源处理。"""
    record.setdefault("kind", "?")
    record.setdefault("status", "cancelled")
    record.setdefault("detail", "")
    record.setdefault("label", None)
    record.setdefault("logs", [])
    record.setdefault("result", None)
    record.setdefault("created_at", time.time())
    record.setdefault("origin", "web")
    record.setdefault("cancel_requested", False)
    return record


def load_records(path=None):
    """读全部任务记录（公开视图，不含 stop_event）。

    读不加锁：写盘是原子替换（临时文件 + os.replace），读永远
    看不到写一半的文件；不加锁也避免只读轮询（取消监听）创建
    锁文件，干扰测试的临时目录清理。
    """
    path = path or tasks_path()
    return [_normalize(dict(r)) for r in _read_records_unlocked(path)]


def get_record(path, task_id):
    """取一条任务记录；不存在返回 None（读不加锁，见 load_records）。"""
    for record in _read_records_unlocked(path):
        if record.get("id") == task_id:
            return _normalize(dict(record))
    return None


def create_record(path, origin, kind, detail="", label=None,
                  status="pending"):
    """新建一条任务记录，返回记录；ID 在锁内分配，唯一。

    ID 格式 <origin>-<kind 首字母><序号>（如 bot-g1、web-b1）。
    web 的计数器也计入旧的无前缀记录（如 g1），避免新 ID 冲突。
    """
    with _locked(path):
        records = _read_records_unlocked(path)
        prefix = f"{origin}-{kind[0]}"
        max_n = 0
        for record in records:
            rid = record.get("id", "")
            if rid.startswith(prefix):
                digits = rid[len(prefix):]
                if digits.isdigit():
                    max_n = max(max_n, int(digits))
        if origin == "web":
            for record in records:
                rid = record.get("id", "")
                rest = rid[len(kind[0]):] if rid.startswith(kind[0]) else ""
                if rest.isdigit():
                    max_n = max(max_n, int(rest))
        task_id = f"{prefix}{max_n + 1}"
        record = _normalize({
            "id": task_id, "kind": kind, "status": status,
            "detail": detail, "label": label, "logs": [],
            "result": None, "created_at": time.time(),
            "origin": origin, "cancel_requested": False,
        })
        records.append(record)
        _write_records_unlocked(path, records)
        return dict(record)


def update_record(path, task_id, status=None, detail=None, result=None,
                  log=None, fields=None):
    """更新一条记录；返回更新后的记录，不存在返回 None。

    detail 只设详情字段；log 追加一条带时间前缀的日志行。
    两边同时传时详情与日志各写一次（调用方传同一条消息即可）。
    fields 是要直接设置的任意字段字典（如 {"grab_spec": spec}），
    与 status/detail/result/log 同时传时一起生效。
    """
    with _locked(path):
        records = _read_records_unlocked(path)
        for record in records:
            if record.get("id") != task_id:
                continue
            if status is not None:
                record["status"] = status
            if detail is not None:
                record["detail"] = detail
            if result is not None:
                record["result"] = result
            if log is not None:
                record.setdefault("logs", []).append(_stamp(log))
            if fields:
                record.update(fields)
            _write_records_unlocked(path, records)
            return _normalize(dict(record))
    return None


def request_cancel(path, task_id):
    """请求取消（跨进程）：置 cancel_requested，返回任务是否在跑。

    任务不存在或已结束返回 False；对方进程的监听线程轮询到后
    置本地 stop_event，任务线程自己把状态标为 cancelled。
    """
    with _locked(path):
        records = _read_records_unlocked(path)
        for record in records:
            if record.get("id") != task_id:
                continue
            if record.get("status") not in ("pending", "running"):
                return False
            record["cancel_requested"] = True
            _write_records_unlocked(path, records)
            return True
    return False


def recover_interrupted(path, origin, keep_ids=None):
    """进程启动时调用：把本来源还在跑的任务标为已中断。

    只动本来源的记录；另一进程的任务还活着，绝不碰。
    keep_ids 里的任务跳过（调用方要恢复重跑的抢机任务）。
    """
    keep = set(keep_ids or ())
    with _locked(path):
        records = _read_records_unlocked(path)
        changed = False
        for record in records:
            if record.get("origin", "web") != origin:
                continue
            if record.get("id") in keep:
                continue
            if record.get("status") in ("pending", "running"):
                record["status"] = "cancelled"
                record.setdefault("logs", []).append(
                    _stamp("服务重启，任务已中断。"))
                changed = True
        if changed:
            _write_records_unlocked(path, records)


def clear_records(path):
    """清空全部任务记录（测试用）。"""
    with _locked(path):
        _write_records_unlocked(path, [])


def delete_record(path, task_id):
    """删除一条任务记录；不存在返回 False。"""
    with _locked(path):
        records = _read_records_unlocked(path)
        kept = [r for r in records if r.get("id") != task_id]
        if len(kept) == len(records):
            return False
        _write_records_unlocked(path, kept)
        return True


def build_grab_spec(profile, region, compartment_id, params, notify=None):
    """拼一条抢机规格（写进任务记录的 grab_spec，重启恢复用）。

    params 只按 GRAB_PARAM_KEYS 白名单取（root_password 不在这里，
    在 notify 里）；notify 只按 GRAB_NOTIFY_KEYS 白名单取，token
    绝不落盘（恢复时从环境变量现读）。successes 是已抢到的台数，
    抢机线程每抢到一台就更新一次，恢复时剩余台数 = count - 已抢到。
    """
    spec_params = {}
    for key in GRAB_PARAM_KEYS:
        value = params.get(key) if params else None
        if value is not None:
            spec_params[key] = value
    spec_params["count"] = (params.get("count", 1) or 1) if params else 1
    spec_notify = {}
    for key in GRAB_NOTIFY_KEYS:
        value = notify.get(key) if notify else None
        if value is not None:
            spec_notify[key] = value
    return {
        "profile": profile,
        "region": region,
        "compartment_id": compartment_id,
        "params": spec_params,
        "notify": spec_notify,
        "successes": [],
    }


def grab_success_to_dict(result):
    """GrabResult -> 可 JSON 化的字典（恢复时按它重建结果对象）。"""
    return {
        "instance_id": result.instance_id,
        "attempts": result.attempts,
        "releases": result.releases,
        "elapsed_seconds": result.elapsed_seconds,
        "cpu_model": getattr(result, "cpu_model", None),
    }


def grab_success_from_dict(data):
    """字典 -> GrabResult（恢复时用；缺字段时用 0 兜底）。"""
    from oci.grabber import GrabResult

    return GrabResult(
        instance_id=data.get("instance_id", ""),
        attempts=data.get("attempts", 0) or 0,
        releases=data.get("releases", 0) or 0,
        elapsed_seconds=data.get("elapsed_seconds", 0.0) or 0.0,
        cpu_model=data.get("cpu_model"),
    )


def public_record(record):
    """任务记录的公开视图：去掉 grab_spec（里有 root 密码），供 API 返回。

    返回浅拷贝，原记录不动；没有 grab_spec 的记录原样返回拷贝。
    """
    if not record or "grab_spec" not in record:
        return dict(record) if record else record
    view = dict(record)
    del view["grab_spec"]
    return view


def start_cancel_watcher(path, task_id, stop_event,
                         interval=WATCH_INTERVAL_SECONDS):
    """启动取消监听线程（daemon）：轮询到 cancel_requested 就置 stop_event。

    任务结束（记录消失或到终态）时线程自行退出。path 为 None
    （纯内存模式）或 stop_event 为 None 时不启动，返回 None。
    """
    if not path or stop_event is None:
        return None

    def _watch():
        while not stop_event.is_set():
            record = get_record(path, task_id)
            if record is None or record.get("status") in TERMINAL_STATUSES:
                return
            if record.get("cancel_requested"):
                stop_event.set()
                return
            time.sleep(interval)

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    return thread
