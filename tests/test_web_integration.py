#!/usr/bin/env python3
"""阶段 3 集成测试（协调员补写）：覆盖三方对接时修掉的协调问题。

  - 镜像解析：前端架构占位符 -> config.IMAGE_IDS 查表，未配置时报 400。
  - /quota 页面路由与模板。
  - pages.py 给模板传 current_page 与 username（侧边栏高亮、顶栏用户名）。
  - 侧边栏 /overview 链接只有一个（去重后）。
  - 全部 8 个页面登录后都能渲染 200。

运行：python3 -m unittest tests.test_web_integration -v
"""

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web import config
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes.create import resolve_image_id
from web.tasks import TASKS

TEST_PASSWORD = "test-password-123"
FAKE_ARM_IMAGE = "ocid1.image.oc1..armtestxxxx"
FAKE_X86_IMAGE = "ocid1.image.oc1..x86testxxxx"


class IntegrationTestCase(unittest.TestCase):
    """基类：建 app、临时 DATA_DIR、登录辅助方法。"""

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
        TASKS.reset()
        self.client = TestClient(self.app)
        self.anon = TestClient(self.app)

    def tearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()

    def _login(self):
        r = self.client.post("/api/auth/login",
                             json={"username": "admin",
                                   "password": TEST_PASSWORD})
        self.assertEqual(r.status_code, 200)
        return r


class TestResolveImageId(unittest.TestCase):
    """镜像解析：架构占位符查表 / 真实 OCID 直通 / 未配置报错。"""

    def test_aarch64_placeholder_resolves(self):
        with patch.dict(config.IMAGE_IDS, {"aarch64": FAKE_ARM_IMAGE}):
            self.assertEqual(
                resolve_image_id("ocid1.image.oc1..xxxxxxxx-aarch64"),
                FAKE_ARM_IMAGE)

    def test_x86_placeholder_resolves(self):
        with patch.dict(config.IMAGE_IDS, {"x86_64": FAKE_X86_IMAGE}):
            self.assertEqual(
                resolve_image_id("ocid1.image.oc1..xxxxxxxx-x86"),
                FAKE_X86_IMAGE)

    def test_unconfigured_placeholder_raises(self):
        with patch.dict(config.IMAGE_IDS,
                        {"aarch64": "ocid1.image.oc1..xxxxxxxx"}):
            with self.assertRaises(ValueError) as ctx:
                resolve_image_id("ocid1.image.oc1..xxxxxxxx-aarch64")
            self.assertIn("IMAGE_IDS", str(ctx.exception))

    def test_real_ocid_passthrough(self):
        self.assertEqual(resolve_image_id(FAKE_ARM_IMAGE), FAKE_ARM_IMAGE)

    def test_bare_placeholder_raises(self):
        with self.assertRaises(ValueError):
            resolve_image_id("ocid1.image.oc1..xxxxxxxx")


class TestCreateImageResolution(IntegrationTestCase):
    """POST /api/create 走镜像解析：未配置时 400，不进抢机。"""

    def test_create_with_unconfigured_image_returns_400(self):
        self._login()
        body = {
            "shape": "VM.Standard.E2.1.Micro",
            "image_id": "ocid1.image.oc1..xxxxxxxx-x86",
            "subnet_id": "ocid1.subnet.oc1..testxxxx",
            "availability_domain": "AD-1",
            "display_name": "test-instance",
            "ssh_keys_file": "/tmp/test-keys.pub",
        }
        with patch("web.routes.create.resolve_profile") as mock_profile, \
             patch("web.routes.create.can_launch",
                   return_value=(True, "")), \
             patch.dict(config.IMAGE_IDS,
                        {"x86_64": "ocid1.image.oc1..xxxxxxxx"}), \
             patch("web.routes.create.start_grab_task") as mock_grab:
            mock_profile.return_value = (object(), "ocid1.tenancy.oc1..test")
            r = self.client.post("/api/create", json=body)
            self.assertEqual(r.status_code, 400)
            self.assertIn("IMAGE_IDS", r.json()["error"])
            mock_grab.assert_not_called()


class TestModuleLevelApp(unittest.TestCase):
    """模块级 app 实例：哈希已设置时 `uvicorn web.app:app` 可用。"""

    def test_app_exists_when_hash_set(self):
        import web.app as app_module
        with patch("web.config.ADMIN_PASSWORD_HASH",
                   hash_password(TEST_PASSWORD)):
            importlib.reload(app_module)
            self.assertIsNotNone(getattr(app_module, "app", None))
            self.assertEqual(app_module.app.title, "R-Bot Web 面板")
        # 恢复：删掉模块级 app 再 reload（reload 不清空旧命名空间，
        # 不删会残留），无哈希时不应重建。
        del app_module.app
        importlib.reload(app_module)
        self.assertFalse(hasattr(app_module, "app"))


class TestQuotaPage(IntegrationTestCase):
    """配额页路由与模板。"""

    def test_quota_page_logged_in(self):
        self._login()
        r = self.client.get("/quota")
        self.assertEqual(r.status_code, 200)
        self.assertIn("免费额度", r.text)
        self.assertIn("quota.js", r.text)
        self.assertIn("A1.Flex", r.text)

    def test_quota_page_anon_redirects(self):
        r = self.anon.get("/quota", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])


class TestPageContext(IntegrationTestCase):
    """模板上下文：current_page 高亮侧边栏，username 显示在顶栏。"""

    def test_overview_highlights_hosts_and_shows_username(self):
        self._login()
        r = self.client.get("/overview")
        self.assertEqual(r.status_code, 200)
        # 侧边栏"主机"链接带 active（current_page=overview 生效）。
        self.assertIn('data-page="overview" class="active"', r.text)
        # 顶栏资源区显示登录用户名。
        self.assertIn('id="res-username">admin<', r.text)

    def test_sidebar_has_single_overview_link(self):
        self._login()
        r = self.client.get("/overview")
        # 侧边栏里指向 /overview 的链接只有一个（顶栏另有一个图标快捷
        # 方式，不计入侧边栏；data-page 属性只在侧边栏链接上出现）。
        self.assertEqual(r.text.count('data-page="overview"'), 1)

    def test_all_pages_render_200(self):
        self._login()
        for path in ("/overview", "/instances", "/create", "/quota",
                     "/volumes", "/tasks", "/terminal"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, f"{path} 应返回 200")
            self.assertIn("R-Bot", r.text, f"{path} 应含品牌名")
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
