#!/usr/bin/env python3
"""Web 面板 IAM 用户管理 API 的测试（TestClient + mock OCI/IAM）。

运行：python3 -m unittest tests.test_web_iam -v

约定：
  - IAM 函数 mock：patch web.routes.iam 命名空间里的 get_oci_client、
    get_profile_tenancy_id、get_profile_user_id、
    get_profile_api_fingerprint 与 oci.iam 的 create_user、
    create_bot_permission_user、create_api_key、delete_user、
    list_api_keys、delete_api_key、create_or_reset_ui_password、
    is_last_admin（iam.py 用 from-import，与 handlers_extra 的测试
    约定一致）。
  - 测试数据只用假 OCID/邮箱，绝不出现真实值。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes import iam as iam_routes

TEST_PASSWORD = "test-pass-123"

FAKE_TENANCY = "ocid1.tenancy.oc1..testtenant1"
FAKE_USER = "ocid1.user.oc1..testuser0001"
FAKE_USER_2 = "ocid1.user.oc1..testuser0002"
FAKE_FINGERPRINT = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"

FAKE_USERS = [
    {"id": FAKE_USER, "name": "alice", "email": "alice@example.com",
     "lifecycle-state": "ACTIVE", "time-created": "2026-01-01T00:00:00Z"},
    {"id": FAKE_USER_2, "name": "bob", "email": "bob@example.com",
     "lifecycle-state": "ACTIVE", "time-created": "2026-01-02T00:00:00Z"},
]

FAKE_KEYS = [
    {"fingerprint": FAKE_FINGERPRINT, "lifecycle-state": "ACTIVE",
     "time-created": "2026-01-01T00:00:00Z"},
]

FAKE_PEM = ("-----BEGIN PRIVATE KEY-----\n"
            "ZmFrZS1rZXktY29udGVudA==\n"
            "-----END PRIVATE KEY-----\n")


class IamApiTestCase(unittest.TestCase):
    """IAM API 测试基类：建 app、登录、mock OCI/IAM。"""

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
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)
        self._login()

        self.cli = MagicMock()
        self.cli.sdk_list_users.return_value = list(FAKE_USERS)
        self._cli_patch = patch.object(
            iam_routes, "get_oci_client", return_value=self.cli)
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)
        self._tenancy_patch = patch.object(
            iam_routes, "get_profile_tenancy_id",
            return_value=FAKE_TENANCY)
        self._tenancy_patch.start()
        self.addCleanup(self._tenancy_patch.stop)
        # 默认：目标用户不是当前 API 用户、不是最后一个管理员。
        self._api_user_patch = patch.object(
            iam_routes, "get_profile_user_id", return_value=FAKE_USER_2)
        self._api_user_patch.start()
        self.addCleanup(self._api_user_patch.stop)
        self._fp_patch = patch.object(
            iam_routes, "get_profile_api_fingerprint",
            return_value="00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff")
        self._fp_patch.start()
        self.addCleanup(self._fp_patch.stop)
        self._last_admin_patch = patch.object(
            iam_routes, "is_last_admin", return_value=False)
        self._last_admin_patch.start()
        self.addCleanup(self._last_admin_patch.stop)

    def _login(self, username="admin", password=None):
        if password is None:
            password = TEST_PASSWORD
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})


class TestListUsers(IamApiTestCase):
    def test_list_users(self):
        resp = self.client.get("/api/iam/users?profile=DEFAULT")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["users"]), 2)
        self.assertEqual(data["users"][0]["name"], "alice")
        self.cli.sdk_list_users.assert_called_once_with(FAKE_TENANCY)

    def test_list_users_requires_login(self):
        resp = self.anon.get("/api/iam/users", follow_redirects=False)
        self.assertEqual(resp.status_code, 401)


class TestCreateUser(IamApiTestCase):
    def test_create_user(self):
        created = dict(FAKE_USERS[0], name="carol",
                       email="carol@example.com")
        with patch.object(iam_routes, "create_user",
                          return_value=created) as mock_create:
            resp = self.client.post("/api/iam/users", json={
                "profile": "DEFAULT", "name": "carol",
                "email": "carol@example.com", "description": "test"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["user"]["name"], "carol")
        mock_create.assert_called_once_with(
            self.cli, FAKE_TENANCY, "carol", "carol@example.com", "test")

    def test_create_user_missing_fields(self):
        resp = self.client.post("/api/iam/users", json={
            "profile": "DEFAULT", "name": "", "email": ""})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不能为空", resp.json()["error"])


class TestCreateBotUser(IamApiTestCase):
    def test_create_bot_user_returns_pem_once(self):
        bot_result = {"user_id": FAKE_USER, "group_created": False,
                      "policy_created": False}
        key_result = {"fingerprint": FAKE_FINGERPRINT,
                      "private_key_pem": FAKE_PEM.encode("utf-8"),
                      "lifecycle_state": "ACTIVE"}
        with patch.object(iam_routes, "create_bot_permission_user",
                          return_value=bot_result), \
             patch.object(iam_routes, "create_api_key",
                          return_value=key_result):
            resp = self.client.post("/api/iam/users/bot",
                                    json={"profile": "DEFAULT"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user_name"], "api_user")
        self.assertEqual(data["private_key_pem"], FAKE_PEM)
        self.assertEqual(data["key"]["fingerprint"], FAKE_FINGERPRINT)


class TestDeleteUser(IamApiTestCase):
    def test_delete_user(self):
        with patch.object(iam_routes, "delete_user",
                          return_value=None) as mock_delete:
            resp = self.client.delete(
                f"/api/iam/users/{FAKE_USER}?profile=DEFAULT")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        mock_delete.assert_called_once_with(self.cli, FAKE_USER)

    def test_delete_user_refuses_current_api_user(self):
        # 目标用户就是当前 profile 的 API 用户 -> 拒绝。
        with patch.object(iam_routes, "get_profile_user_id",
                          return_value=FAKE_USER), \
             patch.object(iam_routes, "delete_user",
                          return_value=None) as mock_delete:
            resp = self.client.delete(
                f"/api/iam/users/{FAKE_USER}?profile=DEFAULT")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("API 用户", resp.json()["error"])
        mock_delete.assert_not_called()

    def test_delete_user_refuses_last_admin(self):
        # 目标用户是 Administrators 组最后一个成员 -> 拒绝。
        with patch.object(iam_routes, "is_last_admin",
                          return_value=True), \
             patch.object(iam_routes, "delete_user",
                          return_value=None) as mock_delete:
            resp = self.client.delete(
                f"/api/iam/users/{FAKE_USER}?profile=DEFAULT")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("最后一个管理员", resp.json()["error"])
        mock_delete.assert_not_called()


class TestApiKeys(IamApiTestCase):
    def test_list_keys(self):
        with patch.object(iam_routes, "list_api_keys",
                          return_value=list(FAKE_KEYS)):
            resp = self.client.get(
                f"/api/iam/users/{FAKE_USER}/keys?profile=DEFAULT")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["keys"]), 1)
        self.assertEqual(data["keys"][0]["fingerprint"], FAKE_FINGERPRINT)

    def test_create_key_returns_pem_once(self):
        key_result = {"fingerprint": FAKE_FINGERPRINT,
                      "private_key_pem": FAKE_PEM.encode("utf-8"),
                      "lifecycle_state": "ACTIVE"}
        with patch.object(iam_routes, "create_api_key",
                          return_value=key_result):
            resp = self.client.post(
                f"/api/iam/users/{FAKE_USER}/keys",
                json={"profile": "DEFAULT"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["private_key_pem"], FAKE_PEM)
        self.assertEqual(data["key"]["fingerprint"], FAKE_FINGERPRINT)

    def test_delete_key(self):
        with patch.object(iam_routes, "delete_api_key",
                          return_value=None) as mock_delete:
            resp = self.client.delete(
                f"/api/iam/users/{FAKE_USER}/keys/{FAKE_FINGERPRINT}"
                "?profile=DEFAULT")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        mock_delete.assert_called_once_with(
            self.cli, FAKE_USER, FAKE_FINGERPRINT)

    def test_delete_key_refuses_current_api_key(self):
        # 目标 Key 就是当前 profile 正在用的密钥 -> 拒绝。
        with patch.object(iam_routes, "get_profile_user_id",
                          return_value=FAKE_USER), \
             patch.object(iam_routes, "get_profile_api_fingerprint",
                          return_value=FAKE_FINGERPRINT), \
             patch.object(iam_routes, "delete_api_key",
                          return_value=None) as mock_delete:
            resp = self.client.delete(
                f"/api/iam/users/{FAKE_USER}/keys/{FAKE_FINGERPRINT}"
                "?profile=DEFAULT")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("API Key", resp.json()["error"])
        mock_delete.assert_not_called()


class TestResetPassword(IamApiTestCase):
    def test_reset_password(self):
        with patch.object(iam_routes, "create_or_reset_ui_password",
                          return_value="TempPass123!") as mock_reset:
            resp = self.client.post(
                f"/api/iam/users/{FAKE_USER}/reset-password",
                json={"profile": "DEFAULT"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["password"], "TempPass123!")
        mock_reset.assert_called_once_with(self.cli, FAKE_USER)


class TestAccountsPageIamSection(IamApiTestCase):
    def test_accounts_page_has_iam_section(self):
        resp = self.client.get("/accounts")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("IAM 用户管理", resp.text)
        self.assertIn("iam-profile", resp.text)
        self.assertIn("table-wrap", resp.text)


if __name__ == "__main__":
    unittest.main()
