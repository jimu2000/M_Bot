#!/usr/bin/env python3
"""阶段 5 工作包 E：云报告汇总的测试（TestClient + mock）。

覆盖：GET /api/cloud-report/summary 多账号汇总、按状态分的实例数、
磁盘总数与 GB、A1/E2 配额、单账号失败不拖垮整份报告。
"""

import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import OCIError
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.cloudreport import clear_cloud_report_cache

# Test password: generated randomly at runtime (no fixed password is ever written in code). 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"

FAKE_PROFILES = {
    "DEFAULT": {"region": "ap-singapore-1",
                "compartment_id": "ocid1.tenancy.oc1..testxxxx"},
    "ACCOUNT2": {"region": "us-sanjose-1",
                 "compartment_id": "ocid1.tenancy.oc1..testyyyy"},
}


class CloudReportTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、双账号配置、登录。"""

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
        self._profiles_patcher = patch("web.config.PROFILES", FAKE_PROFILES)
        self._profiles_patcher.start()
        # Clear the cloud report cache so cached data does not leak across tests. 清云报告缓存，避免缓存跨测试串数据。
        clear_cloud_report_cache()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._profiles_patcher.stop()
        self._data_dir_patcher.stop()
        self._tmp.cleanup()


class TestCloudReport(CloudReportTestCase):
    """GET /api/cloud-report/summary。"""

    @patch("web.routes.cloudreport.get_e2_usage")
    @patch("web.routes.cloudreport.get_a1_usage")
    @patch("web.routes.cloudreport.list_boot_volumes")
    @patch("web.routes.cloudreport.list_instances")
    def test_two_profiles(self, mock_insts, mock_vols, mock_a1, mock_e2):
        mock_insts.return_value = [
            {"id": FAKE_INSTANCE, "lifecycle-state": "RUNNING"},
            {"id": FAKE_INSTANCE_2, "lifecycle-state": "STOPPED"},
        ]
        mock_vols.return_value = [
            {"id": "v1", "size-in-gbs": 47, "lifecycle-state": "AVAILABLE"},
            {"id": "v2", "size-in-gbs": 50, "lifecycle-state": "TERMINATED"},
        ]
        mock_a1.return_value = {"ocpu_used": 2, "ocpu_limit": 4,
                                "memory_used_gb": 12, "memory_limit_gb": 24,
                                "instances": []}
        mock_e2.return_value = {"count": 1, "limit": 2, "instances": []}
        r = self.client.get("/api/cloud-report/summary")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertGreater(body["generatedAtEpochSec"], 0)
        self.assertEqual(len(body["groups"]), 2)
        group = body["groups"][0]
        self.assertEqual(group["profile"], "DEFAULT")
        # The overview page uses profileLabel (the home-region display name) instead of the config key name. 总览页用 profileLabel（主区中文名）代替配置键名。
        self.assertEqual(group["profileLabel"], "新加坡")
        self.assertEqual(group["region"], "ap-singapore-1")
        self.assertEqual(group["instances"]["total"], 2)
        self.assertEqual(group["instances"]["byState"],
                         {"RUNNING": 1, "STOPPED": 1})
        self.assertEqual(group["volumes"], {"count": 1, "totalGB": 47})
        self.assertEqual(group["quota"]["a1"],
                         {"usedOcpu": 2, "capOcpu": 4,
                          "usedMem": 12, "capMem": 24})
        self.assertEqual(group["quota"]["e2"], {"count": 1, "limit": 2})
        self.assertEqual(body["groups"][1]["profile"], "ACCOUNT2")
        self.assertEqual(body["groups"][1]["profileLabel"], "圣何塞")

    @patch("web.routes.cloudreport.list_instances")
    def test_one_profile_fails(self, mock_insts):
        """一个账号查询失败时该 group 带 error，其他账号正常汇总。"""
        def _list(cli, compartment_id):
            if cli.profile == "ACCOUNT2":
                raise OCIError("not authenticated")
            return [{"id": FAKE_INSTANCE, "lifecycle-state": "RUNNING"}]
        mock_insts.side_effect = _list
        with patch("web.routes.cloudreport.list_boot_volumes",
                   return_value=[]), \
            patch("web.routes.cloudreport.get_a1_usage",
                  return_value={"ocpu_used": 0, "ocpu_limit": 4,
                                "memory_used_gb": 0, "memory_limit_gb": 24,
                                "instances": []}), \
            patch("web.routes.cloudreport.get_e2_usage",
                  return_value={"count": 0, "limit": 2, "instances": []}):
            r = self.client.get("/api/cloud-report/summary")
        self.assertEqual(r.status_code, 200)
        groups = r.json()["groups"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["profile"], "DEFAULT")
        self.assertEqual(groups[0]["profileLabel"], "新加坡")
        self.assertNotIn("error", groups[0])
        self.assertEqual(groups[1]["profile"], "ACCOUNT2")
        self.assertEqual(groups[1]["profileLabel"], "圣何塞")
        self.assertIn("error", groups[1])

    def test_anon_401(self):
        r = self.anon.get("/api/cloud-report/summary")
        self.assertEqual(r.status_code, 401)

    @patch("web.routes.cloudreport.get_e2_usage")
    @patch("web.routes.cloudreport.get_a1_usage")
    @patch("web.routes.cloudreport.list_boot_volumes")
    @patch("web.routes.cloudreport.list_instances")
    def test_terminate_invalidates_report_cache(
            self, mock_insts, mock_vols, mock_a1, mock_e2):
        """ARM 全功能测试 Bug 4：终止实例后云报告立即变新数，
        不等 60 秒缓存过期（启动盘 4 块/188GB -> 3 块/141GB）。"""
        mock_insts.return_value = [
            {"id": FAKE_INSTANCE, "lifecycle-state": "RUNNING"},
            {"id": FAKE_INSTANCE_2, "lifecycle-state": "RUNNING"},
        ]
        mock_a1.return_value = {"ocpu_used": 2, "ocpu_limit": 4,
                                "memory_used_gb": 12, "memory_limit_gb": 24,
                                "instances": []}
        mock_e2.return_value = {"count": 1, "limit": 2, "instances": []}
        mock_vols.return_value = [
            {"id": f"v{i}", "size-in-gbs": 47,
             "lifecycle-state": "AVAILABLE"} for i in range(4)]
        r = self.client.get("/api/cloud-report/summary")
        self.assertEqual(r.json()["groups"][0]["volumes"],
                         {"count": 4, "totalGB": 188})
        # 终止后真实情况：只剩 3 块盘。
        mock_vols.return_value = [
            {"id": f"v{i}", "size-in-gbs": 47,
             "lifecycle-state": "AVAILABLE"} for i in range(3)]
        mock_insts.return_value = [
            {"id": FAKE_INSTANCE, "lifecycle-state": "RUNNING"},
        ]
        with patch("web.routes.instances.resolve_profile",
                   return_value=(MagicMock(), "compartment")), \
                patch("web.routes.instances.terminate_instance"):
            r = self.client.post(
                f"/api/instances/{FAKE_INSTANCE_2}/terminate",
                json={"confirm": True})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/cloud-report/summary")
        self.assertEqual(r.json()["groups"][0]["volumes"],
                         {"count": 3, "totalGB": 141})


if __name__ == "__main__":
    unittest.main()
