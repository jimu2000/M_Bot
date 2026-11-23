#!/usr/bin/env python3
"""阶段 5 工作包 E：证书管理的测试（TestClient + mock）。

覆盖：GET /api/cert/status（none/valid/expiring/expired 四种状态，
证书用 cryptography 现场生成自签名证书）、POST /api/cert/renew
（confirm 必填、异步任务、mock subprocess 不真跑 acme.sh）。
"""

import os
import secrets
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.cert import RENEW_TASKS

# 测试密码：运行时随机生成（不在代码里写任何固定口令）。
TEST_PASSWORD = secrets.token_urlsafe(16)
TEST_DOMAIN = "example.com"


def _make_self_signed_cert(not_before, not_after, path):
    """用 cryptography 生成一张自签名证书写到 path（测试专用）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, TEST_DOMAIN),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
    ])
    cert = (x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True)
            .sign(key, hashes.SHA256()))
    from cryptography.hazmat.primitives import serialization
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


class CertTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 ACME_HOME 与 DATA_DIR、登录。"""

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
        RENEW_TASKS.reset()
        # 临时 ACME_HOME：acme.sh 占位可执行文件 + 证书目录。
        self.acme_home = os.path.join(self._tmp.name, "acme.sh")
        os.makedirs(os.path.join(self.acme_home, TEST_DOMAIN))
        self.acme_sh = os.path.join(self.acme_home, "acme.sh")
        with open(self.acme_sh, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.acme_sh, 0o755)
        self._acme_home_patcher = patch("web.routes.cert.ACME_HOME",
                                        self.acme_home)
        self._acme_home_patcher.start()
        self._acme_sh_patcher = patch("web.routes.cert.ACME_SH", self.acme_sh)
        self._acme_sh_patcher.start()
        self._domain_patcher = patch("web.routes.cert.CERT_DOMAIN", "")
        self._domain_patcher.start()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._acme_home_patcher.stop()
        self._acme_sh_patcher.stop()
        self._domain_patcher.stop()
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _write_cert(self, not_before, not_after):
        cert_path = os.path.join(self.acme_home, TEST_DOMAIN,
                                 f"{TEST_DOMAIN}.cer")
        _make_self_signed_cert(not_before, not_after, cert_path)
        return cert_path


class TestCertStatus(CertTestCase):
    """GET /api/cert/status。"""

    def test_no_cert(self):
        r = self.client.get("/api/cert/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["acmeEnabled"])
        self.assertEqual(body["status"], "none")
        self.assertIsNone(body["domain"])
        self.assertIsNone(body["notAfter"])
        self.assertIsNone(body["remainingHours"])

    def test_valid_cert(self):
        now = datetime.now(timezone.utc)
        self._write_cert(now - timedelta(days=1), now + timedelta(days=60))
        r = self.client.get("/api/cert/status")
        body = r.json()
        self.assertEqual(body["status"], "valid")
        self.assertEqual(body["domain"], TEST_DOMAIN)
        self.assertIn(TEST_DOMAIN, body["issuer"])
        self.assertIsNotNone(body["notBefore"])
        self.assertIsNotNone(body["notAfter"])
        self.assertGreater(body["remainingHours"], 24 * 30)

    def test_expiring_cert(self):
        now = datetime.now(timezone.utc)
        self._write_cert(now - timedelta(days=80), now + timedelta(days=3))
        r = self.client.get("/api/cert/status")
        body = r.json()
        self.assertEqual(body["status"], "expiring")
        self.assertLess(body["remainingHours"], 7 * 24)

    def test_expired_cert(self):
        now = datetime.now(timezone.utc)
        self._write_cert(now - timedelta(days=100), now - timedelta(days=1))
        r = self.client.get("/api/cert/status")
        body = r.json()
        self.assertEqual(body["status"], "expired")
        self.assertLess(body["remainingHours"], 0)

    def test_domain_query_param(self):
        now = datetime.now(timezone.utc)
        self._write_cert(now - timedelta(days=1), now + timedelta(days=60))
        r = self.client.get(f"/api/cert/status?domain={TEST_DOMAIN}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["domain"], TEST_DOMAIN)
        # 不存在的域名 -> none。
        r = self.client.get("/api/cert/status?domain=other.example.com")
        self.assertEqual(r.json()["status"], "none")

    def test_bad_domain_400(self):
        r = self.client.get("/api/cert/status?domain=bad_domain!")
        self.assertEqual(r.status_code, 400)

    def test_acme_not_installed(self):
        os.unlink(self.acme_sh)
        r = self.client.get("/api/cert/status")
        self.assertFalse(r.json()["acmeEnabled"])

    def test_anon_401(self):
        r = self.anon.get("/api/cert/status")
        self.assertEqual(r.status_code, 401)


class TestCertRenew(CertTestCase):
    """POST /api/cert/renew 与 GET /api/cert/renew/{taskId}。"""

    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc)
        self._write_cert(now - timedelta(days=1), now + timedelta(days=60))

    def _wait_task(self, task_id, timeout=5.0):
        deadline = time.time() + timeout
        task = None
        while time.time() < deadline:
            r = self.client.get(f"/api/cert/renew/{task_id}")
            task = r.json()["task"]
            if task["status"] in ("done", "failed"):
                return task
            time.sleep(0.05)
        return task

    def test_confirm_required(self):
        r = self.client.post("/api/cert/renew", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("confirm", r.json()["error"])

    @patch("subprocess.run")
    def test_renew_success(self, mock_run):
        mock_run.return_value = SimpleNamespace(
            returncode=0, stdout="Renew success.\n", stderr="")
        r = self.client.post("/api/cert/renew",
                             json={"confirm": True})
        self.assertEqual(r.status_code, 202)
        task_id = r.json()["taskId"]
        self.assertEqual(r.json()["domain"], TEST_DOMAIN)
        task = self._wait_task(task_id)
        self.assertEqual(task["status"], "done")
        self.assertIn("Renew success", task["output"])
        # acme.sh 调用参数：--renew -d <域名>（列表参数，不经过 shell）。
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:3], [self.acme_sh, "--renew", "-d"])
        self.assertEqual(args[3], TEST_DOMAIN)

    @patch("subprocess.run")
    def test_renew_failure(self, mock_run):
        mock_run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr="verify failed")
        r = self.client.post("/api/cert/renew",
                             json={"domain": TEST_DOMAIN, "confirm": True})
        task = self._wait_task(r.json()["taskId"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("退出码 1", task["error"])

    @patch("subprocess.run", side_effect=OSError("no such file"))
    def test_renew_acme_missing_at_runtime(self, mock_run):
        r = self.client.post("/api/cert/renew", json={"confirm": True})
        task = self._wait_task(r.json()["taskId"])
        self.assertEqual(task["status"], "failed")
        self.assertIn("无法执行 acme.sh", task["error"])

    def test_renew_no_cert_404(self):
        os.unlink(os.path.join(self.acme_home, TEST_DOMAIN,
                               f"{TEST_DOMAIN}.cer"))
        r = self.client.post("/api/cert/renew", json={"confirm": True})
        self.assertEqual(r.status_code, 404)

    def test_renew_status_unknown_404(self):
        r = self.client.get("/api/cert/renew/nope")
        self.assertEqual(r.status_code, 404)

    def test_anon_401(self):
        r = self.anon.post("/api/cert/renew", json={"confirm": True})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
