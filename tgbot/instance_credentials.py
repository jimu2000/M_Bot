#!/usr/bin/env python3
"""按实例存 root 密码（实例凭据库）：开机成功后把 root 密码按实例 OCID 存起来，供之后 SSH 用。

用户决定（2026-01-05，明确推翻旧规则）：机器人要自己记住每台实例的
IP 和 root 密码，否则开机后、DD 后没法 SSH 登录（包括之后 DD 完成后
SSH 进去追加上传的 SSH 公钥那一步）。旧规则“密码绝不落盘、面板不存
密码”即日起作废，改为：密码只存这个服务端文件，绝不发给浏览器、
绝不记日志、绝不进通知与任务导出。

  - save_credential：开机成功后调用（机器人端与网页端各一处），
    用已生成的那个密码，不另生成。
  - get_root_password / resolve_root_password：SSH 与 DD 步骤取密码
    用；resolve 先看环境变量固定密码（MBOT_FIXED_ROOT_PASSWORD，
    仍优先），再看本库。
  - delete_credential / prune_credentials：实例终止时删条目。
  - 凭据文件 tgbot/data/instance_credentials.json：
    {实例 OCID: {ip, root_password, updated_at, instance_name}}；
    文件 0600，只供 .160 上的 bot / web 服务端进程读。

安全红线：本模块的任何函数都不记密码、不记 IP 到日志（只记实例
ID 后 4 位）；没有任何接口把本库内容返回给前端（任务记录的公开
视图本来就不含密码；本库也不进任务记录）。
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

from tgbot import config as bot_config

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# Instance credentials file (same directory as cpu_models.json / bark.json). 实例凭据文件（与 cpu_models.json / bark.json 同目录）。
CREDENTIALS_FILE = os.path.join(bot_config.DATA_DIR,
                                "instance_credentials.json")
# =====================================================


# Cache file read/write lock: the bot and the web panel both write this file (each creation saves one entry), so load-modify-write must be serialized, or two instances created at once would overwrite each other's entries. 读写锁：机器人与网页端都会写这个文件（每次开机存一条），load-modify-write 必须串行，否则同时开两台会互相覆盖。
_CREDENTIALS_LOCK = threading.Lock()


def _tail(instance_id):
    """实例 OCID 后 4 位（日志用，不暴露完整 OCID）。"""
    return (instance_id or "?")[-4:]


def load_credentials():
    """读凭据文件，返回 {实例 OCID: {ip, root_password, updated_at, instance_name}}。

    文件不存在或损坏时返回空 dict（不抛异常，调用方按无凭据处理）。
    """
    with _CREDENTIALS_LOCK:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def get_credential(instance_id):
    """取一台实例的凭据条目；无条目返回 None。"""
    if not instance_id:
        return None
    entry = load_credentials().get(instance_id)
    return dict(entry) if isinstance(entry, dict) else None


def get_root_password(instance_id):
    """取一台实例保存的 root 密码；无条目或无密码返回 None。"""
    entry = get_credential(instance_id)
    if not entry:
        return None
    password = entry.get("root_password")
    return password if isinstance(password, str) and password else None


def resolve_root_password(instance_id):
    """SSH / DD 步骤取 root 密码：固定密码优先，再看本库，都没有返回 None。

    固定密码（MBOT_FIXED_ROOT_PASSWORD）是所有新机共用的那份，仍优先；
    没设固定密码时回落到开机时按实例存的那份。
    """
    fixed = os.environ.get("MBOT_FIXED_ROOT_PASSWORD", "").strip()
    if fixed:
        return fixed
    return get_root_password(instance_id)


def save_credential(instance_id, root_password, ip=None,
                    instance_name=None):
    """把一台实例的 root 密码写入凭据库（文件 0600）。

    instance_id 与 root_password 为空时不写；已存在条目时覆盖
    （密码与 IP 都以最新一次开机为准）。绝不记密码、不记 IP。
    """
    if not instance_id or not root_password:
        return
    with _CREDENTIALS_LOCK:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[instance_id] = {
            "ip": ip or None,
            "root_password": root_password,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "instance_name": instance_name or None,
        }
        parent = os.path.dirname(CREDENTIALS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass
    logger.debug("已保存实例 …%s 的 root 密码", _tail(instance_id))


def update_credential_ip(instance_id, ip):
    """只更新一条凭据的 IP（公网 IP 后到时补记），无条目时不做任何事。"""
    if not instance_id or not ip:
        return
    with _CREDENTIALS_LOCK:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or instance_id not in data:
            return
        entry = data[instance_id]
        if not isinstance(entry, dict):
            return
        entry["ip"] = ip
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass


def delete_credential(instance_id):
    """删掉一台实例的凭据条目（实例终止后调用，避免残留密码）。"""
    if not instance_id:
        return
    with _CREDENTIALS_LOCK:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or instance_id not in data:
            return
        del data[instance_id]
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass
    logger.debug("已删除实例 …%s 的 root 密码", _tail(instance_id))


def prune_credentials(keep_ids):
    """删掉不在 keep_ids 里的凭据条目（实例已终止后的清理）。

    只有实际删掉条目时才写文件；返回删掉的条数。
    """
    keep = set(keep_ids or [])
    with _CREDENTIALS_LOCK:
        try:
            with open(CREDENTIALS_FILE, encoding="utf-8") as f:
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
        with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass
    return len(stale)
