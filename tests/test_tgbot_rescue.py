#!/usr/bin/env python3
"""救机机器人入口测试（v2.01，mock OCI，不碰真实 API）。

运行：python3 -m unittest tests.test_tgbot_rescue -v

覆盖：文案构造（前置检查 / 孤儿盘 / 无盘方案 / Netboot / 控制台）/
菜单 44 与 dispatch 接线 / /rescue 命令 / rs: 回调（菜单、孤儿盘
列表与删除确认、前置检查、无盘方案）。
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
from tgbot import handlers_menu as hm
from tgbot.handlers import HELP_TEXT
from tgbot.keyboards import ORACLE_MENU_ITEMS
import tgbot.handlers_extra as he
from tgbot.rescue import (
    RESCUE_MENU_TEXT,
    format_console_text,
    format_diskless_plan_text,
    format_netboot_text,
    format_orphan_list_text,
    format_preflight_text,
    rescue_callback,
    rescue_command,
    rescue_menu_keyboard,
)
from oci.rescue import netboot_guide

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"


def make_update(callback_data=None):
    """mock Update：callback_query.answer / edit_message_text 是 AsyncMock。"""
    update = MagicMock()
    update.effective_user.id = TEST_USER_ID
    update.effective_chat.id = TEST_CHAT_ID
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
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
    """mock Context：user_data 用真实 dict。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot.send_message = AsyncMock()
    return context


class AllowedUserTestCase(unittest.IsolatedAsyncioTestCase):
    """白名单测试基类。"""

    def setUp(self):
        patcher = patch.object(bot_config, "ALLOWED_USER_IDS",
                               {TEST_USER_ID})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestRescueTextBuilders(unittest.TestCase):
    """纯文案构造。"""

    def test_preflight_blocked_text(self):
        result = {
            "can_do": False,
            "blockers": ["实例没有挂载启动盘，无法启动（OCI 409）。"],
            "warnings": [],
            "facts": {"instance_state": "STOPPED",
                      "has_boot_volume": False,
                      "remaining_gb": 59,
                      "is_backup_source": False},
        }
        text = format_preflight_text(result)
        self.assertIn("不能做", text)
        self.assertIn("没有挂载启动盘", text)
        self.assertIn("剩余额度：59 GB", text)

    def test_preflight_ok_text(self):
        result = {"can_do": True, "blockers": [],
                  "warnings": ["终止实例会永久删除引导卷"],
                  "facts": {"instance_state": "RUNNING",
                            "has_boot_volume": True,
                            "remaining_gb": None,
                            "is_backup_source": True,
                            "backup_count": 2}}
        text = format_preflight_text(result)
        self.assertIn("可以做", text)
        self.assertIn("2 个备份的源盘", text)

    def test_orphan_list_empty(self):
        self.assertIn("没有孤儿盘", format_orphan_list_text([], 0))

    def test_orphan_list_with_items(self):
        orphans = [{"display-name": "orphan-1", "size-in-gbs": 47}]
        text = format_orphan_list_text(orphans, 47)
        self.assertIn("1 块", text)
        self.assertIn("47 GB", text)
        self.assertIn("orphan-1", text)

    def test_diskless_plan_text_states_no_in_place_rescue(self):
        plan = {"message": "实例没有挂载启动盘，原实例无法原地救回。",
                "cost_note": "代价：新实例有新的 OCID 与新的公网 IP。",
                "can_rebuild": True,
                "backups": [{"display-name": "bk-1", "size-in-gbs": 47}]}
        text = format_diskless_plan_text(plan)
        self.assertIn("无法原地救回", text)
        self.assertIn("新的 OCID", text)
        self.assertIn("bk-1", text)

    def test_netboot_text_has_efi_and_tftp(self):
        text = format_netboot_text(netboot_guide(FAKE_INSTANCE))
        self.assertIn("FS0:", text)
        self.assertIn("203.0.113.10", text)
        self.assertIn("Alpine", text)
        self.assertIn("ESC", text)

    def test_console_text(self):
        console = {"connection-string": "ssh -o ProxyCommand=...",
                   "lifecycle-state": "ACTIVE",
                   "fingerprint": "aa:bb"}
        text = format_console_text("my-instance", console)
        self.assertIn("ssh -o ProxyCommand", text)
        self.assertIn("ACTIVE", text)

    def test_menu_keyboard_has_all_entries(self):
        keyboard = rescue_menu_keyboard()
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        for expected in ("rs:preflight", "rs:safe", "rs:diskless",
                         "rs:orphans", "rs:netboot", "rs:console",
                         "rs:conlist"):
            self.assertIn(expected, callbacks)


class TestRescueWiring(unittest.TestCase):
    """菜单 44 / dispatch / 命令菜单接线。"""

    def test_oracle_menu_has_rescue_44(self):
        labels = dict(ORACLE_MENU_ITEMS)
        self.assertEqual(labels["rescue"], "44. 救机")
        self.assertEqual(len(ORACLE_MENU_ITEMS), 45)

    def test_dispatch_has_rescue(self):
        self.assertIn("rescue", he._DISPATCH)

    def test_bot_commands_has_rescue(self):
        commands = dict(hm.BOT_COMMANDS)
        self.assertIn("rescue", commands)

    def test_help_lists_rescue(self):
        self.assertIn("/rescue", HELP_TEXT)


class TestRescueCallbacks(AllowedUserTestCase):
    """rs: 回调（mock OCI 函数）。"""

    async def test_menu_callback_edits_menu(self):
        update = make_update("rs:menu")
        context = make_context()
        await rescue_callback(update, context)
        update.callback_query.answer.assert_awaited_once()
        text = (update.callback_query.edit_message_text
                .call_args.args[0])
        self.assertEqual(text, RESCUE_MENU_TEXT)

    async def test_orphans_lists_and_stores(self):
        orphans = [{"id": FAKE_BV, "display-name": "orphan-1",
                    "size-in-gbs": 47}]
        update = make_update("rs:orphans")
        context = make_context()
        with patch("tgbot.rescue.list_rescue_orphans",
                   return_value=orphans), \
                patch("tgbot.rescue.get_cli", return_value=MagicMock()), \
                patch("tgbot.rescue.get_compartment",
                      return_value="ocid1.tenancy.oc1..test"):
            await rescue_callback(update, context)
        self.assertEqual(context.user_data["rescue_orphans"], orphans)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("orphan-1", text)
        keyboard = (update.callback_query.edit_message_text
                    .call_args.kwargs.get("reply_markup")
                    or update.callback_query.edit_message_text
                    .call_args.args[1])
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("rs:odel:0", callbacks)

    async def test_orphan_delete_confirm_then_delete(self):
        orphans = [{"id": FAKE_BV, "display-name": "orphan-1",
                    "size-in-gbs": 47}]
        context = make_context()
        context.user_data["rescue_orphans"] = orphans
        update = make_update("rs:odel:0")
        await rescue_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("确认删除", text)
        update = make_update("rs:odelok:0")
        with patch("tgbot.rescue.delete_boot_volume_guarded",
                   return_value={"deleted": True}) as mock_delete, \
                patch("tgbot.rescue.get_cli", return_value=MagicMock()), \
                patch("tgbot.rescue.get_compartment",
                      return_value="ocid1.tenancy.oc1..test"):
            await rescue_callback(update, context)
        mock_delete.assert_called_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("已删除", text)
        self.assertIn("47 GB", text)

    async def test_preflight_action_runs_check(self):
        context = make_context()
        context.user_data["rescue_instances"] = [
            {"id": FAKE_INSTANCE, "display-name": "my-instance",
             "lifecycle-state": "STOPPED"}]
        update = make_update("rs:pf:0:start")
        result = {"can_do": False,
                  "blockers": ["实例没有挂载启动盘，无法启动。"],
                  "warnings": [],
                  "facts": {"instance_state": "STOPPED",
                            "has_boot_volume": False,
                            "remaining_gb": None,
                            "is_backup_source": False}}
        with patch("tgbot.rescue.preflight_check",
                   return_value=result), \
                patch("tgbot.rescue.get_cli", return_value=MagicMock()), \
                patch("tgbot.rescue.get_compartment",
                      return_value="ocid1.tenancy.oc1..test"):
            await rescue_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("不能做", text)
        self.assertIn("没有挂载启动盘", text)

    async def test_diskless_plan_shows_no_in_place_rescue(self):
        context = make_context()
        context.user_data["rescue_instances"] = [
            {"id": FAKE_INSTANCE, "display-name": "my-instance",
             "lifecycle-state": "STOPPED"}]
        update = make_update("rs:dli:0")
        plan = {"message": "实例没有挂载启动盘，原实例无法原地救回。",
                "cost_note": "代价：新实例有新的 OCID 与新的公网 IP。",
                "can_rebuild": True,
                "backups": [{"id": FAKE_BACKUP, "display-name": "bk-1",
                             "size-in-gbs": 47}]}
        with patch("tgbot.rescue.plan_diskless_rescue",
                   return_value=plan), \
                patch("tgbot.rescue.get_cli", return_value=MagicMock()), \
                patch("tgbot.rescue.get_compartment",
                      return_value="ocid1.tenancy.oc1..test"):
            await rescue_callback(update, context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("无法原地救回", text)
        self.assertIn("新的 OCID", text)


class TestRescueCommand(AllowedUserTestCase):
    """ /rescue 命令回复救机菜单（menu_tracker.reply 经 mock）。"""

    async def test_command_replies_menu(self):
        update = make_update()
        context = make_context()
        with patch("tgbot.rescue.menu_tracker.reply",
                   new=AsyncMock()) as mock_reply:
            await rescue_command(update, context)
        mock_reply.assert_awaited_once()
        text = mock_reply.call_args.args[2]
        self.assertEqual(text, RESCUE_MENU_TEXT)


if __name__ == "__main__":
    unittest.main()
