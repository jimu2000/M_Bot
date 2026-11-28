#!/usr/bin/env python3
"""面板主题测试：10 个主题（经典/星夜/森林/海洋/曜石/落日/樱花/薄荷/冰蓝/沙漠）。

覆盖：
- themes.css 为每个主题定义了完整的 CSS 变量集（与经典主题一致）；
- base.html 的主题选择器含 10 个选项；
- app.js 的固定明暗映射（只有经典主题跟随明暗切换）；
- i18n.js 有 10 个主题名的中英文；
- 渲染后的页面确实含新主题选项。

运行：python3 -m unittest tests.test_themes -v
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

# 主题 -> 固定明暗（None 表示跟随明暗切换，即经典主题）。
THEMES = {
    "classic": None,
    "starry": "dark",
    "forest": "dark",
    "ocean": "dark",
    "obsidian": "dark",
    "sunset": "light",
    "sakura": "light",
    "mint": "light",
    "ice": "light",
    "desert": "light",
}

# 每个主题必须定义的变量（经典亮色主题的变量集）。
REQUIRED_VARS = [
    "--bg", "--bg-soft", "--surface", "--surface-hover", "--surface-muted",
    "--border", "--border-strong", "--text", "--text-muted", "--text-faint",
    "--accent", "--accent-hover", "--accent-soft", "--accent-soft-text",
    "--on-accent", "--success", "--success-soft", "--warning", "--warning-soft",
    "--danger", "--danger-soft", "--info", "--topbar-bg", "--sidebar-bg",
    "--sidebar-active-bg", "--sidebar-active-text", "--input-bg",
    "--shadow", "--shadow-lg", "--login-bg",
]


def _read(relpath):
    with open(os.path.join(WEB_DIR, relpath), encoding="utf-8") as f:
        return f.read()


def _theme_block(css, theme):
    """取 themes.css 里某个主题的变量块（选择器行到下一个闭合花括号）。"""
    if theme == "classic":
        # 经典亮色是 :root 块；经典暗色是 html[data-theme="classic"][data-mode="dark"]。
        m = re.search(r":root,\s*\nhtml\[data-theme=\"classic\"\]\[data-mode=\"light\"\] \{(.*?)\n\}", css, re.S)
        if not m:
            return None
        return m.group(1)
    pattern = r"html\[data-theme=\"%s\"\][^{]*\{(.*?)\n\}" % re.escape(theme)
    m = re.search(pattern, css, re.S)
    return m.group(1) if m else None


class TestThemeCss(unittest.TestCase):
    def test_all_themes_defined_with_full_variables(self):
        """10 个主题都在 themes.css 里，且变量集完整。"""
        css = _read("static/css/themes.css")
        for theme in THEMES:
            block = _theme_block(css, theme)
            self.assertIsNotNone(block, f"themes.css 缺少 {theme} 主题的变量块")
            for var in REQUIRED_VARS:
                self.assertIn(var + ":", block, f"{theme} 主题缺少变量 {var}")
            # 暗色主题声明 color-scheme: dark，亮色声明 light。
            if THEMES[theme] == "dark":
                self.assertIn("color-scheme: dark", block, f"{theme} 应声明 color-scheme: dark")
            elif THEMES[theme] == "light":
                self.assertIn("color-scheme: light", block, f"{theme} 应声明 color-scheme: light")

    def test_classic_dark_variables(self):
        """经典暗色主题的变量集也完整。"""
        css = _read("static/css/themes.css")
        m = re.search(r"html\[data-theme=\"classic\"\]\[data-mode=\"dark\"\] \{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m, "themes.css 缺少经典暗色变量块")
        for var in REQUIRED_VARS:
            self.assertIn(var + ":", m.group(1), f"经典暗色主题缺少变量 {var}")

    def test_glow_themes_have_backdrop_and_z_index(self):
        """带环境光晕的主题有 body::before 光晕，且内容层压在其上。

        v2.25.4 回归：内容层抬升只抬 .shell——.topbar（fixed，z-index 50）
        与 .resourcebar（sticky，z-index 40）本来就在光晕（z-index 0）之上，
        绝不能再被一条 position: relative 规则盖掉（否则非经典主题顶栏
        不固定、资源栏不粘住）。
        """
        css = _read("static/css/themes.css")
        for theme in ("forest", "ocean", "sunset", "sakura", "obsidian", "mint", "ice", "desert"):
            self.assertIn(f'html[data-theme="{theme}"] body::before', css,
                          f"{theme} 主题缺少环境光晕")
            self.assertIn(f'html[data-theme="{theme}"] .shell', css,
                          f"{theme} 主题的内容层 z-index 规则缺失")
        for theme in ("starry", "forest", "ocean", "sunset", "sakura",
                      "obsidian", "mint", "ice", "desert"):
            self.assertNotIn(f'html[data-theme="{theme}"] .topbar', css,
                             f"{theme} 主题不应改 .topbar 的 position（顶栏须保持 fixed）")
            self.assertNotIn(f'html[data-theme="{theme}"] .resourcebar', css,
                             f"{theme} 主题不应改 .resourcebar 的 position（资源栏须保持 sticky）")


class TestThemeSelector(unittest.TestCase):
    def test_base_template_has_all_theme_options(self):
        """base.html 的主题选择器含 10 个选项，经典主题保留。"""
        html = _read("templates/base.html")
        for theme in THEMES:
            self.assertIn(f'<option value="{theme}"', html, f"主题选择器缺少 {theme} 选项")

    def test_early_apply_script_handles_fixed_themes(self):
        """防闪烁的早期应用脚本认识固定明暗主题。"""
        html = _read("templates/base.html")
        self.assertIn('fixed = { starry: "dark", forest: "dark", ocean: "dark", obsidian: "dark", sunset: "light", sakura: "light", mint: "light", ice: "light", desert: "light" }', html)


class TestThemeJs(unittest.TestCase):
    def test_fixed_theme_mode_map(self):
        """app.js 的 FIXED_THEME_MODE 只固定 9 个主题，经典不在其中。"""
        js = _read("static/js/app.js")
        m = re.search(r"var FIXED_THEME_MODE = \{([^}]*)\}", js)
        self.assertIsNotNone(m, "app.js 缺少 FIXED_THEME_MODE")
        mapping = dict(re.findall(r"(\w+): \"(dark|light)\"", m.group(1)))
        self.assertEqual(mapping, {
            "starry": "dark", "forest": "dark", "ocean": "dark", "obsidian": "dark",
            "sunset": "light", "sakura": "light", "mint": "light", "ice": "light",
            "desert": "light",
        })
        self.assertNotIn("classic", mapping, "经典主题不应固定明暗")

    def test_mode_toggle_warns_on_fixed_theme(self):
        """明暗切换按钮在固定主题上给提示，不改明暗。"""
        js = _read("static/js/app.js")
        self.assertIn('FIXED_THEME_MODE[currentTheme()]', js)
        self.assertIn("该主题固定为暗色", js)
        self.assertIn("该主题固定为亮色", js)


class TestThemeI18n(unittest.TestCase):
    def test_theme_names_bilingual(self):
        """10 个主题名在 i18n.js 里都有中英文。"""
        js = _read("static/js/i18n.js")
        for theme in THEMES:
            m = re.search(r'"app\.theme_%s":\s*\{\s*zh:\s*"([^"]+)",\s*en:\s*"([^"]+)"\s*\}' % theme, js)
            self.assertIsNotNone(m, f"i18n.js 缺少 app.theme_{theme}")
            self.assertTrue(m.group(1) and m.group(2), f"app.theme_{theme} 中英文不应为空")


class TestThemeRendered(unittest.TestCase):
    def test_overview_page_contains_new_theme_options(self):
        """渲染后的概览页确实含 10 个主题选项。"""
        from tests.test_web_pages import build_test_app
        from fastapi.testclient import TestClient
        client = TestClient(build_test_app())
        resp = client.get("/overview")
        self.assertEqual(resp.status_code, 200)
        for theme in THEMES:
            self.assertIn(f'<option value="{theme}"', resp.text, f"渲染页缺少 {theme} 选项")


if __name__ == "__main__":
    unittest.main()
