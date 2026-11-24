#!/usr/bin/env python3
"""bot 与网页版共用的任务存储（tasks.json）。

机器人进程与网页进程各跑一份，之前任务注册表互不可见：机器人
建的任务网页看不见，网页建的任务机器人看不见（2026-11-24 用户
实问）。本模块把任务记录存在同一份 tasks.json（web/data/ 下，
网页版原有的文件），两边读写同一份记录。

并发：两进程同时写同一文件，用 flock 锁（tasks.json.lock）
保护读-改-写；写盘用临时文件 + os.replace 原子替换。
记录字段：id / kind / status / detail / label / logs / result /
created_at / origin（"bot"/"web"）/ cancel_requested。
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
# 共享任务文件（MBOT_TASKS_FILE 可覆盖，默认 web DATA_DIR/tasks.json）。
TASKS_FILENAME = "tasks.json"
# 取消监听轮询间隔（秒）。
WATCH_INTERVAL_SECONDS = 5
# 终态：监听线程看到这些状态自行退出。
TERMINAL_STATUSES = ("done", "failed", "cancelled")
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
    """原子写盘（临时文件 + os.replace；调用方需已持锁）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    os.replace(tmp, path)


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
                  log=None):
    """更新一条记录；返回更新后的记录，不存在返回 None。

    detail 只设详情字段；log 追加一条带时间前缀的日志行。
    两边同时传时详情与日志各写一次（调用方传同一条消息即可）。
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


def recover_interrupted(path, origin):
    """进程启动时调用：把本来源还在跑的任务标为已中断。

    只动本来源的记录；另一进程的任务还活着，绝不碰。
    """
    with _locked(path):
        records = _read_records_unlocked(path)
        changed = False
        for record in records:
            if record.get("origin", "web") != origin:
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
