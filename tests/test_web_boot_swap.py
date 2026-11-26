#!/usr/bin/env python3
"""Web 面板引导卷恢复接口测试（计划 v1.17.0）：备份列表、恢复换盘。

运行：python3 -m unittest tests.test_web_boot_swap -v

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
from web.routes.volumes import clear_volumes_cache
from web.routes.boot_backups import clear_boot_backups_cache
from web.routes.objectstorage import clear_objectstorage_cache
from web.routes.quota import clear_quota_cache
from web.tasks import TASKS

# Placeholders (no real OCID / IP in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_OTHER_BV = "ocid1.bootvolume.oc1..otherxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_ATTACHMENT = "ocid1.bootvolumeattachment.oc1..testxxxx"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"

TEST_PASSWORD = "test-password-for-bootswap-only"

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

FAKE_INSTANCE_DETAIL = {"data": {
    "id": FAKE_INSTANCE, "display-name": "test-instance",
    "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
    "availability-domain": FAKE_AD,
}}


class BootSwapWebTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录（同 test_web_boot_backup 模式）。"""

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


class TestBootBackupsApi(BootSwapWebTestCase):
    """GET /api/instances/{id}/boot-backups。"""

    @patch("web.routes.instances.list_boot_volume_backups",
           return_value=FAKE_BACKUPS)
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_lists_only_available(self, _mock_resolve, mock_att, mock_list):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["boot_volume_id"], FAKE_BV)
        # Only the AVAILABLE entry is returned (the CREATING one is filtered out). 只返回 AVAILABLE 的那条（CREATING 的过滤掉）。
        self.assertEqual(len(body["backups"]), 1)
        self.assertEqual(body["backups"][0]["id"], FAKE_BACKUP)
        self.assertEqual(mock_list.call_args.kwargs.get("boot_volume_id"),
                         FAKE_BV)

    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=None)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_no_attachment_400(self, _mock_resolve, _mock_att):
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 400)
        self.assertIn("找不到", r.json()["error"])

    @patch("web.routes.instances.list_boot_volume_backups",
           return_value=[])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_passes_profile_region(self, mock_resolve, _mock_att,
                                   _mock_list):
        r = self.client.get(
            f"/api/instances/{FAKE_INSTANCE}/boot-backups"
            "?profile=ACCOUNT2&region=us-sanjose-1")
        self.assertEqual(r.status_code, 200)
        mock_resolve.assert_called_once_with("ACCOUNT2", "us-sanjose-1")

    @patch("web.routes.instances.list_boot_volume_backups",
           side_effect=[[], FAKE_BACKUPS])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_empty_returns_other_available_count(self, _mock_resolve,
                                                 _mock_att, _mock_list):
        """当前盘没可用备份时，返回账号里其他盘的可用备份数。"""
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["backups"], [])
        self.assertEqual(body["other_available_count"], 1)

    @patch("web.routes.instances.list_boot_volume_backups",
           side_effect=[[], Exception("boom")])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_empty_other_lookup_failure_returns_zero(self, _mock_resolve,
                                                     _mock_att, _mock_list):
        """查旧备份失败时 other_available_count 为 0，主流程不受影响。"""
        r = self.client.get(f"/api/instances/{FAKE_INSTANCE}/boot-backups")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["backups"], [])
        self.assertEqual(body["other_available_count"], 0)


class TestRestoreBootVolumeApi(BootSwapWebTestCase):
    """POST /api/instances/{id}/restore-boot-volume。"""

    def _url(self):
        return (f"/api/instances/{FAKE_INSTANCE}/restore-boot-volume")

    def test_confirm_required(self):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": False})
        self.assertEqual(r.status_code, 400)

    def test_backup_id_required(self):
        r = self.client.post(self._url(),
                             json={"backup_id": "", "confirm": True})
        self.assertEqual(r.status_code, 400)

    @patch("web.routes.instances.get_instance",
           return_value={"data": dict(FAKE_INSTANCE_DETAIL["data"],
                                      **{"lifecycle-state": "TERMINATED"})})
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_terminated_instance_400(self, _mock_resolve, _mock_get):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("只有运行中或已停止", r.json()["error"])

    @patch("web.routes.instances.get_boot_volume_backup",
           return_value=dict(FAKE_BACKUPS[0],
                             **{"boot-volume-id": FAKE_OTHER_BV}))
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_INSTANCE_DETAIL)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_backup_belongs_to_other_volume(self, _mock_resolve,
                                            _mock_get, _mock_att,
                                            _mock_backup):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("不属于这台实例", r.json()["error"])

    @patch("web.routes.instances.get_boot_volume_backup",
           return_value=dict(FAKE_BACKUPS[0],
                             **{"lifecycle-state": "CREATING"}))
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_INSTANCE_DETAIL)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_backup_not_available(self, _mock_resolve, _mock_get,
                                  _mock_att, _mock_backup):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("AVAILABLE", r.json()["error"])

    @patch("web.routes.instances.check_boot_volume_space",
           return_value=(False, 10))
    @patch("web.routes.instances.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[0])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_INSTANCE_DETAIL)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_quota_insufficient(self, _mock_resolve, _mock_get,
                                _mock_att, _mock_backup, _mock_space):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("额度不足", r.json()["error"])

    @patch("web.routes.instances.start_bootswap_task",
           side_effect=ValueError("这台实例正在换盘，请等它完成后再试。"))
    @patch("web.routes.instances.check_boot_volume_space",
           return_value=(True, 59))
    @patch("web.routes.instances.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[0])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_INSTANCE_DETAIL)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_already_running_409(self, _mock_resolve, _mock_get,
                                 _mock_att, _mock_backup, _mock_space,
                                 _mock_start):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 409)
        self.assertIn("正在换盘", r.json()["error"])

    @patch("web.routes.instances.start_bootswap_task",
           return_value="b1")
    @patch("web.routes.instances.check_boot_volume_space",
           return_value=(True, 59))
    @patch("web.routes.instances.get_boot_volume_backup",
           return_value=FAKE_BACKUPS[0])
    @patch("web.routes.instances.get_boot_volume_attachment",
           return_value=FAKE_ATTACHMENT)
    @patch("web.routes.instances.get_instance",
           return_value=FAKE_INSTANCE_DETAIL)
    @patch("web.routes.instances.resolve_profile",
           return_value=(MagicMock(), "ocid1.compartment.oc1..testxxxx"))
    def test_success_starts_task(self, _mock_resolve, _mock_get,
                                 _mock_att, _mock_backup, _mock_space,
                                 mock_start):
        r = self.client.post(self._url(),
                             json={"backup_id": FAKE_BACKUP,
                                   "confirm": True})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["task_id"], "b1")
        mock_start.assert_called_once()
        args = mock_start.call_args.args
        self.assertEqual(args[3], FAKE_INSTANCE)
        self.assertEqual(args[5], FAKE_BACKUP)


class TestBootswapTaskRunner(unittest.TestCase):
    """web.tasks._run_bootswap / start_bootswap_task：任务状态与锁。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir.start()

    def tearDown(self):
        self._data_dir.stop()
        self._tmp.cleanup()
        from web import tasks as tasks_module
        tasks_module._BOOTSWAP_RUNNING.clear()

    def test_run_success_sets_done(self):
        from web.tasks import TaskRegistry, _run_bootswap
        registry = TaskRegistry()
        task_id = registry.create("bootswap", detail="引导卷恢复：test")
        cli = MagicMock()
        with patch("web.tasks.swap_boot_volume",
                   return_value={"old_boot_volume_id": FAKE_BV,
                                 "new_boot_volume_id":
                                     "ocid1.bootvolume.oc1..newxxxx",
                                 "rolled_back": False,
                                 "was_running": True,
                                 "old_volume_deleted": True}):
            _run_bootswap(registry, task_id, cli,
                          "ocid1.compartment.oc1..testxxxx",
                          FAKE_INSTANCE, "test-instance",
                          FAKE_BACKUP, "old-backup", notify=None)
        task = registry.get(task_id)
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["result"]["instance_id"], FAKE_INSTANCE)
        self.assertTrue(task["result"]["old_volume_deleted"])
        self.assertTrue(any("已删除，额度已释放" in log
                            for log in task["logs"]))

    def test_run_old_volume_delete_failed_warns(self):
        """删旧盘失败时任务仍 done，日志里警告手动删除。"""
        from web.tasks import TaskRegistry, _run_bootswap
        registry = TaskRegistry()
        task_id = registry.create("bootswap", detail="引导卷恢复：test")
        cli = MagicMock()
        with patch("web.tasks.swap_boot_volume",
                   return_value={"old_boot_volume_id": FAKE_BV,
                                 "new_boot_volume_id":
                                     "ocid1.bootvolume.oc1..newxxxx",
                                 "rolled_back": False,
                                 "was_running": True,
                                 "old_volume_deleted": False}):
            _run_bootswap(registry, task_id, cli,
                          "ocid1.compartment.oc1..testxxxx",
                          FAKE_INSTANCE, "test-instance",
                          FAKE_BACKUP, "old-backup", notify=None)
        task = registry.get(task_id)
        self.assertEqual(task["status"], "done")
        self.assertFalse(task["result"]["old_volume_deleted"])
        self.assertTrue(any("自动删除失败" in log
                            for log in task["logs"]))

    def test_run_failure_sets_failed(self):
        from oci.boot_swap import BootSwapError
        from web.tasks import TaskRegistry, _run_bootswap
        registry = TaskRegistry()
        task_id = registry.create("bootswap", detail="引导卷恢复：test")
        cli = MagicMock()
        with patch("web.tasks.swap_boot_volume",
                   side_effect=BootSwapError("挂新盘失败",
                                             rolled_back=True)):
            _run_bootswap(registry, task_id, cli,
                          "ocid1.compartment.oc1..testxxxx",
                          FAKE_INSTANCE, "test-instance",
                          FAKE_BACKUP, "old-backup", notify=None)
        task = registry.get(task_id)
        self.assertEqual(task["status"], "failed")
        self.assertTrue(any("换盘失败" in log for log in task["logs"]))

    def test_start_twice_second_rejected(self):
        from web.tasks import TaskRegistry, start_bootswap_task
        registry = TaskRegistry()
        cli = MagicMock()
        with patch("web.tasks.threading.Thread"):
            task_id = start_bootswap_task(
                registry, cli, "ocid1.compartment.oc1..testxxxx",
                FAKE_INSTANCE, "test-instance", FAKE_BACKUP, "old-backup")
        self.assertTrue(task_id.startswith("web-b"))
        with self.assertRaises(ValueError) as ctx:
            start_bootswap_task(
                registry, cli, "ocid1.compartment.oc1..testxxxx",
                FAKE_INSTANCE, "test-instance", FAKE_BACKUP, "old-backup")
        self.assertIn("正在换盘", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
