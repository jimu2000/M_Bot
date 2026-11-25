#!/usr/bin/env python3
"""阶段 5 工作包 E：巡检计划的测试（TestClient + mock）。

覆盖：计划 CRUD（cron/targets 校验）、run_inspection（实例/磁盘/配额
汇总、缺失实例记 error、报告写盘、Telegram 摘要）、报告列表、
调度器启停。OCI 与 Telegram 全部 mock，不碰真实服务。
"""

import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import OCIError
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.inspect import (
    SCHEDULES,
    run_inspection,
    start_inspect_scheduler,
    stop_inspect_scheduler,
)

# Test password: generated randomly at runtime (no fixed password is ever written in code). 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_INSTANCE_2 = "ocid1.instance.oc1..testyyyy"
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..testxxxx"

FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE, "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING"},
    {"id": FAKE_INSTANCE_2, "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED"},
]

FAKE_VOLUMES = [
    {"id": "ocid1.bootvolume.oc1..testxxxx", "display-name": "bv-a1",
     "size-in-gbs": 47, "lifecycle-state": "AVAILABLE"},
    {"id": "ocid1.bootvolume.oc1..testyyyy", "display-name": "bv-dead",
     "size-in-gbs": 50, "lifecycle-state": "TERMINATED"},
]

FAKE_SUMMARY = {
    "a1": {"ocpu_used": 2, "ocpu_limit": 4, "ocpu_remaining": 2,
           "memory_used_gb": 12, "memory_limit_gb": 24,
           "memory_remaining_gb": 12, "instances": []},
    "e2": {"count": 1, "limit": 2, "remaining": 1, "instances": []},
    "boot_volume": {"used_gb": 47, "limit_gb": 200, "remaining_gb": 153,
                    "volumes": []},
    "overall_ok": True,
}


class InspectTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、登录。"""

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
        SCHEDULES.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        stop_inspect_scheduler()
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _add_schedule(self, cron="0 8 * * *", targets=None, enabled=True):
        return self.client.post("/api/inspect-schedules", json={
            "cron": cron, "targets": targets or ["all"],
            "enabled": enabled})


class TestScheduleCrud(InspectTestCase):
    """巡检计划 CRUD。"""

    def test_empty_list(self):
        r = self.client.get("/api/inspect-schedules")
        self.assertEqual(r.json()["schedules"], [])

    def test_create_and_list(self):
        r = self._add_schedule()
        self.assertEqual(r.status_code, 201)
        schedule = r.json()["schedule"]
        self.assertEqual(schedule["cron"], "0 8 * * *")
        self.assertEqual(schedule["targets"], ["all"])
        self.assertTrue(schedule["enabled"])
        self.assertTrue(schedule["id"].startswith("insp"))
        r = self.client.get("/api/inspect-schedules")
        self.assertEqual(len(r.json()["schedules"]), 1)

    def test_create_with_instance_targets(self):
        r = self._add_schedule(targets=[FAKE_INSTANCE, FAKE_INSTANCE_2])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["schedule"]["targets"],
                         [FAKE_INSTANCE, FAKE_INSTANCE_2])

    def test_create_bad_cron_400(self):
        r = self._add_schedule(cron="bogus")
        self.assertEqual(r.status_code, 400)

    def test_create_empty_targets_400(self):
        r = self.client.post("/api/inspect-schedules", json={
            "cron": "0 8 * * *", "targets": [], "enabled": True})
        self.assertEqual(r.status_code, 400)

    def test_create_bad_target_400(self):
        r = self._add_schedule(targets=["not-an-ocid"])
        self.assertEqual(r.status_code, 400)

    def test_delete(self):
        schedule = self._add_schedule().json()["schedule"]
        r = self.client.delete(f"/api/inspect-schedules/{schedule['id']}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/inspect-schedules")
        self.assertEqual(r.json()["schedules"], [])

    def test_delete_unknown_404(self):
        r = self.client.delete("/api/inspect-schedules/nope")
        self.assertEqual(r.status_code, 404)

    def test_anon_401(self):
        r = self.anon.get("/api/inspect-schedules")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/inspect-schedules",
                           json={"cron": "0 8 * * *", "targets": ["all"]})
        self.assertEqual(r.status_code, 401)


class TestRunInspection(InspectTestCase):
    """run_inspection：汇总、报告写盘、Telegram 摘要。"""

    def _mock_oci(self, mock_resolve, mock_list, mock_vols, mock_summary,
                  summary=None):
        mock_cli = MagicMock()
        mock_resolve.return_value = (mock_cli, FAKE_COMPARTMENT)
        mock_list.return_value = FAKE_INSTANCES
        mock_vols.return_value = FAKE_VOLUMES
        mock_summary.return_value = summary or FAKE_SUMMARY

    @patch("web.routes.inspect.send_to_allowed_users")
    @patch("web.routes.inspect.get_free_tier_summary")
    @patch("web.routes.inspect.list_boot_volumes")
    @patch("web.routes.inspect.list_instances")
    @patch("web.routes.inspect.resolve_profile")
    def test_all_targets(self, mock_resolve, mock_list, mock_vols,
                         mock_summary, mock_notify):
        self._mock_oci(mock_resolve, mock_list, mock_vols, mock_summary)
        schedule = {"id": "insp1", "cron": "0 8 * * *", "targets": ["all"]}
        report = run_inspection(schedule)
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["instances"]), 2)
        self.assertEqual(report["instances"][0]["state"], "RUNNING")
        self.assertEqual(report["volumes"], {"count": 1, "totalGB": 47})
        self.assertTrue(report["quota"]["overallOk"])
        self.assertEqual(report["quota"]["a1"]["usedOcpu"], 2)
        self.assertEqual(report["errors"], [])
        report_path = os.path.join(self._tmp.name, "inspect_reports",
                                   report["reportFile"])
        self.assertTrue(os.path.exists(report_path))
        mock_notify.assert_called_once()
        text = mock_notify.call_args[0][0]
        self.assertIn("insp1", text)
        self.assertIn("RUNNING 1", text)

    @patch("web.routes.inspect.send_to_allowed_users")
    @patch("web.routes.inspect.get_free_tier_summary")
    @patch("web.routes.inspect.list_boot_volumes")
    @patch("web.routes.inspect.list_instances")
    @patch("web.routes.inspect.resolve_profile")
    def test_specific_targets_missing(self, mock_resolve, mock_list,
                                      mock_vols, mock_summary, mock_notify):
        self._mock_oci(mock_resolve, mock_list, mock_vols, mock_summary)
        missing = "ocid1.instance.oc1..missing"
        schedule = {"id": "insp1", "cron": "0 8 * * *",
                    "targets": [FAKE_INSTANCE, missing]}
        report = run_inspection(schedule)
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["instances"]), 1)
        self.assertTrue(any(missing in e for e in report["errors"]))

    @patch("web.routes.inspect.send_to_allowed_users")
    @patch("web.routes.inspect.get_free_tier_summary")
    @patch("web.routes.inspect.list_boot_volumes")
    @patch("web.routes.inspect.list_instances")
    @patch("web.routes.inspect.resolve_profile")
    def test_quota_over_limit(self, mock_resolve, mock_list, mock_vols,
                              mock_summary, mock_notify):
        over = dict(FAKE_SUMMARY, overall_ok=False)
        self._mock_oci(mock_resolve, mock_list, mock_vols, mock_summary,
                       summary=over)
        report = run_inspection({"id": "insp1", "cron": "0 8 * * *",
                                 "targets": ["all"]})
        self.assertFalse(report["ok"])
        self.assertTrue(any("超限" in e for e in report["errors"]))

    @patch("web.routes.inspect.send_to_allowed_users")
    @patch("web.routes.inspect.resolve_profile",
           side_effect=OCIError("boom"))
    def test_oci_failure_reported(self, mock_resolve, mock_notify):
        report = run_inspection({"id": "insp1", "cron": "0 8 * * *",
                                 "targets": ["all"]})
        self.assertFalse(report["ok"])
        self.assertTrue(any("巡检查询失败" in e for e in report["errors"]))
        # A failure also writes the report and sends the notice. 失败也写盘、也发通知。
        self.assertIn("reportFile", report)
        mock_notify.assert_called_once()

    @patch("web.routes.inspect.send_to_allowed_users")
    @patch("web.routes.inspect.get_free_tier_summary")
    @patch("web.routes.inspect.list_boot_volumes")
    @patch("web.routes.inspect.list_instances")
    @patch("web.routes.inspect.resolve_profile")
    def test_run_now_endpoint(self, mock_resolve, mock_list, mock_vols,
                              mock_summary, mock_notify):
        self._mock_oci(mock_resolve, mock_list, mock_vols, mock_summary)
        schedule = self._add_schedule().json()["schedule"]
        r = self.client.post(
            f"/api/inspect-schedules/{schedule['id']}/run")
        self.assertEqual(r.status_code, 202)
        # Wait for the background thread to finish (the report file appears). 等后台线程跑完（报告文件出现）。
        deadline = time.time() + 5
        reports_dir = os.path.join(self._tmp.name, "inspect_reports")
        while time.time() < deadline:
            if os.path.isdir(reports_dir) and os.listdir(reports_dir):
                break
            time.sleep(0.05)
        r = self.client.get("/api/inspect-reports")
        self.assertEqual(len(r.json()["reports"]), 1)
        self.assertEqual(r.json()["reports"][0]["scheduleId"],
                         schedule["id"])

    def test_run_unknown_404(self):
        r = self.client.post("/api/inspect-schedules/nope/run")
        self.assertEqual(r.status_code, 404)

    def test_reports_empty(self):
        r = self.client.get("/api/inspect-reports")
        self.assertEqual(r.json()["reports"], [])

    def test_reports_ignores_bad_files(self):
        reports_dir = os.path.join(self._tmp.name, "inspect_reports")
        os.makedirs(reports_dir)
        with open(os.path.join(reports_dir, "insp1-1700000000.json"), "w") as f:
            json.dump({"scheduleId": "insp1", "ok": True}, f)
        with open(os.path.join(reports_dir, "evil.txt"), "w") as f:
            f.write("not a report")
        with open(os.path.join(reports_dir, "insp2-1700000001.json"), "w") as f:
            f.write("{损坏")
        r = self.client.get("/api/inspect-reports")
        self.assertEqual(len(r.json()["reports"]), 1)
        self.assertEqual(r.json()["reports"][0]["scheduleId"], "insp1")


class TestInspectScheduler(InspectTestCase):
    """巡检调度器启停（不真等 cron 触发）。"""

    def test_start_stop_idempotent(self):
        scheduler = start_inspect_scheduler()
        self.assertIsNotNone(scheduler)
        self.assertIs(start_inspect_scheduler(), scheduler)
        stop_inspect_scheduler()
        # After stopping, it can start again (a new scheduler instance). 停掉后可再次启动（新调度器实例）。
        scheduler2 = start_inspect_scheduler()
        self.assertIsNotNone(scheduler2)
        self.assertIsNot(scheduler2, scheduler)
        stop_inspect_scheduler()

    def test_enabled_schedule_gets_job(self):
        self._add_schedule(cron="0 8 * * *", enabled=True)
        self._add_schedule(cron="0 9 * * *", enabled=False)
        scheduler = start_inspect_scheduler()
        job_ids = [job.id for job in scheduler.get_jobs()]
        self.assertIn("inspect-insp1", job_ids)
        self.assertNotIn("inspect-insp2", job_ids)
        self.assertIn("inspect-refresh", job_ids)
        stop_inspect_scheduler()


if __name__ == "__main__":
    unittest.main()
