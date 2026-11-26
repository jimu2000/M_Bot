#!/usr/bin/env python3
"""SQLite 后端：Web 面板认证状态的共享存储（多 worker 用）。

背景：面板只跑 1 个 uvicorn 进程时，会话 / 登录锁定 / TG 验证码 /
一键登录 token 都可以放进程内存；开多 worker 后每个 worker 是
独立进程，内存不再共享——登录态会丢、锁定按 worker 计（安全倒
退）、验证码跨 worker 校验失败。所以这 4 类状态统一迁到单个
SQLite 文件（与 tasks.json 同目录），由 SQLite 文件锁做跨进程
并发控制。

文件：web/data/mbot_sessions.db（DATA_DIR 可配，默认见 web.config），
权限 600；WAL 模式（并发读 + 单写者）；每操作一个短连接，用完即
关，不跨线程共享连接对象（check_same_thread=False 只是兜底）。
关键操作（验证码一次性消费、登录锁定计数）用 BEGIN IMMEDIATE
事务，保证“查到就删 / 递增并判定锁定”是原子的，两个 worker
同时操作时后到的那方会等前者提交后再读到最新值，不会都成功。

db_path 的三种形态：
- None 或 ":memory:"：隔离的内存库（shared-cache URI + 一个常驻
  anchor 连接保活；操作仍用各自的短连接，不共享连接对象）。
  单元测试直接 new 一个实例时用，各实例互不干扰。
- 文件路径字符串：共享文件库（跨 worker 测试与生产都用它）。
- 可调用对象：每次操作时调用它拿当前路径（单例用它，路径跟随
  web.config.DATA_DIR 的变化——测试 patch DATA_DIR 后单例自动
  用临时目录里的库，不会污染真实库）。
"""

import os
import sqlite3
import threading
import uuid

from web import config

DB_FILENAME = "mbot_sessions.db"
# 锁等待上限（毫秒）：另一 worker 正写时，最多等 5 秒再报错。
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_guard (
    username TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    lock_level INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS telegram_codes (
    code_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_cooldowns (
    client_key TEXT PRIMARY KEY,
    last_sent REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS magic_tokens (
    token TEXT PRIMARY KEY,
    expires REAL NOT NULL
);
"""


def default_db_path():
    """默认 SQLite 文件：DATA_DIR/mbot_sessions.db。

    每次调用都重新算（单例把它当可调用对象传进来），所以测试
    patch web.config.DATA_DIR 后单例自动跟到临时目录。
    """
    return os.path.join(config.DATA_DIR, DB_FILENAME)


class AuthDB:
    """一个认证状态库（文件或隔离内存库）。

    只负责连接与事务；表结构与 SQL 由 web/auth.py 里各存储类写。
    """

    def __init__(self, db_path=None):
        # db_path: None/":memory:" -> 隔离内存库；str -> 文件库；
        # callable -> 每次操作调用它拿当前文件路径。
        self._db_path = db_path
        self._lock = threading.Lock()
        # 已建过表的文件路径（内存 URI 固定一个）；路径变了（如测试
        # 换 DATA_DIR）要对新文件重新建表。
        self._schema_done = set()
        self._anchor = None
        if db_path is None or db_path == ":memory:":
            # 隔离内存库：shared-cache URI，各操作用自己的短连接；
            # anchor 连接常驻保活（不跑任何 SQL，只是不让库在连接
            # 全关时被销毁）。
            self._memory_uri = (
                f"file:mbot-auth-{uuid.uuid4().hex}?mode=memory"
                "&cache=shared")
            self._anchor = sqlite3.connect(
                self._memory_uri, uri=True, check_same_thread=False)
            self._anchor.execute("PRAGMA busy_timeout = %d"
                                 % _BUSY_TIMEOUT_MS)
        else:
            self._memory_uri = None

    def _resolve_path(self):
        """本次操作用的路径：内存 URI / 固定文件路径 / 可调用对象现算。"""
        if self._memory_uri is not None:
            return self._memory_uri
        if callable(self._db_path):
            return self._db_path()
        return self._db_path

    @staticmethod
    def _is_memory(path):
        return (isinstance(path, str)
                and (path == ":memory:"
                     or path.startswith("file:")
                     and "mode=memory" in path))

    def _ensure_schema(self, conn, path):
        """建表（幂等）；文件库同时把文件权限收成 600。"""
        conn.executescript(_SCHEMA)
        if not self._is_memory(path):
            for suffix in ("", "-wal", "-shm"):
                try:
                    target = path + suffix
                    if os.path.exists(target):
                        mode = os.stat(target).st_mode & 0o777
                        if mode != 0o600:
                            os.chmod(target, 0o600)
                except OSError:
                    # 权限收不拢不影响功能（文件本身仍可用），不抛异常。
                    pass

    def connect(self):
        """开一个短连接：设 busy 超时 + WAL（文件库）+ 建表。

        调用方负责 close()。每次操作都调它，不复用连接。
        """
        path = self._resolve_path()
        with self._lock:
            conn = sqlite3.connect(
                path, uri=self._is_memory(path),
                timeout=_BUSY_TIMEOUT_MS / 1000.0,
                check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout = %d"
                             % _BUSY_TIMEOUT_MS)
                if not self._is_memory(path):
                    # WAL 持久化到文件头，设一次即可；每次设也无害。
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                if path not in self._schema_done:
                    self._ensure_schema(conn, path)
                    self._schema_done.add(path)
            except Exception:
                conn.close()
                raise
            return conn

    def transaction(self, immediate=False):
        """事务上下文：开短连接，BEGIN（写操作用 IMMEDIATE 拿写锁），
        成功 commit，异常 rollback，退出时关连接。

        用法：with db.transaction(immediate=True) as conn: ...
        """
        return _Transaction(self, immediate)

    def close(self):
        """释放内存库的 anchor（文件库无事可做）。"""
        if self._anchor is not None:
            try:
                self._anchor.close()
            except Exception:
                pass
            self._anchor = None


class _Transaction:
    """AuthDB.transaction() 的上下文管理器。"""

    def __init__(self, db, immediate):
        self._db = db
        self._immediate = immediate
        self._conn = None

    def __enter__(self):
        self._conn = self._db.connect()
        try:
            if self._immediate:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute("BEGIN")
        except Exception:
            self._conn.close()
            self._conn = None
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()
            self._conn = None
        return False
