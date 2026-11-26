#!/usr/bin/env python3
"""Web 面板救机接口测试（v2.01）：前置检查 / 安全恢复 / 无盘救机 /
孤儿盘 / Netboot 向导 / 控制台连接 / 救机页。

运行：python3 -m unittest tests.test_web_rescue -v

注意：
  - TestClient + mock oci 函数，不碰真实 API。
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 全部占位符，不出现真实 OCID / IP。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.volumes import clear_volumes_cache
from web.routes.boot_backups import clear_boot_backups_cache
from web.routes.objectstorage import clear_objectstorage_cache
from web.routes.quota import clear_quota_cache
from web.tasks import TASKS

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_CONNECTION = "ocid1.instanceconsoleconnection.oc1..testxxxx"

TEST_PASSWORD = "TestPass123"

FAKE_PREFLIGHT = {
    "action": "start",
    "can_do": False,
    "blockers": ["实例没有挂载启动盘，无法启动（OCI 409）。"],
    "warnings": [],
    "facts": {"instance_id": FAKE_INSTANCE,
              "instance_name": "test-instance",
              "instance_state": "STOPPED",
              "has_boot_volume": False,
              "boot_volume_id": None,
              "used_gb": 141,
              "remaining_gb": 59,
              "is_backup_source": False,
              "backup_count": 0},
}

FAKE_PLAN = {
    "instance_id": FAKE_INSTANCE,
    "instance_name": "test-instance",
    "instance_state": "STOPPED",
    "has_boot_volume": False,
    "backups": [{"id": FAKE_BACKUP, "display-name": "test-backup",
                 "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
                 "lifecycle-state": "AVAILABLE"}],
    "can_rebuild": True,
    "message": "实例「test-instance」没有挂载启动盘，原实例无法原地救回。",
    "cost_note": "代价：新实例有新的 OCID 与新的公网 IP。",
}

FAKE_ORPHANS = [{
    "id": FAKE_BV,
    "display-name": "orphan-volume",
    "size-in-gbs": 47,
    "lifecycle-state": "AVAILABLE",
    "availability-domain": "FAKE:AP-SINGAPORE-1-AD-1",
    "source-backup-id": FAKE_BACKUP,
}]

FAKE_CONNECTION_DETAIL = {
    "id": FAKE_CONNECTION,
    "connection-string": "ssh -o ProxyCommand=...",
    "fingerprint": "aa:bb",
    "service-host-key-fingerprint": "cc:dd",
    "lifecycle-state": "ACTIVE",
}


class RescueWebTestCase(unittest.TestCase):
    """API 测试基类：建 app、登录（同 test_web_boot_swap 模式）。"""

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
        clear_volumes_cache()
        clear_quota_cache()
        clear_boot_backups_cache()
        clear_objectstorage_cache()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        self.client = TestClient(self.app)
        self._login()

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=None):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password or TEST_PASSWORD})

    def _resolve(self):
        return patch("web.routes.rescue.resolve_profile",
                     return_value=(MagicMock(), "ocid1.compartment.oc1..x"))


class TestPreflightApi(RescueWebTestCase):
    """GET /api/rescue/preflight。"""

    def test_ok(self):
        with self._resolve(), \
                patch("web.routes.rescue.preflight_check",
                      return_value=FAKE_PREFLIGHT) as mock_check:
            r = self.client.get(
                f"/api/rescue/preflight?instance_id={FAKE_INSTANCE}"
                "&action=start")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertFalse(body["can_do"])
            self.assertEqual(body["facts"]["remaining_gb"], 59)
            mock_check.assert_called_once()

    def test_missing_instance_id_400(self):
        with self._resolve():
            r = self.client.get("/api/rescue/preflight?action=start")
            self.assertEqual(r.status_code, 400)

    def test_unknown_action_400(self):
        with self._resolve():
            r = self.client.get(
                f"/api/rescue/preflight?instance_id={FAKE_INSTANCE}"
                "&action=nope")
            self.assertEqual(r.status_code, 400)
            self.assertIn("未知操作", r.json()["error"])

    def test_requires_login(self):
        self.client.cookies.clear()
        r = self.client.get(
            f"/api/rescue/preflight?instance_id={FAKE_INSTANCE}"
            "&action=start")
        self.assertEqual(r.status_code, 401)


class TestSafeRestoreApi(RescueWebTestCase):
    """POST /api/rescue/safe-restore。"""

    def test_requires_confirm(self):
        with self._resolve():
            r = self.client.post(
                "/api/rescue/safe-restore",
                json={"instance_id": FAKE_INSTANCE,
                      "backup_id": FAKE_BACKUP, "confirm": False})
            self.assertEqual(r.status_code, 400)
            self.assertIn("确认", r.json()["error"])

    def test_ok_passes_allow_flag(self):
        with self._resolve(), \
                patch("web.routes.rescue.safe_restore",
                      return_value={"old_boot_volume_id": FAKE_BV,
                                    "new_boot_volume_id": "ocid1.bootvolume.oc1..new",
                                    "old_volume_deleted": True,
                                    "warnings": []}) as mock_restore:
            r = self.client.post(
                "/api/rescue/safe-restore",
                json={"instance_id": FAKE_INSTANCE,
                      "backup_id": FAKE_BACKUP, "confirm": True,
                      "allow_delete_backup_source": True})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["old_volume_deleted"])
            self.assertTrue(mock_restore.call_args.kwargs
                            .get("allow_delete_backup_source"))

    def test_quota_error_surfaces_as_400(self):
        with self._resolve(), \
                patch("web.routes.rescue.safe_restore",
                      side_effect=ValueError("启动盘额度不足")):
            r = self.client.post(
                "/api/rescue/safe-restore",
                json={"instance_id": FAKE_INSTANCE,
                      "backup_id": FAKE_BACKUP, "confirm": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("额度不足", r.json()["error"])


class TestDisklessApi(RescueWebTestCase):
    """GET/POST /api/rescue/diskless/{instance_id}。"""

    def test_plan(self):
        with self._resolve(), \
                patch("web.routes.rescue.plan_diskless_rescue",
                      return_value=FAKE_PLAN):
            r = self.client.get(f"/api/rescue/diskless/{FAKE_INSTANCE}")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["can_rebuild"])
            self.assertIn("无法原地救回", body["message"])
            self.assertEqual(len(body["backups"]), 1)

    def test_rebuild_requires_confirm(self):
        with self._resolve():
            r = self.client.post(
                f"/api/rescue/diskless/{FAKE_INSTANCE}/rebuild",
                json={"backup_id": FAKE_BACKUP, "confirm": False})
            self.assertEqual(r.status_code, 400)

    def test_rebuild_ok(self):
        with self._resolve(), \
                patch("web.routes.rescue.get_boot_volume_backup",
                      return_value={"id": FAKE_BACKUP,
                                    "display-name": "test-backup",
                                    "boot-volume-id": FAKE_BV,
                                    "size-in-gbs": 47,
                                    "lifecycle-state": "AVAILABLE"}), \
                patch("web.routes.rescue.rebuild_diskless_from_backup",
                      return_value={"instance_id": "ocid1.instance.oc1..new",
                                    "boot_volume_id": "ocid1.bootvolume.oc1..new",
                                    "shape": "VM.Standard.A1.Flex",
                                    "ocpus": 1,
                                    "memory_gb": 6}) as mock_rebuild:
            r = self.client.post(
                f"/api/rescue/diskless/{FAKE_INSTANCE}/rebuild",
                json={"backup_id": FAKE_BACKUP,
                      "display_name": "rescued",
                      "availability_domain": "FAKE:AP-SINGAPORE-1-AD-1",
                      "subnet_id": "ocid1.subnet.oc1..testxxxx",
                      "confirm": True})
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertIn("新的 OCID", body["cost_note"])
            mock_rebuild.assert_called_once()

    def test_rebuild_missing_ad_400(self):
        with self._resolve(), \
                patch("web.routes.rescue.get_boot_volume_backup",
                      return_value={"id": FAKE_BACKUP,
                                    "display-name": "test-backup",
                                    "boot-volume-id": FAKE_BV,
                                    "size-in-gbs": 47,
                                    "lifecycle-state": "AVAILABLE"}):
            r = self.client.post(
                f"/api/rescue/diskless/{FAKE_INSTANCE}/rebuild",
                json={"backup_id": FAKE_BACKUP, "confirm": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("可用域", r.json()["error"])


class TestOrphansApi(RescueWebTestCase):
    """GET /api/rescue/orphans 与删除。"""

    def test_list_with_total(self):
        with self._resolve(), \
                patch("web.routes.rescue.list_rescue_orphans",
                      return_value=FAKE_ORPHANS):
            r = self.client.get("/api/rescue/orphans")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["count"], 1)
            self.assertEqual(body["total_gb"], 47)
            self.assertEqual(body["orphans"][0]["volumeId"], FAKE_BV)

    def test_delete_requires_confirm(self):
        with self._resolve():
            r = self.client.post(f"/api/rescue/orphans/{FAKE_BV}/delete",
                                 json={"confirm": False})
            self.assertEqual(r.status_code, 400)

    def test_delete_ok(self):
        with self._resolve(), \
                patch("web.routes.rescue.delete_boot_volume_guarded",
                      return_value={"deleted": True,
                                    "was_backup_source": False}) as mock_del:
            r = self.client.post(f"/api/rescue/orphans/{FAKE_BV}/delete",
                                 json={"confirm": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            self.assertTrue(mock_del.call_args.kwargs.get("confirm"))

    def test_delete_blocked_surfaces_as_400(self):
        with self._resolve(), \
                patch("web.routes.rescue.delete_boot_volume_guarded",
                      side_effect=ValueError("已拦截：该启动盘是 1 个备份的源盘")):
            r = self.client.post(f"/api/rescue/orphans/{FAKE_BV}/delete",
                                 json={"confirm": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("拦截", r.json()["error"])

    def test_delete_check(self):
        with self._resolve(), \
                patch("web.routes.rescue.check_delete_volume_safety",
                      return_value={"is_backup_source": True,
                                    "backups": [{"id": FAKE_BACKUP}],
                                    "warning": "该启动盘是 1 个备份的源盘"}):
            r = self.client.get(
                f"/api/rescue/volumes/{FAKE_BV}/delete-check")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["is_backup_source"])


class TestNetbootGuideApi(RescueWebTestCase):
    """GET /api/rescue/netboot-guide。"""

    def test_guide_contents(self):
        r = self.client.get(
            "/api/rescue/netboot-guide?instance_id=" + FAKE_INSTANCE)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("FS0:", body["efi_commands"])
        self.assertEqual(body["tftp_server"], "203.0.113.10")
        self.assertTrue(any("Alpine" in s["text"]
                            for s in body["menu_steps"]))

    def test_missing_instance_id_400(self):
        r = self.client.get("/api/rescue/netboot-guide")
        self.assertEqual(r.status_code, 400)


class TestConsoleApi(RescueWebTestCase):
    """控制台连接 API。"""

    def test_list(self):
        with self._resolve(), \
                patch("web.routes.rescue.rescue_list_consoles",
                      return_value=[{"id": FAKE_CONNECTION,
                                     "instance-id": FAKE_INSTANCE,
                                     "lifecycle-state": "ACTIVE"}]):
            r = self.client.get("/api/rescue/console")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.json()["connections"]), 1)

    def test_create_requires_confirm(self):
        with self._resolve():
            r = self.client.post("/api/rescue/console",
                                 json={"instance_id": FAKE_INSTANCE,
                                       "confirm": False})
            self.assertEqual(r.status_code, 400)

    def test_create_ok(self):
        with self._resolve(), \
                patch("web.routes.rescue.rescue_create_console",
                      return_value=FAKE_CONNECTION_DETAIL):
            r = self.client.post(
                "/api/rescue/console",
                json={"instance_id": FAKE_INSTANCE, "confirm": True})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["connection"]["lifecycle-state"],
                             "ACTIVE")

    def test_create_ed25519_error_surfaces_as_400(self):
        with self._resolve(), \
                patch("web.routes.rescue.rescue_create_console",
                      side_effect=ValueError(
                          "串行控制台只接受 RSA 密钥（ssh-rsa）")):
            r = self.client.post(
                "/api/rescue/console",
                json={"instance_id": FAKE_INSTANCE, "confirm": True})
            self.assertEqual(r.status_code, 400)
            self.assertIn("RSA", r.json()["error"])

    def test_delete(self):
        with self._resolve(), \
                patch("web.routes.rescue.rescue_delete_console",
                      return_value=None) as mock_del:
            r = self.client.delete(f"/api/rescue/console/{FAKE_CONNECTION}")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])
            mock_del.assert_called_once()


class TestRescuePage(RescueWebTestCase):
    """GET /rescue 页面与导航。"""

    def test_page_renders(self):
        r = self.client.get("/rescue")
        self.assertEqual(r.status_code, 200)
        self.assertIn("救机", r.text)
        self.assertIn("/static/js/rescue.js", r.text)

    def test_page_requires_login(self):
        self.client.cookies.clear()
        r = self.client.get("/rescue", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])

    def test_nav_link_present(self):
        r = self.client.get("/rescue")
        self.assertIn('href="/rescue"', r.text)


if __name__ == "__main__":
    unittest.main()
