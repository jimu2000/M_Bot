#!/usr/bin/env python3
"""Web 面板启动卷备份接口测试（计划 v1.7.0）：配额、列表、创建、
删除、从备份恢复。

运行：python3 -m unittest tests.test_web_boot_backup -v

注意：
  - TestClient + mock oci 函数，不碰真实 API；oci 层测试用 MagicMock cli。
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 全部占位符，不出现真实 OCID / IP。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import ANY, MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError
from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.instances import clear_instance_list_cache
from web.routes.volumes import clear_volumes_cache
from web.tasks import TASKS

# 占位符（测试里不出现真实 OCID / IP）
FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_OLD_BACKUP = "ocid1.bootvolumebackup.oc1..oldxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1..testxxxx"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"
FAKE_SSH_KEYS = "/tmp/test-keys.pub"

TEST_PASSWORD = "test-password-only"

FAKE_BACKUPS = [
    {"id": FAKE_OLD_BACKUP, "display-name": "old-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-20T10:00:00+00:00",
     "image-id": "ocid1.image.oc1..testxxxx"},
    {"id": FAKE_BACKUP, "display-name": "new-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 50,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-23T10:00:00+00:00",
     "image-id": "ocid1.image.oc1..testxxxx"},
]

FAKE_PLAN_DEFAULTS = {
    "availability_domain": FAKE_AD,
    "subnet_id": FAKE_SUBNET,
    "ssh_keys_file": FAKE_SSH_KEYS,
    "shape": "VM.Standard.A1.Flex",
    "ocpus": 1,
    "memory_gb": 6,
    "boot_volume_size_gbs": 50,
    "interval_seconds": 25,
    "max_attempts": 0,
    "target_cpu": "",
}


class BootBackupTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录（同 test_web_api 模式）。"""

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
        clear_instance_list_cache()
        clear_volumes_cache()
        clear_cloud_report_cache()
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


class TestQuotaApi(BootBackupTestCase):
    """GET /api/boot-backups/quota。"""

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_quota_not_full(self, mock_quota):
        r = self.client.get("/api/boot-backups/quota")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["limit"], 5)
        self.assertFalse(body["full"])
        self.assertEqual(body["oldest"]["name"], "old-backup")
        self.assertEqual(body["oldest"]["id"], FAKE_OLD_BACKUP)

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(5, 5, True, FAKE_BACKUPS[0]))
    def test_quota_full(self, mock_quota):
        r = self.client.get("/api/boot-backups/quota")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["full"])
        self.assertEqual(body["count"], 5)

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(0, 5, False, None))
    def test_quota_empty(self, mock_quota):
        r = self.client.get("/api/boot-backups/quota")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["oldest"])

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=True)
    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(7, 5, True, FAKE_BACKUPS[0]))
    def test_quota_paid_unlimited(self, mock_quota, mock_paid):
        r = self.client.get("/api/boot-backups/quota")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["paid"])
        self.assertIsNone(body["limit"])
        self.assertFalse(body["full"])
        self.assertEqual(body["count"], 7)

    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, None))
    @patch("web.routes.boot_backups.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_quota_passes_profile(self, mock_resolve, mock_quota):
        r = self.client.get("/api/boot-backups/quota?profile=ACCOUNT2")
        self.assertEqual(r.status_code, 200)
        mock_resolve.assert_called_once_with("ACCOUNT2", None)


class TestPaidApi(BootBackupTestCase):
    """POST /api/boot-backups/paid：设置账号付费/免费。"""

    @patch("web.routes.boot_backups.set_paid_profile")
    def test_set_paid(self, mock_set):
        r = self.client.post("/api/boot-backups/paid",
                             json={"paid": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["paid"])
        mock_set.assert_called_once_with("DEFAULT", True)

    @patch("web.routes.boot_backups.set_paid_profile")
    @patch("web.routes.boot_backups.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_set_free_with_profile(self, mock_resolve, mock_set):
        r = self.client.post("/api/boot-backups/paid?profile=ACCOUNT3",
                             json={"paid": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["paid"])
        mock_set.assert_called_once_with("ACCOUNT3", False)

    @patch("web.routes.boot_backups.resolve_profile",
           side_effect=ValueError("未知账号"))
    def test_set_paid_unknown_profile_400(self, mock_resolve):
        r = self.client.post("/api/boot-backups/paid?profile=NOPE",
                             json={"paid": True})
        self.assertEqual(r.status_code, 400)


class TestListApi(BootBackupTestCase):
    """GET /api/boot-backups。"""

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    def test_list(self, mock_list):
        r = self.client.get("/api/boot-backups")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["backups"]), 2)
        first = body["backups"][0]
        # camelCase 新字段 + kebab-case 旧字段双写。
        self.assertEqual(first["backupId"], FAKE_OLD_BACKUP)
        self.assertEqual(first["displayName"], "old-backup")
        self.assertEqual(first["sizeInGBs"], 47)
        self.assertEqual(first["lifecycleState"], "AVAILABLE")
        self.assertEqual(first["id"], FAKE_OLD_BACKUP)
        self.assertEqual(first["display-name"], "old-backup")

    @patch("web.routes.boot_backups.list_boot_volume_backups",
           side_effect=NotFoundError("not found"))
    def test_list_oci_error_404(self, mock_list):
        r = self.client.get("/api/boot-backups")
        self.assertEqual(r.status_code, 404)


class TestCreateApi(BootBackupTestCase):
    """POST /api/boot-backups：满了自动删最旧的再建，付费账号不限。"""

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=False)
    @patch("web.routes.boot_backups.create_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_create_when_not_full(self, mock_quota, mock_create,
                                  _mock_paid):
        r = self.client.post("/api/boot-backups",
                             json={"boot_volume_id": FAKE_BV})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["backup"]["backupId"], FAKE_BACKUP)
        self.assertNotIn("deleted_oldest", body)
        mock_create.assert_called_once_with(ANY, FAKE_BV, None)

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=False)
    @patch("web.routes.boot_backups.create_boot_volume_backup_with_limit_retry",
           return_value=FAKE_BACKUPS[1])
    @patch("web.routes.boot_backups.delete_boot_volume_backup")
    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(5, 5, True, FAKE_BACKUPS[0]))
    def test_create_when_full_auto_deletes_oldest(
            self, mock_quota, mock_delete, mock_create, _mock_paid):
        # 不用传 delete_oldest，服务端自动删最早备份再建。
        r = self.client.post("/api/boot-backups",
                             json={"boot_volume_id": FAKE_BV})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted_oldest"]["id"], FAKE_OLD_BACKUP)
        self.assertEqual(body["deleted_oldest"]["name"], "old-backup")
        mock_delete.assert_called_once_with(ANY, FAKE_OLD_BACKUP)
        mock_create.assert_called_once_with(ANY, FAKE_BV, None)

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=True)
    @patch("web.routes.boot_backups.create_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    @patch("web.routes.boot_backups.check_backup_quota")
    def test_create_when_paid_skips_quota(self, mock_quota, mock_create,
                                          _mock_paid):
        r = self.client.post("/api/boot-backups",
                             json={"boot_volume_id": FAKE_BV})
        self.assertEqual(r.status_code, 200)
        mock_quota.assert_not_called()
        mock_create.assert_called_once_with(ANY, FAKE_BV, None)

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=False)
    @patch("web.routes.boot_backups.create_boot_volume_backup_with_limit_retry",
           return_value=FAKE_BACKUPS[1])
    @patch("web.routes.boot_backups.delete_boot_volume_backup")
    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(5, 5, True, FAKE_BACKUPS[0]))
    def test_create_delete_oldest_deletes_first_then_creates(
            self, mock_quota, mock_delete, mock_create, _mock_paid):
        # 用共享 manager 校验调用顺序：先删最早备份，再创建新备份。
        manager = MagicMock()
        manager.attach_mock(mock_delete, "delete")
        manager.attach_mock(mock_create, "create")
        r = self.client.post("/api/boot-backups",
                             json={"boot_volume_id": FAKE_BV,
                                   "delete_oldest": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted_oldest"]["id"], FAKE_OLD_BACKUP)
        self.assertEqual(body["deleted_oldest"]["name"], "old-backup")
        self.assertEqual(
            manager.mock_calls,
            [call.delete(ANY, FAKE_OLD_BACKUP),
             call.create(ANY, FAKE_BV, None)])

    @patch("web.routes.boot_backups.is_paid_profile",
           return_value=False)
    @patch("web.routes.boot_backups.create_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    @patch("web.routes.boot_backups.check_backup_quota",
           return_value=(2, 5, False, FAKE_BACKUPS[0]))
    def test_create_with_display_name(self, mock_quota, mock_create,
                                      _mock_paid):
        r = self.client.post("/api/boot-backups",
                             json={"boot_volume_id": FAKE_BV,
                                   "display_name": "my-backup"})
        self.assertEqual(r.status_code, 200)
        mock_create.assert_called_once_with(ANY, FAKE_BV, "my-backup")


class TestDeleteApi(BootBackupTestCase):
    """DELETE /api/boot-backups/{id}。"""

    @patch("web.routes.boot_backups.delete_boot_volume_backup")
    def test_delete_requires_confirm(self, mock_delete):
        r = self.client.delete(f"/api/boot-backups/{FAKE_BACKUP}")
        self.assertEqual(r.status_code, 400)
        mock_delete.assert_not_called()

    @patch("web.routes.boot_backups.delete_boot_volume_backup")
    def test_delete_success(self, mock_delete):
        r = self.client.delete(
            f"/api/boot-backups/{FAKE_BACKUP}?confirm=true")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_delete.assert_called_once_with(ANY, FAKE_BACKUP)


class TestDeleteBatchApi(BootBackupTestCase):
    """POST /api/boot-backups/delete-batch：批量删除。"""

    @patch("web.routes.boot_backups.delete_boot_volume_backup")
    def test_delete_batch_all(self, mock_delete):
        r = self.client.post("/api/boot-backups/delete-batch",
                             json={"backup_ids": [FAKE_OLD_BACKUP,
                                                  FAKE_BACKUP]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"],
                         [FAKE_OLD_BACKUP, FAKE_BACKUP])
        self.assertEqual(body["failed"], [])
        self.assertEqual(mock_delete.call_count, 2)

    @patch("web.routes.boot_backups.delete_boot_volume_backup",
           side_effect=[None, NotFoundError("gone")])
    def test_delete_batch_partial_failure(self, mock_delete):
        r = self.client.post("/api/boot-backups/delete-batch",
                             json={"backup_ids": [FAKE_OLD_BACKUP,
                                                  FAKE_BACKUP]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["deleted"], [FAKE_OLD_BACKUP])
        self.assertEqual(len(body["failed"]), 1)
        self.assertEqual(body["failed"][0]["id"], FAKE_BACKUP)

    def test_delete_batch_empty_400(self):
        r = self.client.post("/api/boot-backups/delete-batch",
                             json={"backup_ids": []})
        self.assertEqual(r.status_code, 400)


class TestRestoreApi(BootBackupTestCase):
    """POST /api/boot-backups/{id}/restore。"""

    @patch("web.routes.boot_backups.restore_boot_volume_backup")
    def test_restore_requires_confirm(self, mock_restore):
        r = self.client.post(f"/api/boot-backups/{FAKE_BACKUP}/restore",
                             json={})
        self.assertEqual(r.status_code, 400)
        mock_restore.assert_not_called()

    @patch("web.routes.boot_backups.PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch("web.routes.boot_backups.restore_boot_volume_backup")
    @patch("web.routes.boot_backups.can_launch_batch",
           return_value=(False, "A1 配额不足"))
    @patch("web.routes.boot_backups.get_source_instance_shape",
           return_value=("VM.Standard.A1.Flex", 2, 12))
    @patch("web.routes.boot_backups.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    def test_restore_can_launch_blocks(self, mock_get, mock_shape,
                                       mock_can, mock_restore):
        """can_launch 预检不通过时返回 400，不调用恢复。"""
        r = self.client.post(f"/api/boot-backups/{FAKE_BACKUP}/restore",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("配额预检不通过", r.json()["error"])
        mock_restore.assert_not_called()
        # 启动盘按备份实际大小（50GB）校验，不是默认 47GB。
        self.assertEqual(
            mock_can.call_args.kwargs.get("boot_volume_size_gbs"), 50)

    @patch("web.routes.boot_backups.PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch("web.routes.boot_backups.restore_boot_volume_backup")
    @patch("web.routes.boot_backups.can_launch_batch",
           return_value=(True, "ok"))
    @patch("web.routes.boot_backups.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    def test_restore_a1_cap_blocks(self, mock_get, mock_can, mock_restore):
        """A1 超单台上限（8 核）时返回 400，不调用恢复。"""
        r = self.client.post(
            f"/api/boot-backups/{FAKE_BACKUP}/restore",
            json={"confirm": True, "shape": "VM.Standard.A1.Flex",
                  "ocpus": 8, "memory_gb": 48})
        self.assertEqual(r.status_code, 400)
        self.assertIn("4 OCPU", r.json()["error"])
        mock_restore.assert_not_called()

    @patch("web.routes.boot_backups.PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch("web.routes.boot_backups.restore_boot_volume_backup",
           return_value={"instance_id": FAKE_INSTANCE,
                         "boot_volume_id": FAKE_BV,
                         "shape": "VM.Standard.A1.Flex",
                         "ocpus": 2, "memory_gb": 12})
    @patch("web.routes.boot_backups.can_launch_batch",
           return_value=(True, "ok"))
    @patch("web.routes.boot_backups.get_source_instance_shape",
           return_value=("VM.Standard.A1.Flex", 2, 12))
    @patch("web.routes.boot_backups.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    def test_restore_success_auto_shape(self, mock_get, mock_shape,
                                        mock_can, mock_restore):
        """不传形状时用来源实例形状；新实例名默认 <备份名>-restored。"""
        r = self.client.post(f"/api/boot-backups/{FAKE_BACKUP}/restore",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["instance_id"], FAKE_INSTANCE)
        self.assertEqual(body["shape"], "VM.Standard.A1.Flex")
        kwargs = mock_restore.call_args.kwargs
        self.assertEqual(kwargs["display_name"], "new-backup-restored")
        self.assertEqual(kwargs["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(kwargs["ocpus"], 2)
        self.assertEqual(kwargs["memory_gb"], 12)
        self.assertEqual(kwargs["availability_domain"], FAKE_AD)
        self.assertEqual(kwargs["subnet_id"], FAKE_SUBNET)
        self.assertEqual(kwargs["ssh_keys_file"], FAKE_SSH_KEYS)

    @patch("web.routes.boot_backups.PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch("web.routes.boot_backups.restore_boot_volume_backup",
           return_value={"instance_id": FAKE_INSTANCE,
                         "boot_volume_id": FAKE_BV,
                         "shape": "VM.Standard.E2.1.Micro",
                         "ocpus": None, "memory_gb": None})
    @patch("web.routes.boot_backups.can_launch_batch",
           return_value=(True, "ok"))
    @patch("web.routes.boot_backups.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    def test_restore_success_explicit_e2(self, mock_get, mock_can,
                                         mock_restore):
        """显式指定 E2 形状时不查来源形状，直接用 E2。"""
        r = self.client.post(
            f"/api/boot-backups/{FAKE_BACKUP}/restore",
            json={"confirm": True, "shape": "VM.Standard.E2.1.Micro",
                  "display_name": "e2-restored"})
        self.assertEqual(r.status_code, 200)
        kwargs = mock_restore.call_args.kwargs
        self.assertEqual(kwargs["shape"], "VM.Standard.E2.1.Micro")
        self.assertEqual(kwargs["display_name"], "e2-restored")

    @patch("web.routes.boot_backups.PLAN_DEFAULTS",
           {**FAKE_PLAN_DEFAULTS,
            "availability_domain": "xxxx:AP-SINGAPORE-1-AD-1"})
    @patch("web.routes.boot_backups.restore_boot_volume_backup")
    @patch("web.routes.boot_backups.can_launch_batch",
           return_value=(True, "ok"))
    @patch("web.routes.boot_backups.get_source_instance_shape",
           return_value=("VM.Standard.A1.Flex", 1, 6))
    @patch("web.routes.boot_backups.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[1])
    def test_restore_placeholder_ad_400(self, mock_get, mock_shape,
                                        mock_can, mock_restore):
        """AD 仍是占位符时返回 400 提示配置，不调用恢复。"""
        r = self.client.post(f"/api/boot-backups/{FAKE_BACKUP}/restore",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("可用域", r.json()["error"])
        mock_restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
