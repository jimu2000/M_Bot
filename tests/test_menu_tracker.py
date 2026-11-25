#!/usr/bin/env python3
"""tgbot.menu_tracker 的单元测试（v1.34.0：每聊天只留一个活动选择框）。

约定与 test_tgbot_handlers_extra.py 相同：mock Update/Context，
reply_text / send_message / delete_message 用 AsyncMock，
user_data 用真实 dict。
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from tgbot import menu_tracker

TEST_CHAT_ID = 654321


def make_update(callback_data=None, tapped_id=777, query_id="qid-1"):
    """mock Update：回调时 query.message.message_id 是被点的消息。"""
    update = MagicMock()
    update.effective_user.id = 123456
    update.effective_chat.id = TEST_CHAT_ID
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    if callback_data is not None:
        query = MagicMock()
        query.data = callback_data
        query.id = query_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = tapped_id
        update.callback_query = query
    else:
        update.callback_query = None
    return update


def make_context():
    """mock Context：user_data 用真实 dict，bot 方法用 AsyncMock。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def sent_message(message_id):
    """reply_text / send_message 返回的 mock 消息（带真实 ID）。"""
    return MagicMock(message_id=message_id)


def deleted_ids(context):
    """context.bot.delete_message 被调过的所有 message_id。"""
    return [kwargs["message_id"] for _a, kwargs
            in context.bot.delete_message.await_args_list]


class TestMenuTracker(unittest.IsolatedAsyncioTestCase):
    """menu_tracker.reply：选择框清理规则。"""

    async def test_callback_menu_deletes_tapped_and_tracks_new(self):
        """回调发新选择框：删被点的消息，跟踪新消息的 ID。"""
        update = make_update(callback_data="i:detail:0", tapped_id=777)
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        keyboard = MagicMock()
        result = await menu_tracker.reply(
            update, context, "详情", reply_markup=keyboard)
        self.assertEqual(result.message_id, 888)
        self.assertEqual(deleted_ids(context), [777])
        self.assertEqual(context.user_data["live_menu_id"], 888)
        update.effective_message.reply_text.assert_awaited_once()

    async def test_callback_menu_deletes_previous_live_menu(self):
        """回调发新选择框：之前跟踪的旧选择框一并删掉（只留一个）。"""
        update = make_update(callback_data="bb:restore:0", tapped_id=777)
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.user_data["live_menu_id"] = 666
        await menu_tracker.reply(update, context, "选备份",
                                 reply_markup=MagicMock())
        self.assertEqual(sorted(deleted_ids(context)), [666, 777])
        self.assertEqual(context.user_data["live_menu_id"], 888)

    async def test_callback_result_deletes_menu_and_clears_tracking(self):
        """回调发结果（无键盘）：删被点的消息与旧选择框，清空跟踪。"""
        update = make_update(callback_data="c:stop:0", tapped_id=777)
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.user_data["live_menu_id"] = 777
        await menu_tracker.reply(update, context, "✅ 已停止实例。")
        self.assertIn(777, deleted_ids(context))
        self.assertNotIn("live_menu_id", context.user_data)

    async def test_second_reply_in_same_callback_uses_send_message(self):
        """同一回调里第二次回复：被点的消息已删，改走 send_message。"""
        update = make_update(callback_data="o:quota", tapped_id=777,
                             query_id="qid-9")
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.bot.send_message = AsyncMock(
            return_value=sent_message(999))
        await menu_tracker.reply(update, context, "第一条")
        await menu_tracker.reply(update, context, "第二条")
        # First reply goes through the reply path, the second through send_message (the tapped message is already deleted, and replying to it raises Bad Request). 第一次走回复，第二次走 send_message（被点的消息已删，回复它会报 Bad Request）。
        update.effective_message.reply_text.assert_awaited_once()
        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.await_args
        self.assertEqual(kwargs["chat_id"], TEST_CHAT_ID)
        self.assertEqual(kwargs["text"], "第二条")
        # The tapped message is deleted only once. 被点的消息只删一次。
        self.assertEqual(deleted_ids(context), [777])

    async def test_command_menu_tracks_and_deletes_old(self):
        """命令发新选择框（无回调）：删旧跟踪，跟踪新的。"""
        update = make_update()
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.user_data["live_menu_id"] = 666
        await menu_tracker.reply(update, context, "菜单",
                                 reply_markup=MagicMock())
        self.assertEqual(deleted_ids(context), [666])
        self.assertEqual(context.user_data["live_menu_id"], 888)

    async def test_command_result_deletes_live_menu(self):
        """命令/文字发结果（无回调）：删旧选择框，清空跟踪。"""
        update = make_update()
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.user_data["live_menu_id"] = 666
        await menu_tracker.reply(update, context, "✅ 已改名。")
        self.assertEqual(deleted_ids(context), [666])
        self.assertNotIn("live_menu_id", context.user_data)

    async def test_delete_failure_does_not_break_reply(self):
        """删除失败（消息已删等）不影响回复本身。"""
        update = make_update(callback_data="i:list", tapped_id=777)
        update.effective_message.reply_text = AsyncMock(
            return_value=sent_message(888))
        context = make_context()
        context.bot.delete_message = AsyncMock(
            side_effect=RuntimeError("message not found"))
        result = await menu_tracker.reply(update, context, "列表",
                                          reply_markup=MagicMock())
        self.assertEqual(result.message_id, 888)
        self.assertEqual(context.user_data["live_menu_id"], 888)

    async def test_no_message_no_chat_returns_none(self):
        """既没有可回复的消息、也没有 chat_id 时返回 None。"""
        update = MagicMock()
        update.callback_query = None
        update.effective_message = None
        update.effective_chat = None
        context = make_context()
        result = await menu_tracker.reply(update, context, "hi")
        self.assertIsNone(result)

    async def test_forget_live_menu_clears_without_deleting(self):
        """forget_live_menu 只清跟踪，不删消息。"""
        context = make_context()
        context.user_data["live_menu_id"] = 666
        menu_tracker.forget_live_menu(context)
        self.assertNotIn("live_menu_id", context.user_data)
        context.bot.delete_message.assert_not_awaited()

    async def test_delete_live_menu_skips_except_id(self):
        """delete_live_menu 不删 except_id 那条，但照样清空跟踪。"""
        context = make_context()
        context.user_data["live_menu_id"] = 666
        await menu_tracker.delete_live_menu(context, TEST_CHAT_ID,
                                            except_id=666)
        context.bot.delete_message.assert_not_awaited()
        self.assertNotIn("live_menu_id", context.user_data)


if __name__ == "__main__":
    unittest.main()
