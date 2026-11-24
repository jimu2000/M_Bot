#!/usr/bin/env python3
"""网页添加租户的集成测试（TestClient，不碰真实配置与真实 API）。

运行：python3 -m unittest tests.test_web_oci_profiles -v

注意：
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - OCI 配置路径、旧配置文件、HOME、env 文件全部 patch 到临时目录，
    所有写操作（写配置、写 pem、写 env）都落在临时目录里。
  - OCID / 指纹 / Token 全是假值。
"""

import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot import handlers_client as hc
from web import config as web_config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.profiles import PENDING_PROFILES
from web.tasks import TASKS

TEST_PASSWORD = "oci-" + "prof-" + "test"

FAKE_TENANCY = "ocid1.tenancy.oc1..testtenant1"
FAKE_TENANCY_2 = "ocid1.tenancy.oc1..testtenant2"
FAKE_USER = "ocid1.user.oc1..testuser0001"
FAKE_FINGERPRINT = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"

FAKE_PEM = (b"-----BEGIN PRIVATE KEY-----\n"
            b"ZmFrZS1rZXktY29udGVudA==\n"
            b"-----END PRIVATE KEY-----\n")

CONFIG_ONE = """[NEWPROF]
user=%s
fingerprint=%s
key_file=/srv/elsewhere/key.pem
tenancy=%s
region=ap-singapore-1
""" % (FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY)

CONFIG_TWO = """[NEWPROF]
user=%s
fingerprint=%s
key_file=/srv/elsewhere/key.pem
tenancy=%s
region=ap-singapore-1
[SECOND]
user=%s
fingerprint=%s
key_file=/root/2.pem
tenancy=%s
region=us-chicago-1
""" % (FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY,
       FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY_2)

CONFIG_RBOT = """oci=begin
[NEWPROF]
user=%s
fingerprint=%s
key_file=/srv/elsewhere/key.pem
tenancy=%s
region=ap-singapore-1
oci=end
""" % (FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY)

CONFIG_WITH_CF = CONFIG_ONE + """[cloudflare]
cf_token=fake-cf-token-12345
cf_email=tester@example.com
"""

CONFIG_MISSING_KEY = """[NEWPROF]
user=%s
fingerprint=%s
tenancy=%s
region=ap-singapore-1
""" % (FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY)


class WebOciProfilesTestCase(unittest.TestCase):
    """网页添加租户测试：临时 HOME / 配置 / env，不碰真实文件。"""

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
        self._home = os.path.join(self._tmp.name, "home")
        os.makedirs(self._home)
        self._config_path = os.path.join(self._tmp.name, "oci_config")
        self._legacy_path = os.path.join(self._tmp.name, "legacy_config")
        self._env_path = os.path.join(self._tmp.name, "test.env")
        self._patchers = [
            patch.dict(os.environ, {"HOME": self._home}),
            patch.object(hc, "OCI_CONFIG_PATH", self._config_path),
            patch.object(hc, "OCI_CONFIG_LEGACY_PATH", self._legacy_path),
            patch.object(web_config, "ENV_FILE", self._env_path),
            patch("web.config.DATA_DIR", self._tmp.name),
        ]
        for patcher in self._patchers:
            patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        PENDING_PROFILES.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self._login()

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()
        self._tmp.cleanup()

    def _login(self):
        return self.client.post("/api/auth/login",
                                json={"username": "admin",
                                      "password": TEST_PASSWORD})

    def _parse(self, text):
        return self.client.post("/api/profiles/parse",
                                data={"config_text": text})

    def _read_config(self):
        with open(self._config_path, encoding="utf-8") as handle:
            return handle.read()

    # ---------- 解析 ----------

    def test_parse_single_profile(self):
        resp = self._parse(CONFIG_ONE)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["pending_id"])
        self.assertEqual(len(data["profiles"]), 1)
        item = data["profiles"][0]
        self.assertEqual(item["profile"], "NEWPROF")
        self.assertEqual(item["region"], "ap-singapore-1")
        self.assertEqual(item["regionLabel"], "新加坡")
        # key_file 不在 home 下，归一到 ~/NEWPROF.pem。
        self.assertEqual(item["key_file"],
                         os.path.join(self._home, "NEWPROF.pem"))
        self.assertFalse(item["key_ready"])
        self.assertEqual(data["skipped"], [])
        self.assertIsNone(data["cloudflare"])
        # 响应里不回显 OCID 与指纹。
        self.assertNotIn(FAKE_TENANCY, resp.text)
        self.assertNotIn(FAKE_FINGERPRINT, resp.text)

    def test_parse_filters_existing_profile(self):
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_ONE)
        resp = self._parse(CONFIG_TWO)
        data = resp.json()
        self.assertEqual(data["skipped"], ["NEWPROF"])
        self.assertEqual([p["profile"] for p in data["profiles"]],
                         ["SECOND"])

    def test_parse_all_existing_returns_error(self):
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_ONE)
        resp = self._parse(CONFIG_ONE)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("全部已存在", resp.json()["error"])

    def test_parse_rbot_block(self):
        resp = self._parse(CONFIG_RBOT)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual([p["profile"] for p in data["profiles"]],
                         ["NEWPROF"])

    def test_parse_missing_key_returns_error(self):
        resp = self._parse(CONFIG_MISSING_KEY)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("key_file", resp.json()["error"])

    def test_parse_empty_returns_error(self):
        resp = self._parse("   ")
        self.assertEqual(resp.status_code, 400)

    def test_parse_config_file_upload(self):
        resp = self.client.post(
            "/api/profiles/parse",
            files={"config_file": ("oci_config.txt",
                                   CONFIG_ONE.encode("utf-8"),
                                   "text/plain")})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual([p["profile"] for p in data["profiles"]],
                         ["NEWPROF"])

    def test_parse_cloudflare_section_no_value_echo(self):
        resp = self._parse(CONFIG_WITH_CF)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["cloudflare"]["present"])
        self.assertTrue(data["cloudflare"]["has_token"])
        self.assertNotIn("fake-cf-token-12345", resp.text)
        self.assertNotIn("tester@example.com", resp.text)

    def test_parse_key_already_on_server(self):
        pem_path = os.path.join(self._home, "NEWPROF.pem")
        with open(pem_path, "wb") as handle:
            handle.write(FAKE_PEM)
        resp = self._parse(CONFIG_ONE)
        self.assertTrue(resp.json()["profiles"][0]["key_ready"])

    def test_parse_requires_login(self):
        resp = self.anon.post("/api/profiles/parse",
                              data={"config_text": CONFIG_ONE})
        self.assertEqual(resp.status_code, 401)

    # ---------- 提交 ----------

    def test_commit_with_pem_upload(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        resp = self.client.post(
            "/api/profiles/commit",
            data={"pending_id": pending_id},
            files={"pem_NEWPROF": ("oci_api_key.pem", FAKE_PEM,
                                    "application/x-pem-file")})
        data = resp.json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], ["NEWPROF"])
        self.assertTrue(data["keys"][0]["key_ready"])
        self.assertEqual(data["pem_failed"], [])
        self.assertTrue(data["restart_needed"])
        # 配置已写进临时文件。
        content = self._read_config()
        self.assertIn("[NEWPROF]", content)
        self.assertIn(FAKE_TENANCY, content)
        # pem 已写到归一化路径，权限 600。
        pem_path = os.path.join(self._home, "NEWPROF.pem")
        with open(pem_path, "rb") as handle:
            self.assertEqual(handle.read(), FAKE_PEM)
        self.assertEqual(stat.S_IMODE(os.stat(pem_path).st_mode), 0o600)
        # compartment 环境变量已写进临时 env。
        with open(self._env_path, encoding="utf-8") as handle:
            self.assertIn("MBOT_COMPARTMENT_NEWPROF=%s" % FAKE_TENANCY,
                          handle.read())

    def test_commit_without_pem_key_not_ready(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        resp = self.client.post("/api/profiles/commit",
                                data={"pending_id": pending_id})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], ["NEWPROF"])
        self.assertFalse(data["keys"][0]["key_ready"])
        self.assertIn("[NEWPROF]", self._read_config())

    def test_commit_invalid_pem_writes_nothing(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        resp = self.client.post(
            "/api/profiles/commit",
            data={"pending_id": pending_id},
            files={"pem_NEWPROF": ("key.pem", b"not a private key",
                                    "text/plain")})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不是私钥文件", resp.json()["error"])
        self.assertFalse(os.path.exists(self._config_path))

    def test_commit_key_file_path_cannot_escape_home(self):
        evil = CONFIG_ONE.replace("/srv/elsewhere/key.pem", "/etc/passwd")
        pending_id = self._parse(evil).json()["pending_id"]
        resp = self.client.post(
            "/api/profiles/commit",
            data={"pending_id": pending_id},
            files={"pem_NEWPROF": ("key.pem", FAKE_PEM,
                                    "application/x-pem-file")})
        self.assertTrue(resp.json()["ok"])
        # pem 只落在临时 HOME 里，不会写到 /etc。
        self.assertTrue(os.path.exists(
            os.path.join(self._home, "NEWPROF.pem")))
        self.assertIn("key_file = %s" % os.path.join(self._home,
                                                    "NEWPROF.pem"),
                      self._read_config())

    def test_commit_cloudflare_section_saved(self):
        pending_id = self._parse(CONFIG_WITH_CF).json()["pending_id"]
        resp = self.client.post("/api/profiles/commit",
                                data={"pending_id": pending_id})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["cloudflare"], "saved")
        self.assertIn("[cloudflare]", self._read_config())

    def test_commit_pending_consumed_once(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        first = self.client.post("/api/profiles/commit",
                                 data={"pending_id": pending_id})
        self.assertTrue(first.json()["ok"])
        second = self.client.post("/api/profiles/commit",
                                  data={"pending_id": pending_id})
        self.assertEqual(second.status_code, 400)

    def test_commit_expired_pending_rejected(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        PENDING_PROFILES._pending[pending_id]["expires"] = 0
        resp = self.client.post("/api/profiles/commit",
                                data={"pending_id": pending_id})
        self.assertEqual(resp.status_code, 400)

    def test_commit_unknown_pending_rejected(self):
        resp = self.client.post("/api/profiles/commit",
                                data={"pending_id": "no-such-pending"})
        self.assertEqual(resp.status_code, 400)

    def test_commit_requires_login(self):
        pending_id = self._parse(CONFIG_ONE).json()["pending_id"]
        resp = self.anon.post("/api/profiles/commit",
                              data={"pending_id": pending_id})
        self.assertEqual(resp.status_code, 401)

    # ---------- 页面 ----------

    def test_accounts_page_renders(self):
        resp = self.client.get("/accounts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("云账户", resp.text)
        self.assertIn("添加租户", resp.text)
        self.assertIn("accounts.js", resp.text)

    def test_accounts_page_requires_login(self):
        resp = self.anon.get("/accounts", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["location"])


if __name__ == "__main__":
    unittest.main()
