#!/usr/bin/env python3
"""第 5 轮 CPU 优化测试：跨端点去重。

覆盖：
  - oci.instance.slim_instances / oci.volume.simplify_boot_volumes（纯函数）
  - oci.quota：get_free_tier_summary 同一次请求只取一次实例列表；
    get_a1_usage / get_e2_usage / get_boot_volume_usage 传入已取好的
    列表时不再调 SDK。
  - web.oci_cache：30 秒内跨端点共享同一份原始列表（SDK 只调一次）；
    refresh 绕过；过期后重取；clear 生效。
"""

import time
import unittest
from unittest import mock

from oci.instance import slim_instances
from oci.quota import (
    get_a1_usage,
    get_boot_volume_usage,
    get_e2_usage,
    get_free_tier_summary,
)
from oci.volume import simplify_boot_volumes
from web import oci_cache

RAW_INSTANCES = [
    {"id": "ocid1.instance.a", "display-name": "ARM_2026-01-03",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-26T00:00:00Z",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 6}},
    {"id": "ocid1.instance.b", "display-name": "AMD_2026-01-03",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-26T00:00:00Z",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 1}},
    {"id": "ocid1.instance.c", "display-name": "OLD",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "TERMINATED",
     "time-created": "2026-11-25T00:00:00Z",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 6}},
]

RAW_VOLUMES = [
    {"id": "ocid1.bootvolume.a", "display-name": "boot-a",
     "size-in-gbs": 50, "lifecycle-state": "AVAILABLE",
     "availability-domain": "wgwP:AP-SINGAPORE-1-AD-1",
     "time-created": "2026-11-26T00:00:00Z"},
    {"id": "ocid1.bootvolume.b", "display-name": "boot-b",
     "size-in-gbs": 47, "lifecycle-state": "TERMINATED",
     "availability-domain": "wgwP:AP-SINGAPORE-1-AD-1",
     "time-created": "2026-11-25T00:00:00Z"},
]


class _FakeCli:
    """OCIClient 的替身：记录 SDK 调用次数，返回预设原始列表。"""

    def __init__(self, instances=None, volumes=None):
        self.profile = "DEFAULT"
        self.region = "ap-singapore-1"
        self.instances = instances if instances is not None else RAW_INSTANCES
        self.volumes = volumes if volumes is not None else RAW_VOLUMES
        self.list_instances_calls = 0
        self.list_volumes_calls = 0

    def sdk_list_instances(self, compartment_id):
        self.list_instances_calls += 1
        return [dict(item) for item in self.instances]

    def sdk_list_boot_volumes(self, compartment_id):
        self.list_volumes_calls += 1
        return [dict(item) for item in self.volumes]


class TestSlimFunctions(unittest.TestCase):
    def test_slim_instances_default(self):
        slim = slim_instances(RAW_INSTANCES)
        self.assertEqual(len(slim), 3)
        self.assertEqual(set(slim[0].keys()),
                         {"id", "display-name", "shape", "lifecycle-state",
                          "time-created"})
        self.assertNotIn("shape-config", slim[0])

    def test_slim_instances_with_shape_config(self):
        slim = slim_instances(RAW_INSTANCES, include_shape_config=True)
        self.assertEqual(slim[0]["shape-config"],
                         {"ocpus": 1, "memory-in-gbs": 6})

    def test_simplify_boot_volumes(self):
        slim = simplify_boot_volumes(RAW_VOLUMES)
        self.assertEqual(len(slim), 2)
        self.assertEqual(slim[0]["size-in-gbs"], 50)
        self.assertEqual(slim[0]["availability-domain"],
                         "wgwP:AP-SINGAPORE-1-AD-1")


class TestQuotaDedup(unittest.TestCase):
    def test_free_tier_summary_fetches_instances_once(self):
        cli = _FakeCli()
        summary = get_free_tier_summary(cli, "ocid1.compartment.x")
        # 以前 get_a1_usage 与 get_e2_usage 各调一次 _list_instances（2 次），
        # 现在只取一次。
        self.assertEqual(cli.list_instances_calls, 1)
        self.assertEqual(cli.list_volumes_calls, 1)
        self.assertEqual(summary["a1"]["ocpu_used"], 1)
        self.assertEqual(summary["a1"]["memory_used_gb"], 6)
        self.assertEqual(summary["e2"]["count"], 1)
        self.assertEqual(summary["boot_volume"]["used_gb"], 50)

    def test_getters_use_passed_lists_without_sdk(self):
        cli = _FakeCli()
        a1 = get_a1_usage(cli, "ocid1.compartment.x",
                          instances=RAW_INSTANCES)
        e2 = get_e2_usage(cli, "ocid1.compartment.x",
                          instances=RAW_INSTANCES)
        boot = get_boot_volume_usage(cli, "ocid1.compartment.x",
                                     volumes=RAW_VOLUMES)
        self.assertEqual(cli.list_instances_calls, 0)
        self.assertEqual(cli.list_volumes_calls, 0)
        self.assertEqual(a1["ocpu_used"], 1)
        self.assertEqual(e2["count"], 1)
        self.assertEqual(boot["used_gb"], 50)

    def test_getters_still_fetch_when_not_passed(self):
        cli = _FakeCli()
        get_a1_usage(cli, "ocid1.compartment.x")
        get_e2_usage(cli, "ocid1.compartment.x")
        get_boot_volume_usage(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 2)
        self.assertEqual(cli.list_volumes_calls, 1)


class TestWebOciCache(unittest.TestCase):
    def setUp(self):
        oci_cache.clear_oci_cache()

    def tearDown(self):
        oci_cache.clear_oci_cache()

    def test_instances_shared_across_calls(self):
        cli = _FakeCli()
        first = oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        second = oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 1)
        self.assertIs(first, second)

    def test_volumes_shared_across_calls(self):
        cli = _FakeCli()
        first = oci_cache.cached_raw_boot_volumes(cli, "ocid1.compartment.x")
        second = oci_cache.cached_raw_boot_volumes(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_volumes_calls, 1)
        self.assertIs(first, second)

    def test_refresh_bypasses_cache(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x",
                                       refresh=True)
        self.assertEqual(cli.list_instances_calls, 2)

    def test_expiry_refetches(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        # 把过期点拨到过去，模拟 TTL 到期。
        key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x",
               "instances")
        with oci_cache._CACHE_LOCK:
            oci_cache._CACHE[key]["expires"] = time.monotonic() - 1
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 2)

    def test_different_compartment_not_shared(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.y")
        self.assertEqual(cli.list_instances_calls, 2)

    def test_clear_by_profile(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.clear_oci_cache("DEFAULT")
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 2)


class TestCachePrewarm(unittest.TestCase):
    def setUp(self):
        oci_cache.clear_oci_cache()
        oci_cache.stop_cache_prewarm()

    def tearDown(self):
        oci_cache.stop_cache_prewarm()
        oci_cache.clear_oci_cache()

    def _expire_soon(self, key):
        with oci_cache._CACHE_LOCK:
            oci_cache._CACHE[key]["expires"] = (
                time.monotonic() + oci_cache.OCI_CACHE_PREWARM_AHEAD - 1)

    def test_prewarm_refreshes_expiring_entry(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 1)
        key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x",
               "instances")
        self._expire_soon(key)
        self.assertEqual(oci_cache._prewarm_once(), 1)
        self.assertEqual(cli.list_instances_calls, 2)

    def test_prewarm_skips_fresh_entry(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(oci_cache._prewarm_once(), 0)
        self.assertEqual(cli.list_instances_calls, 1)

    def test_prewarm_empty_cache_noop(self):
        self.assertEqual(oci_cache._prewarm_once(), 0)

    def test_prewarm_refreshes_both_kinds(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_boot_volumes(cli, "ocid1.compartment.x")
        for kind in ("instances", "boot_volumes"):
            key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x", kind)
            self._expire_soon(key)
        self.assertEqual(oci_cache._prewarm_once(), 2)
        self.assertEqual(cli.list_instances_calls, 2)
        self.assertEqual(cli.list_volumes_calls, 2)

    def test_prewarm_survives_single_failure(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_boot_volumes(cli, "ocid1.compartment.x")
        for kind in ("instances", "boot_volumes"):
            key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x", kind)
            self._expire_soon(key)
        # 实例刷新抛错，启动盘仍应刷到，且 _prewarm_once 不抛异常。
        with mock.patch.object(
                _FakeCli, "sdk_list_instances",
                side_effect=RuntimeError("oci down")):
            self.assertEqual(oci_cache._prewarm_once(), 1)
        self.assertEqual(cli.list_volumes_calls, 2)

    def test_start_stop_thread(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x",
               "instances")
        self._expire_soon(key)
        with mock.patch.object(oci_cache, "OCI_CACHE_PREWARM_INTERVAL",
                               0.05):
            oci_cache.start_cache_prewarm()
            # 重复启动幂等：仍只有一个线程在跑。
            oci_cache.start_cache_prewarm()
            deadline = time.monotonic() + 2
            while (cli.list_instances_calls < 2
                   and time.monotonic() < deadline):
                time.sleep(0.05)
        self.assertEqual(cli.list_instances_calls, 2)
        oci_cache.stop_cache_prewarm()
        # 未启动时停掉不报错。
        oci_cache.stop_cache_prewarm()


if __name__ == "__main__":
    unittest.main()
