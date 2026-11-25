#!/usr/bin/env python3
"""阶段 5 工作包 E：批量命令执行的测试（TestClient + mock）。

覆盖：黑名单拒绝（复用 tgbot 的 find_dangerous_pattern）、confirm 必填、
逐台执行汇总、单台失败不影响其他台、取消未执行的主机、未知主机 404。
远程执行全部 mock，不碰真实 SSH。
"""

import json
import os
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot.handlers_client import RemoteCommandError
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.batch import BATCHES

# Test password: generated randomly at runtime (no fixed password is ever written in code). 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)

FAKE_HOSTS = [
    {"id": "host-a", "name": "主机 A", "host": "203.0.113.10",
     "port": 22, "username": "root", "auth_type": "key",
     "key_path": "/root/.ssh/id_ed25519"},
    {"id": "host-b", "name": "主机 B", "host": "203.0.113.11",
     "port": 22, "username": "root", "auth_type": "key",
     "key_path": "/root/.ssh/id_ed25519"},
    {"id": "host-c", "name": "主机 C", "host": "203.0.113.12",
     "port": 22, "username": "root", "auth_type": "password"},
]


class BatchTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR（含 hosts.json）、登录。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir_patcher = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        BATCHES.reset()
        with open(os.path.join(self._tmp.name, "hosts.json"), "w") as f:
            json.dump(FAKE_HOSTS, f)
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _wait_batch(self, batch_id, timeout=5.0):
        """轮询批量任务直到 done/cancelled，返回 batch 字典。"""
        deadline = time.time() + timeout
        batch = None
        while time.time() < deadline:
            r = self.client.get(f"/api/batch/{batch_id}")
            batch = r.json()["batch"]
            if batch["status"] in ("done", "cancelled"):
                return batch
            time.sleep(0.05)
        return batch


class TestBatchExec(BatchTestCase):
    """POST /api/batch/exec。"""

    @patch("web.routes.batch.run_remote_command",
           return_value=(0, "uptime ok\n", ""))
    def test_exec_two_hosts(self, mock_run):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a", "host-b"], "command": "uptime",
            "confirm": True})
        self.assertEqual(r.status_code, 202)
        batch_id = r.json()["batchId"]
        batch = self._wait_batch(batch_id)
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["results"]["host-a"]["status"], "done")
        self.assertEqual(batch["results"]["host-a"]["exitCode"], 0)
        self.assertEqual(batch["results"]["host-a"]["stdout"], "uptime ok\n")
        self.assertEqual(batch["results"]["host-b"]["status"], "done")
        self.assertEqual(mock_run.call_count, 2)

    def test_blacklist_rejected(self):
        """危险命令走 tgbot 黑名单，直接 400 拒绝，不建任务。"""
        for cmd in ("rm -rf /", "shutdown -h now", "mkfs.ext4 /dev/sda"):
            r = self.client.post("/api/batch/exec", json={
                "host_ids": ["host-a"], "command": cmd, "confirm": True})
            self.assertEqual(r.status_code, 400, cmd)
            self.assertIn("危险命令", r.json()["error"])

    def test_confirm_required(self):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a"], "command": "uptime"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirm", r.json()["error"])

    def test_empty_hosts_400(self):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": [], "command": "uptime", "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_empty_command_400(self):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a"], "command": "  ", "confirm": True})
        self.assertEqual(r.status_code, 400)

    def test_unknown_host_404(self):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a", "host-nope"], "command": "uptime",
            "confirm": True})
        self.assertEqual(r.status_code, 404)
        self.assertIn("host-nope", r.json()["error"])

    def test_too_many_hosts_400(self):
        r = self.client.post("/api/batch/exec", json={
            "host_ids": [f"host-{i}" for i in range(25)],
            "command": "uptime", "confirm": True})
        self.assertEqual(r.status_code, 400)

    @patch("web.routes.batch.run_remote_command")
    def test_one_host_fails_others_continue(self, mock_run):
        """单台执行失败不影响其他主机。"""
        def _run(host, command, timeout=None):
            if host["id"] == "host-a":
                raise RemoteCommandError("连接失败")
            return (0, "ok", "")
        mock_run.side_effect = _run
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a", "host-b"], "command": "uptime",
            "confirm": True})
        batch = self._wait_batch(r.json()["batchId"])
        self.assertEqual(batch["status"], "done")
        self.assertEqual(batch["results"]["host-a"]["status"], "failed")
        self.assertIn("连接失败", batch["results"]["host-a"]["error"])
        self.assertEqual(batch["results"]["host-b"]["status"], "done")

    @patch("web.routes.batch.run_remote_command",
           return_value=(0, "x" * 30000, ""))
    def test_output_truncated(self, mock_run):
        """超长输出截断并加提示。"""
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a"], "command": "cat big.log",
            "confirm": True})
        batch = self._wait_batch(r.json()["batchId"])
        stdout = batch["results"]["host-a"]["stdout"]
        self.assertLess(len(stdout), 30000)
        self.assertIn("输出过长，已截断", stdout)

    def test_anon_401(self):
        r = self.anon.post("/api/batch/exec", json={
            "host_ids": ["host-a"], "command": "uptime", "confirm": True})
        self.assertEqual(r.status_code, 401)


class TestBatchGetCancel(BatchTestCase):
    """GET /api/batch/{id} 与 POST /api/batch/{id}/cancel。"""

    def test_get_unknown_404(self):
        r = self.client.get("/api/batch/nope")
        self.assertEqual(r.status_code, 404)

    def test_cancel_unknown_404(self):
        r = self.client.post("/api/batch/nope/cancel")
        self.assertEqual(r.status_code, 404)

    @patch("web.routes.batch.run_remote_command")
    def test_cancel_skips_pending_hosts(self, mock_run):
        """取消后未执行的主机标记为 cancelled，不再执行。"""
        started = []
        gate = {"release": False}

        def _run(host, command, timeout=None):
            started.append(host["id"])
            if host["id"] == "host-a":
                # The first host blocks while executing until the test sends a cancel, then is released. 第一台执行时卡住，等测试发取消后再放行。
                deadline = time.time() + 5
                while not gate["release"] and time.time() < deadline:
                    time.sleep(0.02)
            return (0, "ok", "")
        mock_run.side_effect = _run
        r = self.client.post("/api/batch/exec", json={
            "host_ids": ["host-a", "host-b", "host-c"], "command": "uptime",
            "confirm": True})
        batch_id = r.json()["batchId"]
        deadline = time.time() + 5
        while "host-a" not in started and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("host-a", started)
        # Cancel, then release the first host. 取消，再放行第一台。
        r = self.client.post(f"/api/batch/{batch_id}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["batch"]["cancel_requested"])
        gate["release"] = True
        batch = self._wait_batch(batch_id)
        self.assertEqual(batch["status"], "cancelled")
        self.assertEqual(batch["results"]["host-a"]["status"], "done")
        self.assertEqual(batch["results"]["host-b"]["status"], "cancelled")
        self.assertEqual(batch["results"]["host-c"]["status"], "cancelled")
        # host-b/host-c were never executed. host-b/host-c 没被执行过。
        self.assertEqual(mock_run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
