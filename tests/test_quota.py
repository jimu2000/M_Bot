#!/usr/bin/env python3
"""quota.py 的单元测试（mock OCIClient.run，不碰真实 API）。

运行：python3 -m unittest tests.test_quota -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import OCIClient
from oci.quota import (can_launch, can_launch_batch, get_a1_usage,
                       get_boot_volume_usage, get_e2_usage,
                       get_free_tier_summary)

# Placeholders (no real OCID in tests). 占位符（测试里不出现真实 OCID）。
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"


def _a1(inst_id, display_name, ocpus, memory_gb, state="RUNNING"):
    return {
        "id": f"ocid1.instance.oc1..{inst_id}",
        "display-name": display_name,
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": state,
        "shape-config": {"ocpus": ocpus, "memory-in-gbs": memory_gb},
    }


def _e2(inst_id, display_name, state="RUNNING"):
    return {
        "id": f"ocid1.instance.oc1..{inst_id}",
        "display-name": display_name,
        "shape": "VM.Standard.E2.1.Micro",
        "lifecycle-state": state,
        "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
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


# Default scenario: A1 uses 4 OCPUs / 18 GB (full), 1 E2 instance, 141 GB of boot volume. 默认场景：A1 用了 4 核/18GB（已满），E2 有 1 台，启动盘 141GB。
# Matches the current state of the real account's Singapore region. 与真实账号新加坡区的现状一致。
DEFAULT_INSTANCES = [
    _a1("a1-1", "a1-main", 3, 12),
    _a1("a1-2", "a1-small", 1, 6),
    _a1("a1-dead", "a1-terminated", 4, 24, state="TERMINATED"),
    _e2("e2-1", "e2-one"),
    _e2("e2-dead", "e2-terminated", state="TERMINATED"),
]
DEFAULT_BOOT_VOLUMES = [
    _boot_volume("bv-1", "boot-1", 47),
    _boot_volume("bv-2", "boot-2", 47),
    _boot_volume("bv-3", "boot-3", 47),
    _boot_volume("bv-dead", "boot-terminating", 47, state="TERMINATING"),
]


class TestGetA1Usage(unittest.TestCase):
    def test_sums_running_a1_only(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        result = get_a1_usage(cli, FAKE_COMPARTMENT)
        # Only RUNNING A1 counts: 3+1 OCPUs, 12+6 GB; terminated and E2 instances do not count. 只统计 RUNNING 的 A1：3+1 核，12+6GB；已终止的与 E2 不算。
        self.assertEqual(result["ocpu_used"], 4)
        self.assertEqual(result["ocpu_limit"], 4)
        self.assertEqual(result["memory_used_gb"], 18)
        self.assertEqual(result["memory_limit_gb"], 24)
        self.assertEqual(len(result["instances"]), 2)
        self.assertEqual(result["instances"][0]["display_name"], "a1-main")
        self.assertEqual(result["instances"][0]["ocpus"], 3)

    def test_empty_when_no_instances(self):
        cli = _make_cli([], [])
        result = get_a1_usage(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["ocpu_used"], 0)
        self.assertEqual(result["memory_used_gb"], 0)
        self.assertEqual(result["instances"], [])


class TestGetE2Usage(unittest.TestCase):
    def test_counts_running_e2_only(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        result = get_e2_usage(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["limit"], 2)
        self.assertEqual(len(result["instances"]), 1)
        self.assertEqual(result["instances"][0]["display_name"], "e2-one")

    def test_empty_when_no_instances(self):
        cli = _make_cli([], [])
        result = get_e2_usage(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["instances"], [])


class TestGetBootVolumeUsage(unittest.TestCase):
    def test_excludes_terminating_and_terminated(self):
        vols = DEFAULT_BOOT_VOLUMES + [
            _boot_volume("bv-gone", "boot-terminated", 47, state="TERMINATED"),
        ]
        cli = _make_cli(DEFAULT_INSTANCES, vols)
        result = get_boot_volume_usage(cli, FAKE_COMPARTMENT)
        # 3 AVAILABLE 47 GB volumes; TERMINATING and TERMINATED ones do not count. 3 块 AVAILABLE 的 47GB；删除中（TERMINATING）与已删除（TERMINATED）的不算。
        self.assertEqual(result["used_gb"], 141)
        self.assertEqual(result["limit_gb"], 200)
        self.assertEqual(len(result["volumes"]), 3)

    def test_empty_when_no_volumes(self):
        cli = _make_cli([], [])
        result = get_boot_volume_usage(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["used_gb"], 0)
        self.assertEqual(result["volumes"], [])


class TestGetFreeTierSummary(unittest.TestCase):
    def test_summary_within_limits(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        result = get_free_tier_summary(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["a1"]["ocpu_used"], 4)
        self.assertEqual(result["a1"]["ocpu_remaining"], 0)
        self.assertEqual(result["a1"]["memory_used_gb"], 18)
        self.assertEqual(result["a1"]["memory_remaining_gb"], 6)
        self.assertEqual(result["e2"]["count"], 1)
        self.assertEqual(result["e2"]["remaining"], 1)
        self.assertEqual(result["boot_volume"]["used_gb"], 141)
        self.assertEqual(result["boot_volume"]["remaining_gb"], 59)
        self.assertTrue(result["overall_ok"])

    def test_summary_over_limit(self):
        # A1 OCPU over the limit (5 OCPUs) -> overall_ok is False and remaining is negative. A1 OCPU 超限（5 核）-> overall_ok 为 False，remaining 为负。
        instances = [_a1("a1-big", "a1-over", 5, 24)]
        cli = _make_cli(instances, [])
        result = get_free_tier_summary(cli, FAKE_COMPARTMENT)
        self.assertEqual(result["a1"]["ocpu_remaining"], -1)
        self.assertFalse(result["overall_ok"])


class TestCanLaunch(unittest.TestCase):
    def test_e2_can_launch(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "VM.Standard.E2.1.Micro")
        self.assertTrue(ok)
        self.assertEqual(reason, "可以创建")

    def test_e2_full(self):
        instances = DEFAULT_INSTANCES + [_e2("e2-2", "e2-two")]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT, "E2.1.Micro")
        self.assertFalse(ok)
        self.assertEqual(reason, "E2 已满（2/2）")

    def test_a1_ocpu_insufficient(self):
        # In the default scenario A1 OCPU is already full (4/4), so one more OCPU cannot be launched. 默认场景 A1 OCPU 已满（4/4），再要 1 核开不了。
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "VM.Standard.A1.Flex", ocpus=1, memory_gb=6)
        self.assertFalse(ok)
        self.assertIn("OCPU 不足", reason)
        self.assertIn("4/4", reason)

    def test_a1_memory_insufficient(self):
        # Enough OCPUs (3/4) but not enough memory (6 GB left, 12 GB wanted). OCPU 够（3/4）但内存不够（剩 6GB，要 12GB）。
        instances = [_a1("a1-1", "a1-main", 3, 18)]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "VM.Standard.A1.Flex", ocpus=1, memory_gb=12)
        self.assertFalse(ok)
        self.assertIn("内存不足", reason)

    def test_boot_volume_insufficient(self):
        # Only 40 GB of boot volume left < 47 GB, so neither E2 nor A1 can be launched. 启动盘只剩 40GB < 47GB，E2 与 A1 都开不了。
        volumes = [_boot_volume("bv-1", "boot-1", 160)]
        cli = _make_cli([], volumes)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "VM.Standard.E2.1.Micro")
        self.assertFalse(ok)
        self.assertIn("启动盘余量不足", reason)
        self.assertIn("40GB", reason)

    def test_a1_can_launch(self):
        # A1 uses only 3 OCPUs / 12 GB and there is enough boot volume left, so one more 1 OCPU / 6 GB instance fits. A1 只用了 3 核/12GB，启动盘余量足，可以再开 1 核 6GB。
        instances = [_a1("a1-1", "a1-main", 3, 12)]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "A1.Flex", ocpus=1, memory_gb=6)
        self.assertTrue(ok)
        self.assertEqual(reason, "可以创建")

    def test_unknown_shape(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch(cli, FAKE_COMPARTMENT,
                                "VM.Standard.E5.Flex")
        self.assertFalse(ok)
        self.assertIn("不支持的规格", reason)


class TestCanLaunchBatch(unittest.TestCase):
    """批量开机配额预检：单台规则 × 数量。"""

    def test_single_matches_can_launch(self):
        instances = [_a1("a1-1", "a1-main", 3, 12)]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.A1.Flex", ocpus=1,
                                      memory_gb=6, count=1)
        self.assertTrue(ok)
        self.assertEqual(reason, "可以创建")

    def test_batch_a1_ok(self):
        # A1 uses 2 OCPUs / 12 GB, leaving 2 OCPUs / 12 GB, so exactly 2 instances of 1 OCPU / 6 GB fit; boot volume uses only 47 GB (153 GB left), so 2 x 47 GB also fits. A1 用了 2 核/12GB，剩 2 核/12GB，开 2 台 1 核/6GB 刚好够；启动盘只用了 47GB（剩 153GB），2 台 × 47GB 也够。
        instances = [_a1("a1-1", "a1-main", 2, 12)]
        volumes = [_boot_volume("bv-1", "boot-1", 47)]
        cli = _make_cli(instances, volumes)
        ok, _ = can_launch_batch(cli, FAKE_COMPARTMENT,
                                 "VM.Standard.A1.Flex", ocpus=1,
                                 memory_gb=6, count=2)
        self.assertTrue(ok)

    def test_batch_a1_ocpu_insufficient(self):
        # 1 OCPU left, 2 instances each need 1 OCPU -> not enough (one instance fits, a batch of two does not). 剩 1 核，2 台各要 1 核 -> 不足（单台能开，批量不能）。
        instances = [_a1("a1-1", "a1-main", 3, 12)]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.A1.Flex", ocpus=1,
                                      memory_gb=6, count=2)
        self.assertFalse(ok)
        self.assertIn("OCPU 不足", reason)
        self.assertIn("2 台需要 2 核", reason)

    def test_batch_a1_memory_insufficient(self):
        # Enough OCPUs (2 left) but only 12 GB of memory left, and 2 instances each need 12 GB -> not enough. OCPU 够（剩 2 核），内存剩 12GB，2 台各要 12GB -> 不足。
        instances = [_a1("a1-1", "a1-main", 2, 12)]
        cli = _make_cli(instances, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.A1.Flex", ocpus=1,
                                      memory_gb=12, count=2)
        self.assertFalse(ok)
        self.assertIn("内存不足", reason)
        self.assertIn("2 台需要 24GB", reason)

    def test_batch_e2_ok(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, _ = can_launch_batch(cli, FAKE_COMPARTMENT,
                                 "VM.Standard.E2.1.Micro", count=1)
        self.assertTrue(ok)

    def test_batch_e2_count_insufficient(self):
        # 1 E2 slot left, 2 instances wanted -> not enough. E2 剩 1 台，要开 2 台 -> 不足。
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.E2.1.Micro", count=2)
        self.assertFalse(ok)
        self.assertIn("E2 余量不足", reason)
        self.assertIn("需要 2 台", reason)

    def test_batch_boot_volume_multiplied(self):
        # 59 GB of boot volume left; 2 instances at 47 GB each (default) need 94 GB -> not enough. 启动盘剩 59GB，2 台各 47GB（默认）需要 94GB -> 不足。
        cli = _make_cli([], DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.E2.1.Micro", count=2)
        self.assertFalse(ok)
        self.assertIn("启动盘余量不足", reason)
        self.assertIn("2 台需要 94GB", reason)

    def test_batch_boot_volume_custom_size(self):
        # A custom 100 GB per instance means 1 instance already needs 100 GB > 59 GB remaining -> not enough. 自定义 100GB/台，1 台就需要 100GB > 59GB 余量 -> 不足。
        cli = _make_cli([], DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.E2.1.Micro", count=1,
                                      boot_volume_size_gbs=100)
        self.assertFalse(ok)
        self.assertIn("启动盘余量不足", reason)
        self.assertIn("需要 100GB", reason)

    def test_batch_count_below_one_treated_as_one(self):
        cli = _make_cli(DEFAULT_INSTANCES, DEFAULT_BOOT_VOLUMES)
        ok, _ = can_launch_batch(cli, FAKE_COMPARTMENT,
                                 "VM.Standard.E2.1.Micro", count=0)
        self.assertTrue(ok)

    def test_batch_unknown_shape(self):
        cli = _make_cli([], DEFAULT_BOOT_VOLUMES)
        ok, reason = can_launch_batch(cli, FAKE_COMPARTMENT,
                                      "VM.Standard.E5.Flex", count=2)
        self.assertFalse(ok)
        self.assertIn("不支持的规格", reason)


if __name__ == "__main__":
    unittest.main()
