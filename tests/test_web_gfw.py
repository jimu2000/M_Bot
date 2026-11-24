#!/usr/bin/env python3
"""Web 面板 GFW 自动换 IP 接口测试（v1.10.0）：状态、Key、实例列表、
开关、立即检查。

TestClient + mock oci 函数，不碰真实 API；gfw 配置写临时目录。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot import config as bot_config
from tgbot import gfw
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

TEST_PASSWORD = "unit-test-" + "only-123"

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaa", "display-name": "web-1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "public-ip": "203.0.113.10"},
    {"id": "ocid1.instance.oc1..bbb", "display-name": "stopped-1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "STOPPED",
     "public-ip": None},
]


class GfwWebTestCase(unittest.TestCase):
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
        self._data_dir_patcher = patch.object(bot_config, "DATA_DIR",
                                              self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()


class TestGfwStatusApi(GfwWebTestCase):
    def test_status_unconfigured(self):
        r = self.client.get("/api/gfw/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["configured"])
        self.assertEqual(body["key_masked"], "未设置")
        self.assertEqual(body["enabled"], {})
        self.assertEqual(body["interval_minutes"], 30)

    def test_status_configured_masked(self):
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        gfw.set_instance_enabled("ocid1.instance.oc1..aaa", True,
                                 name="web-1", ip="203.0.113.10")
        r = self.client.get("/api/gfw/status")
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["key_masked"], "abcd…7890")
        self.assertNotIn("abcdef12-3456-7890-abcd-ef1234567890",
                         r.text)
        self.assertIn("ocid1.instance.oc1..aaa", body["enabled"])


class TestGfwKeyApi(GfwWebTestCase):
    def test_set_key_ok(self):
        r = self.client.post("/api/gfw/key",
                             json={"key": "abcdef12-3456-7890-abcd-ef1234567890"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["key_masked"], "abcd…7890")
        self.assertEqual(gfw.load_gfw_config()["api_key"],
                         "abcdef12-3456-7890-abcd-ef1234567890")

    def test_set_key_too_short_400(self):
        r = self.client.post("/api/gfw/key", json={"key": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])


class TestGfwInstancesApi(GfwWebTestCase):
    @patch("web.routes.gfw.resolve_profile")
    @patch("web.routes.gfw.list_instances")
    @patch("web.routes.gfw.attach_public_ips")
    def test_lists_running_with_ip(self, mock_attach, mock_list,
                                   mock_resolve):
        mock_resolve.return_value = (MagicMock(), "ocid1.tenancy.oc1..t")
        mock_list.return_value = list(FAKE_INSTANCES)
        mock_attach.side_effect = lambda cli, insts: insts
        r = self.client.get("/api/gfw/instances?profile=DEFAULT")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["instances"]), 1)
        self.assertEqual(body["instances"][0]["display-name"], "web-1")
        self.assertEqual(body["instances"][0]["public-ip"], "203.0.113.10")

    @patch("web.routes.gfw.resolve_profile",
           side_effect=ValueError("未知 profile"))
    def test_unknown_profile_400(self, mock_resolve):
        r = self.client.get("/api/gfw/instances?profile=NOPE")
        self.assertEqual(r.status_code, 400)


class TestGfwToggleApi(GfwWebTestCase):
    def test_toggle_on_and_off(self):
        r = self.client.post("/api/gfw/toggle", json={
            "instance_id": "ocid1.instance.oc1..aaa", "enabled": True,
            "profile": "DEFAULT", "region": "ap-singapore-1",
            "name": "web-1", "ip": "203.0.113.10"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["enabled"])
        self.assertIn("ocid1.instance.oc1..aaa",
                      gfw.load_gfw_config()["enabled"])
        r = self.client.post("/api/gfw/toggle", json={
            "instance_id": "ocid1.instance.oc1..aaa", "enabled": False})
        self.assertFalse(r.json()["enabled"])
        self.assertEqual(gfw.load_gfw_config()["enabled"], {})

    def test_toggle_missing_id_400(self):
        r = self.client.post("/api/gfw/toggle",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 400)


class TestGfwCheckNowApi(GfwWebTestCase):
    def test_check_now_without_key_400(self):
        r = self.client.post("/api/gfw/check-now")
        self.assertEqual(r.status_code, 400)

    def test_check_now_without_enabled_400(self):
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        r = self.client.post("/api/gfw/check-now")
        self.assertEqual(r.status_code, 400)

    def test_check_now_starts_thread(self):
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        gfw.set_instance_enabled("ocid1.instance.oc1..aaa", True)
        with patch("web.routes.gfw.threading.Thread") as thread_cls:
            r = self.client.post("/api/gfw/check-now")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()


class TestGfwPage(GfwWebTestCase):
    def test_gfw_page_renders(self):
        r = self.client.get("/gfw")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GFW 自动换 IP", r.text)
        self.assertIn("gfw.js", r.text)


if __name__ == "__main__":
    unittest.main()
