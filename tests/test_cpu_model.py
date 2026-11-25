#!/usr/bin/env python3
"""CPU 型号检测的单元测试（解析 / 缓存 / 获取，不碰真实 SSH）。

运行：python3 -m unittest tests.test_cpu_model -v

覆盖：tgbot.cpu_model 的 parse_cpu_model / load_cpu_models /
cached_cpu_model / save_cpu_model / delete_cpu_model /
prune_cpu_models / host_for_ip / fetch_cpu_model / get_cpu_model。
缓存文件用临时路径，不污染真实数据目录。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import cpu_model as cm  # noqa: E402

# Placeholders (no real OCID / IP in tests). 占位符（测试里不出现真实 OCID / IP）。
FAKE_OCID = "ocid1.instance.oc1..cputest1"
FAKE_OCID_2 = "ocid1.instance.oc1..cputest2"
FAKE_HOST = {"host": "203.0.113.10", "port": 14422, "username": "root",
             "key_path": "/root/.ssh/rbot_guard"}


class TestParseCpuModel(unittest.TestCase):
    """parse_cpu_model：原始行 -> 短型号。"""

    def test_amd_7j13(self):
        self.assertEqual(
            cm.parse_cpu_model(
                "model name\t: AMD EPYC 7J13 64-Core Processor"),
            "AMD EPYC 7J13")

    def test_amd_7742(self):
        self.assertEqual(
            cm.parse_cpu_model("model name : AMD EPYC 7742 64-Core Processor"),
            "AMD EPYC 7742")

    def test_amd_7551(self):
        self.assertEqual(
            cm.parse_cpu_model("model name : AMD EPYC 7551 32-Core Processor"),
            "AMD EPYC 7551")

    def test_ampere_altra_lscpu(self):
        self.assertEqual(
            cm.parse_cpu_model("Model name: Ampere(R) Altra(R)"),
            "Ampere Altra")

    def test_neoverse_n1_maps_to_ampere_altra(self):
        # An A1 instance's lscpu only reports the core codename Neoverse-N1; display the chip name. A1 实例的 lscpu 只报核心代号 Neoverse-N1，显示成芯片名。
        self.assertEqual(
            cm.parse_cpu_model("Model name: Neoverse-N1"),
            "Ampere Altra")

    def test_intel_xeon(self):
        self.assertEqual(
            cm.parse_cpu_model(
                "model name : Intel(R) Xeon(R) Platinum 8272CL CPU @ 2.60GHz"),
            "Intel Xeon Platinum 8272CL")

    def test_first_line_only(self):
        # From multi-line output, take only the first line that has a colon. 多行输出只取第一行有冒号的值。
        self.assertEqual(
            cm.parse_cpu_model("Model name: AMD EPYC 7J13 64-Core Processor\n"
                               "Model name: AMD EPYC 7742 64-Core Processor"),
            "AMD EPYC 7J13")

    def test_empty_and_garbage(self):
        self.assertIsNone(cm.parse_cpu_model(""))
        self.assertIsNone(cm.parse_cpu_model(None))
        self.assertIsNone(cm.parse_cpu_model("no colon here"))
        self.assertIsNone(cm.parse_cpu_model("model name :   "))

    def test_unknown_model_truncated(self):
        long_name = "x" * 100
        self.assertEqual(cm.parse_cpu_model(f"model name : {long_name}"),
                         "x" * cm.UNKNOWN_MODEL_MAX_LEN)


class CpuModelCacheTestCase(unittest.TestCase):
    """缓存测试基类：CPU_MODELS_FILE 指到临时文件。"""

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.tmp_path)
        self._patch = patch.object(cm, "CPU_MODELS_FILE", self.tmp_path)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(lambda: os.path.exists(self.tmp_path)
                        and os.unlink(self.tmp_path))


class TestCache(CpuModelCacheTestCase):
    """缓存文件的读写 / 删除 / 清理。"""

    def test_save_and_load_roundtrip(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        data = cm.load_cpu_models()
        self.assertEqual(data[FAKE_OCID]["model"], "AMD EPYC 7J13")
        self.assertIn("checked_at", data[FAKE_OCID])
        self.assertEqual(cm.cached_cpu_model(FAKE_OCID), "AMD EPYC 7J13")

    def test_missing_file_is_empty(self):
        self.assertEqual(cm.load_cpu_models(), {})
        self.assertIsNone(cm.cached_cpu_model(FAKE_OCID))

    def test_corrupt_file_is_empty(self):
        with open(self.tmp_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(cm.load_cpu_models(), {})

    def test_save_writes_0600(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        self.assertEqual(os.stat(self.tmp_path).st_mode & 0o777, 0o600)

    def test_delete_removes_entry(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        cm.save_cpu_model(FAKE_OCID_2, "Ampere Altra")
        cm.delete_cpu_model(FAKE_OCID)
        self.assertIsNone(cm.cached_cpu_model(FAKE_OCID))
        self.assertEqual(cm.cached_cpu_model(FAKE_OCID_2), "Ampere Altra")

    def test_prune_keeps_only_listed(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        cm.save_cpu_model(FAKE_OCID_2, "Ampere Altra")
        removed = cm.prune_cpu_models([FAKE_OCID_2])
        self.assertEqual(removed, 1)
        self.assertIsNone(cm.cached_cpu_model(FAKE_OCID))
        self.assertEqual(cm.cached_cpu_model(FAKE_OCID_2), "Ampere Altra")

    def test_prune_nothing_to_remove(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        self.assertEqual(cm.prune_cpu_models([FAKE_OCID]), 0)


class TestHostForIp(unittest.TestCase):
    """host_for_ip：有密钥+公网 IP 才建成，否则 None。"""

    def test_with_key_and_ip(self):
        with patch("tgbot.config.GUARD_SSH_KEY", "/root/.ssh/rbot_guard"):
            host = cm.host_for_ip("203.0.113.10")
        self.assertEqual(host["host"], "203.0.113.10")
        self.assertEqual(host["port"], 14422)
        self.assertEqual(host["username"], "root")

    def test_no_key_returns_none(self):
        with patch("tgbot.config.GUARD_SSH_KEY", ""):
            self.assertIsNone(cm.host_for_ip("203.0.113.10"))

    def test_no_ip_returns_none(self):
        with patch("tgbot.config.GUARD_SSH_KEY", "/root/.ssh/rbot_guard"):
            self.assertIsNone(cm.host_for_ip(None))
            self.assertIsNone(cm.host_for_ip(""))


class TestFetchCpuModel(unittest.TestCase):
    """fetch_cpu_model：SSH 命令的输出解析与失败处理。"""

    def test_success_parses_model(self):
        run = lambda host, cmd, timeout: (  # noqa: E731
            0, "model name : AMD EPYC 7742 64-Core Processor", "")
        self.assertEqual(cm.fetch_cpu_model(FAKE_HOST, run),
                         "AMD EPYC 7742")

    def test_nonzero_exit_returns_none(self):
        run = lambda host, cmd, timeout: (1, "", "not found")  # noqa: E731
        self.assertIsNone(cm.fetch_cpu_model(FAKE_HOST, run))

    def test_exception_returns_none(self):
        def run(host, cmd, timeout):
            raise RuntimeError("connection refused")
        self.assertIsNone(cm.fetch_cpu_model(FAKE_HOST, run))

    def test_no_host_returns_none(self):
        calls = []
        run = lambda host, cmd, timeout: calls.append(1) or (0, "", "")  # noqa: E731
        self.assertIsNone(cm.fetch_cpu_model(None, run))
        self.assertEqual(calls, [])


class TestGetCpuModel(CpuModelCacheTestCase):
    """get_cpu_model：缓存优先，未命中才 SSH，失败不抛异常。"""

    def test_cache_hit_skips_ssh(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7J13")
        calls = []
        run = lambda host, cmd, timeout: calls.append(1) or (0, "", "")  # noqa: E731
        self.assertEqual(cm.get_cpu_model(FAKE_OCID, FAKE_HOST, run),
                         "AMD EPYC 7J13")
        self.assertEqual(calls, [])

    def test_cache_miss_fetches_and_caches(self):
        run = lambda host, cmd, timeout: (  # noqa: E731
            0, "model name : AMD EPYC 7J13 64-Core Processor", "")
        self.assertEqual(cm.get_cpu_model(FAKE_OCID, FAKE_HOST, run),
                         "AMD EPYC 7J13")
        self.assertEqual(cm.cached_cpu_model(FAKE_OCID), "AMD EPYC 7J13")

    def test_fetch_failure_returns_none(self):
        run = lambda host, cmd, timeout: (1, "", "error")  # noqa: E731
        self.assertIsNone(cm.get_cpu_model(FAKE_OCID, FAKE_HOST, run))
        self.assertIsNone(cm.cached_cpu_model(FAKE_OCID))

    def test_force_refetches(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7742")
        run = lambda host, cmd, timeout: (  # noqa: E731
            0, "model name : AMD EPYC 7J13 64-Core Processor", "")
        self.assertEqual(
            cm.get_cpu_model(FAKE_OCID, FAKE_HOST, run, force=True),
            "AMD EPYC 7J13")

    def test_force_failure_keeps_old_value(self):
        cm.save_cpu_model(FAKE_OCID, "AMD EPYC 7742")
        run = lambda host, cmd, timeout: (1, "", "error")  # noqa: E731
        self.assertEqual(
            cm.get_cpu_model(FAKE_OCID, FAKE_HOST, run, force=True),
            "AMD EPYC 7742")


if __name__ == "__main__":
    unittest.main()
