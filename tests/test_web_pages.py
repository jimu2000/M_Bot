#!/usr/bin/env python3
"""Web 面板页面渲染测试（mock 掉登录依赖，只测模板渲染）。

后端 app 尚未写好时，用这里的最小测试 app 挂 static 与 templates，
GET 每个页面，断言 200 且关键元素存在。

运行：python3 -m unittest tests.test_web_pages -v
同时会把每个页面渲染后的 HTML 存到 /tmp/mbot-pages/ 供协调员检查。
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
DUMP_DIR = "/tmp/mbot-pages"


def build_test_app():
    """最小测试 app：只挂 static 与模板渲染，不依赖真实后端。"""
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")
    templates = Jinja2Templates(directory=os.path.join(WEB_DIR, "templates"))

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
            # The only IPs allowed to appear are the documentation range 203.0.113.x (TEST-NET-3). 允许出现的 IP 只有文档网段 203.0.113.x（TEST-NET-3）。
            for ip in ipv4_re.findall(resp.text):
                self.assertTrue(
                    ip.startswith("203.0.113."),
                    f"{path} 出现了非文档网段 IP：{ip}",
                )
            # Placeholder OCIDs are allowed (ocid1.<type>.oc1..xxxxxxxx, with an optional -arch suffix), but real-OCID shapes (long base64 suffixes) are not. 占位 OCID 允许出现（ocid1.<类型>.oc1..xxxxxxxx，允许 -arch 后缀），但不允许出现真实 OCID 形态（长串 base64 后缀）。
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
            "M_Bot Oracle开源管理面板",
            "/static/js/login.js",
            "https://github.com/jimu2000/M_Bot",
            "GitHub 免费开源项目",
            'target="_blank"',
        ])
        self.assertNotIn('class="sidebar"', self.pages["/login"].text)

    def test_base_layout_elements(self):
        """继承 base 的页面：顶栏、资源栏、侧边栏、主题与退出按钮。"""
        for path in ("/overview", "/instances", "/volumes", "/create", "/tasks"):
            self._assert_page_ok(path, [
                "M_Bot",
                "Oracle管理面板",
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
                "https://github.com/jimu2000/M_Bot",
                'target="_blank"',
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
        """实例管理页：Profile 下拉（由 JS 从 /api/profiles 动态填充，
        不再写死 DEFAULT 选项）、刷新按钮、卡片容器、IP 管理与启动盘
        弹窗、终止确认文案。"""
        self._assert_page_ok("/instances", [
            'id="profile-select"',
            'id="btn-refresh-instances"',
            'id="instance-grid"',
            "公网 IP",
            'id="terminate-modal"',
            "将永久删除引导卷",
            'id="ip-modal"',
            'id="boot-volume-modal"',
            "/static/js/instances.js",
        ])
        resp = self.client.get("/instances")
        self.assertNotIn('<option value="DEFAULT"', resp.text)

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
            "/static/js/i18n.js",
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
        # Test password hash (valid only inside the test process, never written to a file). 测试用密码哈希（仅测试进程内有效，不写文件）。
        config.ADMIN_PASSWORD_HASH = hash_password("test-password-123")
        cls.app = create_app()
        cls.client = TestClient(cls.app, raise_server_exceptions=False)

    def test_login_page_renders(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="login-btn"', resp.text)
        self.assertIn("M_Bot Oracle开源管理面板", resp.text)

    def test_static_files_send_no_cache(self):
        # Static files must send Cache-Control: no-cache, or Safari heuristically caches stale JS. 静态文件必须带 Cache-Control: no-cache，否则 Safari 会按启发式缓存旧 JS。
        for asset in ("/static/js/overview.js", "/static/css/base.css"):
            resp = self.client.get(asset)
            self.assertEqual(resp.status_code, 200, asset)
            self.assertEqual(resp.headers.get("cache-control"), "no-cache", asset)

    def test_login_page_clarity_when_configured(self):
        from unittest.mock import patch

        from web import config

        with patch.object(config, "CLARITY_ID", "claritytest1"):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("clarity.ms/tag/", resp.text)
        self.assertIn('"claritytest1"', resp.text)
        self.assertNotIn("getclicky.com", resp.text)

    def test_login_page_no_clarity_when_unconfigured(self):
        from unittest.mock import patch

        from web import config

        with patch.object(config, "CLARITY_ID", ""):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("clarity.ms", resp.text)
        self.assertNotIn("getclicky.com", resp.text)

    def test_login_page_matomo_when_configured(self):
        from unittest.mock import patch

        from web import config

        with patch.object(config, "MATOMO_URL",
                          "https://matomo.example.com/"), \
                patch.object(config, "MATOMO_SITE_ID", "7"):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        # URL 由 JS 拼接（g.src=u+'matomo.js'），断言基地址与拼接形式。
        self.assertIn('var u="https://matomo.example.com/";', resp.text)
        self.assertIn("g.src=u+'matomo.js'", resp.text)
        self.assertIn("setSiteId', '7'", resp.text)
        self.assertIn("matomo.php", resp.text)

    def test_login_page_no_matomo_when_unconfigured(self):
        from unittest.mock import patch

        from web import config

        with patch.object(config, "MATOMO_URL", ""), \
                patch.object(config, "MATOMO_SITE_ID", ""):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("matomo.js", resp.text)
        self.assertNotIn("matomo.php", resp.text)

    def test_login_page_no_matomo_when_only_url_configured(self):
        # URL 配了、站点 ID 没配时也不加载（两项缺一不可）。
        from unittest.mock import patch

        from web import config

        with patch.object(config, "MATOMO_URL",
                          "https://matomo.example.com/"), \
                patch.object(config, "MATOMO_SITE_ID", ""):
            resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("matomo.js", resp.text)

    def test_overview_redirects_when_anonymous(self):
        # Use a fresh client (no login cookie, so sessions do not leak across tests). 用全新 client（不带登录 cookie，避免其他测试的会话串用）。
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

    def test_github_link_on_login_and_overview(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("https://github.com/jimu2000/M_Bot", resp.text)
        self.assertIn("GitHub 免费开源项目", resp.text)
        self.assertIn('target="_blank"', resp.text)
        resp = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/overview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("https://github.com/jimu2000/M_Bot", resp.text)
        self.assertIn('target="_blank"', resp.text)

    def test_cloud_report_renders_table(self):
        resp = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/overview")
        self.assertEqual(resp.status_code, 200)
        for needle in ('id="cloud-report-card"', 'id="btn-refresh-cloud-report"',
                       'id="cloud-report-body"', 'id="cloud-report-generated"',
                       "云报告"):
            self.assertIn(needle, resp.text, f"概览页应有云报告卡片元素 {needle}")
        with open(os.path.join(ROOT, "web", "static", "js", "overview.js"),
                  encoding="utf-8") as f:
            js = f.read()
        self.assertIn('<div class="table-wrap"><table class="data cloud-report-table">', js)
        for header in ("cloud.th_region", "cloud.th_instances", "cloud.th_volumes",
                       "cloud.th_a1", "cloud.th_e2"):
            self.assertIn('tr("%s")' % header, js, f"云报告表头应走 i18n 键 {header}")
        with open(os.path.join(ROOT, "web", "static", "js", "i18n.js"),
                  encoding="utf-8") as f:
            i18n = f.read()
        for key, label in (("cloud.th_region", "区域"), ("cloud.th_instances", "实例"),
                           ("cloud.th_volumes", "启动盘"), ("cloud.th_a1", "A1"),
                           ("cloud.th_e2", "E2")):
            self.assertIn('"%s": { zh: "%s"' % (key, label), i18n,
                          f"字典里 {key} 的中文应为 {label}")
        self.assertIn("groups.map(function (g)", js, "每区域应渲染成一行")
        self.assertIn('"<tr>"', js)
        for field in ("g.instances.total", "byState.RUNNING", "byState.TERMINATED",
                      "g.volumes.count", "g.volumes.totalGB",
                      "a1.usedOcpu", "a1.capOcpu", "a1.usedMem", "a1.capMem",
                      "e2.count", "e2.limit", "g.profileLabel", "g.region"):
            self.assertIn(field, js, f"表格应取后端字段 {field}")
        self.assertIn('colspan="4"', js)
        self.assertIn("查询失败", js)
        with open(os.path.join(ROOT, "web", "static", "css", "base.css"),
                  encoding="utf-8") as f:
            css = f.read()
        self.assertIn(".table-wrap { overflow-x: auto; }", css,
                      "小屏横向滚动靠 .table-wrap")
        self.assertIn("table.data.cloud-report-table { min-width: 560px; }", css)
        self.assertIn("table.data.cloud-report-table td { white-space: nowrap; }", css)

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

    def test_api_docs_disabled(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 404, f"{path} 应 404")

    def test_login_cookie_secure_only_over_https(self):
        resp = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"})
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        self.assertNotIn("Secure", set_cookie)
        resp = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "test-password-123"},
            headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn("Secure", set_cookie)
        self.assertIn("HttpOnly", set_cookie)

    def test_422_error_hides_server_paths(self):
        resp = self.client.post("/api/auth/login", json={})
        self.assertEqual(resp.status_code, 422)
        body = resp.text
        self.assertNotIn(".py", body)
        self.assertNotIn("line ", body)
        self.assertNotIn("/opt/", body)
        self.assertNotIn("File ", body)
        self.assertIn("请求参数错误", body)


class TestMobileNav(unittest.TestCase):
    """手机端导航抽屉（v1.27.0）：≤900px 时侧边栏不再 display: none，
    而是收进抽屉，汉堡按钮可打开——否则手机上 DNS 管理等页面
    完全没有入口。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(build_test_app())
        with open(os.path.join(WEB_DIR, "static", "css", "base.css"),
                  encoding="utf-8") as f:
            cls.css = f.read()
        with open(os.path.join(WEB_DIR, "static", "js", "app.js"),
                  encoding="utf-8") as f:
            cls.js = f.read()

    def test_rendered_page_has_nav_toggle(self):
        resp = self.client.get("/overview")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="nav-toggle"', resp.text)
        self.assertIn('id="sidebar-backdrop"', resp.text)
        self.assertIn('href="/dns"', resp.text)
        self.assertIn("DNS 管理", resp.text)

    def test_login_page_has_no_nav_toggle(self):
        """登录页是独立页面，不继承 base.html，没有汉堡按钮。"""
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('id="nav-toggle"', resp.text)

    def test_sidebar_not_display_none_on_mobile(self):
        """≤900px 的媒体查询里不再有 .sidebar { display: none; }。"""
        self.assertNotIn(".sidebar { display: none; }", self.css)

    def test_drawer_css_rules(self):
        self.assertIn(".sidebar.open", self.css)
        self.assertIn("translateX(-105%)", self.css)
        self.assertIn(".sidebar-backdrop", self.css)
        self.assertIn(".nav-toggle", self.css)

    def test_app_js_wires_drawer(self):
        self.assertIn('getElementById("nav-toggle")', self.js)
        self.assertIn('getElementById("sidebar-backdrop")', self.js)
        self.assertIn("closeNavDrawer", self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
