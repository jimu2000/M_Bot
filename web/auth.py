#!/usr/bin/env python3
"""Web 面板认证：密码哈希、登录失败锁定、会话管理。

  - 密码哈希用标准库 hashlib.pbkdf2_hmac（sha256，100000 次，盐 16 字节），
    格式 "pbkdf2_sha256$iterations$salt_hex$hash_hex"，只存哈希不存明文。
  - 登录失败锁定：连续失败达阈值后锁定，锁定时长翻倍增长
    （1 → 2 → 4 → 8 → 16 → 30 分钟封顶）。
  - 会话用 secrets.token_urlsafe 生成 token，存 SQLite（见
    web/auth_store.py），多 worker 共享同一文件；过期自动失效。

命令行用法（生成管理员密码哈希）：
    python3 -m web.auth
按提示输入两次密码后打印哈希，把它设为环境变量 MBOT_WEB_PASSWORD_HASH。
"""

import getpass
import hashlib
import hmac
import secrets
import sys
import time

from web import config
from web.auth_store import AuthDB, default_db_path

# ================= 配置区（改这里） =================
# Password hash algorithm and parameters (pbkdf2_hmac, stdlib implementation, no third-party dependency). 密码哈希算法与参数（pbkdf2_hmac，标准库实现，不依赖第三方包）。
HASH_ALGORITHM = "sha256"
HASH_ITERATIONS = 100000
SALT_BYTES = 16
# Hash string format: pbkdf2_sha256$iterations$salt_hex$hash_hex. 哈希字符串格式：pbkdf2_sha256$iterations$salt_hex$hash_hex。
HASH_PREFIX = "pbkdf2_sha256"
# =====================================================


# ---------------- 密码哈希 ----------------

def hash_password(password, iterations=HASH_ITERATIONS):
    """对明文密码做 pbkdf2 哈希，返回可存入配置的哈希字符串。"""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(HASH_ALGORITHM, password.encode("utf-8"),
                                 salt, iterations)
    return (f"{HASH_PREFIX}${iterations}"
            f"${salt.hex()}${digest.hex()}")


def verify_password(password, stored_hash):
    """验证明文密码是否与存储的哈希匹配。

    stored_hash 格式不对（或为空）时返回 False，不抛异常。
    用 hmac.compare_digest 做恒定时间比较，防时序攻击。
    """
    if not password or not stored_hash:
        return False
    try:
        prefix, iterations, salt_hex, hash_hex = stored_hash.split("$")
        if prefix != HASH_PREFIX:
            return False
        iterations = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(HASH_ALGORITHM, password.encode("utf-8"),
                                 salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------------- 登录失败锁定 ----------------

class LoginGuard:
    """登录失败锁定：SQLite 记失败次数与锁定到期时间（多 worker 共享）。

    规则：同一用户名连续失败达阈值（默认 5 次）后锁定；
    锁定时长从 1 分钟起，每锁定一次翻倍，30 分钟封顶。
    登录成功后失败次数与锁定级别全部清零。
    record_failure() 的“读-递增-判定锁定”在同一个 BEGIN IMMEDIATE
    事务里做，两个 worker 同时记失败时后到的那方读到最新值，
    不会把同一次失败算两次、也不会漏锁。
    """

    def __init__(self, threshold=None, base_minutes=None, max_minutes=None,
                 db_path=None):
        self.threshold = (config.LOCKOUT_THRESHOLD
                          if threshold is None else threshold)
        self.base_minutes = (config.LOCKOUT_BASE_MINUTES
                             if base_minutes is None else base_minutes)
        self.max_minutes = (config.LOCKOUT_MAX_MINUTES
                            if max_minutes is None else max_minutes)
        self._db = AuthDB(db_path)

    def is_locked(self, username):
        """该用户当前是否处于锁定中。"""
        return self.remaining_lock_seconds(username) > 0

    def remaining_lock_seconds(self, username):
        """剩余锁定时长（秒）；未锁定返回 0。"""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT locked_until FROM login_guard WHERE username=?",
                (username,)).fetchone()
        if not row:
            return 0
        return max(0, int(row[0] - time.time()))

    def record_failure(self, username):
        """记一次登录失败；达阈值时锁定（时长按锁定级别翻倍，封顶）。

        原子操作：同一事务里读记录、递增、判定锁定并写回。
        """
        with self._db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT failures, locked_until, lock_level "
                "FROM login_guard WHERE username=?",
                (username,)).fetchone()
            if row is None:
                failures, locked_until, lock_level = 0, 0.0, 0
            else:
                failures, locked_until, lock_level = row
            if locked_until > time.time():
                # While locked, do not count further attempts (so tries during the lockout do not extend it). 已锁定时不再重复记（避免锁定期间的尝试延长锁定时长）。
                return
            failures += 1
            if failures >= self.threshold:
                minutes = min(self.base_minutes * (2 ** lock_level),
                              self.max_minutes)
                locked_until = time.time() + minutes * 60
                lock_level += 1
                failures = 0
            conn.execute(
                "INSERT OR REPLACE INTO login_guard "
                "(username, failures, locked_until, lock_level) "
                "VALUES (?,?,?,?)",
                (username, failures, locked_until, lock_level))

    def record_success(self, username):
        """登录成功：清掉该用户的失败次数与锁定级别。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM login_guard WHERE username=?",
                         (username,))

    def reset(self):
        """清空全部锁定记录（测试用）。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM login_guard")


# ---------------- 会话 ----------------

class SessionStore:
    """登录会话：token -> {username, expires}，存 SQLite（多 worker 共享）。

    token 用 secrets.token_urlsafe 生成；get() 时过期会话自动失效并删除。
    注意：重启后会话仍在（SQLite 持久化），与以前“内存、重启即失效”
    不同——多 worker 下各 worker 启动时清会话会互相误删，所以不
    在启动时清；会话仍按 TTL（默认 12 小时，“记住我” 30 天）过期，
    登出立即失效。见 CHANGELOG v2.11。
    """

    def __init__(self, expire_hours=None, db_path=None):
        hours = (config.SESSION_EXPIRE_HOURS
                 if expire_hours is None else expire_hours)
        self.expire_seconds = hours * 3600
        self._db = AuthDB(db_path)

    def create(self, username, expire_seconds=None):
        """为用户创建一个会话，返回 token。

        expire_seconds 不传时用默认有效期；“记住我”登录时调用方
        传更长的值（cookie 的 max-age 由调用方同步延长）。"""
        token = secrets.token_urlsafe(32)
        ttl = (self.expire_seconds if expire_seconds is None
               else expire_seconds)
        expires = time.time() + ttl
        with self._db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO sessions (token, username, expires) "
                "VALUES (?,?,?)",
                (token, username, expires))
        return token

    def get(self, token):
        """查 token 对应的用户名；不存在或已过期返回 None。

        先读（不拿写锁）；过期时再另起一个写事务删掉该条——
        常见路径（有效会话）不拿写锁，不阻塞其他 worker。
        """
        if not token:
            return None
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT username, expires FROM sessions WHERE token=?",
                (token,)).fetchone()
        if row is None:
            return None
        username, expires = row
        if expires <= time.time():
            with self._db.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM sessions WHERE token=?",
                             (token,))
            return None
        return username

    def revoke(self, token):
        """注销会话（token 不存在时静默跳过）。"""
        if token:
            with self._db.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM sessions WHERE token=?",
                             (token,))

    def reset(self):
        """清空全部会话（测试用）。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM sessions")


# ---------------- Telegram 登录验证码 ----------------
# Telegram code login: a 6-digit code, 5 minutes to expire, single-use; codes are sent with a 60-second per-client-IP cooldown (anti-abuse). Codes are never written to logs. Telegram 验证码登录：6 位数字码，5 分钟过期，一次性；发码按客户端 IP 60 秒冷却（防刷）。验证码绝不写日志。

TELEGRAM_CODE_EXPIRE_SECONDS = 5 * 60
TELEGRAM_CODE_COOLDOWN_SECONDS = 60
# One-click login link lifetime (seconds): same as the code, 5 minutes. 一键登录链接有效期（秒）：与验证码一致，5 分钟。
MAGIC_LOGIN_EXPIRE_SECONDS = 5 * 60


class TelegramCodeStore:
    """Telegram 登录验证码存储（SQLite，多 worker 共享）。

    code_id -> {code, expires}：验证码 5 分钟过期，verify() 无论
    成功失败都删除该条（一次性，防重放与暴力猜测）。
    client_key -> 上次发码时间：同一客户端 60 秒内只能发一次码。
    verify() 的“查-删”在同一个 BEGIN IMMEDIATE 事务里做，两个
    worker 同时验同一条码时后到的那方查不到记录，不会都成功。
    """

    def __init__(self, expire_seconds=TELEGRAM_CODE_EXPIRE_SECONDS,
                 cooldown_seconds=TELEGRAM_CODE_COOLDOWN_SECONDS,
                 db_path=None):
        self.expire_seconds = expire_seconds
        self.cooldown_seconds = cooldown_seconds
        self._db = AuthDB(db_path)

    def can_send(self, client_key):
        """该客户端现在能不能发码；返回 (能/不能, 剩余冷却秒数)。"""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT last_sent FROM telegram_cooldowns "
                "WHERE client_key=?",
                (client_key or "",)).fetchone()
        if row is None:
            return True, 0
        remaining = int(self.cooldown_seconds - (time.time() - row[0]))
        if remaining <= 0:
            return True, 0
        return False, remaining

    def create(self, code=None):
        """生成一条验证码，返回 (code_id, code)。

        code 不传时随机生成 6 位数字；传了则用指定值（测试用）。
        注意：本方法不记发码时间——发送成功后调用方调 mark_sent()
        记冷却（发送失败时不进冷却，用户可立即重试）。
        """
        if code is None:
            code = f"{secrets.randbelow(900000) + 100000:06d}"
        code_id = secrets.token_urlsafe(16)
        expires = time.time() + self.expire_seconds
        with self._db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO telegram_codes (code_id, code, expires) "
                "VALUES (?,?,?)",
                (code_id, code, expires))
        return code_id, code

    def mark_sent(self, client_key):
        """记下该客户端的发码时间（发送成功后调用，进 60 秒冷却）。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO telegram_cooldowns "
                "(client_key, last_sent) VALUES (?,?)",
                (client_key or "", time.time()))

    def verify(self, code_id, code):
        """校验验证码（一次性）：成功返回 True，否则 False。

        code_id 不存在、已过期、验证码不符都返回 False；
        校验后无论结果都删除该条。验证码绝不写日志。
        “查-删”在同一事务里，原子化，防跨 worker 重放。
        """
        if not code_id or not code:
            return False
        with self._db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT code, expires FROM telegram_codes "
                "WHERE code_id=?",
                (code_id,)).fetchone()
            conn.execute("DELETE FROM telegram_codes WHERE code_id=?",
                         (code_id,))
            if row is None:
                return False
            stored_code, expires = row
            if expires <= time.time():
                return False
            return hmac.compare_digest(stored_code, str(code).strip())

    def revoke(self, code_id):
        """作废一条验证码（发送失败时调用，不进冷却）。"""
        if code_id:
            with self._db.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM telegram_codes WHERE code_id=?",
                             (code_id,))

    def reset(self):
        """清空全部验证码与冷却记录（测试用）。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM telegram_codes")
            conn.execute("DELETE FROM telegram_cooldowns")


class MagicLoginStore:
    """Telegram 一键登录链接的 token 存储（SQLite，多 worker 共享）。

    token -> expires：发码时随验证码一起生成，链接形如
    /login?token=...，点开即登录。token 32 字节随机（比 6 位
    数字码强得多），5 分钟过期，consume() 无论成功失败都删除
    该条（一次性，防重放）。token 值绝不写日志。
    consume() 的“查-删”在同一个 BEGIN IMMEDIATE 事务里做，防
    跨 worker 重放。
    """

    def __init__(self, expire_seconds=MAGIC_LOGIN_EXPIRE_SECONDS,
                 db_path=None):
        self.expire_seconds = expire_seconds
        self._db = AuthDB(db_path)

    def create(self):
        """生成一个一键登录 token，返回 token 字符串。"""
        token = secrets.token_urlsafe(32)
        expires = time.time() + self.expire_seconds
        with self._db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO magic_tokens (token, expires) VALUES (?,?)",
                (token, expires))
        return token

    def consume(self, token):
        """消费一个 token：存在且未过期返回 True，否则 False。

        无论结果都删除该条（一次性）；空 token 直接返回 False。
        """
        if not token:
            return False
        with self._db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT expires FROM magic_tokens WHERE token=?",
                (token,)).fetchone()
            conn.execute("DELETE FROM magic_tokens WHERE token=?",
                         (token,))
            if row is None:
                return False
            return row[0] > time.time()

    def revoke(self, token):
        """作废一个 token（发码失败时调用，与验证码一起作废）。"""
        if token:
            with self._db.transaction(immediate=True) as conn:
                conn.execute("DELETE FROM magic_tokens WHERE token=?",
                             (token,))

    def reset(self):
        """清空全部 token（测试用）。"""
        with self._db.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM magic_tokens")


# Global singleton (shared across workers via the SQLite file; the path is resolved lazily from DATA_DIR on every operation, so tests patching DATA_DIR get an isolated temp file). 全局单例（经 SQLite 文件跨 worker 共享；路径每次操作都从 DATA_DIR 现算，测试 patch DATA_DIR 后单例自动用临时目录里的库）。
LOGIN_GUARD = LoginGuard(db_path=default_db_path)
SESSIONS = SessionStore(db_path=default_db_path)
TELEGRAM_CODES = TelegramCodeStore(db_path=default_db_path)
MAGIC_LOGIN_TOKENS = MagicLoginStore(db_path=default_db_path)


# ---------------- 命令行：生成密码哈希 ----------------

def _main():
    """`python3 -m web.auth`：交互生成管理员密码哈希。"""
    print("生成 Web 面板管理员密码哈希（哈希只显示一次，请妥善保存）")
    password = getpass.getpass("输入新密码：")
    confirm = getpass.getpass("再输入一次：")
    if not password:
        print("密码不能为空。")
        sys.exit(1)
    if password != confirm:
        print("两次密码不一致，请重新运行。")
        sys.exit(1)
    print()
    print(hash_password(password))
    print()
    print("把上面这行设为环境变量 MBOT_WEB_PASSWORD_HASH 后再启动面板，"
          "例如：")
    print("  export MBOT_WEB_PASSWORD_HASH='<上面那行>'")


if __name__ == "__main__":
    _main()
