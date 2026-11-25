#!/usr/bin/env python3
"""Telegram 机器人中英文切换的单元测试（tgbot/i18n.py，v2.01）。

覆盖：
  - 字典齐全：每条都有中英两种文案且非空；/oracle 每个菜单项
    都有 menu.<key>；t() 缺键/未知语言时回退，不崩。
  - 切换持久化：按用户 ID 存 user_lang.json（0600），toggle
    来回切换，两个用户互不干扰，损坏文件时回退默认中文。
  - 同一 handler 在中英两种语言下输出对应文案：/start、/help、
    /oracle、/lang、o:lang 回调、实例列表/详情/操作确认、开机表单。
  - 菜单一致性：/lang 在 BOT_COMMANDS 里，不在 SKIPPED_COMMANDS 里，
    HELP_TEXT 列出 /lang。

全部用 mock 与临时目录，不碰真实服务器、真实 OCI 与真实数据目录。
测试里不出现真实 OCID / IP / Token / 密码：IP 用 203.0.113.0/24
文档网段，OCID 用 ocid1...fake 占位符。

运行：cd ~/workspace/rbot-clone && python3 -m unittest tests.test_tgbot_i18n -v
"""

import os
import stat
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config  # noqa: E402
from tgbot import handlers  # noqa: E402
from tgbot import i18n as bot_i18n  # noqa: E402
from tgbot import keyboards as kb  # noqa: E402
from tgbot.boot_form import (boot_form_keyboard, fmt_boot_form_summary,  # noqa: E402
                             fmt_form_text, new_form)
from tgbot.handlers_menu import BOT_COMMANDS, SKIPPED_COMMANDS  # noqa: E402
from tgbot.services import (fmt_error, fmt_instance_detail,  # noqa: E402
                            fmt_instance_list, fmt_quota_summary)
from oci.client import AuthError, OutOfCapacityError, RateLimitError  # noqa: E402

ALLOWED_USER = 123
OTHER_USER = 456
DENIED_USER = 999
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..fake"
FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaaa", "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-11T10:00:00.000Z"},
    {"id": "ocid1.instance.oc1..bbbb", "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-12T10:00:00.000Z"},
]
FAKE_DETAIL = {
    "data": {
        "id": "ocid1.instance.oc1..aaaa",
        "display-name": "instance-a1",
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": "RUNNING",
        "availability-domain": "AD-1",
        "time-created": "2026-11-11T10:00:00.000Z",
        "shape-config": {"ocpus": 1, "memory-in-gbs": 6},
    }
}
FAKE_DETAIL_E2 = {
    "data": {
        "id": "ocid1.instance.oc1..bbbb",
        "display-name": "instance-e2",
        "shape": "VM.Standard.E2.1.Micro",
        "lifecycle-state": "STOPPED",
        "availability-domain": "AD-1",
        "time-created": "2026-11-12T10:00:00.000Z",
        "shape-config": {"ocpus": 1, "memory-in-gbs": 1},
    }
}
FAKE_IP = "203.0.113.10"


def make_message_update(user_id=ALLOWED_USER):
    """命令消息的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.callback_query = None
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1001
    return update


def make_callback_update(data, user_id=ALLOWED_USER):
    """回调按钮的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_message = message
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = message
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = 1001
    return update


def make_context(lang=None):
    """mock Context：user_data 是真实 dict，可预置语言缓存。"""
    context = MagicMock()
    context.user_data = {}
    if lang is not None:
        context.user_data["lang"] = lang
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def reply_text_of(update):
    """该 update 上 reply_text 被调过的第一条文本。"""
    return update.effective_message.reply_text.call_args.args[0]


def reply_markup_of(update):
    """该 update 上 reply_text 的 reply_markup。"""
    return update.effective_message.reply_text.call_args.kwargs.get(
        "reply_markup")


def button_texts(keyboard):
    """取 InlineKeyboardMarkup 里全部按钮文字。"""
    return [btn.text for row in keyboard.inline_keyboard for btn in row]


def button_data(keyboard):
    """取 InlineKeyboardMarkup 里全部 callback_data。"""
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


# ================= 字典 =================

class TestDictionary(unittest.TestCase):

    def test_every_entry_has_zh_and_en(self):
        """每条字典都有中英两种文案，且非空。"""
        self.assertGreater(len(bot_i18n.STRINGS), 100)
        for key, entry in bot_i18n.STRINGS.items():
            with self.subTest(key=key):
                self.assertIn("zh", entry, f"{key} 缺中文")
                self.assertIn("en", entry, f"{key} 缺英文")
                self.assertTrue(entry["zh"].strip(), f"{key} 中文为空")
                self.assertTrue(entry["en"].strip(), f"{key} 英文为空")

    def test_every_oracle_menu_item_has_label(self):
        """/oracle 每个菜单项都有 menu.<key> 中英文标签。"""
        for key, zh_text in kb.ORACLE_MENU_ITEMS:
            with self.subTest(key=key):
                entry = bot_i18n.STRINGS.get("menu." + key)
                self.assertIsNotNone(entry, f"菜单项 {key} 缺字典")
                self.assertEqual(entry["zh"], zh_text)
                self.assertTrue(entry["en"].strip())

    def test_t_missing_key_returns_key(self):
        """缺键时返回 key 本身，不崩。"""
        self.assertEqual(bot_i18n.t("zh", "no.such.key"), "no.such.key")
        self.assertEqual(bot_i18n.t("en", "no.such.key"), "no.such.key")

    def test_t_unknown_lang_falls_back_to_zh(self):
        """未知语言回退中文。"""
        self.assertEqual(bot_i18n.t("fr", "common.cancel"), "已取消。")
        self.assertEqual(bot_i18n.t(None, "common.cancel"), "已取消。")

    def test_t_formats_kwargs(self):
        """带占位符的文案能 format；缺参数时返回原文，不崩。"""
        self.assertIn("instance-a1",
                      bot_i18n.t("en", "instances.confirm_start",
                                 name="instance-a1"))
        self.assertIn("{name}",
                      bot_i18n.t("en", "instances.confirm_start"))

    def test_normalize_lang(self):
        """语言值归一化：zh-CN/en-US/中文/英文都能认。"""
        self.assertEqual(bot_i18n.normalize_lang("zh-CN"), "zh")
        self.assertEqual(bot_i18n.normalize_lang("en-US"), "en")
        self.assertEqual(bot_i18n.normalize_lang("中文"), "zh")
        self.assertEqual(bot_i18n.normalize_lang("English"), "en")
        self.assertIsNone(bot_i18n.normalize_lang("ru"))
        self.assertIsNone(bot_i18n.normalize_lang(None))
        self.assertIsNone(bot_i18n.normalize_lang(123))


# ================= 持久化 =================

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "user_lang.json")

    def test_default_is_zh(self):
        """没存过的用户默认中文。"""
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER,
                                                path=self.path), "zh")

    def test_set_get_roundtrip(self):
        """设置后能读回。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en", path=self.path)
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER,
                                                path=self.path), "en")

    def test_toggle_goes_back_and_forth(self):
        """toggle 在中英之间来回切。"""
        self.assertEqual(bot_i18n.toggle_user_lang(ALLOWED_USER,
                                                   path=self.path), "en")
        self.assertEqual(bot_i18n.toggle_user_lang(ALLOWED_USER,
                                                   path=self.path), "zh")
        self.assertEqual(bot_i18n.toggle_user_lang(ALLOWED_USER,
                                                   path=self.path), "en")

    def test_users_are_independent(self):
        """两个用户的语言互不干扰。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en", path=self.path)
        self.assertEqual(bot_i18n.get_user_lang(OTHER_USER,
                                                path=self.path), "zh")
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER,
                                                path=self.path), "en")

    def test_file_mode_0600(self):
        """语言文件权限是 600。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en", path=self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_corrupt_file_falls_back_to_default(self):
        """文件损坏时读不到偏好，回退默认中文，不崩。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER,
                                                path=self.path), "zh")

    def test_invalid_lang_value_normalized(self):
        """非法语言值写进去后读出来归一化为默认中文。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"123": "ru"}')
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER,
                                                path=self.path), "zh")


class TestLangOf(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "user_lang.json")
        self._data_dir = patch.object(bot_config, "DATA_DIR",
                                      self._tmp.name)
        self._data_dir.start()
        self.addCleanup(self._data_dir.stop)

    def test_user_data_cache_wins(self):
        """user_data 里的缓存优先于文件（切换后立即生效）。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en", path=self.path)
        update = make_message_update()
        context = make_context(lang="zh")
        self.assertEqual(bot_i18n.lang_of(update, context), "zh")

    def test_file_fallback(self):
        """没有缓存时读文件。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en", path=self.path)
        update = make_message_update()
        self.assertEqual(bot_i18n.lang_of(update, make_context()), "en")

    def test_no_user_defaults_zh(self):
        """拿不到用户时默认中文。"""
        update = MagicMock()
        update.effective_user = None
        self.assertEqual(bot_i18n.lang_of(update, make_context()), "zh")
        self.assertEqual(bot_i18n.lang_of(None, make_context()), "zh")


# ================= handler 中英文输出 =================

class HandlersI18nTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + 假 handlers_extra 模块（同 test_tgbot_handlers）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._data_dir = patch.object(bot_config, "DATA_DIR",
                                      self._tmp.name)
        self._data_dir.start()
        self.addCleanup(self._data_dir.stop)
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {ALLOWED_USER})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self.cli = MagicMock()
        self._cli = patch("tgbot.handlers.get_cli",
                          return_value=self.cli)
        self._cli.start()
        self.addCleanup(self._cli.stop)
        self._compartment = patch("tgbot.handlers.get_compartment",
                                  return_value=FAKE_COMPARTMENT)
        self._compartment.start()
        self.addCleanup(self._compartment.stop)
        self._profile = patch("tgbot.handlers.get_current_profile",
                              return_value="DEFAULT")
        self._profile.start()
        self.addCleanup(self._profile.stop)
        import tgbot as tgbot_pkg
        self._tgbot_pkg = tgbot_pkg
        self.extra = types.ModuleType("tgbot.handlers_extra")
        self.extra.dispatch_oracle_action = AsyncMock(return_value=True)
        self.extra.handle_confirm = AsyncMock(return_value=True)
        sys.modules["tgbot.handlers_extra"] = self.extra
        tgbot_pkg.handlers_extra = self.extra

    def tearDown(self):
        sys.modules.pop("tgbot.handlers_extra", None)
        if hasattr(self._tgbot_pkg, "handlers_extra"):
            delattr(self._tgbot_pkg, "handlers_extra")


class TestStartHelpOracle(HandlersI18nTestCase):

    async def test_start_zh_default(self):
        """/start 默认中文：欢迎词中文，主菜单中文，带语言切换按钮。"""
        update = make_message_update()
        await handlers.start_command(update, make_context())
        text = reply_text_of(update)
        self.assertIn("你好", text)
        self.assertIn("/oracle", text)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("☁️ Oracle 菜单", texts)
        self.assertIn("🌐 Language: English", texts)
        self.assertIn("o:lang", button_data(reply_markup_of(update)))

    async def test_start_en(self):
        """/start 英文：欢迎词英文，主菜单英文，切换按钮指回中文。"""
        update = make_message_update()
        await handlers.start_command(update, make_context(lang="en"))
        text = reply_text_of(update)
        self.assertIn("Hi!", text)
        self.assertIn("/oracle", text)
        self.assertNotIn("你好", text)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("☁️ Oracle Menu", texts)
        self.assertIn("🌐 Language: 中文", texts)

    async def test_help_en(self):
        """/help 英文：命令清单英文，含 /lang。"""
        update = make_message_update()
        await handlers.help_command(update, make_context(lang="en"))
        text = reply_text_of(update)
        self.assertIn("Command list", text)
        self.assertIn("/lang", text)
        self.assertNotIn("命令清单", text)
        """/start 中文：主菜单 GitHub URL 按钮 + 欢迎词里 GitHub 链接明文，
        一键测活不在主菜单（仍在 /oracle 菜单第 15 项）。"""
        update = make_message_update()
        await handlers.start_command(update, make_context())
        text = reply_text_of(update)
        self.assertIn("GitHub 免费开源项目："
                      "https://github.com/jimu2000/M_Bot", text)
        markup = reply_markup_of(update)
        texts = button_texts(markup)
        self.assertIn("GitHub 免费开源项目", texts)
        self.assertNotIn("❤️ 一键测活", texts)
        urls = [btn.url for row in markup.inline_keyboard for btn in row]
        self.assertIn("https://github.com/jimu2000/M_Bot", urls)
        github_btn = [btn for row in markup.inline_keyboard for btn in row
                      if btn.url == "https://github.com/jimu2000/M_Bot"]
        self.assertEqual(len(github_btn), 1)
        self.assertEqual(github_btn[0].text, "GitHub 免费开源项目")

    async def test_start_github_button_and_welcome_url_en(self):
        """/start 英文：主菜单 GitHub URL 按钮 + 欢迎词里 GitHub 链接明文。"""
        update = make_message_update()
        await handlers.start_command(update, make_context(lang="en"))
        text = reply_text_of(update)
        self.assertIn("GitHub Free Open Source Project: "
                      "https://github.com/jimu2000/M_Bot", text)
        markup = reply_markup_of(update)
        texts = button_texts(markup)
        self.assertIn("GitHub Free Open Source Project", texts)
        urls = [btn.url for row in markup.inline_keyboard for btn in row]
        self.assertIn("https://github.com/jimu2000/M_Bot", urls)

    def test_health_still_in_oracle_menu(self):
        """一键测活仍可从 /oracle 菜单进入（第 15 项，o:health）。"""
        markup = kb.oracle_menu_keyboard()
        self.assertIn("o:health", button_data(markup))
        self.assertIn("15. 一键测活", button_texts(markup))

    async def test_oracle_en(self):
        """/oracle 英文：标题与菜单项英文，带语言切换按钮。"""
        update = make_message_update()
        await handlers.oracle_command(update, make_context(lang="en"))
        text = reply_text_of(update)
        self.assertIn("Oracle menu", text)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("1. Boot (Grab)", texts)
        self.assertIn("20. Check Quota", texts)
        self.assertIn("21. Running Tasks", texts)
        self.assertIn("🌐 Language: 中文", texts)
        self.assertIn("o:lang", button_data(reply_markup_of(update)))
        page2 = kb.oracle_menu_keyboard(1, lang="en")
        self.assertIn("25. Last 3 Months Cost", button_texts(page2))
        self.assertIn("44. Rescue", button_texts(page2))

    async def test_oracle_zh_keeps_chinese_labels(self):
        """/oracle 中文：标题与菜单项保持原中文。"""
        update = make_message_update()
        await handlers.oracle_command(update, make_context())
        text = reply_text_of(update)
        self.assertIn("Oracle 菜单", text)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("1. 开机(刷机)", texts)
        self.assertIn("20. 查询配额", texts)

    async def test_unknown_command_en(self):
        """未知命令英文提示。"""
        update = make_message_update()
        await handlers.unknown_command(update, make_context(lang="en"))
        self.assertIn("Unknown command", reply_text_of(update))

    async def test_cancel_en(self):
        """取消回调英文提示。"""
        update = make_callback_update("x")
        await handlers.cancel_callback(update, make_context(lang="en"))
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, "Cancelled.")


class TestLangCommand(HandlersI18nTestCase):

    async def test_lang_toggles_zh_to_en_and_persists(self):
        """/lang：中文 -> 英文，回复英文确认，偏好落盘。"""
        update = make_message_update()
        context = make_context()
        await handlers.lang_command(update, context)
        text = reply_text_of(update)
        self.assertIn("Switched to English", text)
        self.assertEqual(context.user_data.get("lang"), "en")
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER), "en")
        texts = button_texts(reply_markup_of(update))
        self.assertIn("☁️ Oracle Menu", texts)

    async def test_lang_toggles_en_to_zh(self):
        """/lang：英文 -> 中文，回复中文确认。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en")
        update = make_message_update()
        context = make_context(lang="en")
        await handlers.lang_command(update, context)
        text = reply_text_of(update)
        self.assertIn("已切换到中文", text)
        self.assertEqual(context.user_data.get("lang"), "zh")
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER), "zh")

    async def test_oracle_lang_callback_toggles_and_rerenders(self):
        """o:lang 回调：切换语言并用新语言重绘菜单。"""
        update = make_callback_update("o:lang")
        context = make_context()
        await handlers.oracle_menu_callback(update, context)
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("Oracle menu", call.args[0])
        texts = button_texts(call.kwargs["reply_markup"])
        self.assertIn("1. Boot (Grab)", texts)
        self.assertEqual(context.user_data.get("lang"), "en")
        self.assertEqual(bot_i18n.get_user_lang(ALLOWED_USER), "en")

    async def test_oracle_lang_callback_back_to_zh(self):
        """o:lang 回调第二次点：切回中文。"""
        bot_i18n.set_user_lang(ALLOWED_USER, "en")
        update = make_callback_update("o:lang")
        context = make_context(lang="en")
        await handlers.oracle_menu_callback(update, context)
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("Oracle 菜单", call.args[0])
        self.assertEqual(context.user_data.get("lang"), "zh")


class TestInstanceFlowsEn(HandlersI18nTestCase):

    async def test_instances_list_en(self):
        """/instances 英文：标题、分组数量、按钮状态词都是英文。"""
        update = make_message_update()
        context = make_context(lang="en")
        with patch("tgbot.handlers.list_instances",
                   return_value=list(FAKE_INSTANCES)), \
                patch("tgbot.handlers.attach_public_ips",
                      side_effect=lambda cli, insts: insts):
            await handlers.instances_command(update, context)
        text = reply_text_of(update)
        self.assertIn("[DEFAULT] Instances (2):", text)
        self.assertIn("🟢 Running 1 · 🟡 Stopped 1", text)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("instance-a1 · A1 · Running", texts)
        self.assertIn("instance-e2 · E2 · Stopped", texts)

    async def test_instance_detail_en(self):
        """实例详情英文：状态/规格/CPU/公网 IP 标签英文。"""
        update = make_callback_update("i:detail:0")
        context = make_context(lang="en")
        context.user_data["instances"] = list(FAKE_INSTANCES)
        with patch("tgbot.handlers.get_instance",
                   return_value=dict(FAKE_DETAIL)), \
                patch("tgbot.handlers.get_public_ip",
                      return_value=FAKE_IP), \
                patch("tgbot.handlers.get_cpu_model",
                      return_value="AMD EPYC 7J13"):
            await handlers.instance_detail_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("State：RUNNING", text)
        self.assertIn("Shape：A1.Flex（1 OCPU / 6GB）", text)
        self.assertIn("CPU model：AMD EPYC 7J13", text)
        self.assertIn("Public IP：203.0.113.10", text)
        keyboard = (update.callback_query.edit_message_text.call_args
                    .kwargs["reply_markup"])
        texts = button_texts(keyboard)
        self.assertIn("⏹️ Stop", texts)
        self.assertIn("🔁 Change IP", texts)
        self.assertIn("🗑️ Terminate Instance", texts)

    async def test_instance_detail_e2_free_forever_en(self):
        """E2 实例详情英文：规格后带 Free forever。"""
        update = make_callback_update("i:detail:1")
        context = make_context(lang="en")
        context.user_data["instances"] = list(FAKE_INSTANCES)
        with patch("tgbot.handlers.get_instance",
                   return_value=dict(FAKE_DETAIL_E2)), \
                patch("tgbot.handlers.get_public_ip",
                      return_value=None):
            await handlers.instance_detail_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("E2.1.Micro（1 OCPU / 1GB）Free forever", text)

    async def test_action_confirm_en(self):
        """启动确认英文：确认文案与确认按钮英文。"""
        update = make_callback_update("i:start:1")
        context = make_context(lang="en")
        context.user_data["instances"] = list(FAKE_INSTANCES)
        await handlers.instance_action_callback(update, context)
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("Start instance", call.args[0])
        self.assertIn("instance-e2", call.args[0])
        keyboard = call.kwargs["reply_markup"]
        self.assertIn("✅ Confirm", button_texts(keyboard))
        self.assertIn("❌ Cancel", button_texts(keyboard))

    async def test_confirm_start_success_en(self):
        """确认启动成功英文。"""
        update = make_callback_update("c:start:1")
        context = make_context(lang="en")
        context.user_data["instances"] = list(FAKE_INSTANCES)
        with patch("tgbot.handlers.start_instance", return_value=None):
            await handlers.confirm_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Instance", text)
        self.assertIn("started", text)
        self.assertIn("instance-e2", text)

    def test_fmt_error_en(self):
        """fmt_error 英文：无容量/限流/认证提示英文。"""
        self.assertIn("Out of host capacity",
                      fmt_error(OutOfCapacityError("nope"), lang="en"))
        self.assertIn("rate limited",
                      fmt_error(RateLimitError("slow"), lang="en"))
        self.assertIn("Authentication failed",
                      fmt_error(AuthError("bad"), lang="en"))

    def test_fmt_quota_summary_en(self):
        """配额汇总英文。"""
        summary = {"a1": {"ocpu_used": 1, "ocpu_limit": 4,
                          "memory_used_gb": 6, "memory_limit_gb": 24},
                   "e2": {"count": 1, "limit": 2},
                   "boot_volume": {"used_gb": 100, "limit_gb": 200},
                   "overall_ok": True}
        text = fmt_quota_summary(summary, "Singapore", lang="en")
        self.assertIn("Free tier", text)
        self.assertIn("Within limits", text)
        self.assertIn("A1.Flex: 1/4 OCPUs, 6/24GB", text)
        self.assertIn("E2.1.Micro: 1/2 instances", text)

    def test_fmt_instance_list_en_empty(self):
        """空实例列表英文。"""
        self.assertEqual(fmt_instance_list([], "Singapore", lang="en"),
                         "[Singapore] No instances in this account.")


# ================= 开机表单 =================

class TestBootFormEn(unittest.TestCase):

    def test_form_text_en(self):
        """开机表单正文英文，无中文残留。"""
        form = new_form()
        text = fmt_form_text(form, lang="en")
        self.assertIn("Boot configuration", text)
        self.assertIn("Not selected", text)
        self.assertNotIn("未选", text)
        self.assertNotIn("开机配置", text)

    def test_form_keyboard_en(self):
        """开机表单键盘英文。"""
        form = new_form()
        texts = button_texts(boot_form_keyboard(form, lang="en"))
        self.assertIn("🚀 One-click boot (default AMD)", texts)
        self.assertIn("Custom", texts)
        self.assertIn("✅ Confirm Boot", texts)
        self.assertIn("💾 Save as Quick Boot", texts)
        self.assertIn("✅ Use bot SSH key", texts)
        self.assertNotIn("自定义", texts)
        self.assertNotIn("确定开机", texts)

    def test_form_summary_en(self):
        """配置摘要确认页英文。"""
        preset = {
            "shape": "VM.Standard.A1.Flex", "ocpus": 1, "memory_gb": 6,
            "os": "ubuntu", "display_name": "ARM_2026-11-25",
            "count": 1, "boot_volume_size_gbs": None,
            "min_delay": 25, "max_delay": 25,
            "ssh_keys_file": "/root/.ssh/rbot_guard.pub",
            "root_login": True, "root_password": "abc123",
            "assign_public_ip": True, "assign_ipv6": False,
            "startup_script": None,
        }
        text = fmt_boot_form_summary(preset, True, "ok", lang="en")
        self.assertIn("Confirm boot configuration", text)
        self.assertIn("Root password", text)
        self.assertIn("Quota pre-check", text)
        self.assertNotIn("配额预检", text)

    def test_form_text_zh_unchanged(self):
        """中文表单与原来一致（默认 zh）。"""
        form = new_form()
        text = fmt_form_text(form)
        self.assertIn("🚀 开机配置", text)
        self.assertIn("未选", text)


# ================= 菜单一致性 =================

class TestMenuConsistency(unittest.TestCase):

    def test_lang_in_bot_commands(self):
        """/lang 在命令菜单里。"""
        commands = {cmd for cmd, _ in BOT_COMMANDS}
        self.assertIn("lang", commands)

    def test_lang_not_skipped(self):
        """/lang 不在跳过列表里。"""
        self.assertNotIn("lang", SKIPPED_COMMANDS)

    def test_help_lists_lang(self):
        """HELP_TEXT 列出 /lang。"""
        self.assertIn("/lang", handlers.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
