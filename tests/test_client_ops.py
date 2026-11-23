#!/usr/bin/env python3
"""client_ops.py 的单元测试（mock subprocess.run，绝不真跑 systemctl /
git / pip / ufw / ssh）。

运行：python3 -m unittest tests.test_client_ops -v
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import client_ops
from tgbot.client_ops import (
    BOT_SERVICE,
    WEB_SERVICE,
    is_git_repo,
    restart_service,
    service_status,
    ufw_allow_port,
    upgrade_client,
)


def _completed(returncode=0, stdout="", stderr=""):
    """造一个 subprocess.CompletedProcess 替身。"""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestServiceStatus(unittest.TestCase):
    @patch.object(client_ops, "_run")
    def test_active(self, mock_run):
        mock_run.return_value = _completed(0, "active\n")
        self.assertEqual(service_status(WEB_SERVICE), "active")
        mock_run.assert_called_once_with(
            ["systemctl", "is-active", WEB_SERVICE],
            timeout=client_ops.SYSTEMCTL_TIMEOUT)

    @patch.object(client_ops, "_run")
    def test_inactive_and_failed(self, mock_run):
        # is-active 对非 active 服务返回非 0 退出码，这是正常情况。
        mock_run.return_value = _completed(3, "inactive\n")
        self.assertEqual(service_status(BOT_SERVICE), "inactive")
        mock_run.return_value = _completed(3, "failed\n")
        self.assertEqual(service_status(BOT_SERVICE), "failed")

    @patch.object(client_ops, "_run")
    def test_unknown_on_garbage_output(self, mock_run):
        mock_run.return_value = _completed(1, "", "System has not been booted with systemd")
        self.assertEqual(service_status(WEB_SERVICE), "unknown")

    @patch.object(client_ops, "_run")
    def test_unknown_on_os_error_and_timeout(self, mock_run):
        mock_run.side_effect = FileNotFoundError("no systemctl")
        self.assertEqual(service_status(WEB_SERVICE), "unknown")
        mock_run.side_effect = subprocess.TimeoutExpired("systemctl", 60)
        self.assertEqual(service_status(WEB_SERVICE), "unknown")

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValueError):
            service_status("sshd")
        with self.assertRaises(ValueError):
            service_status("rbot-clone-web; rm -rf /")


class TestRestartService(unittest.TestCase):
    @patch.object(client_ops, "_run")
    def test_success(self, mock_run):
        mock_run.return_value = _completed(0, "")
        self.assertIsNone(restart_service(BOT_SERVICE))
        mock_run.assert_called_once_with(
            ["systemctl", "restart", BOT_SERVICE],
            timeout=client_ops.SYSTEMCTL_TIMEOUT)

    @patch.object(client_ops, "_run")
    def test_failure_raises_runtime_error(self, mock_run):
        mock_run.return_value = _completed(1, "", "Failed to restart: not found")
        with self.assertRaises(RuntimeError) as ctx:
            restart_service(WEB_SERVICE)
        self.assertIn("not found", str(ctx.exception))

    @patch.object(client_ops, "_run")
    def test_timeout_raises_runtime_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("systemctl", 60)
        with self.assertRaises(RuntimeError):
            restart_service(WEB_SERVICE)

    @patch.object(client_ops, "_run")
    def test_os_error_raises_runtime_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("no systemctl")
        with self.assertRaises(RuntimeError):
            restart_service(WEB_SERVICE)

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValueError):
            restart_service("nginx")


class TestIsGitRepo(unittest.TestCase):
    @patch.object(client_ops, "_run")
    def test_true(self, mock_run):
        mock_run.return_value = _completed(0, "true\n")
        self.assertTrue(is_git_repo("/opt/rbot-clone"))
        mock_run.assert_called_once_with(
            ["git", "-C", "/opt/rbot-clone", "rev-parse",
             "--is-inside-work-tree"],
            timeout=client_ops.GIT_TIMEOUT)

    @patch.object(client_ops, "_run")
    def test_false_on_nonzero_exit(self, mock_run):
        mock_run.return_value = _completed(128, "", "not a git repository")
        self.assertFalse(is_git_repo("/tmp"))

    @patch.object(client_ops, "_run")
    def test_false_on_os_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("no git")
        self.assertFalse(is_git_repo("/tmp"))


class TestUpgradeClient(unittest.TestCase):
    @patch.object(client_ops, "restart_service")
    @patch.object(client_ops, "_run")
    def test_success_flow_order(self, mock_run, mock_restart):
        """成功流程：git 仓库检查 -> git pull -> pip -> 先重启面板再重启机器人。"""
        mock_run.side_effect = [
            _completed(0, "true\n"),                       # rev-parse
            _completed(0, "Already up to date.\n"),        # git pull
            _completed(0, "Successfully installed x\n"),   # pip
        ]
        summary = upgrade_client("/opt/rbot-clone")
        self.assertEqual(summary, "Already up to date.")
        commands = [call[0][0] for call in mock_run.call_args_list]
        self.assertEqual(commands[0][:2], ["git", "-C"])
        self.assertEqual(commands[1],
                         ["git", "-C", "/opt/rbot-clone", "pull", "--ff-only"])
        self.assertEqual(commands[2][:3],
                         [sys.executable, "-m", "pip"])
        self.assertIn("/opt/rbot-clone/requirements.txt", commands[2])
        # 重启顺序：先面板，后机器人。
        self.assertEqual([c[0][0] for c in mock_restart.call_args_list],
                         [WEB_SERVICE, BOT_SERVICE])

    @patch.object(client_ops, "is_git_repo", return_value=False)
    @patch.object(client_ops, "_run")
    def test_not_a_git_repo(self, mock_run, _mock_is_repo):
        """不是 git 仓库时抛 ValueError，什么都不执行。"""
        with self.assertRaises(ValueError) as ctx:
            upgrade_client("/tmp/not-a-repo")
        self.assertIn("手动升级", str(ctx.exception))
        mock_run.assert_not_called()

    @patch.object(client_ops, "restart_service")
    @patch.object(client_ops, "_run")
    def test_git_pull_failure_aborts(self, mock_run, mock_restart):
        """git pull 失败时抛错，不继续 pip 与重启。"""
        mock_run.side_effect = [
            _completed(0, "true\n"),
            _completed(1, "", "CONFLICT (content): Merge conflict"),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            upgrade_client("/opt/rbot-clone")
        self.assertIn("git pull 失败", str(ctx.exception))
        mock_restart.assert_not_called()

    @patch.object(client_ops, "restart_service")
    @patch.object(client_ops, "_run")
    def test_pip_failure_aborts(self, mock_run, mock_restart):
        """pip 失败时抛错，不重启服务。"""
        mock_run.side_effect = [
            _completed(0, "true\n"),
            _completed(0, "Updating abc..def\n"),
            _completed(1, "", "ERROR: Could not install packages"),
        ]
        with self.assertRaises(RuntimeError) as ctx:
            upgrade_client("/opt/rbot-clone")
        self.assertIn("pip install 失败", str(ctx.exception))
        mock_restart.assert_not_called()

    @patch.object(client_ops, "restart_service")
    @patch.object(client_ops, "_run")
    def test_summary_is_last_pull_line(self, mock_run, mock_restart):
        mock_run.side_effect = [
            _completed(0, "true\n"),
            _completed(0, "From github.com:x/y\nUpdating abc..def\nFast-forward\n"),
            _completed(0, ""),
        ]
        self.assertEqual(upgrade_client("/opt/rbot-clone"), "Fast-forward")


class TestUfwAllowPort(unittest.TestCase):
    @patch.object(client_ops, "_run")
    def test_local(self, mock_run):
        mock_run.return_value = _completed(0, "Rule added\n")
        self.assertIsNone(ufw_allow_port(9527))
        mock_run.assert_called_once_with(
            ["ufw", "allow", "9527/tcp"], timeout=client_ops.UFW_TIMEOUT)

    @patch.object(client_ops, "_run")
    def test_remote_via_ssh(self, mock_run):
        mock_run.return_value = _completed(0, "Rule added\n")
        self.assertIsNone(ufw_allow_port(9527, host="203.0.113.10"))
        mock_run.assert_called_once_with(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-p", "14422", "root@203.0.113.10",
             "ufw", "allow", "9527/tcp"],
            timeout=client_ops.SSH_TIMEOUT)

    @patch.object(client_ops, "_run")
    def test_remote_custom_ssh(self, mock_run):
        mock_run.return_value = _completed(0, "")
        ufw_allow_port(9527, host="203.0.113.10", ssh_port=22,
                       ssh_user="admin")
        args = mock_run.call_args[0][0]
        self.assertIn("admin@203.0.113.10", args)
        self.assertIn("22", args)

    @patch.object(client_ops, "_run")
    def test_failure_raises(self, mock_run):
        mock_run.return_value = _completed(1, "", "ERROR: ufw not enabled")
        with self.assertRaises(RuntimeError) as ctx:
            ufw_allow_port(9527)
        self.assertIn("ufw not enabled", str(ctx.exception))

    def test_invalid_port_rejected(self):
        for bad in (0, 65536, -1, "abc", None):
            with self.assertRaises(ValueError, msg=bad):
                ufw_allow_port(bad)


if __name__ == "__main__":
    unittest.main()
