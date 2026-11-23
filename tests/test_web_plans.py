#!/usr/bin/env python3
"""抢机计划（web/tasks.py 的 GrabberPlanRegistry + 调度器）的单元测试。

运行：python3 -m unittest tests.test_web_plans -v

注意：
  - 计划文件写临时 DATA_DIR，不污染真实数据目录。
  - 调度器用真实的 APScheduler BackgroundScheduler（本机内存调度，
    不碰网络），每个测试后必须停掉；抢机触发用 mock 的 start_grab_task，
    绝不真跑 Grabber。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import config
from web.tasks import (
    PLANS,
    _plan_grab_params,
    _run_plan_job,
    refresh_plan_scheduler,
    start_plan_scheduler,
    stop_plan_scheduler,
    validate_cron,
)

SHAPE_E2 = "VM.Standard.E2.1.Micro"
SHAPE_A1 = "VM.Standard.A1.Flex"


class PlansTestCase(unittest.TestCase):
    """临时 DATA_DIR + 清空计划。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        PLANS.reset()

    def tearDown(self):
        stop_plan_scheduler()
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _plans_file_text(self):
        path = os.path.join(self._tmp.name, "grabber_plans.json")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()


class TestValidateCron(unittest.TestCase):
    def test_valid_expressions(self):
        for good in ("0 2 * * *", "*/10 * * * *", "0 0 1 * *",
                     "30 4 * * 1-5", " 0 2 * * * "):
            self.assertEqual(validate_cron(good), good.strip(), good)

    def test_invalid_expressions(self):
        for bad in ("", "   ", "not-a-cron", "0 2 * *",
                    "0 2 * * * *", "61 25 32 13 8", "0 2 * * MONDAY"):
            with self.assertRaises(ValueError, msg=bad):
                validate_cron(bad)

    def test_non_string_rejected(self):
        for bad in (None, 123, ["0", "2", "*", "*", "*"]):
            with self.assertRaises(ValueError, msg=bad):
                validate_cron(bad)


class TestPlanRegistry(PlansTestCase):
    def test_add_and_list(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 4, 24)
        self.assertEqual(plan["id"], "p1")
        self.assertEqual(plan["cron"], "0 2 * * *")
        self.assertEqual(plan["shape"], SHAPE_A1)
        self.assertEqual(plan["ocpus"], 4)
        self.assertEqual(plan["memory_gb"], 24)
        self.assertTrue(plan["enabled"])
        self.assertEqual(len(PLANS.list()), 1)
        # 持久化到文件。
        with open(os.path.join(self._tmp.name, "grabber_plans.json"),
                  encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], "p1")

    def test_add_with_extra_fields(self):
        plan = PLANS.add("0 2 * * *", SHAPE_E2, 1, 1,
                         display_name="my-e2", target_cpu="7J13",
                         profile="ACCOUNT2", unknown_field="dropped")
        self.assertEqual(plan["display_name"], "my-e2")
        self.assertEqual(plan["target_cpu"], "7J13")
        self.assertEqual(plan["profile"], "ACCOUNT2")
        self.assertNotIn("unknown_field", plan)

    def test_add_disabled(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6, enabled=False)
        self.assertFalse(plan["enabled"])

    def test_ids_increment_after_delete(self):
        PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        PLANS.add("0 3 * * *", SHAPE_A1, 1, 6)
        PLANS.delete("p2")
        plan = PLANS.add("0 4 * * *", SHAPE_A1, 1, 6)
        # id 按现存计划的最大数字后缀 +1：删掉 p2 后只剩 p1，新计划是 p2。
        # 复用已删 id 是安全的：调度器同步时先删全部 plan job 再重建，
        # 且 add_job 带 replace_existing=True，不会残留旧 cron。
        self.assertEqual(plan["id"], "p2")

    def test_add_invalid_cron_rejected(self):
        with self.assertRaises(ValueError):
            PLANS.add("not-a-cron", SHAPE_A1, 1, 6)
        self.assertEqual(PLANS.list(), [])

    def test_add_invalid_shape_rejected(self):
        with self.assertRaises(ValueError):
            PLANS.add("0 2 * * *", "VM.Standard.E5.Flex", 1, 6)
        self.assertEqual(PLANS.list(), [])

    def test_add_out_of_range_rejected(self):
        for ocpus, memory_gb in ((0, 6), (5, 6), (1, 0), (1, 25), (1, -1)):
            with self.assertRaises(ValueError, msg=(ocpus, memory_gb)):
                PLANS.add("0 2 * * *", SHAPE_A1, ocpus, memory_gb)
        self.assertEqual(PLANS.list(), [])

    def test_add_non_integer_rejected(self):
        with self.assertRaises(ValueError):
            PLANS.add("0 2 * * *", SHAPE_A1, "four", 6)
        self.assertEqual(PLANS.list(), [])

    def test_get_and_delete(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        self.assertEqual(PLANS.get(plan["id"])["cron"], "0 2 * * *")
        self.assertIsNone(PLANS.get("p999"))
        self.assertTrue(PLANS.delete(plan["id"]))
        self.assertFalse(PLANS.delete(plan["id"]))
        self.assertEqual(PLANS.list(), [])

    def test_set_enabled(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        updated = PLANS.set_enabled(plan["id"], False)
        self.assertFalse(updated["enabled"])
        self.assertFalse(PLANS.get(plan["id"])["enabled"])
        updated = PLANS.set_enabled(plan["id"], True)
        self.assertTrue(updated["enabled"])
        self.assertIsNone(PLANS.set_enabled("p999", True))

    def test_corrupt_file_returns_empty(self):
        path = os.path.join(self._tmp.name, "grabber_plans.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertEqual(PLANS.list(), [])
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"not": "a list"}')
        self.assertEqual(PLANS.list(), [])

    def test_two_registries_share_file(self):
        """Web 面板与 Telegram 机器人是两个进程，共用同一文件：
        一个注册表实例写的计划，另一个实例立即能读到。"""
        from web.tasks import GrabberPlanRegistry
        other = GrabberPlanRegistry()
        PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        self.assertEqual(len(other.list()), 1)
        other.delete("p1")
        self.assertEqual(PLANS.list(), [])


class TestPlanGrabParams(PlansTestCase):
    def test_defaults_filled(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 4, 24)
        params = _plan_grab_params(plan)
        self.assertEqual(params["shape"], SHAPE_A1)
        self.assertEqual(params["ocpus"], 4)
        self.assertEqual(params["memory_gb"], 24)
        # 缺的字段用 PLAN_DEFAULTS 补。
        self.assertTrue(params["availability_domain"])
        self.assertTrue(params["image_id"])
        self.assertTrue(params["subnet_id"])
        self.assertTrue(params["ssh_keys_file"])
        # display_name 没填时用 默认名 + 计划 id。
        self.assertEqual(params["display_name"], "plan-grab-p1")

    def test_explicit_fields_win(self):
        plan = PLANS.add("0 2 * * *", SHAPE_E2, 1, 1,
                         display_name="my-e2",
                         availability_domain="FAKE-AD-1",
                         image_id="ocid1.image.oc1..fake",
                         subnet_id="ocid1.subnet.oc1..fake",
                         ssh_keys_file="/tmp/keys.pub",
                         target_cpu="7J13")
        params = _plan_grab_params(plan)
        self.assertEqual(params["display_name"], "my-e2")
        self.assertEqual(params["availability_domain"], "FAKE-AD-1")
        self.assertEqual(params["image_id"], "ocid1.image.oc1..fake")
        self.assertEqual(params["target_cpu"], "7J13")


class TestRunPlanJob(PlansTestCase):
    def test_trigger_starts_grab_task(self):
        """计划触发时复用 start_grab_task（mock，不真跑 Grabber）。"""
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        with patch("web.tasks.start_grab_task") as mock_start, \
             patch("web.tasks.TASKS") as mock_tasks:
            mock_tasks.list.return_value = []
            _run_plan_job(plan["id"])
            mock_start.assert_called_once()
            args, kwargs = mock_start.call_args
            params = args[3]
            self.assertEqual(params["shape"], SHAPE_A1)
            self.assertEqual(params["ocpus"], 1)
            self.assertEqual(params["memory_gb"], 6)
            self.assertEqual(kwargs["detail_prefix"],
                             f"抢机计划 {plan['id']}")

    def test_disabled_plan_skipped(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6, enabled=False)
        with patch("web.tasks.start_grab_task") as mock_start:
            _run_plan_job(plan["id"])
            mock_start.assert_not_called()

    def test_missing_plan_skipped(self):
        with patch("web.tasks.start_grab_task") as mock_start:
            _run_plan_job("p999")
            mock_start.assert_not_called()

    def test_duplicate_trigger_skipped(self):
        """同一计划已有抢机任务在跑时，本次触发跳过（不重复抢机）。"""
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        running = {"id": "g1", "kind": "grab", "status": "running",
                   "detail": f"抢机计划 {plan['id']}：抢机：x（{SHAPE_A1}）"}
        with patch("web.tasks.start_grab_task") as mock_start, \
             patch("web.tasks.TASKS") as mock_tasks:
            mock_tasks.list.return_value = [running]
            _run_plan_job(plan["id"])
            mock_start.assert_not_called()

    def test_finished_task_does_not_block(self):
        """同一计划的上次任务已结束（done）时，本次触发正常启动。"""
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        done = {"id": "g1", "kind": "grab", "status": "done",
                "detail": f"抢机计划 {plan['id']}：抢机：x（{SHAPE_A1}）"}
        with patch("web.tasks.start_grab_task") as mock_start, \
             patch("web.tasks.TASKS") as mock_tasks:
            mock_tasks.list.return_value = [done]
            _run_plan_job(plan["id"])
            mock_start.assert_called_once()

    def test_unknown_profile_does_not_raise(self):
        """计划的 profile 不存在时触发失败也不抛异常（调度器线程内吞掉）。"""
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6, profile="NOPE")
        with patch("web.tasks.start_grab_task") as mock_start:
            # 不抛异常，且没启动抢机。
            _run_plan_job(plan["id"])
            mock_start.assert_not_called()


class TestPlanScheduler(PlansTestCase):
    def test_start_registers_enabled_plans_only(self):
        PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        PLANS.add("0 3 * * *", SHAPE_E2, 1, 1, enabled=False)
        scheduler = start_plan_scheduler()
        self.assertIsNotNone(scheduler)
        job_ids = {job.id for job in scheduler.get_jobs()}
        self.assertIn("plan-p1", job_ids)
        self.assertNotIn("plan-p2", job_ids)
        # 每分钟同步 job 也注册了。
        self.assertIn("plans-refresh", job_ids)

    def test_start_is_idempotent(self):
        first = start_plan_scheduler()
        second = start_plan_scheduler()
        self.assertIs(first, second)
        job_ids = [job.id for job in first.get_jobs()]
        self.assertEqual(job_ids.count("plans-refresh"), 1)

    def test_refresh_syncs_jobs(self):
        plan = PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        scheduler = start_plan_scheduler()
        self.assertIn("plan-p1", {j.id for j in scheduler.get_jobs()})
        # 停用后刷新，job 被移除；同步 job 还在。
        PLANS.set_enabled(plan["id"], False)
        refresh_plan_scheduler()
        job_ids = {j.id for j in scheduler.get_jobs()}
        self.assertNotIn("plan-p1", job_ids)
        self.assertIn("plans-refresh", job_ids)
        # 再启用后刷新，job 回来。
        PLANS.set_enabled(plan["id"], True)
        refresh_plan_scheduler()
        self.assertIn("plan-p1", {j.id for j in scheduler.get_jobs()})
        # 删除后刷新，job 被移除。
        PLANS.delete(plan["id"])
        refresh_plan_scheduler()
        self.assertNotIn("plan-p1", {j.id for j in scheduler.get_jobs()})

    def test_refresh_without_scheduler_is_noop(self):
        PLANS.add("0 2 * * *", SHAPE_A1, 1, 6)
        refresh_plan_scheduler()  # 调度器未启动时不报错
        stop_plan_scheduler()  # 同样不报错

    def test_corrupt_cron_in_file_skipped(self):
        """计划文件被手工改坏（cron 非法）时调度器跳过该计划，不崩。"""
        path = os.path.join(self._tmp.name, "grabber_plans.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": "p1", "cron": "bogus", "shape": SHAPE_A1,
                        "ocpus": 1, "memory_gb": 6, "enabled": True}],
                      f)
        scheduler = start_plan_scheduler()
        self.assertIsNotNone(scheduler)
        self.assertNotIn("plan-p1", {j.id for j in scheduler.get_jobs()})


if __name__ == "__main__":
    unittest.main()
