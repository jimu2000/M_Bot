#!/usr/bin/env python3
"""阶段 5 工作包 E：登录验证码与 Telegram 验证码登录的测试。

覆盖：
  - 图形验证码：CaptchaStore 一次性/过期/大小写不敏感、PNG 与 SVG 渲染、
    GET /api/auth/captcha（开/关）、登录集成（缺码/错码/正确）。
  - Telegram 验证码：TelegramCodeStore 一次性/过期/60 秒冷却、
    send-code（冷却 429、机器人未配置 503）、verify（正确建会话、
    错码/过期/重用 401）。
  - Telegram 发送全部 mock，不真发消息；验证码绝不出现在响应里。
"""

import base64
import os
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import (
    LOGIN_GUARD,
    SESSIONS,
    TELEGRAM_CODES,
    TelegramCodeStore,
)
from web.auth import hash_password
from web.captcha import (
    CAPTCHAS,
    CaptchaStore,
    generate_captcha,
    render_captcha_image,
)

# 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)


class CaptchaStoreTest(unittest.TestCase):
    """CaptchaStore 纯逻辑测试。"""

    def test_verify_once(self):
        store = CaptchaStore()
        captcha_id, answer = store.create(answer="Ab123")
        self.assertTrue(store.verify(captcha_id, "Ab123"))
        # 一次性：再验同一条失败。
        self.assertFalse(store.verify(captcha_id, "Ab123"))

    def test_verify_case_insensitive(self):
        store = CaptchaStore()
        captcha_id, _ = store.create(answer="Ab123")
        self.assertTrue(store.verify(captcha_id, "ab123"))

    def test_verify_wrong_answer_consumes(self):
        """答错也消耗验证码（防暴力猜测）。"""
        store = CaptchaStore()
        captcha_id, _ = store.create(answer="Ab123")
        self.assertFalse(store.verify(captcha_id, "wrong"))
        self.assertFalse(store.verify(captcha_id, "Ab123"))

    def test_verify_expired(self):
        store = CaptchaStore(expire_seconds=0)
        captcha_id, answer = store.create(answer="Ab123")
        self.assertFalse(store.verify(captcha_id, answer))

    def test_verify_unknown_id(self):
        store = CaptchaStore()
        self.assertFalse(store.verify("nope", "Ab123"))

    def test_random_answer_length(self):
        _cid, answer = CaptchaStore().create()
        self.assertEqual(len(answer), 5)


class CaptchaRenderTest(unittest.TestCase):
    """验证码图片渲染：PNG（Pillow）与 SVG 兜底。"""

    def test_png_render(self):
        image, mime = render_captcha_image("Ab123")
        # 本机装了 Pillow，应为 PNG。
        self.assertEqual(mime, "image/png")
        self.assertTrue(image.startswith(b"\x89PNG"))

    def test_svg_fallback(self):
        with patch("web.captcha._PIL_AVAILABLE", False):
            image, mime = render_captcha_image("Ab123")
        self.assertEqual(mime, "image/svg+xml")
        self.assertIn(b"<svg", image)
        # 每个字符单独一个 <text> 元素。
        for ch in b"Ab123":
            self.assertIn(bytes([ch]) + b"</text>", image)

    def test_generate_captcha_data_url(self):
        CAPTCHAS.reset()
        captcha_id, data_url = generate_captcha()
        self.assertTrue(captcha_id)
        self.assertTrue(data_url.startswith("data:image/"))
        # data URL 能解码。
        _header, b64 = data_url.split(",", 1)
        self.assertTrue(base64.b64decode(b64))
        CAPTCHAS.reset()


class TelegramCodeStoreTest(unittest.TestCase):
    """TelegramCodeStore 纯逻辑测试。"""

    def test_code_is_six_digits(self):
        _cid, code = TelegramCodeStore().create()
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())

    def test_verify_once(self):
        store = TelegramCodeStore()
        code_id, code = store.create()
        self.assertTrue(store.verify(code_id, code))
        self.assertFalse(store.verify(code_id, code))

    def test_verify_wrong_consumes(self):
        store = TelegramCodeStore()
        code_id, code = store.create()
        self.assertFalse(store.verify(code_id, "000000"))
        self.assertFalse(store.verify(code_id, code))

    def test_verify_expired(self):
        store = TelegramCodeStore()
        code_id, code = store.create()
        store._codes[code_id]["expires"] = time.time() - 1
        self.assertFalse(store.verify(code_id, code))

    def test_cooldown(self):
        store = TelegramCodeStore(cooldown_seconds=60)
        can, remaining = store.can_send("203.0.113.10")
        self.assertTrue(can)
        self.assertEqual(remaining, 0)
        store.mark_sent("203.0.113.10")
        can, remaining = store.can_send("203.0.113.10")
        self.assertFalse(can)
        self.assertGreater(remaining, 0)
        # 别的客户端不受影响。
        can, _ = store.can_send("203.0.113.11")
        self.assertTrue(can)

    def test_revoke(self):
        store = TelegramCodeStore()
        code_id, code = store.create()
        store.revoke(code_id)
        self.assertFalse(store.verify(code_id, code))


class Phase5eAuthTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        CAPTCHAS.reset()
        TELEGRAM_CODES.reset()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()


class TestCaptchaApi(Phase5eAuthTestCase):
    """图形验证码接口与登录集成。"""

    def test_captcha_disabled_by_default(self):
        r = self.client.get("/api/auth/captcha")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])
        self.assertNotIn("captchaId", r.json())

    def test_captcha_enabled_returns_image(self):
        with patch("web.config.CAPTCHA_ENABLED", True):
            r = self.client.get("/api/auth/captcha")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertTrue(body["captchaId"])
        self.assertTrue(body["image"].startswith("data:image/"))

    def test_login_without_captcha_when_enabled_400(self):
        with patch("web.config.CAPTCHA_ENABLED", True):
            r = self.client.post("/api/auth/login", json={
                "username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(r.status_code, 400)
        self.assertIn("验证码", r.json()["error"])

    def test_login_wrong_captcha_400(self):
        captcha_id, _ = CAPTCHAS.create(answer="Ab123")
        with patch("web.config.CAPTCHA_ENABLED", True):
            r = self.client.post("/api/auth/login", json={
                "username": "admin", "password": TEST_PASSWORD,
                "captcha_id": captcha_id, "captcha_answer": "wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("验证码", r.json()["error"])

    def test_login_correct_captcha_ok(self):
        captcha_id, _ = CAPTCHAS.create(answer="Ab123")
        with patch("web.config.CAPTCHA_ENABLED", True):
            r = self.client.post("/api/auth/login", json={
                "username": "admin", "password": TEST_PASSWORD,
                "captcha_id": captcha_id, "captcha_answer": "ab123"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_login_captcha_not_reusable(self):
        """验证码一次性：登录成功后同一 captchaId 不能再用。"""
        captcha_id, _ = CAPTCHAS.create(answer="Ab123")
        with patch("web.config.CAPTCHA_ENABLED", True):
            r1 = self.client.post("/api/auth/login", json={
                "username": "admin", "password": TEST_PASSWORD,
                "captcha_id": captcha_id, "captcha_answer": "Ab123"})
            self.assertEqual(r1.status_code, 200)
            r2 = self.client.post("/api/auth/login", json={
                "username": "admin", "password": TEST_PASSWORD,
                "captcha_id": captcha_id, "captcha_answer": "Ab123"})
            self.assertEqual(r2.status_code, 400)

    def test_login_disabled_captcha_unchanged(self):
        """验证码关闭时登录流程不变（不带 captcha 字段也能登）。"""
        r = self.client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(r.status_code, 200)


class TestRememberMe(Phase5eAuthTestCase):
    """v1.23.0：记住我——勾选时会话与 cookie 同时延长到 30 天。"""

    def test_remember_extends_cookie_and_session(self):
        r = self.client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD,
            "remember": True})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("set-cookie", "")
        self.assertIn(f"Max-Age={30 * 24 * 3600}", cookie)
        # 服务端会话过期时间也在约 30 天后（不是默认 12 小时）。
        self.assertEqual(len(SESSIONS._sessions), 1)
        session = next(iter(SESSIONS._sessions.values()))
        remaining = session["expires"] - time.time()
        self.assertGreater(remaining, 29 * 24 * 3600)
        self.assertLessEqual(remaining, 30 * 24 * 3600)

    def test_default_login_keeps_12_hours(self):
        r = self.client.post("/api/auth/login", json={
            "username": "admin", "password": TEST_PASSWORD})
        self.assertEqual(r.status_code, 200)
        cookie = r.headers.get("set-cookie", "")
        self.assertIn(f"Max-Age={12 * 3600}", cookie)
        session = next(iter(SESSIONS._sessions.values()))
        remaining = session["expires"] - time.time()
        self.assertGreater(remaining, 11 * 3600)
        self.assertLessEqual(remaining, 12 * 3600)


class TestTelegramLoginApi(Phase5eAuthTestCase):
    """Telegram 验证码登录接口。"""

    @patch("web.routes.auth.send_to_allowed_users", return_value=1)
    def test_send_code_ok(self, mock_send):
        r = self.client.post("/api/auth/telegram/send-code", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["codeId"])
        self.assertEqual(body["expiresInSeconds"], 300)
        # 验证码本身绝不出现在响应里。
        self.assertNotIn("code", body)
        mock_send.assert_called_once()
        text = mock_send.call_args[0][0]
        self.assertIn("验证码", text)

    @patch("web.routes.auth.send_to_allowed_users", return_value=1)
    def test_send_code_cooldown_429(self, mock_send):
        r1 = self.client.post("/api/auth/telegram/send-code", json={})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/auth/telegram/send-code", json={})
        self.assertEqual(r2.status_code, 429)
        self.assertIn("retry_after_seconds", r2.json())
        self.assertGreater(r2.json()["retry_after_seconds"], 0)
        # 冷却期内只发了一次。
        self.assertEqual(mock_send.call_count, 1)

    @patch("web.routes.auth.send_to_allowed_users", return_value=0)
    def test_send_code_no_bot_503(self, mock_send):
        """机器人未配置（发给 0 人）时 503，且不进冷却。"""
        r = self.client.post("/api/auth/telegram/send-code", json={})
        self.assertEqual(r.status_code, 503)
        # 不进冷却：立即再发仍是 503（不是 429）。
        r2 = self.client.post("/api/auth/telegram/send-code", json={})
        self.assertEqual(r2.status_code, 503)

    def test_verify_ok_creates_session(self):
        code_id, code = TELEGRAM_CODES.create()
        r = self.client.post("/api/auth/telegram/verify",
                             json={"code_id": code_id, "code": code})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["username"], "admin")
        # 会话 cookie 已设，/api/auth/me 能用。
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "admin")

    def test_verify_wrong_code_401(self):
        code_id, _code = TELEGRAM_CODES.create()
        r = self.client.post("/api/auth/telegram/verify",
                             json={"code_id": code_id, "code": "000000"})
        self.assertEqual(r.status_code, 401)

    def test_verify_code_one_time(self):
        code_id, code = TELEGRAM_CODES.create()
        r1 = self.client.post("/api/auth/telegram/verify",
                              json={"code_id": code_id, "code": code})
        self.assertEqual(r1.status_code, 200)
        r2 = self.client.post("/api/auth/telegram/verify",
                              json={"code_id": code_id, "code": code})
        self.assertEqual(r2.status_code, 401)

    def test_verify_expired_401(self):
        code_id, code = TELEGRAM_CODES.create()
        TELEGRAM_CODES._codes[code_id]["expires"] = time.time() - 1
        r = self.client.post("/api/auth/telegram/verify",
                             json={"code_id": code_id, "code": code})
        self.assertEqual(r.status_code, 401)

    def test_verify_unknown_id_401(self):
        r = self.client.post("/api/auth/telegram/verify",
                             json={"code_id": "nope", "code": "123456"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
