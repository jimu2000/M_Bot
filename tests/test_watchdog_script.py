#!/usr/bin/env python3
"""看门狗模板脚本（scripts/oracle_traffic_guard.py）测试。

模板是独立脚本（不 import 项目模块），用 importlib 按文件路径加载。
重点：SSH 端口自动识别三路回落、Bark 配置三级读取、熔断命令用识别
出的端口、模板里不能有写死的 Key 与端口。
"""

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(ROOT, "scripts", "oracle_traffic_guard.py")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "oracle_traffic_guard_tpl", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    # 加载时 get_active_iface 会跑 ip route，mock 掉 run_cmd 的底层调用。
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="default via 10.0.0.1 dev eth0\n", stderr="")
        spec.loader.exec_module(module)
    return module


class TestTemplateHasNoSecrets(unittest.TestCase):
    """模板不能含写死的 Key 与端口（公开仓库可直接发）。"""

    def setUp(self):
        with open(SCRIPT_PATH, encoding="utf-8") as f:
            self.text = f.read()

    def test_no_hardcoded_bark_keys(self):
        # 顶层赋值 BARK_KEY = "..." / BARK_ENC_KEY = "..." 不许出现
        # （读取逻辑里的 env.get("BARK_KEY") 是字符串键名，不算赋值）。
        self.assertNotRegex(self.text, r"^BARK_KEY\s*=")
        self.assertNotRegex(self.text, r"^BARK_ENC_KEY\s*=")
        self.assertNotRegex(self.text, r"^BARK_SERVER\s*=")

    def test_no_hardcoded_ssh_port(self):
        self.assertNotRegex(self.text, r"^SSH_PORT\s*=")
        # 注释里可以提 14422（“不要写死 22/14422”），但不能是赋值。
        self.assertNotRegex(self.text, r"SSH_PORT\s*=\s*\d+")

    def test_has_auto_detect_marker(self):
        self.assertIn("def detect_ssh_port", self.text)
        self.assertIn("load_bark_settings", self.text)


class TestDetectSshPort(unittest.TestCase):

    def setUp(self):
        self.mod = _load_script()

    def test_ss_listening_port_wins(self):
        ss_out = ("LISTEN 0 128 0.0.0.0:14422 0.0.0.0:* "
                  "users:((\"sshd\",pid=123,fd=3))\n")
        with patch.object(self.mod, "run_cmd",
                          return_value=(0, ss_out, "")):
            self.assertEqual(self.mod.detect_ssh_port(), 14422)

    def test_ss_ipv6_line(self):
        ss_out = ("LISTEN 0 128 [::]:2222 [::]:* "
                  "users:((\"sshd\",pid=123,fd=3))\n")
        with patch.object(self.mod, "run_cmd",
                          return_value=(0, ss_out, "")):
            self.assertEqual(self.mod.detect_ssh_port(), 2222)

    def test_sshd_config_fallback(self):
        real_open = open
        with patch.object(self.mod, "run_cmd", return_value=(1, "", "")), \
                tempfile.NamedTemporaryFile(
                    "w", suffix=".conf", delete=False) as f:
            f.write("# comment\nPort 2222\n")
            conf = f.name
        try:
            def fake_open(path, **kw):
                if path == "/etc/ssh/sshd_config":
                    return real_open(conf, **kw)
                return real_open(path, **kw)

            with patch("builtins.open", side_effect=fake_open):
                self.assertEqual(self.mod.detect_ssh_port(), 2222)
        finally:
            os.unlink(conf)

    def test_default_22(self):
        with patch.object(self.mod, "run_cmd", return_value=(1, "", "")), \
                patch("builtins.open", side_effect=OSError("no file")):
            self.assertEqual(self.mod.detect_ssh_port(), 22)


class TestLoadBarkSettings(unittest.TestCase):

    def setUp(self):
        self.mod = _load_script()

    def test_env_wins_over_file(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".conf", delete=False) as f:
            f.write("BARK_SERVER=https://file.example.com\n"
                    "BARK_KEY=file-key\n"
                    "BARK_ENC_KEY=file-enc\n")
            path = f.name
        try:
            server, key, enc = self.mod.load_bark_settings(
                path=path, environ={"BARK_KEY": "env-key",
                                    "BARK_ENC_KEY": "env-enc",
                                    "BARK_SERVER": "https://env.example.com"})
            self.assertEqual((server, key, enc),
                             ("https://env.example.com", "env-key", "env-enc"))
        finally:
            os.unlink(path)

    def test_file_used_when_no_env(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".conf", delete=False) as f:
            f.write("# comment\nBARK_KEY=file-key\n"
                    "BARK_ENC_KEY=file-enc\n")
            path = f.name
        try:
            server, key, enc = self.mod.load_bark_settings(
                path=path, environ={})
            self.assertEqual((server, key, enc),
                             ("https://api.day.app", "file-key", "file-enc"))
        finally:
            os.unlink(path)

    def test_nothing_configured(self):
        server, key, enc = self.mod.load_bark_settings(
            path="/nonexistent/nope", environ={})
        self.assertEqual((server, key, enc), ("https://api.day.app", "", ""))


class TestTripUsesDetectedPort(unittest.TestCase):
    """熔断的 iptables 逃生门必须放行自动识别出的端口。"""

    def setUp(self):
        self.mod = _load_script()

    def test_trip_rule_uses_detected_port(self):
        commands = []

        def fake_run(cmd):
            commands.append(cmd)
            return 0, "", ""

        with patch.object(self.mod, "run_cmd", side_effect=fake_run), \
                patch.object(self.mod, "detect_ssh_port", return_value=14422), \
                patch.object(self.mod, "get_ip_and_geo",
                             return_value=("203.0.113.10", "新加坡")), \
                patch.object(self.mod, "get_monthly_rx_tx_gb",
                             return_value=(1.0, 2.0)), \
                patch.object(self.mod, "send_bark_notification"):
            self.mod.trip_circuit_breaker("测试")
        sport_rules = [c for c in commands if "--sport" in c]
        self.assertEqual(len(sport_rules), 1)
        self.assertIn("--sport 14422", sport_rules[0])
        self.assertTrue(any(c.endswith("-j DROP") for c in commands))

    def test_trip_rule_uses_port_22_when_detected(self):
        commands = []

        def fake_run(cmd):
            commands.append(cmd)
            return 0, "", ""

        with patch.object(self.mod, "run_cmd", side_effect=fake_run), \
                patch.object(self.mod, "detect_ssh_port", return_value=22), \
                patch.object(self.mod, "get_ip_and_geo",
                             return_value=("203.0.113.10", "新加坡")), \
                patch.object(self.mod, "get_monthly_rx_tx_gb",
                             return_value=(1.0, 2.0)), \
                patch.object(self.mod, "send_bark_notification"):
            self.mod.trip_circuit_breaker("测试")
        self.assertTrue(any("--sport 22" in c for c in commands))


class TestCheckTraffic(unittest.TestCase):

    def setUp(self):
        self.mod = _load_script()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = os.path.join(self._tmp.name, "state.json")
        self._state_patch = patch.object(
            self.mod, "STATE_FILE", self.state_file)
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self._dir_patch = patch.object(
            self.mod, "STATE_DIR", self._tmp.name)
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)

    def _run_check(self, tx_gb, mode=0):
        with patch.object(self.mod, "get_monthly_tx_bytes",
                          return_value=int(tx_gb * (1024 ** 3))), \
                patch.object(self.mod, "OVER_LIMIT_MODE", mode), \
                patch.object(self.mod, "ensure_chain"), \
                patch.object(self.mod, "get_ip_and_geo",
                             return_value=("203.0.113.10", "新加坡")), \
                patch.object(self.mod, "get_monthly_rx_tx_gb",
                             return_value=(1.0, tx_gb)), \
                patch.object(self.mod, "send_bark_notification") as bark, \
                patch.object(self.mod, "trip_circuit_breaker") as trip, \
                patch.object(self.mod, "apply_rate_limit") as limit, \
                patch.object(self.mod, "is_circuit_broken",
                             return_value=False), \
                patch.object(self.mod, "is_rate_limited",
                             return_value=False):
            self.mod.check_traffic()
            return bark, trip, limit

    def test_stage_notification_once_per_month(self):
        bark, _, _ = self._run_check(6.0)
        self.assertEqual(bark.call_count, 1)
        self.assertIn("5GB", bark.call_args.args[0])
        # 同月再跑：5GB 档已推过，不再推。
        bark2, _, _ = self._run_check(6.0)
        self.assertEqual(bark2.call_count, 0)

    def test_over_limit_trips_in_mode_0(self):
        _bark, trip, limit = self._run_check(250.0, mode=0)
        trip.assert_called_once()
        limit.assert_not_called()

    def test_over_limit_rate_limits_in_mode_1(self):
        _bark, trip, limit = self._run_check(250.0, mode=1)
        limit.assert_called_once()
        trip.assert_not_called()

    def test_under_limit_no_action(self):
        _bark, trip, limit = self._run_check(3.0)
        trip.assert_not_called()
        limit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
