#!/usr/bin/env python3
"""实例详情引导卷备份 / 删除历史备份机器人回调测试（计划 v1.18.0）。

全部用 mock，不碰真实 OCI API。

约定（同 test_tgbot_boot_swap.py）：
  - mock Update/Context：reply_text 用 AsyncMock；user_data / bot_data
    用真实 dict。
  - 白名单：把 tgbot.config.ALLOWED_USER_IDS 临时设为 {TEST_USER_ID}。
  - oci 函数 patch tgbot.handlers_extra 命名空间；get_cli /
    get_compartment 同样 patch 本模块命名空间。
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
import tgbot.handlers_extra as he
from tgbot.keyboards import (
    MENU_PAGE_SIZE,
    ORACLE_MENU_ITEMS,
    instance_actions_keyboard,
    oracle_menu_keyboard,
)

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_OLD_BACKUP = "ocid1.bootvolumebackup.oc1..oldxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_ATTACHMENT_ID = "ocid1.bootvolumeattachment.oc1..testxxxx"

FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE, "display-name": "test-instance",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-20T10:00:00+00:00"},
]

FAKE_BACKUPS = [
    {"id": FAKE_OLD_BACKUP, "display-name": "old-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-20T10:00:00+00:00"},
    {"id": FAKE_BACKUP, "display-name": "new-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 50,
     "lifecycle-state": "CREATING", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-23T10:00:00+00:00"},
]

FAKE_ATTACHMENT = {"id": FAKE_ATTACHMENT_ID, "boot-volume-id": FAKE_BV,
                   "instance-id": FAKE_INSTANCE,
                   "lifecycle-state": "ATTACHED"}


def make_update(callback_data=None):
    """mock Update：effective_message.reply_text 与 callback_query.answer
    都是 AsyncMock。"""
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
        query.message = MagicMock()
        query.message.delete = AsyncMock()
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


def callback_datas(markup):
    """把 InlineKeyboardMarkup 里的 callback_data 全取出来。"""
    if markup is None:
        return []
    return [btn.callback_data
            for row in markup.inline_keyboard for btn in row]


class BootBackupInstanceTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + mock cli/compartment/attachment。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self._cli = patch.object(he, "get_cli",
                                 return_value=MagicMock())
        self._cli.start()
        self._compartment = patch.object(he, "get_compartment",
                                         return_value=TEST_COMPARTMENT)
        self._compartment.start()
        self._attachment = patch.object(
            he, "get_boot_volume_attachment",
            return_value=dict(FAKE_ATTACHMENT))
        self._attachment.start()

    def tearDown(self):
        self._allowed.stop()
        self._cli.stop()
        self._compartment.stop()
        self._attachment.stop()


class TestBootBackupKeyboard(unittest.TestCase):
    """实例操作键盘：备份与恢复同排、备份在前；删除历史备份单独一行。"""

    def test_running_has_backup_restore_row(self):
        markup = instance_actions_keyboard(0, "RUNNING")
        rows = [[b.text for b in row] for row in markup.inline_keyboard]
        datas = [[b.callback_data for b in row]
                 for row in markup.inline_keyboard]
        self.assertIn(["💾 引导卷备份", "💾 引导卷恢复"], rows)
        self.assertIn(["i:bootbackup:0", "i:bootswap:0"], datas)
        self.assertIn(["🗑️ 删除历史备份"], rows)
        self.assertIn(["i:bootbackup-del:0"], datas)

    def test_stopped_has_backup_buttons(self):
        datas = callback_datas(instance_actions_keyboard(2, "STOPPED"))
        self.assertIn("i:bootbackup:2", datas)
        self.assertIn("i:bootbackup-del:2", datas)

    def test_terminated_has_no_backup_buttons(self):
        datas = callback_datas(instance_actions_keyboard(0, "TERMINATED"))
        self.assertNotIn("i:bootbackup:0", datas)
        self.assertNotIn("i:bootbackup-del:0", datas)
        self.assertNotIn("i:bootswap:0", datas)


class TestMenuPageSize(unittest.TestCase):
    """菜单每页 22 项（用户要求 2026-11-24：每页加两行，第一页到 23 号）。"""

    def test_page_size_is_22(self):
        self.assertEqual(MENU_PAGE_SIZE, 22)

    def test_first_page_ends_at_23(self):
        texts = [b.text for row in oracle_menu_keyboard(0).inline_keyboard
                 for b in row]
        self.assertIn("23. 占用25%内存", texts)
        self.assertNotIn("24. 清除缓存和占用", texts)

    def test_two_pages_total(self):
        self.assertEqual(len(ORACLE_MENU_ITEMS), 42)
        # 第 2 页含最后一项；越界页码钳制到最后一页。
        texts = [b.text for row in oracle_menu_keyboard(1).inline_keyboard
                 for b in row]
        self.assertIn("43. Bark 通知设置", texts)
        clamped = [b.text
                   for row in oracle_menu_keyboard(5).inline_keyboard
                   for b in row]
        self.assertEqual(texts, clamped)


class TestInstanceBootBackup(BootBackupInstanceTestCase):
    """i:bootbackup：满 5 提示先删再建，未满直接创建。"""

    async def test_instance_gone(self):
        update = make_update()
        context = make_context()
        await he.instance_boot_backup_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))

    async def test_no_attachment(self):
        self._attachment.stop()
        with patch.object(he, "get_boot_volume_attachment",
                          return_value=None):
            update = make_update()
            context = make_context()
            context.user_data["pending_idx"] = "0"
            context.user_data["instances"] = FAKE_INSTANCES
            await he.instance_boot_backup_callback(update, context)
            self.assertIn("找不到", reply_text_of(update))

    async def test_free_full_prompts_delete_first(self):
        update = make_update()
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "is_paid_profile", return_value=False), \
                patch.object(he, "check_backup_quota",
                             return_value=(5, 5, True, FAKE_BACKUPS[0])), \
                patch.object(he, "create_boot_volume_backup") as create:
            await he.instance_boot_backup_callback(update, context)
            create.assert_not_called()
        text = reply_text_of(update)
        self.assertIn("已满 5/5 个", text)
        self.assertIn("先删除", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("i:bootbackup-del:0", datas)
        self.assertIn("i:detail:0", datas)

    async def test_free_not_full_creates(self):
        update = make_update()
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "is_paid_profile", return_value=False), \
                patch.object(he, "check_backup_quota",
                             return_value=(3, 5, False, None)), \
                patch.object(he, "create_boot_volume_backup",
                             return_value={"display-name": "test-backup",
                                           "id": FAKE_BACKUP}) as create, \
                patch.object(he, "start_backup_watch") as watch:
            await he.instance_boot_backup_callback(update, context)
            create.assert_called_once()
            self.assertEqual(create.call_args.args[1], FAKE_BV)
            watch.assert_called_once()
            self.assertEqual(watch.call_args[0][1], FAKE_BACKUP)
        text = reply_text_of(update)
        self.assertIn("创建中", text)
        self.assertIn("4/5", text)

    async def test_paid_skips_quota_check(self):
        update = make_update()
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "is_paid_profile", return_value=True), \
                patch.object(he, "check_backup_quota") as quota, \
                patch.object(he, "create_boot_volume_backup",
                             return_value={"display-name": "test-backup",
                                           "id": FAKE_BACKUP}), \
                patch.object(he, "start_backup_watch") as watch:
            await he.instance_boot_backup_callback(update, context)
            quota.assert_not_called()
            watch.assert_called_once()
            self.assertEqual(watch.call_args[0][1], FAKE_BACKUP)
        self.assertIn("付费账号", reply_text_of(update))


class TestInstanceBootBackupDelete(BootBackupInstanceTestCase):
    """i:bootbackup-del：列本实例备份，多选删除（ib: 前缀）。"""

    async def test_no_backups(self):
        update = make_update()
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "list_boot_volume_backups",
                          return_value=[]) as list_backups:
            await he.instance_boot_backup_delete_callback(update, context)
            list_backups.assert_called_once()
            self.assertEqual(
                list_backups.call_args.kwargs.get("boot_volume_id"),
                FAKE_BV)
        self.assertIn("还没有备份", reply_text_of(update))

    async def test_lists_backups_with_ib_keyboard(self):
        update = make_update()
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        with patch.object(he, "list_boot_volume_backups",
                          return_value=list(FAKE_BACKUPS)):
            await he.instance_boot_backup_delete_callback(update, context)
        self.assertEqual(context.user_data["ib_backups"], FAKE_BACKUPS)
        self.assertEqual(context.user_data["ib_idx"], "0")
        self.assertEqual(context.user_data["ib_ms_selected"], set())
        text = reply_text_of(update)
        self.assertIn("test-instance", text)
        self.assertIn("共 2 个", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("ib:mst:0", datas)
        self.assertIn("ib:mst:1", datas)
        self.assertIn("ib:mscancel", datas)
        self.assertNotIn("ib:msdel", datas)  # 未选时不显示删除按钮

    async def test_mstoggle(self):
        context = make_context()
        context.user_data["ib_backups"] = list(FAKE_BACKUPS)
        context.user_data["ib_ms_selected"] = set()
        context.user_data["ib_title"] = "「test-instance」的历史备份"
        update = make_update("ib:mst:1")
        await he.instance_boot_backup_mstoggle_callback(update, context)
        self.assertEqual(context.user_data["ib_ms_selected"], {1})
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        markup = edit.call_args.kwargs["reply_markup"]
        self.assertIn("ib:msdel", callback_datas(markup))
        self.assertIn("已选 1 个", edit.call_args.args[0])

    async def test_msdelete_requires_selection(self):
        context = make_context()
        context.user_data["ib_backups"] = list(FAKE_BACKUPS)
        context.user_data["ib_ms_selected"] = set()
        update = make_update("ib:msdel")
        await he.instance_boot_backup_msdelete_callback(update, context)
        update.callback_query.answer.assert_awaited_once_with(
            "还没选任何备份")

    async def test_msdelete_confirm_keyboard(self):
        context = make_context()
        context.user_data["ib_backups"] = list(FAKE_BACKUPS)
        context.user_data["ib_ms_selected"] = {0, 1}
        update = make_update("ib:msdel")
        await he.instance_boot_backup_msdelete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("即将删除 2 个备份", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:delibbsel:0,1", datas)

    async def test_mscancel_back_to_detail(self):
        context = make_context()
        context.user_data["ib_idx"] = "0"
        context.user_data["ib_ms_selected"] = {1}
        update = make_update("ib:mscancel")
        await he.instance_boot_backup_mscancel_callback(update, context)
        self.assertNotIn("ib_ms_selected", context.user_data)
        edit = update.callback_query.edit_message_text
        edit.assert_awaited_once()
        self.assertIn("已退出多选", edit.call_args.args[0])
        markup = edit.call_args.kwargs["reply_markup"]
        self.assertIn("i:detail:0", callback_datas(markup))

    async def test_confirm_delibbsel(self):
        context = make_context()
        context.user_data["ib_backups"] = list(FAKE_BACKUPS)
        context.user_data["ib_ms_selected"] = {0, 1}
        update = make_update()
        with patch.object(he, "delete_boot_volume_backup") as delete:
            handled = await he.handle_confirm("delibbsel", "0,1",
                                              update, context)
        self.assertTrue(handled)
        self.assertEqual(delete.call_count, 2)
        self.assertNotIn("ib_ms_selected", context.user_data)
        self.assertIn("已删除 2 个备份", reply_text_of(update))


class TestSwitchInPlace(BootBackupInstanceTestCase):
    """切换账号/区域：在原消息里改，不弹新框。"""

    async def test_handle_profiles_edits_in_place(self):
        update = make_update("o:profiles")
        context = make_context()
        context.user_data["profile"] = "DEFAULT"
        with patch.object(he, "get_profile_region_counts",
                          return_value={"DEFAULT": 3}), \
                patch.object(he, "get_current_profile",
                             return_value="DEFAULT"), \
                patch.object(he, "get_current_region",
                             return_value="ap-singapore-1"):
            await he.handle_profiles(update, context)
        update.callback_query.edit_message_text.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()
        text = (update.callback_query.edit_message_text
                .call_args.args[0])
        self.assertIn("当前账号", text)

    async def test_handle_profiles_falls_back_without_query(self):
        update = make_update()  # 无 callback_query（命令触发）
        context = make_context()
        context.user_data["profile"] = "DEFAULT"
        with patch.object(he, "get_profile_region_counts",
                          return_value={"DEFAULT": 3}), \
                patch.object(he, "get_current_profile",
                             return_value="DEFAULT"), \
                patch.object(he, "get_current_region",
                             return_value="ap-singapore-1"):
            await he.handle_profiles(update, context)
        self.assertIn("当前账号", reply_text_of(update))

    async def test_profile_callback_multi_region_edits_in_place(self):
        update = make_update("p:DEFAULT")
        context = make_context()
        regions = [{"region-name": "ap-singapore-1",
                    "is-home-region": True},
                   {"region-name": "ap-seoul-1",
                    "is-home-region": False}]
        with patch.object(he, "get_profile_regions",
                          return_value={"DEFAULT": regions}), \
                patch.object(he, "get_current_profile",
                             return_value="DEFAULT"), \
                patch.object(he, "get_current_region",
                             return_value="ap-singapore-1"):
            await he.profile_callback(update, context)
        update.callback_query.edit_message_text.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()
        text = (update.callback_query.edit_message_text
                .call_args.args[0])
        self.assertIn("切换工作区域", text)


if __name__ == "__main__":
    unittest.main()
