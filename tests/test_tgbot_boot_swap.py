#!/usr/bin/env python3
"""引导卷恢复机器人回调的单元测试（计划 v1.17.0）。

全部用 mock，不碰真实 OCI API。

约定（同 test_tgbot_boot_backup.py）：
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

from oci.boot_swap import BootSwapError
from tgbot import config as bot_config
import tgbot.handlers_extra as he
from tgbot.keyboards import instance_actions_keyboard

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

FAKE_BV = "ocid1.bootvolume.oc1..testxxxx"
FAKE_BACKUP = "ocid1.bootvolumebackup.oc1..testxxxx"
FAKE_OLD_BACKUP = "ocid1.bootvolumebackup.oc1..oldxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_ATTACHMENT = "ocid1.bootvolumeattachment.oc1..testxxxx"
FAKE_AD = "FAKE:AP-SINGAPORE-1-AD-1"

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

FAKE_ATTACHMENT = {"id": FAKE_ATTACHMENT, "boot-volume-id": FAKE_BV,
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


class BootSwapBotTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + mock cli/compartment + 清换盘锁。"""

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
        he._BOOTSWAP_RUNNING.clear()

    def tearDown(self):
        self._allowed.stop()
        self._cli.stop()
        self._compartment.stop()
        he._BOOTSWAP_RUNNING.clear()


class TestBootswapKeyboard(unittest.TestCase):
    """实例操作键盘：运行中/已停止有恢复按钮，已终止没有。"""

    def test_running_has_bootswap_button(self):
        datas = callback_datas(instance_actions_keyboard(0, "RUNNING"))
        self.assertIn("i:bootswap:0", datas)

    def test_stopped_has_bootswap_button(self):
        datas = callback_datas(instance_actions_keyboard(2, "STOPPED"))
        self.assertIn("i:bootswap:2", datas)

    def test_terminated_no_bootswap_button(self):
        datas = callback_datas(instance_actions_keyboard(0, "TERMINATED"))
        self.assertNotIn("i:bootswap:0", datas)


class TestHandleBootswap(BootSwapBotTestCase):
    """handle_bootswap：备份选择列表。"""

    async def test_no_instance(self):
        update = make_update("i:bootswap:0")
        context = make_context()
        await he.handle_bootswap(update, context)
        self.assertIn("先在实例列表里选一台实例", reply_text_of(update))

    async def test_terminated_instance(self):
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = [
            dict(FAKE_INSTANCES[0], **{"lifecycle-state": "TERMINATED"})]
        await he.handle_bootswap(update, context)
        self.assertIn("只有运行中或已停止的实例才能恢复",
                      reply_text_of(update))

    @patch.object(he, "get_boot_volume_attachment", return_value=None)
    async def test_no_attachment(self, _mock_att):
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.handle_bootswap(update, context)
        self.assertIn("找不到", reply_text_of(update))

    @patch.object(he, "list_boot_volume_backups", return_value=[])
    @patch.object(he, "get_boot_volume_attachment",
                  return_value=FAKE_ATTACHMENT)
    async def test_no_backups(self, _mock_att, _mock_list):
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.handle_bootswap(update, context)
        self.assertIn("还没有可用备份", reply_text_of(update))

    @patch.object(he, "list_boot_volume_backups",
                  side_effect=[[], FAKE_BACKUPS])
    @patch.object(he, "get_boot_volume_attachment",
                  return_value=FAKE_ATTACHMENT)
    async def test_no_backups_mentions_other_backups(self, _mock_att,
                                                     _mock_list):
        """当前盘没备份、账号里还有别的盘的备份时，提示说明旧备份还在。"""
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.handle_bootswap(update, context)
        text = reply_text_of(update)
        self.assertIn("还没有可用备份", text)
        self.assertIn("还有 1 个其他启动盘的可用备份", text)
        self.assertIn("删盘不会删备份", text)
        self.assertIn("原地恢复只列当前启动盘的备份", text)

    @patch.object(he, "list_boot_volume_backups",
                  side_effect=[[], Exception("boom")])
    @patch.object(he, "get_boot_volume_attachment",
                  return_value=FAKE_ATTACHMENT)
    async def test_no_backups_other_lookup_failure_ignored(
            self, _mock_att, _mock_list):
        """查旧备份失败时不影响主提示（只显示原来的两句）。"""
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.handle_bootswap(update, context)
        text = reply_text_of(update)
        self.assertIn("还没有可用备份", text)
        self.assertNotIn("删盘不会删备份", text)
        self.assertNotIn("其他启动盘", text)

    @patch.object(he, "list_boot_volume_backups",
                  return_value=FAKE_BACKUPS)
    @patch.object(he, "get_boot_volume_attachment",
                  return_value=FAKE_ATTACHMENT)
    async def test_lists_only_available(self, mock_att, mock_list):
        update = make_update("i:bootswap:0")
        context = make_context()
        context.user_data["pending_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.handle_bootswap(update, context)
        # 只列 AVAILABLE 的那条（CREATING 的不出现）。
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("bs:pick:0", datas)
        self.assertNotIn("bs:pick:1", datas)
        self.assertEqual(len(context.user_data["bootswap_backups"]), 1)
        self.assertEqual(context.user_data["bootswap_idx"], "0")
        # 按当前启动盘过滤备份。
        self.assertEqual(mock_list.call_args.kwargs.get("boot_volume_id"),
                         FAKE_BV)


class TestBootswapCallback(BootSwapBotTestCase):
    """bootswap_callback：选备份后出确认页。"""

    async def test_pick_shows_confirm(self):
        update = make_update("bs:pick:0")
        context = make_context()
        context.user_data["bootswap_backups"] = FAKE_BACKUPS[:1]
        context.user_data["bootswap_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        await he.bootswap_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认用备份恢复", text)
        self.assertIn("old-backup", text)
        self.assertIn("备份之后的数据会丢失", text)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("c:bootswap:0", datas)

    async def test_pick_expired(self):
        update = make_update("bs:pick:0")
        context = make_context()
        await he.bootswap_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))


class TestBootswapConfirm(BootSwapBotTestCase):
    """handle_confirm("bootswap")：额度重检 + 锁 + 起后台线程。"""

    async def test_expired_selection(self):
        update = make_update("c:bootswap:0")
        context = make_context()
        result = await he.handle_confirm("bootswap", "0", update, context)
        self.assertTrue(result)
        self.assertIn("已过期", reply_text_of(update))

    @patch.object(he, "check_boot_volume_space",
                  return_value=(False, 10))
    async def test_quota_insufficient(self, _mock_space):
        update = make_update("c:bootswap:0")
        context = make_context()
        context.user_data["bootswap_backups"] = FAKE_BACKUPS[:1]
        context.user_data["bootswap_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        result = await he.handle_confirm("bootswap", "0", update, context)
        self.assertTrue(result)
        self.assertIn("额度不足", reply_text_of(update))

    @patch.object(he, "check_boot_volume_space",
                  return_value=(True, 59))
    async def test_already_running(self, _mock_space):
        he._BOOTSWAP_RUNNING.add(FAKE_INSTANCE)
        update = make_update("c:bootswap:0")
        context = make_context()
        context.user_data["bootswap_backups"] = FAKE_BACKUPS[:1]
        context.user_data["bootswap_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        result = await he.handle_confirm("bootswap", "0", update, context)
        self.assertTrue(result)
        self.assertIn("正在换盘", reply_text_of(update))

    @patch.object(he, "threading")
    @patch.object(he, "check_boot_volume_space",
                  return_value=(True, 59))
    async def test_starts_thread(self, _mock_space, mock_threading):
        update = make_update("c:bootswap:0")
        context = make_context()
        context.user_data["bootswap_backups"] = FAKE_BACKUPS[:1]
        context.user_data["bootswap_idx"] = "0"
        context.user_data["instances"] = FAKE_INSTANCES
        result = await he.handle_confirm("bootswap", "0", update, context)
        self.assertTrue(result)
        self.assertIn("已开始恢复", reply_text_of(update))
        self.assertIn(FAKE_INSTANCE, he._BOOTSWAP_RUNNING)
        thread_cls = mock_threading.Thread
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs.get("target"),
                         he._bootswap_thread)
        thread_cls.return_value.start.assert_called_once()


class TestBootswapThread(unittest.TestCase):
    """_bootswap_thread：成功/失败通知 + 锁释放。"""

    def setUp(self):
        he._BOOTSWAP_RUNNING.clear()
        he._BOOTSWAP_RUNNING.add(FAKE_INSTANCE)

    def tearDown(self):
        he._BOOTSWAP_RUNNING.clear()

    @patch.object(he, "notify_dual")
    @patch.object(he, "swap_boot_volume",
                  return_value={"old_boot_volume_id": FAKE_BV,
                                "new_boot_volume_id":
                                    "ocid1.bootvolume.oc1..newxxxx",
                                "rolled_back": False,
                                "was_running": True,
                                "old_volume_deleted": True})
    @patch.object(bot_config, "get_oci_client", return_value=MagicMock())
    def test_success(self, _mock_cli, _mock_swap, mock_notify):
        he._bootswap_thread("token", TEST_CHAT_ID, "DEFAULT",
                            "ap-singapore-1", TEST_COMPARTMENT,
                            FAKE_INSTANCE, "test-instance",
                            FAKE_BACKUP, "old-backup")
        self.assertNotIn(FAKE_INSTANCE, he._BOOTSWAP_RUNNING)
        titles = [c.args[2] for c in mock_notify.call_args_list]
        self.assertIn("💾 引导卷恢复成功", titles)
        body = mock_notify.call_args_list[-1].args[3]
        self.assertIn("已重新开机", body)
        self.assertIn("已删除，额度已释放", body)

    @patch.object(he, "notify_dual")
    @patch.object(he, "swap_boot_volume",
                  return_value={"old_boot_volume_id": FAKE_BV,
                                "new_boot_volume_id":
                                    "ocid1.bootvolume.oc1..newxxxx",
                                "rolled_back": False,
                                "was_running": True,
                                "old_volume_deleted": False})
    @patch.object(bot_config, "get_oci_client", return_value=MagicMock())
    def test_old_volume_delete_failed_warns(self, _mock_cli, _mock_swap,
                                            mock_notify):
        """删旧盘失败时恢复仍算成功，消息里警告用户手动删除。"""
        he._bootswap_thread("token", TEST_CHAT_ID, "DEFAULT",
                            "ap-singapore-1", TEST_COMPARTMENT,
                            FAKE_INSTANCE, "test-instance",
                            FAKE_BACKUP, "old-backup")
        titles = [c.args[2] for c in mock_notify.call_args_list]
        self.assertIn("💾 引导卷恢复成功", titles)
        body = mock_notify.call_args_list[-1].args[3]
        self.assertIn("自动删除失败", body)
        self.assertIn("手动删除", body)

    @patch.object(he, "notify_dual")
    @patch.object(he, "swap_boot_volume",
                  side_effect=BootSwapError("挂新盘失败", rolled_back=True))
    @patch.object(bot_config, "get_oci_client", return_value=MagicMock())
    def test_rollback_reported(self, _mock_cli, _mock_swap, mock_notify):
        he._bootswap_thread("token", TEST_CHAT_ID, "DEFAULT",
                            "ap-singapore-1", TEST_COMPARTMENT,
                            FAKE_INSTANCE, "test-instance",
                            FAKE_BACKUP, "old-backup")
        self.assertNotIn(FAKE_INSTANCE, he._BOOTSWAP_RUNNING)
        body = mock_notify.call_args_list[-1].args[3]
        self.assertIn("已换回旧盘", body)

    @patch.object(he, "notify_dual")
    @patch.object(he, "swap_boot_volume",
                  side_effect=RuntimeError("boom"))
    @patch.object(bot_config, "get_oci_client", return_value=MagicMock())
    def test_generic_error(self, _mock_cli, _mock_swap, mock_notify):
        he._bootswap_thread("token", TEST_CHAT_ID, "DEFAULT",
                            "ap-singapore-1", TEST_COMPARTMENT,
                            FAKE_INSTANCE, "test-instance",
                            FAKE_BACKUP, "old-backup")
        self.assertNotIn(FAKE_INSTANCE, he._BOOTSWAP_RUNNING)
        body = mock_notify.call_args_list[-1].args[3]
        self.assertIn("boom", body)


if __name__ == "__main__":
    unittest.main()
