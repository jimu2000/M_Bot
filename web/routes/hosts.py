#!/usr/bin/env python3
"""已保存 SSH 主机 CRUD（主机面板用）。

数据存在 DATA_DIR/hosts.json（JSON 文件），字段：
id / name / host / port / username / auth_type / key_path /
tags / last_used / notes。

安全规则（硬性）：
  - 密码绝不存明文：hosts.json 里只存 key_path；auth_type=password
    的主机不存密码，连接时前端每次让用户输入密码。
  - 请求体里即使带了 password 字段也会被丢弃，不写入文件。
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web import config
from web.deps import error_response, get_current_user

router = APIRouter()

# 主机记录允许的字段（请求体里的其他字段一律丢弃，尤其 password）。
_HOST_FIELDS = ("name", "host", "port", "username", "auth_type",
                "key_path", "tags", "notes")
# 认证方式：password（连接时现输密码）/ key（密钥文件）。
AUTH_TYPES = ("password", "key")

_HOSTS_LOCK = threading.Lock()


# ---------------- 文件存储 ----------------

def _hosts_file():
    """hosts.json 路径（每次都从 config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(config.DATA_DIR, "hosts.json")


def _load_hosts():
    """读 hosts.json，返回主机列表；文件不存在或格式不对返回 []。"""
    try:
        with open(_hosts_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_hosts(hosts):
    """把主机列表写回 hosts.json。"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_hosts_file(), "w", encoding="utf-8") as f:
        json.dump(hosts, f, ensure_ascii=False, indent=2)


def _clean_host_data(data):
    """只保留允许的字段（password 等一律丢弃，绝不进文件）。"""
    return {k: v for k, v in data.items() if k in _HOST_FIELDS}


def _validate_host(data):
    """校验主机字段；不合法抛 ValueError。"""
    if not (data.get("name") or "").strip():
        raise ValueError("主机名（name）不能为空")
    if not (data.get("host") or "").strip():
        raise ValueError("主机地址（host）不能为空")
    port = data.get("port", 22)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1-65535 之间，收到 {port!r}")
    auth_type = data.get("auth_type", "password")
    if auth_type not in AUTH_TYPES:
        raise ValueError(f"auth_type 只能是 password 或 key，收到 {auth_type!r}")
    if auth_type == "key" and not (data.get("key_path") or "").strip():
        raise ValueError("auth_type=key 时必须提供 key_path（密钥文件路径）")
    if auth_type == "password":
        # 密码认证的主机不存密钥路径（避免混淆）。
        data.pop("key_path", None)


# ---------------- CRUD（供路由与 Web SSH 模块共用） ----------------

def list_hosts():
    """全部已保存主机。"""
    with _HOSTS_LOCK:
        return _load_hosts()


def get_host(host_id):
    """按 id 取主机；不存在返回 None。"""
    with _HOSTS_LOCK:
        for host in _load_hosts():
            if host.get("id") == host_id:
                return host
    return None


def create_host(data):
    """新建主机，返回完整记录（含生成的 id 与 last_used=None）。"""
    cleaned = _clean_host_data(data)
    cleaned.setdefault("port", 22)
    cleaned.setdefault("username", "root")
    cleaned.setdefault("auth_type", "password")
    cleaned.setdefault("tags", [])
    cleaned.setdefault("notes", "")
    _validate_host(cleaned)
    host = {"id": uuid.uuid4().hex[:12], "last_used": None}
    host.update(cleaned)
    with _HOSTS_LOCK:
        hosts = _load_hosts()
        hosts.append(host)
        _save_hosts(hosts)
    return host


def update_host(host_id, data):
    """更新主机（只改传入的字段）；不存在返回 None。"""
    cleaned = _clean_host_data(data)
    with _HOSTS_LOCK:
        hosts = _load_hosts()
        for host in hosts:
            if host.get("id") != host_id:
                continue
            merged = dict(host)
            merged.update(cleaned)
            _validate_host(merged)
            host.update(cleaned)
            _save_hosts(hosts)
            return host
    return None


def delete_host(host_id):
    """删除主机；不存在返回 False。"""
    with _HOSTS_LOCK:
        hosts = _load_hosts()
        kept = [h for h in hosts if h.get("id") != host_id]
        if len(kept) == len(hosts):
            return False
        _save_hosts(kept)
        return True


def mark_host_used(host_id):
    """更新主机的上次使用时间为现在（Web SSH 连接成功后调用）。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _HOSTS_LOCK:
        hosts = _load_hosts()
        for host in hosts:
            if host.get("id") == host_id:
                host["last_used"] = now
                _save_hosts(hosts)
                return True
    return False


# ---------------- 路由 ----------------

class HostBody(BaseModel):
    """新建主机请求体（password 字段即使传入也会被丢弃，不存储）。"""

    name: str
    host: str
    port: int = 22
    username: str = "root"
    auth_type: str = "password"
    key_path: str = None
    tags: list = []
    notes: str = ""
    password: str = None  # 接受但丢弃：密码绝不存文件


class HostUpdateBody(BaseModel):
    """更新主机请求体（全部可选，只改传入的字段）。"""

    name: str = None
    host: str = None
    port: int = None
    username: str = None
    auth_type: str = None
    key_path: str = None
    tags: list = None
    notes: str = None
    password: str = None  # 接受但丢弃


@router.get("/api/hosts")
def list_all(username: str = Depends(get_current_user)):
    """已保存主机列表。"""
    return {"ok": True, "hosts": list_hosts()}


@router.post("/api/hosts", status_code=201)
def create(body: HostBody, username: str = Depends(get_current_user)):
    """新建主机。"""
    try:
        host = create_host(body.model_dump(exclude_none=True))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"ok": True, "host": host}


@router.put("/api/hosts/{host_id}")
def update(host_id: str, body: HostUpdateBody,
           username: str = Depends(get_current_user)):
    """更新主机。"""
    try:
        host = update_host(host_id, body.model_dump(exclude_none=True))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    if host is None:
        return error_response(f"主机不存在：{host_id}", status_code=404)
    return {"ok": True, "host": host}


@router.delete("/api/hosts/{host_id}")
def delete(host_id: str, username: str = Depends(get_current_user)):
    """删除主机。"""
    if not delete_host(host_id):
        return error_response(f"主机不存在：{host_id}", status_code=404)
    return {"ok": True, "host_id": host_id}
