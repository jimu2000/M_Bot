#!/usr/bin/env python3
"""Web SSH 终端后端（FastAPI + WebSocket + Paramiko + xterm.js 前端）。

设计参考 py-web-ssh：每个 SSH 连接对应一个 UUID 会话，前端用 WebSocket
收发终端数据；服务端保留每个会话最近的输出（output_buffer），前端断线
重连时先调 snapshot 接口回放输出，再重新接上 WebSocket。

协议（WebSocket JSON 文本消息）：
  前端 -> 后端：{"type": "input", "data": "..."}   键盘输入，写进 channel
               {"type": "resize", "cols": 80, "rows": 24}
               {"type": "ping"}                    心跳，后端回 {"type": "pong"}
  后端 -> 前端：{"type": "output", "data": "..."}  channel 输出，base64 编码
               {"type": "closed", "reason": "..."} SSH 通道已断开

安全注意：
  - 密码只在内存里用，绝不落盘、不记日志。
  - host key 用 AutoAddPolicy 记到 DATA_DIR/known_hosts（首次连接不失败）。

认证对接：复用 web.auth 的全局单例 SESSIONS（登录流程与本模块共用
同一个会话字典）；web.config 提供 DATA_DIR（hosts.json 所在目录）与
SESSION_COOKIE 名。web.auth / web.config 不可用时（比如单独跑本模块
的测试）回退到本地默认值与极简内存会话存储。
"""

import asyncio
import base64
import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi import WebSocketDisconnect
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ================= 配置区（放文件顶端，方便修改）=================
SSH_CONNECT_TIMEOUT = 10       # SSH 连接超时（秒）
SSH_BANNER_TIMEOUT = 10        # 欢迎横幅命令执行超时（秒）
OUTPUT_BUFFER_LINES = 1000     # 每个会话保留的最近输出行数（供重连回放快照）
MAX_SESSIONS = 20              # 最大并发 SSH 会话数（最大并发 20）
IDLE_TIMEOUT_MINUTES = 30      # 会话空闲超时（分钟），超时自动关闭
SESSION_GRACE_MINUTES = 5      # WebSocket 断开后会话保留时长（分钟），供重连
CLEANUP_INTERVAL_SECONDS = 60  # 空闲清理后台线程的检查间隔（秒）
WS_OUTPUT_POLL_SECONDS = 0.02  # 输出泵轮询 channel 的间隔（秒）

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"  # AutoAddPolicy 记住的 host key
HOSTS_FILE = DATA_DIR / "hosts.json"         # 已保存主机列表（host_id 查询用）
SESSION_COOKIE = "mbot_session"              # 登录会话 cookie 名

try:
    # 与面板其他模块共用配置：hosts.json 在 web.config.DATA_DIR 下，
    # 登录 cookie 名也在 web.config 里定义。
    from web import config as _web_config

    DATA_DIR = Path(_web_config.DATA_DIR)
    KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"
    HOSTS_FILE = DATA_DIR / "hosts.json"
    SESSION_COOKIE = _web_config.SESSION_COOKIE
except Exception:
    pass  # web.config 不可用时用上面的默认值
# ================================================================

# 欢迎横幅里的 ASCII 艺术字
R_BOT_ART = r"""
 ____       ____   ___ _____
|  _ \     | __ ) / _ \_   _|
| |_) |    |  _ \| | | || |
|  _ <     | |_) | |_| || |
|_| \_\    |____/ \___/ |_|
"""

# 欢迎横幅里跑的系统信息命令
BANNER_COMMAND = "uname -a; uptime; df -h /; free -h"


def build_banner(sysinfo: str) -> str:
    """拼欢迎横幅：ASCII 艺术字 + 系统信息（uname 黄、uptime 绿）。"""
    lines = [""]
    lines.append("\x1b[1;36m" + R_BOT_ART.strip("\n") + "\x1b[0m")
    lines.append("\x1b[1;35mR-BOT · Web SSH\x1b[0m")
    lines.append("")
    info_lines = [line for line in sysinfo.splitlines() if line.strip()]
    if info_lines:
        lines.append("\x1b[1;33m" + info_lines[0] + "\x1b[0m")  # uname -a
        if len(info_lines) > 1:
            lines.append("\x1b[1;32m" + info_lines[1] + "\x1b[0m")  # uptime
        lines.extend(info_lines[2:])  # df / free 原样输出
    else:
        lines.append("(系统信息获取失败)")
    lines.append("")
    return "\n".join(lines) + "\n"


# ================= 认证（兼容 web.auth，见模块 docstring）=================

class _FallbackSessionStore:
    """极简内存会话存储（web.auth 未就绪时的临时实现）。

    接口与预期的 web.auth.SessionStore 一致：get / create / delete。
    """

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        import secrets

        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "username": username,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        return token

    def get(self, token: Optional[str]):
        if not token:
            return None
        with self._lock:
            return self._sessions.get(token)

    def delete(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)


_session_store = None


def get_session_store():
    """取会话存储：优先用 web.auth 的全局单例 SESSIONS。

    必须用单例而非新建 SessionStore()——登录流程把会话写进 SESSIONS，
    新建实例的字典是空的，会导致已登录用户也被 401。
    web.auth 不可用时用本地极简实现（内存字典）。
    """
    global _session_store
    if _session_store is None:
        try:
            from web.auth import SESSIONS

            _session_store = SESSIONS
            logger.info("Web SSH 使用 web.auth.SESSIONS 单例")
        except Exception:
            try:
                from web.auth import SessionStore

                _session_store = SessionStore()
            except Exception as exc:
                logger.warning("web.auth 不可用，使用本地内存会话存储：%s", exc)
                _session_store = _FallbackSessionStore()
    return _session_store


# 模块级单例（测试可直接 patch web.ssh.session_store）
session_store = get_session_store()


def _token_from_request(request: Request) -> Optional[str]:
    """从 cookie 或查询参数读登录 token。"""
    return request.cookies.get(SESSION_COOKIE) or request.query_params.get("token")


def require_auth(request: Request):
    """REST 端点的登录校验（FastAPI Depends）。"""
    session = session_store.get(_token_from_request(request))
    if session is None:
        raise HTTPException(status_code=401, detail="未登录")
    return session


def _ws_auth(websocket: WebSocket):
    """WebSocket 的登录校验，返回会话 dict 或 None。"""
    token = websocket.cookies.get(SESSION_COOKIE) or websocket.query_params.get("token")
    return session_store.get(token) if token else None


# ================= 主机列表（hosts.json）=================

def load_hosts() -> list:
    """读 DATA_DIR/hosts.json，返回主机记录列表；文件不存在返回空列表。

    支持两种格式：直接是 list，或 {"hosts": [...]}。
    每条记录：{id, name, host, port, username, key_path?}
    """
    if not HOSTS_FILE.exists():
        return []
    try:
        data = json.loads(HOSTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("hosts.json 读取失败：%s", exc)
        return []
    if isinstance(data, dict):
        data = data.get("hosts", [])
    return data if isinstance(data, list) else []


def get_host(host_id: str):
    """按 id 查一台主机，找不到返回 None。"""
    for record in load_hosts():
        if str(record.get("id")) == str(host_id):
            return record
    return None


# ================= SSH 会话 =================

class SSHSession:
    """一个 SSH 连接：持有一个 paramiko.SSHClient + 一个交互 shell channel。"""

    def __init__(self):
        self.client: Optional[paramiko.SSHClient] = None
        self.channel = None
        self.banner = ""  # 欢迎横幅文本（connect 成功后生成）
        self._active = False

    def connect(self, host: str, port: int = 22, username: str = "root",
                password: Optional[str] = None,
                key_path: Optional[str] = None,
                cols: int = 80, rows: int = 24) -> str:
        """建连并打开交互 shell，返回欢迎横幅文本。

        密码或密钥二选一（都不给时走 agent / 默认密钥查找）。
        密码只在内存里传给 paramiko，绝不落盘、不记日志。
        """
        if key_path and not os.path.exists(key_path):
            raise FileNotFoundError(f"密钥文件不存在：{key_path}")
        client = paramiko.SSHClient()
        try:
            if KNOWN_HOSTS_PATH.exists():
                try:
                    client.load_host_keys(str(KNOWN_HOSTS_PATH))
                except Exception as exc:
                    logger.warning("known_hosts 读取失败：%s", exc)
            # AutoAddPolicy：首次连接自动记住 host key，不用 RejectPolicy
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = {
                "hostname": host,
                "port": port,
                "username": username,
                "timeout": SSH_CONNECT_TIMEOUT,
                "banner_timeout": SSH_BANNER_TIMEOUT,
            }
            if key_path:
                kwargs["key_filename"] = key_path
                kwargs["look_for_keys"] = False
            elif password is not None:
                kwargs["password"] = password
                kwargs["look_for_keys"] = False
                kwargs["allow_agent"] = False
            client.connect(**kwargs)
            try:
                KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(KNOWN_HOSTS_PATH))
            except Exception as exc:
                logger.warning("known_hosts 保存失败（不影响连接）：%s", exc)
            # 先用 exec_command 跑系统信息命令拿横幅内容，再开交互 shell。
            # 同一个 SSHClient 可以开多个 channel，两者不冲突。
            sysinfo = self._fetch_system_info(client)
            channel = client.invoke_shell(width=cols, height=rows)
            self.client = client
            self.channel = channel
            self._active = True
            self.banner = build_banner(sysinfo)
            return self.banner
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _fetch_system_info(client: paramiko.SSHClient) -> str:
        """exec_command 跑 uname/uptime/df/free，失败返回空串（不阻断连接）。"""
        try:
            stdin, stdout, stderr = client.exec_command(
                BANNER_COMMAND, timeout=SSH_BANNER_TIMEOUT)
            try:
                stdout.channel.recv_exit_status()  # 等命令跑完
            except Exception:
                pass
            raw = stdout.read()
            return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception as exc:
            logger.warning("获取系统信息失败：%s", exc)
            return ""
        finally:
            try:
                stdin.close()
                stdout.close()
                stderr.close()
            except Exception:
                pass

    def send(self, data) -> None:
        """写数据进 channel（str 自动按 utf-8 编码）。"""
        if self.channel is None:
            raise RuntimeError("SSH 会话未连接")
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        self.channel.send(data)

    def recv(self) -> bytes:
        """非阻塞读 channel，有数据返回 bytes，没数据返回 b""。"""
        if self.channel is None:
            return b""
        try:
            if self.channel.recv_ready():
                return self.channel.recv(65536)
        except (EOFError, OSError):
            return b""
        return b""

    def resize(self, cols: int, rows: int) -> None:
        """调终端大小。"""
        if self.channel is not None:
            self.channel.resize_pty(width=cols, height=rows)

    def is_active(self) -> bool:
        """channel 还活着且 transport 还连着。"""
        if not self._active or self.channel is None:
            return False
        if getattr(self.channel, "closed", True):
            return False
        transport = self.client.get_transport() if self.client else None
        if transport is not None and not transport.is_active():
            return False
        return True

    def close(self) -> None:
        """关 channel 与 client。"""
        self._active = False
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None


# ================= 会话管理器 =================

class SSHSessionManager:
    """线程安全的 SSH 会话字典 + 空闲清理后台线程。

    每条记录：{session, host, port, username, host_id, created_at,
    last_active, output_buffer(deque), ws_connected, disconnected_at}
    """

    def __init__(self, start_cleanup_thread: bool = True):
        self._sessions = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cleanup_thread = None
        if start_cleanup_thread:
            thread = threading.Thread(target=self._cleanup_loop, daemon=True,
                                      name="ssh-cleanup")
            thread.start()
            self._cleanup_thread = thread

    def _cleanup_loop(self) -> None:
        """后台线程：每分钟跑一次 cleanup_idle。"""
        while not self._stop.wait(CLEANUP_INTERVAL_SECONDS):
            try:
                self.cleanup_idle()
            except Exception as exc:
                logger.warning("空闲清理异常：%s", exc)

    def stop(self) -> None:
        """停后台线程（测试/关机用）。"""
        self._stop.set()

    def create_session(self, host: str, port: int = 22, username: str = "root",
                       password: Optional[str] = None,
                       key_path: Optional[str] = None,
                       host_id: Optional[str] = None,
                       cols: int = 80, rows: int = 24) -> str:
        """新建 SSH 会话，返回 session_id（uuid4）。

        超过 MAX_SESSIONS 抛 RuntimeError；连接失败抛 paramiko 异常。
        """
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(f"SSH 会话已满（{MAX_SESSIONS} 个），请先关闭一些")
        session = SSHSession()
        banner = session.connect(host, port, username, password=password,
                                 key_path=key_path, cols=cols, rows=rows)
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        output_buffer = deque(maxlen=OUTPUT_BUFFER_LINES)
        output_buffer.extend(banner.splitlines(keepends=True))
        with self._lock:
            self._sessions[session_id] = {
                "session": session,
                "host": host,
                "port": port,
                "username": username,
                "host_id": host_id,
                "created_at": now,
                "last_active": now,
                "output_buffer": output_buffer,
                "ws_connected": False,
                "disconnected_at": None,
                # SFTP 缓存（阶段 3b，web/sftp.py 用）：首次使用时才创建，
                # close_session 里一并关闭。
                "sftp": None,
                "sftp_home": None,
            }
        logger.info("SSH 会话建立：%s -> %s@%s:%s", session_id, username, host, port)
        return session_id

    def get(self, session_id: str):
        """取一条会话记录（含 session 对象），不存在返回 None。"""
        with self._lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> bool:
        """关闭并删除会话，成功返回 True，不存在返回 False。"""
        with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        # 阶段 3b：先关缓存的 SFTPClient（web/sftp.py 在 entry["sftp"] 里存的），
        # 再关 SSH 会话本身；老记录可能没有 "sftp" 键，用 .get 兜底。
        sftp_client = entry.get("sftp")
        if sftp_client is not None:
            try:
                sftp_client.close()
            except Exception:
                pass
        try:
            entry["session"].close()
        except Exception:
            pass
        logger.info("SSH 会话关闭：%s", session_id)
        return True

    def list_sessions(self) -> list:
        """返回会话摘要列表（不含 client/channel 对象）。"""
        with self._lock:
            entries = sorted(self._sessions.items(),
                             key=lambda item: item[1]["created_at"])
            return [{
                "session_id": sid,
                "host": entry["host"],
                "port": entry["port"],
                "username": entry["username"],
                "host_id": entry["host_id"],
                "created_at": entry["created_at"].isoformat(),
                "last_active": entry["last_active"].isoformat(),
                "active": entry["session"].is_active(),
                "ws_connected": entry["ws_connected"],
            } for sid, entry in entries]

    def touch(self, session_id: str) -> None:
        """刷新会话的最后活跃时间。"""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is not None:
                entry["last_active"] = datetime.now(timezone.utc)

    def append_output(self, session_id: str, text: str) -> None:
        """把一段输出按行追加到 output_buffer（deque 自动丢最旧的行）。"""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is not None:
                entry["output_buffer"].extend(text.splitlines(keepends=True))

    def snapshot(self, session_id: str) -> Optional[str]:
        """返回 output_buffer 里的最近输出（供重连回放），会话不存在返回 None。"""
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            return "".join(entry["output_buffer"])

    def cleanup_idle(self) -> int:
        """关掉空闲超时 / WS 断开超宽限期 / SSH 已死无人接管的会话，返回关闭数。"""
        now = datetime.now(timezone.utc)
        victims = []
        with self._lock:
            for sid, entry in self._sessions.items():
                idle_minutes = (now - entry["last_active"]).total_seconds() / 60
                if idle_minutes > IDLE_TIMEOUT_MINUTES:
                    victims.append(sid)
                    continue
                if not entry["ws_connected"] and entry["disconnected_at"] is not None:
                    grace = (now - entry["disconnected_at"]).total_seconds() / 60
                    if grace > SESSION_GRACE_MINUTES:
                        victims.append(sid)
                    elif not entry["session"].is_active():
                        victims.append(sid)  # SSH 已死，等重连也没意义
        for sid in victims:
            self.close_session(sid)
        if victims:
            logger.info("空闲清理关闭 %d 个 SSH 会话", len(victims))
        return len(victims)


# 模块级单例（测试可直接 patch web.ssh.manager）
manager = SSHSessionManager()


# ================= FastAPI 路由 =================

router = APIRouter()


class ConnectRequest(BaseModel):
    """POST /api/ssh/connect 的请求体。

    host_id 与手动填 host 二选一：给 host_id 时从 hosts.json 查主机信息，
    body 里的 host/port/username/key_path 可覆盖查到的值。
    """

    host: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = None
    password: Optional[str] = None  # 只在内存里用，绝不落盘、不记日志
    key_path: Optional[str] = None
    host_id: Optional[str] = None


@router.post("/api/ssh/connect")
def api_ssh_connect(body: ConnectRequest, _session=Depends(require_auth)):
    """新建 SSH 会话，成功返回 {session_id, host, username}。"""
    host, port, username, key_path = body.host, body.port, body.username, body.key_path
    if body.host_id:
        record = get_host(body.host_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"主机不存在：{body.host_id}")
        host = host or record.get("host")
        username = username or record.get("username", "root")
        port = port or record.get("port", 22)
        key_path = key_path or record.get("key_path")
    port = port or 22
    if not host or not username:
        raise HTTPException(status_code=400, detail="host 与 username 必填（或提供 host_id）")
    try:
        session_id = manager.create_session(
            host, port, username, password=body.password,
            key_path=key_path, host_id=body.host_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except paramiko.AuthenticationException:
        raise HTTPException(status_code=401, detail="SSH 认证失败：用户名或密码/密钥不正确")
    except (paramiko.SSHException, OSError, socket.timeout) as exc:
        raise HTTPException(status_code=502, detail=f"SSH 连接失败：{exc}")
    # 日志只记 host/username，绝不记密码
    logger.info("SSH 连接成功：%s@%s:%s -> %s", username, host, port, session_id)
    return {"session_id": session_id, "host": host, "username": username}


@router.get("/api/ssh/sessions")
def api_ssh_list_sessions(_session=Depends(require_auth)):
    """列出当前 SSH 会话摘要。"""
    sessions = manager.list_sessions()
    return {"sessions": sessions, "total": len(sessions),
            "max_sessions": MAX_SESSIONS}


@router.delete("/api/ssh/sessions/{session_id}")
def api_ssh_close_session(session_id: str, _session=Depends(require_auth)):
    """关闭一个 SSH 会话。"""
    if not manager.close_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": session_id}


@router.get("/api/ssh/sessions/{session_id}/snapshot")
def api_ssh_snapshot(session_id: str, _session=Depends(require_auth)):
    """返回会话最近输出（供前端重连时回放）。"""
    output = manager.snapshot(session_id)
    if output is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"session_id": session_id, "output": output}


@router.websocket("/ws/ssh/{session_id}")
async def ssh_websocket(websocket: WebSocket, session_id: str):
    """WebSocket 终端通道（协议见模块 docstring）。"""
    if _ws_auth(websocket) is None:
        await websocket.close(code=4401)  # 未登录
        return
    entry = manager.get(session_id)
    if entry is None:
        await websocket.close(code=4404)  # 会话不存在
        return
    session = entry["session"]
    await websocket.accept()
    entry["ws_connected"] = True
    entry["disconnected_at"] = None
    manager.touch(session_id)

    loop = asyncio.get_running_loop()
    stop_event = threading.Event()

    def _schedule(coro):
        """从输出泵线程往事件循环里投递一个协程（吞掉已关闭时的异常）。"""
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.add_done_callback(
                lambda f: f.exception() if not f.cancelled() else None)
        except RuntimeError:
            pass

    def _pump():
        """后台线程：循环读 channel 输出，写 output_buffer 并推给前端。"""
        try:
            while not stop_event.is_set():
                try:
                    data = session.recv()
                except Exception:
                    break
                if data:
                    text = data.decode("utf-8", errors="replace")
                    manager.append_output(session_id, text)
                    manager.touch(session_id)
                    payload = base64.b64encode(data).decode("ascii")
                    _schedule(websocket.send_json({"type": "output", "data": payload}))
                else:
                    if not session.is_active():
                        break
                    time.sleep(WS_OUTPUT_POLL_SECONDS)
        finally:
            if not stop_event.is_set():
                # channel 死了（不是前端主动断开），通知前端
                _schedule(websocket.send_json(
                    {"type": "closed", "reason": "SSH 会话已断开"}))

    pump_thread = threading.Thread(target=_pump, daemon=True,
                                   name=f"ssh-pump-{session_id[:8]}")
    pump_thread.start()
    try:
        while True:
            try:
                msg = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                break
            mtype = msg.get("type")
            if mtype == "input":
                data = msg.get("data", "")
                if data:
                    try:
                        session.send(data)
                    except Exception as exc:
                        logger.warning("SSH 写入失败：%s", exc)
                        break
                    manager.touch(session_id)
            elif mtype == "resize":
                try:
                    session.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
                except (TypeError, ValueError):
                    pass
            elif mtype == "ping":
                await websocket.send_json({"type": "pong"})
    finally:
        stop_event.set()
        entry["ws_connected"] = False
        entry["disconnected_at"] = datetime.now(timezone.utc)
        # 注意：不断开 SSH 会话，保留 SESSION_GRACE_MINUTES 供前端重连；
        # 空闲清理线程到期后才真正关闭。
