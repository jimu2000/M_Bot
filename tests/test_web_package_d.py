#!/usr/bin/env python3
"""工作包 D 的 Web 面板 API 集成测试（TestClient + mock oci 函数）。

覆盖：对象存储 API、Email Delivery API、抢机计划 API、改密码接口、
新页面路由（/objectstorage、/email）。

运行：python3 -m unittest tests.test_web_package_d -v

注意：
  - 测试密码哈希在 setUpClass 里生成并 patch 进 web.config，
    不使用任何真实密码。
  - 计划与 env 文件测试用临时 DATA_DIR / 临时 env 文件，不污染真实目录。
  - 所有写操作（删 Bucket/对象、创建发件人/凭据、改密码）全部 mock，
    不碰真实生产资源。
  - SMTP 凭据密码在测试里是假占位符，且只断言存在，不做快照比对。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from oci.client import NotFoundError
from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password, verify_password
from web.tasks import PLANS, TASKS

# 占位符（测试里不出现真实 OCID / 命名空间 / 密码）
FAKE_NAMESPACE = "fake-namespace"
FAKE_BUCKET = "test-bucket"
FAKE_USER = "ocid1.user.oc1..testxxxx"
FAKE_SENDER = "ocid1.emailsender.oc1..testxxxx"

TEST_PASSWORD = "test-password"
NEW_PASSWORD = "new-password-123"

FAKE_BUCKETS = [
    {"name": FAKE_BUCKET, "time-created": "2026-11-20T00:00:00Z",
     "public-access-type": "NoPublicAccess", "storage-tier": "Standard"},
    {"name": "backup-bucket", "time-created": "2026-11-21T00:00:00Z",
     "public-access-type": "NoPublicAccess", "storage-tier": "Standard"},
]

FAKE_OBJECTS = [
    {"name": "file.txt", "size": 1024,
     "time-created": "2026-11-20T00:00:00Z"},
    {"name": "dir/nested.txt", "size": 2048,
     "time-created": "2026-11-21T00:00:00Z"},
]

FAKE_SENDERS = [
    {"id": FAKE_SENDER, "email-address": "noreply@example.com",
     "lifecycle-state": "ACTIVE", "time-created": "2026-11-20T00:00:00Z"},
]

FAKE_IAM_USERS = [
    {"id": FAKE_USER, "name": "admin-user",
     "email": "admin@example.com", "lifecycle-state": "ACTIVE"},
]

FAKE_CREDENTIAL = {
    "id": "ocid1.smtpcredential.oc1..testxxxx",
    "username": "ocid1.user.oc1..testxxxx",
    "password": "fake-smtp-password-placeholder",
    "description": "rbot-clone",
}


class PackageDTestCase(unittest.TestCase):
    """API 测试基类：建 app、临时 DATA_DIR、临时 env 文件、登录辅助方法。"""

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
        self._env_file = os.path.join(self._tmp.name, "test.env")
        self._env_patcher = patch("web.config.ENV_FILE", self._env_file)
        self._env_patcher.start()
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        TASKS.reset()
        PLANS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)

    def tearDown(self):
        self._env_patcher.stop()
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self, username="admin", password=TEST_PASSWORD):
        """登录并返回响应（成功时 TestClient 自动保存 cookie）。"""
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})


class TestObjectStorageApi(PackageDTestCase):
    """对象存储 API：Bucket / 对象 CRUD。"""

    def setUp(self):
        super().setUp()
        self._login()
        self._ns_patcher = patch(
            "web.routes.objectstorage.get_namespace",
            return_value=FAKE_NAMESPACE)
        self._ns_patcher.start()
        self.addCleanup(self._ns_patcher.stop)

    @patch("web.routes.objectstorage.list_buckets",
           return_value=FAKE_BUCKETS)
    def test_list_buckets(self, mock_list):
        r = self.client.get("/api/objectstorage/buckets")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["buckets"]), 2)
        self.assertEqual(body["buckets"][0]["name"], FAKE_BUCKET)
        mock_list.assert_called_once_with(ANY, ANY,
                                          namespace=FAKE_NAMESPACE)

    @patch("web.routes.objectstorage.create_bucket",
           return_value=FAKE_BUCKETS[0])
    def test_create_bucket(self, mock_create):
        r = self.client.post("/api/objectstorage/buckets",
                             json={"name": FAKE_BUCKET})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["bucket"]["name"], FAKE_BUCKET)
        mock_create.assert_called_once_with(ANY, ANY, FAKE_BUCKET,
                                            namespace=FAKE_NAMESPACE)

    @patch("web.routes.objectstorage.create_bucket")
    def test_create_bucket_invalid_name_400(self, mock_create):
        mock_create.side_effect = ValueError("Bucket 名不合法")
        r = self.client.post("/api/objectstorage/buckets",
                             json={"name": "BAD NAME"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])

    @patch("web.routes.objectstorage.delete_bucket")
    def test_delete_bucket(self, mock_delete):
        r = self.client.delete(f"/api/objectstorage/buckets/{FAKE_BUCKET}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_delete.assert_called_once_with(ANY, FAKE_BUCKET,
                                            namespace=FAKE_NAMESPACE,
                                            empty=False)

    @patch("web.routes.objectstorage.delete_bucket")
    def test_delete_bucket_with_empty(self, mock_delete):
        r = self.client.delete(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}?empty=true")
        self.assertEqual(r.status_code, 200)
        mock_delete.assert_called_once_with(ANY, FAKE_BUCKET,
                                            namespace=FAKE_NAMESPACE,
                                            empty=True)

    @patch("web.routes.objectstorage.delete_bucket",
           side_effect=NotFoundError("not found"))
    def test_delete_bucket_not_found_404(self, mock_delete):
        r = self.client.delete(f"/api/objectstorage/buckets/{FAKE_BUCKET}")
        self.assertEqual(r.status_code, 404)

    @patch("web.routes.objectstorage.list_objects",
           return_value=FAKE_OBJECTS)
    def test_list_objects(self, mock_list):
        r = self.client.get(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}/objects")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["objects"]), 2)
        self.assertEqual(body["objects"][0]["size"], 1024)
        mock_list.assert_called_once_with(ANY, FAKE_BUCKET,
                                          namespace=FAKE_NAMESPACE)

    @patch("web.routes.objectstorage.upload_object",
           return_value={"name": "file.txt"})
    def test_upload_object(self, mock_upload):
        r = self.client.post(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}/objects",
            files={"file": ("file.txt", b"hello world", "text/plain")})
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["object_name"], "file.txt")
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        self.assertEqual(args[1], FAKE_BUCKET)
        self.assertEqual(args[2], "file.txt")
        # 临时文件路径传进去了（上传完路由会删掉它）。
        self.assertTrue(os.path.isabs(args[3]))
        self.assertEqual(kwargs["namespace"], FAKE_NAMESPACE)

    @patch("web.routes.objectstorage.upload_object",
           return_value={"name": "renamed.txt"})
    def test_upload_object_custom_name(self, mock_upload):
        r = self.client.post(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}/objects",
            data={"name": "renamed.txt"},
            files={"file": ("file.txt", b"x", "text/plain")})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["object_name"], "renamed.txt")

    @patch("web.routes.objectstorage.delete_object")
    def test_delete_object(self, mock_delete):
        r = self.client.delete(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}/objects/file.txt")
        self.assertEqual(r.status_code, 200)
        mock_delete.assert_called_once_with(ANY, FAKE_BUCKET, "file.txt",
                                            namespace=FAKE_NAMESPACE)

    @patch("web.routes.objectstorage.delete_object")
    def test_delete_object_with_slash_in_name(self, mock_delete):
        """对象名含斜杠时 :path 转换器能完整收到。"""
        r = self.client.delete(
            f"/api/objectstorage/buckets/{FAKE_BUCKET}/objects/dir/nested.txt")
        self.assertEqual(r.status_code, 200)
        mock_delete.assert_called_once_with(ANY, FAKE_BUCKET,
                                            "dir/nested.txt",
                                            namespace=FAKE_NAMESPACE)

    def test_unknown_profile_400(self):
        r = self.client.get("/api/objectstorage/buckets?profile=NOPE")
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_401(self):
        r = self.anon.get("/api/objectstorage/buckets")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/objectstorage/buckets",
                           json={"name": FAKE_BUCKET})
        self.assertEqual(r.status_code, 401)


class TestEmailApi(PackageDTestCase):
    """Email Delivery API：发件人 / SMTP 凭据。"""

    def setUp(self):
        super().setUp()
        self._login()

    @patch("web.routes.email.list_senders", return_value=FAKE_SENDERS)
    def test_list_senders(self, mock_list):
        r = self.client.get("/api/email/senders")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["senders"]), 1)
        self.assertEqual(body["senders"][0]["email-address"],
                         "noreply@example.com")
        mock_list.assert_called_once_with(ANY, ANY)

    @patch("web.routes.email.create_sender",
           return_value=FAKE_SENDERS[0])
    def test_create_sender(self, mock_create):
        r = self.client.post("/api/email/senders",
                             json={"email_address": "noreply@example.com"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["sender"]["id"], FAKE_SENDER)
        mock_create.assert_called_once_with(ANY, ANY,
                                            "noreply@example.com")

    @patch("web.routes.email.create_sender")
    def test_create_sender_invalid_email_400(self, mock_create):
        mock_create.side_effect = ValueError("邮箱格式不正确")
        r = self.client.post("/api/email/senders",
                             json={"email_address": "not-an-email"})
        self.assertEqual(r.status_code, 400)

    @patch("web.routes.email.list_iam_users", return_value=FAKE_IAM_USERS)
    def test_list_users(self, mock_list):
        r = self.client.get("/api/email/users")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["users"]), 1)
        self.assertEqual(r.json()["users"][0]["name"], "admin-user")

    @patch("web.routes.email.create_smtp_credential",
           return_value=FAKE_CREDENTIAL)
    def test_create_smtp_credential(self, mock_create):
        """SMTP 凭据只返回一次：响应里含用户名与密码（假占位符）。"""
        r = self.client.post("/api/email/smtp-credentials",
                             json={"user_id": FAKE_USER,
                                   "description": "my-mailer"})
        self.assertEqual(r.status_code, 201)
        cred = r.json()["credential"]
        self.assertEqual(cred["username"], FAKE_CREDENTIAL["username"])
        # 只断言密码字段存在且非空，不对密码值做快照比对。
        self.assertTrue(cred["password"])
        mock_create.assert_called_once_with(ANY, FAKE_USER,
                                            description="my-mailer")

    @patch("web.routes.email.create_smtp_credential",
           return_value=FAKE_CREDENTIAL)
    def test_create_smtp_credential_default_description(self, mock_create):
        r = self.client.post("/api/email/smtp-credentials",
                             json={"user_id": FAKE_USER})
        self.assertEqual(r.status_code, 201)
        mock_create.assert_called_once_with(ANY, FAKE_USER,
                                            description="rbot-clone")

    def test_unauthenticated_401(self):
        r = self.anon.get("/api/email/senders")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/email/smtp-credentials",
                           json={"user_id": FAKE_USER})
        self.assertEqual(r.status_code, 401)


class TestPlansApi(PackageDTestCase):
    """抢机计划 API：增删改查（临时 DATA_DIR）。"""

    def setUp(self):
        super().setUp()
        self._login()

    def test_crud_flow(self):
        # 列表初始为空。
        r = self.client.get("/api/plans")
        self.assertEqual(r.json()["plans"], [])
        # 新增。
        r = self.client.post("/api/plans", json={
            "cron": "0 2 * * *", "shape": "VM.Standard.A1.Flex",
            "ocpus": 4, "memory_gb": 24})
        self.assertEqual(r.status_code, 201)
        plan = r.json()["plan"]
        self.assertEqual(plan["id"], "p1")
        self.assertTrue(plan["enabled"])
        # 列表里有一条。
        r = self.client.get("/api/plans")
        self.assertEqual(len(r.json()["plans"]), 1)
        # 停用。
        r = self.client.post(f"/api/plans/{plan['id']}/toggle",
                             json={"enabled": False})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["plan"]["enabled"])
        # 启用。
        r = self.client.post(f"/api/plans/{plan['id']}/toggle",
                             json={"enabled": True})
        self.assertTrue(r.json()["plan"]["enabled"])
        # 删除。
        r = self.client.delete(f"/api/plans/{plan['id']}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/plans")
        self.assertEqual(r.json()["plans"], [])

    def test_create_invalid_cron_400(self):
        r = self.client.post("/api/plans", json={
            "cron": "not-a-cron", "shape": "VM.Standard.A1.Flex",
            "ocpus": 1, "memory_gb": 6})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cron", r.json()["error"])
        self.assertEqual(self.client.get("/api/plans").json()["plans"], [])

    def test_create_invalid_shape_400(self):
        r = self.client.post("/api/plans", json={
            "cron": "0 2 * * *", "shape": "VM.Standard.E5.Flex",
            "ocpus": 1, "memory_gb": 6})
        self.assertEqual(r.status_code, 400)

    def test_create_out_of_range_400(self):
        r = self.client.post("/api/plans", json={
            "cron": "0 2 * * *", "shape": "VM.Standard.A1.Flex",
            "ocpus": 8, "memory_gb": 24})
        self.assertEqual(r.status_code, 400)

    def test_delete_and_toggle_not_found_404(self):
        r = self.client.delete("/api/plans/p999")
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/plans/p999/toggle",
                             json={"enabled": True})
        self.assertEqual(r.status_code, 404)

    def test_plans_persist_to_temp_data_dir(self):
        self.client.post("/api/plans", json={
            "cron": "0 2 * * *", "shape": "VM.Standard.A1.Flex",
            "ocpus": 1, "memory_gb": 6})
        path = os.path.join(self._tmp.name, "grabber_plans.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["cron"], "0 2 * * *")

    def test_unauthenticated_401(self):
        r = self.anon.get("/api/plans")
        self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/plans", json={
            "cron": "0 2 * * *", "shape": "VM.Standard.A1.Flex",
            "ocpus": 1, "memory_gb": 6})
        self.assertEqual(r.status_code, 401)


class TestChangePassword(PackageDTestCase):
    """改密码接口：验旧密码 + 强度校验 + 写 env 文件。"""

    def setUp(self):
        super().setUp()
        self._login()

    def _env_text(self):
        if not os.path.exists(self._env_file):
            return ""
        with open(self._env_file, encoding="utf-8") as f:
            return f.read()

    def test_success_updates_env_file(self):
        # env 文件里先有一行会话密钥，改密码后必须保留。
        with open(self._env_file, "w", encoding="utf-8") as f:
            f.write("RBOT_WEB_SECRET=old-secret-value\n")
            f.write(f"RBOT_WEB_PASSWORD_HASH='{hash_password(TEST_PASSWORD)}'\n")
        r = self.client.post("/api/auth/change-password",
                             json={"old_password": TEST_PASSWORD,
                                   "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("重启 Web 面板服务后生效", body["message"])
        # env 文件里的哈希已换成新密码的哈希，会话密钥行保留。
        text = self._env_text()
        self.assertIn("RBOT_WEB_SECRET=old-secret-value", text)
        self.assertEqual(text.count("RBOT_WEB_PASSWORD_HASH="), 1)
        for line in text.splitlines():
            if line.startswith("RBOT_WEB_PASSWORD_HASH="):
                stored = line.split("=", 1)[1].strip("'\"")
                self.assertTrue(verify_password(NEW_PASSWORD, stored))
                self.assertFalse(verify_password(TEST_PASSWORD, stored))
        # env 文件权限 600。
        self.assertEqual(oct(os.stat(self._env_file).st_mode & 0o777), "0o600")

    def test_success_creates_env_file_when_missing(self):
        """env 文件不存在时新建（只含哈希行）。"""
        self.assertFalse(os.path.exists(self._env_file))
        r = self.client.post("/api/auth/change-password",
                             json={"old_password": TEST_PASSWORD,
                                   "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 200)
        text = self._env_text()
        self.assertEqual(text.count("RBOT_WEB_PASSWORD_HASH="), 1)

    def test_wrong_old_password_401(self):
        r = self.client.post("/api/auth/change-password",
                             json={"old_password": "wrong-password",
                                   "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(os.path.exists(self._env_file))

    def test_weak_new_password_400(self):
        """新密码太短 / 纯字母 / 纯数字时返回 400，env 文件不动。"""
        for weak in ("short1", "onlyletters", "12345678", "abc123"):
            r = self.client.post(
                "/api/auth/change-password",
                json={"old_password": TEST_PASSWORD,
                      "new_password": weak})
            self.assertEqual(r.status_code, 400, weak)
            self.assertFalse(r.json()["ok"])
        self.assertFalse(os.path.exists(self._env_file))

    def test_same_as_old_password_400(self):
        r = self.client.post("/api/auth/change-password",
                             json={"old_password": TEST_PASSWORD,
                                   "new_password": TEST_PASSWORD})
        self.assertEqual(r.status_code, 400)

    def test_unauthenticated_401(self):
        r = self.anon.post("/api/auth/change-password",
                           json={"old_password": TEST_PASSWORD,
                                 "new_password": NEW_PASSWORD})
        self.assertEqual(r.status_code, 401)


class TestNewPages(PackageDTestCase):
    """新页面路由：/objectstorage、/email。"""

    def test_redirect_when_logged_out(self):
        for path in ("/objectstorage", "/email"):
            r = self.anon.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 302, path)
            self.assertEqual(r.headers["location"], "/login", path)

    def test_ok_when_logged_in(self):
        self._login()
        for path in ("/objectstorage", "/email"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn("text/html", r.headers["content-type"])

    def test_objectstorage_page_contains_key_elements(self):
        self._login()
        text = self.client.get("/objectstorage").text
        self.assertIn("对象存储", text)
        self.assertIn("新建 Bucket", text)
        self.assertIn("objectstorage.js", text)

    def test_email_page_contains_one_time_warning(self):
        """Email 页必须有“只显示一次”的明确提示。"""
        self._login()
        text = self.client.get("/email").text
        self.assertIn("请立即保存，关闭后无法再次查看", text)
        self.assertIn("email.js", text)

    def test_sidebar_links_present(self):
        """侧边栏有对象存储与 Email 入口。"""
        self._login()
        text = self.client.get("/overview").text
        self.assertIn('href="/objectstorage"', text)
        self.assertIn('href="/email"', text)
        # 改密码弹窗在 base.html 里，所有继承页都有。
        self.assertIn("change-password-modal", text)
        self.assertIn("settings.js", text)


if __name__ == "__main__":
    unittest.main()
