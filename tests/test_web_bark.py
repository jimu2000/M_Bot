#!/usr/bin/env python3
"""Web 面板 Bark 通知设置接口测试（v1.11.0）：状态、配置、开关、
测试推送、页面渲染。

TestClient + 临时 bark.json，不碰真实配置与网络；测试推送 mock。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot import bark as bark_mod
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

TEST_PASSWORD = "unit-test-" + "only-123"
FAKE_URL = "https://api.day.app/FAKEDEVICEKEY123/"
FAKE_KEY = "0123456789abcdef0123456789abcdef"


class BarkWebTestCase(unittest.TestCase):
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
        # bark.json 写临时文件（BARK_CONFIG_FILE 是 import 时绑定的，
        # patch DATA_DIR 影响不到它，必须直接 patch 该常量）。
        self._cfg_patcher = patch.object(
            bark_mod, "BARK_CONFIG_FILE",
            os.path.join(self._tmp.name, "bark.json"))
        self._cfg_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._cfg_patcher.stop()
        self._tmp.cleanup()

    def _write_config(self, url=FAKE_URL, key=FAKE_KEY, enabled=False):
        bark_mod.save_bark_config({"url": url, "key": key,
                                   "enabled": enabled, "chat_id": None})


class TestBarkStatusApi(BarkWebTestCase):
    def test_status_unconfigured(self):
        r = self.client.get("/api/bark/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["enabled"])
        self.assertFalse(body["configured"])
        self.assertEqual(body["url_masked"], "未设置")
        self.assertFalse(body["key_set"])

    def test_status_configured_masked(self):
        self._write_config()
        r = self.client.get("/api/bark/status")
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["key_set"])
        # 地址只返回脱敏版，Key 原文绝不出现在响应里。
        self.assertNotEqual(body["url_masked"], FAKE_URL)
        self.assertNotIn(FAKE_KEY, r.text)
        self.assertNotIn(FAKE_URL, r.text)

    def test_status_requires_login(self):
        anon = TestClient(self.app)
        r = anon.get("/api/bark/status")
        self.assertEqual(r.status_code, 401)


class TestBarkConfigApi(BarkWebTestCase):
    def test_set_url_and_key(self):
        r = self.client.post("/api/bark/config",
                             json={"url": FAKE_URL, "key": FAKE_KEY})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["key_set"])
        cfg = bark_mod.load_bark_config()
        self.assertEqual(cfg["url"], FAKE_URL)
        self.assertEqual(cfg["key"], FAKE_KEY)

    def test_set_key_only_keeps_url(self):
        self._write_config(url=FAKE_URL, key="")
        r = self.client.post("/api/bark/config",
                             json={"key": FAKE_KEY})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["configured"])
        self.assertEqual(bark_mod.load_bark_config()["url"], FAKE_URL)

    def test_bad_url_400(self):
        r = self.client.post("/api/bark/config",
                             json={"url": "api.day.app/xxx"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("http", r.json()["error"])

    def test_short_key_400(self):
        r = self.client.post("/api/bark/config",
                             json={"key": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("32 位", r.json()["error"])

    def test_empty_400(self):
        r = self.client.post("/api/bark/config", json={})
        self.assertEqual(r.status_code, 400)


class TestBarkToggleApi(BarkWebTestCase):
    def test_toggle_on_requires_config(self):
        r = self.client.post("/api/bark/toggle",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("配齐", r.json()["error"])

    def test_toggle_on_off(self):
        self._write_config()
        r = self.client.post("/api/bark/toggle",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])
        self.assertTrue(bark_mod.load_bark_config()["enabled"])
        r = self.client.post("/api/bark/toggle",
                             json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])


class TestBarkTestApi(BarkWebTestCase):
    def test_test_requires_config(self):
        r = self.client.post("/api/bark/test", json={})
        self.assertEqual(r.status_code, 400)

    def test_test_success(self):
        self._write_config()
        with patch("web.routes.bark.send_bark_test",
                   return_value=(True, "")) as mock_test:
            r = self.client.post("/api/bark/test", json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["sent"])
        mock_test.assert_called_once()

    def test_test_failure_502(self):
        self._write_config()
        with patch("web.routes.bark.send_bark_test",
                   return_value=(False, "boom")):
            r = self.client.post("/api/bark/test", json={})
        self.assertEqual(r.status_code, 502)
        self.assertIn("boom", r.json()["error"])


class TestBarkGenerateKeyApi(BarkWebTestCase):
    """自动生成 Key 接口（v1.12.0）：返回 Key 原文 + 二维码，只显示一次。"""

    def test_generate_returns_key_and_qr(self):
        r = self.client.post("/api/bark/generate-key", json={})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        key = data["key"]
        self.assertEqual(len(key), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in key))
        self.assertTrue(data["key_set"])
        self.assertTrue(data["qr_png"].startswith("data:image/png;base64,"))
        cfg = bark_mod.load_bark_config()
        self.assertEqual(cfg["key"], key)

    def test_generate_overwrites_old_key(self):
        self._write_config(key=FAKE_KEY)
        r = self.client.post("/api/bark/generate-key", json={})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.json()["key"], FAKE_KEY)

    def test_generate_requires_login(self):
        anon = TestClient(self.app)
        r = anon.post("/api/bark/generate-key", json={})
        self.assertEqual(r.status_code, 401)


class TestBarkPage(BarkWebTestCase):
    def test_bark_page_renders(self):
        r = self.client.get("/bark")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Bark 通知设置", r.text)
        self.assertIn("/static/js/bark.js", r.text)
        self.assertIn('data-page="bark" class="active"', r.text)
        self.assertIn("btn-gen-bark-key", r.text)

    def test_bark_page_requires_login(self):
        anon = TestClient(self.app)
        r = anon.get("/bark", follow_redirects=False)
        self.assertIn(r.status_code, (302, 303, 307))


if __name__ == "__main__":
    unittest.main()
