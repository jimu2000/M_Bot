#!/usr/bin/env python3
"""web/ssh.py 的测试（mock + WebSocket + 真实回环 SSH 端到端）。

运行：python3 -m unittest discover -s tests -v
或：   python3 -m unittest tests.test_web_ssh -v

测试主机只用 203.0.113.x（TEST-NET-3 文档段）或 127.0.0.1 回环，
绝不出现真实 IP/密码。
"""

import base64
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import web.ssh as ssh_module
from web.ssh import (OUTPUT_BUFFER_LINES, SSHSession, SSHSessionManager,
                     _FallbackSessionStore, build_banner, get_host,
                     load_hosts, parse_console_connection_string, router)

# Test hosts (documentation range / loopback, not real IPs). 测试用主机（文档段 / 回环，不是真实 IP）。
TEST_HOST = "203.0.113.10"

app = FastAPI()
app.include_router(router)


class _StubSessionStore:
    """测试用认证存储：只有 token=good-token 能登录。"""

    def get(self, token):
        return {"username": "tester"} if token == "good-token" else None


def _make_mock_client(sysinfo=b"Linux testhost 6.1.0 x86_64\n"
                              b" 10:00:00 up 1 day, load average: 0.00\n"):
    """造一个 mock 的 paramiko.SSHClient（实例）与其 shell channel。"""
    client = MagicMock()
    mock_stdout = MagicMock()
    mock_stdout.read.return_value = sysinfo
    mock_stdout.channel.recv_exit_status.return_value = 0
    client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())
    channel = MagicMock()
    channel.closed = False
    client.invoke_shell.return_value = channel
    transport = MagicMock()
    transport.is_active.return_value = True
    client.get_transport.return_value = transport
    return client, channel


class TestBuildBanner(unittest.TestCase):
    def test_banner_contains_art_and_sysinfo(self):
        banner = build_banner("Linux testhost 6.1.0\n 10:00 up 1 day\nFilesystem\nMem:")
        self.assertIn("M_Bot", banner)
        self.assertIn("Linux testhost", banner)
        self.assertIn("10:00 up 1 day", banner)

    def test_banner_empty_sysinfo(self):
        banner = build_banner("")
        self.assertIn("M_Bot", banner)
        self.assertIn("系统信息获取失败", banner)


class TestFallbackSessionStore(unittest.TestCase):
    def test_create_get_delete(self):
        store = _FallbackSessionStore()
        token = store.create("alice")
        self.assertEqual(store.get(token)["username"], "alice")
        self.assertIsNone(store.get("不存在的 token"))
        self.assertIsNone(store.get(None))
        store.delete(token)
        self.assertIsNone(store.get(token))


class TestSSHSession(unittest.TestCase):
    def setUp(self):
        # known_hosts goes into a temp directory so the repo is not polluted. known_hosts 写到临时目录，不污染仓库。
        self._tmp = tempfile.TemporaryDirectory()
        self._kh_patch = patch("web.ssh.KNOWN_HOSTS_PATH",
                               Path(self._tmp.name) / "known_hosts")
        self._kh_patch.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._kh_patch.stop)

    def test_connect_password_success(self):
        client, channel = _make_mock_client()
        with patch.object(ssh_module.paramiko, "SSHClient", return_value=client):
            session = SSHSession()
            banner = session.connect(TEST_HOST, 22, "root", password="s3cret")
        self.assertIn("M_Bot", banner)
        self.assertIn("Linux testhost", banner)
        self.assertIs(session.channel, channel)
        self.assertTrue(session.is_active())
        client.connect.assert_called_once()
        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs["hostname"], TEST_HOST)
        self.assertEqual(kwargs["password"], "s3cret")
        self.assertFalse(kwargs["look_for_keys"])

    def test_connect_key_success(self):
        key_file = Path(self._tmp.name) / "id_ed25519"
        key_file.write_text("fake-key")
        client, _ = _make_mock_client()
        with patch.object(ssh_module.paramiko, "SSHClient", return_value=client):
            session = SSHSession()
            session.connect(TEST_HOST, 2222, "root", key_path=str(key_file))
        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs["key_filename"], str(key_file))
        self.assertEqual(kwargs["port"], 2222)

    def test_connect_key_missing_raises(self):
        session = SSHSession()
        with self.assertRaises(FileNotFoundError):
            session.connect(TEST_HOST, 22, "root",
                            key_path="/nonexistent/id_ed25519")

    def test_connect_auth_failure(self):
        client, _ = _make_mock_client()
        client.connect.side_effect = paramiko.AuthenticationException("nope")
        with patch.object(ssh_module.paramiko, "SSHClient", return_value=client):
            session = SSHSession()
            with self.assertRaises(paramiko.AuthenticationException):
                session.connect(TEST_HOST, 22, "root", password="wrong")
        client.close.assert_called()  # On failure the client must be closed, leaving no half-open connection. 失败时 client 要关掉，不留半开连接。
        self.assertFalse(session.is_active())

    def test_connect_banner_failure_does_not_block(self):
        # When exec_command fails (e.g. the server forbids exec), the connection should still succeed and the banner falls back. exec_command 失败（如服务器禁 exec）时连接仍应成功，横幅走兜底。
        client, _ = _make_mock_client()
        client.exec_command.side_effect = paramiko.SSHException("exec 禁用")
        with patch.object(ssh_module.paramiko, "SSHClient", return_value=client):
            session = SSHSession()
            banner = session.connect(TEST_HOST, 22, "root", password="x")
        self.assertIn("系统信息获取失败", banner)
        self.assertTrue(session.is_active())

    def test_send_recv_resize_close(self):
        client, channel = _make_mock_client()
        channel.recv_ready.side_effect = [True, False]
        channel.recv.return_value = b"hello"
        with patch.object(ssh_module.paramiko, "SSHClient", return_value=client):
            session = SSHSession()
            session.connect(TEST_HOST, 22, "root", password="x")
            session.send("ls\n")
            channel.send.assert_called_once_with(b"ls\n")
            self.assertEqual(session.recv(), b"hello")
            self.assertEqual(session.recv(), b"")
            session.resize(120, 40)
            channel.resize_pty.assert_called_once_with(width=120, height=40)
            session.close()
        channel.close.assert_called()
        client.close.assert_called()
        self.assertFalse(session.is_active())

    def test_send_before_connect_raises(self):
        session = SSHSession()
        with self.assertRaises(RuntimeError):
            session.send("ls\n")
        self.assertEqual(session.recv(), b"")
        self.assertFalse(session.is_active())


class TestSSHSessionManager(unittest.TestCase):
    def setUp(self):
        self._session_patch = patch("web.ssh.SSHSession")
        mock_cls = self._session_patch.start()
        self.addCleanup(self._session_patch.stop)
        self.mock_session = mock_cls.return_value
        self.mock_session.connect.return_value = "BANNER\nline2\n"
        self.mock_session.is_active.return_value = True
        self.mgr = SSHSessionManager(start_cleanup_thread=False)

    def test_create_get_list_close(self):
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        entry = self.mgr.get(sid)
        self.assertIs(entry["session"], self.mock_session)
        self.assertEqual(entry["host"], TEST_HOST)
        self.assertEqual("".join(entry["output_buffer"]), "BANNER\nline2\n")
        summaries = self.mgr.list_sessions()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["session_id"], sid)
        self.assertNotIn("session", summaries[0])
        self.assertTrue(self.mgr.close_session(sid))
        self.assertIsNone(self.mgr.get(sid))
        self.assertFalse(self.mgr.close_session(sid))
        self.mock_session.close.assert_called()

    def test_max_sessions(self):
        with patch("web.ssh.MAX_SESSIONS", 1):
            self.mgr.create_session(TEST_HOST, 22, "root", password="x")
            with self.assertRaises(RuntimeError):
                self.mgr.create_session(TEST_HOST, 22, "root", password="x")

    def test_output_buffer_limit(self):
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        for i in range(OUTPUT_BUFFER_LINES + 200):
            self.mgr.append_output(sid, f"line {i}\n")
        entry = self.mgr.get(sid)
        self.assertEqual(len(entry["output_buffer"]), OUTPUT_BUFFER_LINES)
        # The oldest rows are dropped, the newest ones stay. 最旧的行被丢掉，最新的还在。
        self.assertIn(f"line {OUTPUT_BUFFER_LINES + 199}\n",
                      entry["output_buffer"])
        snapshot = self.mgr.snapshot(sid)
        self.assertIn(f"line {OUTPUT_BUFFER_LINES + 199}", snapshot)
        self.assertIsNone(self.mgr.snapshot("不存在的会话"))

    def test_cleanup_idle_timeout(self):
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        entry = self.mgr.get(sid)
        entry["last_active"] = datetime.now(timezone.utc) - timedelta(minutes=31)
        self.assertEqual(self.mgr.cleanup_idle(), 1)
        self.assertIsNone(self.mgr.get(sid))

    def test_cleanup_grace_period(self):
        # WS disconnected for 1 minute with SSH still alive -> keep (for reconnect). WS 断开 1 分钟、SSH 还活着 -> 保留（供重连）。
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        entry = self.mgr.get(sid)
        entry["disconnected_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.assertEqual(self.mgr.cleanup_idle(), 0)
        self.assertIsNotNone(self.mgr.get(sid))
        # WS disconnected for 6 minutes -> close. WS 断开 6 分钟 -> 关闭。
        entry["disconnected_at"] = datetime.now(timezone.utc) - timedelta(minutes=6)
        self.assertEqual(self.mgr.cleanup_idle(), 1)
        self.assertIsNone(self.mgr.get(sid))

    def test_cleanup_dead_ssh_without_ws(self):
        # SSH is already dead and nobody will take over -> no need to wait out the grace period, clean up directly. SSH 已死且无人接管 -> 不用等宽限期，直接清理。
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        entry = self.mgr.get(sid)
        entry["disconnected_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.mock_session.is_active.return_value = False
        self.assertEqual(self.mgr.cleanup_idle(), 1)
        self.assertIsNone(self.mgr.get(sid))

    def test_touch_refreshes_last_active(self):
        sid = self.mgr.create_session(TEST_HOST, 22, "root", password="x")
        entry = self.mgr.get(sid)
        entry["last_active"] = datetime.now(timezone.utc) - timedelta(minutes=29)
        self.mgr.touch(sid)
        self.assertEqual(self.mgr.cleanup_idle(), 0)

    def test_cleanup_thread_starts_by_default(self):
        mgr = SSHSessionManager()
        try:
            self.assertIsNotNone(mgr._cleanup_thread)
            self.assertTrue(mgr._cleanup_thread.is_alive())
        finally:
            mgr.stop()


class TestLoadHosts(unittest.TestCase):
    def test_load_hosts_and_get_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts_file = Path(tmp) / "hosts.json"
            hosts_file.write_text(
                '[{"id": "h1", "name": "测试机", "host": "203.0.113.10",'
                ' "port": 2222, "username": "root"}]', encoding="utf-8")
            with patch("web.ssh.HOSTS_FILE", hosts_file):
                self.assertEqual(len(load_hosts()), 1)
                record = get_host("h1")
                self.assertEqual(record["host"], TEST_HOST)
                self.assertEqual(record["port"], 2222)
                self.assertIsNone(get_host("不存在"))

    def test_load_hosts_missing_file(self):
        with patch("web.ssh.HOSTS_FILE", Path("/nonexistent/hosts.json")):
            self.assertEqual(load_hosts(), [])


class _AuthPatchedTestCase(unittest.TestCase):
    """REST / WS 测试基类：认证走 stub，manager 走 mock。"""

    def setUp(self):
        self._auth_patch = patch("web.ssh.session_store", _StubSessionStore())
        self._auth_patch.start()
        self.addCleanup(self._auth_patch.stop)
        self._mgr_patch = patch("web.ssh.manager")
        self.mock_manager = self._mgr_patch.start()
        self.addCleanup(self._mgr_patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _auth_cookies(self):
        return {"mbot_session": "good-token"}


class TestREST(_AuthPatchedTestCase):
    def test_connect_success(self):
        self.mock_manager.create_session.return_value = "sid-1"
        resp = self.client.post(
            "/api/ssh/connect",
            json={"host": TEST_HOST, "port": 2222, "username": "root",
                  "password": "pw"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"session_id": "sid-1", "host": TEST_HOST,
                                       "username": "root"})
        self.mock_manager.create_session.assert_called_once_with(
            TEST_HOST, 2222, "root", password="pw", key_path=None, host_id=None)

    def test_connect_unauthorized(self):
        resp = self.client.post("/api/ssh/connect",
                                json={"host": TEST_HOST, "username": "root"})
        self.assertEqual(resp.status_code, 401)

    def test_connect_with_host_id(self):
        record = {"id": "h1", "host": TEST_HOST, "port": 2222,
                  "username": "admin", "key_path": "/root/.ssh/id_ed25519"}
        self.mock_manager.create_session.return_value = "sid-2"
        with patch("web.ssh.get_host", return_value=record):
            resp = self.client.post("/api/ssh/connect", json={"host_id": "h1"},
                                    cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.mock_manager.create_session.assert_called_once_with(
            TEST_HOST, 2222, "admin", password=None,
            key_path="/root/.ssh/id_ed25519", host_id="h1")

    def test_connect_host_id_not_found(self):
        with patch("web.ssh.get_host", return_value=None):
            resp = self.client.post("/api/ssh/connect", json={"host_id": "nope"},
                                    cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)

    def test_connect_missing_host(self):
        resp = self.client.post("/api/ssh/connect", json={"username": "root"},
                                cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 400)

    def test_connect_auth_failure(self):
        self.mock_manager.create_session.side_effect = \
            paramiko.AuthenticationException("nope")
        resp = self.client.post(
            "/api/ssh/connect",
            json={"host": TEST_HOST, "username": "root", "password": "bad"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 401)

    def test_connect_ssh_failure(self):
        self.mock_manager.create_session.side_effect = \
            paramiko.SSHException("refused")
        resp = self.client.post(
            "/api/ssh/connect",
            json={"host": TEST_HOST, "username": "root", "password": "x"},
            cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 502)

    def test_list_sessions(self):
        self.mock_manager.list_sessions.return_value = [
            {"session_id": "sid-1", "host": TEST_HOST}]
        resp = self.client.get("/api/ssh/sessions", cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)

    def test_delete_session(self):
        self.mock_manager.close_session.return_value = True
        resp = self.client.delete("/api/ssh/sessions/sid-1",
                                  cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.mock_manager.close_session.return_value = False
        resp = self.client.delete("/api/ssh/sessions/sid-1",
                                  cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)

    def test_snapshot(self):
        self.mock_manager.snapshot.return_value = "banner\n$ "
        resp = self.client.get("/api/ssh/sessions/sid-1/snapshot",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["output"], "banner\n$ ")
        self.mock_manager.snapshot.return_value = None
        resp = self.client.get("/api/ssh/sessions/sid-1/snapshot",
                               cookies=self._auth_cookies())
        self.assertEqual(resp.status_code, 404)


class TestConnectInstance(_AuthPatchedTestCase):
    """POST /api/ssh/connect-instance：实例卡片 SSH 一键直连。"""

    def _mock_oci(self, public_ip=TEST_HOST):
        cli = MagicMock()
        resolve_patch = patch("web.deps.resolve_profile",
                              return_value=(cli, "ocid1.compartment.oc1..test"))
        ip_patch = patch("oci.network.get_public_ip", return_value=public_ip)
        resolve_patch.start()
        ip_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(ip_patch.stop)
        return cli

    def _post(self, **body):
        payload = {"instance_id": "ocid1.instance.oc1..test"}
        payload.update(body)
        return self.client.post("/api/ssh/connect-instance", json=payload,
                                cookies=self._auth_cookies())

    def test_success_first_port(self):
        self._mock_oci()
        self.mock_manager.create_session.return_value = "sid-9"
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["session_id"], "sid-9")
        self.assertEqual(resp.json()["port"], 14422)
        # 密码只在服务端用，绝不返回给前端。
        self.assertNotIn("fixed-pw-test", resp.text)
        self.mock_manager.create_session.assert_called_once_with(
            TEST_HOST, 14422, "root", password="fixed-pw-test")

    def test_fallback_to_port_22(self):
        self._mock_oci()
        self.mock_manager.create_session.side_effect = [
            paramiko.SSHException("refused"), "sid-9"]
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["port"], 22)
        calls = self.mock_manager.create_session.call_args_list
        self.assertEqual([c.args[1] for c in calls], [14422, 22])

    def test_no_fixed_password(self):
        self._mock_oci()
        with patch.dict(os.environ, {"MBOT_FIXED_ROOT_PASSWORD": ""}):
            resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.mock_manager.create_session.assert_not_called()

    def test_unknown_profile(self):
        self._mock_oci()
        with patch("web.deps.resolve_profile",
                   side_effect=ValueError("未知 profile：NOPE")), \
                patch.dict(os.environ,
                           {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 400)

    def test_no_public_ip(self):
        self._mock_oci(public_ip=None)
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 502)
        self.mock_manager.create_session.assert_not_called()

    def test_auth_failure_both_ports(self):
        self._mock_oci()
        self.mock_manager.create_session.side_effect = \
            paramiko.AuthenticationException("nope")
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}):
            resp = self._post()
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            self.mock_manager.create_session.call_count, 2)  # 14422 与 22 都试过

    def test_stored_password_fallback(self):
        # No fixed password, but the instance has a stored password:
        # one-click SSH must connect with it (server-side only, never
        # returned to the browser). 没固定密码、实例有保存的密码时，
        # 一键 SSH 必须用保存的密码直连（只在服务端用，绝不返回给前端）。
        self._mock_oci()
        self.mock_manager.create_session.return_value = "sid-9"
        with patch.dict(os.environ, {"MBOT_FIXED_ROOT_PASSWORD": ""}), \
                patch("tgbot.instance_credentials.get_root_password",
                      return_value="stored-pw-test"):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["session_id"], "sid-9")
        self.assertNotIn("stored-pw-test", resp.text)
        self.mock_manager.create_session.assert_called_once_with(
            TEST_HOST, 14422, "root", password="stored-pw-test")

    def test_fixed_password_wins_over_stored(self):
        # Fixed password set: it wins, the stored one is never consulted.
        # 固定密码设了就优先，保存的密码不会被取用。
        self._mock_oci()
        self.mock_manager.create_session.return_value = "sid-9"
        with patch.dict(os.environ,
                        {"MBOT_FIXED_ROOT_PASSWORD": "fixed-pw-test"}), \
                patch("tgbot.instance_credentials.get_root_password",
                      return_value="stored-pw-test") as mock_get:
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        mock_get.assert_not_called()
        self.mock_manager.create_session.assert_called_once_with(
            TEST_HOST, 14422, "root", password="fixed-pw-test")


# Serial console direct connect (POST /api/ssh/connect-console). 串行控制台直连。
FAKE_CONSOLE_STRING = (
    "ssh -o ProxyCommand='ssh -W %h:%p -p 443 "
    "ocid1.instanceconsoleconnection.oc1..consoletest"
    "@instance-console.ap-singapore-1.oci.oraclecloud.com' "
    "ocid1.instance.oc1..consoletest")
FAKE_CONSOLE_STRING_SIMPLE = (
    "ssh -i /root/.ssh/id_rsa -p 443 "
    "ocid1.instanceconsoleconnection.oc1..consoletest"
    "@instance-console.ap-singapore-1.oci.oraclecloud.com")


class TestParseConsoleConnectionString(unittest.TestCase):
    """parse_console_connection_string：连接命令 -> (host, port, username, instance_id)。"""

    def test_proxycommand_form(self):
        host, port, username, instance_id = \
            parse_console_connection_string(FAKE_CONSOLE_STRING)
        self.assertEqual(
            host, "instance-console.ap-singapore-1.oci.oraclecloud.com")
        self.assertEqual(port, 443)
        self.assertEqual(
            username, "ocid1.instanceconsoleconnection.oc1..consoletest")
        self.assertEqual(instance_id, "ocid1.instance.oc1..consoletest")

    def test_simple_form_without_instance_id_rejected(self):
        # 简单形式里没有实例 OCID，直连无法确定 direct-tcpip 目的地，
        # 明确拒绝（而不是静默连错地方）。
        with self.assertRaises(ValueError):
            parse_console_connection_string(FAKE_CONSOLE_STRING_SIMPLE)

    def test_rejects_non_console_host(self):
        with self.assertRaises(ValueError):
            parse_console_connection_string("ssh root@203.0.113.10")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_console_connection_string("")


class TestConnectConsole(_AuthPatchedTestCase):
    """POST /api/ssh/connect-console：串行控制台直连。"""

    def _post(self, connection_string=FAKE_CONSOLE_STRING, **extra):
        payload = {"connection_string": connection_string}
        payload.update(extra)
        return self.client.post("/api/ssh/connect-console", json=payload,
                                cookies=self._auth_cookies())

    def test_success(self):
        self.mock_manager.create_console_session.return_value = "sid-9"
        with patch("web.ssh._console_private_key_path",
                   return_value=None):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["session_id"], "sid-9")
        self.assertEqual(
            resp.json()["host"],
            "instance-console.ap-singapore-1.oci.oraclecloud.com")
        self.assertEqual(resp.json()["port"], 443)
        self.assertEqual(resp.json()["instance_id"],
                         "ocid1.instance.oc1..consoletest")
        self.mock_manager.create_console_session.assert_called_once_with(
            "instance-console.ap-singapore-1.oci.oraclecloud.com", 443,
            "ocid1.instanceconsoleconnection.oc1..consoletest",
            "ocid1.instance.oc1..consoletest",
            key_path=None)

    def test_bad_connection_string(self):
        resp = self._post(connection_string="ssh root@203.0.113.10")
        self.assertEqual(resp.status_code, 400)
        self.mock_manager.create_console_session.assert_not_called()

    def test_auth_failure(self):
        self.mock_manager.create_console_session.side_effect = \
            paramiko.AuthenticationException("nope")
        with patch("web.ssh._console_private_key_path",
                   return_value=None):
            resp = self._post()
        self.assertEqual(resp.status_code, 401)

    def test_console_key_from_env(self):
        # MBOT_CONSOLE_SSH_KEY_FILE points at a .pub: the private key is
        # the same path without .pub. 环境变量指向 .pub 时，私钥是去掉
        # .pub 的同路径文件。
        with tempfile.TemporaryDirectory() as tmp:
            pub = os.path.join(tmp, "id_rsa.pub")
            priv = os.path.join(tmp, "id_rsa")
            Path(pub).write_text("ssh-rsa AAAA test\n")
            Path(priv).write_text("private\n")
            with patch.dict(os.environ,
                            {"MBOT_CONSOLE_SSH_KEY_FILE": pub}):
                self.assertEqual(ssh_module._console_private_key_path(),
                                 priv)


CONSOLE_HOST = "instance-console.ap-singapore-1.oci.oraclecloud.com"
CONSOLE_CONN_OCID = "ocid1.instanceconsoleconnection.oc1..consoletest"
CONSOLE_INSTANCE_OCID = "ocid1.instance.oc1..consoletest"


class TestConsoleTwoStageFlow(unittest.TestCase):
    """串行控制台两阶段直连（SSHSession.connect_console）：内层 Transport
    开 direct-tcpip 通道，外层 SSHClient 以通道作 socket 再开 shell。
    控制台端点不接受直接的 "session" channel（旧代码的 bug），这里断言
    代理 Transport 上永远只开 direct-tcpip 通道。"""

    def _mocks(self):
        proxy = MagicMock()
        proxy.is_active.return_value = True
        forward = MagicMock()
        forward.closed = False
        proxy.open_channel.return_value = forward
        client = MagicMock()
        shell = MagicMock()
        shell.closed = False
        client.invoke_shell.return_value = shell
        transport = MagicMock()
        transport.is_active.return_value = True
        client.get_transport.return_value = transport
        return proxy, forward, client, shell

    def test_two_stage_flow(self):
        proxy, forward, client, shell = self._mocks()
        fake_pkey = MagicMock()
        with patch.object(ssh_module.paramiko, "Transport",
                          return_value=proxy) as mock_transport, \
                patch.object(ssh_module.paramiko, "SSHClient",
                             return_value=client), \
                patch("web.ssh._load_console_private_key",
                      return_value=fake_pkey):
            session = SSHSession()
            banner = session.connect_console(
                CONSOLE_HOST, 443, CONSOLE_CONN_OCID, CONSOLE_INSTANCE_OCID)
        # Stage 1: inner Transport to the console endpoint, authenticated
        # as the connection OCID. 内层 Transport 连控制台端点，用连接 OCID 认证。
        mock_transport.assert_called_once_with((CONSOLE_HOST, 443))
        proxy.start_client.assert_called_once()
        proxy.auth_publickey.assert_called_once_with(CONSOLE_CONN_OCID,
                                                     fake_pkey)
        # Stage 1 channel: exactly one direct-tcpip channel to the
        # instance OCID:22 — never a "session" channel on the proxy.
        # 内层只开一个 direct-tcpip 转发通道（目的地实例 OCID:22），
        # 绝不在代理 Transport 上开 "session" 通道。
        proxy.open_channel.assert_called_once_with(
            "direct-tcpip", (CONSOLE_INSTANCE_OCID, 22), ("127.0.0.1", 0))
        proxy.invoke_shell.assert_not_called()
        # Stage 2: outer SSHClient uses the forwarded channel as its
        # socket, then opens the shell on the OUTER transport.
        # 外层 SSHClient 以转发通道作 socket，再在外层开 shell。
        client.connect.assert_called_once()
        kwargs = client.connect.call_args.kwargs
        self.assertIs(kwargs["sock"], forward)
        self.assertIs(kwargs["pkey"], fake_pkey)
        self.assertEqual(client.connect.call_args.args[0],
                         CONSOLE_INSTANCE_OCID)
        client.invoke_shell.assert_called_once()
        self.assertIs(session.channel, shell)
        self.assertIs(session.proxy_transport, proxy)
        self.assertTrue(session.is_active())
        self.assertIn("串行控制台", banner)
        # Closing the session closes the proxy transport too.
        # 关会话时一并关掉代理 Transport。
        session.close()
        proxy.close.assert_called_once()
        client.close.assert_called_once()

    def test_no_key_raises_auth(self):
        with patch("web.ssh._load_console_private_key",
                   return_value=None), \
                patch.object(ssh_module.paramiko, "Transport") as transport:
            session = SSHSession()
            with self.assertRaises(paramiko.AuthenticationException):
                session.connect_console(CONSOLE_HOST, 443, CONSOLE_CONN_OCID,
                                        CONSOLE_INSTANCE_OCID)
        transport.assert_not_called()

    def test_proxy_auth_failure_closes_proxy(self):
        proxy, _forward, client, _shell = self._mocks()
        proxy.auth_publickey.side_effect = \
            paramiko.AuthenticationException("bad key")
        with patch.object(ssh_module.paramiko, "Transport",
                          return_value=proxy), \
                patch.object(ssh_module.paramiko, "SSHClient",
                             return_value=client), \
                patch("web.ssh._load_console_private_key",
                      return_value=MagicMock()):
            session = SSHSession()
            with self.assertRaises(paramiko.AuthenticationException):
                session.connect_console(CONSOLE_HOST, 443, CONSOLE_CONN_OCID,
                                        CONSOLE_INSTANCE_OCID)
        proxy.close.assert_called_once()
        client.connect.assert_not_called()
        self.assertFalse(session.is_active())


class TestWebSocket(_AuthPatchedTestCase):
    def _mock_entry(self, recv_side_effect=None):
        mock_session = MagicMock()
        if recv_side_effect is not None:
            mock_session.recv.side_effect = recv_side_effect
        else:
            mock_session.recv.return_value = b""
        mock_session.is_active.return_value = True
        entry = {"session": mock_session, "ws_connected": False,
                 "disconnected_at": None}
        self.mock_manager.get.return_value = entry
        return mock_session, entry

    def test_input_resize_ping(self):
        mock_session, _ = self._mock_entry()
        with self.client.websocket_connect(
                "/ws/ssh/sid-1?token=good-token") as ws:
            ws.send_json({"type": "input", "data": "ls\n"})
            ws.send_json({"type": "resize", "cols": 120, "rows": 40})
            ws.send_json({"type": "ping"})
            # Receiving pong proves the first two messages were processed in order. 收到 pong 说明前两条消息已按序处理完。
            self.assertEqual(ws.receive_json(), {"type": "pong"})
        mock_session.send.assert_called_once_with("ls\n")
        mock_session.resize.assert_called_once_with(120, 40)

    def test_output_forwarded_base64(self):
        state = {"n": 0}

        def fake_recv():
            state["n"] += 1
            return b"hello output" if state["n"] == 1 else b""

        self._mock_entry(recv_side_effect=fake_recv)
        with self.client.websocket_connect(
                "/ws/ssh/sid-1?token=good-token") as ws:
            msg = ws.receive_json()
        self.assertEqual(msg["type"], "output")
        self.assertEqual(base64.b64decode(msg["data"]), b"hello output")

    def test_unauthorized_closes_4401(self):
        self._mock_entry()
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/ws/ssh/sid-1"):
                pass
        self.assertEqual(ctx.exception.code, 4401)

    def test_session_not_found_closes_4404(self):
        self.mock_manager.get.return_value = None
        with self.assertRaises(WebSocketDisconnect) as ctx:
            with self.client.websocket_connect("/ws/ssh/nope?token=good-token"):
                pass
        self.assertEqual(ctx.exception.code, 4404)

    def test_disconnect_keeps_session(self):
        # After the WS disconnects, the session is not destroyed, only its disconnect time is marked (for reconnect within 5 minutes). WS 断开后会话不销毁，只标记断开时间（供 5 分钟内重连）。
        _, entry = self._mock_entry()
        with self.client.websocket_connect("/ws/ssh/sid-1?token=good-token"):
            self.assertTrue(entry["ws_connected"])
        self.assertFalse(entry["ws_connected"])
        self.assertIsNotNone(entry["disconnected_at"])
        self.mock_manager.close_session.assert_not_called()


# ================= 真实回环 SSH 端到端测试 =================

class _LoopbackServer(paramiko.ServerInterface):
    """进程内最小 SSH 服务器：密码认证 + exec + shell 回显。"""

    def __init__(self):
        self.exec_event = threading.Event()
        self.shell_event = threading.Event()

    def check_auth_password(self, username, password):
        if username == "testuser" and password == "testpass":
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.exec_event.set()
        return True

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_event.set()
        return True


class TestLoopbackSSH(unittest.TestCase):
    """真实 SSH 协议回环测试：paramiko 起服务器，SSHSession 真连它。

    不依赖系统 sshd，只用 127.0.0.1 随机高端口。
    """

    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(1024)
        cls.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(5)
        cls.sock.settimeout(0.5)
        cls.port = cls.sock.getsockname()[1]
        cls.stop_event = threading.Event()
        cls.thread = threading.Thread(target=cls._serve, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop_event.set()
        cls.thread.join(timeout=15)
        cls.sock.close()

    @classmethod
    def _serve(cls):
        while not cls.stop_event.is_set():
            try:
                conn, _ = cls.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            transport = paramiko.Transport(conn)
            transport.add_server_key(cls.host_key)
            server = _LoopbackServer()
            try:
                transport.start_server(server=server)
            except Exception:
                transport.close()
                continue
            # First channel: exec (the system-info command for the welcome banner). 第一个 channel：exec（欢迎横幅的系统信息命令）。
            chan = transport.accept(timeout=3)
            if chan is None:
                transport.close()
                continue
            server.exec_event.wait(timeout=5)
            try:
                chan.send(b"Linux testhost 6.1.0 x86_64 GNU/Linux\n")
                chan.send(b" 10:00:00 up 1 day, 1 user, load average: 0.00\n")
                chan.send(b"Filesystem Size Used Avail Use% Mounted on\n"
                          b"/dev/vda1 35G 18G 15G 54% /\n")
                chan.send(b"Mem: 7.8Gi 2.3Gi 1.8Gi 32Mi 4.2Gi 5.5Gi\n")
                chan.send_exit_status(0)
            except Exception:
                pass
            finally:
                try:
                    chan.close()
                except Exception:
                    pass
            if not transport.is_active():
                transport.close()
                continue
            # Second channel: an interactive shell that echoes back everything received. 第二个 channel：交互 shell，回显收到的所有数据。
            chan2 = transport.accept(timeout=3)
            if chan2 is not None:
                server.shell_event.wait(timeout=5)
                chan2.settimeout(5)
                try:
                    while not cls.stop_event.is_set():
                        try:
                            data = chan2.recv(1024)
                        except socket.timeout:
                            break
                        if not data:
                            break
                        chan2.send(data)
                except (EOFError, OSError):
                    pass
                finally:
                    try:
                        chan2.close()
                    except Exception:
                        pass
            transport.close()

    def test_loopback_connect_banner_and_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            known_hosts = Path(tmp) / "known_hosts"
            with patch("web.ssh.KNOWN_HOSTS_PATH", known_hosts):
                session = SSHSession()
                banner = session.connect("127.0.0.1", self.port,
                                         "testuser", password="testpass")
                try:
                    self.assertIn("M_Bot", banner)
                    self.assertIn("Linux testhost", banner)
                    self.assertIn("up 1 day", banner)
                    self.assertTrue(session.is_active())
                    self.assertTrue(known_hosts.exists())
                    session.send("echo hello\r")
                    out = b""
                    deadline = time.time() + 5
                    while time.time() < deadline and b"hello" not in out:
                        out += session.recv()
                        time.sleep(0.05)
                    self.assertIn(b"hello", out)
                finally:
                    session.close()
                self.assertFalse(session.is_active())

    def test_loopback_wrong_password(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("web.ssh.KNOWN_HOSTS_PATH", Path(tmp) / "known_hosts"):
                session = SSHSession()
                with self.assertRaises(paramiko.AuthenticationException):
                    session.connect("127.0.0.1", self.port,
                                    "testuser", password="wrong")


if __name__ == "__main__":
    unittest.main()
