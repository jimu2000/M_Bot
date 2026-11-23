#!/usr/bin/env python3
"""阶段 5 工作包 A：quota.py 新增函数的单元测试。

覆盖：a1_health_check（超限检查 / 用满警告 / 降配建议 / 全部正常）。
全部 mock OCIClient.run，不碰真实 API。

运行：python3 -m unittest tests.test_phase5_quota -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import OCIClient
from oci.quota import a1_health_check

# 占位符（测试里不出现真实 OCID）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"


def _a1(inst_id, display_name, ocpus, memory_gb, state="RUNNING"):
    return {
        "id": f"ocid1.instance.oc1..{inst_id}",
        "display-name": display_name,
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": state,
        "shape-config": {"ocpus": ocpus, "memory-in-gbs": memory_gb},
    }


def _boot_volume(vol_id, display_name, size_gb, state="AVAILABLE"):
    return {
        "id": f"ocid1.bootvolume.oc1..{vol_id}",
        "display-name": display_name,
        "size-in-gbs": size_gb,
        "lifecycle-state": state,
    }


def _make_cli(instances, boot_volumes):
    """造一个 OCIClient，把 run 换成 mock：按命令返回预设的假数据。"""
    cli = OCIClient(profile="DEFAULT", region="ap-singapore-1")

    def _mock_run(args, **kwargs):
        if args[:2] == ["compute", "instance"]:
            return {"data": instances}
        if args[:2] == ["bv", "boot-volume"]:
            return {"data": boot_volumes}
        raise AssertionError(f"unexpected command: {args}")

    cli.run = _mock_run
    return cli


def _severities(result):
    return [s["severity"] for s in result["suggestions"]]


class TestA1HealthCheck(unittest.TestCase):
    def test_all_healthy_returns_single_ok(self):
        cli = _make_cli(
            [_a1("a1-1", "a1-small", 1, 6)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertTrue(result["overall_ok"])
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertEqual(result["suggestions"][0]["severity"], "ok")
        self.assertIn("健康", result["suggestions"][0]["message"])

    def test_empty_account_is_ok(self):
        cli = _make_cli([], [])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertTrue(result["overall_ok"])
        self.assertEqual(_severities(result), ["ok"])

    def test_ocpu_over_limit_is_error(self):
        # 5 核 > 4 核上限
        cli = _make_cli(
            [_a1("a1-1", "a1-big", 4, 24),
             _a1("a1-2", "a1-extra", 1, 6)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertFalse(result["overall_ok"])
        self.assertIn("error", _severities(result))
        errors = [s for s in result["suggestions"]
                  if s["severity"] == "error"]
        self.assertTrue(any("OCPU" in s["message"] for s in errors))

    def test_memory_over_limit_is_error(self):
        # 30GB > 24GB 上限
        cli = _make_cli(
            [_a1("a1-1", "a1-mem", 2, 30)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertFalse(result["overall_ok"])
        errors = [s for s in result["suggestions"]
                  if s["severity"] == "error"]
        self.assertTrue(any("内存" in s["message"] for s in errors))

    def test_boot_volume_over_limit_is_error(self):
        # 250GB > 200GB 上限
        cli = _make_cli(
            [_a1("a1-1", "a1-small", 1, 6)],
            [_boot_volume("bv-1", "boot-1", 100),
             _boot_volume("bv-2", "boot-2", 100),
             _boot_volume("bv-3", "boot-3", 50)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertFalse(result["overall_ok"])
        errors = [s for s in result["suggestions"]
                  if s["severity"] == "error"]
        self.assertTrue(any("启动盘" in s["message"] for s in errors))

    def test_ocpu_full_is_warning_not_error(self):
        # 正好 4 核：用满警告，但 overall_ok 仍为 True
        cli = _make_cli(
            [_a1("a1-1", "a1-full", 2, 12),
             _a1("a1-2", "a1-full-2", 2, 12)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertTrue(result["overall_ok"])
        self.assertIn("warning", _severities(result))
        self.assertNotIn("error", _severities(result))

    def test_oversized_instance_suggests_downsize(self):
        # 单台 4 核/24GB，高于常用需求（2 核/12GB）
        cli = _make_cli(
            [_a1("a1-1", "a1-big", 4, 24)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        infos = [s for s in result["suggestions"]
                 if s["severity"] == "info"]
        self.assertEqual(len(infos), 1)
        self.assertIn("a1-big", infos[0]["message"])
        self.assertIn("降配", infos[0]["message"])

    def test_typical_sized_instance_no_downsize_tip(self):
        # 2 核/12GB 正好是常用需求，不建议降配
        cli = _make_cli(
            [_a1("a1-1", "a1-typical", 2, 12)],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertNotIn("info", _severities(result))

    def test_stopped_instances_ignored(self):
        # STOPPED 的 A1 不计入用量（与 get_a1_usage 一致）
        cli = _make_cli(
            [_a1("a1-1", "a1-stopped", 4, 24, state="STOPPED")],
            [_boot_volume("bv-1", "boot-1", 47)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        self.assertTrue(result["overall_ok"])
        self.assertEqual(_severities(result), ["ok"])

    def test_every_suggestion_has_severity_and_message(self):
        cli = _make_cli(
            [_a1("a1-1", "a1-big", 4, 24),
             _a1("a1-2", "a1-extra", 1, 6)],
            [_boot_volume("bv-1", "boot-1", 250)])
        result = a1_health_check(cli, FAKE_COMPARTMENT)
        for suggestion in result["suggestions"]:
            self.assertIn(suggestion["severity"],
                          ("error", "warning", "info", "ok"))
            self.assertTrue(suggestion["message"])


if __name__ == "__main__":
    unittest.main()
