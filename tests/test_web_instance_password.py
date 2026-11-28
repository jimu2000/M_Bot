#!/usr/bin/env python3
"""实例初始开机密码接口（GET /api/instances/{id}/password）的测试。

密码只对单台实例显式请求才返回（管理员登录后可用）；列表与详情接口
都不带密码。测试用临时凭据库，不碰真实数据目录。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot.instance_credentials import save_credential
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.instances import format_instance
from web.tasks import TASKS

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
TEST_PASSWORD = "test-admin-password"


class PasswordApiTestCase(unittest.TestCase):
    """密码接口测试基类：建 app、临时 DATA_DIR、临时凭据库、登录。"""

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
        self._cred_patcher = patch(
            "tgbot.instance_credentials.CREDENTIALS_FILE",
            os.path.join(self._tmp.name, "instance_credentials.json"))
        self._cred_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin",
                                      "password": TEST_PASSWORD})
        assert resp.status_code == 200, resp.text

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._cred_patcher.stop()
        self._tmp.cleanup()


class TestInstancePasswordApi(PasswordApiTestCase):
    """GET /api/instances/{id}/password。"""

    def test_returns_404_when_no_credential(self):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/password")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_returns_password_when_stored(self):
        save_credential(FAKE_INSTANCE, "pw-test-123",
                        instance_name="AMD_2026-01-02")
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/password")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["root_password"], "pw-test-123")

    def test_requires_admin_auth(self):
        r = self.anon.get(f"/api/instances/{FAKE_INSTANCE}/password")
        self.assertEqual(r.status_code, 401)

    def test_detail_response_has_no_password(self):
        # Even with a stored password, the detail API must not leak it.
        # 即使存了密码，详情接口也不能把它带出来。
        save_credential(FAKE_INSTANCE, "pw-test-123",
                        instance_name="AMD_2026-01-02")
        detail = {"id": FAKE_INSTANCE, "display-name": "AMD_2026-01-02",
                  "shape": "VM.Standard.E2.1.Micro",
                  "lifecycle-state": "STOPPED",
                  "availability-domain": "AD-1",
                  "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
                  "time-created": "2026-01-02T10:00:00.000Z"}
        extras = {"private_ip": None, "ipv6_addresses": [],
                  "boot_volume_size_gbs": 47, "boot_volume_id": None,
                  "boot_volume_vpus_per_gb": None,
                  "boot_volume_state": None,
                  "boot_volume_attachment_state": None}
        with patch("web.routes.instances.resolve_profile",
                   return_value=(MagicMock(),
                                 "ocid1.compartment.oc1..test")), \
                patch("web.routes.instances.get_instance",
                      return_value=detail), \
                patch("web.routes.instances._safe_public_ip",
                      return_value=None), \
                patch("web.routes.instances._detail_extras",
                      return_value=extras):
            r = self.client.get(f"/api/instances/{FAKE_INSTANCE}")
        self.assertEqual(r.status_code, 200)
        body = json.dumps(r.json())
        self.assertNotIn("root_password", body)
        self.assertNotIn("pw-test-123", body)

    def test_format_instance_has_no_password(self):
        inst = {"id": FAKE_INSTANCE, "display-name": "AMD_2026-01-02",
                "shape": "VM.Standard.E2.1.Micro",
                "lifecycle-state": "RUNNING",
                "time-created": "2026-01-02T10:00:00.000Z",
                "shape-config": {"ocpus": 1, "memory-in-gbs": 1}}
        out = format_instance(inst, "203.0.113.10", "ap-singapore-1")
        self.assertNotIn("root_password", out)
        self.assertNotIn("password", out)


if __name__ == "__main__":
    unittest.main()
