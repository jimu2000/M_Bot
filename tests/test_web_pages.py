#!/usr/bin/env python3
"""Web 面板页面渲染测试（mock 掉登录依赖，只测模板渲染）。

后端 app 尚未写好时，用这里的最小测试 app 挂 static 与 templates，
GET 每个页面，断言 200 且关键元素存在。

运行：python3 -m unittest tests.test_web_pages -v
同时会把每个页面渲染后的 HTML 存到 /tmp/rbot-pages/ 供协调员检查。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
DUMP_DIR = "/tmp/rbot-pages"


def build_test_app():
    """最小测试 app：只挂 static 与模板渲染，不依赖真实后端。"""
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(WEB_DIR, "templates"))

    # 页面路由：与正式后端约定的 URL 一致
    PAGES = {
        "/login": ("login.html", None),
        "/overview": ("overview.html", "overview"),
        "/instances": ("instances.html", "instances"),
        "/volumes": ("volumes.html", "volumes"),
        "/create": ("create.html", "create"),
        "/tasks": ("tasks.html", "tasks"),
    }

    for path, (template_name, current_page) in PAGES.items():
        def make_handler(tpl=template_name, page=current_page):
            def handler(request: Request):
                return templates.TemplateResponse(request, tpl, {
                    "request": request,
                    "current_page": page,
                    "username": "test-user",
                })
            return handler
        app.get(path)(make_handler())

    return app


class TestWebPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_test_app())
        # 渲染每个页面并存到 /tmp/rbot-pages/ 供协调员检查
        os.makedirs(DUMP_DIR, exist_ok=True)
        cls.pages = {}
        for path in ("/login", "/overview", "/instances", "/volumes", "/create", "/tasks"):
            resp = cls.client.get(path)
            cls.pages[path] = resp
            name = path.strip("/") + ".html"
            with open(os.path.join(DUMP_DIR, name), "w", encoding="utf-8") as f:
                f.write(resp.text)

    def _assert_page_ok(self, path, must_contain):
        resp = self.pages[path]
        self.assertEqual(resp.status_code, 200, f"{path} 应返回 200，实际 {resp.status_code}")
        for needle in must_contain:
            self.assertIn(needle, resp.text, f"{path} 应包含 {needle!r}")

    def test_all_pages_return_200(self):
        """6 个页面全部返回 200。"""
        for path, resp in self.pages.items():
            self.assertEqual(resp.status_code, 200, f"{path} 应返回 200，实际 {resp.status_code}")

    def test_no_real_secrets_in_rendered_html(self):
        """渲染后的 HTML 不含真实 IP/OCID/密码（只允许占位符与文档网段）。
        注意：测试里不写真实 IP 的字面量，改用正则白名单检查。"""
        import re
        ipv4_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for path, resp in self.pages.items():
            # 允许出现的 IP 只有文档网段 203.0.113.x（TEST-NET-3）
            for ip in ipv4_re.findall(resp.text):
                self.assertTrue(
                    ip.startswith("203.0.113."),
                    f"{path} 出现了非文档网段 IP：{ip}",
                )
            # 占位 OCID 允许出现（ocid1.<类型>.oc1..xxxxxxxx，允许 -arch 后缀），
            # 但不允许出现真实 OCID 形态（长串 base64 后缀）
            placeholder_re = re.compile(r"ocid1\.[a-z]+\.oc1\.\.x+(?:-[a-z0-9]+)?")
            stripped = placeholder_re.sub("", resp.text)
            self.assertNotIn("ocid1.", stripped, f"{path} 出现了非占位 OCID")

    def test_login_page(self):
        """登录页：用户名/密码框、登录按钮、错误区、凭据说明。"""
        self._assert_page_ok("/login", [
            'id="username"',
            'id="password"',
            'id="login-btn"',
            'id="login-error"',
            'id="lockout-countdown"',
            "默认凭据请查看部署文档",
            "R-Bot 开源克隆版",
            "/static/js/login.js",
        ])
        # 登录页不继承 base，不应有侧边栏
        self.assertNotIn('class="sidebar"', self.pages["/login"].text)

    def test_base_layout_elements(self):
        """继承 base 的页面：顶栏、资源栏、侧边栏、主题与退出按钮。"""
        for path in ("/overview", "/instances", "/volumes", "/create", "/tasks"):
            self._assert_page_ok(path, [
                "R-Bot",
                "开源克隆版",
                'id="theme-select"',
                'id="mode-toggle"',
                'id="logout-btn"',
                'id="gauge-cpu"',
                'id="gauge-mem"',
                'id="gauge-disk"',
                'id="net-down"',
                'id="net-up"',
                'class="sidebar"',
                "/static/js/app.js",
                "/static/css/base.css",
                "/static/css/themes.css",
            ])

    def test_sidebar_highlights_current_page(self):
        """当前页面在侧边栏高亮。"""
        checks = {
            "/overview": 'data-page="overview" class="active"',
            "/instances": 'data-page="instances" class="active"',
            "/volumes": 'data-page="volumes" class="active"',
            "/create": 'data-page="create" class="active"',
            "/tasks": 'data-page="tasks" class="active"',
        }
        for path, needle in checks.items():
            self.assertIn(needle, self.pages[path].text, f"{path} 侧边栏应高亮当前页面")

    def test_overview_page(self):
        """概览页：已保存会话标题、搜索框、添加按钮、卡片容器、空状态。"""
        self._assert_page_ok("/overview", [
            "已保存会话",
            'id="host-count"',
            'id="host-search"',
            'id="btn-add-host"',
            'id="host-grid"',
            'id="host-empty"',
            "还没有保存的主机",
            'id="host-modal"',
            "/static/js/overview.js",
        ])

    def test_instances_page(self):
        """实例管理页：Profile 下拉、刷新按钮、表格列、终止确认文案。"""
        self._assert_page_ok("/instances", [
            'id="profile-select"',
            "DEFAULT",
            'id="btn-refresh-instances"',
            'id="instance-table"',
            "公网 IP",
            'id="terminate-modal"',
            "将永久删除引导卷",
            "/static/js/instances.js",
        ])

    def test_create_page(self):
        """创建实例页：四卡表单字段、快捷开机、提交按钮。"""
        self._assert_page_ok("/create", [
            "基础配置",
            "系统配置",
            "网络与安全",
            "高级选项",
            'id="shape"',
            "VM.Standard.A1.Flex",
            "VM.Standard.E2.1.Micro",
            'id="ocpus"',
            'id="memory-gb"',
            'id="image-id"',
            "ocid1.image.oc1..xxxxxxxx",
            'id="disk-gb"',
            'id="count"',
            'id="public-ip"',
            'id="ipv6"',
            'id="availability-domain"',
            'id="subnet-id"',
            'id="ssh-keys-file"',
            'id="min-delay"',
            'id="max-delay"',
            'id="target-cpu"',
            'id="startup-script"',
            "cloud-init 脚本（bash）",
            "快捷开机",
            'id="preset-grid"',
            "AMD 微型",
            "ARM A1 2C/12G",
            "ARM A1 4C/24G",
            'id="btn-create"',
            "开始创建",
            "/static/js/create.js",
        ])

    def test_volumes_page(self):
        """启动盘页：卷表格、孤儿盘区、扩容/删除弹窗。"""
        self._assert_page_ok("/volumes", [
            'id="volume-table"',
            'id="orphan-tbody"',
            "孤儿盘",
            'id="resize-modal"',
            'id="resize-size"',
            'id="delete-volume-modal"',
            "/static/js/volumes.js",
        ])

    def test_tasks_page(self):
        """任务管理页：任务表格、刷新按钮。"""
        self._assert_page_ok("/tasks", [
            'id="task-table"',
            'id="task-tbody"',
            'id="btn-refresh-tasks"',
            "进度消息",
            "/static/js/tasks.js",
        ])

    def test_static_files_served(self):
        """静态 CSS/JS 全部可正常访问（200）。"""
        for asset in (
            "/static/css/base.css",
            "/static/css/themes.css",
            "/static/js/app.js",
            "/static/js/login.js",
            "/static/js/overview.js",
            "/static/js/instances.js",
            "/static/js/create.js",
            "/static/js/volumes.js",
            "/static/js/tasks.js",
        ):
            resp = self.client.get(asset)
            self.assertEqual(resp.status_code, 200, f"{asset} 应返回 200，实际 {resp.status_code}")

    def test_no_xterm_outside_terminal(self):
        """除终端页外，其他页面不引用 xterm.js。"""
        for path, resp in self.pages.items():
            self.assertNotIn("xterm", resp.text.lower(), f"{path} 不应引用 xterm.js")


class TestRealBackendIntegration(unittest.TestCase):
    """与真实后端 app 的集成冒烟测试（后端已就绪时跑）。

    用测试密码哈希启动真实 web.app：验证登录页渲染、未登录重定向、
    登录成功后概览页渲染、/api/system 返回嵌套格式（前端已兼容）。
    后端模块导入失败时跳过（后端子代理工作中）。
    """

    @classmethod
    def setUpClass(cls):
        try:
            from web import config
            from web.app import create_app
            from web.auth import hash_password
        except Exception as exc:  # noqa: BLE001 - 后端未就绪时跳过
            raise unittest.SkipTest(f"真实后端未就绪，跳过集成测试：{exc}")
        # 测试用密码哈希（仅测试进程内有效，不写文件）
        config.ADMIN_PASSWORD_HASH = hash_password("test-password-123")
        cls.app = create_app()
        cls.client = TestClient(cls.app, raise_server_exceptions=False)

    def test_login_page_renders(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="login-btn"', resp.text)
        self.assertIn("R-Bot 开源克隆版", resp.text)

    def test_overview_redirects_when_anonymous(self):
        # 用全新 client（不带登录 cookie，避免其他测试的会话串用）
        anon = TestClient(self.app, raise_server_exceptions=False)
        resp = anon.get("/overview", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/login")

    def test_login_and_overview_renders(self):
        resp = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("ok"))
        resp = self.client.get("/overview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="host-grid"', resp.text)
        self.assertIn("已保存会话", resp.text)

    def test_system_api_nested_shape(self):
        """后端 /api/system 返回嵌套格式（前端 app.js 已兼容该格式）。"""
        self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        resp = self.client.get("/api/system")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("cpu", data)
        self.assertIn("percent", data["cpu"])
        self.assertIn("network", data)
        self.assertIn("rx_bytes_per_sec", data["network"])

    def test_tasks_api_empty_list(self):
        self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        resp = self.client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("tasks"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
