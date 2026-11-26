#!/usr/bin/env python3
"""web/auth.py 四个存储迁 SQLite 后的跨 worker 测试。

核心场景：两个独立的 store 实例连同一个 SQLite 文件（模拟两个
uvicorn worker），A 写的会话 / 锁定 / 验证码，B 必须能读到；
A 登出 / 消费后，B 读不到。验证码与一键登录 token 的一次性消费
用 BEGIN IMMEDIATE 事务保证原子性，并发验同一条码时只能一人成功。

运行：python3 -m unittest tests.test_web_auth_sqlite -v
"""

import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest

from web.auth import (
    LoginGuard,
    MagicLoginStore,
    SessionStore,
    TelegramCodeStore,
)


def _db_rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


class _TempDBTestCase(unittest.TestCase):
    """每个测试一个临时 SQLite 文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "auth.db")

    def tearDown(self):
        self._tmp.cleanup()


class TestSessionCrossWorker(_TempDBTestCase):
    """会话：A 建的会话 B 能读；A 登出后 B 读不到。"""

    def test_create_visible_to_other_instance(self):
        a = SessionStore(db_path=self.db_path)
        b = SessionStore(db_path=self.db_path)
        token = a.create("admin")
        self.assertEqual(b.get(token), "admin")

    def test_revoke_visible_to_other_instance(self):
        a = SessionStore(db_path=self.db_path)
        b = SessionStore(db_path=self.db_path)
        token = a.create("admin")
        self.assertEqual(b.get(token), "admin")
        a.revoke(token)
        self.assertIsNone(b.get(token))

    def test_expired_session_removed_on_get(self):
        a = SessionStore(db_path=self.db_path)
        token = a.create("admin", expire_seconds=-1)
        self.assertIsNone(a.get(token))
        self.assertEqual(
            _db_rows(self.db_path,
                     "SELECT COUNT(*) FROM sessions WHERE token=?",
                     (token,)),
            [(0,)])

    def test_reset_visible_to_other_instance(self):
        a = SessionStore(db_path=self.db_path)
        b = SessionStore(db_path=self.db_path)
        token = a.create("admin")
        self.assertEqual(b.get(token), "admin")
        a.reset()
        self.assertIsNone(b.get(token))


class TestLoginGuardCrossWorker(_TempDBTestCase):
    """登录锁定：跨两个实例累计失败次数，达阈值后锁定。"""

    def test_failures_accumulate_across_instances(self):
        a = LoginGuard(threshold=5, base_minutes=1, max_minutes=30,
                       db_path=self.db_path)
        b = LoginGuard(threshold=5, base_minutes=1, max_minutes=30,
                       db_path=self.db_path)
        for _ in range(3):
            a.record_failure("admin")
        self.assertFalse(b.is_locked("admin"))
        for _ in range(2):
            b.record_failure("admin")
        # 3 + 2 = 5 次，两个实例都看到锁定。
        self.assertTrue(a.is_locked("admin"))
        self.assertTrue(b.is_locked("admin"))
        self.assertGreater(a.remaining_lock_seconds("admin"), 55)

    def test_success_on_one_instance_clears_for_other(self):
        a = LoginGuard(threshold=5, base_minutes=1, max_minutes=30,
                       db_path=self.db_path)
        b = LoginGuard(threshold=5, base_minutes=1, max_minutes=30,
                       db_path=self.db_path)
        for _ in range(5):
            a.record_failure("admin")
        self.assertTrue(b.is_locked("admin"))
        b.record_success("admin")
        self.assertFalse(a.is_locked("admin"))
        self.assertFalse(b.is_locked("admin"))

    def test_concurrent_failures_count_exactly_once_each(self):
        """10 个线程各记 1 次失败：总数必须是 10 次（不丢不重），
        5 次后锁定，锁定期间的失败不计入（锁定时长不被延长）。"""
        guard = LoginGuard(threshold=5, base_minutes=1, max_minutes=30,
                           db_path=self.db_path)
        barrier = threading.Barrier(10)
        errors = []

        def _fail_once():
            try:
                barrier.wait(timeout=10)
                guard.record_failure("admin")
            except Exception as exc:  # noqa: BLE001 - 记下来再断言
                errors.append(exc)

        threads = [threading.Thread(target=_fail_once)
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertTrue(guard.is_locked("admin"))
        # 锁定只触发一次（1 分钟），不是多次叠加。
        remaining = guard.remaining_lock_seconds("admin")
        self.assertGreater(remaining, 55)
        self.assertLessEqual(remaining, 60)


class TestTelegramCodeCrossWorker(_TempDBTestCase):
    """验证码：A 发的码 B 能验；验过一次后重放失败；冷却跨实例共享。"""

    def test_verify_from_other_instance(self):
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        code_id, code = a.create()
        self.assertTrue(b.verify(code_id, code))

    def test_replay_fails(self):
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        code_id, code = a.create()
        self.assertTrue(b.verify(code_id, code))
        self.assertFalse(a.verify(code_id, code))
        self.assertFalse(b.verify(code_id, code))

    def test_wrong_code_consumes(self):
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        code_id, code = a.create()
        self.assertFalse(b.verify(code_id, "000000"))
        # 错一次就消费掉了，正确的码也验不过了。
        self.assertFalse(a.verify(code_id, code))

    def test_cooldown_shared_across_instances(self):
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        can, _ = a.can_send("203.0.113.10")
        self.assertTrue(can)
        a.mark_sent("203.0.113.10")
        can, remaining = b.can_send("203.0.113.10")
        self.assertFalse(can)
        self.assertGreater(remaining, 0)
        # 另一个客户端不受影响。
        can, _ = b.can_send("203.0.113.11")
        self.assertTrue(can)

    def test_concurrent_verify_only_one_wins(self):
        """两个实例同时验同一条码：只能一人成功，另一人必须失败
        （BEGIN IMMEDIATE 保证“查-删”原子，防重放）。"""
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        code_id, code = a.create()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def _verify(store):
            try:
                barrier.wait(timeout=10)
                results.append(store.verify(code_id, code))
            except Exception as exc:  # noqa: BLE001 - 记下来再断言
                errors.append(exc)

        t1 = threading.Thread(target=_verify, args=(a,))
        t2 = threading.Thread(target=_verify, args=(b,))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_revoke_visible_to_other_instance(self):
        a = TelegramCodeStore(db_path=self.db_path)
        b = TelegramCodeStore(db_path=self.db_path)
        code_id, code = a.create()
        a.revoke(code_id)
        self.assertFalse(b.verify(code_id, code))


class TestMagicLoginCrossWorker(_TempDBTestCase):
    """一键登录 token：A 建的 B 能消费；消费一次后重放失败。"""

    def test_consume_from_other_instance(self):
        a = MagicLoginStore(db_path=self.db_path)
        b = MagicLoginStore(db_path=self.db_path)
        token = a.create()
        self.assertTrue(b.consume(token))

    def test_replay_fails(self):
        a = MagicLoginStore(db_path=self.db_path)
        b = MagicLoginStore(db_path=self.db_path)
        token = a.create()
        self.assertTrue(b.consume(token))
        self.assertFalse(a.consume(token))
        self.assertFalse(b.consume(token))

    def test_concurrent_consume_only_one_wins(self):
        """两个实例同时消费同一个 token：只能一人成功。"""
        a = MagicLoginStore(db_path=self.db_path)
        b = MagicLoginStore(db_path=self.db_path)
        token = a.create()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def _consume(store):
            try:
                barrier.wait(timeout=10)
                results.append(store.consume(token))
            except Exception as exc:  # noqa: BLE001 - 记下来再断言
                errors.append(exc)

        t1 = threading.Thread(target=_consume, args=(a,))
        t2 = threading.Thread(target=_consume, args=(b,))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_revoke_visible_to_other_instance(self):
        a = MagicLoginStore(db_path=self.db_path)
        b = MagicLoginStore(db_path=self.db_path)
        token = a.create()
        a.revoke(token)
        self.assertFalse(b.consume(token))


class TestAuthDBFileProperties(_TempDBTestCase):
    """DB 文件本身：WAL 模式、权限 600。"""

    def test_file_created_with_mode_600_and_wal(self):
        store = SessionStore(db_path=self.db_path)
        store.create("admin")
        mode = stat.S_IMODE(os.stat(self.db_path).st_mode)
        self.assertEqual(mode, 0o600)
        conn = sqlite3.connect(self.db_path)
        try:
            journal = conn.execute(
                "PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(journal, "wal")

    def test_all_four_tables_exist(self):
        # 各建一条记录，触发全部建表。
        SessionStore(db_path=self.db_path).create("admin")
        LoginGuard(db_path=self.db_path).record_failure("admin")
        TelegramCodeStore(db_path=self.db_path).create()
        MagicLoginStore(db_path=self.db_path).create()
        tables = _db_rows(
            self.db_path,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name")
        names = {row[0] for row in tables}
        self.assertTrue(
            {"sessions", "login_guard", "telegram_codes",
             "telegram_cooldowns", "magic_tokens"} <= names)


class TestMemoryModeIsolated(unittest.TestCase):
    """不传 db_path 时是隔离内存库：各实例互不干扰（旧测试行为）。"""

    def test_instances_do_not_share(self):
        a = SessionStore()
        b = SessionStore()
        token = a.create("admin")
        self.assertIsNone(b.get(token))
        self.assertEqual(a.get(token), "admin")

    def test_guard_instances_do_not_share(self):
        a = LoginGuard(threshold=5, base_minutes=1, max_minutes=30)
        b = LoginGuard(threshold=5, base_minutes=1, max_minutes=30)
        for _ in range(5):
            a.record_failure("admin")
        self.assertTrue(a.is_locked("admin"))
        self.assertFalse(b.is_locked("admin"))


if __name__ == "__main__":
    unittest.main()
