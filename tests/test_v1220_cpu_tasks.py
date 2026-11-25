#!/usr/bin/env python3
"""v1.22.0 单元测试：CPU 选项 / 任务列表说明 / 已终止隐藏（后者在
test_tgbot_instance_list.py 里）。

覆盖：
  - tgbot.keyboards.boot_confirm_keyboard：E2 才有 CPU 选项行，
    当前选中带 ✅；A1 / 无 preset 时无选项行。
  - tgbot.boot_form.fmt_boot_form_summary 与
    tgbot.handlers_extra.fmt_boot_preset：E2 始终显示目标 CPU 行
    （只留配置里的型号，默认 7J13 / 什么 CPU 都可以），A1 不显示。
  - tgbot.handlers_extra.boot_cpu_callback：切换 target_cpu 并
    就地刷新摘要。
  - tgbot.services.TaskRegistry.register(label) / fmt_task_list /
    tgbot.keyboards.task_list_keyboard：任务显示 label，不再暴露
    g1 / grab 这类内部编号与类型。
  - web.tasks.TaskRegistry：label 持久化往返；start_grab_task
    生成的 label 含规格与实例名。
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config  # noqa: E402
from tgbot import cpu_policy  # noqa: E402
from tgbot.boot_form import fmt_boot_form_summary  # noqa: E402
from tgbot.keyboards import (  # noqa: E402
    boot_confirm_keyboard,
    task_list_keyboard,
)
from tgbot.services import TaskRegistry, fmt_task_list  # noqa: E402
import tgbot.handlers_extra as he  # noqa: E402
from web import tasks as web_tasks  # noqa: E402

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

E2_SHAPE = "VM.Standard.E2.1.Micro"
A1_SHAPE = "VM.Standard.A1.Flex"


def _e2_preset(target_cpu="7J13"):
    return {
        "shape": E2_SHAPE, "ocpus": 1, "memory_gb": 1,
        "display_name": "AMD_2026-11-24", "os": "ubuntu",
        "boot_volume_size_gbs": None, "count": 1,
        "ssh_keys_file": "/tmp/fake.pub", "root_login": False,
        "assign_public_ip": True, "assign_ipv6": False,
        "startup_script": None, "target_cpu": target_cpu,
    }


def _a1_preset():
    preset = _e2_preset(target_cpu=None)
    preset["shape"] = A1_SHAPE
    preset["ocpus"] = 1
    preset["memory_gb"] = 6
    preset["display_name"] = "ARM_2026-11-24"
    return preset


def _rows(keyboard):
    return [[(btn.text, btn.callback_data) for btn in row]
            for row in keyboard.inline_keyboard]


def make_update(callback_data=None):
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
        query.message.message_id = 777
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


def edit_text_of(update):
    mock = update.callback_query.edit_message_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def edit_keyboard_of(update):
    mock = update.callback_query.edit_message_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[-1]
    return kwargs.get("reply_markup")


class _DefaultCpuPolicyMixin:
    """把 CPU 规则文件指到不存在的临时路径（用默认 7J13）。"""

    def setUp(self):
        super().setUp()
        self._policy_tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._policy_tmpdir.cleanup)
        patcher = patch.object(
            cpu_policy, "policy_path",
            return_value=os.path.join(self._policy_tmpdir.name,
                                      "cpu_policy.json"))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestBootConfirmKeyboard(_DefaultCpuPolicyMixin, unittest.TestCase):
    """确认键盘：E2 有 CPU 选项行，A1 没有。"""

    def test_e2_with_7j13_marks_only_7j13(self):
        rows = _rows(boot_confirm_keyboard(_e2_preset("7J13")))
        self.assertEqual(rows[0], [("✅ 只抢 7J13", "b:cpu:7j13"),
                                   ("什么 CPU 都可以", "b:cpu:any")])
        self.assertEqual(rows[1], [("🚀 开始抢机", "b:go")])
        self.assertEqual(rows[2], [("❌ 取消", "x")])

    def test_e2_any_cpu_marks_any(self):
        rows = _rows(boot_confirm_keyboard(_e2_preset(None)))
        self.assertEqual(rows[0], [("只抢 7J13", "b:cpu:7j13"),
                                   ("✅ 什么 CPU 都可以", "b:cpu:any")])

    def test_a1_no_cpu_row(self):
        rows = _rows(boot_confirm_keyboard(_a1_preset()))
        self.assertEqual(rows, [[("🚀 开始抢机", "b:go")],
                                [("❌ 取消", "x")]])

    def test_no_preset_old_behavior(self):
        rows = _rows(boot_confirm_keyboard())
        self.assertEqual(rows, [[("🚀 开始抢机", "b:go")],
                                [("❌ 取消", "x")]])


class TestCpuSummaryLine(_DefaultCpuPolicyMixin, unittest.TestCase):
    """摘要里的目标 CPU 行：E2 始终有，A1 没有。"""

    def test_form_e2_7j13(self):
        text = fmt_boot_form_summary(_e2_preset("7J13"), True, "可以创建")
        self.assertIn("目标 CPU：只留 7J13，不符就释放继续抢", text)

    def test_form_e2_any(self):
        text = fmt_boot_form_summary(_e2_preset(None), True, "可以创建")
        self.assertIn("目标 CPU：什么 CPU 都可以，抢到就留", text)

    def test_form_a1_no_line(self):
        text = fmt_boot_form_summary(_a1_preset(), True, "可以创建")
        self.assertNotIn("目标 CPU", text)

    def test_preset_e2_7j13(self):
        text = he.fmt_boot_preset(_e2_preset("7J13"), True, "可以创建")
        self.assertIn("目标 CPU：只留 7J13，不符就释放继续抢", text)

    def test_preset_e2_any(self):
        text = he.fmt_boot_preset(_e2_preset(None), True, "可以创建")
        self.assertIn("目标 CPU：什么 CPU 都可以，抢到就留", text)

    def test_preset_a1_no_line(self):
        text = he.fmt_boot_preset(_a1_preset(), True, "可以创建")
        self.assertNotIn("目标 CPU", text)


class TestBootCpuCallback(_DefaultCpuPolicyMixin,
                          unittest.IsolatedAsyncioTestCase):
    """b:cpu 回调：切换 target_cpu，就地刷新摘要。"""

    def setUp(self):
        super().setUp()
        self._old_allowed = bot_config.ALLOWED_USER_IDS
        bot_config.ALLOWED_USER_IDS = {TEST_USER_ID}

    def tearDown(self):
        bot_config.ALLOWED_USER_IDS = self._old_allowed

    async def _run(self, callback_data, preset, summary_kind="form"):
        update = make_update(callback_data)
        context = make_context()
        context.user_data["pending_boot"] = preset
        context.user_data["pending_boot_summary"] = summary_kind
        with patch.object(he, "run_sync",
                          AsyncMock(return_value=(True, "可以创建"))), \
             patch.object(he, "get_cli", return_value=MagicMock()), \
             patch.object(he, "get_compartment", return_value="ocid1.test"):
            await he.boot_cpu_callback(update, context)
        return update, context

    async def test_switch_to_any(self):
        update, context = await self._run("b:cpu:any", _e2_preset("7J13"))
        self.assertIsNone(context.user_data["pending_boot"]["target_cpu"])
        text = edit_text_of(update)
        self.assertIn("目标 CPU：什么 CPU 都可以，抢到就留", text)
        rows = _rows(edit_keyboard_of(update))
        self.assertEqual(rows[0][1][0], "✅ 什么 CPU 都可以")

    async def test_switch_to_7j13(self):
        update, context = await self._run("b:cpu:7j13", _e2_preset(None))
        self.assertEqual(context.user_data["pending_boot"]["target_cpu"],
                         "7J13")
        text = edit_text_of(update)
        self.assertIn("目标 CPU：只留 7J13", text)
        rows = _rows(edit_keyboard_of(update))
        self.assertEqual(rows[0][0][0], "✅ 只抢 7J13")

    async def test_preset_summary_kind(self):
        # The one-click boot path uses fmt_boot_preset (its title is "开机配置："). 一键开机路径用 fmt_boot_preset（标题是“开机配置：”）。
        update, _ctx = await self._run("b:cpu:any", _e2_preset("7J13"),
                                       summary_kind="preset")
        self.assertTrue(edit_text_of(update).startswith("🚀 开机配置："))

    async def test_expired_pending_boot(self):
        update = make_update("b:cpu:any")
        context = make_context()
        await he.boot_cpu_callback(update, context)
        args, _kw = update.effective_message.reply_text.await_args_list[-1]
        self.assertIn("配置已过期", args[0])


class TestTaskLabel(unittest.TestCase):
    """任务列表：显示 label，不暴露内部编号与类型。"""

    def test_register_stores_label(self):
        registry = TaskRegistry()
        task_id = registry.register("grab", detail="准备中…",
                                    label="E2.1.Micro（新加坡）· AMD_2026-11-24")
        task = registry.get(task_id)
        self.assertEqual(task["label"], "E2.1.Micro（新加坡）· AMD_2026-11-24")

    def test_register_without_label(self):
        registry = TaskRegistry()
        task_id = registry.register("grab", detail="准备中…")
        self.assertIsNone(registry.get(task_id)["label"])

    def test_fmt_task_list_shows_label(self):
        registry = TaskRegistry()
        registry.register("grab", detail="第 11 次失败（无容量），29 秒后重试。",
                          label="E2.1.Micro（新加坡）· AMD_2026-11-24")
        text = fmt_task_list(registry.list_all())
        self.assertIn("• E2.1.Micro（新加坡）· AMD_2026-11-24", text)
        self.assertIn("第 11 次失败（无容量），29 秒后重试。", text)
        self.assertNotIn("g1", text)
        self.assertNotIn("grab", text)
        self.assertNotIn("running", text)

    def test_fmt_task_list_fallback_no_label(self):
        registry = TaskRegistry()
        registry.register("grab", detail="准备中…")
        text = fmt_task_list(registry.list_all())
        self.assertIn("• 任务 g1", text)

    def test_fmt_task_list_empty(self):
        self.assertEqual(fmt_task_list([]), "当前没有运行中的任务。")

    def test_cancel_keyboard_uses_label(self):
        registry = TaskRegistry()
        registry.register("grab", detail="准备中…",
                          label="E2.1.Micro（新加坡）· AMD_2026-11-24")
        registry.register("grab", detail="准备中…")
        rows = _rows(task_list_keyboard(registry.list_all()))
        self.assertEqual(rows[0], [("❌ 取消 E2.1.Micro（新加坡）· AMD_2026-11-24",
                                    "t:cancel:g1")])
        self.assertEqual(rows[1], [("❌ 取消 g2", "t:cancel:g2")])


class TestWebTaskLabel(unittest.TestCase):
    """网页版任务注册表：label 进公开视图并落盘恢复。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = web_tasks.config.DATA_DIR
        web_tasks.config.DATA_DIR = self._tmp.name

    def tearDown(self):
        web_tasks.config.DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def test_create_and_public_label(self):
        registry = web_tasks.TaskRegistry()
        task_id = registry.create("grab", detail="抢机中",
                                  label="E2.1.Micro · AMD_2026-11-24")
        task = registry.get(task_id)
        self.assertEqual(task["label"], "E2.1.Micro · AMD_2026-11-24")

    def test_label_survives_reload(self):
        registry = web_tasks.TaskRegistry()
        task_id = registry.create("grab", detail="抢机中",
                                  label="E2.1.Micro · AMD_2026-11-24")
        # A fresh registry instance restores from tasks.json (status becomes cancelled, label is kept). 新注册表实例从 tasks.json 恢复（状态变 cancelled，label 保留）。
        restored = web_tasks.TaskRegistry()
        task = restored.get(task_id)
        self.assertEqual(task["label"], "E2.1.Micro · AMD_2026-11-24")

    def test_start_grab_task_label(self):
        registry = web_tasks.TaskRegistry()
        params = {"shape": E2_SHAPE, "display_name": "AMD_2026-11-24",
                  "availability_domain": "ad1", "image_id": "img",
                  "subnet_id": "sub", "ssh_keys_file": ""}
        with patch.object(web_tasks.threading, "Thread") as mock_thread_cls:
            task_id = web_tasks.start_grab_task(
                registry, MagicMock(), "ocid1.test", params,
                notify={"region_label": "新加坡"})
            mock_thread_cls.assert_called_once()
        task = registry.get(task_id)
        self.assertEqual(task["label"],
                         "E2.1.Micro（新加坡）· AMD_2026-11-24")


if __name__ == "__main__":
    unittest.main()
