#!/usr/bin/env python3
"""实例系统版本检测：经 SSH 读 /etc/os-release（Debian 再读
/etc/debian_version 取小版本），结果持久缓存。

OCI API 不返回实例里跑的操作系统版本，唯一可靠来源是实例自身
的 /etc/os-release（Debian 的小版本只在 /etc/debian_version 里，
如 13.7；os-release 只有大版本 13）。系统版本对一台实例终身
不变（DD 后会变，见下），查到一次就缓存，不再重复 SSH。

  - parse_os_version：把命令输出解析成短版本，如
    "Debian 13.7"、"Ubuntu 22.04"、"Oracle Linux 9.4"。
  - get_os_version：先读缓存，未命中再 SSH 检测并写入缓存；SSH
    失败返回 None（调用方显示“未知”），不抛异常。
  - 缓存文件 tgbot/data/os_versions.json：{实例 OCID: {version,
    checked_at}}；实例终止后残留条目由 prune_os_versions 清理。

DD 后系统会变：DD 重装会换操作系统，缓存里的旧版本就错了。
调用方在 DD 完成 / 重装后应调 delete_os_version 清掉缓存，下次
打开详情时重新检测。

SSH 走流量守护的专用密钥（MBOT_GUARD_SSH_KEY），host 记录由
host_for_ip 构造；run_command 由调用方注入（生产用
tgbot.handlers_client.run_remote_command，测试用假函数）。
"""

import json
import os
import re
import threading
from datetime import datetime, timezone

from tgbot import config as bot_config

# ================= 配置区（改这里） =================
# OS version cache file (same directory as bark.json / cpu_models.json). 系统版本缓存文件（与 bark.json / cpu_models.json 同目录）。
OS_VERSIONS_FILE = os.path.join(bot_config.DATA_DIR, "os_versions.json")
# Detection command: /etc/os-release has NAME + VERSION_ID (Debian only
# has the major version, e.g. 13); /etc/debian_version has the point
# release (e.g. 13.7). The marker line separates the two files.
# 检测命令：/etc/os-release 有 NAME + VERSION_ID（Debian 只有大版本，如
# 13）；/etc/debian_version 有小版本（如 13.7）。标记行分隔两个文件。
OS_VERSION_COMMAND = (
    "cat /etc/os-release 2>/dev/null; "
    "echo '---DEBIAN_VERSION---'; "
    "cat /etc/debian_version 2>/dev/null"
)
# Timeout (seconds) for one SSH detection: used by the synchronous detail-page check, so keep it short. 单次 SSH 检测超时（秒）：详情页同步检测用，不宜太长。
OS_FETCH_TIMEOUT = 20
# Truncation length (characters) for unknown versions. 未知版本截断长度（字符）。
UNKNOWN_VERSION_MAX_LEN = 40
# =====================================================


# Cache file read/write lock: the web panel's background detection is multi-threaded, so load-modify-write must be serialized. 缓存文件读写锁：网页端后台检测是多线程，load-modify-write 必须串行。
_CACHE_LOCK = threading.Lock()


def _short_distro_name(name, distro_id):
    """把 os-release 的 NAME 简化成短发行版名。

    "Debian GNU/Linux" -> "Debian"；"Ubuntu" -> "Ubuntu"；
    "Oracle Linux Server" -> "Oracle Linux"；其他取第一个词。
    """
    if not name:
        return None
    name = name.strip().strip('"').strip("'")
    if not name:
        return None
    lowered = name.lower()
    if "debian" in lowered:
        return "Debian"
    if "ubuntu" in lowered:
        return "Ubuntu"
    if "oracle" in lowered:
        return "Oracle Linux"
    if "rocky" in lowered:
        return "Rocky Linux"
    if "almalinux" in lowered:
        return "AlmaLinux"
    if "centos" in lowered:
        return "CentOS"
    if "fedora" in lowered:
        return "Fedora"
    if "alpine" in lowered:
        return "Alpine"
    if "arch" in lowered:
        return "Arch Linux"
    if "opensuse" in lowered:
        return "openSUSE"
    # Fallback: first word of NAME (e.g. "Kali" from "Kali GNU/Linux").
    # 兜底：NAME 的第一个词（如 "Kali GNU/Linux" -> "Kali"）。
    return name.split()[0]


def parse_os_version(text):
    """把检测命令输出解析成短系统版本；解析不出返回 None。

    输入是 OS_VERSION_COMMAND 的输出（/etc/os-release + 标记行 +
    /etc/debian_version）。Debian 用 debian_version 的小版本
    （如 13.7），其他发行版用 VERSION_ID（如 22.04）。
    """
    if not text:
        return None
    text = str(text)
    # Split off the debian_version part (after the marker line).
    # 把 debian_version 部分切出来（标记行之后）。
    debian_version = None
    if "---DEBIAN_VERSION---" in text:
        head, _, tail = text.partition("---DEBIAN_VERSION---")
        text = head
        debian_version = tail.strip().splitlines()
        debian_version = debian_version[0].strip() if debian_version else None
        if debian_version and not re.match(r"^\d+(\.\d+)*$", debian_version):
            debian_version = None
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    name = fields.get("NAME") or fields.get("PRETTY_NAME") or ""
    distro_id = (fields.get("ID") or "").lower()
    short = _short_distro_name(name, distro_id)
    if not short:
        return None
    # Debian: prefer the point release from /etc/debian_version.
    # Debian：优先用 /etc/debian_version 的小版本。
    if distro_id == "debian" or short == "Debian":
        version = debian_version or fields.get("VERSION_ID")
    else:
        version = fields.get("VERSION_ID")
    if version:
        version = version.strip()
        if not re.match(r"^\d+(\.\d+)*$", version):
            # VERSION_ID is sometimes "22.04 LTS" or similar; keep the numeric head.
            # VERSION_ID 有时是 "22.04 LTS" 这类，只取数字开头部分。
            m = re.match(r"^(\d+(?:\.\d+)*)", version)
            version = m.group(1) if m else None
    if not version:
        # No usable version: fall back to PRETTY_NAME cleaned up.
        # 没拿到可用版本：用清理过的 PRETTY_NAME 兜底。
        pretty = fields.get("PRETTY_NAME") or ""
        pretty = pretty.strip().strip('"').strip("'")
        if pretty:
            pretty = " ".join(pretty.split())
            # Strip the codename in parentheses, e.g. "Debian GNU/Linux 13 (trixie)" -> "Debian GNU/Linux 13".
            # 去掉括号里的代号，如 "Debian GNU/Linux 13 (trixie)" -> "Debian GNU/Linux 13"。
            pretty = re.sub(r"\s*\([^)]*\)\s*$", "", pretty)
            return pretty[:UNKNOWN_VERSION_MAX_LEN] or None
        return None
    result = f"{short} {version}"
    return result[:UNKNOWN_VERSION_MAX_LEN]


def load_os_versions():
    """读缓存文件，返回 {实例 OCID: {"version": str, "checked_at": str}}。

    文件不存在或损坏时返回空 dict（不抛异常，调用方按未检测处理）。
    """
    with _CACHE_LOCK:
        try:
            with open(OS_VERSIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def cached_os_version(instance_id):
    """取一台实例缓存的系统版本；无缓存返回 None。"""
    if not instance_id:
        return None
    entry = load_os_versions().get(instance_id) or {}
    return entry.get("version") or None


def save_os_version(instance_id, version):
    """把一台实例的系统版本写入缓存（文件 0600，与其他数据文件一致）。"""
    if not instance_id or not version:
        return
    with _CACHE_LOCK:
        try:
            with open(OS_VERSIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[instance_id] = {
            "version": version,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        parent = os.path.dirname(OS_VERSIONS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = OS_VERSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OS_VERSIONS_FILE)


def delete_os_version(instance_id):
    """清掉一台实例的系统版本缓存（实例终止 / DD / 重装后调）。"""
    if not instance_id:
        return
    with _CACHE_LOCK:
        try:
            with open(OS_VERSIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or instance_id not in data:
            return
        del data[instance_id]
        tmp = OS_VERSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OS_VERSIONS_FILE)


def prune_os_versions(known_ids):
    """清掉已不在实例列表里的缓存条目（实例终止后残留清理）。"""
    if known_ids is None:
        return
    known = {str(i) for i in known_ids}
    with _CACHE_LOCK:
        try:
            with open(OS_VERSIONS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        pruned = {k: v for k, v in data.items() if k in known}
        if len(pruned) == len(data):
            return
        tmp = OS_VERSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OS_VERSIONS_FILE)


def fetch_os_version(host, run_command, timeout=OS_FETCH_TIMEOUT):
    """SSH 检测一次系统版本（不读缓存、不写缓存）；失败返回 None。"""
    if not host:
        return None
    try:
        code, out, _err = run_command(host, OS_VERSION_COMMAND,
                                      timeout=timeout)
    except Exception:
        return None
    if code != 0 and not out:
        return None
    return parse_os_version(out)


def get_os_version(instance_id, host, run_command, force=False,
                   timeout=OS_FETCH_TIMEOUT):
    """取一台实例的系统版本：先读缓存，未命中（或 force）再 SSH 检测。

    检测成功写入缓存；失败返回 None（缓存里原来有就返回旧值——
    force 重测失败时不把旧值清掉）。
    """
    if not force:
        cached = cached_os_version(instance_id)
        if cached:
            return cached
    version = fetch_os_version(host, run_command, timeout=timeout)
    if version:
        save_os_version(instance_id, version)
        return version
    return cached_os_version(instance_id) if force else None
