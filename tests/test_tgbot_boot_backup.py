#!/usr/bin/env python3
"""启动卷备份机器人回调的单元测试（计划 v1.7.0）。

全部用 mock，不碰真实 OCI API。

约定（同 test_tgbot_handlers_extra.py）：
  - mock Update/Context：reply_text / edit_message_text / send_message
    用 AsyncMock；user_data / bot_data 用真实 dict。
  - 白名单：把 tgbot.config.ALLOWED_USER_IDS 临时设为 {TEST_USER_ID}。
  - oci 函数 patch tgbot.handlers_extra 命名空间；get_cli /
    get_compartment 同样 patch 本模块命名空间。
"""

import os
import sys
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_OLD_BACKUP = "ocid1.bootvolumebackup.oc1..oldxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_SUBNET = "ocid1.subnet.oc1..testxxxx"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"

FAKE_VOLUMES = [
    {"id": FAKE_BV, "display-name": "my-instance", "size-in-gbs": 50,
     "lifecycle-state": "AVAILABLE", "availability-domain": FAKE_AD},
]

FAKE_BACKUPS = [
    {"id": FAKE_OLD_BACKUP, "display-name": "old-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 47,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-20T10:00:00+00:00"},
    {"id": FAKE_BACKUP, "display-name": "new-backup",
     "boot-volume-id": FAKE_BV, "size-in-gbs": 50,
     "lifecycle-state": "AVAILABLE", "source-type": "MANUAL",
     "type": "FULL", "time-created": "2026-11-23T10:00:00+00:00"},
]

FAKE_PLAN_DEFAULTS = {
    "availability_domain": FAKE_AD,
    "subnet_id": FAKE_SUBNET,
    "ssh_keys_file": "/tmp/test-keys.pub",
}


def make_update(callback_data=None, text=None):
    """mock Update：effective_message.reply_text 与 callback_query.answer
    都是 AsyncMock；text 是用户回复的纯文字。"""
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
        update.callback_query = query
    else:
        update.callback_query = None
    return update


def make_context():
    """mock Context：user_data / bot_data 用真实 dict，bot.send_message
    用 AsyncMock。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
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


class BootBackupBotTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + mock cli/compartment。"""

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

    def tearDown(self):
        self._allowed.stop()
        self._cli.stop()
        self._compartment.stop()


class TestBackupFormatting(unittest.TestCase):
    """备份列表文案与配额满了提示文案。"""

    def test_fmt_list_full(self):
        text = he.fmt_boot_backup_list(FAKE_BACKUPS, 5, 5, True)
        self.assertIn("5/5", text)
        self.assertIn("已满", text)
        self.assertIn("old-backup", text)
        self.assertIn("new-backup", text)
        self.assertIn("2026-11-20 10:00", text)

    def test_fmt_list_empty(self):
        text = he.fmt_boot_backup_list([], 0, 5, False)
        self.assertIn("0/5", text)
        self.assertIn("暂无备份", text)

    def test_fmt_quota_full_with_oldest(self):
        text = he.fmt_backup_quota_full(5, 5, FAKE_BACKUPS[0])
        self.assertIn("免费备份已满（5/5）", text)
        self.assertIn("old-backup", text)
        self.assertIn("2026-11-20 10:00", text)
        self.assertIn("是否删除它以腾出位置", text)

    def test_fmt_quota_full_without_oldest(self):
        text = he.fmt_backup_quota_full(5, 5, None)
        self.assertIn("免费备份已满（5/5）", text)


class TestBackupKeyboards(unittest.TestCase):
    """键盘：磁盘列表加备份入口、磁盘操作加备份按钮、备份列表按钮。"""

    def test_volume_list_keyboard_has_backup_entry(self):
        markup = he.volume_list_keyboard(FAKE_VOLUMES)
        self.assertIn("bb:list", callback_datas(markup))

    def test_volume_actions_keyboard_has_backup_button(self):
        markup = he.volume_actions_keyboard(0)
        self.assertIn("v:backup:0", callback_datas(markup))

    def test_boot_backup_list_keyboard(self):
        markup = he.boot_backup_list_keyboard(FAKE_BACKUPS)
        datas = callback_datas(markup)
        self.assertIn("bb:restore:0", datas)
        self.assertIn("bb:delete:0", datas)
        self.assertIn("bb:restore:1", datas)
        self.assertIn("bb:delete:1", datas)
        self.assertIn("o:volumes", datas)

    def test_boot_backup_list_keyboard_non_available_no_restore(self):
        creating = [{**FAKE_BACKUPS[0], "lifecycle-state": "CREATING"}]
        markup = he.boot_backup_list_keyboard(creating)
        datas = callback_datas(markup)
        self.assertNotIn("bb:restore:0", datas)
        self.assertIn("bb:delete:0", datas)


class TestBootBackupsCallback(BootBackupBotTestCase):
    """回调 "bb:list"：备份列表 + 免费额度。"""

    @patch.object(he, "check_backup_quota",
                  return_value=(2, 5, False, FAKE_BACKUPS[0]))
    @patch.object(he, "list_boot_volume_backups",
                  return_value=FAKE_BACKUPS)
    async def test_list(self, mock_list, mock_quota):
        update = make_update("bb:list")
        context = make_context()
        await he.boot_backups_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("old-backup", text)
        self.assertIn("2/5", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("bb:restore:0", datas)
        # 列表已存进 user_data，后续回调按下标取回。
        self.assertEqual(context.user_data["boot_backups"], FAKE_BACKUPS)


class TestVolumeBackupCallback(BootBackupBotTestCase):
    """回调 "v:backup:<idx>"：未满直接创建，满了提示最早备份。"""

    @patch.object(he, "create_boot_volume_backup",
                  return_value=FAKE_BACKUPS[1])
    @patch.object(he, "check_backup_quota",
                  return_value=(2, 5, False, FAKE_BACKUPS[0]))
    async def test_backup_when_not_full(self, mock_quota, mock_create):
        update = make_update("v:backup:0")
        context = make_context()
        context.user_data["volumes"] = FAKE_VOLUMES
        await he.volume_backup_callback(update, context)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args[0][1], FAKE_BV)
        text = reply_text_of(update)
        self.assertIn("new-backup", text)
        self.assertIn("创建中", text)

    @patch.object(he, "create_boot_volume_backup")
    @patch.object(he, "check_backup_quota",
                  return_value=(5, 5, True, FAKE_BACKUPS[0]))
    async def test_backup_when_full_prompts_oldest(self, mock_quota,
                                                   mock_create):
        update = make_update("v:backup:0")
        context = make_context()
        context.user_data["volumes"] = FAKE_VOLUMES
        await he.volume_backup_callback(update, context)
        mock_create.assert_not_called()
        text = reply_text_of(update)
        self.assertIn("免费备份已满（5/5）", text)
        self.assertIn("old-backup", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:deloldestbb:0", datas)

    async def test_backup_volume_expired(self):
        update = make_update("v:backup:0")
        context = make_context()
        await he.volume_backup_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))


class TestBootBackupRestoreDeleteCallbacks(BootBackupBotTestCase):
    """回调 "bb:restore:<idx>" / "bb:delete:<idx>"。"""

    async def test_restore_prompts_for_name(self):
        update = make_update("bb:restore:1")
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        await he.boot_backup_restore_callback(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "restore_name", "idx": "1"})
        text = reply_text_of(update)
        self.assertIn("new-backup", text)
        self.assertIn("默认", text)

    async def test_restore_rejects_non_available(self):
        creating = [{**FAKE_BACKUPS[0], "lifecycle-state": "CREATING"}]
        update = make_update("bb:restore:0")
        context = make_context()
        context.user_data["boot_backups"] = creating
        await he.boot_backup_restore_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("CREATING", reply_text_of(update))

    async def test_delete_prompts_confirm(self):
        update = make_update("bb:delete:0")
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        await he.boot_backup_delete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("old-backup", text)
        self.assertIn("不可恢复", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:delbb:0", datas)


class TestHandleConfirmBackup(BootBackupBotTestCase):
    """handle_confirm 的 delbb / deloldestbb / restorebb。"""

    @patch.object(he, "delete_boot_volume_backup")
    async def test_delbb(self, mock_delete):
        update = make_update()
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        result = await he.handle_confirm("delbb", "0", update, context)
        self.assertTrue(result)
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args[0][1], FAKE_OLD_BACKUP)
        self.assertIn("old-backup", reply_text_of(update))

    @patch.object(he, "create_boot_volume_backup_with_limit_retry",
                  return_value=FAKE_BACKUPS[1])
    @patch.object(he, "delete_boot_volume_backup")
    @patch.object(he, "check_backup_quota",
                  return_value=(5, 5, True, FAKE_BACKUPS[0]))
    async def test_deloldestbb_deletes_first_then_creates(
            self, mock_quota, mock_delete, mock_create):
        update = make_update()
        context = make_context()
        context.user_data["volumes"] = FAKE_VOLUMES
        manager = MagicMock()
        manager.attach_mock(mock_delete, "delete")
        manager.attach_mock(mock_create, "create")
        result = await he.handle_confirm("deloldestbb", "0", update,
                                         context)
        self.assertTrue(result)
        self.assertEqual(
            manager.mock_calls,
            [call.delete(ANY, FAKE_OLD_BACKUP),
             call.create(ANY, FAKE_BV)])
        text = reply_text_of(update)
        self.assertIn("old-backup", text)
        self.assertIn("new-backup", text)

    @patch.object(he, "PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch.object(he, "restore_boot_volume_backup")
    @patch.object(he, "can_launch_batch", return_value=(False, "A1 配额不足"))
    @patch.object(he, "get_source_instance_shape",
                  return_value=("VM.Standard.A1.Flex", 2, 12))
    async def test_restorebb_can_launch_blocks(self, mock_shape, mock_can,
                                               mock_restore):
        update = make_update()
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_restore_name"] = "restored-1"
        result = await he.handle_confirm("restorebb", "1", update, context)
        self.assertTrue(result)
        self.assertIn("配额预检不通过", reply_text_of(update))
        mock_restore.assert_not_called()
        # 启动盘按备份实际大小（50GB）校验，不是默认 47GB。
        self.assertEqual(
            mock_can.call_args.kwargs.get("boot_volume_size_gbs"), 50)

    @patch.object(he, "PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch.object(he, "restore_boot_volume_backup",
                  return_value={"instance_id": FAKE_INSTANCE,
                                "boot_volume_id": FAKE_BV,
                                "shape": "VM.Standard.A1.Flex",
                                "ocpus": 2, "memory_gb": 12})
    @patch.object(he, "can_launch_batch", return_value=(True, "ok"))
    @patch.object(he, "get_source_instance_shape",
                  return_value=("VM.Standard.A1.Flex", 2, 12))
    async def test_restorebb_success(self, mock_shape, mock_can,
                                     mock_restore):
        update = make_update()
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_restore_name"] = "restored-1"
        result = await he.handle_confirm("restorebb", "1", update, context)
        self.assertTrue(result)
        kwargs = mock_restore.call_args.kwargs
        self.assertEqual(kwargs["display_name"], "restored-1")
        self.assertEqual(kwargs["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(kwargs["ocpus"], 2)
        self.assertEqual(kwargs["memory_gb"], 12)
        self.assertEqual(kwargs["availability_domain"], FAKE_AD)
        self.assertEqual(kwargs["subnet_id"], FAKE_SUBNET)
        text = reply_text_of(update)
        self.assertIn("恢复成功", text)
        self.assertIn("restored-1", text)

    @patch.object(he, "PLAN_DEFAULTS", FAKE_PLAN_DEFAULTS)
    @patch.object(he, "restore_boot_volume_backup",
                  return_value={"instance_id": FAKE_INSTANCE,
                                "boot_volume_id": FAKE_BV,
                                "shape": "VM.Standard.A1.Flex",
                                "ocpus": 1, "memory_gb": 6})
    @patch.object(he, "can_launch_batch", return_value=(True, "ok"))
    @patch.object(he, "get_source_instance_shape", return_value=None)
    async def test_restorebb_source_unknown_uses_default_a1(
            self, mock_shape, mock_can, mock_restore):
        """来源实例查不到时用默认 A1 1核/6GB。"""
        update = make_update()
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_restore_name"] = "restored-1"
        result = await he.handle_confirm("restorebb", "1", update, context)
        self.assertTrue(result)
        kwargs = mock_restore.call_args.kwargs
        self.assertEqual(kwargs["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(kwargs["ocpus"], 1)
        self.assertEqual(kwargs["memory_gb"], 6)

    async def test_restorebb_without_pending_name(self):
        update = make_update()
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        result = await he.handle_confirm("restorebb", "1", update, context)
        self.assertTrue(result)
        self.assertIn("已过期", reply_text_of(update))


class TestRestoreNameInput(BootBackupBotTestCase):
    """text_input_callback 的 "restore_name" 分支。"""

    async def test_default_name(self):
        update = make_update(text="默认")
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_input"] = {"kind": "restore_name",
                                              "idx": "1"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["pending_restore_name"],
                         "new-backup-restored")
        self.assertNotIn("pending_input", context.user_data)
        text = reply_text_of(update)
        self.assertIn("new-backup-restored", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:restorebb:1", datas)

    async def test_custom_name(self):
        update = make_update(text="my-restored")
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_input"] = {"kind": "restore_name",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["pending_restore_name"],
                         "my-restored")
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:restorebb:0", datas)

    async def test_name_too_long(self):
        update = make_update(text="x" * 256)
        context = make_context()
        context.user_data["boot_backups"] = FAKE_BACKUPS
        context.user_data["pending_input"] = {"kind": "restore_name",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertNotIn("pending_restore_name", context.user_data)
        self.assertIn("不能超过", reply_text_of(update))

    async def test_backup_expired(self):
        update = make_update(text="my-restored")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "restore_name",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))


if __name__ == "__main__":
    unittest.main()
