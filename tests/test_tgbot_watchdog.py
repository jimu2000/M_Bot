#!/usr/bin/env python3
"""tgbot/watchdog.py 测试：安装/关闭/状态编排（假 runner，不连 SSH）。"""

import os
import unittest

from tgbot.watchdog import (
    WATCHDOG_CONFIG_PATH,
    WATCHDOG_SCRIPT_PATH,
    WatchdogError,
    bark_settings_for_install,
    disable_watchdog,
    guard_status_label,
    install_watchdog,
    parse_bark_url,
    parse_extracted_keys,
    parse_probe_output,
    probe_command,
    render_bark_config,
)

FAKE_HOST = {"host": "203.0.113.10", "port": 14422, "username": "root",
             "key_path": "/root/.ssh/rbot_guard"}
BARK_CFG = {"url": "https://api.day.app/devicekey22charsxx/",
            "key": "a" * 32, "enabled": True}

PROBE_NOT_INSTALLED = "SCRIPT=0\nAUTO=0\nCRON=0\nTRIPPED=0\nLIMITED=0\n"
PROBE_OLD_INSTALLED = ("SCRIPT=1\nAUTO=0\nCRON=1\nMODE=0\n"
                       "TRIPPED=0\nLIMITED=0\n")
PROBE_OLD_NOT_ENABLED = ("SCRIPT=1\nAUTO=0\nCRON=0\nMODE=1\n"
                         "TRIPPED=0\nLIMITED=0\n")
PROBE_NEW_ENABLED = ("SCRIPT=1\nAUTO=1\nCRON=1\nMODE=0\n"
                     "TRIPPED=0\nLIMITED=0\n")
PROBE_NEW_NOT_ENABLED = ("SCRIPT=1\nAUTO=1\nCRON=0\nMODE=0\n"
                         "TRIPPED=0\nLIMITED=0\n")


class FakeRunner:
    """记录命令与上传，probe/keys 输出可预设。"""

    def __init__(self, probe_output=PROBE_NOT_INSTALLED, keys_output=""):
        self.commands = []
        self.files = {}
        self.probe_output = probe_output
        self.keys_output = keys_output

    def run_command(self, host, command, timeout=None):
        self.commands.append(command)
        if "SCRIPT=" in command:
            return 0, self.probe_output, ""
        if command.startswith("grep -E '^BARK_KEY'"):
            return 0, self.keys_output, ""
        return 0, "", ""

    def put_file(self, host, content, remote_path, mode=0o644):
        self.files[remote_path] = (content, mode)


class TestParseBarkUrl(unittest.TestCase):

    def test_official(self):
        server, key = parse_bark_url("https://api.day.app/abc123/")
        self.assertEqual((server, key), ("https://api.day.app", "abc123"))

    def test_self_hosted_no_trailing_slash(self):
        server, key = parse_bark_url("https://bark.example.com/mykey")
        self.assertEqual((server, key),
                         ("https://bark.example.com", "mykey"))

    def test_bad_urls(self):
        self.assertEqual(parse_bark_url(""), (None, None))
        self.assertEqual(parse_bark_url("https://api.day.app/"), (None, None))
        self.assertEqual(parse_bark_url("not-a-url"), (None, None))


class TestBarkSettingsForInstall(unittest.TestCase):

    def test_complete(self):
        self.assertEqual(bark_settings_for_install(BARK_CFG),
                         ("https://api.day.app", "devicekey22charsxx",
                          "a" * 32))

    def test_incomplete(self):
        self.assertIsNone(bark_settings_for_install(None))
        self.assertIsNone(bark_settings_for_install({"url": "", "key": ""}))
        self.assertIsNone(bark_settings_for_install(
            {"url": "https://api.day.app/k/", "key": "short"}))


class TestRenderBarkConfig(unittest.TestCase):

    def test_format(self):
        text = render_bark_config("https://api.day.app", "devkey", "e" * 32)
        self.assertEqual(text, "BARK_SERVER=https://api.day.app\n"
                               "BARK_KEY=devkey\n"
                               f"BARK_ENC_KEY={'e' * 32}\n")


class TestParseProbeOutput(unittest.TestCase):

    def test_not_installed(self):
        status = parse_probe_output(PROBE_NOT_INSTALLED)
        self.assertEqual(status, {"installed": False, "auto": False,
                                  "enabled": False, "mode": None,
                                  "tripped": False, "limited": False})

    def test_new_enabled(self):
        status = parse_probe_output(PROBE_NEW_ENABLED)
        self.assertTrue(status["installed"])
        self.assertTrue(status["auto"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["mode"], 0)

    def test_old_not_enabled(self):
        status = parse_probe_output(PROBE_OLD_NOT_ENABLED)
        self.assertTrue(status["installed"])
        self.assertFalse(status["auto"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["mode"], 1)

    def test_tripped(self):
        status = parse_probe_output(
            "SCRIPT=1\nAUTO=1\nCRON=1\nMODE=0\nTRIPPED=1\nLIMITED=0\n")
        self.assertTrue(status["tripped"])

    def test_empty(self):
        status = parse_probe_output("")
        self.assertFalse(status["installed"])


class TestGuardStatusLabel(unittest.TestCase):

    def test_labels(self):
        self.assertEqual(guard_status_label(None), "看门狗：未知（SSH 连不上）")
        self.assertEqual(guard_status_label({"installed": False}),
                         "看门狗：未安装")
        self.assertEqual(guard_status_label(
            {"installed": True, "enabled": False}), "看门狗：已安装，未开启")
        self.assertEqual(guard_status_label(
            {"installed": True, "enabled": True, "mode": 0}),
            "看门狗：已开启（阻断模式）")
        self.assertEqual(guard_status_label(
            {"installed": True, "enabled": True, "mode": 1}),
            "看门狗：已开启（限速模式）")
        self.assertEqual(guard_status_label(
            {"installed": True, "enabled": True, "tripped": True}),
            "看门狗：已熔断断网！")
        self.assertEqual(guard_status_label(
            {"installed": True, "enabled": True, "limited": True}),
            "看门狗：限速中")


class TestParseExtractedKeys(unittest.TestCase):

    def test_both_keys(self):
        keys = parse_extracted_keys(
            "BARK_KEY = \"devkey123\"\nBARK_ENC_KEY = \"enc456\"\n")
        self.assertEqual(keys, ("devkey123", "enc456"))

    def test_single_quotes(self):
        keys = parse_extracted_keys("BARK_KEY = 'devkey123'\n")
        self.assertEqual(keys, ("devkey123", ""))

    def test_placeholders_ignored(self):
        keys = parse_extracted_keys(
            "BARK_KEY = \"xxxxxxxxx\"\n"
            "BARK_ENC_KEY = \"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\"\n")
        self.assertEqual(keys, ("", ""))

    def test_empty(self):
        self.assertEqual(parse_extracted_keys(""), ("", ""))


class TestInstallWatchdog(unittest.TestCase):

    def test_fresh_install_full_flow(self):
        runner = FakeRunner(probe_output=PROBE_NOT_INSTALLED)
        result = install_watchdog(
            FAKE_HOST, BARK_CFG, runner.run_command, runner.put_file,
            "ensure-cmd", template_text="# template\n")
        self.assertEqual(result["action"], "installed")
        self.assertTrue(result["test_ok"])
        # Order: probe -> dependencies -> write config -> upload script -> cron -> test. 顺序：probe → 依赖 → 写配置 → 传脚本 → cron → test。
        self.assertIn("SCRIPT=", runner.commands[0])
        self.assertIn("ensure-cmd", runner.commands)
        self.assertTrue(any("crontab -" in c and "oracle_traffic_guard"
                            in c for c in runner.commands))
        self.assertTrue(any(c.endswith(" test") for c in runner.commands))
        # Bark config comes from the bot config, mode 600; the script is 755. Bark 配置来自机器人配置，600 权限；脚本 755。
        cfg, cfg_mode = runner.files[WATCHDOG_CONFIG_PATH]
        self.assertIn("BARK_KEY=devicekey22charsxx", cfg)
        self.assertIn("BARK_ENC_KEY=" + "a" * 32, cfg)
        self.assertEqual(cfg_mode, 0o600)
        script, script_mode = runner.files[WATCHDOG_SCRIPT_PATH]
        self.assertEqual(script, "# template\n")
        self.assertEqual(script_mode, 0o755)

    def test_upgrade_keeps_host_keys(self):
        # Old script exists: the Key extracted from the old file wins over the bot config. 旧脚本在：从旧文件提取的 Key 优先于机器人配置。
        keys_out = ("BARK_KEY = \"old-device-key\"\n"
                    "BARK_ENC_KEY = \"" + "b" * 32 + "\"\n")
        runner = FakeRunner(probe_output=PROBE_OLD_INSTALLED,
                            keys_output=keys_out)
        result = install_watchdog(
            FAKE_HOST, BARK_CFG, runner.run_command, runner.put_file,
            "ensure-cmd", template_text="# template\n")
        self.assertEqual(result["action"], "upgraded")
        cfg, _mode = runner.files[WATCHDOG_CONFIG_PATH]
        self.assertIn("BARK_KEY=old-device-key", cfg)
        self.assertIn("BARK_ENC_KEY=" + "b" * 32, cfg)
        self.assertNotIn("devicekey22charsxx", cfg)

    def test_upgrade_falls_back_to_bot_keys(self):
        # Old script exists but no complete Key can be extracted: use the bot config. 旧脚本在但提不出完整 Key：用机器人配置。
        runner = FakeRunner(probe_output=PROBE_OLD_INSTALLED,
                            keys_output="BARK_KEY = \"only-one\"\n")
        result = install_watchdog(
            FAKE_HOST, BARK_CFG, runner.run_command, runner.put_file,
            "ensure-cmd", template_text="# template\n")
        self.assertEqual(result["action"], "upgraded")
        cfg, _mode = runner.files[WATCHDOG_CONFIG_PATH]
        self.assertIn("BARK_KEY=devicekey22charsxx", cfg)

    def test_already_enabled_short_circuits(self):
        runner = FakeRunner(probe_output=PROBE_NEW_ENABLED)
        result = install_watchdog(
            FAKE_HOST, BARK_CFG, runner.run_command, runner.put_file,
            "ensure-cmd", template_text="# template\n")
        self.assertEqual(result["action"], "already")
        self.assertEqual(len(runner.commands), 1)
        self.assertEqual(runner.files, {})

    def test_reenable_returns_enabled_not_upgraded(self):
        # New version installed and only the cron was removed: re-enable it, with action enabled, not upgraded (a real-machine check once found the re-enable misreported as "已升级"). 新版已装、只是 cron 被摘了：重新开启，action 是 enabled 不是 upgraded（真机验证挖出的文案 bug）。
        runner = FakeRunner(probe_output=PROBE_NEW_NOT_ENABLED)
        result = install_watchdog(
            FAKE_HOST, BARK_CFG, runner.run_command, runner.put_file,
            "ensure-cmd", template_text="# template\n")
        self.assertEqual(result["action"], "enabled")
        self.assertTrue(result["test_ok"])
        # Re-install the cron + run test; the Bark config uses the bot's (no old script to migrate from). 重新挂 cron + 跑 test；Bark 配置用机器人的（无旧脚本可迁移）。
        self.assertTrue(any("crontab -" in c and "oracle_traffic_guard"
                            in c for c in runner.commands))
        self.assertTrue(any(c.endswith(" test") for c in runner.commands))
        cfg, _mode = runner.files[WATCHDOG_CONFIG_PATH]
        self.assertIn("BARK_KEY=devicekey22charsxx", cfg)

    def test_no_bark_configured_fails(self):
        runner = FakeRunner(probe_output=PROBE_NOT_INSTALLED)
        with self.assertRaises(WatchdogError) as ctx:
            install_watchdog(FAKE_HOST, {"url": "", "key": ""},
                             runner.run_command, runner.put_file,
                             "ensure-cmd", template_text="# template\n")
        self.assertIn("菜单 43", str(ctx.exception))
        self.assertEqual(runner.files, {})

    def test_ensure_failure_aborts(self):
        runner = FakeRunner(probe_output=PROBE_NOT_INSTALLED)

        def failing_run(host, command, timeout=None):
            if command == "ensure-cmd":
                return 1, "", "apt failed"
            return runner.run_command(host, command, timeout)

        with self.assertRaises(WatchdogError) as ctx:
            install_watchdog(FAKE_HOST, BARK_CFG, failing_run,
                             runner.put_file, "ensure-cmd",
                             template_text="# template\n")
        self.assertIn("安装依赖失败", str(ctx.exception))
        self.assertEqual(runner.files, {})

    def test_test_failure_does_not_fail_install(self):
        runner = FakeRunner(probe_output=PROBE_NOT_INSTALLED)

        def test_fails(host, command, timeout=None):
            if command.endswith(" test"):
                return 1, "", "bark failed"
            return runner.run_command(host, command, timeout)

        result = install_watchdog(FAKE_HOST, BARK_CFG, test_fails,
                                  runner.put_file, "ensure-cmd",
                                  template_text="# template\n")
        self.assertEqual(result["action"], "installed")
        self.assertFalse(result["test_ok"])


class TestDisableWatchdog(unittest.TestCase):

    def test_disable_enabled(self):
        runner = FakeRunner(probe_output=PROBE_NEW_ENABLED)
        result = disable_watchdog(FAKE_HOST, runner.run_command)
        self.assertEqual(result["action"], "disabled")
        self.assertTrue(any("grep -v oracle_traffic_guard" in c
                            for c in runner.commands))
        self.assertTrue(any(c.endswith(" reset") for c in runner.commands))

    def test_disable_already_off(self):
        runner = FakeRunner(probe_output=PROBE_NOT_INSTALLED)
        result = disable_watchdog(FAKE_HOST, runner.run_command)
        self.assertEqual(result["action"], "already_off")
        self.assertEqual(len(runner.commands), 1)


class TestProbeCommand(unittest.TestCase):

    def test_covers_all_signals(self):
        cmd = probe_command()
        for marker in ("SCRIPT=", "AUTO=", "CRON=", "MODE=", "TRIPPED=",
                       "LIMITED=", "detect_ssh_port", "TRAFFIC_GUARD"):
            self.assertIn(marker, cmd)


if __name__ == "__main__":
    unittest.main()
