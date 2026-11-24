#!/usr/bin/env python3
"""tgbot/cpu_policy.py 的单元测试（CPU 筛选规则配置）。

规则文件、guard 公钥、合并启动 keys、CPU 检查 hook 全部用临时文件
与 mock，不碰真实部署配置与真实 SSH。
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import cpu_policy
from tgbot.cpu_policy import (
    BootKeys, combined_boot_keys, cpu_matches, get_allowed_cpu_models,
    get_guard_public_key, load_cpu_policy, make_cpu_check_hook,
    save_cpu_policy,
)

FAKE_GUARD_PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEGUARDKEY "
                  "m-bot-traffic-guard")
FAKE_USER_PUB = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEUSERKEY "
                 "user@example")


class _PolicyPathMixin(unittest.TestCase):
    """把规则文件指到临时目录（不存在时返回默认值）。"""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._policy = os.path.join(self._tmpdir.name, "cpu_policy.json")
        patcher = patch.object(cpu_policy, "policy_path",
                               return_value=self._policy)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestPolicyLoadSave(_PolicyPathMixin):

    def test_missing_file_returns_defaults(self):
        self.assertEqual(get_allowed_cpu_models(), ["7J13", "7745"])
        self.assertEqual(load_cpu_policy(),
                         {"allowed_cpu_models": ["7J13", "7745"]})

    def test_corrupt_file_returns_defaults(self):
        with open(self._policy, "w", encoding="utf-8") as f:
            f.write("not json {{{")
        self.assertEqual(get_allowed_cpu_models(), ["7J13", "7745"])

    def test_non_dict_returns_defaults(self):
        with open(self._policy, "w", encoding="utf-8") as f:
            json.dump(["7J13"], f)
        self.assertEqual(get_allowed_cpu_models(), ["7J13", "7745"])

    def test_empty_list_returns_defaults(self):
        with open(self._policy, "w", encoding="utf-8") as f:
            json.dump({"allowed_cpu_models": []}, f)
        self.assertEqual(get_allowed_cpu_models(), ["7J13", "7745"])

    def test_save_and_load_round_trip(self):
        self.assertTrue(save_cpu_policy(["7J13", " 7745 ", "7763"]))
        self.assertEqual(get_allowed_cpu_models(),
                         ["7J13", "7745", "7763"])

    def test_save_empty_returns_false(self):
        self.assertFalse(save_cpu_policy([]))
        self.assertFalse(save_cpu_policy(["  ", ""]))
        self.assertFalse(save_cpu_policy("7J13"))
        self.assertFalse(os.path.exists(self._policy))

    def test_save_writes_0600(self):
        self.assertTrue(save_cpu_policy(["7J13"]))
        mode = stat.S_IMODE(os.stat(self._policy).st_mode)
        self.assertEqual(mode, 0o600)


class TestCpuMatches(_PolicyPathMixin):

    def test_substring_match(self):
        allowed = ["7J13", "7745"]
        self.assertTrue(cpu_matches("AMD EPYC 7J13", allowed))
        self.assertTrue(cpu_matches("AMD EPYC 7745", allowed))
        self.assertFalse(cpu_matches("AMD EPYC 7551", allowed))
        self.assertFalse(cpu_matches("AMD EPYC 7742", allowed))

    def test_empty_model_never_matches(self):
        self.assertFalse(cpu_matches("", ["7J13"]))
        self.assertFalse(cpu_matches(None, ["7J13"]))

    def test_default_allowed_when_none(self):
        self.assertTrue(cpu_matches("AMD EPYC 7745"))
        self.assertFalse(cpu_matches("AMD EPYC 7551"))


class TestGuardPublicKey(unittest.TestCase):

    def test_no_key_configured(self):
        with patch("tgbot.config.GUARD_SSH_KEY", ""):
            self.assertIsNone(get_guard_public_key())

    def test_pub_file_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            priv = os.path.join(tmpdir, "guard")
            with open(priv, "w", encoding="utf-8") as f:
                f.write("fake private key")
            with open(priv + ".pub", "w", encoding="utf-8") as f:
                f.write(FAKE_GUARD_PUB + "\n")
            with patch("tgbot.config.GUARD_SSH_KEY", priv):
                self.assertEqual(get_guard_public_key(), FAKE_GUARD_PUB)

    def test_ssh_keygen_fallback(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = FAKE_GUARD_PUB + "\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            priv = os.path.join(tmpdir, "guard")
            with open(priv, "w", encoding="utf-8") as f:
                f.write("fake private key")
            with patch("tgbot.config.GUARD_SSH_KEY", priv), \
                    patch.object(cpu_policy.subprocess, "run",
                                 return_value=proc) as mock_run:
                self.assertEqual(get_guard_public_key(), FAKE_GUARD_PUB)
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0][:3],
                             ["ssh-keygen", "-y", "-f"])

    def test_ssh_keygen_failure_returns_none(self):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        with tempfile.TemporaryDirectory() as tmpdir:
            priv = os.path.join(tmpdir, "guard")
            with open(priv, "w", encoding="utf-8") as f:
                f.write("fake private key")
            with patch("tgbot.config.GUARD_SSH_KEY", priv), \
                    patch.object(cpu_policy.subprocess, "run",
                                 return_value=proc):
                self.assertIsNone(get_guard_public_key())


class TestCombinedBootKeys(unittest.TestCase):

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.user_keys = os.path.join(self._tmpdir.name, "keys.pub")
        with open(self.user_keys, "w", encoding="utf-8") as f:
            f.write(FAKE_USER_PUB + "\n")
        patcher = patch.object(cpu_policy, "get_guard_public_key",
                               return_value=FAKE_GUARD_PUB)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_guard_uses_original(self):
        with patch.object(cpu_policy, "get_guard_public_key",
                          return_value=None):
            with combined_boot_keys(self.user_keys) as boot_keys:
                self.assertEqual(boot_keys,
                                 BootKeys(path=self.user_keys,
                                          guard_included=False))

    def test_include_guard_false_uses_original(self):
        with combined_boot_keys(self.user_keys,
                                include_guard=False) as boot_keys:
            self.assertEqual(boot_keys,
                             BootKeys(path=self.user_keys,
                                      guard_included=False))

    def test_guard_already_present_uses_original(self):
        with open(self.user_keys, "w", encoding="utf-8") as f:
            f.write(FAKE_USER_PUB + "\n" + FAKE_GUARD_PUB + "\n")
        with combined_boot_keys(self.user_keys) as boot_keys:
            self.assertEqual(boot_keys,
                             BootKeys(path=self.user_keys,
                                      guard_included=True))

    def test_guard_appended_to_temp_file(self):
        with combined_boot_keys(self.user_keys) as boot_keys:
            self.assertTrue(boot_keys.guard_included)
            self.assertNotEqual(boot_keys.path, self.user_keys)
            with open(boot_keys.path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn(FAKE_USER_PUB, content)
            self.assertIn(FAKE_GUARD_PUB, content)
            temp_path = boot_keys.path
        # 退出后临时文件已删，原文件未动
        self.assertFalse(os.path.exists(temp_path))
        with open(self.user_keys, encoding="utf-8") as f:
            self.assertNotIn(FAKE_GUARD_PUB, f.read())

    def test_no_user_file_only_guard(self):
        with combined_boot_keys(None) as boot_keys:
            self.assertTrue(boot_keys.guard_included)
            with open(boot_keys.path, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), FAKE_GUARD_PUB)
            temp_path = boot_keys.path
        self.assertFalse(os.path.exists(temp_path))


class TestMakeCpuCheckHook(unittest.TestCase):

    def setUp(self):
        super().setUp()
        patcher = patch.object(cpu_policy, "get_cpu_model")
        self.mock_get_model = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(cpu_policy, "wait_for_public_ip")
        self.mock_wait_ip = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(cpu_policy.time, "sleep")
        self.mock_sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.run_command = MagicMock()

    def _hook(self, **kwargs):
        kwargs.setdefault("ssh_attempts", 3)
        kwargs.setdefault("ssh_interval", 0)
        return make_cpu_check_hook(MagicMock(), self.run_command, **kwargs)

    def test_ip_given_model_found(self):
        self.mock_get_model.return_value = "AMD EPYC 7J13"
        hook = self._hook()
        self.assertEqual(hook("ocid1.instance.oc1..aaa1", "203.0.113.10"),
                         "AMD EPYC 7J13")
        self.mock_wait_ip.assert_not_called()
        self.mock_get_model.assert_called_once()
        args = self.mock_get_model.call_args[0]
        self.assertEqual(args[0], "ocid1.instance.oc1..aaa1")
        self.assertEqual(args[1]["host"], "203.0.113.10")
        self.assertEqual(args[1]["port"], 22)
        self.assertEqual(args[1]["username"], "root")
        self.assertIs(args[2], self.run_command)
        self.assertTrue(self.mock_get_model.call_args[1]["force"])

    def test_ip_missing_waits_first(self):
        self.mock_wait_ip.return_value = "203.0.113.10"
        self.mock_get_model.return_value = "AMD EPYC 7745"
        hook = self._hook()
        self.assertEqual(hook("ocid1.instance.oc1..aaa1", None),
                         "AMD EPYC 7745")
        self.mock_wait_ip.assert_called_once()

    def test_ip_never_assigned_returns_none(self):
        self.mock_wait_ip.return_value = None
        hook = self._hook()
        self.assertIsNone(hook("ocid1.instance.oc1..aaa1", None))
        self.mock_get_model.assert_not_called()

    def test_retries_then_found(self):
        self.mock_get_model.side_effect = [None, None, "AMD EPYC 7745"]
        hook = self._hook()
        self.assertEqual(hook("ocid1.instance.oc1..aaa1", "203.0.113.10"),
                         "AMD EPYC 7745")
        self.assertEqual(self.mock_get_model.call_count, 3)
        self.assertEqual(self.mock_sleep.call_count, 2)

    def test_all_retries_fail_returns_none(self):
        self.mock_get_model.return_value = None
        hook = self._hook()
        self.assertIsNone(hook("ocid1.instance.oc1..aaa1", "203.0.113.10"))
        self.assertEqual(self.mock_get_model.call_count, 3)


if __name__ == "__main__":
    unittest.main()
