#!/usr/bin/env python3
"""web/routes/instances.py 后台 CPU 型号检测节流（第 7 轮）。

覆盖：
  - 并发检测最多 2 个（信号量），5 台未缓存实例不会同时起 5 个 SSH 握手；
  - 60 秒冷却期内不重复尝试（在途去重 + 冷却）；
  - 冷却过期后可再试；
  - 已有缓存型号的实例不检测。

运行：python3 -m unittest tests.test_instance_cpu_fetch -v
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web.routes.instances as instances_mod  # noqa: E402

INSTANCES = [
    {"id": "ocid1.instance.oc1..%s" % c, "lifecycle-state": "RUNNING"}
    for c in "abcde"
]
IPS = ["203.0.113.%d" % (10 + i) for i in range(5)]


class TestCpuFetchThrottle(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.current = 0
        self.max_concurrent = 0
        self.lock = threading.Lock()
        self._patcher = mock.patch.object(
            instances_mod, "get_cpu_model", side_effect=self._fake_get)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        with instances_mod._CPU_FETCH_LOCK:
            instances_mod._CPU_FETCH_IN_FLIGHT.clear()
            instances_mod._CPU_FETCH_LAST_ATTEMPT.clear()

    def tearDown(self):
        self._wait_idle()
        with instances_mod._CPU_FETCH_LOCK:
            instances_mod._CPU_FETCH_IN_FLIGHT.clear()
            instances_mod._CPU_FETCH_LAST_ATTEMPT.clear()

    def _fake_get(self, ocid, host, run_fn):
        """假检测：记调用、记并发峰值、睡 0.2 秒模拟 SSH 握手。"""
        with self.lock:
            self.calls.append(ocid)
            self.current += 1
            self.max_concurrent = max(self.max_concurrent, self.current)
        time.sleep(0.2)
        with self.lock:
            self.current -= 1

    def _wait_idle(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with instances_mod._CPU_FETCH_LOCK:
                if not instances_mod._CPU_FETCH_IN_FLIGHT:
                    return True
            time.sleep(0.05)
        return False

    def test_max_two_concurrent_detections(self):
        """5 台未缓存实例：同时最多 2 个 SSH 检测，5 台都会被检测到。"""
        instances_mod._refresh_cpu_models_background(INSTANCES, IPS, {})
        self.assertTrue(self._wait_idle())
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(sorted(self.calls),
                         sorted(inst["id"] for inst in INSTANCES))
        self.assertEqual(self.max_concurrent, 2)

    def test_cooldown_blocks_immediate_retry(self):
        """在途时再调不产生新尝试；跑完后 60 秒冷却期内仍不重试；
        把上次尝试时间拨到 61 秒前，冷却过期后可再试。"""
        instances_mod._refresh_cpu_models_background(INSTANCES, IPS, {})
        # 在途时再调一次：不产生新尝试。
        instances_mod._refresh_cpu_models_background(INSTANCES, IPS, {})
        self.assertTrue(self._wait_idle())
        self.assertEqual(len(self.calls), 5)
        # 跑完后立即再调：冷却期内仍不重试。
        instances_mod._refresh_cpu_models_background(INSTANCES, IPS, {})
        time.sleep(0.3)
        self.assertEqual(len(self.calls), 5)
        # 冷却过期：可再试。
        with instances_mod._CPU_FETCH_LOCK:
            for key in list(instances_mod._CPU_FETCH_LAST_ATTEMPT):
                instances_mod._CPU_FETCH_LAST_ATTEMPT[key] = (
                    time.monotonic() - 61)
        instances_mod._refresh_cpu_models_background(INSTANCES, IPS, {})
        self.assertTrue(self._wait_idle())
        self.assertEqual(len(self.calls), 10)

    def test_cached_model_skips_detection(self):
        """已有缓存型号的实例不检测。"""
        cpu_models = {inst["id"]: {"model": "AMD EPYC 7J13"}
                      for inst in INSTANCES}
        instances_mod._refresh_cpu_models_background(
            INSTANCES, IPS, cpu_models)
        time.sleep(0.3)
        self.assertEqual(self.calls, [])

    def test_stopped_or_missing_ip_skipped(self):
        """STOPPED / 无 IP 的实例不检测（旧行为保持）。"""
        mixed = [
            {"id": "ocid1.instance.oc1..a", "lifecycle-state": "STOPPED"},
            {"id": "ocid1.instance.oc1..b", "lifecycle-state": "RUNNING"},
            {"id": "", "lifecycle-state": "RUNNING"},
        ]
        ips = ["203.0.113.10", None, "203.0.113.12"]
        instances_mod._refresh_cpu_models_background(mixed, ips, {})
        time.sleep(0.3)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
