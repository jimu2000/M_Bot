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
import re
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
SSH_CONNECT_TIMEOUT = 10  # SSH connection timeout (seconds). SSH 连接超时（秒）。
SSH_BANNER_TIMEOUT = 10  # Welcome-banner command execution timeout (seconds). 欢迎横幅命令执行超时（秒）。
OUTPUT_BUFFER_LINES = 1000  # Recent output lines kept per session (for reconnect replay snapshots). 每个会话保留的最近输出行数（供重连回放快照）。
MAX_SESSIONS = 20  # Max concurrent SSH sessions (20 max). 最大并发 SSH 会话数（最大并发 20）。
IDLE_TIMEOUT_MINUTES = 30  # Session idle timeout (minutes); idle sessions are closed automatically. 会话空闲超时（分钟），超时自动关闭。
SESSION_GRACE_MINUTES = 5  # How long a session is kept after its WebSocket disconnects (minutes), for reconnects. WebSocket 断开后会话保留时长（分钟），供重连。
CLEANUP_INTERVAL_SECONDS = 60  # Idle-cleanup background thread check interval (seconds). 空闲清理后台线程的检查间隔（秒）。
WS_OUTPUT_POLL_SECONDS = 0.02  # Output pump polling interval for the channel (seconds). 输出泵轮询 channel 的间隔（秒）。
INSTANCE_SSH_PORTS = (14422, 22)  # Instance one-click login tries these ports in order: 14422 (DD / hardening script) first, then 22 (OCI default). 实例一键登录按顺序试这些端口：先 14422（DD / 加固脚本改的端口），再 22（OCI 默认）。

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"  # Host keys remembered by AutoAddPolicy. AutoAddPolicy 记住的 host key。
HOSTS_FILE = DATA_DIR / "hosts.json"  # Saved host list (for host_id lookups). 已保存主机列表（host_id 查询用）。
SESSION_COOKIE = "mbot_session"  # Login session cookie name. 登录会话 cookie 名。

try:
    # Config shared with other panel modules: hosts.json lives under web.config.DATA_DIR, and the login cookie name is also defined in web.config. 与面板其他模块共用配置：hosts.json 在 web.config.DATA_DIR 下，登录 cookie 名也在 web.config 里定义。
    from web import config as _web_config

    DATA_DIR = Path(_web_config.DATA_DIR)
    KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"
    HOSTS_FILE = DATA_DIR / "hosts.json"
    SESSION_COOKIE = _web_config.SESSION_COOKIE
except Exception:
    pass  # Fall back to the defaults above when web.config is unavailable. web.config 不可用时用上面的默认值。
# ================================================================

# ASCII art in the welcome banner. 欢迎横幅里的 ASCII 艺术字。
M_BOT_ART = r"""
 __  __           ____     ___    _____
|  \/  |         | __ )   / _ \  |_   _|
| |\/| |         |  _ \  | | | |   | |
| |  | |         | |_) | | |_| |   | |
|_|  |_|  _____  |____/   \___/    |_|
"""

# System-info commands run in the welcome banner. 欢迎横幅里跑的系统信息命令。
BANNER_COMMAND = "uname -a; uptime; df -h /; free -h"


def build_banner(sysinfo: str) -> str:
    """拼欢迎横幅：ASCII 艺术字 + 系统信息（uname 黄、uptime 绿）。"""
    lines = [""]
    lines.append("\x1b[1;36m" + M_BOT_ART.strip("\n") + "\x1b[0m")
    lines.append("\x1b[1;35mM_Bot · Web SSH\x1b[0m")
    lines.append("")
    info_lines = [line for line in sysinfo.splitlines() if line.strip()]
    if info_lines:
        lines.append("\x1b[1;33m" + info_lines[0] + "\x1b[0m")
        if len(info_lines) > 1:
            lines.append("\x1b[1;32m" + info_lines[1] + "\x1b[0m")
        lines.extend(info_lines[2:])
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


# Module-level singleton (tests can patch web.ssh.session_store directly). 模块级单例（测试可直接 patch web.ssh.session_store）。
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
        # Console sessions hold a proxy Transport (the direct-tcpip forwarding channel lives on it; closing it kills the session). 控制台会话的代理 Transport（转发通道挂在它上面，关了会话就断）。
        self.proxy_transport = None
        self.banner = ""
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
            # AutoAddPolicy: automatically remember the host key on first connect, instead of RejectPolicy. AutoAddPolicy：首次连接自动记住 host key，不用 RejectPolicy。
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
            # First run the system-info commands via exec_command to get the banner content, then open the interactive shell. 先用 exec_command 跑系统信息命令拿横幅内容，再开交互 shell。
            # The same SSHClient can open multiple channels; the two do not conflict. 同一个 SSHClient 可以开多个 channel，两者不冲突。
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

    def connect_console(self, host: str, port: int, connection_ocid: str,
                        instance_id: str, key_path: Optional[str] = None,
                        cols: int = 80, rows: int = 24) -> str:
        """串行控制台两阶段直连，返回欢迎横幅文本。

        OCI 控制台端点（instance-console.<region>.oci.oraclecloud.com:443）
        不接受直接的 "session" channel（会报 ChannelException(3,
        'Unknown channel type')）；正确的接法与 ssh 连接命令的
        ProxyCommand 等价，分两阶段：
          1. 内层 Transport 连控制台端点（用户名是连接 OCID，注册的
             密钥认证），在其上开一个 "direct-tcpip" 转发通道，目的地
             是 (instance_id, 22)；
          2. 外层 SSHClient 以该通道作 socket（sock=channel），再正常
             认证并开交互 shell——shell 通道开在外层 Transport 上，控制
             台服务接受。
        内层 Transport 必须随会话一直活着（关了转发通道就断），存在
        self.proxy_transport 里，close() 时一并关闭。系统信息横幅不取
        （串口控制台不是 shell，exec 通道不适用），用固定横幅。
        """
        pkey = _load_console_private_key(key_path)
        if pkey is None:
            raise paramiko.AuthenticationException(
                "串行控制台密钥未找到：服务端没有注册连接时那把密钥对应"
                "的私钥（MBOT_CONSOLE_SSH_KEY_FILE 或 ~/.ssh/id_ed25519），"
                "请检查后重试")
        proxy = paramiko.Transport((host, port))
        client = None
        try:
            proxy.start_client(timeout=SSH_CONNECT_TIMEOUT)
            proxy.auth_publickey(connection_ocid, pkey)
            # direct-tcpip forwarding channel to the instance's serial console. 转发通道：目的地是实例 OCID:22（控制台服务据此把流量接到该实例的串口）。
            forward = proxy.open_channel(
                "direct-tcpip", (instance_id, 22), ("127.0.0.1", 0))
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(instance_id, port=22, username="root", pkey=pkey,
                           sock=forward, timeout=SSH_CONNECT_TIMEOUT,
                           banner_timeout=SSH_BANNER_TIMEOUT,
                           look_for_keys=False, allow_agent=False)
            channel = client.invoke_shell(width=cols, height=rows)
            self.client = client
            self.channel = channel
            self.proxy_transport = proxy
            self._active = True
            self.banner = ("已连接到实例串行控制台\n"
                           "（实例的串口输出将显示在这里，登录提示出现后"
                           "输入 root 及密码登录）")
            return self.banner
        except Exception:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            try:
                proxy.close()
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
                stdout.channel.recv_exit_status()
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
        # Console sessions also hold a proxy transport (the forwarding channel dies with it). 控制台会话还持有一个代理 Transport（它断了转发通道就断）。
        if self.proxy_transport is not None \
                and not self.proxy_transport.is_active():
            return False
        return True

    def close(self) -> None:
        """关 channel 与 client（控制台会话还关代理 Transport）。"""
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
        if self.proxy_transport is not None:
            try:
                self.proxy_transport.close()
            except Exception:
                pass
            self.proxy_transport = None


# ================= 会话管理器 =================

class SSHSessionManager:
    """线程安全的 SSH 会话字典 + 空闲清理后台线程。

    每条记录：{session, host, port, username, host_id, kind, created_at,
    last_active, output_buffer(deque), ws_connected, disconnected_at}
    （kind 是 "ssh" 或 "console"，见 create_session /
    create_console_session）。
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
                # Session kind: "ssh" (regular host login) or "console"
                # (OCI serial console). The console dedupe in
                # create_console_session only ever reuses kind == "console".
                # 会话类型：ssh（常规主机登录）或 console（OCI 串行控制台）；
                # create_console_session 的复用只认 console 类型。
                "kind": "ssh",
                "created_at": now,
                "last_active": now,
                "output_buffer": output_buffer,
                "ws_connected": False,
                "disconnected_at": None,
                # SFTP cache (used by web/sftp.py): created only on first use, and closed together with the session in close_session. SFTP 缓存（web/sftp.py 用）：首次使用时才创建，close_session 里一并关闭。
                "sftp": None,
                "sftp_home": None,
            }
        logger.info("SSH 会话建立：%s -> %s@%s:%s", session_id, username, host, port)
        return session_id

    def create_console_session(self, host: str, port: int,
                               connection_ocid: str, instance_id: str,
                               key_path: Optional[str] = None,
                               cols: int = 80, rows: int = 24) -> str:
        """新建串行控制台会话（两阶段直连，见 SSHSession.connect_console），
        返回 session_id（uuid4）。

        同一实例幂等：OCI 串行控制台每个连接只允许一个 SSH 客户端，
        第二个客户端连上来会把第一个踢掉（控制台端点报 "Closing
        connection. Another ssh client has connected."），所以先查
        有没有该实例还活着的控制台会话，有就直接复用它的 session_id，
        不再新开 SSH 连接。检查 + 拨号 + 入库全程持锁，两个并发的
        POST 也不可能同时拨号。已死（SSH 断了）的同实例控制台会话
        先摘掉再重建。超过 MAX_SESSIONS 抛 RuntimeError；连接失败抛
        paramiko 异常。会话记录里的 host 是控制台端点，username 是
        连接 OCID，host_id 是实例 OCID，kind 是 "console"。
        """
        with self._lock:
            # Reuse a live console session for this instance (OCI allows
            # only one SSH client per console connection; a second dial
            # would kick the first). Dead same-instance console sessions
            # are dropped so the dial below replaces them. The lock is
            # held across check + dial + insert so two concurrent POSTs
            # cannot both dial. 复用该实例还活着的控制台会话（OCI 每个控
            # 制台连接只允许一个 SSH 客户端，第二个拨号会把第一个踢掉）；
            # 已死的同实例会话先摘掉；全程持锁，两个并发 POST 也不会同时拨号。
            for sid, entry in list(self._sessions.items()):
                if entry.get("kind") != "console":
                    continue
                if entry.get("host_id") != instance_id:
                    continue
                if entry["session"].is_active():
                    entry["last_active"] = datetime.now(timezone.utc)
                    logger.info("串行控制台会话已存在，复用：%s（实例 %s）",
                                sid, instance_id)
                    return sid
                # Dead console session for this instance: drop it (close
                # the SSH objects here; close() only touches paramiko
                # objects and never takes the manager lock). 已死的同实例
                # 控制台会话：摘掉（close 只碰 paramiko 对象，不拿管理器锁）。
                self._sessions.pop(sid, None)
                try:
                    entry["session"].close()
                except Exception:
                    pass
            if len(self._sessions) >= MAX_SESSIONS:
                raise RuntimeError(f"SSH 会话已满（{MAX_SESSIONS} 个），请先关闭一些")
            session = SSHSession()
            banner = session.connect_console(host, port, connection_ocid,
                                             instance_id, key_path=key_path,
                                             cols=cols, rows=rows)
            session_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            output_buffer = deque(maxlen=OUTPUT_BUFFER_LINES)
            output_buffer.extend(banner.splitlines(keepends=True))
            self._sessions[session_id] = {
                "session": session,
                "host": host,
                "port": port,
                "username": connection_ocid,
                "host_id": instance_id,
                "kind": "console",
                "created_at": now,
                "last_active": now,
                "output_buffer": output_buffer,
                "ws_connected": False,
                "disconnected_at": None,
                "sftp": None,
                "sftp_home": None,
            }
            logger.info("串行控制台会话建立：%s -> %s@%s:%s（实例 %s）",
                        session_id, connection_ocid, host, port, instance_id)
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
        # Close the cached SFTPClient first (stored by web/sftp.py in entry["sftp"]), then the SSH session itself; old records may lack the "sftp" key, so use .get as a fallback. 先关缓存的 SFTPClient（web/sftp.py 在 entry["sftp"] 里存的），再关 SSH 会话本身；老记录可能没有 "sftp" 键，用 .get 兜底。
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
                # "ssh" or "console"; old records (and test stubs) may lack
                # it, default to "ssh". 新老记录都可能没有 kind，缺省按 ssh。
                "kind": entry.get("kind", "ssh"),
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
                        victims.append(sid)  # The SSH connection is dead; waiting for a reconnect is pointless. SSH 已死，等重连也没意义。
        for sid in victims:
            self.close_session(sid)
        if victims:
            logger.info("空闲清理关闭 %d 个 SSH 会话", len(victims))
        return len(victims)


# Module-level singleton (tests can patch web.ssh.manager directly). 模块级单例（测试可直接 patch web.ssh.manager）。
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
    password: Optional[str] = None  # In-memory only; never written to disk or logs. 只在内存里用，绝不落盘、不记日志。
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
    # Logs record only host/username, never passwords. 日志只记 host/username，绝不记密码。
    logger.info("SSH 连接成功：%s@%s:%s -> %s", username, host, port, session_id)
    return {"session_id": session_id, "host": host, "username": username}


class ConnectInstanceRequest(BaseModel):
    """POST /api/ssh/connect-instance 的请求体：实例卡片 SSH 一键直连。"""

    instance_id: str
    profile: Optional[str] = None
    region: Optional[str] = None


@router.post("/api/ssh/connect-instance")
def api_ssh_connect_instance(body: ConnectInstanceRequest,
                             _session=Depends(require_auth)):
    """实例卡片 SSH 一键直连：查实例公网 IP，用 root 密码自动登录 root。

    密码优先级：环境变量固定密码（MBOT_FIXED_ROOT_PASSWORD）优先，
    没设时回落到开机时按实例存进凭据库的那份。密码只在服务端内存里
    用，绝不返回给前端、不记日志。两种密码都没有、实例无公网 IP、
    或自动登录失败时返回明确错误，前端回落到手动连接表单（预填主机）。
    端口先试 14422（DD / 加固脚本改的端口），再试 22（OCI 默认）。
    """
    password = os.environ.get("MBOT_FIXED_ROOT_PASSWORD", "").strip()
    if not password:
        # Fall back to the per-instance password saved at creation time. 没设固定密码时，回落到开机时按实例存的那份。
        try:
            from tgbot.instance_credentials import get_root_password
            password = get_root_password(body.instance_id) or ""
        except Exception:
            password = ""
    if not password:
        raise HTTPException(
            status_code=400,
            detail="未设置固定 root 密码（MBOT_FIXED_ROOT_PASSWORD），"
                   "且该实例没有保存的 root 密码，无法自动登录；"
                   "请在连接表单里手动输入密码")
    try:
        # Lazy imports: web.deps pulls in oci + tgbot config; importing at
        # module top would risk a cycle with the app wiring. 延迟导入：
        # web.deps 会拉 oci 与 tgbot 配置，模块顶层导入可能与应用装配循环。
        from web.deps import resolve_profile
        from oci.network import get_public_ip
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"服务端依赖不可用：{exc}")
    try:
        cli, _compartment_id = resolve_profile(body.profile, body.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        host = get_public_ip(cli, body.instance_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"查实例公网 IP 失败：{exc}")
    if not host:
        raise HTTPException(status_code=502, detail="实例暂无公网 IP，无法直连")
    auth_failed = False
    last_error = None
    for port in INSTANCE_SSH_PORTS:
        try:
            session_id = manager.create_session(
                host, port, "root", password=password)
        except paramiko.AuthenticationException:
            auth_failed = True
            last_error = "SSH 认证失败：root 密码不正确"
            continue
        except (paramiko.SSHException, OSError, socket.timeout) as exc:
            last_error = f"SSH 连接失败：{exc}"
            continue
        logger.info("实例 SSH 直连成功：root@%s:%s -> %s", host, port, session_id)
        return {"session_id": session_id, "host": host,
                "username": "root", "port": port}
    if auth_failed:
        raise HTTPException(status_code=401, detail=last_error or "SSH 认证失败")
    raise HTTPException(status_code=502, detail=last_error or "SSH 连接失败")


# OCI 串行控制台的连接串（oci/instance.py 的 get_serial_console 返回的
# connection-string），实测格式（见 docs/rescue-feature-v2.md）：
#   ssh -o ProxyCommand='ssh -W %h:%p -p 443 <connection-ocid>@instance-console.<region>.oci.oraclecloud.com' <instance-ocid>
# 外层 ssh 的目标是实例 OCID（只作 %h 传给 ProxyCommand），真正要连的
# 是 ProxyCommand 里的控制台主机：用户名是连接 OCID，端口 443，密钥用
# 注册连接时那把 RSA 公钥对应的私钥。
# OCI serial console connection strings (the connection-string returned by
# oci/instance.py get_serial_console), verified format (see
# docs/rescue-feature-v2.md): the outer ssh target is the instance OCID
# (only passed as %h to the ProxyCommand); the real endpoint is the
# console host inside the ProxyCommand, username = connection OCID,
# port 443, keyed by the private key matching the registered RSA key.
_CONSOLE_HOST_RE = re.compile(
    r"^instance-console\.[a-z0-9-]+\.oci\.oraclecloud\.com$")
_CONSOLE_USER_HOST_RE = re.compile(r"(\S+)@(\S+)")
# Instance OCID in the outer ssh target of a console connection string
# (the direct-tcpip destination of the forwarding channel). 控制台连接
# 命令中外层 ssh 目标里的实例 OCID（转发通道的 direct-tcpip 目的地）。
# Real OCIDs carry a region (ocid1.instance.oc1.ap-singapore-1.<id>);
# test fixtures use the region-less form (ocid1.instance.oc1..<id>).
# Both must match: the old regex only matched the region-less form, so
# direct-connect rejected every real connection string with a 400.
_INSTANCE_OCID_RE = re.compile(
    r"ocid1\.instance\.oc1\.\.?(?:[a-z0-9-]+\.)?[a-z0-9]+")


def parse_console_connection_string(connection_string):
    """解析串行控制台连接串，返回 (host, port, username, instance_id)。

    先认 ProxyCommand 形式（OCI 实测格式），再认简单形式
    ssh [-i key] [-p port] user@host；host 必须是 OCI 控制台主机
    （instance-console.<region>.oci.oraclecloud.com），否则报
    ValueError（本接口只连 OCI 控制台，任意主机走手动连接表单）。
    instance_id 是外层 ssh 的目标（实例 OCID），直连时作
    direct-tcpip 通道的目的地；命令里没有实例 OCID（如只有内层
    ssh 的简单形式）时报 ValueError——没有它无法转发到串口。
    """
    text = (connection_string or "").strip()
    if not text:
        raise ValueError("串行控制台连接命令为空")
    # ProxyCommand 形式：取引号里的内层 ssh 命令。
    proxy = re.search(r"ProxyCommand='([^']+)'", text)
    inner = proxy.group(1) if proxy else text
    port_match = re.search(r"-p\s+(\d+)", inner)
    port = int(port_match.group(1)) if port_match else 443
    user_host = _CONSOLE_USER_HOST_RE.search(inner)
    if not user_host:
        raise ValueError(f"无法解析串行控制台连接命令：{text[:80]}")
    username, host = user_host.group(1), user_host.group(2)
    if not _CONSOLE_HOST_RE.match(host):
        raise ValueError(f"不是 OCI 串行控制台主机：{host}")
    instance_match = _INSTANCE_OCID_RE.search(text)
    if not instance_match:
        raise ValueError(
            "串行控制台连接命令里没有实例 OCID，无法直连；请用控制台"
            "弹窗里新获取的连接命令重试")
    return host, port, username, instance_match.group(0)


def _console_private_key_path():
    """串行控制台登录用的私钥路径（服务端文件）。

    注册连接时用的公钥由环境变量 MBOT_CONSOLE_SSH_KEY_FILE 指定，
    没设时走 ~/.ssh/id_ed25519.pub（与 oci/instance.py 的
    get_serial_console 同一套解析）；私钥是公钥路径去掉 .pub 后缀。
    文件不存在时返回 None——此时 paramiko 走默认查找（ssh-agent 与
    ~/.ssh/id_rsa、id_ed25519 等），与直接跑连接命令的行为一致。
    """
    pub = os.environ.get("MBOT_CONSOLE_SSH_KEY_FILE") or \
        os.path.expanduser("~/.ssh/id_ed25519.pub")
    private = pub[:-4] if pub.endswith(".pub") else pub
    return private if os.path.isfile(private) else None


def _load_console_private_key(key_path=None):
    """加载串行控制台登录用的私钥，返回 paramiko PKey 对象，找不到返回 None。

    依次试：显式 key_path（或 _console_private_key_path 解析出的那把）、
    ~/.ssh/id_ed25519、~/.ssh/id_rsa，最后试 ssh-agent 里的密钥。
    加密私钥（无口令解不开）跳过。供两阶段直连用：内层 Transport 与
    外层 SSHClient 都要拿这个 PKey 做认证（注册连接时用的就是这把）。
    """
    candidates = []
    resolved = key_path or _console_private_key_path()
    if resolved:
        candidates.append(resolved)
    for default in ("~/.ssh/id_ed25519", "~/.ssh/id_rsa"):
        expanded = os.path.expanduser(default)
        if expanded not in candidates:
            candidates.append(expanded)
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        for key_cls in (paramiko.RSAKey, paramiko.Ed25519Key,
                        paramiko.ECDSAKey):
            try:
                return key_cls.from_private_key_file(candidate)
            except paramiko.SSHException:
                continue  # 不是这种密钥类型，换下一种试。
            except (OSError, ValueError):
                break  # 文件读不了，换下一个文件。
    try:
        agent_keys = paramiko.Agent().get_keys()
    except Exception:
        agent_keys = []
    return agent_keys[0] if agent_keys else None


class ConnectConsoleRequest(BaseModel):
    """POST /api/ssh/connect-console 的请求体。

    connection_string 是串行控制台弹窗里显示的那条连接命令（服务端
    据此解析出控制台主机/端口/连接 OCID 与实例 OCID 并两阶段直连）；
    label 是终端标签。
    """

    connection_string: str
    label: Optional[str] = None


@router.post("/api/ssh/connect-console")
def api_ssh_connect_console(body: ConnectConsoleRequest,
                            _session=Depends(require_auth)):
    """串行控制台直连：两阶段连接（见 SSHSession.connect_console）。

    救机入口（实例自身 SSH 不通时用）：先以内层 Transport 连 OCI 串行
    控制台主机（443 端口，用户名是连接 OCID，密钥认证），在其上开
    direct-tcpip 转发通道到实例 OCID:22，再以外层 SSHClient 经该通道
    开交互 shell，返回 session_id，前端据此在终端页开标签。密钥只在
    服务端用，绝不返回给前端、不记日志。解析失败报 400；密钥认证失败
    报 401；连不上报 502。
    """
    try:
        host, port, username, instance_id = \
            parse_console_connection_string(body.connection_string)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        session_id = manager.create_console_session(
            host, port, username, instance_id,
            key_path=_console_private_key_path())
    except paramiko.AuthenticationException:
        raise HTTPException(
            status_code=401,
            detail="串行控制台认证失败：服务端没有注册连接时那把 RSA 密钥"
                   "对应的私钥（MBOT_CONSOLE_SSH_KEY_FILE 或 ~/.ssh/"
                   "id_ed25519），请检查后重试")
    except (paramiko.SSHException, OSError, socket.timeout) as exc:
        raise HTTPException(status_code=502, detail=f"串行控制台连接失败：{exc}")
    logger.info("串行控制台直连成功：%s@%s:%s（实例 %s）-> %s",
                username, host, port, instance_id, session_id)
    return {"session_id": session_id, "host": host,
            "username": username, "port": port,
            "instance_id": instance_id}


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
        await websocket.close(code=4401)
        return
    entry = manager.get(session_id)
    if entry is None:
        await websocket.close(code=4404)
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
                # The channel died (not a client-initiated disconnect); notify the frontend. channel 死了（不是前端主动断开），通知前端。
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
        # Note: do not close the SSH session; keep it for SESSION_GRACE_MINUTES so the frontend can reconnect, and let the idle-cleanup thread close it for real when it expires. 注意：不断开 SSH 会话，保留 SESSION_GRACE_MINUTES 供前端重连；空闲清理线程到期后才真正关闭。
