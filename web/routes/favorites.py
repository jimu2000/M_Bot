#!/usr/bin/env python3
"""终端收藏夹（阶段 5 工作包 E）。

  - GET /api/term-favorites：全部收藏。
  - POST /api/term-favorites：新增一条收藏 {name, command, hostId?}。
  - DELETE /api/term-favorites/{id}：删除一条收藏
    （前端删除前二次确认；后端按 id 精确删，不存在时 404）。

数据存在 DATA_DIR/term_favorites.json，结构
{version: 1, items: [{id, name, command, hostId}]}，
与 /term-favorites 的 {version, items} 形状一致。
hostId 为空字符串表示不限主机（任意终端都能用）。
"""

import json
import os
import threading
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web import config
from web.deps import error_response, get_current_user

router = APIRouter()

# 收藏夹文件格式版本（固定 1）。
FAVORITES_VERSION = 1
# 单条收藏的命令最大长度（字符）。
MAX_COMMAND_LENGTH = 2000
# 收藏名称最大长度（字符）。
MAX_NAME_LENGTH = 100

_FAVORITES_LOCK = threading.Lock()


def _favorites_file():
    """term_favorites.json 路径（每次都从 config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(config.DATA_DIR, "term_favorites.json")


def _load_favorites():
    """读收藏夹文件，返回 {version, items}；不存在/损坏时返回空结构。"""
    try:
        with open(_favorites_file(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"version": FAVORITES_VERSION, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"version": FAVORITES_VERSION, "items": []}
    return {"version": FAVORITES_VERSION,
            "items": [item for item in data["items"]
                      if isinstance(item, dict) and item.get("id")]}


def _save_favorites(data):
    """把收藏夹写回文件。"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_favorites_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_favorites():
    """全部收藏（{version, items}）。"""
    with _FAVORITES_LOCK:
        return _load_favorites()


def add_favorite(name, command, host_id=None):
    """新增一条收藏，返回完整记录。

    name/command 为空或超长时抛 ValueError。
    """
    name = (name or "").strip()
    command = (command or "").strip()
    if not name:
        raise ValueError("收藏名称不能为空")
    if not command:
        raise ValueError("收藏命令不能为空")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"收藏名称不能超过 {MAX_NAME_LENGTH} 个字符")
    if len(command) > MAX_COMMAND_LENGTH:
        raise ValueError(f"收藏命令不能超过 {MAX_COMMAND_LENGTH} 个字符")
    item = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "command": command,
        "hostId": host_id or "",
    }
    with _FAVORITES_LOCK:
        data = _load_favorites()
        data["items"].append(item)
        _save_favorites(data)
    return item


def delete_favorite(favorite_id):
    """删除一条收藏；不存在返回 False。"""
    with _FAVORITES_LOCK:
        data = _load_favorites()
        kept = [item for item in data["items"]
                if item.get("id") != favorite_id]
        if len(kept) == len(data["items"]):
            return False
        data["items"] = kept
        _save_favorites(data)
        return True


class FavoriteBody(BaseModel):
    """新增收藏请求体。"""

    name: str
    command: str
    hostId: str = None


@router.get("/api/term-favorites")
def list_all(username: str = Depends(get_current_user)):
    """全部终端收藏（{version, items}）。"""
    return {"ok": True, **list_favorites()}


@router.post("/api/term-favorites", status_code=201)
def create(body: FavoriteBody, username: str = Depends(get_current_user)):
    """新增一条终端收藏。"""
    try:
        item = add_favorite(body.name, body.command, body.hostId)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"ok": True, "item": item}


@router.delete("/api/term-favorites/{favorite_id}")
def delete(favorite_id: str, username: str = Depends(get_current_user)):
    """删除一条终端收藏（前端删除前二次确认）。"""
    if not delete_favorite(favorite_id):
        return error_response(f"收藏不存在：{favorite_id}", status_code=404)
    return {"ok": True, "id": favorite_id}
