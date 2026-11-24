#!/usr/bin/env python3
"""Web 面板实例引导卷备份接口测试（计划 v1.18.0）：创建备份、满 5 提示、
删除列表（include_all）。

运行：python3 -m unittest tests.test_web_boot_backup_instance -v

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
from web.routes.instances import clear_instance_list_cache
from web.tasks import TASKS

# 占位符（测试里不出现真实 OCID / IP）
FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_ATTACHMENT = "ocid1.bootvolumeattachment.oc1..testxxxx"

TEST_PASSWORD = "test" + "-pw-" + "5678"

FAKE_ATTACHMENT = {"id": FAKE_ATTACHMENT, "boot-volume-id": FAKE_BV,
                   "instance-id": FAKE_INSTANCE,
                   "lifecycle-state": "ATTACHED"}

FAKE_BACKUPS = [
    {"id": FAKE_BACKUP, "display-name": "old-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-20T10:00:00+00:00"},
    {"id": "ocid1.bootvolumebackup.oc1..creating",
     "display-name": "creating-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
     "lifecycle-state": "CREATING", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-23T10:00:00+00:00"},
]


class BootBackupInstanceWebTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录（同 test_web_boot_swap 模式）。"""

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


class TestBootBackupsIncludeAll(BootBackupInstanceWebTestCase):
    """GET /api/instances/{id}/boot-backups?include_all=true。"""

    @patch("web.routes.instances.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_include_all_returns_creating_too(self, _mock_resolve,
                                              _mock_att, _mock_list):
        r = self.client.get(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups"
            "?include_all=true")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        # 全部未删除备份都返回（CREATING 的也含，删除列表用）。
        self.assertEqual(len(body["backups"]), 2)
        states = {b["lifecycle-state"] for b in body["backups"]}
        self.assertEqual(states, {"AVAILABLE", "CREATING"})

    @patch("web.routes.instances.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_default_still_only_available(self, _mock_resolve, _mock_att,
                                          _mock_list):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["backups"]), 1)


class TestCreateBootBackup(BootBackupInstanceWebTestCase):
    """POST /api/instances/{id}/boot-backups。"""

    @patch("web.routes.instances.create_boot_volume_backup",
           return_value={"id": FAKE_BACKUP, "display-name": "new-backup",
                         "lifecycle-state": "CREATING"})
    @patch("web.routes.instances.check_backup_quota",
           return_value=(3, 5, False, None))
    @patch("web.routes.instances.is_paid_profile", return_value=False)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    @patch("web.routes.instances._notify_chat_id", return_value=123)
    @patch("web.routes.instances.get_instance",
           return_value={"display-name": "test-inst"})
    @patch("web.routes.instances.start_backup_watch")
    def test_free_not_full_creates(self, mock_watch, _mock_get_inst,
                                   _mock_chat, _mock_resolve, _mock_att,
                                   _mock_paid, _mock_quota, mock_create):
        r = self.client.post(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["backup"]["id"], FAKE_BACKUP)
        self.assertEqual(body["count"], 4)
        self.assertEqual(body["limit"], 5)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args.args[1], FAKE_BV)
        # 创建成功后起完成通知轮询。
        mock_watch.assert_called_once()
        self.assertEqual(mock_watch.call_args[0][1], FAKE_BACKUP)
        self.assertEqual(mock_watch.call_args[0][2], "test-inst")
        self.assertEqual(mock_watch.call_args[0][3], "new-backup")
        self.assertEqual(mock_watch.call_args[0][5], 123)

    @patch("web.routes.instances.create_boot_volume_backup")
    @patch("web.routes.instances.check_backup_quota",
           return_value=(5, 5, True, FAKE_BACKUPS[0]))
    @patch("web.routes.instances.is_paid_profile", return_value=False)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_free_full_409_no_create(self, _mock_resolve, _mock_att,
                                     _mock_paid, _mock_quota, mock_create):
        r = self.client.post(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["full"])
        self.assertEqual(body["count"], 5)
        self.assertEqual(body["limit"], 5)
        self.assertIn("先删除", body["error"])
        mock_create.assert_not_called()

    @patch("web.routes.instances.create_boot_volume_backup",
           return_value={"id": FAKE_BACKUP, "display-name": "new-backup",
                         "lifecycle-state": "CREATING"})
    @patch("web.routes.instances.check_backup_quota")
    @patch("web.routes.instances.is_paid_profile", return_value=True)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    @patch("web.routes.instances._notify_chat_id", return_value=123)
    @patch("web.routes.instances.get_instance",
           return_value={"display-name": "test-inst"})
    @patch("web.routes.instances.start_backup_watch")
    def test_paid_skips_quota_check(self, mock_watch, _mock_get_inst,
                                    _mock_chat, _mock_resolve, _mock_att,
                                    _mock_paid, mock_quota, _mock_create):
        r = self.client.post(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        mock_quota.assert_not_called()
        self.assertNotIn("count", r.json())
        mock_watch.assert_called_once()
        self.assertEqual(mock_watch.call_args[0][1], FAKE_BACKUP)

    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=None)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_no_attachment_400(self, _mock_resolve, _mock_att):
        r = self.client.post(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 400)
        self.assertIn("找不到", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
