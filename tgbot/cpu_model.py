#!/usr/bin/env python3
"""实例 CPU 型号检测：经 SSH 读 /proc/cpuinfo（ARM 兜底 lscpu），结果持久缓存。

OCI API 不返回宿主机 CPU 型号，唯一可靠来源是实例自身的
/proc/cpuinfo（编排器查 7J13 用的也是这条路）。CPU 型号对一台实例
终身不变，查到一次就缓存，不再重复 SSH。

  - parse_cpu_model：把原始 model name 行解析成短型号，如
    "AMD EPYC 7J13 64-Core Processor" -> "AMD EPYC 7J13"、
    "Ampere(R) Altra(R)" -> "Ampere Altra"。
  - get_cpu_model：先读缓存，未命中再 SSH 检测并写入缓存；SSH
    失败返回 None（调用方显示“未知”），不抛异常。
  - 缓存文件 tgbot/data/cpu_models.json：{实例 OCID: {model,
    checked_at}}；实例终止后残留条目由 prune_cpu_models 清理。

SSH 走流量守护的专用密钥（MBOT_GUARD_SSH_KEY），host 记录由
host_for_ip 构造；run_command 由调用方注入（生产用
tgbot.handlers_client.run_remote_command，测试用假函数）。
"""

import json
import os
import threading
from datetime import datetime, timezone

from tgbot import config as bot_config

# ================= 配置区（改这里） =================
# CPU model cache file (same directory as bark.json / region_plans.json). CPU 型号缓存文件（与 bark.json / region_plans.json 同目录）。
CPU_MODELS_FILE = os.path.join(bot_config.DATA_DIR, "cpu_models.json")
# Detection command: x86 /proc/cpuinfo has a model name; ARM (A1) cpuinfo lacks that field, so fall back to lscpu (ships with util-linux, present by default on Debian). 检测命令：x86 的 /proc/cpuinfo 有 model name；ARM（A1）的 cpuinfo 没有该字段，兜底 lscpu（util-linux 自带，Debian 默认有）。
CPU_MODEL_COMMAND = (
    "grep -m1 'model name' /proc/cpuinfo 2>/dev/null || "
    "lscpu 2>/dev/null | grep -m1 -i 'model name:'"
)
# Timeout (seconds) for one SSH detection: used by the synchronous detail-page check, so keep it short. 单次 SSH 检测超时（秒）：详情页同步检测用，不宜太长。
CPU_FETCH_TIMEOUT = 20
# Truncation length (characters) for unknown models. 未知型号截断长度（字符）。
UNKNOWN_MODEL_MAX_LEN = 40
# =====================================================


# Cache file read/write lock: the web panel's background detection is multi-threaded, so load-modify-write must be serialized, or two instances detected at once would overwrite each other's cache entries. 缓存文件读写锁：网页端后台检测是多线程，load-modify-write 必须串行，否则两个实例同时检测会互相覆盖对方的缓存条目。
_CACHE_LOCK = threading.Lock()


def parse_cpu_model(text):
    """把 model name 原始行解析成短型号；解析不出返回 None。

    输入是命令输出（如 "model name\\t: AMD EPYC 7J13 64-Core Processor"
    或 "Model name: Ampere(R) Altra(R)"），只取第一行冒号后的值。
    """
    if not text:
        return None
    value = None
    for line in str(text).splitlines():
        if ":" in line:
            value = line.split(":", 1)[1].strip()
            if value:
                break
    if not value:
        return None
    # Strip "(R)" and collapse whitespace, e.g. "Ampere(R) Altra(R)" -> "Ampere Altra". 去 "(R)"、压空白，如 "Ampere(R) Altra(R)" -> "Ampere Altra"。
    value = " ".join(value.replace("(R)", "").replace("(r)", "").split())
    if not value:
        return None
    tokens = value.split()
    if tokens[:2] == ["AMD", "EPYC"] and len(tokens) >= 3:
        return " ".join(tokens[:3])
    if tokens[:1] == ["Ampere"] and len(tokens) >= 2:
        return " ".join(tokens[:2])
    if tokens[:1] == ["Intel"] and len(tokens) >= 4:
        return " ".join(tokens[:4])
    if value == "Neoverse-N1":
        # A1.Flex only runs Ampere Altra on OCI (Neoverse-N1 cores); lscpu only reports the core codename, so show the chip name directly. A1.Flex 在 OCI 上只跑 Ampere Altra（Neoverse-N1 核心），lscpu 只报核心代号，这里直接显示芯片名。
        return "Ampere Altra"
    return value[:UNKNOWN_MODEL_MAX_LEN]


def load_cpu_models():
    """读缓存文件，返回 {实例 OCID: {"model": str, "checked_at": str}}。

    文件不存在或损坏时返回空 dict（不抛异常，调用方按未检测处理）。
    """
    with _CACHE_LOCK:
        try:
            with open(CPU_MODELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def cached_cpu_model(instance_id):
    """取一台实例缓存的 CPU 型号；无缓存返回 None。"""
    if not instance_id:
        return None
    entry = load_cpu_models().get(instance_id) or {}
    return entry.get("model") or None


def save_cpu_model(instance_id, model):
    """把一台实例的 CPU 型号写入缓存（文件 0600，与其他数据文件一致）。"""
    if not instance_id or not model:
        return
    with _CACHE_LOCK:
        try:
            with open(CPU_MODELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[instance_id] = {
            "model": model,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        parent = os.path.dirname(CPU_MODELS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(CPU_MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CPU_MODELS_FILE, 0o600)
    except OSError:
        pass


def delete_cpu_model(instance_id):
    """删掉一台实例的缓存条目（实例终止后调用，避免残留）。"""
    if not instance_id:
        return
    with _CACHE_LOCK:
        try:
            with open(CPU_MODELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or instance_id not in data:
            return
        del data[instance_id]
        with open(CPU_MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CPU_MODELS_FILE, 0o600)
    except OSError:
        pass


def prune_cpu_models(keep_ids):
    """删掉不在 keep_ids 里的缓存条目（实例已终止后的清理）。

    只有实际删掉条目时才写文件；返回删掉的条数。
    """
    keep = set(keep_ids or [])
    with _CACHE_LOCK:
        try:
            with open(CPU_MODELS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        stale = [key for key in data if key not in keep]
        if not stale:
            return 0
        for key in stale:
            del data[key]
        with open(CPU_MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CPU_MODELS_FILE, 0o600)
    except OSError:
        pass
    return len(stale)


def host_for_ip(public_ip):
    """CPU 检测的 SSH 主机记录（实例公网 IP + 守护专用密钥）。

    未配置 MBOT_GUARD_SSH_KEY 或无公网 IP 时返回 None。与
    handlers_extra._guard_host_for_instance 同款，放这里避免
    handlers.py / web 路由反向 import handlers_extra。
    """
    if not public_ip or not bot_config.GUARD_SSH_KEY:
        return None
    return {
        "host": public_ip,
        "port": bot_config.GUARD_SSH_PORT,
        "username": bot_config.GUARD_SSH_USER,
        "key_path": bot_config.GUARD_SSH_KEY,
    }


def fetch_cpu_model(host, run_command, timeout=CPU_FETCH_TIMEOUT):
    """SSH 上去取一台实例的 CPU 型号；失败返回 None（不抛异常）。"""
    if not host:
        return None
    try:
        code, out, _err = run_command(host, CPU_MODEL_COMMAND,
                                      timeout=timeout)
    except Exception:  # noqa: BLE001 - 连接/超时都按检测失败处理
        return None
    if code != 0:
        return None
    return parse_cpu_model(out)


def get_cpu_model(instance_id, host, run_command, force=False,
                  timeout=CPU_FETCH_TIMEOUT):
    """取一台实例的 CPU 型号：先读缓存，未命中（或 force）再 SSH 检测。

    检测成功写入缓存；失败返回 None（缓存里原来有就返回旧值——
    force 重测失败时不把旧值清掉）。
    """
    if not force:
        cached = cached_cpu_model(instance_id)
        if cached:
            return cached
    model = fetch_cpu_model(host, run_command, timeout=timeout)
    if model:
        save_cpu_model(instance_id, model)
        return model
    return cached_cpu_model(instance_id) if force else None
