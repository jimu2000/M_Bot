#!/usr/bin/env python3
"""阶段 5 工作包 A：Telegram 新增功能的单元测试。

覆盖：批量操作（多选/动作/确认/执行汇报）、改名、重装、串行控制台、
A1 配置体检、文字输入回调、/oracle 菜单新增项。
全部用 mock，不碰真实 OCI API。

约定（与 test_tgbot_handlers_extra.py 一致）：
  - mock Update/Context：reply_text / edit_message_text / send_message
    用 AsyncMock；user_data / bot_data 用真实 dict。
  - 白名单：把 tgbot.config.ALLOWED_USER_IDS 临时设为 {TEST_USER_ID}。
  - oci 函数 patch tgbot.handlers_extra 命名空间。

运行：python3 -m unittest tests.test_phase5_tgbot -v
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
from tgbot.keyboards import (
    ORACLE_MENU_ITEMS,
    batch_action_keyboard,
    batch_select_keyboard,
    instance_actions_keyboard,
    oracle_menu_keyboard,
)
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaa1", "display-name": "web-server",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-20T00:00:00Z"},
    {"id": "ocid1.instance.oc1..bbb2", "display-name": "e2-test",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-21T00:00:00Z"},
]

FAKE_HEALTH = {
    "overall_ok": False,
    "suggestions": [
        {"severity": "error", "message": "A1 OCPU 超免费额度。"},
        {"severity": "info", "message": "实例「web-server」可考虑降配。"},
    ],
}


def make_update(callback_data=None, text=None):
    """mock Update：effective_message.reply_text 与 callback_query.answer
    都是 AsyncMock；回调时 query.edit_message_text 也是 AsyncMock。"""
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
        query.message = MagicMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update


def make_context():
    """mock Context：user_data / bot_data 用真实 dict。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def reply_text_of(update):
    """取 update 上 reply_text 最后一次调用的文本（没调过返回 None）。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def reply_markup_of(update):
    """取 update 上 reply_text 最后一次调用的 reply_markup。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[-1]
    return kwargs.get("reply_markup")


class Phase5TgbotTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、mock cli。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        self.cli = MagicMock()
        self.cli.run.return_value = {"data": []}
        self._cli_patch = patch.object(bot_config, "get_oci_client",
                                       return_value=self.cli)
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)


class TestOracleMenuItems(Phase5TgbotTestCase):
    def test_menu_has_batch_and_a1health(self):
        keys = dict(ORACLE_MENU_ITEMS)
        self.assertIn("batch", keys)
        self.assertIn("a1health", keys)
        self.assertEqual(keys["batch"], "35. 批量操作")
        self.assertEqual(keys["a1health"], "36. A1 配置体检")

    def test_menu_three_pages(self):
        # 45 items, 22 per page, 3 pages in total (two more rows per page, so page 1 reaches item 23; with item 13 deleted, 22 items land exactly on item 23; items 45-46 added in v2.14.1). 45 项，每页 22 项，共 3 页（每页加两行，第一页显示到 23 号；13 号已删，22 项正好排到 23 号；v2.14.1 新增 45/46 号）。
        keyboard = oracle_menu_keyboard(1)
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("35. 批量操作", labels)
        self.assertIn("36. A1 配置体检", labels)

    def test_menu_13_removed_top_switch_kept(self):
        # 13. profile管理 is a duplicate entry (the top row already has the switch-account button), so remove it from the menu; the other numbers stay as-is and are never renumbered. 13. profile管理 是重复入口（顶行已有切换账号按钮），从菜单里删掉；其余编号保持不动，不重新编号。
        keys = dict(ORACLE_MENU_ITEMS)
        self.assertNotIn("profiles", keys)
        self.assertNotIn("13. profile管理", keys.values())
        self.assertEqual(len(ORACLE_MENU_ITEMS), 45)
        self.assertEqual(keys["clients"], "14. 客户端管理")
        self.assertEqual(keys["traffic"], "19. 本月流量")
        self.assertEqual(keys["bark"], "43. Bark 通知设置")
        # The top-row switch button is still there, and its callback is still o:profiles. 顶行切换按钮还在，回调仍是 o:profiles。
        keyboard = oracle_menu_keyboard(0, current_account="新加坡（3 个区）")
        top = keyboard.inline_keyboard[0][0]
        self.assertIn("切换账号：新加坡（3 个区）", top.text)
        self.assertEqual(top.callback_data, "o:profiles")
        labels = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertNotIn("13. profile管理", labels)

    def test_instance_actions_keyboard_has_new_buttons(self):
        keyboard = instance_actions_keyboard(0, "RUNNING")
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("i:rename:0", callbacks)
        self.assertIn("i:reinstall:0", callbacks)
        self.assertIn("i:console:0", callbacks)

    def test_batch_keyboards(self):
        keyboard = batch_select_keyboard(FAKE_INSTANCES, {0})
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        self.assertTrue(buttons[0].text.startswith("✅"))
        self.assertTrue(buttons[1].text.startswith("☐"))
        self.assertEqual(buttons[0].callback_data, "ba:toggle:0")
        action_kb = batch_action_keyboard()
        callbacks = [b.callback_data for row in action_kb.inline_keyboard
                     for b in row]
        self.assertIn("ba:action:start", callbacks)
        self.assertIn("ba:action:terminate", callbacks)


class TestBatchFlow(Phase5TgbotTestCase):
    async def test_handle_batch_shows_select_keyboard(self):
        update = make_update(callback_data="o:batch")
        context = make_context()
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            await he.handle_batch(update, context)
        self.assertIn("批量操作", reply_text_of(update))
        self.assertEqual(context.user_data["instances"], FAKE_INSTANCES)
        self.assertEqual(context.user_data["batch_selected"], set())
        markup = reply_markup_of(update)
        self.assertIsNotNone(markup)

    async def test_handle_batch_no_instances(self):
        update = make_update(callback_data="o:batch")
        context = make_context()
        with patch.object(he, "list_instances", return_value=[]):
            await he.handle_batch(update, context)
        self.assertIn("没有实例", reply_text_of(update))

    async def test_dispatch_batch_returns_true(self):
        update = make_update(callback_data="o:batch")
        context = make_context()
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            handled = await he.dispatch_oracle_action(
                "batch", update, context)
        self.assertTrue(handled)

    async def test_toggle_selects_and_deselects(self):
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = set()
        update = make_update(callback_data="ba:toggle:0")
        await he.batch_toggle_callback(update, context)
        self.assertEqual(context.user_data["batch_selected"], {0})
        # Edit the message to refresh the keyboard (do not send a new message). 编辑消息刷新键盘（不是发新消息）。
        update.callback_query.edit_message_text.assert_awaited_once()
        update2 = make_update(callback_data="ba:toggle:0")
        await he.batch_toggle_callback(update2, context)
        self.assertEqual(context.user_data["batch_selected"], set())

    async def test_toggle_invalid_index(self):
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        update = make_update(callback_data="ba:toggle:99")
        await he.batch_toggle_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_next_without_selection_prompts(self):
        update = make_update(callback_data="ba:next")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = set()
        await he.batch_next_callback(update, context)
        self.assertIn("先选至少一台", reply_text_of(update))

    async def test_next_with_selection_shows_actions(self):
        update = make_update(callback_data="ba:next")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {0, 1}
        await he.batch_next_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("web-server", text)
        self.assertIn("e2-test", text)
        self.assertIn("选批量动作", text)

    async def test_action_terminate_shows_boot_volume_warning(self):
        update = make_update(callback_data="ba:action:terminate")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {0}
        await he.batch_action_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("批量删除", text)
        self.assertIn("永久删除", text)
        self.assertIn("引导卷", text)
        markup = reply_markup_of(update)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("c:batchterminate:0", callbacks)

    async def test_action_start_shows_confirm(self):
        update = make_update(callback_data="ba:action:start")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {1}
        await he.batch_action_callback(update, context)
        self.assertIn("批量开机", reply_text_of(update))
        markup = reply_markup_of(update)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("c:batchstart:0", callbacks)

    async def test_action_unknown(self):
        update = make_update(callback_data="ba:action:explode")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {0}
        await he.batch_action_callback(update, context)
        self.assertIn("未知", reply_text_of(update))

    async def test_confirm_batchstart_reports_per_instance(self):
        update = make_update(callback_data="c:batchstart:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {0, 1}
        fake_result = {
            "succeeded": [FAKE_INSTANCES[0]["id"]],
            "failed": [{"id": FAKE_INSTANCES[1]["id"], "error": "boom"}],
        }
        with patch.object(he, "batch_start",
                          return_value=fake_result) as mock_batch:
            handled = await he.handle_confirm(
                "batchstart", "0", update, context)
        self.assertTrue(handled)
        mock_batch.assert_called_once()
        args = mock_batch.call_args[0]
        self.assertEqual(args[1], [FAKE_INSTANCES[0]["id"],
                                   FAKE_INSTANCES[1]["id"]])
        text = reply_text_of(update)
        self.assertIn("成功 1 台", text)
        self.assertIn("失败 1 台", text)
        self.assertIn("web-server", text)
        self.assertIn("e2-test", text)
        self.assertIn("boom", text)
        # Clear the selection state after executing. 执行后清理选中状态。
        self.assertNotIn("batch_selected", context.user_data)

    async def test_confirm_batchterminate_calls_terminate(self):
        update = make_update(callback_data="c:batchterminate:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {0}
        with patch.object(
                he, "batch_terminate",
                return_value={"succeeded": [FAKE_INSTANCES[0]["id"]],
                              "failed": []}) as mock_batch:
            handled = await he.handle_confirm(
                "batchterminate", "0", update, context)
        self.assertTrue(handled)
        mock_batch.assert_called_once()

    async def test_confirm_batch_expired_selection(self):
        update = make_update(callback_data="c:batchstop:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["batch_selected"] = {99}
        with patch.object(he, "batch_stop") as mock_batch:
            handled = await he.handle_confirm(
                "batchstop", "0", update, context)
        self.assertTrue(handled)
        mock_batch.assert_not_called()
        self.assertIn("已过期", reply_text_of(update))

    def test_fmt_batch_result(self):
        text = he.fmt_batch_result(
            "开机",
            {"succeeded": ["id-1"],
             "failed": [{"id": "id-2", "error": "boom"}]},
            {"id-1": "web", "id-2": "e2"})
        self.assertIn("批量开机完成", text)
        self.assertIn("✅ web", text)
        self.assertIn("❌ e2：boom", text)


class TestRenameReinstallConsole(Phase5TgbotTestCase):
    async def test_rename_prompt_sets_pending_input(self):
        update = make_update(callback_data="i:rename:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_idx"] = "0"
        await he.handle_rename_prompt(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "rename", "idx": "0"})
        self.assertIn("web-server", reply_text_of(update))
        self.assertIn("新名字", reply_text_of(update))

    async def test_rename_prompt_no_instance(self):
        update = make_update(callback_data="i:rename:0")
        context = make_context()
        await he.handle_rename_prompt(update, context)
        self.assertIn("先在实例列表", reply_text_of(update))

    async def test_text_input_rename_shows_confirm(self):
        update = make_update(text="my-new-name")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_input"] = {"kind": "rename", "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        self.assertEqual(context.user_data["pending_rename_name"],
                         "my-new-name")
        text = reply_text_of(update)
        self.assertIn("web-server", text)
        self.assertIn("my-new-name", text)
        markup = reply_markup_of(update)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("c:rename:0", callbacks)

    async def test_text_input_rename_empty_cancels(self):
        update = make_update(text="   ")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_input"] = {"kind": "rename", "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("不能为空", reply_text_of(update))
        self.assertNotIn("pending_rename_name", context.user_data)

    async def test_text_input_rename_too_long_cancels(self):
        update = make_update(text="x" * 300)
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_input"] = {"kind": "rename", "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("255", reply_text_of(update))

    async def test_text_input_ignored_without_pending(self):
        update = make_update(text="hello")
        context = make_context()
        await he.text_input_callback(update, context)
        update.effective_message.reply_text.assert_not_awaited()

    async def test_confirm_rename(self):
        update = make_update(callback_data="c:rename:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_rename_name"] = "my-new-name"
        with patch.object(he, "rename_instance") as mock_rename:
            handled = await he.handle_confirm(
                "rename", "0", update, context)
        self.assertTrue(handled)
        mock_rename.assert_called_once()
        args = mock_rename.call_args[0]
        self.assertEqual(args[1], FAKE_INSTANCES[0]["id"])
        self.assertEqual(args[2], "my-new-name")
        self.assertIn("已改名为", reply_text_of(update))
        self.assertNotIn("pending_rename_name", context.user_data)

    async def test_confirm_rename_missing_name(self):
        update = make_update(callback_data="c:rename:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "rename_instance") as mock_rename:
            handled = await he.handle_confirm(
                "rename", "0", update, context)
        self.assertTrue(handled)
        mock_rename.assert_not_called()
        self.assertIn("已过期", reply_text_of(update))

    async def test_reinstall_prompt_sets_pending_input(self):
        update = make_update(callback_data="i:reinstall:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_idx"] = "0"
        await he.handle_reinstall_prompt(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "reinstall", "idx": "0"})
        text = reply_text_of(update)
        self.assertIn("清空", text)
        self.assertIn("web-server", text)

    async def test_text_input_reinstall_match_shows_confirm(self):
        update = make_update(text="web-server")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_input"] = {"kind": "reinstall", "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("最后确认", reply_text_of(update))
        markup = reply_markup_of(update)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("c:reinstall:0", callbacks)

    async def test_text_input_reinstall_mismatch_cancels(self):
        update = make_update(text="wrong-name")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_input"] = {"kind": "reinstall", "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("不符", reply_text_of(update))
        self.assertIn("已取消", reply_text_of(update))
        self.assertNotIn("pending_input", context.user_data)

    async def test_confirm_reinstall(self):
        update = make_update(callback_data="c:reinstall:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "reinstall_instance") as mock_reinstall:
            handled = await he.handle_confirm(
                "reinstall", "0", update, context)
        self.assertTrue(handled)
        mock_reinstall.assert_called_once()
        args = mock_reinstall.call_args[0]
        self.assertEqual(args[1], FAKE_INSTANCES[0]["id"])
        self.assertIn("重装已提交", reply_text_of(update))

    async def test_dispatch_rename_reinstall_console(self):
        for item in ("rename", "reinstall", "console"):
            with self.subTest(item=item):
                update = make_update(callback_data=f"i:{item}:0")
                context = make_context()
                context.user_data["instances"] = FAKE_INSTANCES
                context.user_data["pending_idx"] = "0"
                with patch.object(
                        he, "get_serial_console",
                        return_value={"connection-string": "ssh test",
                                      "lifecycle-state": "ACTIVE"}):
                    handled = await he.dispatch_oracle_action(
                        item, update, context)
                self.assertTrue(handled, item)
                self.assertIsNotNone(reply_text_of(update), item)

    async def test_handle_console_shows_connection_string(self):
        update = make_update(callback_data="i:console:0")
        context = make_context()
        context.user_data["instances"] = FAKE_INSTANCES
        context.user_data["pending_idx"] = "0"
        fake_console = {
            "connection-string": "ssh -i key test@instance-console",
            "fingerprint": "aa:bb:cc",
            "lifecycle-state": "ACTIVE",
        }
        with patch.object(he, "get_serial_console",
                          return_value=fake_console):
            await he.handle_console(update, context)
        text = reply_text_of(update)
        self.assertIn("ssh -i key", text)
        self.assertIn("ACTIVE", text)
        self.assertIn("aa:bb:cc", text)

    def test_fmt_serial_console_no_connection(self):
        text = he.fmt_serial_console("web-server", {})
        self.assertIn("web-server", text)
        self.assertIn("未返回连接命令", text)


class TestA1Health(Phase5TgbotTestCase):
    async def test_handle_a1_health_formats_suggestions(self):
        update = make_update(callback_data="o:a1health")
        context = make_context()
        with patch.object(he, "a1_health_check",
                          return_value=FAKE_HEALTH):
            await he.handle_a1_health(update, context)
        text = reply_text_of(update)
        self.assertIn("A1 配置体检", text)
        self.assertIn("❌", text)
        self.assertIn("💡", text)
        self.assertIn("超免费额度", text)
        self.assertIn("降配", text)

    async def test_dispatch_a1health_returns_true(self):
        update = make_update(callback_data="o:a1health")
        context = make_context()
        with patch.object(he, "a1_health_check",
                          return_value={"overall_ok": True,
                                        "suggestions": []}):
            handled = await he.dispatch_oracle_action(
                "a1health", update, context)
        self.assertTrue(handled)

    def test_fmt_a1_health_ok(self):
        text = he.fmt_a1_health(
            {"overall_ok": True,
             "suggestions": [{"severity": "ok", "message": "一切正常。"}]},
            "DEFAULT")
        self.assertIn("未发现问题", text)
        self.assertIn("✅", text)


if __name__ == "__main__":
    unittest.main()
