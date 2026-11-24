#!/usr/bin/env python3
"""web/auth.py 的单元测试：密码哈希、登录锁定、会话管理。

运行：python3 -m unittest tests.test_web_auth -v
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.auth import (
    HASH_ITERATIONS,
    LoginGuard,
    SessionStore,
    hash_password,
    verify_password,
)


class TestHashPassword(unittest.TestCase):
    """密码哈希 / 验证。"""

    def test_roundtrip(self):
        """哈希后用原密码能验证通过。"""
        hashed = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", hashed))

    def test_wrong_password_fails(self):
        """错误密码验证失败。"""
        hashed = hash_password("correct-password")
        self.assertFalse(verify_password("wrong-password", hashed))
        self.assertFalse(verify_password("", hashed))
        self.assertFalse(verify_password("correct-passwor", hashed))

    def test_hash_format(self):
        """哈希格式为 pbkdf2_sha256$iterations$salt_hex$hash_hex。"""
        hashed = hash_password("pw")
        parts = hashed.split("$")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "pbkdf2_sha256")
        self.assertEqual(int(parts[1]), HASH_ITERATIONS)
        self.assertEqual(len(parts[2]), 32)   # 16 字节盐的 hex
        self.assertEqual(len(parts[3]), 64)   # sha256 的 hex

    def test_same_password_different_hash(self):
        """同一密码每次哈希结果不同（随机盐）。"""
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_malformed_hash_fails(self):
        """格式不对的存储哈希验证失败，不抛异常。"""
        self.assertFalse(verify_password("pw", ""))
        self.assertFalse(verify_password("pw", "not-a-hash"))
        self.assertFalse(verify_password("pw", "pbkdf2_sha256$100000$zz"))
        self.assertFalse(verify_password("pw", "bcrypt$100000$aa$bb"))
        self.assertFalse(verify_password("pw", "pbkdf2_sha256$abc$aa$bb"))
        self.assertFalse(verify_password("", hash_password("pw")))


class TestLoginGuard(unittest.TestCase):
    """登录失败锁定。"""

    def setUp(self):
        self.guard = LoginGuard(threshold=5, base_minutes=1, max_minutes=30)

    def _fail_times(self, username, n):
        for _ in range(n):
            self.guard.record_failure(username)

    def _expire_lock(self, username):
        """把锁定到期时间改到过去（模拟锁定期已过）。"""
        self.guard._records[username]["locked_until"] = time.time() - 1

    def test_not_locked_initially(self):
        self.assertFalse(self.guard.is_locked("admin"))
        self.assertEqual(self.guard.remaining_lock_seconds("admin"), 0)

    def test_few_failures_no_lock(self):
        """4 次失败不锁定。"""
        self._fail_times("admin", 4)
        self.assertFalse(self.guard.is_locked("admin"))

    def test_five_failures_locks(self):
        """5 次失败锁定，剩余约 60 秒。"""
        self._fail_times("admin", 5)
        self.assertTrue(self.guard.is_locked("admin"))
        remaining = self.guard.remaining_lock_seconds("admin")
        self.assertGreater(remaining, 55)
        self.assertLessEqual(remaining, 60)

    def test_lock_duration_doubles(self):
        """锁定时长翻倍增长：1 → 2 → 4 → 8 → 16 分钟。"""
        for expected_minutes in (1, 2, 4, 8, 16):
            self._fail_times("admin", 5)
            self.assertTrue(self.guard.is_locked("admin"))
            remaining = self.guard.remaining_lock_seconds("admin")
            self.assertGreater(remaining, expected_minutes * 60 - 5)
            self.assertLessEqual(remaining, expected_minutes * 60)
            self._expire_lock("admin")

    def test_lock_capped_at_max(self):
        """锁定时长封顶 30 分钟（第 6 次及以后不再翻倍）。"""
        for _ in range(5):  # 1/2/4/8/16 分钟各锁一次
            self._fail_times("admin", 5)
            self._expire_lock("admin")
        for _ in range(2):  # 第 6、7 次锁定都应是 30 分钟
            self._fail_times("admin", 5)
            remaining = self.guard.remaining_lock_seconds("admin")
            self.assertGreater(remaining, 30 * 60 - 5)
            self.assertLessEqual(remaining, 30 * 60)
            self._expire_lock("admin")

    def test_failures_during_lock_do_not_extend(self):
        """锁定期间的失败不延长锁定时长。"""
        self._fail_times("admin", 5)
        first_remaining = self.guard.remaining_lock_seconds("admin")
        self.guard.record_failure("admin")
        self.guard.record_failure("admin")
        # 剩余时间只应自然减少（误差 1 秒），不应被重置回 60 秒。
        self.assertLessEqual(self.guard.remaining_lock_seconds("admin"),
                             first_remaining)

    def test_success_resets_failures(self):
        """登录成功清零失败次数（之后要重新攒满 5 次才锁定）。"""
        self._fail_times("admin", 4)
        self.guard.record_success("admin")
        self._fail_times("admin", 4)
        self.assertFalse(self.guard.is_locked("admin"))

    def test_success_resets_lock_level(self):
        """锁定期过后登录成功，锁定级别清零（下次锁定回到 1 分钟）。"""
        self._fail_times("admin", 5)   # 锁 1 分钟
        self._expire_lock("admin")
        self.guard.record_success("admin")
        self._fail_times("admin", 5)   # 应再次锁 1 分钟，而不是 2 分钟
        remaining = self.guard.remaining_lock_seconds("admin")
        self.assertGreater(remaining, 55)
        self.assertLessEqual(remaining, 60)

    def test_users_are_independent(self):
        """不同用户的锁定互不影响。"""
        self._fail_times("alice", 5)
        self.assertTrue(self.guard.is_locked("alice"))
        self.assertFalse(self.guard.is_locked("bob"))


class TestSessionStore(unittest.TestCase):
    """会话创建 / 获取 / 过期 / 注销。"""

    def setUp(self):
        self.store = SessionStore(expire_hours=12)

    def test_create_and_get(self):
        token = self.store.create("admin")
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 20)
        self.assertEqual(self.store.get(token), "admin")

    def test_unknown_token_returns_none(self):
        self.assertIsNone(self.store.get("no-such-token"))
        self.assertIsNone(self.store.get(""))
        self.assertIsNone(self.store.get(None))

    def test_revoke(self):
        token = self.store.create("admin")
        self.store.revoke(token)
        self.assertIsNone(self.store.get(token))

    def test_revoke_unknown_is_silent(self):
        self.store.revoke("no-such-token")  # 不抛异常

    def test_expired_session_invalid(self):
        """过期会话自动失效（直接改过期时间模拟）。"""
        token = self.store.create("admin")
        self.store._sessions[token]["expires"] = time.time() - 1
        self.assertIsNone(self.store.get(token))
        # 过期会话已从字典里删除。
        self.assertNotIn(token, self.store._sessions)

    def test_short_expiry_really_expires(self):
        """真实短有效期：1 秒后会话失效。"""
        store = SessionStore(expire_hours=1 / 3600)  # 1 秒
        token = store.create("admin")
        self.assertEqual(store.get(token), "admin")
        time.sleep(1.2)
        self.assertIsNone(store.get(token))

    def test_tokens_are_unique(self):
        tokens = {self.store.create("admin") for _ in range(10)}
        self.assertEqual(len(tokens), 10)

    def test_create_with_custom_expiry(self):
        """create() 可传自定义有效期（记住我登录用 30 天）。"""
        token = self.store.create("admin", expire_seconds=30 * 24 * 3600)
        remaining = self.store._sessions[token]["expires"] - time.time()
        self.assertGreater(remaining, 29 * 24 * 3600)
        self.assertLessEqual(remaining, 30 * 24 * 3600)
        self.assertEqual(self.store.get(token), "admin")


if __name__ == "__main__":
    unittest.main()
