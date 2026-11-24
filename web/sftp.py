#!/usr/bin/env python3
"""SFTP 文件面板后端（阶段 3b）。

复用 web.ssh 的 SSH 会话：从 session.client.get_transport() 用
paramiko.SFTPClient.from_transport(transport) 开 SFTP 会话，每个 SSH
会话缓存一个 SFTPClient（存在 manager 记录的 entry["sftp"] 里，SSH
会话关闭时由 web.ssh.close_session 一并关闭）。

安全（硬性要求）：
  - 所有路径必须先过 resolve_safe_path() 校验，限定在家目录内，
    绝不信任前端传来的路径；符号链接先用 sftp.normalize() 解析
    真实路径再校验，跳出家目录的拒绝访问。
  - 上传 / 在线编辑有大小上限；编辑只允许文本扩展名（或无扩展名）。

分块上传（阶段 5 工作包 E，/sftp/upload* 风格）：
  init -> chunk（多轮）-> complete，另有 progress 查询与 cancel 取消。
  分块暂存 DATA_DIR/sftp_uploads/{uploadId}/，complete 成功或 cancel
  后清理；超期（UPLOAD_STALE_HOURS）未完成的由 init 时顺带清理。

认证复用 web.ssh.require_auth（与面板登录共用 SESSIONS 单例）。
"""

import json
import logging
import os
import posixpath
import re
import shutil
import stat
import time
import uuid
from typing import Optional

import paramiko
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from web import config
from web.ssh import manager, require_auth

logger = logging.getLogger(__name__)

# ================= 配置区（放文件顶端，方便修改）=================
MAX_UPLOAD_BYTES = 100 * 1024 * 1024   # 单文件上传上限（100MB），超限 413
MAX_EDIT_BYTES = 1024 * 1024           # 在线编辑文件大小上限（1MB），超限 413
CHUNK_SIZE = 64 * 1024                 # 上传/下载分块大小（64KB，流式不占内存）
CHUNK_UPLOAD_SIZE = 1024 * 1024        # 分块上传的建议分块大小（1MB）
UPLOAD_STALE_HOURS = 24                # 分块上传临时目录保留时长（小时）

# 在线编辑允许的文本扩展名（小写，含点）；无扩展名文件也允许编辑。
# 不在白名单里的按二进制处理，拒绝在线编辑（走下载）。
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".json5",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".config",
    ".env", ".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".css", ".scss", ".less", ".html", ".htm",
    ".xml", ".csv", ".tsv", ".sql", ".service", ".timer", ".socket",
    ".c", ".h", ".cpp", ".cc", ".hpp", ".go", ".rs", ".java", ".rb",
    ".php", ".pl", ".lua", ".vim", ".dockerfile", ".gitignore",
    ".gitattributes", ".editorconfig", ".properties", ".gradle",
})
# ================================================================


# ================= 路径穿越防护 =================

def _is_within(home: str, path: str) -> bool:
    """path 是否在 home 内（含 home 本身）。home 与 path 均须是绝对路径。"""
    home = posixpath.normpath(home)
    path = posixpath.normpath(path)
    if path == home:
        return True
    prefix = home.rstrip("/") + "/"
    return path.startswith(prefix)


def resolve_safe_path(home: str, requested: Optional[str]) -> str:
    """把用户传的路径规范化后限定在家目录内，返回绝对路径。

    - requested 为空/None/"." -> home 本身；
    - 相对路径拼到 home 下，绝对路径直接用；
    - 内部 ".."（如 a/../b）合法，放行；
    - 跳出 home（../、../../etc/passwd、/etc/passwd）、NUL 字节、
      非绝对路径的 home -> 抛 ValueError。

    纯函数，不碰 SFTP，供测试直接调用。注意：这里只做词法校验，
    符号链接跳出需调用方再用 sftp.normalize() 解析后复核（见
    _resolve_real_path）。
    """
    if home is None or "\x00" in home:
        raise ValueError("家目录非法")
    home = posixpath.normpath(home)
    if not posixpath.isabs(home):
        raise ValueError("家目录必须是绝对路径")
    if requested is None or requested == "":
        return home
    if "\x00" in requested:
        raise ValueError("路径含非法字符")
    if posixpath.isabs(requested):
        resolved = posixpath.normpath(requested)
    else:
        resolved = posixpath.normpath(posixpath.join(home, requested))
    if not _is_within(home, resolved):
        raise ValueError("路径超出家目录范围")
    return resolved


def _resolve_real_path(sftp: paramiko.SFTPClient, home: str,
                       safe_path: str) -> str:
    """对已过词法校验的路径解析符号链接，返回真实路径。

    真实路径跳出 home -> 抛 PermissionError（调用方转 403）；
    路径不存在 -> sftp.normalize 抛 IOError（调用方按 errno 转 404）。
    """
    real = sftp.normalize(safe_path)
    if not _is_within(home, real):
        raise PermissionError("目标路径超出家目录范围（符号链接）")
    return real


def _resolve_parent(sftp: paramiko.SFTPClient, home: str,
                    safe_path: str) -> str:
    """解析父目录的真实路径，返回“真实父目录 + 文件名”。

    用于新建文件/上传/新建目录：目标本身不存在时 normalize 无法解析
    目标，但父目录可能是跳出 home 的符号链接，必须解析父目录复核。
    父目录不存在 -> IOError errno 2；跳出 home -> PermissionError。
    """
    parent = posixpath.dirname(safe_path)
    real_parent = _resolve_real_path(sftp, home, parent)
    return posixpath.join(real_parent, posixpath.basename(safe_path))


def _is_text_file(filename: str) -> bool:
    """按扩展名判断是否可在线编辑：无扩展名或在白名单里。"""
    _, ext = posixpath.splitext(filename)
    if not ext:
        return True
    return ext.lower() in TEXT_EXTENSIONS


# ================= 会话 / SFTP 客户端 =================

def _get_entry(session_id: str):
    """取 SSH 会话记录，不存在 -> 404。"""
    entry = manager.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return entry


def _get_sftp(entry) -> paramiko.SFTPClient:
    """取（或新建并缓存）该 SSH 会话的 SFTPClient。

    SFTP 不可用（如远端无 sftp-server、transport 已断）-> 502 友好错误，
    不把 paramiko 原始异常堆栈漏给前端。
    """
    sftp = entry.get("sftp")
    if sftp is not None:
        return sftp
    session = entry.get("session")
    client = getattr(session, "client", None) if session is not None else None
    if client is None:
        raise HTTPException(status_code=502,
                            detail="SSH 会话已断开，无法使用 SFTP")
    try:
        transport = client.get_transport()
    except Exception:
        transport = None
    if transport is None:
        raise HTTPException(status_code=502,
                            detail="SSH 会话已断开，无法使用 SFTP")
    try:
        if not transport.is_active():
            raise HTTPException(status_code=502,
                                detail="SSH 会话已断开，无法使用 SFTP")
        sftp = paramiko.SFTPClient.from_transport(transport)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("SFTP 打开失败：%s", exc)
        raise HTTPException(
            status_code=502,
            detail="远端 SFTP 不可用（可能未安装 sftp-server），"
                   "请检查远端 sftp 子系统")
    entry["sftp"] = sftp
    return sftp


def _get_home(entry, sftp: paramiko.SFTPClient) -> str:
    """取远端家目录（首次用 sftp.normalize(".")，之后缓存到会话记录）。"""
    home = entry.get("sftp_home")
    if home:
        return home
    try:
        home = sftp.normalize(".")
    except Exception as exc:
        logger.warning("获取远端家目录失败：%s", exc)
        raise HTTPException(status_code=502, detail="无法获取远端家目录")
    entry["sftp_home"] = home
    return home


def _safe_path_or_400(home: str, requested: Optional[str]) -> str:
    """resolve_safe_path 的接口包装：ValueError -> 400。"""
    try:
        return resolve_safe_path(home, requested)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _sftp_error(exc: Exception, action: str) -> HTTPException:
    """把 SFTP 层异常转成友好 HTTP 错误（不漏堆栈）。

    errno 2 -> 404（不存在），13 -> 403（无权限），39 -> 400（目录非空），
    其他 IOError -> 400，SSH 层异常 -> 502。
    """
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=f"{action}失败：文件或目录不存在")
    if isinstance(exc, OSError):
        errno = exc.errno
        if errno == 2:
            return HTTPException(status_code=404,
                                 detail=f"{action}失败：文件或目录不存在")
        if errno == 13:
            return HTTPException(status_code=403,
                                 detail=f"{action}失败：没有权限")
        if errno == 39:  # 目录非空
            return HTTPException(status_code=400,
                                 detail="目录非空，请先清空目录后再删除")
        if errno == 17:  # 已存在
            return HTTPException(status_code=400,
                                 detail=f"{action}失败：目标已存在")
        return HTTPException(status_code=400, detail=f"{action}失败：{exc}")
    if isinstance(exc, paramiko.SSHException):
        logger.warning("SFTP %s 时 SSH 异常：%s", action, exc)
        return HTTPException(status_code=502, detail="SSH 连接已断开")
    logger.warning("SFTP %s 时未知异常：%s", action, exc)
    return HTTPException(status_code=400, detail=f"{action}失败")


def _entry_info(sftp: paramiko.SFTPClient, home: str, dir_path: str,
                attr) -> dict:
    """把 SFTPAttributes 转成接口返回的条目 dict。

    符号链接：跟随 stat 判断目标是否目录，并用 normalize 解析真实路径，
    跳出 home 的标记 outside_home=True（前端置灰，下载/打开时后端再拒）。
    """
    name = attr.filename
    mode = attr.st_mode or 0
    is_symlink = stat.S_ISLNK(mode)
    is_dir = stat.S_ISDIR(mode)
    outside_home = False
    broken = False
    if is_symlink:
        full = posixpath.join(dir_path, name)
        try:
            target_stat = sftp.stat(full)  # 跟随链接
            is_dir = stat.S_ISDIR(target_stat.st_mode or 0)
        except (OSError, paramiko.SSHException):
            broken = True  # 断链：按普通文件展示，不可进入
        try:
            real = sftp.normalize(full)
            outside_home = not _is_within(home, real)
        except (OSError, paramiko.SSHException):
            broken = True
    try:
        permissions = stat.filemode(mode)
    except Exception:
        permissions = ""
    return {
        "name": name,
        "size": attr.st_size or 0,
        "mtime": attr.st_mtime or 0,
        "is_dir": is_dir,
        "is_symlink": is_symlink,
        "permissions": permissions,
        "outside_home": outside_home,
        "broken": broken,
    }


# ================= FastAPI 路由 =================

router = APIRouter()


class MkdirRequest(BaseModel):
    """POST mkdir 请求体：{path, name} 或只给 {path}（path 即新目录全路径）。"""
    path: Optional[str] = None
    name: Optional[str] = None


class RenameRequest(BaseModel):
    """POST rename 请求体：{src, dst}。"""
    src: str
    dst: str


class WriteFileRequest(BaseModel):
    """PUT file 请求体：{path, content}。"""
    path: str
    content: str = ""


@router.get("/api/ssh/sessions/{session_id}/sftp/list")
def api_sftp_list(session_id: str, path: Optional[str] = None,
                  _session=Depends(require_auth)):
    """列目录。path 缺省为家目录；返回 {path, entries: [...]}。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_path = _safe_path_or_400(home, path)
    try:
        # 目录本身也可能是跳出 home 的符号链接，先解析复核
        _resolve_real_path(sftp, home, safe_path)
        attrs = sftp.listdir_attr(safe_path)
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "列目录")
    entries = [_entry_info(sftp, home, safe_path, attr) for attr in attrs]
    # 目录在前、文件在后，各按名字排序
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    manager.touch(session_id)
    return {"path": safe_path, "home": home, "entries": entries}


@router.get("/api/ssh/sessions/{session_id}/sftp/download")
def api_sftp_download(session_id: str, path: str,
                      _session=Depends(require_auth)):
    """下载文件（StreamingResponse 分块读，不一次读进内存）。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_path = _safe_path_or_400(home, path)
    try:
        _resolve_real_path(sftp, home, safe_path)  # 解析符号链接再校验
        info = sftp.stat(safe_path)
        if stat.S_ISDIR(info.st_mode or 0):
            raise HTTPException(status_code=400, detail="不能下载目录")
        remote_file = sftp.open(safe_path, "rb")
    except HTTPException:
        raise
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "下载")

    def _iter_chunks():
        try:
            while True:
                chunk = remote_file.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            except Exception:
                pass

    manager.touch(session_id)
    filename = posixpath.basename(safe_path)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(_iter_chunks(),
                             media_type="application/octet-stream",
                             headers=headers)


@router.post("/api/ssh/sessions/{session_id}/sftp/upload")
def api_sftp_upload(session_id: str, path: Optional[str] = None,
                    file: UploadFile = File(...),
                    _session=Depends(require_auth)):
    """上传文件到 path 目录（缺省家目录），流式写入，超限 413。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_dir = _safe_path_or_400(home, path)
    filename = posixpath.basename(file.filename or "")
    if not filename or "\x00" in filename:
        raise HTTPException(status_code=400, detail="文件名非法")
    safe_path = posixpath.join(safe_dir, filename)
    if not _is_within(home, safe_path):
        raise HTTPException(status_code=400, detail="路径超出家目录范围")
    try:
        # 目标目录本身可能是跳出 home 的符号链接，解析真实路径复核
        safe_path = _resolve_parent(sftp, home, safe_path)
        remote_file = sftp.open(safe_path, "wb")
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "上传")
    written = 0
    try:
        while True:
            chunk = file.file.read(CHUNK_SIZE)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过上传上限（{MAX_UPLOAD_BYTES // 1024 // 1024}MB）")
            remote_file.write(chunk)
    except HTTPException:
        # 超限：删掉已写了一半的远端文件，不留残留
        try:
            remote_file.close()
        except Exception:
            pass
        try:
            sftp.remove(safe_path)
        except Exception:
            pass
        raise
    except (OSError, paramiko.SSHException) as exc:
        try:
            remote_file.close()
        except Exception:
            pass
        raise _sftp_error(exc, "上传")
    finally:
        try:
            remote_file.close()
        except Exception:
            pass
    manager.touch(session_id)
    return {"ok": True, "path": safe_path, "size": written}


@router.post("/api/ssh/sessions/{session_id}/sftp/mkdir")
def api_sftp_mkdir(session_id: str, body: MkdirRequest,
                   _session=Depends(require_auth)):
    """新建目录。body: {path, name} 或 {path}（path 即新目录全路径）。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    if body.name:
        if "/" in body.name or body.name in (".", "..") or "\x00" in body.name:
            raise HTTPException(status_code=400, detail="目录名非法")
        base = _safe_path_or_400(home, body.path)
        safe_path = posixpath.join(base, body.name)
        if not _is_within(home, safe_path):
            raise HTTPException(status_code=400, detail="路径超出家目录范围")
    else:
        if not body.path:
            raise HTTPException(status_code=400, detail="path 与 name 至少给一个")
        safe_path = _safe_path_or_400(home, body.path)
    try:
        # 父目录可能是跳出 home 的符号链接，解析真实路径复核
        safe_path = _resolve_parent(sftp, home, safe_path)
        sftp.mkdir(safe_path)
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "新建目录")
    manager.touch(session_id)
    return {"ok": True, "path": safe_path}


@router.delete("/api/ssh/sessions/{session_id}/sftp")
def api_sftp_delete(session_id: str, path: str,
                    _session=Depends(require_auth)):
    """删除文件或空目录；目录非空时 400 提示先清空（不递归删）。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_path = _safe_path_or_400(home, path)
    if safe_path == home:
        raise HTTPException(status_code=400, detail="不能删除家目录本身")
    try:
        _resolve_real_path(sftp, home, safe_path)  # 符号链接跳出则拒
        info = sftp.stat(safe_path)
        if stat.S_ISDIR(info.st_mode or 0):
            sftp.rmdir(safe_path)  # 非空目录会抛 OSError errno 39
        else:
            sftp.remove(safe_path)
    except HTTPException:
        raise
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "删除")
    manager.touch(session_id)
    return {"ok": True, "path": safe_path}


@router.post("/api/ssh/sessions/{session_id}/sftp/rename")
def api_sftp_rename(session_id: str, body: RenameRequest,
                    _session=Depends(require_auth)):
    """重命名 / 移动（src 与 dst 都必须在家目录内）。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_src = _safe_path_or_400(home, body.src)
    safe_dst = _safe_path_or_400(home, body.dst)
    if safe_src == home or safe_dst == home:
        raise HTTPException(status_code=400, detail="不能重命名家目录本身")
    try:
        _resolve_real_path(sftp, home, safe_src)
        # dst 的父目录可能是跳出 home 的符号链接，解析复核
        safe_dst = _resolve_parent(sftp, home, safe_dst)
        sftp.rename(safe_src, safe_dst)
    except HTTPException:
        raise
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "重命名")
    manager.touch(session_id)
    return {"ok": True, "src": safe_src, "dst": safe_dst}


@router.get("/api/ssh/sessions/{session_id}/sftp/file")
def api_sftp_read_file(session_id: str, path: str,
                       _session=Depends(require_auth)):
    """读小文本文件内容（在线编辑用）：上限 1MB，只允许文本扩展名。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_path = _safe_path_or_400(home, path)
    if not _is_text_file(safe_path):
        raise HTTPException(status_code=415, detail="仅支持编辑文本文件，请下载查看")
    try:
        _resolve_real_path(sftp, home, safe_path)
        info = sftp.stat(safe_path)
        if stat.S_ISDIR(info.st_mode or 0):
            raise HTTPException(status_code=400, detail="不能编辑目录")
        if (info.st_size or 0) > MAX_EDIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过在线编辑上限（{MAX_EDIT_BYTES // 1024}KB），请下载编辑")
        with sftp.open(safe_path, "r") as remote_file:
            raw = remote_file.read(MAX_EDIT_BYTES + 1)
    except HTTPException:
        raise
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "读取文件")
    if len(raw) > MAX_EDIT_BYTES:  # stat 与实际大小不一致时兜底
        raise HTTPException(status_code=413, detail="文件超过在线编辑上限")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="文件不是有效的 UTF-8 文本")
    manager.touch(session_id)
    return {"path": safe_path, "content": content, "size": len(raw)}


@router.put("/api/ssh/sessions/{session_id}/sftp/file")
def api_sftp_write_file(session_id: str, body: WriteFileRequest,
                        _session=Depends(require_auth)):
    """写文本文件（在线编辑保存）：上限 1MB，只允许文本扩展名。"""
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_path = _safe_path_or_400(home, body.path)
    if not _is_text_file(safe_path):
        raise HTTPException(status_code=415, detail="仅支持编辑文本文件")
    data = body.content.encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        raise HTTPException(status_code=413, detail="内容超过在线编辑上限（1MB）")
    try:
        try:
            _resolve_real_path(sftp, home, safe_path)
        except OSError as exc:
            if exc.errno == 2:
                # 新文件：目标不存在，改查父目录真实路径
                # （防父目录是跳出 home 的符号链接）
                _resolve_parent(sftp, home, safe_path)
            else:
                raise
        with sftp.open(safe_path, "w") as remote_file:
            remote_file.write(data)
    except HTTPException:
        raise
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "保存文件")
    manager.touch(session_id)
    return {"ok": True, "path": safe_path, "size": len(data)}


# ================= 分块上传（带进度） =================

# uploadId 格式：uuid4 hex（32 位小写十六进制），防路径穿越。
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _uploads_dir():
    """分块上传临时根目录（每次都从 config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(config.DATA_DIR, "sftp_uploads")


def _upload_dir(upload_id):
    """某次上传的临时目录；uploadId 格式非法时抛 ValueError。"""
    if not _UPLOAD_ID_RE.match(upload_id or ""):
        raise ValueError("uploadId 格式非法")
    return os.path.join(_uploads_dir(), upload_id)


def _upload_meta_path(upload_id):
    """某次上传的元信息文件路径。"""
    return os.path.join(_upload_dir(upload_id), "meta.json")


def _load_upload_meta(upload_id):
    """读上传元信息；不存在 / 损坏 / 格式非法时返回 None。"""
    try:
        with open(_upload_meta_path(upload_id), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _save_upload_meta(upload_id, meta):
    """写上传元信息（目录已存在时直接写）。"""
    with open(_upload_meta_path(upload_id), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _total_chunks(size):
    """文件总分块数（向上取整；size 为 0 时为 0）。"""
    if size <= 0:
        return 0
    return (size + CHUNK_UPLOAD_SIZE - 1) // CHUNK_UPLOAD_SIZE


def _received_bytes(upload_id, meta):
    """按磁盘上已有的分块文件重新统计已收字节（重传分块时不重复计）。"""
    total = 0
    chunks = set()
    directory = _upload_dir(upload_id)
    for index in meta.get("chunks") or []:
        part = os.path.join(directory, f"{index}.part")
        try:
            total += os.path.getsize(part)
            chunks.add(index)
        except OSError:
            continue
    return total, sorted(chunks)


def _upload_progress(upload_id, meta):
    """返回上传进度字典（progress / chunk 接口共用）。"""
    total = meta.get("size") or 0
    received, chunks = _received_bytes(upload_id, meta)
    total_chunks = _total_chunks(total)
    percent = round(100.0 * received / total, 1) if total else 0.0
    return {
        "uploadId": upload_id,
        "filename": meta.get("filename"),
        "receivedBytes": received,
        "totalBytes": total,
        "percent": percent,
        "chunksReceived": len(chunks),
        "totalChunks": total_chunks,
        "status": "complete" if (total_chunks and len(chunks) >= total_chunks
                                 and received >= total)
                  else "uploading",
    }


def _cleanup_stale_uploads():
    """清理超期未完成的上传临时目录（best-effort，失败静默跳过）。"""
    root = _uploads_dir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    cutoff = time.time() - UPLOAD_STALE_HOURS * 3600
    for name in names:
        if not _UPLOAD_ID_RE.match(name):
            continue
        directory = os.path.join(root, name)
        try:
            meta = _load_upload_meta(name)
            created = (meta or {}).get("createdAt") or 0
            if created and created > cutoff:
                continue
            if not created and os.path.getmtime(directory) > cutoff:
                continue
            shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            continue


def _get_upload_or_404(session_id, upload_id):
    """取上传元信息：不存在、格式非法、或不属于该会话时抛 404。"""
    try:
        meta = _load_upload_meta(upload_id)
    except ValueError:
        meta = None
    if meta is None or meta.get("sessionId") != session_id:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return meta


class ChunkUploadInitRequest(BaseModel):
    """POST upload/init 请求体：{filename, size, path?}。"""
    filename: str
    size: int
    path: Optional[str] = None


class ChunkUploadCompleteRequest(BaseModel):
    """POST upload/complete 请求体：{uploadId}。"""
    uploadId: str


class ChunkUploadCancelRequest(BaseModel):
    """POST upload/cancel 请求体：{uploadId, confirm}（取消需明确确认）。"""
    uploadId: str
    confirm: bool = False


@router.post("/api/ssh/sessions/{session_id}/sftp/upload/init")
@router.post("/api/terminal/{session_id}/sftp/upload/init")
def api_sftp_upload_init(session_id: str, body: ChunkUploadInitRequest,
                         _session=Depends(require_auth)):
    """分块上传第一步：建上传任务，返回 uploadId + 分块大小。

    filename 只取 basename（防路径穿越）；size 超过 MAX_UPLOAD_BYTES
    时 413；目标目录先做词法校验（真实路径在 complete 时再解析复核）。
    """
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    safe_dir = _safe_path_or_400(home, body.path)
    filename = posixpath.basename(body.filename or "")
    if not filename or filename in (".", "..") or "\x00" in filename:
        raise HTTPException(status_code=400, detail="文件名非法")
    if body.size is None or body.size <= 0:
        raise HTTPException(status_code=400, detail="文件大小必须大于 0")
    if body.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上传上限（{MAX_UPLOAD_BYTES // 1024 // 1024}MB）")
    _cleanup_stale_uploads()
    upload_id = uuid.uuid4().hex
    directory = _upload_dir(upload_id)
    os.makedirs(directory, exist_ok=True)
    meta = {
        "uploadId": upload_id,
        "sessionId": session_id,
        "filename": filename,
        "size": body.size,
        "chunkSize": CHUNK_UPLOAD_SIZE,
        "path": safe_dir,
        "createdAt": time.time(),
        "chunks": [],
    }
    _save_upload_meta(upload_id, meta)
    manager.touch(session_id)
    return {"ok": True, "uploadId": upload_id,
            "chunkSize": CHUNK_UPLOAD_SIZE,
            "totalChunks": _total_chunks(body.size)}


@router.post("/api/ssh/sessions/{session_id}/sftp/upload/chunk")
@router.post("/api/terminal/{session_id}/sftp/upload/chunk")
def api_sftp_upload_chunk(session_id: str, uploadId: str, chunkIndex: int,
                          file: UploadFile = File(...),
                          _session=Depends(require_auth)):
    """分块上传第二步：上传一分块（multipart：uploadId + chunkIndex + file）。

    分块大小不得超过 CHUNK_UPLOAD_SIZE（最后一块除外，按剩余算）；
    同一 chunkIndex 重传时覆盖旧分块（不断点续传时前端可重传失败块）。
    返回当前进度。
    """
    _get_entry(session_id)
    meta = _get_upload_or_404(session_id, uploadId)
    total = meta.get("size") or 0
    total_chunks = _total_chunks(total)
    if chunkIndex < 0 or chunkIndex >= total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"chunkIndex 超出范围（0-{total_chunks - 1}）")
    # 该块应有的大小：最后一块按剩余字节算，其余按 CHUNK_UPLOAD_SIZE。
    expected = (total - chunkIndex * CHUNK_UPLOAD_SIZE
                if chunkIndex == total_chunks - 1 else CHUNK_UPLOAD_SIZE)
    data = file.file.read(expected + 1)
    if len(data) > expected:
        raise HTTPException(status_code=400, detail="分块数据超过应有大小")
    if len(data) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"分块数据不完整（应 {expected} 字节，收到 {len(data)} 字节）")
    part_path = os.path.join(_upload_dir(uploadId), f"{chunkIndex}.part")
    with open(part_path, "wb") as f:
        f.write(data)
    chunks = set(meta.get("chunks") or [])
    chunks.add(chunkIndex)
    meta["chunks"] = sorted(chunks)
    _save_upload_meta(uploadId, meta)
    manager.touch(session_id)
    return {"ok": True, **_upload_progress(uploadId, meta)}


@router.get("/api/ssh/sessions/{session_id}/sftp/upload/progress")
@router.get("/api/terminal/{session_id}/sftp/upload/progress")
def api_sftp_upload_progress(session_id: str, uploadId: str,
                             _session=Depends(require_auth)):
    """查分块上传进度：已上传字节 / 总字节 / 百分比 / 分块数。"""
    _get_entry(session_id)
    meta = _get_upload_or_404(session_id, uploadId)
    manager.touch(session_id)
    return {"ok": True, **_upload_progress(uploadId, meta)}


@router.post("/api/ssh/sessions/{session_id}/sftp/upload/complete")
@router.post("/api/terminal/{session_id}/sftp/upload/complete")
def api_sftp_upload_complete(session_id: str,
                             body: ChunkUploadCompleteRequest,
                             _session=Depends(require_auth)):
    """分块上传第三步：分块齐了就按序合并写到远端，成功后清理临时分块。

    分块不齐时 400；远端写入失败时保留临时分块（前端可重试 complete，
    不用重新上传）。目标路径同样做符号链接复核（防跳出家目录）。
    """
    entry = _get_entry(session_id)
    sftp = _get_sftp(entry)
    home = _get_home(entry, sftp)
    meta = _get_upload_or_404(session_id, body.uploadId)
    progress = _upload_progress(body.uploadId, meta)
    if progress["status"] != "complete":
        raise HTTPException(
            status_code=400,
            detail=f"分块不完整：已收到 {progress['chunksReceived']}/"
                   f"{progress['totalChunks']} 块")
    safe_path = posixpath.join(meta.get("path") or home,
                               meta.get("filename") or "")
    if not _is_within(home, safe_path):
        raise HTTPException(status_code=400, detail="路径超出家目录范围")
    try:
        safe_path = _resolve_parent(sftp, home, safe_path)
        remote_file = sftp.open(safe_path, "wb")
    except (OSError, paramiko.SSHException, PermissionError) as exc:
        raise _sftp_error(exc, "上传")
    written = 0
    try:
        for index in range(progress["totalChunks"]):
            part_path = os.path.join(_upload_dir(body.uploadId),
                                     f"{index}.part")
            with open(part_path, "rb") as part:
                while True:
                    chunk = part.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    remote_file.write(chunk)
                    written += len(chunk)
    except (OSError, paramiko.SSHException) as exc:
        try:
            remote_file.close()
        except Exception:
            pass
        raise _sftp_error(exc, "上传")
    finally:
        try:
            remote_file.close()
        except Exception:
            pass
    shutil.rmtree(_upload_dir(body.uploadId), ignore_errors=True)
    manager.touch(session_id)
    return {"ok": True, "path": safe_path, "size": written}


@router.post("/api/ssh/sessions/{session_id}/sftp/upload/cancel")
@router.post("/api/terminal/{session_id}/sftp/upload/cancel")
def api_sftp_upload_cancel(session_id: str,
                           body: ChunkUploadCancelRequest,
                           _session=Depends(require_auth)):
    """取消分块上传：删除临时分块（需 confirm=true 明确确认）。"""
    if not body.confirm:
        raise HTTPException(status_code=400,
                            detail="取消上传需要确认：请求体须含 confirm=true")
    _get_entry(session_id)
    meta = _get_upload_or_404(session_id, body.uploadId)
    shutil.rmtree(_upload_dir(body.uploadId), ignore_errors=True)
    manager.touch(session_id)
    return {"ok": True, "uploadId": body.uploadId,
            "filename": meta.get("filename")}
