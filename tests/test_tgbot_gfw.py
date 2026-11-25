#!/usr/bin/env python3
"""GFW 自动换 IP（菜单 16）机器人回调的单元测试（v1.10.0）。

全部用 mock，不碰真实 OCI/Atlas。约定同 test_tgbot_boot_backup.py。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
from tgbot import gfw
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaa", "display-name": "web-1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "public-ip": "203.0.113.10"},
    {"id": "ocid1.instance.oc1..bbb", "display-name": "web-2",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "RUNNING",
     "public-ip": "203.0.113.11"},
    {"id": "ocid1.instance.oc1..ccc", "display-name": "stopped-1",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "STOPPED",
     "public-ip": None},
]


def make_update(callback_data=None, text=None):
    update = MagicMock()
    update.effective_user.id = TEST_USER_ID
    update.effective_chat.id = TEST_CHAT_ID
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.text = text
    if callback_data is not None:
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update


def make_context():
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def reply_text_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    return mock.await_args_list[-1][0][0]


def reply_markup_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    kwargs = mock.await_args_list[-1][1]
    return kwargs.get("reply_markup")


def button_texts(markup):
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


class GfwHandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._dir_patch = patch.object(gfw.bot_config, "DATA_DIR",
                                       self.tmp.name)
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)
        self._allowed_patch = patch.object(
            bot_config, "ALLOWED_USER_IDS", {TEST_USER_ID})
        self._allowed_patch.start()
        self.addCleanup(self._allowed_patch.stop)
        self._cli_patch = patch.object(he, "get_cli",
                                       return_value=MagicMock())
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)
        self._comp_patch = patch.object(he, "get_compartment",
                                        return_value="ocid1.tenancy.oc1..t")
        self._comp_patch.start()
        self.addCleanup(self._comp_patch.stop)


class TestHandleAutoip(GfwHandlerTestCase):
    def test_lists_running_instances_with_public_ip(self):
        update, context = make_update(), make_context()
        with patch.object(he, "list_instances",
                          return_value=list(FAKE_INSTANCES)), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts):
            import asyncio
            asyncio.run(he.handle_autoip(update, context))
        stored = context.user_data["gfw_instances"]
        self.assertEqual(len(stored), 2)
        texts = button_texts(reply_markup_of(update))
        self.assertTrue(any("web-1" in t and "203.0.113.10" in t
                            for t in texts))
        self.assertFalse(any("stopped-1" in t for t in texts))

    def test_menu_label_is_gfw(self):
        from tgbot.keyboards import ORACLE_MENU_ITEMS
        labels = dict(ORACLE_MENU_ITEMS)
        self.assertEqual(labels["autoip"], "16. GFW自动换IP")

    def test_no_key_hides_check_button(self):
        update, context = make_update(), make_context()
        with patch.object(he, "list_instances",
                          return_value=list(FAKE_INSTANCES)), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts):
            import asyncio
            asyncio.run(he.handle_autoip(update, context))
        texts = button_texts(reply_markup_of(update))
        self.assertNotIn("🔍 立即检查", texts)
        self.assertIn("🔑 设置 API Key", texts)
        self.assertIn("未设置", reply_text_of(update))

    def test_configured_shows_check_button_and_masked_key(self):
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        update, context = make_update(), make_context()
        with patch.object(he, "list_instances",
                          return_value=list(FAKE_INSTANCES)), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts):
            import asyncio
            asyncio.run(he.handle_autoip(update, context))
        texts = button_texts(reply_markup_of(update))
        self.assertIn("🔍 立即检查", texts)
        body = reply_text_of(update)
        self.assertIn("abcd…7890", body)
        self.assertNotIn("abcdef12-3456-7890-abcd-ef1234567890", body)

    def test_enabled_instance_shows_checkmark(self):
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        gfw.set_instance_enabled("ocid1.instance.oc1..aaa", True,
                                 profile="DEFAULT",
                                 region="ap-singapore-1",
                                 name="web-1", ip="203.0.113.10")
        update, context = make_update(), make_context()
        with patch.object(he, "list_instances",
                          return_value=list(FAKE_INSTANCES)), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts):
            import asyncio
            asyncio.run(he.handle_autoip(update, context))
        texts = button_texts(reply_markup_of(update))
        self.assertTrue(any(t.startswith("✅") and "web-1" in t
                            for t in texts))
        self.assertTrue(any(t.startswith("☐") and "web-2" in t
                            for t in texts))


class TestGfwCallback(GfwHandlerTestCase):
    def _context_with_instances(self):
        context = make_context()
        context.user_data["gfw_instances"] = FAKE_INSTANCES[:2]
        return context

    def test_toggle_enables_and_disables(self):
        import asyncio
        context = self._context_with_instances()
        with patch.object(he, "handle_autoip",
                          new=AsyncMock()) as fake_handle:
            asyncio.run(he.gfw_callback(make_update("gfw:toggle:0"),
                                        context))
            fake_handle.assert_awaited_once()
        cfg = gfw.load_gfw_config()
        self.assertIn("ocid1.instance.oc1..aaa", cfg["enabled"])
        self.assertEqual(cfg["enabled"]["ocid1.instance.oc1..aaa"]["name"],
                         "web-1")
        with patch.object(he, "handle_autoip",
                          new=AsyncMock()):
            asyncio.run(he.gfw_callback(make_update("gfw:toggle:0"),
                                        context))
        self.assertEqual(gfw.load_gfw_config()["enabled"], {})

    def test_toggle_bad_index_replies_expired(self):
        import asyncio
        context = self._context_with_instances()
        update = make_update("gfw:toggle:9")
        asyncio.run(he.gfw_callback(update, context))
        self.assertIn("已过期", reply_text_of(update))

    def test_key_prompt_sets_pending_input(self):
        import asyncio
        update, context = make_update("gfw:key"), make_context()
        with patch.object(he, "_reply_prompt",
                          new=AsyncMock()) as prompt:
            asyncio.run(he.gfw_callback(update, context))
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "gfw_key")
        prompt.assert_awaited_once()

    def test_cancel_input_clears_and_reopens(self):
        import asyncio
        context = make_context()
        context.user_data["pending_input"] = {"kind": "gfw_key"}
        with patch.object(he, "handle_autoip",
                          new=AsyncMock()) as fake_handle, \
             patch.object(he, "_delete_tracked_prompts",
                          new=AsyncMock()):
            asyncio.run(he.gfw_callback(make_update("gfw:cancel_input"),
                                        context))
        self.assertNotIn("pending_input", context.user_data)
        fake_handle.assert_awaited_once()

    def test_check_without_key_refused(self):
        import asyncio
        update, context = make_update("gfw:check"), make_context()
        asyncio.run(he.gfw_callback(update, context))
        self.assertIn("还没设置", reply_text_of(update))

    def test_check_without_enabled_refused(self):
        import asyncio
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        update, context = make_update("gfw:check"), make_context()
        asyncio.run(he.gfw_callback(update, context))
        self.assertIn("还没开启", reply_text_of(update))

    def test_check_starts_thread(self):
        import asyncio
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        gfw.set_instance_enabled("ocid1.instance.oc1..aaa", True)
        update, context = make_update("gfw:check"), make_context()
        with patch.object(he.threading, "Thread") as thread_cls:
            asyncio.run(he.gfw_callback(update, context))
        thread_cls.assert_called_once()
        thread_cls.return_value.start.assert_called_once()
        self.assertIn("已开始检查", reply_text_of(update))


class TestGfwKeyInput(GfwHandlerTestCase):
    def test_valid_key_saved_masked(self):
        import asyncio
        update, context = make_update(text="abcdef12-3456-7890-abcd-ef1234567890"), make_context()
        asyncio.run(he._handle_gfw_key_input(
            update, context, "abcdef12-3456-7890-abcd-ef1234567890"))
        self.assertEqual(
            gfw.load_gfw_config()["api_key"],
            "abcdef12-3456-7890-abcd-ef1234567890")
        body = reply_text_of(update)
        self.assertIn("abcd…7890", body)
        self.assertNotIn("abcdef12-3456-7890-abcd-ef1234567890", body)

    def test_short_key_rejected(self):
        import asyncio
        update, context = make_update(text="abc"), make_context()
        asyncio.run(he._handle_gfw_key_input(update, context, "abc"))
        self.assertIn("格式不正确", reply_text_of(update))
        self.assertEqual(gfw.load_gfw_config()["api_key"], "")

    def test_key_with_space_rejected(self):
        import asyncio
        update, context = make_update(text="abcd efgh"), make_context()
        asyncio.run(he._handle_gfw_key_input(update, context, "abcd efgh"))
        self.assertIn("格式不正确", reply_text_of(update))


class TestGfwJob(GfwHandlerTestCase):
    def test_job_skips_without_key_or_enabled(self):
        import asyncio
        context = make_context()
        with patch.object(he, "run_monitor_cycle") as cycle:
            asyncio.run(he.gfw_check_job(context))
            cycle.assert_not_called()
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        with patch.object(he, "run_monitor_cycle") as cycle:
            asyncio.run(he.gfw_check_job(context))
            cycle.assert_not_called()

    def test_job_runs_cycle_with_notify(self):
        import asyncio
        gfw.set_api_key("abcdef12-3456-7890-abcd-ef1234567890")
        gfw.set_instance_enabled("ocid1.instance.oc1..aaa", True)
        context = make_context()
        with patch.object(he, "run_monitor_cycle",
                          return_value=[]) as cycle, \
             patch.object(he, "_notify_chat_id",
                          return_value=TEST_CHAT_ID):
            asyncio.run(he.gfw_check_job(context))
        cycle.assert_called_once()
        notify = cycle.call_args[0][0]
        self.assertTrue(callable(notify))


class TestGfwDispatch(GfwHandlerTestCase):
    def test_autoip_in_dispatch(self):
        self.assertIn("autoip", he._DISPATCH)
        self.assertIs(he._DISPATCH["autoip"], he.handle_autoip)


if __name__ == "__main__":
    unittest.main()
