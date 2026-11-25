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

# Test password: generated randomly at runtime (no fixed password is ever written in code). 测试密码：运行时随机生成（不在代码里写任何固定口令）。
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
        # Temp ACME_HOME: a placeholder acme.sh executable + a cert directory. 临时 ACME_HOME：acme.sh 占位可执行文件 + 证书目录。
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
        # The multi-source fallback directories also point at temp empty dirs (isolated from the real machine environment). 多来源回退的目录也指到临时空目录（与本机真实环境隔离）。
        self.certbot_live = os.path.join(self._tmp.name, "live")
        self.lego_dir = os.path.join(self._tmp.name, "lego")
        self.nginx_dir = os.path.join(self._tmp.name, "nginx")
        os.makedirs(self.certbot_live)
        os.makedirs(self.lego_dir)
        os.makedirs(self.nginx_dir)
        self._certbot_patcher = patch("web.routes.cert.CERTBOT_LIVE_DIR",
                                      self.certbot_live)
        self._certbot_patcher.start()
        self._lego_patcher = patch("web.routes.cert.LEGO_CERT_DIRS",
                                   [self.lego_dir])
        self._lego_patcher.start()
        self._nginx_patcher = patch(
            "web.routes.cert.NGINX_CONF_GLOBS",
            [os.path.join(self.nginx_dir, "*.conf")])
        self._nginx_patcher.start()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})

    def tearDown(self):
        self._acme_home_patcher.stop()
        self._acme_sh_patcher.stop()
        self._domain_patcher.stop()
        self._certbot_patcher.stop()
        self._lego_patcher.stop()
        self._nginx_patcher.stop()
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
        r = self.client.get("/api/cert/status?domain=other.example.com")
        self.assertEqual(r.json()["status"], "none")

    def test_bad_domain_400(self):
        r = self.client.get("/api/cert/status?domain=bad_domain!")
        self.assertEqual(r.status_code, 400)

    def test_ecc_dir_skips_ca_cert(self):
        # An acme.sh ECC cert directory is <domain>_ecc; inside, ca.cer is the intermediate cert (longer-lived) and <domain>.cer is the leaf; the status page must show the leaf, and the domain has no _ecc suffix; ?domain= also finds certs in _ecc directories. acme.sh 的 ECC 证书目录是 <domain>_ecc，里面 ca.cer 是中间证书（有效期更长），叶子证书是 <domain>.cer；状态页要显示叶子，域名不带 _ecc 后缀；?domain= 也能找到 _ecc 目录里的证书。
        ecc_dir = os.path.join(self.acme_home, f"{TEST_DOMAIN}_ecc")
        os.makedirs(ecc_dir, exist_ok=True)
        now = datetime.now(timezone.utc)
        _make_self_signed_cert(now - timedelta(days=1),
                                now + timedelta(days=1095),
                                os.path.join(ecc_dir, "ca.cer"))
        _make_self_signed_cert(now - timedelta(days=1),
                                now + timedelta(days=30),
                                os.path.join(ecc_dir, f"{TEST_DOMAIN}.cer"))
        r = self.client.get("/api/cert/status")
        body = r.json()
        self.assertEqual(body["status"], "valid")
        self.assertEqual(body["source"], "acme.sh")
        self.assertEqual(body["domain"], TEST_DOMAIN)
        # About 30 days left (the leaf), not 1095 days (the intermediate cert). 剩余约 30 天（叶子），不是 1095 天（中间证书）。
        self.assertLess(body["remainingHours"], 24 * 60)
        r = self.client.get("/api/cert/status",
                            params={"domain": TEST_DOMAIN})
        self.assertEqual(r.json()["domain"], TEST_DOMAIN)

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
        # acme.sh call arguments: --renew -d <domain> (list arguments, not through a shell). acme.sh 调用参数：--renew -d <域名>（列表参数，不经过 shell）。
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


class TestCertStatusSources(CertTestCase):
    """GET /api/cert/status 的多来源回退（v1.37.0）。

    acme.sh 无证书时按 certbot → lego → nginx 的顺序找；
    找到的来源在响应里标明；续期仍只走 acme.sh。
    """

    def _valid_window(self):
        now = datetime.now(timezone.utc)
        return now - timedelta(days=1), now + timedelta(days=30)

    def _write_certbot_cert(self, domain):
        cert_dir = os.path.join(self.certbot_live, domain)
        os.makedirs(cert_dir, exist_ok=True)
        path = os.path.join(cert_dir, "fullchain.pem")
        _make_self_signed_cert(*self._valid_window(), path)
        return path

    def _write_lego_cert(self, domain):
        path = os.path.join(self.lego_dir, f"{domain}.crt")
        _make_self_signed_cert(*self._valid_window(), path)
        return path

    def _write_nginx_conf(self, cert_path):
        conf_path = os.path.join(self.nginx_dir, "panel.conf")
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("server {\n"
                    f"    ssl_certificate {cert_path};\n"
                    "    ssl_certificate_key /tmp/nonexistent.key;\n"
                    "}\n")
        return conf_path

    def test_certbot_fallback(self):
        self._write_certbot_cert(TEST_DOMAIN)
        r = self.client.get("/api/cert/status")
        data = r.json()
        self.assertEqual(data["status"], "valid")
        self.assertEqual(data["source"], "certbot")
        self.assertEqual(data["domain"], TEST_DOMAIN)
        self.assertIsNotNone(data["notAfter"])

    def test_certbot_domain_param(self):
        self._write_certbot_cert(TEST_DOMAIN)
        self._write_certbot_cert("other.com")
        r = self.client.get("/api/cert/status",
                            params={"domain": "other.com"})
        data = r.json()
        self.assertEqual(data["domain"], "other.com")
        self.assertEqual(data["source"], "certbot")
        r = self.client.get("/api/cert/status",
                            params={"domain": "missing.com"})
        self.assertEqual(r.json()["status"], "none")

    def test_lego_fallback(self):
        self._write_lego_cert(TEST_DOMAIN)
        r = self.client.get("/api/cert/status")
        data = r.json()
        self.assertEqual(data["status"], "valid")
        self.assertEqual(data["source"], "lego")
        self.assertEqual(data["domain"], TEST_DOMAIN)

    def test_nginx_fallback_domain_from_cn(self):
        # The cert sits at an irregular path, so the domain can only be read from the cert CN. 证书在无规律路径下，域名只能从证书 CN 读。
        cert_path = os.path.join(self._tmp.name, "random-name.pem")
        _make_self_signed_cert(*self._valid_window(), cert_path)
        self._write_nginx_conf(cert_path)
        r = self.client.get("/api/cert/status")
        data = r.json()
        self.assertEqual(data["status"], "valid")
        self.assertEqual(data["source"], "nginx")
        self.assertEqual(data["domain"], TEST_DOMAIN)

    def test_acme_takes_precedence(self):
        self._write_cert(*self._valid_window())
        self._write_certbot_cert(TEST_DOMAIN)
        r = self.client.get("/api/cert/status")
        data = r.json()
        self.assertEqual(data["source"], "acme.sh")
        self.assertEqual(data["status"], "valid")

    def test_certbot_before_lego(self):
        self._write_certbot_cert(TEST_DOMAIN)
        self._write_lego_cert(TEST_DOMAIN)
        r = self.client.get("/api/cert/status")
        self.assertEqual(r.json()["source"], "certbot")

    def test_no_cert_anywhere(self):
        r = self.client.get("/api/cert/status")
        data = r.json()
        self.assertEqual(data["status"], "none")
        self.assertIsNone(data["source"])
        self.assertIsNone(data["domain"])

    def test_renew_still_acme_only(self):
        self._write_certbot_cert(TEST_DOMAIN)
        r = self.client.post("/api/cert/renew", json={"confirm": True})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
