#!/usr/bin/env python3
"""M_Bot v2.01 Web 改动（A 部分）测试。

覆盖：
- A1 云报告状态中文显示（overview.js + i18n.js 的状态映射表）
- A2 Web 中英文切换（lang-select、翻译字典、localStorage 持久化）
- A3 GitHub 链接补齐（2.02：登录页只留一个 GitHub 链接，改名“GitHub 免费开源项目”；顶栏图标手机/电脑可见）
- A4 版本号 2.01（VERSION、README、双语 CHANGELOG）

只读本地文件与模板渲染，不碰服务器、不写密码字面量。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def parse_i18n_dict(js):
    """从 i18n.js 的 I18N 字典里解析 {key: (zh, en)}。"""
    out = {}
    for m in re.finditer(r'"([^"]+)":\s*\{\s*zh:\s*"([^"]*)",\s*en:\s*"([^"]*)"\s*\}', js):
        out[m.group(1)] = (m.group(2), m.group(3))
    return out


class TestCloudReportChineseStates(unittest.TestCase):
    """A1：云报告实例状态中文显示。"""

    @classmethod
    def setUpClass(cls):
        cls.js = read("web/static/js/overview.js")
        cls.i18n = read("web/static/js/i18n.js")

    def test_state_map_covers_required_states(self):
        """映射表覆盖任务列出的全部状态。"""
        expected = {
            "RUNNING": "运行中",
            "TERMINATED": "已终止",
            "STOPPED": "已停止",
            "PROVISIONING": "创建中",
            "STARTING": "启动中",
            "STOPPING": "停止中",
            "TERMINATING": "终止中",
            "CREATING": "创建中",
        }
        for state, label in expected.items():
            self.assertIn('%s: "%s"' % (state, label), self.i18n,
                          f"INSTANCE_STATE_ZH 应有 {state}→{label}")

    def test_unknown_state_falls_back_to_original(self):
        """未知状态兜底显示原文（不丢状态）。"""
        self.assertIn("INSTANCE_STATE_ZH[s.toUpperCase()] || s", self.i18n)

    def test_english_mode_shows_original_codes(self):
        """英文模式下显示 OCI 原文，不翻中文。"""
        self.assertIn('if (getLang() === "en") return s;', self.i18n)

    def test_overview_uses_state_labels(self):
        """云报告表格用中文状态拼实例数，不再直出 RUNNING / TERMINATED。"""
        self.assertIn('cloudStateLabel("RUNNING")', self.js)
        self.assertIn('cloudStateLabel("TERMINATED")', self.js)
        self.assertIn("cloudStateLabel(s)", self.js)
        self.assertNotIn('"RUNNING " + (byState.RUNNING', self.js)

    def test_api_and_data_unchanged(self):
        """只改显示：后端接口与取数字段不变。"""
        self.assertIn('"/api/cloud-report/summary"', self.js)
        for field in ("byState.RUNNING", "byState.TERMINATED",
                      "g.instances.total", "g.volumes.count",
                      "g.volumes.totalGB", "a1.usedOcpu", "e2.count"):
            self.assertIn(field, self.js, f"表格仍应取后端字段 {field}")

    def test_table_headers_go_through_i18n(self):
        """表头走翻译字典，切换英文后表头也变英文。"""
        for key in ("cloud.th_region", "cloud.th_instances", "cloud.th_volumes",
                    "cloud.th_a1", "cloud.th_e2"):
            self.assertIn('tr("%s")' % key, self.js)

    def test_instances_state_badge_shows_raw_oci_code(self):
        """实例页状态徽标显示 OCI 原始状态码（STOPPED/STOPPING/RUNNING…），
        不再走中文映射（v2.21 救机实测 bug 1：中文“停止中/已停止”与面板
        其他英文状态不一致）。云报告仍走中文映射（v2.0 决定，见 overview.js）。"""
        inst = read("web/static/js/instances.js")
        self.assertNotIn("instanceStateLabel(state)", inst)
        self.assertIn('var label = s || "—";', inst)
        overview = read("web/static/js/overview.js")
        self.assertIn("instanceStateLabel(state)", overview)

    def test_cpu_model_label_a1_free_tier_and_e2_full_model(self):
        """CPU 型号显示：A1 一律显示“A1配额内免费”，E2 未检测到时回落到
        完整型号 "AMD EPYC 7J13"，不再出现光秃秃的 "AMD"；补拉与详情
        重测两处都走同一 cpuModelLabel，不绕过它直接写原始值（补拉现
        在调轻量接口 /api/instances/cpu-models 取型号字典，不再重拉列表）。"""
        inst = read("web/static/js/instances.js")
        self.assertIn("instances.cpu_a1_free", inst)
        self.assertIn('"AMD EPYC 7J13"', inst)
        self.assertNotIn('return "AMD";', inst)
        self.assertNotIn('return "ARM";', inst)
        self.assertIn("cell.textContent = cpuModelLabel(model, fresh ? fresh.shape : \"\");",
                      inst)
        self.assertIn("cell.textContent = cpuModelLabel(res.cpu_model, detailTarget.shape)",
                      inst)
        i18n = read("web/static/js/i18n.js")
        self.assertIn('"instances.cpu_a1_free": { zh: "A1配额内免费", en: "A1 free-tier" }',
                      i18n)

    def test_instances_list_uses_state_all(self):
        """实例列表拉取 state=all：STOPPED 的实例也在列表里，能点启动。

        2.02 Bug 1：后端 state=all 早就支持，但前端一直调默认
        state=running，停掉的实例从列表消失、没法从面板再启动。
        只有列表加载带 state=all；CPU 型号补拉改走轻量接口
        /api/instances/cpu-models，不再带 state=all 重拉列表。
        启停按钮文案走 i18n（v2.21 bug 1：“关机”→“停止/Stop”）。
        """
        inst = read("web/static/js/instances.js")
        self.assertEqual(inst.count("state=all"), 1,
                         "只有列表加载带 state=all；CPU 补拉走轻量接口，不重拉列表")
        self.assertIn('data-act="start"', inst)
        self.assertIn('"instances.start"', inst)
        self.assertIn('"instances.stop"', inst)
        self.assertIn('var stopped = state === "STOPPED"', inst)

    def test_create_form_checks_image_arch(self):
        """建抢机表单提交前校验镜像架构与形状（2.02 Bug 6）。

        没填自定义镜像时，下拉选项的 data-arch 对形状要求的架构
        （E2 要 x86_64、A1 要 aarch64），不匹配直接报错，
        不等 OCI 报 InvalidParameter。
        """
        create_js = read("web/static/js/create.js")
        self.assertIn('getAttribute("data-arch")', create_js)
        self.assertIn('payload.shape === SHAPE_E2 ? "x86_64" : "aarch64"',
                      create_js)
        self.assertIn("镜像架构与形状不匹配", create_js)

    def test_create_form_has_dd_toggle(self):
        """创建表单有 DD 重装开关（与机器人开机表单同步）。

        开关默认勾选（用户偏好 DD 默认开）；collectPayload 把开关
        状态发给后端（dd 字段）；开关标签与说明进中英文字典
        （字典覆盖测试会校验模板里每个 data-i18n 键都在字典里）。
        """
        html = read("web/templates/create.html")
        self.assertIn('id="dd"', html)
        self.assertIn('data-i18n="create.dd"', html)
        self.assertIn('data-i18n="create.dd_hint"', html)
        create_js = read("web/static/js/create.js")
        self.assertIn('dd: $("dd").checked', create_js)
        i18n = read("web/static/js/i18n.js")
        self.assertIn(
            '"create.dd": { zh: "DD重装（开 Debian13）", '
            'en: "DD reinstall (On Debian13)" }', i18n)
        self.assertIn('"create.dd_hint": { zh: "开启后实例重装为 Debian 13，'
                      '装完执行上方启动脚本（DD 初始化脚本）；'
                      '关闭时启动脚本作 cloud-init 开机脚本。", '
                      'en: "When on, the instance is reinstalled to Debian 13 '
                      'and the startup script above runs after DD '
                      '(DD init script); when off, the script runs as a '
                      'cloud-init boot script." }', i18n)



class TestI18nSwitch(unittest.TestCase):
    """A2：Web 中英文切换。"""

    @classmethod
    def setUpClass(cls):
        cls.i18n = read("web/static/js/i18n.js")
        cls.base = read("web/templates/base.html")
        cls.login = read("web/templates/login.html")
        cls.dict = parse_i18n_dict(cls.i18n)

    def test_lang_select_has_both_languages(self):
        """顶栏 #lang-select 有中英两项。"""
        m = re.search(r'<select id="lang-select"[^>]*>(.*?)</select>',
                      self.base, re.S)
        self.assertIsNotNone(m, "base.html 应有 #lang-select")
        self.assertIn('value="zh-CN"', m.group(1))
        self.assertIn('value="en"', m.group(1))
        self.assertIn("English", m.group(1))

    def test_login_page_has_own_switcher(self):
        """登录页没有顶栏，页内放一个切换入口，共用同一套逻辑。"""
        m = re.search(r'<select id="login-lang-select"[^>]*>(.*?)</select>',
                      self.login, re.S)
        self.assertIsNotNone(m, "login.html 应有 #login-lang-select")
        self.assertIn('value="zh-CN"', m.group(1))
        self.assertIn('value="en"', m.group(1))
        self.assertIn('src="/static/js/i18n.js"', self.login)

    def test_i18n_loaded_before_app_js(self):
        """i18n.js 在 app.js 之前加载，各页面脚本才能用 MBotI18n。"""
        for html, name in ((self.base, "base.html"), (self.login, "login.html")):
            self.assertLess(html.index('/static/js/i18n.js'),
                            html.index('/static/js/app.js'),
                            f"{name} 里 i18n.js 应在 app.js 之前")

    def test_default_language_is_chinese(self):
        """默认中文（未存过偏好时模式为固定中文，不是跟随）。"""
        self.assertIn('_mode = "zh-CN";', self.i18n)
        self.assertIn('<option value="zh-CN" selected>', self.base)

    def test_language_persisted_in_local_storage(self):
        """语言选择持久化（localStorage，刷新不丢）。"""
        self.assertIn('LANG_KEY = "mbot-lang"', self.i18n)
        self.assertIn("localStorage.getItem(LANG_KEY)", self.i18n)
        self.assertIn("localStorage.setItem(LANG_KEY, next)", self.i18n)

    def test_switch_dispatches_event_and_pages_rerender(self):
        """切换后派发事件，动态页面重渲染。"""
        self.assertIn('new CustomEvent("mbot-lang-change"', self.i18n)
        self.assertIn('addEventListener("change", function () { setLang(sel.value); })',
                      self.i18n)
        self.assertIn('addEventListener("mbot-lang-change"',
                      read("web/static/js/overview.js"))
        self.assertIn('addEventListener("mbot-lang-change"',
                      read("web/static/js/instances.js"))

    def test_dictionary_covers_all_data_i18n_keys(self):
        """模板里每个 data-i18n / placeholder / title 键字典里都有，
        且中英文文案都不为空。"""
        templates = {}
        for name in ("base.html", "login.html", "overview.html",
                     "instances.html", "create.html"):
            templates[name] = read("web/templates/" + name)
        used = set()
        for name, html in templates.items():
            for key in re.findall(r'data-i18n="([^"]+)"', html):
                used.add(key)
            for key in re.findall(r'data-i18n-placeholder="([^"]+)"', html):
                used.add(key)
            for key in re.findall(r'data-i18n-title="([^"]+)"', html):
                used.add(key)
            for key in re.findall(r'data-i18n-aria="([^"]+)"', html):
                used.add(key)
        self.assertTrue(used, "模板里应有用 data-i18n 标注的文案")
        # Plenty of keys are parsed from the dict (the regex only matches single-line entries, and dict entries are all single-line). 字典解析到的键不少（正则只认单行条目，字典条目都是单行）。
        self.assertGreaterEqual(len(self.dict), 60,
                                f"字典应至少 60 条，实际 {len(self.dict)} 条")
        missing = sorted(k for k in used if k not in self.dict)
        self.assertEqual(missing, [], f"字典缺这些键：{missing}")
        for key in sorted(used):
            zh, en = self.dict[key]
            self.assertTrue(zh.strip(), f"{key} 的中文文案为空")
            self.assertTrue(en.strip(), f"{key} 的英文文案为空")

    def test_key_pages_have_data_i18n(self):
        """主要页面（登录、概览/云报告、实例、创建、设置/导航）都有标注。"""
        for name, minimum in (("base.html", 20), ("login.html", 10),
                              ("overview.html", 8), ("instances.html", 15),
                              ("create.html", 5)):
            html = read("web/templates/" + name)
            count = len(re.findall(r"data-i18n", html))
            self.assertGreaterEqual(count, minimum,
                                    f"{name} 的 data-i18n 标注太少：{count}")

    def test_english_translations_are_not_machine_style_placeholders(self):
        """抽查几条英文翻译是准确自然的英文，不是键名原样回填。"""
        checks = {
            "nav.instances": "Instances",
            "overview.cloud_report": "Cloud Report",
            "login.submit": "Log in",
            "create.submit": "Create",
            "instances.title": "Instance Management",
            "app.logout": "Log out",
        }
        for key, en in checks.items():
            self.assertEqual(self.dict[key][1], en, f"{key} 的英文翻译应为 {en}")
            self.assertNotEqual(self.dict[key][0], self.dict[key][1],
                                f"{key} 的中英文不应相同")


class TestGithubLinks(unittest.TestCase):
    """A3：GitHub 链接补齐（2.02：登录页只留一个，改名“GitHub 免费开源项目”）。"""

    @classmethod
    def setUpClass(cls):
        cls.login = read("web/templates/login.html")
        cls.base = read("web/templates/base.html")
        cls.css = read("web/static/css/base.css")
        cls.dict = parse_i18n_dict(read("web/static/js/i18n.js"))

    def test_login_github_link_only_once_with_new_name(self):
        """登录页 GitHub 链接只出现一次、文案是新名字，新标签页打开。

        2.02：删掉了 2.01 加的“GitHub开源主页”那行（与页脚重复），
        页脚“GitHub 项目”改名“GitHub 免费开源项目”，链接地址不变。
        """
        self.assertNotIn("GitHub开源主页", self.login)
        self.assertNotIn("login.github_home", self.login)
        self.assertEqual(self.login.count(">GitHub 免费开源项目</a>"), 1)
        self.assertEqual(
            self.login.count("https://github.com/jimu2000/M_Bot"), 1)
        m = re.search(r'<a href="https://github\.com/jimu2000/M_Bot" target="_blank"[^>]*>'
                      r'[^<]*GitHub 免费开源项目</a>', self.login)
        self.assertIsNotNone(m, "GitHub 链接应是新标签页打开的新名字链接")

    def test_login_github_i18n_names(self):
        """i18n：登录页 GitHub 文案中英文都是新名字，旧键已删。"""
        self.assertNotIn("login.github_home", self.dict)
        self.assertEqual(self.dict["login.github_project"],
                         ("GitHub 免费开源项目",
                          "GitHub Free Open Source Project"))

    def test_topbar_github_icon_present(self):
        """内页顶栏 GitHub 图标（Feather 描边）在 base.html 里。"""
        self.assertIn("github-link", self.base)
        self.assertIn("https://github.com/jimu2000/M_Bot", self.base)
        self.assertIn('target="_blank"', self.base)
        # Feather outline icon: fill="none"; do not use the filled mark. Feather 描边图标：fill="none"，不要用填充版 mark。
        m = re.search(r'class="icon-btn github-link"[^>]*>\s*<svg[^>]*>',
                      self.base)
        self.assertIsNotNone(m)
        self.assertIn('fill="none"', m.group(0))

    def test_github_icon_visible_on_mobile_and_desktop(self):
        """手机端（≤900px）与电脑端 GitHub 图标都可见、不被挤掉或隐藏。"""
        # Desktop: the icon is not squeezed out by flex. 桌面端：图标不被 flex 挤掉。
        self.assertIn(".topbar .github-link { flex-shrink: 0; }", self.css)
        # Mobile: the media query explicitly keeps the icon, with no display: none hiding it. 手机端：媒体查询里明确保留图标，且没有任何 display: none 藏它。
        mobile = self.css[self.css.index("@media (max-width: 900px)"):]
        self.assertIn(".topbar .github-link { display: inline-flex;", mobile)
        self.assertNotIn(".github-link { display: none", self.css)
        self.assertNotIn(".topbar .github-link { display: none", self.css)


NODE = shutil.which("node")


def run_i18n_scenario(navigator_language, seed_storage=None, steps=None):
    """用 node + 测试桩跑真实的 i18n.js，返回观测到的结果。

    每个调用都是一个全新的 node 进程（localStorage 只含 seed_storage），
    相当于一次页面加载/刷新，可验证跟随模式的持久化。
    """
    scenario = {
        "navigatorLanguage": navigator_language,
        "seedStorage": seed_storage or {},
        "steps": steps or [],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(scenario, f)
        path = f.name
    try:
        out = subprocess.run(
            [NODE, os.path.join(ROOT, "tests", "i18n_harness.js"), path],
            capture_output=True, text=True, timeout=30, check=True)
    finally:
        os.unlink(path)
    return json.loads(out.stdout)


def scenario_values(run, op):
    return [r["value"] for r in run["results"] if r["op"] == op]


class TestFollowBrowserLanguage(unittest.TestCase):
    """A2 补：Web 语言切换第三个选项“跟随浏览器语言”（auto）。

    回落规则：跟随模式只识别中文（zh 开头）与英文（en 开头）；
    俄文、日文、法文等其他任何浏览器语言一律回落英文，面板只在
    中英之间切换，永远不会出现面板不支持的语言。
    """

    @classmethod
    def setUpClass(cls):
        cls.i18n = read("web/static/js/i18n.js")
        cls.base = read("web/templates/base.html")
        cls.login = read("web/templates/login.html")
        cls.dict = parse_i18n_dict(cls.i18n)

    def test_both_selects_have_follow_option(self):
        """两个选择器都有第三个选项“跟随浏览器语言”。"""
        for html, name in ((self.base, "base.html"), (self.login, "login.html")):
            m = re.search(r'<select id="(?:lang-select|login-lang-select)"[^>]*>'
                          r'(.*?)</select>', html, re.S)
            self.assertIsNotNone(m, f"{name} 应有语言选择器")
            self.assertIn('value="auto"', m.group(1), f"{name} 应有 auto 选项")
            self.assertIn('data-i18n="app.language_follow"', m.group(1),
                          f"{name} 的 auto 选项文案应走翻译字典")

    def test_follow_option_dictionary_entry(self):
        """跟随选项中英文文案都在字典里。"""
        self.assertEqual(self.dict.get("app.language_follow"),
                         ("跟随浏览器语言", "Follow browser language"))

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_browser_chinese_follows_chinese(self):
        """浏览器中文（zh-CN、zh-TW）→ 中文。"""
        for nav in ("zh-CN", "zh-TW", "zh-HK", "zh"):
            run = run_i18n_scenario(nav, {"mbot-lang": "auto"},
                                    [{"op": "domContentLoaded"},
                                     {"op": "getMode"}, {"op": "getLang"}])
            self.assertEqual(scenario_values(run, "getMode"), ["auto"])
            self.assertEqual(scenario_values(run, "getLang"), ["zh-CN"],
                             f"浏览器语言 {nav} 应跟随为中文")

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_browser_english_follows_english(self):
        """浏览器英文（en-US、en-GB）→ 英文。"""
        for nav in ("en-US", "en-GB", "en"):
            run = run_i18n_scenario(nav, {"mbot-lang": "auto"},
                                    [{"op": "domContentLoaded"},
                                     {"op": "getLang"}])
            self.assertEqual(scenario_values(run, "getLang"), ["en"],
                             f"浏览器语言 {nav} 应跟随为英文")

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_browser_russian_falls_back_to_english(self):
        """浏览器俄文（ru-RU）→ 回落英文，不显示面板不支持的语言。"""
        run = run_i18n_scenario("ru-RU", {"mbot-lang": "auto"},
                                [{"op": "domContentLoaded"},
                                 {"op": "getLang"}, {"op": "resolveBrowserLang"}])
        self.assertEqual(scenario_values(run, "getLang"), ["en"])
        self.assertEqual(scenario_values(run, "resolveBrowserLang"), ["en"])

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_other_languages_fall_back_to_english(self):
        """日文、法文、德文、阿拉伯文等其他语言一律回落英文。"""
        for nav in ("ja-JP", "fr-FR", "de-DE", "ar-EG", "ko-KR", "es-ES"):
            run = run_i18n_scenario(nav, {"mbot-lang": "auto"},
                                    [{"op": "domContentLoaded"},
                                     {"op": "getLang"}])
            self.assertEqual(scenario_values(run, "getLang"), ["en"],
                             f"浏览器语言 {nav} 应回落英文")

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_follow_mode_persists_across_reload(self):
        """跟随模式持久化：刷新（新进程，只带存过的 localStorage）后
        仍是跟随模式，并重新按当前浏览器语言取语言。"""
        run = run_i18n_scenario("en-US", {"mbot-lang": "auto"},
                                [{"op": "domContentLoaded"},
                                 {"op": "getMode"}, {"op": "getLang"},
                                 {"op": "stored"}])
        self.assertEqual(scenario_values(run, "getMode"), ["auto"])
        self.assertEqual(scenario_values(run, "getLang"), ["en"])
        self.assertEqual(scenario_values(run, "stored"), ["auto"])
        run = run_i18n_scenario("zh-CN", {"mbot-lang": "auto"},
                                [{"op": "domContentLoaded"},
                                 {"op": "getMode"}, {"op": "getLang"}])
        self.assertEqual(scenario_values(run, "getMode"), ["auto"])
        self.assertEqual(scenario_values(run, "getLang"), ["zh-CN"])

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_manual_choice_sticks_and_ignores_browser(self):
        """手动选中文/英文后固定，不再跟随浏览器语言。"""
        run = run_i18n_scenario("en-US", {},
                                [{"op": "domContentLoaded"},
                                 {"op": "setLang", "value": "zh-CN"},
                                 {"op": "getLang"}, {"op": "getMode"},
                                 {"op": "stored"},
                                 {"op": "changeSelect", "value": "en"},
                                 {"op": "getLang"}, {"op": "getMode"},
                                 {"op": "stored"}])
        langs = scenario_values(run, "getLang")
        self.assertEqual(langs, ["zh-CN", "en"],
                         "浏览器是英文时手动选中文应保持中文，再选英文应变英文")
        self.assertEqual(scenario_values(run, "getMode"), ["zh-CN", "en"])
        self.assertEqual(scenario_values(run, "stored"), ["zh-CN", "en"])
        run = run_i18n_scenario("zh-CN", {},
                                [{"op": "domContentLoaded"},
                                 {"op": "setLang", "value": "en"},
                                 {"op": "getLang"}, {"op": "stored"}])
        self.assertEqual(scenario_values(run, "getLang"), ["en"],
                         "浏览器是中文时手动选英文应保持英文")
        self.assertEqual(scenario_values(run, "stored"), ["en"])

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_select_shows_follow_mode(self):
        """跟随模式下选择器显示选中“跟随浏览器语言”，同时实际生效
        的语言按浏览器语言应用（选项文案本身也跟着译）。"""
        run = run_i18n_scenario("en-US", {"mbot-lang": "auto"},
                                [{"op": "domContentLoaded"},
                                 {"op": "selectValue"}, {"op": "optionText"},
                                 {"op": "htmlLang"}])
        sel = [r for r in run["results"] if r["op"] == "selectValue"][0]
        self.assertEqual(sel["topbar"], "auto")
        self.assertEqual(sel["login"], "auto")
        self.assertEqual(scenario_values(run, "optionText"),
                         ["Follow browser language"])
        self.assertEqual(scenario_values(run, "htmlLang"), ["en"])
        run = run_i18n_scenario("zh-CN", {"mbot-lang": "auto"},
                                [{"op": "domContentLoaded"},
                                 {"op": "selectValue"}, {"op": "optionText"},
                                 {"op": "htmlLang"}])
        sel = [r for r in run["results"] if r["op"] == "selectValue"][0]
        self.assertEqual(sel["topbar"], "auto")
        self.assertEqual(scenario_values(run, "optionText"), ["跟随浏览器语言"])
        self.assertEqual(scenario_values(run, "htmlLang"), ["zh-CN"])

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_switch_to_follow_and_back(self):
        """固定模式 → 跟随（俄文浏览器回落英文）→ 再切回固定中文，
        模式与生效语言都跟着变，localStorage 存的是模式本身。"""
        run = run_i18n_scenario("ru-RU", {},
                                [{"op": "domContentLoaded"},
                                 {"op": "setLang", "value": "auto"},
                                 {"op": "getMode"}, {"op": "getLang"},
                                 {"op": "stored"}, {"op": "selectValue"},
                                 {"op": "setLang", "value": "zh-CN"},
                                 {"op": "getMode"}, {"op": "getLang"},
                                 {"op": "stored"}])
        self.assertEqual(scenario_values(run, "getMode"), ["auto", "zh-CN"])
        self.assertEqual(scenario_values(run, "getLang"), ["en", "zh-CN"])
        self.assertEqual(scenario_values(run, "stored"), ["auto", "zh-CN"])

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_follow_event_carries_mode(self):
        """切到跟随模式时派发的事件带 mode=auto 与回落后的生效语言。"""
        run = run_i18n_scenario("ru-RU", {},
                                [{"op": "domContentLoaded"},
                                 {"op": "setLang", "value": "auto"},
                                 {"op": "events"}])
        evts = scenario_values(run, "events")[0]
        self.assertEqual(len(evts), 1)
        self.assertEqual(evts[0]["type"], "mbot-lang-change")
        self.assertEqual(evts[0]["detail"], {"lang": "en", "mode": "auto"})

    @unittest.skipIf(NODE is None, "本机没有 node，跑不了 JS 行为测试")
    def test_fresh_user_default_and_legacy_values(self):
        """未存过偏好仍默认固定中文；旧版存过的 zh-CN / en 照常识别。"""
        run = run_i18n_scenario("en-US", {},
                                [{"op": "domContentLoaded"},
                                 {"op": "getMode"}, {"op": "getLang"}])
        self.assertEqual(scenario_values(run, "getMode"), ["zh-CN"])
        self.assertEqual(scenario_values(run, "getLang"), ["zh-CN"])
        for saved, expected in (("en", "en"), ("zh-CN", "zh-CN")):
            run = run_i18n_scenario("ru-RU", {"mbot-lang": saved},
                                    [{"op": "domContentLoaded"},
                                     {"op": "getMode"}, {"op": "getLang"}])
            self.assertEqual(scenario_values(run, "getMode"), [saved])
            self.assertEqual(scenario_values(run, "getLang"), [expected])


class TestVersion200(unittest.TestCase):
    """A4：版本号 2.10。"""

    def test_version_file(self):
        self.assertEqual(read("VERSION").strip(), "2.25.0")

    def test_readme_top_version(self):
        readme = read("README.md")
        self.assertIn("**当前版本：v2.25.0**", readme)

    def test_changelogs_cover_v200(self):
        for rel, needles in (
            ("CHANGELOG.md", ("## [2.01]", "中英文切换", "云报告",
                              "GitHub", "救机功能")),
            ("CHANGELOG.en.md", ("## [2.01]", "language switch",
                                 "Cloud report", "GitHub", "Rescue")),
        ):
            text = read(rel)
            for needle in needles:
                self.assertIn(needle, text, f"{rel} 应提到 {needle}")

    def test_i18n_js_served(self):
        """新增的 i18n.js 能正常 serving（200）。"""
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")),
                  name="static")
        resp = TestClient(app).get("/static/js/i18n.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("MBotI18n", resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
