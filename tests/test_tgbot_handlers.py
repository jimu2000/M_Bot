#!/usr/bin/env python3
"""handlers.py 的单元测试（mock OCI 调用与 Telegram 对象，不碰真实 API）。

运行：cd ~/workspace/rbot-clone && python3 -m unittest tests.test_tgbot_handlers -v
"""

import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oci.client import OCIError  # noqa: E402
from tgbot import config as bot_config  # noqa: E402
from tgbot import handlers  # noqa: E402

# 占位符（测试里不出现真实 OCID / IP；203.0.113.0/24 是文档专用网段）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..xxxxxxxx"
FAKE_INSTANCES = [
    {
        "id": "ocid1.instance.oc1..aaaa",
        "display-name": "instance-a1",
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": "RUNNING",
        "time-created": "2026-11-11T10:00:00.000Z",
    },
    {
        "id": "ocid1.instance.oc1..bbbb",
        "display-name": "instance-e2",
        "shape": "VM.Standard.E2.1.Micro",
        "lifecycle-state": "STOPPED",
        "time-created": "2026-11-12T10:00:00.000Z",
    },
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
FAKE_IP = "203.0.113.10"
ALLOWED_USER = 123
DENIED_USER = 999


def make_message_update(user_id=ALLOWED_USER):
    """命令消息的 mock Update（effective_user + effective_message）。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_message = message
    update.callback_query = None
    return update


def make_callback_update(data, user_id=ALLOWED_USER):
    """回调按钮的 mock Update（callback_query + 其所属消息）。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_message = message
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = message
    update.callback_query = query
    return update


def make_context():
    """mock Context：user_data 是真实 dict，其余字段按需 patch。"""
    context = MagicMock()
    context.user_data = {}
    return context


def _all_callback_data(keyboard):
    """取 InlineKeyboardMarkup 里全部 callback_data。"""
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


class HandlersTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + OCI 边界 mock + 假 handlers_extra 模块。"""

    def setUp(self):
        # 白名单只放 ALLOWED_USER。
        self._allowed = patch.object(
            bot_config, "ALLOWED_USER_IDS", {ALLOWED_USER})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        # get_cli / get_compartment / get_current_profile 不走真实配置。
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

        # 假 handlers_extra 模块（隔离测试：不调真实扩展模块，避免
        # 扩展逻辑的 OCI 调用漏进核心命令测试）：注入 sys.modules 并
        # 挂到 tgbot 包上，`from tgbot import handlers_extra` 才能拿到它。
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

    def _remove_extra_module(self):
        """模拟 handlers_extra 模块尚未就绪（ImportError 路径）。

        注意：只 pop sys.modules 条目不够——真实 handlers_extra.py 已落盘，
        `from tgbot import handlers_extra` 会从磁盘重新 import 成功。把条目
        置为 None，import 系统会直接抛 ImportError，才真正模拟"模块缺失"。
        """
        sys.modules["tgbot.handlers_extra"] = None
        if hasattr(self._tgbot_pkg, "handlers_extra"):
            delattr(self._tgbot_pkg, "handlers_extra")


class TestCommands(HandlersTestCase):
    """命令：/start /help /oracle /instances / 未知命令 / 白名单。"""

    async def test_start_command(self):
        update = make_message_update()
        await handlers.start_command(update, make_context())
        update.effective_message.reply_text.assert_awaited_once()
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("/oracle", text)
        keyboard = update.effective_message.reply_text.call_args.kwargs[
            "reply_markup"]
        self.assertIn("o:menu", _all_callback_data(keyboard))

    async def test_start_command_denied(self):
        update = make_message_update(user_id=DENIED_USER)
        await handlers.start_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("未授权", text)
        self.assertNotIn("reply_markup",
                         update.effective_message.reply_text.call_args.kwargs)

    async def test_callback_denied(self):
        update = make_callback_update("i:list", user_id=DENIED_USER)
        await handlers.instance_list_callback(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("未授权", text)
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_help_command(self):
        update = make_message_update()
        await handlers.help_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("/start", text)
        self.assertIn("/oracle", text)
        self.assertIn("/instances", text)

    async def test_oracle_command(self):
        update = make_message_update()
        await handlers.oracle_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("Oracle 菜单", text)
        keyboard = update.effective_message.reply_text.call_args.kwargs[
            "reply_markup"]
        data = _all_callback_data(keyboard)
        self.assertIn("o:boot", data)
        self.assertIn("o:instances", data)
        self.assertIn("o:page:1", data)

    async def test_instances_command(self):
        update = make_message_update()
        context = make_context()
        with patch("tgbot.handlers.list_instances",
                   return_value=list(FAKE_INSTANCES)) as mock_list:
            await handlers.instances_command(update, context)
        mock_list.assert_called_once_with(self.cli, FAKE_COMPARTMENT)
        self.assertEqual(context.user_data["instances"], FAKE_INSTANCES)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("instance-a1", text)
        self.assertIn("instance-e2", text)
        keyboard = update.effective_message.reply_text.call_args.kwargs[
            "reply_markup"]
        self.assertIn("i:detail:0", _all_callback_data(keyboard))

    async def test_instances_command_empty(self):
        update = make_message_update()
        with patch("tgbot.handlers.list_instances", return_value=[]):
            await handlers.instances_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("没有实例", text)

    async def test_instances_command_error(self):
        update = make_message_update()
        with patch("tgbot.handlers.list_instances",
                   side_effect=OCIError("boom")):
            await handlers.instances_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("OCI 调用失败", text)

    async def test_unknown_command(self):
        update = make_message_update()
        await handlers.unknown_command(update, make_context())
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("/help", text)


class TestOracleMenuCallback(HandlersTestCase):
    """o:* 回调：菜单 / 翻页 / 实例列表 / 转发。"""

    async def test_menu(self):
        update = make_callback_update("o:menu")
        await handlers.oracle_menu_callback(update, make_context())
        update.callback_query.answer.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("Oracle 菜单", text)

    async def test_page(self):
        update = make_callback_update("o:page:1")
        await handlers.oracle_menu_callback(update, make_context())
        keyboard = update.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"]
        data = _all_callback_data(keyboard)
        self.assertIn("o:page:0", data)   # 第 2 页有"上一页"
        self.assertIn("o:logs", data)     # 第 34 项在第 2 页

    async def test_page_invalid(self):
        update = make_callback_update("o:page:xx")
        await handlers.oracle_menu_callback(update, make_context())
        keyboard = update.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"]
        self.assertIn("o:boot", _all_callback_data(keyboard))

    async def test_instances(self):
        update = make_callback_update("o:instances")
        context = make_context()
        with patch("tgbot.handlers.list_instances",
                   return_value=list(FAKE_INSTANCES)):
            await handlers.oracle_menu_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("instance-a1", text)
        self.assertEqual(context.user_data["instances"], FAKE_INSTANCES)

    async def test_forward_quota(self):
        update = make_callback_update("o:quota")
        context = make_context()
        await handlers.oracle_menu_callback(update, context)
        self.extra.dispatch_oracle_action.assert_awaited_once_with(
            "quota", update, context)
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_forward_unimplemented(self):
        self.extra.dispatch_oracle_action.return_value = False
        update = make_callback_update("o:traffic")
        await handlers.oracle_menu_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.NOT_IMPLEMENTED_TEXT)

    async def test_forward_no_extra_module(self):
        self._remove_extra_module()
        update = make_callback_update("o:health")
        await handlers.oracle_menu_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.NOT_IMPLEMENTED_TEXT)


class TestInstanceListCallback(HandlersTestCase):
    """i:list 回调。"""

    async def test_list(self):
        update = make_callback_update("i:list")
        context = make_context()
        with patch("tgbot.handlers.list_instances",
                   return_value=list(FAKE_INSTANCES)):
            await handlers.instance_list_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("2 台", text)
        self.assertIn("instance-a1", text)

    async def test_list_empty(self):
        update = make_callback_update("i:list")
        with patch("tgbot.handlers.list_instances", return_value=[]):
            await handlers.instance_list_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("没有实例", text)

    async def test_list_error(self):
        update = make_callback_update("i:list")
        with patch("tgbot.handlers.list_instances",
                   side_effect=OCIError("boom")):
            await handlers.instance_list_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("OCI 调用失败", text)


class TestInstanceDetailCallback(HandlersTestCase):
    """i:detail:<idx> 回调。"""

    def _context_with_instances(self):
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        return context

    async def test_detail(self):
        update = make_callback_update("i:detail:0")
        with patch("tgbot.handlers.get_instance",
                   return_value=dict(FAKE_DETAIL)) as mock_get, \
             patch("tgbot.handlers.get_public_ip",
                   return_value=FAKE_IP) as mock_ip:
            await handlers.instance_detail_callback(
                update, self._context_with_instances())
        mock_get.assert_called_once_with(self.cli, "ocid1.instance.oc1..aaaa")
        mock_ip.assert_called_once_with(self.cli, "ocid1.instance.oc1..aaaa")
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("instance-a1", text)
        self.assertIn(FAKE_IP, text)
        self.assertIn("RUNNING", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"]
        data = _all_callback_data(keyboard)
        self.assertIn("i:stop:0", data)
        self.assertIn("i:terminate:0", data)

    async def test_detail_ip_failure_ignored(self):
        """查 IP 失败不影响详情展示（只省略 IP 行）。"""
        update = make_callback_update("i:detail:0")
        with patch("tgbot.handlers.get_instance",
                   return_value=dict(FAKE_DETAIL)), \
             patch("tgbot.handlers.get_public_ip",
                   side_effect=OCIError("boom")):
            await handlers.instance_detail_callback(
                update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("instance-a1", text)
        self.assertNotIn(FAKE_IP, text)

    async def test_detail_invalid_idx(self):
        update = make_callback_update("i:detail:99")
        await handlers.instance_detail_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.INSTANCE_GONE_TEXT)

    async def test_detail_no_instances_stored(self):
        update = make_callback_update("i:detail:0")
        await handlers.instance_detail_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.INSTANCE_GONE_TEXT)

    async def test_detail_error(self):
        update = make_callback_update("i:detail:0")
        with patch("tgbot.handlers.get_instance",
                   side_effect=OCIError("boom")):
            await handlers.instance_detail_callback(
                update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("OCI 调用失败", text)


class TestInstanceActionCallback(HandlersTestCase):
    """i:<action>:<idx> 回调：确认流程 / 转发。"""

    def _context_with_instances(self):
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        return context

    async def test_start_confirm(self):
        update = make_callback_update("i:start:1")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("确认启动", text)
        self.assertIn("instance-e2", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"]
        self.assertIn("c:start:1", _all_callback_data(keyboard))

    async def test_stop_confirm(self):
        update = make_callback_update("i:stop:0")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("确认停止", text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs[
            "reply_markup"]
        self.assertIn("c:stop:0", _all_callback_data(keyboard))

    async def test_restart_confirm(self):
        update = make_callback_update("i:restart:0")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("确认重启", text)

    async def test_terminate_confirm_warns_boot_volume(self):
        update = make_callback_update("i:terminate:0")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("确认终止", call.args[0])
        self.assertIn("将永久删除引导卷", call.args[0])
        self.assertEqual(call.kwargs.get("parse_mode"), "HTML")
        keyboard = call.kwargs["reply_markup"]
        self.assertIn("c:terminate:0", _all_callback_data(keyboard))

    async def test_ip_forward(self):
        update = make_callback_update("i:ip:0")
        context = self._context_with_instances()
        await handlers.instance_action_callback(update, context)
        self.assertEqual(context.user_data["pending_idx"], "0")
        # 实例操作 "ip" 转发为 "changeip"（换 IP 确认流程），不是菜单 2
        # 的 "ip" 总览。
        self.extra.dispatch_oracle_action.assert_awaited_once_with(
            "changeip", update, context)

    async def test_resize_forward(self):
        update = make_callback_update("i:resize:1")
        context = self._context_with_instances()
        await handlers.instance_action_callback(update, context)
        self.assertEqual(context.user_data["pending_idx"], "1")
        self.extra.dispatch_oracle_action.assert_awaited_once_with(
            "resize", update, context)

    async def test_ip_forward_unimplemented(self):
        self.extra.dispatch_oracle_action.return_value = False
        update = make_callback_update("i:ip:0")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.NOT_IMPLEMENTED_TEXT)

    async def test_action_invalid_idx(self):
        update = make_callback_update("i:stop:99")
        await handlers.instance_action_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.INSTANCE_GONE_TEXT)


class TestConfirmCallback(HandlersTestCase):
    """c:<action>:<idx> 回调：执行与失败。"""

    def _context_with_instances(self):
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        return context

    async def test_confirm_start_success(self):
        update = make_callback_update("c:start:1")
        with patch("tgbot.handlers.start_instance") as mock_start:
            await handlers.confirm_callback(
                update, self._context_with_instances())
        mock_start.assert_called_once_with(
            self.cli, "ocid1.instance.oc1..bbbb")
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("已启动", text)
        self.assertIn("instance-e2", text)

    async def test_confirm_stop_success(self):
        update = make_callback_update("c:stop:0")
        with patch("tgbot.handlers.stop_instance") as mock_stop:
            await handlers.confirm_callback(
                update, self._context_with_instances())
        mock_stop.assert_called_once_with(self.cli, "ocid1.instance.oc1..aaaa")
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("已停止", text)

    async def test_confirm_restart_success(self):
        update = make_callback_update("c:restart:0")
        with patch("tgbot.handlers.restart_instance") as mock_restart:
            await handlers.confirm_callback(
                update, self._context_with_instances())
        mock_restart.assert_called_once_with(
            self.cli, "ocid1.instance.oc1..aaaa")
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("已重启", text)

    async def test_confirm_terminate_success(self):
        update = make_callback_update("c:terminate:0")
        with patch("tgbot.handlers.terminate_instance") as mock_term:
            await handlers.confirm_callback(
                update, self._context_with_instances())
        # 只传 (cli, id)，靠 terminate_instance 默认值删引导卷。
        mock_term.assert_called_once_with(self.cli, "ocid1.instance.oc1..aaaa")
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("已终止", text)
        self.assertIn("引导卷已永久删除", text)

    async def test_confirm_failure(self):
        update = make_callback_update("c:start:1")
        with patch("tgbot.handlers.start_instance",
                   side_effect=OCIError("boom")):
            await handlers.confirm_callback(
                update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("OCI 调用失败", text)

    async def test_confirm_invalid_idx(self):
        update = make_callback_update("c:stop:99")
        await handlers.confirm_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.INSTANCE_GONE_TEXT)

    async def test_confirm_other_action_forward(self):
        update = make_callback_update("c:changeip:0")
        context = self._context_with_instances()
        await handlers.confirm_callback(update, context)
        self.extra.handle_confirm.assert_awaited_once_with(
            "changeip", "0", update, context)

    async def test_confirm_other_action_unimplemented(self):
        self.extra.handle_confirm.return_value = False
        update = make_callback_update("c:resize:0")
        await handlers.confirm_callback(
            update, self._context_with_instances())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, handlers.NOT_IMPLEMENTED_TEXT)


class TestCancelUnknownError(HandlersTestCase):
    """取消 / 未知命令 / 错误处理。"""

    async def test_cancel(self):
        update = make_callback_update("x")
        await handlers.cancel_callback(update, make_context())
        update.callback_query.answer.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertEqual(text, "已取消。")

    async def test_error_handler_notifies_user(self):
        update = make_message_update()
        context = make_context()
        context.error = OCIError("boom")
        await handlers.error_handler(update, context)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("OCI 调用失败", text)

    async def test_error_handler_update_none(self):
        """update 为 None 时不崩溃（只记日志）。"""
        context = make_context()
        context.error = OCIError("boom")
        await handlers.error_handler(None, context)


if __name__ == "__main__":
    unittest.main()
