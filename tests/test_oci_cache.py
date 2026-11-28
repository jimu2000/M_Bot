#!/usr/bin/env python3
"""第 5 轮 CPU 优化测试：跨端点去重。

覆盖：
  - oci.instance.slim_instances / oci.volume.simplify_boot_volumes（纯函数）
  - oci.quota：get_free_tier_summary 同一次请求只取一次实例列表；
    get_a1_usage / get_e2_usage / get_boot_volume_usage 传入已取好的
    列表时不再调 SDK。
  - web.oci_cache：30 秒内跨端点共享同一份原始列表（SDK 只调一次）；
    refresh 绕过；过期后重取；clear 生效。
  - 第 7 轮：VNIC 挂载列表与公网 IP 映射进共享缓存；单飞
    （singleflight）——10 个并发冷请求只调一次 OCI。
"""

import threading
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
    {"id": "ocid1.instance.a", "display-name": "ARM_2026-11-26",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-26T00:00:00Z",
     "shape-config": {"ocpus": 1, "memory-in-gbs": 6}},
    {"id": "ocid1.instance.b", "display-name": "AMD_2026-11-26",
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


RAW_ATTACHMENTS = [
    {"instance-id": "ocid1.instance.a", "vnic-id": "ocid1.vnic.a1"},
    {"instance-id": "ocid1.instance.b", "vnic-id": "ocid1.vnic.b1"},
    {"instance-id": "ocid1.instance.a", "vnic-id": "ocid1.vnic.a2"},
]

RAW_VNICS = {
    "ocid1.vnic.a1": {"id": "ocid1.vnic.a1", "public-ip": "203.0.113.10",
                      "is-primary": True},
    "ocid1.vnic.b1": {"id": "ocid1.vnic.b1", "public-ip": "203.0.113.11",
                      "is-primary": True},
    "ocid1.vnic.a2": {"id": "ocid1.vnic.a2", "public-ip": None,
                      "is-primary": False},
}


class _FakeCli:
    """OCIClient 的替身：记录 SDK 调用次数，返回预设原始列表。"""

    def __init__(self, instances=None, volumes=None, attachments=None,
                 vnics=None):
        self.profile = "DEFAULT"
        self.region = "ap-singapore-1"
        self.instances = instances if instances is not None else RAW_INSTANCES
        self.volumes = volumes if volumes is not None else RAW_VOLUMES
        self.attachments = (attachments if attachments is not None
                            else RAW_ATTACHMENTS)
        self.vnics = vnics if vnics is not None else RAW_VNICS
        self.list_instances_calls = 0
        self.list_volumes_calls = 0
        self.list_attachments_calls = 0
        self.get_vnic_calls = 0
        self.get_vnic_ids = []

    def sdk_list_instances(self, compartment_id):
        self.list_instances_calls += 1
        return [dict(item) for item in self.instances]

    def sdk_list_boot_volumes(self, compartment_id):
        self.list_volumes_calls += 1
        return [dict(item) for item in self.volumes]

    def sdk_list_vnic_attachments(self, compartment_id):
        self.list_attachments_calls += 1
        return [dict(item) for item in self.attachments]

    def sdk_get_vnic(self, vnic_id):
        self.get_vnic_calls += 1
        self.get_vnic_ids.append(vnic_id)
        vnic = self.vnics.get(vnic_id)
        if vnic is None:
            raise RuntimeError("vnic not found: %s" % vnic_id)
        return dict(vnic)


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

    def test_prewarm_refreshes_vnic_entries(self):
        cli = _FakeCli()
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x")
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_attachments_calls, 1)
        self.assertEqual(cli.get_vnic_calls, 3)
        for kind in ("vnic_attachments", "vnic_public_ips"):
            key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x", kind)
            self._expire_soon(key)
        self.assertEqual(oci_cache._prewarm_once(), 2)
        # 挂载列表刷了一次；IP 映射复用刚刷好的挂载列表，VNIC 各查一次。
        self.assertEqual(cli.list_attachments_calls, 2)
        self.assertEqual(cli.get_vnic_calls, 6)

    def test_prewarm_vnic_failure_does_not_block_others(self):
        cli = _FakeCli()
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x")
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        for kind in ("instances", "vnic_public_ips"):
            key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x", kind)
            self._expire_soon(key)
        # 挂载列表条目彻底过期：IP 映射刷新时内部读挂载列表（refresh=False）
        # 会穿透到 SDK，同样抛错；两条 VNIC 刷新都失败，但实例仍应刷到。
        key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x",
               "vnic_attachments")
        with oci_cache._CACHE_LOCK:
            oci_cache._CACHE[key]["expires"] = time.monotonic() - 1
        # 挂载列表刷新抛错：VNIC 两条目都刷不到，但实例仍应刷到。
        with mock.patch.object(
                _FakeCli, "sdk_list_vnic_attachments",
                side_effect=RuntimeError("oci down")):
            self.assertEqual(oci_cache._prewarm_once(), 1)
        self.assertEqual(cli.list_instances_calls, 2)

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


class TestVnicCache(unittest.TestCase):
    """第 7 轮：VNIC 挂载列表与公网 IP 映射进共享缓存。"""

    def setUp(self):
        oci_cache.clear_oci_cache()

    def tearDown(self):
        oci_cache.clear_oci_cache()

    def test_attachments_shared_across_calls(self):
        cli = _FakeCli()
        first = oci_cache.cached_raw_vnic_attachments(
            cli, "ocid1.compartment.x")
        second = oci_cache.cached_raw_vnic_attachments(
            cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_attachments_calls, 1)
        self.assertIs(first, second)

    def test_ip_map_built_once_and_shared(self):
        cli = _FakeCli()
        mapping = oci_cache.cached_vnic_public_ips(
            cli, "ocid1.compartment.x")
        self.assertEqual(mapping, {
            "ocid1.vnic.a1": {"is-primary": True,
                              "public-ip": "203.0.113.10"},
            "ocid1.vnic.b1": {"is-primary": True,
                              "public-ip": "203.0.113.11"},
            "ocid1.vnic.a2": {"is-primary": False, "public-ip": None},
        })
        # 挂载列表 1 次 + 每个 VNIC 各 1 次；第二次命中，不再调 SDK。
        self.assertEqual(cli.list_attachments_calls, 1)
        self.assertEqual(cli.get_vnic_calls, 3)
        self.assertEqual(sorted(cli.get_vnic_ids),
                         ["ocid1.vnic.a1", "ocid1.vnic.a2",
                          "ocid1.vnic.b1"])
        second = oci_cache.cached_vnic_public_ips(
            cli, "ocid1.compartment.x")
        self.assertIs(mapping, second)
        self.assertEqual(cli.list_attachments_calls, 1)
        self.assertEqual(cli.get_vnic_calls, 3)

    def test_ip_map_single_vnic_failure_becomes_none(self):
        # a2 的 VNIC 查不到，记 None，不影响其他 VNIC。
        cli = _FakeCli(vnics={
            "ocid1.vnic.a1": dict(RAW_VNICS["ocid1.vnic.a1"]),
            "ocid1.vnic.b1": dict(RAW_VNICS["ocid1.vnic.b1"])})
        mapping = oci_cache.cached_vnic_public_ips(
            cli, "ocid1.compartment.x")
        self.assertEqual(mapping["ocid1.vnic.a1"]["public-ip"],
                         "203.0.113.10")
        self.assertEqual(mapping["ocid1.vnic.b1"]["public-ip"],
                         "203.0.113.11")
        self.assertEqual(mapping["ocid1.vnic.a2"],
                         {"is-primary": False, "public-ip": None})

    def test_vnic_refresh_bypasses_cache(self):
        cli = _FakeCli()
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x")
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x",
                                              refresh=True)
        self.assertEqual(cli.list_attachments_calls, 2)
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x",
                                         refresh=True)
        self.assertEqual(cli.get_vnic_calls, 6)

    def test_vnic_expiry_refetches(self):
        cli = _FakeCli()
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x")
        key = ("DEFAULT", "ap-singapore-1", "ocid1.compartment.x",
               "vnic_attachments")
        with oci_cache._CACHE_LOCK:
            oci_cache._CACHE[key]["expires"] = time.monotonic() - 1
        oci_cache.cached_raw_vnic_attachments(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_attachments_calls, 2)

    def test_vnic_different_compartment_not_shared(self):
        cli = _FakeCli()
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.y")
        self.assertEqual(cli.list_attachments_calls, 2)
        self.assertEqual(cli.get_vnic_calls, 6)

    def test_clear_by_profile_clears_vnic(self):
        cli = _FakeCli()
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        oci_cache.clear_oci_cache("DEFAULT")
        oci_cache.cached_vnic_public_ips(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_attachments_calls, 2)
        self.assertEqual(cli.get_vnic_calls, 6)


class TestSingleflight(unittest.TestCase):
    """第 7 轮：单飞——同 key 并发冷请求只调一次 OCI。"""

    def setUp(self):
        oci_cache.clear_oci_cache()

    def tearDown(self):
        oci_cache.clear_oci_cache()
        self.assertEqual(oci_cache._INFLIGHT, {})

    def _run_concurrent(self, fn, count=10):
        """10 个线程同时调 fn，返回 (results, errors) 两个列表。"""
        barrier = threading.Barrier(count)
        results = [None] * count
        errors = [None] * count

        def _work(i):
            barrier.wait(timeout=10)
            try:
                results[i] = fn()
            except Exception as exc:  # noqa: BLE001 - 收集起来断言
                errors[i] = exc

        threads = [threading.Thread(target=_work, args=(i,))
                   for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return results, errors

    def test_ten_concurrent_cold_requests_one_sdk_call(self):
        """10 个并发冷请求：SDK 只调 1 次，10 人拿到同一份结果。"""
        class _SlowCli(_FakeCli):
            def sdk_list_instances(self, compartment_id):
                time.sleep(0.3)  # 慢速加载，确保 10 个请求真的重叠
                return super().sdk_list_instances(compartment_id)

        cli = _SlowCli()
        results, errors = self._run_concurrent(
            lambda: oci_cache.cached_raw_instances(
                cli, "ocid1.compartment.x"))
        self.assertEqual(errors, [None] * 10)
        self.assertEqual(cli.list_instances_calls, 1)
        for result in results:
            self.assertIs(result, results[0])

    def test_ten_concurrent_vnic_map_one_load(self):
        """10 个并发冷请求取 IP 映射：挂载列表 1 次，每 VNIC 各 1 次。"""
        class _SlowCli(_FakeCli):
            def sdk_list_vnic_attachments(self, compartment_id):
                time.sleep(0.3)
                return super().sdk_list_vnic_attachments(compartment_id)

        cli = _SlowCli()
        results, errors = self._run_concurrent(
            lambda: oci_cache.cached_vnic_public_ips(
                cli, "ocid1.compartment.x"))
        self.assertEqual(errors, [None] * 10)
        self.assertEqual(cli.list_attachments_calls, 1)
        self.assertEqual(cli.get_vnic_calls, 3)
        for result in results:
            self.assertIs(result, results[0])

    def test_loader_failure_propagates_and_retries(self):
        """加载失败时等待者同样拿到异常；恢复后下一次请求重试成功。"""
        cli = _FakeCli()
        with mock.patch.object(
                _FakeCli, "sdk_list_instances",
                side_effect=RuntimeError("oci down")):
            _results, errors = self._run_concurrent(
                lambda: oci_cache.cached_raw_instances(
                    cli, "ocid1.compartment.x"))
        self.assertEqual(len(errors), 10)
        self.assertTrue(all(isinstance(e, RuntimeError) for e in errors))
        # 失败不进缓存：恢复后重试能拿到数据，之后命中缓存。
        result = oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(len(result), 3)
        self.assertEqual(cli.list_instances_calls, 1)
        oci_cache.cached_raw_instances(cli, "ocid1.compartment.x")
        self.assertEqual(cli.list_instances_calls, 1)


if __name__ == "__main__":
    unittest.main()
