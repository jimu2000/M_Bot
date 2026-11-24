#!/usr/bin/env python3
"""菜单 1 按钮矩阵表单的 handler 单元测试（async，全部用 mock）。

覆盖：handle_boot（表单初始化）、boot_form_callback（点选切换 /
自定义提示 / 确认页 / 保存）、text_input_callback 的 boot_* 分支、
boot_form_document_callback（脚本文件上传）、boot_preset_load_callback
（菜单 17 一键开机）、带 root 密码的 boot_go_callback、批量抢机线程
（逐台创建 / 中途配额不足 / 保存时脱敏）。
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import boot_form as bf
from tgbot import config as bot_config
from tgbot.services import TaskRegistry
import tgbot.handlers_extra as he
from oci.client import QuotaExceededError
from oci.grabber import GrabCancelledError, GrabResult

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

FAKE_IMAGE_AARCH64 = "ocid1.image.oc1.ap-singapore-1.aaa111"
FAKE_IMAGE_X86_64 = "ocid1.image.oc1.ap-singapore-1.xxx222"
FAKE_OL_AARCH64 = "ocid1.image.oc1.ap-singapore-1.ol111"
FAKE_OL_X86_64 = "ocid1.image.oc1.ap-singapore-1.ol222"
FAKE_DEPLOY = {
    "availability_domain": "wgwP:AP-SINGAPORE-1-AD-1",
    "subnet_id": "ocid1.subnet.oc1.ap-singapore-1.ssssss",
    "ssh_keys_file": "/opt/rbot-clone/web/data/ssh_keys.pub",
    "image_aarch64": FAKE_IMAGE_AARCH64,
    "image_x86_64": FAKE_IMAGE_X86_64,
    "ol_aarch64": FAKE_OL_AARCH64,
    "ol_x86_64": FAKE_OL_X86_64,
}


def make_update(callback_data=None, text=None):
    """mock Update：reply_text / query.answer / query.edit_message_text
    都是 AsyncMock；text 非空时模拟用户发的文字消息。"""
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
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def reply_markup_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[-1]
    return kwargs.get("reply_markup")


def callbacks_of(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def filled_form(**overrides):
    form = bf.new_form()
    form.update({
        "cpu": "arm", "os": "ubuntu", "ocpus": 1, "memory_gb": 6,
        "disk_gb": None, "count": 1, "delays": [5, 20],
        "ssh_key": "use", "root_login": False, "public_ip": True,
        "ipv6": False, "script": None,
    })
    form.update(overrides)
    return form


class BootFormHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、新 TASKS、mock cli、假镜像/假部署配置。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        self.tasks = TaskRegistry()
        self._tasks_patch = patch.object(he, "TASKS", self.tasks)
        self._tasks_patch.start()
        self.addCleanup(self._tasks_patch.stop)

        self.cli = MagicMock()
        self.cli.run.return_value = {"data": []}
        self._cli_patch = patch.object(bot_config, "get_oci_client",
                                       return_value=self.cli)
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)

        self._images = patch("tgbot.boot_form.IMAGE_IDS", {
            "aarch64": FAKE_IMAGE_AARCH64, "x86_64": FAKE_IMAGE_X86_64})
        self._images.start()
        self.addCleanup(self._images.stop)
        self._ol_images = patch("tgbot.boot_form.OL_IMAGE_IDS", {
            "aarch64": FAKE_OL_AARCH64, "x86_64": FAKE_OL_X86_64})
        self._ol_images.start()
        self.addCleanup(self._ol_images.stop)
        self._deploy = patch("tgbot.boot_form.get_deploy_defaults",
                             return_value=dict(FAKE_DEPLOY))
        self._deploy.start()
        self.addCleanup(self._deploy.stop)
        # handlers_extra 直接 from-import 了 get_deploy_defaults，要另 patch
        # 它自己的命名空间（_boot_form_confirm 用的是这一份）。
        self._deploy_he = patch("tgbot.handlers_extra.get_deploy_defaults",
                                return_value=dict(FAKE_DEPLOY))
        self._deploy_he.start()
        self.addCleanup(self._deploy_he.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tmp_path(self, name):
        return os.path.join(self._tmp.name, name)

    async def _wait_task(self, task_id, status, timeout=5.0):
        for _ in range(int(timeout / 0.1)):
            if self.tasks.get(task_id)["status"] == status:
                return self.tasks.get(task_id)
            await asyncio.sleep(0.1)
        return self.tasks.get(task_id)


class TestHandleBoot(BootFormHandlersTestCase):

    async def test_handle_boot_initializes_form_and_shows_matrix(self):
        update = make_update()
        context = make_context()
        await he.handle_boot(update, context)
        form = context.user_data.get("boot_form")
        self.assertIsNotNone(form)
        self.assertIsNone(form["cpu"])
        self.assertEqual(form["count"], 1)
        text = reply_text_of(update)
        self.assertIn("CPU：", text)
        self.assertIn("数量：1 台", text)
        self.assertIn("bf:cpu:arm", callbacks_of(reply_markup_of(update)))
        self.assertIn("bf:confirm", callbacks_of(reply_markup_of(update)))
        self.assertIn("bf:save", callbacks_of(reply_markup_of(update)))


class TestBootFormCallback(BootFormHandlersTestCase):

    async def test_toggle_edits_message_in_place(self):
        update = make_update(callback_data="bf:cpu:arm")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        self.assertEqual(context.user_data["boot_form"]["cpu"], "arm")
        query = update.callback_query
        query.edit_message_text.assert_awaited_once()
        args, kwargs = query.edit_message_text.await_args
        self.assertIn("ARM", args[0])
        self.assertIn("bf:cpu:arm", callbacks_of(kwargs["reply_markup"]))
        # 点选切换不发新消息。
        update.effective_message.reply_text.assert_not_awaited()

    async def test_toggle_deselect(self):
        update = make_update(callback_data="bf:cpu:arm")
        context = make_context()
        context.user_data["boot_form"] = filled_form()
        await he.boot_form_callback(update, context)
        self.assertIsNone(context.user_data["boot_form"]["cpu"])

    async def test_delay_third_selection_alerts(self):
        update = make_update(callback_data="bf:delay:60")
        context = make_context()
        context.user_data["boot_form"] = filled_form(delays=[5, 20])
        await he.boot_form_callback(update, context)
        self.assertEqual(context.user_data["boot_form"]["delays"], [5, 20])
        query = update.callback_query
        query.answer.assert_awaited_once()
        _args, kwargs = query.answer.await_args
        self.assertIn("最多选两个", kwargs.get("text", ""))
        self.assertTrue(kwargs.get("show_alert"))

    async def test_noop_only_answers(self):
        update = make_update(callback_data="bf:noop")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        update.callback_query.answer.assert_awaited_once()
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_custom_prompts_and_sets_pending_input(self):
        update = make_update(callback_data="bf:cpu:custom")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "boot_cpu")
        self.assertIn("形状名", reply_text_of(update))
        self.assertIn("bf:cancel_input",
                      callbacks_of(reply_markup_of(update)))

    async def test_script_upload_prompts(self):
        update = make_update(callback_data="bf:script:upload")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "boot_script")
        self.assertIn("脚本", reply_text_of(update))

    async def test_cancel_input(self):
        """取消自定义输入：清 pending_input，重发表单继续填。

        v1.34.0：提示发出时旧表单已被选择框清理删掉，取消不再只
        编辑提示说“表单未变”，而是重发表单（与输入完成后的做法一致）。
        """
        update = make_update(callback_data="bf:cancel_input")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "boot_cpu"}
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("开机配置", reply_text_of(update))
        self.assertIn("bf:confirm",
                      callbacks_of(reply_markup_of(update)))

    async def test_expired_form_prompts_reopen(self):
        update = make_update(callback_data="bf:cpu:arm")
        context = make_context()
        await he.boot_form_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_confirm_incomplete_lists_errors(self):
        update = make_update(callback_data="bf:confirm")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("还没填完", text)
        self.assertIn("未选 CPU 类型", text)
        self.assertNotIn("pending_boot", context.user_data)

    async def test_confirm_complete_shows_summary_and_prechecks(self):
        update = make_update(callback_data="bf:confirm")
        context = make_context()
        context.user_data["boot_form"] = filled_form()
        with patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")) as mock_pre:
            await he.boot_form_callback(update, context)
        preset = context.user_data["pending_boot"]
        self.assertEqual(preset["shape"], "VM.Standard.A1.Flex")
        self.assertEqual(preset["image_id"], FAKE_IMAGE_AARCH64)
        mock_pre.assert_called_once()
        # 预检参数：shape / ocpus / mem / count / disk。
        args = mock_pre.call_args[0]
        self.assertEqual(args[2:], ("VM.Standard.A1.Flex", 1, 6, 1, None))
        text = reply_text_of(update)
        self.assertIn("A1.Flex", text)
        self.assertIn("✅ 可以创建", text)
        self.assertIn("b:go", callbacks_of(reply_markup_of(update)))

    async def test_oneclick_amd_goes_to_confirm_page(self):
        # 一键开机（默认 AMD）：不依赖用户正在填的表单，直接进确认页
        update = make_update(callback_data="bf:oneclick:amd")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        with patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")):
            await he.boot_form_callback(update, context)
        preset = context.user_data["pending_boot"]
        self.assertEqual(preset["shape"], "VM.Standard.E2.1.Micro")
        self.assertEqual(preset["ocpus"], 1)
        self.assertEqual(preset["memory_gb"], 1)
        self.assertEqual(preset["image_id"], FAKE_IMAGE_X86_64)
        self.assertTrue(preset["root_login"])
        self.assertRegex(preset["display_name"],
                         r"^AMD_\d{4}-\d{2}-\d{2}$")
        text = reply_text_of(update)
        self.assertIn("E2.1.Micro", text)
        self.assertIn("✅ 可以创建", text)
        self.assertIn("b:go", callbacks_of(reply_markup_of(update)))
        # 一键开机不覆盖用户正在填的表单
        self.assertIsNone(context.user_data["boot_form"]["cpu"])

    async def test_oneclick_amd_works_without_open_form(self):
        # 表单已过期（user_data 里没有 boot_form）时一键开机仍能用
        update = make_update(callback_data="bf:oneclick:amd")
        context = make_context()
        with patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")):
            await he.boot_form_callback(update, context)
        self.assertEqual(context.user_data["pending_boot"]["shape"],
                         "VM.Standard.E2.1.Micro")

    async def test_confirm_quota_blocked_shows_reason(self):
        update = make_update(callback_data="bf:confirm")
        context = make_context()
        context.user_data["boot_form"] = filled_form(count=4)
        with patch.object(he, "can_launch_batch",
                          return_value=(False, "A1 OCPU 不足")):
            await he.boot_form_callback(update, context)
        self.assertIn("❌ A1 OCPU 不足", reply_text_of(update))
        # 预检失败也存 pending_boot（与旧预设流程一致，Grabber 运行时
        # 会再检一次；用户点开始抢机后会立即因配额停下）。
        self.assertIn("pending_boot", context.user_data)

    async def test_save_incomplete_errors(self):
        update = make_update(callback_data="bf:save")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_callback(update, context)
        self.assertIn("还没填完", reply_text_of(update))

    async def test_save_complete_writes_presets(self):
        presets_file = self.tmp_path("presets.json")
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="bf:save")
            context = make_context()
            context.user_data["boot_form"] = filled_form()
            await he.boot_form_callback(update, context)
        self.assertIn("已保存为快捷开机", reply_text_of(update))
        with open(presets_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["shape"], "VM.Standard.A1.Flex")
        self.assertNotIn("user_data", saved[0])
        self.assertNotIn("root_password", saved[0])


class TestBootFormInput(BootFormHandlersTestCase):

    async def _send_text(self, kind, text):
        update = make_update(text=text)
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        context.user_data["pending_input"] = {"kind": kind}
        await he.text_input_callback(update, context)
        return update, context

    async def test_custom_cpu_sets_shape(self):
        update, context = await self._send_text(
            "boot_cpu", "VM.Standard.E3.Flex")
        self.assertEqual(context.user_data["boot_form"]["cpu"],
                         "VM.Standard.E3.Flex")
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("E3.Flex", reply_text_of(update))

    async def test_custom_value_invalid_reprompts(self):
        update, context = await self._send_text("boot_disk", "999")
        self.assertIn("❌", reply_text_of(update))
        self.assertIsNone(context.user_data["boot_form"]["disk_gb"])
        # 失败时不保留 pending_input，用户重新点"自定义"再输入。
        self.assertNotIn("pending_input", context.user_data)

    async def test_custom_delay_appends(self):
        update, context = await self._send_text("boot_delay", "7")
        self.assertEqual(context.user_data["boot_form"]["delays"], [7])

    async def test_pasted_script_sets_script(self):
        update, context = await self._send_text(
            "boot_script", "#!/bin/bash\necho hello")
        script = context.user_data["boot_form"]["script"]
        self.assertIsNotNone(script)
        self.assertIn("echo hello", script["content"])
        self.assertEqual(script["name"], "粘贴的脚本")
        self.assertIn("已上传", reply_text_of(update))

    async def test_pasted_script_too_long(self):
        update, context = await self._send_text(
            "boot_script", "x" * (bf.SCRIPT_MAX_CHARS + 1))
        self.assertIn("脚本太长", reply_text_of(update))
        self.assertIsNone(context.user_data["boot_form"]["script"])

    async def test_input_without_form_reports_expired(self):
        update = make_update(text="VM.Standard.E3.Flex")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "boot_cpu"}
        await he.text_input_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))


class TestBootFormDocument(BootFormHandlersTestCase):

    def _doc_update(self, file_name="setup.sh", file_size=100,
                    content=b"#!/bin/bash\necho hi"):
        update = make_update()
        document = MagicMock()
        document.file_name = file_name
        document.file_size = file_size
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(content))
        document.get_file = AsyncMock(return_value=tg_file)
        update.effective_message.document = document
        return update

    async def test_ignored_without_pending_script(self):
        update = self._doc_update()
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        await he.boot_form_document_callback(update, context)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertIsNone(context.user_data["boot_form"]["script"])

    async def test_upload_sets_script(self):
        update = self._doc_update()
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        context.user_data["pending_input"] = {"kind": "boot_script"}
        await he.boot_form_document_callback(update, context)
        script = context.user_data["boot_form"]["script"]
        self.assertIsNotNone(script)
        self.assertIn("echo hi", script["content"])
        self.assertEqual(script["name"], "setup.sh")
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("已上传", reply_text_of(update))

    async def test_upload_too_large(self):
        update = self._doc_update(file_size=bf.SCRIPT_MAX_CHARS + 1)
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        context.user_data["pending_input"] = {"kind": "boot_script"}
        await he.boot_form_document_callback(update, context)
        self.assertIn("文件太大", reply_text_of(update))
        self.assertIsNone(context.user_data["boot_form"]["script"])
        self.assertNotIn("pending_input", context.user_data)

    async def test_upload_bad_encoding(self):
        update = self._doc_update(content=b"\xff\xfe\x00bad")
        context = make_context()
        context.user_data["boot_form"] = bf.new_form()
        context.user_data["pending_input"] = {"kind": "boot_script"}
        await he.boot_form_document_callback(update, context)
        self.assertIn("读取文件失败", reply_text_of(update))
        self.assertIsNone(context.user_data["boot_form"]["script"])


class TestBootPresetLoad(BootFormHandlersTestCase):

    def _write_presets(self, presets_file, presets):
        with open(presets_file, "w", encoding="utf-8") as fh:
            json.dump(presets, fh)

    async def test_load_shows_summary_and_prechecks(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file), \
             patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")) as mock_pre:
            update = make_update(callback_data="b:load:0")
            context = make_context()
            await he.boot_preset_load_callback(update, context)
        self.assertEqual(context.user_data["pending_boot"]["shape"],
                         "VM.Standard.A1.Flex")
        mock_pre.assert_called_once()
        text = reply_text_of(update)
        self.assertIn("A1.Flex", text)
        self.assertIn("✅ 可以创建", text)
        self.assertIn("b:go", callbacks_of(reply_markup_of(update)))

    async def test_load_old_preset_gets_defaults(self):
        presets_file = self.tmp_path("presets.json")
        old_preset = {
            "shape": "VM.Standard.E2.1.Micro", "ocpus": 1, "memory_gb": 1,
            "display_name": "e2-micro",
            "image_id": FAKE_IMAGE_X86_64,
            "availability_domain": FAKE_DEPLOY["availability_domain"],
            "subnet_id": FAKE_DEPLOY["subnet_id"],
            "ssh_keys_file": FAKE_DEPLOY["ssh_keys_file"],
            "target_cpu": "7J13",
        }
        self._write_presets(presets_file, [old_preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file), \
             patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")):
            update = make_update(callback_data="b:load:0")
            context = make_context()
            await he.boot_preset_load_callback(update, context)
        pending = context.user_data["pending_boot"]
        self.assertEqual(pending["count"], 1)
        self.assertTrue(pending["assign_public_ip"])
        self.assertFalse(pending["assign_ipv6"])
        self.assertFalse(pending["root_login"])
        self.assertIn("7J13", reply_text_of(update))

    async def test_load_out_of_range(self):
        presets_file = self.tmp_path("presets.json")
        self._write_presets(presets_file, [])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:load:0")
            context = make_context()
            await he.boot_preset_load_callback(update, context)
        self.assertIn("已不存在", reply_text_of(update))
        self.assertNotIn("pending_boot", context.user_data)

    async def test_load_region_mismatch_blocked(self):
        """已保存的新加坡配置，在圣何塞工作区域下不给进确认页。"""
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file), \
             patch.object(he, "can_launch_batch",
                          return_value=(True, "可以创建")) as mock_pre:
            update = make_update(callback_data="b:load:0")
            context = make_context()
            context.user_data["region"] = "us-sanjose-1"
            await he.boot_preset_load_callback(update, context)
        self.assertNotIn("pending_boot", context.user_data)
        mock_pre.assert_not_called()
        self.assertIn("不一致", reply_text_of(update))

    async def test_presets_menu_has_launch_buttons(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update()
            context = make_context()
            await he.handle_presets(update, context)
        self.assertIn("b:load:0", callbacks_of(reply_markup_of(update)))
        self.assertIn(preset["display_name"], reply_text_of(update))

    async def test_presets_menu_has_edit_delete_buttons(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update()
            context = make_context()
            await he.handle_presets(update, context)
        callbacks = callbacks_of(reply_markup_of(update))
        self.assertIn("b:load:0", callbacks)
        self.assertIn("b:edit:0", callbacks)
        self.assertIn("b:del:0", callbacks)

    async def test_edit_prefills_form(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:edit:0")
            context = make_context()
            await he.boot_preset_edit_callback(update, context)
        form = context.user_data["boot_form"]
        self.assertEqual(form["cpu"], "arm")
        self.assertEqual(form["os"], "ubuntu")
        self.assertEqual(form["ocpus"], 1)
        self.assertEqual(form["memory_gb"], 6)
        self.assertEqual(context.user_data["editing_preset_idx"], 0)
        self.assertIn("正在编辑", reply_text_of(update))
        self.assertIn("bf:save", callbacks_of(reply_markup_of(update)))

    async def test_edit_out_of_range(self):
        presets_file = self.tmp_path("presets.json")
        self._write_presets(presets_file, [])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:edit:0")
            context = make_context()
            await he.boot_preset_edit_callback(update, context)
        self.assertIn("已不存在", reply_text_of(update))
        self.assertNotIn("editing_preset_idx", context.user_data)

    async def test_delete_asks_confirm(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:del:0")
            context = make_context()
            await he.boot_preset_delete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确定删除", text)
        self.assertIn(preset["display_name"], text)
        callbacks = callbacks_of(reply_markup_of(update))
        self.assertIn("b:delok:0", callbacks)
        self.assertIn("o:presets", callbacks)
        # 确认前文件没动。
        with open(presets_file, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)), 1)

    async def test_delete_confirm_removes_and_refreshes(self):
        presets_file = self.tmp_path("presets.json")
        first = bf.build_preset(filled_form(), FAKE_DEPLOY)
        second = bf.build_preset(filled_form(memory_gb=12), FAKE_DEPLOY)
        second["display_name"] = "ARM_2026-11-24-keep"
        self._write_presets(presets_file, [first, second])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:delok:0")
            context = make_context()
            await he.boot_preset_delete_confirm_callback(update, context)
        with open(presets_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["display_name"], "ARM_2026-11-24-keep")
        text = reply_text_of(update)
        self.assertIn("已删除", text)
        self.assertIn("ARM_2026-11-24-keep", text)
        self.assertIn("快捷开机配置（1 条）", text)
        callbacks = callbacks_of(reply_markup_of(update))
        self.assertIn("b:load:0", callbacks)
        self.assertIn("b:edit:0", callbacks)
        self.assertIn("b:del:0", callbacks)

    async def test_delete_confirm_out_of_range(self):
        presets_file = self.tmp_path("presets.json")
        self._write_presets(presets_file, [])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:delok:0")
            context = make_context()
            await he.boot_preset_delete_confirm_callback(update, context)
        self.assertIn("已不存在", reply_text_of(update))

    async def test_save_when_editing_updates_in_place(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="bf:save")
            context = make_context()
            context.user_data["boot_form"] = filled_form(memory_gb=12)
            context.user_data["editing_preset_idx"] = 0
            await he.boot_form_callback(update, context)
        self.assertIn("已更新快捷开机配置", reply_text_of(update))
        self.assertNotIn("editing_preset_idx", context.user_data)
        with open(presets_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["memory_gb"], 12)
        # 编辑是改配置不是改名。
        self.assertEqual(saved[0]["display_name"],
                         preset["display_name"])

    async def test_save_when_editing_missing_falls_back_to_append(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        self._write_presets(presets_file, [preset])
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="bf:save")
            context = make_context()
            context.user_data["boot_form"] = filled_form(memory_gb=12)
            context.user_data["editing_preset_idx"] = 5
            await he.boot_form_callback(update, context)
        self.assertIn("已保存为快捷开机", reply_text_of(update))
        with open(presets_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["memory_gb"], 12)


class TestBootGoWithRoot(BootFormHandlersTestCase):

    async def _run_boot_go(self, preset):
        """mock Grabber 跑完 boot_go，返回 (update, mock_grabber_cls, task)。"""
        presets_file = self.tmp_path("presets.json")
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:go")
            context = make_context()
            context.user_data["pending_boot"] = dict(preset)
            mock_grabber_cls = MagicMock()
            mock_grabber_cls.return_value.run.return_value = GrabResult(
                instance_id="ocid1.instance.oc1..zzzz", attempts=3,
                releases=1, elapsed_seconds=120.0)
            with patch.object(he, "Grabber", mock_grabber_cls):
                await he.boot_go_callback(update, context)
                task_id = self.tasks.list_all()[0]["id"]
                task = await self._wait_task(task_id, "done")
                # 等保存文件的写盘（状态先置 done、再写文件）。
                saved = []
                for _ in range(50):
                    saved = he.load_presets(presets_file)
                    if saved:
                        break
                    await asyncio.sleep(0.1)
            return update, mock_grabber_cls, task, saved

    async def test_root_login_shows_password_once(self):
        preset = bf.build_preset(
            filled_form(ssh_key="none", root_login=True,
                        script={"name": "s.sh", "content": "echo hi"}),
            FAKE_DEPLOY)
        update, mock_grabber_cls, task, saved = await self._run_boot_go(preset)
        text = reply_text_of(update)
        self.assertIn("抢机已启动", text)
        self.assertIn("root 登录密码", text)
        self.assertIn("只显示这一次", text)
        # 密码在 Markdown 代码段里（UUID 32 位 hex），且不在任务详情里。
        self.assertRegex(text, r"`[0-9a-f]{32}`")
        self.assertNotIn("root 登录密码", task["detail"])
        # Grabber 收到了带 chpasswd 的 user-data（user_data 是关键字参数，
        # ssh_keys_file 是第 8 个位置参数）。
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertIn("chpasswd", kwargs["user_data"])
        self.assertIn("echo hi", kwargs["user_data"])
        self.assertIsNone(mock_grabber_cls.call_args.args[7])
        # 保存的预设里没有 user-data / 密码。
        self.assertEqual(len(saved), 1)
        self.assertNotIn("user_data", saved[0])
        self.assertNotIn("root_password", saved[0])
        self.assertTrue(saved[0]["root_login"])

    async def test_boot_go_region_mismatch_blocked(self):
        """确认页出来后切了区域，旧区部署值不给启动抢机。"""
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        update = make_update(callback_data="b:go")
        context = make_context()
        context.user_data["pending_boot"] = dict(preset)
        context.user_data["region"] = "us-sanjose-1"
        await he.boot_go_callback(update, context)
        self.assertEqual(self.tasks.list_all(), [])
        self.assertIn("不一致", reply_text_of(update))

    async def test_boot_go_uniquifies_taken_display_name(self):
        # 已有同名实例时启动前加 -2 后缀（日期名同日再开机）
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        base_name = preset["display_name"]
        with patch.object(he, "list_instances",
                          return_value=[{"display-name": base_name}]):
            _update, mock_grabber_cls, _task, _saved = \
                await self._run_boot_go(preset)
        self.assertEqual(mock_grabber_cls.call_args.args[6],
                         f"{base_name}-2")

    async def test_boot_go_keeps_name_when_no_conflict(self):
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        with patch.object(he, "list_instances", return_value=[]):
            _update, mock_grabber_cls, _task, _saved = \
                await self._run_boot_go(preset)
        self.assertEqual(mock_grabber_cls.call_args.args[6],
                         preset["display_name"])

    async def test_no_root_login_no_password(self):
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        update, mock_grabber_cls, task, _saved = await self._run_boot_go(preset)
        text = reply_text_of(update)
        self.assertIn("抢机已启动", text)
        self.assertNotIn("root 登录密码", text)
        kwargs = mock_grabber_cls.call_args.kwargs
        self.assertIsNone(kwargs["user_data"])
        self.assertEqual(mock_grabber_cls.call_args.args[7],
                         FAKE_DEPLOY["ssh_keys_file"])
        # count=1 时保持旧版详情格式。
        self.assertIn("尝试 3 次", task["detail"])
        self.assertIn("释放 1 台", task["detail"])
        mock_grabber_cls.assert_called_once()

    async def test_boot_go_without_preset_new_prompt(self):
        update = make_update(callback_data="b:go")
        context = make_context()
        await he.boot_go_callback(update, context)
        self.assertIn("请先在菜单 1 里配好开机表单", reply_text_of(update))
        self.assertEqual(self.tasks.list_all(), [])

    async def test_boot_go_pending_boot_cleared_after_start(self):
        preset = bf.build_preset(filled_form(), FAKE_DEPLOY)
        update = make_update(callback_data="b:go")
        context = make_context()
        context.user_data["pending_boot"] = dict(preset)
        mock_grabber_cls = MagicMock()
        mock_grabber_cls.return_value.run.return_value = GrabResult(
            instance_id="ocid1.instance.oc1..zzzz", attempts=1,
            releases=0, elapsed_seconds=1.0)
        with patch.object(he, "Grabber", mock_grabber_cls):
            await he.boot_go_callback(update, context)
        # 启动后立即清除（含带密码的 user-data，不留内存）。
        self.assertNotIn("pending_boot", context.user_data)


class TestBatchGrabberThread(BootFormHandlersTestCase):

    async def test_batch_creates_sequentially_with_suffix(self):
        presets_file = self.tmp_path("presets.json")
        preset = bf.build_preset(filled_form(count=3), FAKE_DEPLOY)
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:go")
            context = make_context()
            context.user_data["pending_boot"] = dict(preset)
            mock_grabber_cls = MagicMock()
            mock_grabber_cls.return_value.run.side_effect = [
                GrabResult(instance_id="ocid1.instance.oc1..aaa1",
                           attempts=2, releases=0, elapsed_seconds=60.0),
                GrabResult(instance_id="ocid1.instance.oc1..bbb2",
                           attempts=1, releases=1, elapsed_seconds=30.0),
                GrabResult(instance_id="ocid1.instance.oc1..ccc3",
                           attempts=4, releases=0, elapsed_seconds=90.0),
            ]
            with patch.object(he, "Grabber", mock_grabber_cls):
                await he.boot_go_callback(update, context)
                task_id = self.tasks.list_all()[0]["id"]
                task = await self._wait_task(task_id, "done")
        self.assertEqual(mock_grabber_cls.call_count, 3)
        names = [call.args[6] for call in mock_grabber_cls.call_args_list]
        base = preset["display_name"]
        self.assertEqual(names, [f"{base}-1", f"{base}-2", f"{base}-3"])
        self.assertEqual(task["status"], "done")
        self.assertIn("抢到 3 台实例", task["detail"])
        self.assertIn("共尝试 7 次", task["detail"])
        self.assertIn("释放 1 台", task["detail"])

    async def test_batch_quota_stop_keeps_progress(self):
        preset = bf.build_preset(filled_form(count=3), FAKE_DEPLOY)
        update = make_update(callback_data="b:go")
        context = make_context()
        context.user_data["pending_boot"] = dict(preset)
        mock_grabber_cls = MagicMock()
        mock_grabber_cls.return_value.run.side_effect = [
            GrabResult(instance_id="ocid1.instance.oc1..aaa1", attempts=1,
                       releases=0, elapsed_seconds=10.0),
            QuotaExceededError("A1 OCPU 不足"),
        ]
        with patch.object(he, "Grabber", mock_grabber_cls):
            await he.boot_go_callback(update, context)
            task_id = self.tasks.list_all()[0]["id"]
            task = await self._wait_task(task_id, "failed")
        self.assertEqual(task["status"], "failed")
        self.assertIn("配额不足", task["detail"])
        self.assertIn("已抢到 1/3 台", task["detail"])
        self.assertEqual(mock_grabber_cls.call_count, 2)

    async def test_batch_cancel_marks_cancelled(self):
        preset = bf.build_preset(filled_form(count=2), FAKE_DEPLOY)
        update = make_update(callback_data="b:go")
        context = make_context()
        context.user_data["pending_boot"] = dict(preset)
        mock_grabber_cls = MagicMock()
        mock_grabber_cls.return_value.run.side_effect = GrabCancelledError(
            "cancelled")
        with patch.object(he, "Grabber", mock_grabber_cls):
            await he.boot_go_callback(update, context)
            task_id = self.tasks.list_all()[0]["id"]
            task = await self._wait_task(task_id, "cancelled")
        self.assertEqual(task["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
