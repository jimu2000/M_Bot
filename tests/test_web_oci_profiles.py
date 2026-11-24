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

    # ---------- 已配置账号的私钥补传（v1.37.1） ----------

    def _setup_configured_profile(self, key_file):
        """往临时配置里写一个已配置账号，并把 web PROFILES 指过去。"""
        text = ("[TESTPROF]\nuser=%s\nfingerprint=%s\nkey_file=%s\n"
                "tenancy=%s\nregion=ap-singapore-1\n"
                % (FAKE_USER, FAKE_FINGERPRINT, key_file, FAKE_TENANCY))
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        patcher = patch.object(web_config, "PROFILES", {
            "TESTPROF": {"region": "ap-singapore-1",
                         "compartment_id": FAKE_TENANCY}})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("get_region_counts", "get_profile_regions"):
            region_patcher = patch("web.routes.profiles.%s" % name,
                                   return_value={})
            region_patcher.start()
            self.addCleanup(region_patcher.stop)

    def _upload_key(self, profile, content, filename="key.pem"):
        return self.client.post("/api/profiles/%s/key" % profile,
                                files={"key_file": (filename, content)})

    def test_list_profiles_shows_key_missing(self):
        key_path = os.path.join(self._home, "missing.pem")
        self._setup_configured_profile(key_path)
        data = self.client.get("/api/profiles").json()
        self.assertTrue(data["ok"])
        item = [p for p in data["profiles"]
                if p["name"] == "TESTPROF"][0]
        self.assertEqual(item["key_file"], key_path)
        self.assertFalse(item["key_ready"])

    def test_list_profiles_shows_key_ready(self):
        key_path = os.path.join(self._home, "ready.pem")
        with open(key_path, "wb") as handle:
            handle.write(FAKE_PEM)
        self._setup_configured_profile(key_path)
        data = self.client.get("/api/profiles").json()
        item = [p for p in data["profiles"]
                if p["name"] == "TESTPROF"][0]
        self.assertTrue(item["key_ready"])

    def test_upload_key_success(self):
        key_path = os.path.join(self._home, ".oci", "oci_api_key.pem")
        self._setup_configured_profile(key_path)
        resp = self._upload_key("TESTPROF", FAKE_PEM, "oci_api_key.pem")
        data = resp.json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["key_file"], key_path)
        self.assertTrue(data["key_ready"])
        with open(key_path, "rb") as handle:
            self.assertEqual(handle.read(), FAKE_PEM)
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)
        # 列表随之变已就位。
        listed = self.client.get("/api/profiles").json()
        item = [p for p in listed["profiles"]
                if p["name"] == "TESTPROF"][0]
        self.assertTrue(item["key_ready"])

    def test_upload_key_rejects_non_key(self):
        key_path = os.path.join(self._home, "key.pem")
        self._setup_configured_profile(key_path)
        resp = self._upload_key("TESTPROF", b"not a private key")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不是私钥文件", resp.json()["error"])
        self.assertFalse(os.path.exists(key_path))

    def test_upload_key_unknown_profile(self):
        self._setup_configured_profile(
            os.path.join(self._home, "key.pem"))
        resp = self._upload_key("NOPE", FAKE_PEM)
        self.assertEqual(resp.status_code, 404)

    def test_upload_key_path_outside_home_rejected(self):
        self._setup_configured_profile("/etc/outside.pem")
        resp = self._upload_key("TESTPROF", FAKE_PEM)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不在 home 下", resp.json()["error"])
        self.assertFalse(os.path.exists("/etc/outside.pem"))

    def test_upload_key_oversize_rejected(self):
        key_path = os.path.join(self._home, "key.pem")
        self._setup_configured_profile(key_path)
        resp = self._upload_key("TESTPROF", b"A" * (16384 + 1))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("超过 16KB", resp.json()["error"])
        self.assertFalse(os.path.exists(key_path))

    def test_upload_key_requires_login(self):
        self._setup_configured_profile(
            os.path.join(self._home, "key.pem"))
        resp = self.anon.post("/api/profiles/TESTPROF/key",
                              files={"key_file": ("key.pem", FAKE_PEM)})
        self.assertEqual(resp.status_code, 401)

    # ---------- 删除已配置账号（v1.38.0） ----------

    def _setup_profiles_for_delete(self, specs):
        """写临时配置 + env 文件 + patch PROFILES 与 os.environ。

        specs: [(profile 名, key_file 路径, region)]。
        """
        sections = []
        profiles = {}
        env_vars = {}
        for name, key_file, region in specs:
            sections.append(
                "[%s]\nuser=%s\nfingerprint=%s\nkey_file=%s\n"
                "tenancy=%s\nregion=%s\n"
                % (name, FAKE_USER, FAKE_FINGERPRINT, key_file,
                   FAKE_TENANCY, region))
            profiles[name] = {"region": region,
                              "compartment_id": FAKE_TENANCY}
            env_vars["MBOT_COMPARTMENT_%s" % name] = FAKE_TENANCY
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sections))
        with open(self._env_path, "w", encoding="utf-8") as handle:
            for key, value in sorted(env_vars.items()):
                handle.write("%s=%s\n" % (key, value))
        patcher = patch.object(web_config, "PROFILES", profiles)
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patcher = patch.dict(os.environ, env_vars)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        for name in ("get_region_counts", "get_profile_regions"):
            region_patcher = patch("web.routes.profiles.%s" % name,
                                   return_value={})
            region_patcher.start()
            self.addCleanup(region_patcher.stop)

    def test_delete_profile_success(self):
        key_a = os.path.join(self._home, "a.pem")
        key_b = os.path.join(self._home, "b.pem")
        for key_path in (key_a, key_b):
            with open(key_path, "wb") as handle:
                handle.write(FAKE_PEM)
        self._setup_profiles_for_delete([
            ("DEFAULT", key_a, "ap-singapore-1"),
            ("SECOND", key_b, "us-chicago-1")])
        resp = self.client.delete("/api/profiles/SECOND")
        data = resp.json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["config_removed"])
        self.assertTrue(data["env_removed"])
        self.assertTrue(data["key_removed"])
        self.assertTrue(data["bot_restart_needed"])
        content = self._read_config()
        self.assertNotIn("[SECOND]", content)
        self.assertIn("[DEFAULT]", content)
        with open(self._env_path, encoding="utf-8") as handle:
            env_text = handle.read()
        self.assertNotIn("MBOT_COMPARTMENT_SECOND=", env_text)
        self.assertIn("MBOT_COMPARTMENT_DEFAULT=", env_text)
        self.assertFalse(os.path.exists(key_b))
        self.assertTrue(os.path.exists(key_a))
        listed = self.client.get("/api/profiles").json()
        names = [p["name"] for p in listed["profiles"]]
        self.assertNotIn("SECOND", names)
        self.assertIn("DEFAULT", names)

    def test_delete_profile_shared_key_kept(self):
        shared = os.path.join(self._home, "shared.pem")
        with open(shared, "wb") as handle:
            handle.write(FAKE_PEM)
        self._setup_profiles_for_delete([
            ("DEFAULT", shared, "ap-singapore-1"),
            ("SECOND", shared, "us-chicago-1")])
        resp = self.client.delete("/api/profiles/SECOND")
        data = resp.json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertFalse(data["key_removed"])
        self.assertTrue(os.path.exists(shared))

    def test_delete_default_blocked(self):
        self._setup_profiles_for_delete([
            ("DEFAULT", os.path.join(self._home, "a.pem"),
             "ap-singapore-1"),
            ("SECOND", os.path.join(self._home, "b.pem"),
             "us-chicago-1")])
        resp = self.client.delete("/api/profiles/DEFAULT")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("默认账号", resp.json()["error"])
        self.assertIn("[DEFAULT]", self._read_config())

    def test_delete_last_profile_blocked(self):
        self._setup_profiles_for_delete([
            ("ONLYONE", os.path.join(self._home, "a.pem"),
             "ap-singapore-1")])
        resp = self.client.delete("/api/profiles/ONLYONE")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("至少保留一个账号", resp.json()["error"])
        self.assertIn("[ONLYONE]", self._read_config())

    def test_delete_unknown_profile(self):
        self._setup_profiles_for_delete([
            ("DEFAULT", os.path.join(self._home, "a.pem"),
             "ap-singapore-1"),
            ("SECOND", os.path.join(self._home, "b.pem"),
             "us-chicago-1")])
        resp = self.client.delete("/api/profiles/NOPE")
        self.assertEqual(resp.status_code, 404)

    def test_delete_requires_login(self):
        self._setup_profiles_for_delete([
            ("DEFAULT", os.path.join(self._home, "a.pem"),
             "ap-singapore-1"),
            ("SECOND", os.path.join(self._home, "b.pem"),
             "us-chicago-1")])
        resp = self.anon.delete("/api/profiles/SECOND")
        self.assertEqual(resp.status_code, 401)

    def test_remove_oci_profile_keeps_other_sections(self):
        from tgbot.handlers_client import remove_oci_profile
        text = ("[KEEP]\nuser=%s\nregion=ap-singapore-1\n\n"
                "[cloudflare]\ncf_email=a@example.com\n\n"
                "[DROP]\nuser=%s\nregion=us-chicago-1\n"
                % (FAKE_USER, FAKE_USER))
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.assertTrue(remove_oci_profile("DROP"))
        content = self._read_config()
        self.assertNotIn("[DROP]", content)
        self.assertIn("[KEEP]", content)
        self.assertIn("[cloudflare]", content)
        self.assertIn("cf_email=a@example.com", content)
        self.assertFalse(remove_oci_profile("DROP"))

    def test_remove_oci_profile_round_trip_byte_identical(self):
        # v1.38.1 回归：添加再删除一个段后，文件必须与原来逐字节一致。
        from tgbot.handlers_client import (
            append_oci_profile, remove_oci_profile)
        original = ("[KEEP]\nuser=%s\nfingerprint=%s\n"
                    "key_file=/root/keep.pem\ntenancy=%s\n"
                    "region=ap-singapore-1\n"
                    % (FAKE_USER, FAKE_FINGERPRINT, FAKE_TENANCY))
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write(original)
        append_oci_profile("TEMP", {
            "user": FAKE_USER, "fingerprint": FAKE_FINGERPRINT,
            "key_file": "/root/temp.pem", "tenancy": FAKE_TENANCY_2,
            "region": "us-chicago-1"})
        self.assertIn("[TEMP]", self._read_config())
        self.assertTrue(remove_oci_profile("TEMP"))
        self.assertEqual(self._read_config(), original)


if __name__ == "__main__":
    unittest.main()
