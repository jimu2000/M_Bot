#!/usr/bin/env python3
"""阶段 5 工作包 A：Web 面板新增接口的测试（TestClient + mock oci 函数）。

覆盖：批量操作、改名、重装、串行控制台、改 VPU、A1 配置体检。
不碰真实 API；测试密码哈希在 setUpClass 里生成并 patch 进 web.config。

运行：python3 -m unittest tests.test_phase5_web -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password

# Placeholders (no real OCID / IP in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_VOLUME = "ocid1.bootvolume.oc1..testxxxx"
FAKE_IMAGE = "ocid1.image.oc1..testxxxx"

TEST_PASSWORD = "test-password"

FAKE_BATCH_RESULT = {
    "succeeded": [FAKE_INSTANCE],
    "failed": [{"id": FAKE_INSTANCE_2, "error": "boom"}],
}

FAKE_HEALTH = {
    "overall_ok": True,
    "suggestions": [{"severity": "ok", "message": "A1 配置健康。"}],
}


class Phase5WebTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录辅助方法。"""

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
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self._login()

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=TEST_PASSWORD):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})


class TestBatchApi(Phase5WebTestCase):
    """POST /api/instances/batch。"""

    @patch("web.routes.instances.batch_start",
           return_value=FAKE_BATCH_RESULT)
    def test_batch_start(self, mock_batch):
        r = self.client.post("/api/instances/batch",
                             json={"action": "start",
                                   "instance_ids": [FAKE_INSTANCE,
                                                    FAKE_INSTANCE_2]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "start")
        self.assertEqual(body["succeeded"], [FAKE_INSTANCE])
        self.assertEqual(len(body["failed"]), 1)
        mock_batch.assert_called_once_with(
            ANY, [FAKE_INSTANCE, FAKE_INSTANCE_2])

    @patch("web.routes.instances.batch_stop",
           return_value={"succeeded": [FAKE_INSTANCE], "failed": []})
    def test_batch_stop(self, mock_batch):
        r = self.client.post("/api/instances/batch",
                             json={"action": "stop",
                                   "instance_ids": [FAKE_INSTANCE]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "stop")

    @patch("web.routes.instances.batch_restart",
           return_value={"succeeded": [FAKE_INSTANCE], "failed": []})
    def test_batch_restart(self, mock_batch):
        r = self.client.post("/api/instances/batch",
                             json={"action": "restart",
                                   "instance_ids": [FAKE_INSTANCE]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "restart")

    @patch("web.routes.instances.batch_terminate")
    def test_batch_terminate_without_confirm_400(self, mock_batch):
        """批量删除缺 confirm 返回 400，且不调用删除函数。"""
        for body in ({}, {"confirm": False}):
            r = self.client.post(
                "/api/instances/batch",
                json={"action": "terminate",
                      "instance_ids": [FAKE_INSTANCE], **body})
            self.assertEqual(r.status_code, 400)
        mock_batch.assert_not_called()

    @patch("web.routes.instances.batch_terminate",
           return_value={"succeeded": [FAKE_INSTANCE], "failed": []})
    def test_batch_terminate_with_confirm(self, mock_batch):
        r = self.client.post("/api/instances/batch",
                             json={"action": "terminate",
                                   "instance_ids": [FAKE_INSTANCE],
                                   "confirm": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "terminate")
        mock_batch.assert_called_once_with(ANY, [FAKE_INSTANCE])

    def test_batch_unknown_action_400(self):
        r = self.client.post("/api/instances/batch",
                             json={"action": "explode",
                                   "instance_ids": [FAKE_INSTANCE]})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    def test_batch_empty_ids_400(self):
        r = self.client.post("/api/instances/batch",
                             json={"action": "start", "instance_ids": []})
        self.assertEqual(r.status_code, 400)

    def test_batch_unauthenticated_401(self):
        r = self.anon.post("/api/instances/batch",
                           json={"action": "start",
                                 "instance_ids": [FAKE_INSTANCE]})
        self.assertEqual(r.status_code, 401)


class TestRenameApi(Phase5WebTestCase):
    """POST /api/instances/{id}/rename。"""

    @patch("web.routes.instances.rename_instance")
    def test_rename_ok(self, mock_rename):
        mock_rename.return_value = {"id": FAKE_INSTANCE,
                                    "display-name": "new-name"}
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/rename",
                             json={"name": "new-name"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["instance"]["display-name"], "new-name")
        mock_rename.assert_called_once_with(ANY, FAKE_INSTANCE, "new-name")

    @patch("web.routes.instances.rename_instance")
    def test_rename_empty_name_400(self, mock_rename):
        for name in ("", "   "):
            r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/rename",
                                 json={"name": name})
            self.assertEqual(r.status_code, 400, repr(name))
        mock_rename.assert_not_called()

    @patch("web.routes.instances.rename_instance",
           side_effect=ValueError("新实例名不能超过 255 字符"))
    def test_rename_validation_400(self, mock_rename):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/rename",
                             json={"name": "x" * 300})
        self.assertEqual(r.status_code, 400)

    @patch("web.routes.instances.rename_instance",
           side_effect=NotFoundError("not found"))
    def test_rename_not_found_404(self, mock_rename):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/rename",
                             json={"name": "new-name"})
        self.assertEqual(r.status_code, 404)


class TestReinstallApi(Phase5WebTestCase):
    """POST /api/instances/{id}/reinstall。"""

    @patch("web.routes.instances.reinstall_instance")
    @patch("web.routes.instances.get_instance")
    def test_reinstall_ok(self, mock_get, mock_reinstall):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "web-server"}}
        mock_reinstall.return_value = {"id": FAKE_INSTANCE}
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/reinstall",
                             json={"confirm": True,
                                   "confirm_name": "web-server"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_reinstall.assert_called_once_with(ANY, FAKE_INSTANCE,
                                               image_id=None)

    @patch("web.routes.instances.reinstall_instance")
    @patch("web.routes.instances.get_instance")
    def test_reinstall_with_image_id(self, mock_get, mock_reinstall):
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "web-server"}}
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/reinstall",
                             json={"confirm": True,
                                   "confirm_name": "web-server",
                                   "image_id": FAKE_IMAGE})
        self.assertEqual(r.status_code, 200)
        mock_reinstall.assert_called_once_with(ANY, FAKE_INSTANCE,
                                               image_id=FAKE_IMAGE)

    @patch("web.routes.instances.reinstall_instance")
    def test_reinstall_without_confirm_400(self, mock_reinstall):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/reinstall",
                             json={"confirm_name": "web-server"})
        self.assertEqual(r.status_code, 400)
        mock_reinstall.assert_not_called()

    @patch("web.routes.instances.reinstall_instance")
    @patch("web.routes.instances.get_instance")
    def test_reinstall_name_mismatch_400(self, mock_get, mock_reinstall):
        """确认名与实例名不符时返回 400，不执行重装。"""
        mock_get.return_value = {"data": {"id": FAKE_INSTANCE,
                                          "display-name": "web-server"}}
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/reinstall",
                             json={"confirm": True,
                                   "confirm_name": "wrong-name"})
        self.assertEqual(r.status_code, 400)
        mock_reinstall.assert_not_called()

    @patch("web.routes.instances.get_instance",
           side_effect=NotFoundError("not found"))
    def test_reinstall_not_found_404(self, mock_get):
        r = self.client.post(f"/api/instances/{FAKE_INSTANCE}/reinstall",
                             json={"confirm": True,
                                   "confirm_name": "web-server"})
        self.assertEqual(r.status_code, 404)


class TestConsoleApi(Phase5WebTestCase):
    """GET /api/instances/{id}/console。"""

    @patch("web.routes.instances.get_serial_console")
    def test_console_ok(self, mock_console):
        mock_console.return_value = {
            "id": "ocid1.instanceconsoleconnection.oc1..test",
            "connection-string": "ssh -i key test@instance-console",
            "fingerprint": "aa:bb:cc",
            "lifecycle-state": "ACTIVE",
        }
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/console")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("ssh -i key", body["console"]["connection-string"])
        self.assertEqual(body["console"]["lifecycle-state"], "ACTIVE")
        mock_console.assert_called_once_with(ANY, FAKE_INSTANCE)

    @patch("web.routes.instances.get_serial_console",
           side_effect=NotFoundError("not found"))
    def test_console_not_found_404(self, mock_console):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/console")
        self.assertEqual(r.status_code, 404)

    def test_console_unauthenticated_401(self):
        r = self.anon.get(f"/api/instances/{FAKE_INSTANCE}/console")
        self.assertEqual(r.status_code, 401)


class TestVpusApi(Phase5WebTestCase):
    """POST /api/volumes/{id}/vpus。"""

    @patch("web.routes.volumes.update_boot_volume_vpus")
    def test_update_vpus_ok(self, mock_update):
        mock_update.return_value = {"id": FAKE_VOLUME, "vpus-per-gb": 20}
        r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/vpus",
                             json={"vpus_per_gb": 20})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["volume"]["vpus-per-gb"], 20)
        mock_update.assert_called_once_with(ANY, FAKE_VOLUME, 20)

    @patch("web.routes.volumes.update_boot_volume_vpus",
           side_effect=ValueError("vpus_per_gb 必须是 (10, 20, 30) 之一"))
    def test_update_vpus_invalid_400(self, mock_update):
        r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/vpus",
                             json={"vpus_per_gb": 15})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.volumes.update_boot_volume_vpus",
           side_effect=NotFoundError("not found"))
    def test_update_vpus_not_found_404(self, mock_update):
        r = self.client.post(f"/api/volumes/{FAKE_VOLUME}/vpus",
                             json={"vpus_per_gb": 20})
        self.assertEqual(r.status_code, 404)


class TestA1HealthApi(Phase5WebTestCase):
    """GET /api/quota/a1-health。"""

    @patch("web.routes.quota.a1_health_check", return_value=FAKE_HEALTH)
    def test_a1_health_ok(self, mock_health):
        r = self.client.get("/api/quota/a1-health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["health"], FAKE_HEALTH)
        mock_health.assert_called_once()

    def test_a1_health_unauthenticated_401(self):
        r = self.anon.get("/api/quota/a1-health")
        self.assertEqual(r.status_code, 401)

    def test_a1_health_unknown_profile_400(self):
        r = self.client.get("/api/quota/a1-health?profile=NOPE")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
