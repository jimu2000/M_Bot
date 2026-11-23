#!/usr/bin/env python3
"""tgbot.handlers_extra 的单元测试。

全部用 mock，不碰真实 OCI API，不真跑抢机（Grabber 必须 mock）。

约定：
  - mock Update/Context：reply_text / edit_message_text / send_message
    用 AsyncMock；user_data / bot_data 用真实 dict。
  - 白名单：把 tgbot.config.ALLOWED_USER_IDS 临时设为 {TEST_USER_ID}。
  - oci 函数 patch tgbot.handlers_extra 命名空间；get_oci_client
    patch tgbot.config（handlers_extra 经 bot_config 调用它）。
  - TASKS 换成全新的 TaskRegistry，避免测试间互相干扰。
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import config as bot_config
from tgbot.services import TaskRegistry
import tgbot.handlers_extra as he
from oci.grabber import GrabCancelledError, GrabResult

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

# 菜单里本模块实现的 33 项（memballoon / memclear 由
# tgbot.handlers_client 实现，分发表指向该模块的 handler；
# openport / clients / restart / upgrade / objectstorage / email /
# plans 由阶段 5 工作包 D 实现）。
IMPLEMENTED_ITEMS = [
    "boot", "ip", "resume", "ipv6", "resize", "volumes", "users",
    "monitor", "autostart", "workflows", "profiles", "health",
    "presets", "subscription", "traffic", "quota", "tasks", "load",
    "cost", "mfa", "daily", "emails", "regions", "logs",
    "memballoon", "memclear",
    "openport", "clients", "restart", "upgrade",
    "objectstorage", "email", "plans",
]
# 本模块不实现的项（dispatch 应返回 False）。
NOT_IMPLEMENTED_ITEMS = [
    "delapi", "autoip", "disable", "instances", "nosuchkey",
]

FAKE_SUMMARY = {
    "a1": {"ocpu_used": 3, "ocpu_limit": 4, "ocpu_remaining": 1,
           "memory_used_gb": 12, "memory_limit_gb": 24,
           "memory_remaining_gb": 12, "instances": []},
    "e2": {"count": 1, "limit": 2, "remaining": 1, "instances": []},
    "boot_volume": {"used_gb": 100, "limit_gb": 200, "remaining_gb": 100,
                    "volumes": []},
    "overall_ok": True,
}

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaa1", "display-name": "web-server",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-20T00:00:00Z"},
    {"id": "ocid1.instance.oc1..bbb2", "display-name": "e2-test",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-21T00:00:00Z"},
]


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


def fake_cli_run(args, **kwargs):
    """按命令返回 canned 数据的 cli.run 替身（dispatch 冒烟测试用）。"""
    if args[:2] == ["usage-api", "usage-summary"]:
        return {"data": {"items": []}}
    if args[:2] == ["iam", "tenancy"]:
        return {"data": {"name": "test-tenancy", "home-region-key": "SIN",
                         "description": "test"}}
    if args[:2] == ["iam", "user"]:
        return {"data": []}
    if args[0] == "work-requests":
        return {"data": []}
    if args[:2] == ["iam", "region-subscription"]:
        return {"data": []}
    if args[:2] == ["network", "ipv6"]:
        return {"data": []}
    return {"data": []}


class HandlersExtraTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、新 TASKS、mock cli。"""

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

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tmp_path(self, name):
        return os.path.join(self._tmp.name, name)


class TestDispatch(HandlersExtraTestCase):

    async def test_dispatch_implemented_items_return_true(self):
        """33 项已实现菜单 dispatch 都返回 True 且有回复。"""
        import tgbot.handlers_client as hc
        with patch.object(he, "get_free_tier_summary",
                          return_value=FAKE_SUMMARY), \
             patch.object(he, "list_instances", return_value=[]), \
             patch.object(hc, "list_instances", return_value=[]), \
             patch.object(hc, "MEM_HOG_FILE",
                          self.tmp_path("no-such-mem-hog.json")), \
             patch.object(he, "list_public_ips", return_value=[]), \
             patch.object(he, "list_boot_volumes", return_value=[]), \
             patch.object(he, "list_orphan_boot_volumes", return_value=[]), \
             patch.object(he, "total_boot_volume_gb", return_value=0), \
             patch.object(he, "list_buckets", return_value=[]), \
             patch.object(he, "service_status", return_value="active"), \
             patch("web.config.DATA_DIR", self.tmp_path("data")), \
             patch.object(bot_config, "LOG_FILE",
                          self.tmp_path("no-such.log")), \
             patch.object(bot_config, "PRESETS_FILE",
                          self.tmp_path("no-such-presets.json")):
            self.cli.run.side_effect = fake_cli_run
            for item in IMPLEMENTED_ITEMS:
                with self.subTest(item=item):
                    update = make_update(callback_data=f"o:{item}")
                    context = make_context()
                    if item == "resize":
                        # resize 需要 pending_idx + 已存实例（E2 走固定规格分支）
                        context.user_data["instances"] = [FAKE_INSTANCES[1]]
                        context.user_data["pending_idx"] = 0
                    handled = await he.dispatch_oracle_action(
                        item, update, context)
                    self.assertTrue(handled, f"{item} 应返回 True")
                    self.assertIsNotNone(
                        reply_text_of(update), f"{item} 应回复消息")

    async def test_dispatch_not_implemented_returns_false(self):
        """未实现项 dispatch 返回 False 且不回复。"""
        for item in NOT_IMPLEMENTED_ITEMS:
            with self.subTest(item=item):
                update = make_update(callback_data=f"o:{item}")
                context = make_context()
                handled = await he.dispatch_oracle_action(
                    item, update, context)
                self.assertFalse(handled, f"{item} 应返回 False")
                update.effective_message.reply_text.assert_not_awaited()

    async def test_require_allowed_blocks_non_whitelisted(self):
        """不在白名单的用户被拒绝，handler 不执行。"""
        with patch.object(bot_config, "ALLOWED_USER_IDS", {999999}):
            update = make_update()
            context = make_context()
            with patch.object(he, "get_free_tier_summary") as mock_quota:
                await he.handle_quota(update, context)
                mock_quota.assert_not_called()
            self.assertIn("未授权", reply_text_of(update))


class TestQuotaTrafficCost(HandlersExtraTestCase):

    async def test_quota_formats_summary(self):
        update = make_update()
        context = make_context()
        with patch.object(he, "get_free_tier_summary",
                          return_value=FAKE_SUMMARY):
            await he.handle_quota(update, context)
        text = reply_text_of(update)
        self.assertIn("A1.Flex：3/4 核，12/24GB", text)
        self.assertIn("E2.1.Micro：1/2 台", text)
        self.assertIn("启动盘：100/200GB", text)

    async def test_quota_error_shows_friendly_message(self):
        update = make_update()
        context = make_context()
        with patch.object(he, "get_free_tier_summary",
                          side_effect=RuntimeError("boom")):
            await he.handle_quota(update, context)
        self.assertIn("出错了", reply_text_of(update))

    async def test_traffic_formats_monthly(self):
        items = [
            {"time-usage-started": "2026-09-05T00:00:00Z",
             "computed-amount": 1.5, "currency": "SGD",
             "service": "Compute", "computed-quantity": 10},
            {"time-usage-started": "2026-09-20T00:00:00Z",
             "computed-amount": 0.5, "currency": "SGD",
             "service": "Data Transfer", "computed-quantity": 12.5},
            {"time-usage-started": "2026-10-03T00:00:00Z",
             "computed-amount": 2.0, "currency": "SGD",
             "service": "Data Transfer", "computed-quantity": 3.25},
        ]
        self.cli.run.return_value = {"data": {"items": items}}
        update = make_update()
        context = make_context()
        await he.handle_traffic(update, context)
        text = reply_text_of(update)
        self.assertIn("2026-09：费用 2.00 SGD，出站流量 12.50 GB", text)
        self.assertIn("2026-10：费用 2.00 SGD，出站流量 3.25 GB", text)
        self.assertIn("合计：4.00 SGD", text)
        # 流量菜单用 MONTHLY 粒度
        called_args = self.cli.run.call_args[0][0]
        self.assertIn("MONTHLY", called_args)

    async def test_traffic_empty(self):
        self.cli.run.return_value = {"data": {"items": []}}
        update = make_update()
        context = make_context()
        await he.handle_traffic(update, context)
        self.assertIn("没有用量记录", reply_text_of(update))

    async def test_cost_total(self):
        items = [
            {"computed-amount": 13.46, "currency": "SGD"},
            {"computed-amount": 0.0, "currency": "SGD"},
        ]
        self.cli.run.return_value = {"data": {"items": items}}
        update = make_update()
        context = make_context()
        await he.handle_cost(update, context)
        text = reply_text_of(update)
        self.assertIn("近 3 个月总花费：13.46 SGD", text)
        called_args = self.cli.run.call_args[0][0]
        # 费用汇总也用 MONTHLY 粒度再求和（用量 API 没有 TOTAL 粒度，
        # 真实 API 实测报 "Unknown Granularity"）。
        self.assertIn("MONTHLY", called_args)

    def test_usage_time_window_is_midnight(self):
        """时间窗口必须截断到午夜（用量 API 要求时/分/秒为 0）。"""
        start, end = he.usage_time_window(months=3)
        self.assertTrue(start.endswith("T00:00:00Z"), start)
        self.assertTrue(end.endswith("T00:00:00Z"), end)
        self.assertLess(start, end)

    async def test_cost_empty(self):
        self.cli.run.return_value = {"data": {"items": []}}
        update = make_update()
        context = make_context()
        await he.handle_cost(update, context)
        self.assertIn("没有用量记录", reply_text_of(update))

    def test_fmt_usage_monthly_pure(self):
        """fmt_usage_monthly 是纯函数，直接测。"""
        self.assertIn("没有用量记录", he.fmt_usage_monthly([]))
        text = he.fmt_usage_monthly([
            {"time-usage-started": "2026-11-01T00:00:00Z",
             "computed-amount": 1, "currency": "USD",
             "service": "Data Transfer", "computed-quantity": 2}])
        self.assertIn("2026-11", text)
        self.assertIn("2.00 GB", text)

    def test_fmt_usage_total_pure(self):
        self.assertIn("没有用量记录", he.fmt_usage_total([]))
        self.assertIn("5.50 EUR", he.fmt_usage_total(
            [{"computed-amount": 5.5, "currency": "EUR"}]))


class TestHealthTasksProfiles(HandlersExtraTestCase):

    async def test_health_multi_profile_with_one_error(self):
        """两账号：一正常一报错，报错单列不影响另一个。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        cli2 = MagicMock()
        self._cli_patch.stop()

        def fake_get_client(profile=None, region=None):
            if profile == "ACCOUNT2":
                raise RuntimeError("auth failed")
            return cli2

        with patch.object(bot_config, "PROFILES", profiles), \
             patch.object(bot_config, "get_oci_client",
                          side_effect=fake_get_client), \
             patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            update = make_update()
            context = make_context()
            await he.handle_health(update, context)
        text = reply_text_of(update)
        self.assertIn("DEFAULT：2 台实例，1 台运行中", text)
        self.assertIn("ACCOUNT2", text)
        self.assertIn("auth failed", text)

    async def test_tasks_list_and_cancel(self):
        task_id = self.tasks.register("grab", detail="抢机测试中")
        update = make_update()
        context = make_context()
        await he.handle_tasks(update, context)
        text = reply_text_of(update)
        self.assertIn(task_id, text)
        self.assertIn("抢机测试中", text)

        # 取消一次成功
        update2 = make_update(callback_data=f"t:cancel:{task_id}")
        await he.task_cancel_callback(update2, make_context())
        self.assertIn("已取消", reply_text_of(update2))
        self.assertEqual(self.tasks.get(task_id)["status"], "cancelled")

        # 再取消已结束的任务失败
        update3 = make_update(callback_data=f"t:cancel:{task_id}")
        await he.task_cancel_callback(update3, make_context())
        self.assertIn("不存在或已结束", reply_text_of(update3))

    async def test_tasks_empty(self):
        update = make_update()
        context = make_context()
        await he.handle_tasks(update, context)
        self.assertIn("没有运行中的任务", reply_text_of(update))

    async def test_profile_switch(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        with patch.object(bot_config, "PROFILES", profiles):
            update = make_update()
            context = make_context()
            await he.handle_profiles(update, context)
            self.assertIn("DEFAULT", reply_text_of(update))

            update2 = make_update(callback_data="p:ACCOUNT2")
            await he.profile_callback(update2, context)
            self.assertEqual(context.user_data["profile"], "ACCOUNT2")
            self.assertIn("已切换到账号", reply_text_of(update2))

            # 未知 profile 切换失败
            update3 = make_update(callback_data="p:NOPE")
            await he.profile_callback(update3, context)
            self.assertIn("切换失败", reply_text_of(update3))


class TestBoot(HandlersExtraTestCase):

    async def test_handle_boot_sends_form_keyboard(self):
        """菜单 1 入口已改成按钮矩阵表单（不再是预设选择键盘）。"""
        update = make_update()
        context = make_context()
        await he.handle_boot(update, context)
        mock = update.effective_message.reply_text
        mock.assert_awaited_once()
        _, kwargs = mock.await_args
        markup = kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        callbacks = [b.callback_data for row in markup.inline_keyboard
                     for b in row]
        self.assertIn("bf:cpu:arm", callbacks)
        self.assertIn("bf:confirm", callbacks)
        self.assertIn("bf:save", callbacks)
        self.assertIsNotNone(context.user_data.get("boot_form"))

    async def test_boot_preset_callback_stores_preset_and_prechecks(self):
        update = make_update(callback_data="b:boot:e2")
        context = make_context()
        with patch.object(he, "can_launch", return_value=(True, "可以创建")):
            await he.boot_preset_callback(update, context)
        self.assertEqual(context.user_data["pending_boot"]["shape"],
                         "VM.Standard.E2.1.Micro")
        text = reply_text_of(update)
        self.assertIn("E2.1.Micro", text)
        self.assertIn("可以创建", text)
        self.assertIn("7J13", text)

    async def test_boot_preset_callback_unknown_preset(self):
        update = make_update(callback_data="b:boot:nope")
        context = make_context()
        await he.boot_preset_callback(update, context)
        self.assertIn("未知", reply_text_of(update))

    async def test_boot_preset_callback_quota_blocked(self):
        update = make_update(callback_data="b:boot:a1full")
        context = make_context()
        with patch.object(he, "can_launch",
                          return_value=(False, "A1 OCPU 不足")):
            await he.boot_preset_callback(update, context)
        self.assertIn("A1 OCPU 不足", reply_text_of(update))

    async def test_boot_go_without_preset_prompts(self):
        update = make_update(callback_data="b:go")
        context = make_context()
        await he.boot_go_callback(update, context)
        self.assertIn("请先在菜单 1 里配好开机表单", reply_text_of(update))
        self.assertEqual(self.tasks.list_all(), [])

    async def test_boot_go_starts_thread_and_registers_task(self):
        """mock Grabber：断言线程启动、任务注册、成功后保存预设。"""
        presets_file = self.tmp_path("presets.json")
        with patch.object(bot_config, "PRESETS_FILE", presets_file):
            update = make_update(callback_data="b:go")
            context = make_context()
            context.user_data["pending_boot"] = dict(he.BOOT_PRESETS["e2"])
            mock_grabber_cls = MagicMock()
            mock_grabber = mock_grabber_cls.return_value
            mock_grabber.run.return_value = GrabResult(
                instance_id="ocid1.instance.oc1..zzzz", attempts=3,
                releases=1, elapsed_seconds=120.0)
            with patch.object(he, "Grabber", mock_grabber_cls):
                await he.boot_go_callback(update, context)
                text = reply_text_of(update)
                self.assertIn("抢机已启动", text)
                self.assertIn("菜单 21", text)
                registered = self.tasks.list_all()
                self.assertEqual(len(registered), 1)
                task_id = registered[0]["id"]
                self.assertIn(task_id, text)
                # 等 daemon 线程跑完（mock Grabber 立即返回）。
                # 注意：整个等待过程必须留在 PRESETS_FILE 的 patch 里，
                # 否则线程里的 save_preset 会写到真实默认路径。
                for _ in range(50):
                    if self.tasks.get(task_id)["status"] == "done":
                        break
                    await asyncio.sleep(0.1)
                task = self.tasks.get(task_id)
                self.assertEqual(task["status"], "done")
                self.assertIn("尝试 3 次", task["detail"])
                self.assertIn("释放 1 台", task["detail"])
                mock_grabber_cls.assert_called_once()
                mock_grabber.run.assert_called_once()
                # 成功后保存了快捷开机配置（状态先置 done、再写文件，
                # 所以要轮询等文件写好，不能立即读）。
                saved = []
                for _ in range(50):
                    saved = he.load_presets(presets_file)
                    if len(saved) == 1:
                        break
                    await asyncio.sleep(0.1)
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0]["shape"],
                                 "VM.Standard.E2.1.Micro")

    async def test_boot_go_cancelled_marks_task(self):
        """Grabber 抛 GrabCancelledError 时任务状态为 cancelled。"""
        update = make_update(callback_data="b:go")
        context = make_context()
        context.user_data["pending_boot"] = dict(he.BOOT_PRESETS["a1small"])
        mock_grabber_cls = MagicMock()
        mock_grabber_cls.return_value.run.side_effect = GrabCancelledError(
            "cancelled")
        with patch.object(he, "Grabber", mock_grabber_cls):
            await he.boot_go_callback(update, context)
        task_id = self.tasks.list_all()[0]["id"]
        for _ in range(50):
            if self.tasks.get(task_id)["status"] == "cancelled":
                break
            await asyncio.sleep(0.1)
        self.assertEqual(self.tasks.get(task_id)["status"], "cancelled")


class TestHandleConfirm(HandlersExtraTestCase):

    async def test_confirm_changeip(self):
        update = make_update(callback_data="c:changeip:0")
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "change_public_ip",
                          return_value="203.0.113.10") as mock_change:
            handled = await he.handle_confirm("changeip", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_change.assert_called_once()
        text = reply_text_of(update)
        self.assertIn("203.0.113.10", text)
        self.assertIn("旧 IP 已释放", text)
        self.assertIn("DNS", text)

    async def test_confirm_changeip_no_instance(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("changeip", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("不存在或已过期", reply_text_of(update))

    async def test_confirm_resize(self):
        update = make_update(callback_data="c:resize:0")
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        context.user_data["pending_resize"] = {"ocpus": 2, "memory_gb": 12}
        handled = await he.handle_confirm("resize", "0", update, context)
        self.assertTrue(handled)
        called_args = self.cli.run.call_args[0][0]
        self.assertEqual(called_args[:3],
                         ["compute", "instance", "update"])
        shape_config = json.loads(
            called_args[called_args.index("--shape-config") + 1])
        self.assertEqual(shape_config, {"ocpus": 2, "memoryInGBs": 12})
        self.assertNotIn("pending_resize", context.user_data)
        self.assertIn("2 核 / 12GB", reply_text_of(update))

    async def test_confirm_resize_missing_target(self):
        update = make_update()
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        handled = await he.handle_confirm("resize", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("已过期", reply_text_of(update))
        self.cli.run.assert_not_called()

    async def test_confirm_delvol(self):
        update = make_update(callback_data="c:delvol:0")
        context = make_context()
        context.user_data["volumes"] = [
            {"id": "ocid1.bootvolume.oc1..vvv1", "display-name": "boot-vol",
             "size-in-gbs": 50, "lifecycle-state": "AVAILABLE"}]
        with patch.object(he, "delete_boot_volume",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("delvol", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_called_once()
        self.assertIn("已删除", reply_text_of(update))

    async def test_confirm_delmfa(self):
        update = make_update(callback_data="c:delmfa:0")
        context = make_context()
        devices = [
            {"user_id": "ocid1.user.oc1..u1", "user_name": "alice",
             "device": {"id": "ocid1.mfatotpdevice.oc1..d1"}},
            {"user_id": "ocid1.user.oc1..u2", "user_name": "bob",
             "device": {"id": "ocid1.mfatotpdevice.oc1..d2"}},
        ]
        with patch.object(he, "list_mfa_devices",
                          return_value=devices), \
             patch.object(he, "delete_mfa_device",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("delmfa", "0",
                                              update, context)
        self.assertTrue(handled)
        self.assertEqual(mock_delete.call_count, 2)
        self.assertIn("成功 2 个", reply_text_of(update))

    async def test_confirm_unknown_action_returns_false(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("explode", "0", update, context)
        self.assertFalse(handled)


class TestChangeipPrompt(HandlersExtraTestCase):
    """换 IP 确认提示（handle_changeip_prompt，dispatch 项 "changeip"）。

    协调员补的测试：实例操作 "i:ip:<idx>" 转发到这里弹确认，否则
    handle_confirm("changeip") 永远没有按钮能触发（集成检查发现）。
    """

    def _context_with_instance(self):
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        context.user_data["pending_idx"] = 0
        return context

    @staticmethod
    def _callback_data_list(update):
        """取 reply_text 最后一次调用的键盘里全部 callback_data。"""
        _args, kwargs = \
            update.effective_message.reply_text.await_args_list[-1]
        keyboard = kwargs.get("reply_markup")
        return [btn.callback_data for row in keyboard.inline_keyboard
                for btn in row]

    async def test_prompt_shows_current_ip_and_confirm_button(self):
        update = make_update(callback_data="i:ip:0")
        context = self._context_with_instance()
        with patch.object(he, "get_public_ip",
                          return_value="203.0.113.10"):
            await he.handle_changeip_prompt(update, context)
        text = reply_text_of(update)
        self.assertIn("确认给实例 web-server 换公网 IP", text)
        self.assertIn("203.0.113.10", text)
        self.assertIn("旧 IP 立即释放", text)
        self.assertIn("c:changeip:0", self._callback_data_list(update))

    async def test_prompt_no_instance(self):
        update = make_update(callback_data="i:ip:0")
        context = make_context()
        await he.handle_changeip_prompt(update, context)
        self.assertIn("请先在实例列表里选一台实例",
                      reply_text_of(update))

    async def test_prompt_ip_lookup_failure_still_prompts(self):
        update = make_update(callback_data="i:ip:0")
        context = self._context_with_instance()
        with patch.object(he, "get_public_ip",
                          side_effect=RuntimeError("boom")):
            await he.handle_changeip_prompt(update, context)
        text = reply_text_of(update)
        self.assertIn("确认给实例 web-server 换公网 IP", text)
        self.assertIn("无公网 IP", text)
        self.assertIn("c:changeip:0", self._callback_data_list(update))

    async def test_dispatch_changeip_returns_true(self):
        update = make_update(callback_data="i:ip:0")
        context = self._context_with_instance()
        with patch.object(he, "get_public_ip",
                          return_value="203.0.113.10"):
            handled = await he.dispatch_oracle_action(
                "changeip", update, context)
        self.assertTrue(handled)
        self.assertIn("确认给实例 web-server 换公网 IP",
                      reply_text_of(update))


class TestJobs(HandlersExtraTestCase):

    def _monitor_context(self, on=True, autostart=False):
        context = make_context()
        context.bot_data["monitor"] = {
            "on": on, "autostart": autostart, "chat_id": TEST_CHAT_ID}
        return context

    async def test_check_job_off_does_nothing(self):
        context = self._monitor_context(on=False)
        with patch.object(he, "list_instances") as mock_list:
            await he.check_instances_job(context)
            mock_list.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_check_job_first_run_only_baselines(self):
        """首次跑只记基线，不发状态变化通知。"""
        context = self._monitor_context()
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            await he.check_instances_job(context)
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(
            context.bot_data["last_states"]["ocid1.instance.oc1..aaa1"],
            "RUNNING")

    async def test_check_job_notifies_on_state_change(self):
        context = self._monitor_context()
        context.bot_data["last_states"] = {
            "ocid1.instance.oc1..aaa1": "RUNNING",
            "ocid1.instance.oc1..bbb2": "RUNNING",  # 上次是 RUNNING
        }
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            await he.check_instances_job(context)
        context.bot.send_message.assert_awaited_once()
        args, kwargs = context.bot.send_message.await_args
        self.assertEqual(kwargs["chat_id"], TEST_CHAT_ID)
        self.assertIn("e2-test", kwargs["text"])
        self.assertIn("RUNNING → STOPPED", kwargs["text"])
        self.assertEqual(
            context.bot_data["last_states"]["ocid1.instance.oc1..bbb2"],
            "STOPPED")

    async def test_check_job_autostart_starts_stopped(self):
        context = self._monitor_context(autostart=True)
        context.bot_data["last_states"] = {
            "ocid1.instance.oc1..aaa1": "RUNNING",
            "ocid1.instance.oc1..bbb2": "STOPPED",  # 基线已是 STOPPED，不通知
        }
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "start_instance",
                          return_value=None) as mock_start:
            await he.check_instances_job(context)
        mock_start.assert_called_once_with(
            self.cli, "ocid1.instance.oc1..bbb2")
        context.bot.send_message.assert_awaited_once()
        _, kwargs = context.bot.send_message.await_args
        self.assertIn("已自动启动", kwargs["text"])
        self.assertIn("e2-test", kwargs["text"])

    async def test_check_job_list_failure_notifies(self):
        context = self._monitor_context()
        with patch.object(he, "list_instances",
                          side_effect=RuntimeError("api down")):
            await he.check_instances_job(context)
        context.bot.send_message.assert_awaited_once()
        _, kwargs = context.bot.send_message.await_args
        self.assertIn("监控检查失败", kwargs["text"])

    async def test_daily_report_job(self):
        context = make_context()
        context.bot_data["daily_report"] = {"on": True,
                                            "chat_id": TEST_CHAT_ID}
        items = [{"computed-amount": 2.5, "currency": "SGD"}]
        with patch.object(he, "get_free_tier_summary",
                          return_value=FAKE_SUMMARY), \
             patch.object(he, "fetch_usage_summary", return_value=items):
            await he.daily_report_job(context)
        context.bot.send_message.assert_awaited_once()
        _, kwargs = context.bot.send_message.await_args
        self.assertEqual(kwargs["chat_id"], TEST_CHAT_ID)
        self.assertIn("每日报告", kwargs["text"])
        self.assertIn("A1.Flex：3/4 核", kwargs["text"])
        self.assertIn("2.50 SGD", kwargs["text"])

    async def test_daily_report_job_off_does_nothing(self):
        context = make_context()
        context.bot_data["daily_report"] = {"on": False,
                                            "chat_id": TEST_CHAT_ID}
        with patch.object(he, "get_free_tier_summary") as mock_quota:
            await he.daily_report_job(context)
            mock_quota.assert_not_called()
        context.bot.send_message.assert_not_awaited()


class TestPresetsLogsLoad(HandlersExtraTestCase):

    def test_presets_save_and_load(self):
        path = self.tmp_path("presets.json")
        self.assertEqual(he.load_presets(path), [])
        he.save_preset({"shape": "VM.Standard.E2.1.Micro",
                        "display_name": "e2-micro",
                        "ocpus": 1, "memory_gb": 1}, path=path)
        he.save_preset({"shape": "VM.Standard.A1.Flex",
                        "display_name": "a1-small",
                        "ocpus": 1, "memory_gb": 6}, path=path)
        presets = he.load_presets(path)
        self.assertEqual(len(presets), 2)
        self.assertEqual(presets[0]["display_name"], "e2-micro")
        self.assertIn("saved_at", presets[0])

    def test_load_presets_corrupt_file_returns_empty(self):
        path = self.tmp_path("bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(he.load_presets(path), [])

    async def test_handle_presets_lists_saved(self):
        path = self.tmp_path("presets.json")
        he.save_preset({"shape": "VM.Standard.A1.Flex",
                        "display_name": "a1-small",
                        "ocpus": 1, "memory_gb": 6}, path=path)
        with patch.object(bot_config, "PRESETS_FILE", path):
            update = make_update()
            context = make_context()
            await he.handle_presets(update, context)
        text = reply_text_of(update)
        self.assertIn("a1-small", text)
        self.assertIn("A1.Flex", text)

    async def test_handle_presets_empty(self):
        with patch.object(bot_config, "PRESETS_FILE",
                          self.tmp_path("none.json")):
            update = make_update()
            context = make_context()
            await he.handle_presets(update, context)
        self.assertIn("还没有保存", reply_text_of(update))

    async def test_logs_missing_file(self):
        with patch.object(bot_config, "LOG_FILE",
                          self.tmp_path("no-such.log")):
            update = make_update()
            context = make_context()
            await he.handle_logs(update, context)
        self.assertIn("日志文件不存在", reply_text_of(update))

    async def test_logs_tail_30_lines(self):
        path = self.tmp_path("bot.log")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(40):
                f.write(f"line {i}\n")
        with patch.object(bot_config, "LOG_FILE", path):
            update = make_update()
            context = make_context()
            await he.handle_logs(update, context)
        text = reply_text_of(update)
        self.assertIn("line 39", text)
        self.assertNotIn("line 0\n", text)
        self.assertIn("```", text)

    def test_read_log_tail_returns_none_when_missing(self):
        self.assertIsNone(he.read_log_tail(self.tmp_path("no.log")))

    async def test_handle_load(self):
        update = make_update()
        context = make_context()
        await he.handle_load(update, context)
        text = reply_text_of(update)
        self.assertIn("负载", text)
        self.assertIn("内存", text)

    def test_read_local_load(self):
        data = he.read_local_load()
        self.assertIsNotNone(data["load1"])
        self.assertIsNotNone(data["mem_total_mb"])
        self.assertGreater(data["mem_total_mb"], 0)


class TestIpIpv6ResumeResizeVolumes(HandlersExtraTestCase):

    async def test_handle_ip_lists_reserved_and_instance_ips(self):
        reserved = [{"id": "ocid1.publicip.oc1..p1",
                     "ip-address": "203.0.113.56", "lifetime": "RESERVED",
                     "lifecycle-state": "AVAILABLE",
                     "assigned-entity": None}]
        with patch.object(he, "list_public_ips",
                          return_value=reserved), \
             patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "get_public_ip",
                          return_value="203.0.113.139"):
            update = make_update()
            context = make_context()
            await he.handle_ip(update, context)
        text = reply_text_of(update)
        self.assertIn("203.0.113.56", text)
        self.assertIn("未绑定", text)
        self.assertIn("web-server：203.0.113.139", text)

    async def test_handle_ipv6_lists_per_instance(self):
        vnics = [{"id": "ocid1.vnic.oc1..v1", "display-name": "vnic",
                  "public-ip": "203.0.113.139", "private-ip": "10.0.0.5",
                  "is-primary": True}]
        self.cli.run.return_value = {"data": [
            {"ip-address": "2603:c024:c021:1d01::1",
             "lifecycle-state": "ASSIGNED"}]}
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "list_vnics", return_value=vnics):
            update = make_update()
            context = make_context()
            await he.handle_ipv6(update, context)
        text = reply_text_of(update)
        self.assertIn("2603:c024:c021:1d01::1", text)
        called_args = self.cli.run.call_args[0][0]
        self.assertEqual(called_args[:3], ["network", "ipv6", "list"])
        self.assertIn("ocid1.vnic.oc1..v1", called_args)

    def test_ipv6_create_delete_helpers(self):
        """create_ipv6 / delete_ipv6 只调 cli.run（mock，不发真实请求）。"""
        cli = MagicMock()
        cli.run.return_value = {"data": {"ip-address": "2603::2"}}
        result = he.create_ipv6(cli, "ocid1.vnic.oc1..v1")
        self.assertEqual(result["ip-address"], "2603::2")
        called_args = cli.run.call_args[0][0]
        self.assertEqual(called_args[:3], ["network", "ipv6", "create"])
        self.assertIn("ocid1.vnic.oc1..v1", called_args)

        he.delete_ipv6(cli, "ocid1.ipv6.oc1..i1")
        called_args = cli.run.call_args[0][0]
        self.assertEqual(called_args[:3], ["network", "ipv6", "delete"])
        self.assertIn("ocid1.ipv6.oc1..i1", called_args)
        self.assertIn("--force", called_args)

    async def test_handle_resume_lists_stopped_only(self):
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            update = make_update()
            context = make_context()
            await he.handle_resume(update, context)
        text = reply_text_of(update)
        self.assertIn("e2-test", text)
        self.assertNotIn("web-server", text)
        self.assertEqual(len(context.user_data["instances"]), 1)

    async def test_handle_resume_none_stopped(self):
        with patch.object(he, "list_instances",
                          return_value=[FAKE_INSTANCES[0]]):
            update = make_update()
            context = make_context()
            await he.handle_resume(update, context)
        self.assertIn("没有被暂停", reply_text_of(update))

    async def test_handle_resize_e2_fixed(self):
        update = make_update()
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[1]]
        context.user_data["pending_idx"] = 0
        await he.handle_resize(update, context)
        text = reply_text_of(update)
        self.assertIn("E2.1.Micro", text)
        self.assertIn("不支持升/降配", text)

    async def test_handle_resize_no_pending_idx(self):
        update = make_update()
        context = make_context()
        await he.handle_resize(update, context)
        self.assertIn("请先在实例列表里选一台实例", reply_text_of(update))

    async def test_handle_resize_a1_shows_current_and_presets(self):
        full = {"data": {"shape-config": {"ocpus": 1,
                                          "memory-in-gbs": 6}}}
        with patch.object(he, "get_instance", return_value=full):
            update = make_update()
            context = make_context()
            context.user_data["instances"] = [FAKE_INSTANCES[0]]
            context.user_data["pending_idx"] = 0
            await he.handle_resize(update, context)
        text = reply_text_of(update)
        self.assertIn("当前配置：1 核 / 6GB", text)
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        self.assertIsNotNone(keyboard)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("r:2:12:0", callbacks)
        self.assertIn("r:4:24:0", callbacks)

    async def test_resize_preset_callback_stores_target(self):
        update = make_update(callback_data="r:2:12:0")
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        await he.resize_preset_callback(update, context)
        self.assertEqual(context.user_data["pending_resize"],
                         {"ocpus": 2, "memory_gb": 12})
        text = reply_text_of(update)
        self.assertIn("2 核 / 12GB", text)
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("c:resize:0", callbacks)

    async def test_handle_volumes_lists_and_marks_orphans(self):
        volumes = [
            {"id": "ocid1.bootvolume.oc1..v1", "display-name": "boot-a",
             "size-in-gbs": 50, "lifecycle-state": "AVAILABLE",
             "availability-domain": "AD-1"},
            {"id": "ocid1.bootvolume.oc1..v2", "display-name": "boot-b",
             "size-in-gbs": 100, "lifecycle-state": "AVAILABLE",
             "availability-domain": "AD-1"},
        ]
        with patch.object(he, "list_boot_volumes",
                          return_value=volumes), \
             patch.object(he, "list_orphan_boot_volumes",
                          return_value=[volumes[1]]), \
             patch.object(he, "total_boot_volume_gb", return_value=150):
            update = make_update()
            context = make_context()
            await he.handle_volumes(update, context)
        text = reply_text_of(update)
        self.assertIn("共 150GB", text)
        self.assertIn("boot-a", text)
        self.assertIn("孤儿盘", text)
        self.assertEqual(len(context.user_data["volumes"]), 2)

    async def test_volume_detail_and_resize_flow(self):
        volumes = [{"id": "ocid1.bootvolume.oc1..v1",
                    "display-name": "boot-a", "size-in-gbs": 50,
                    "lifecycle-state": "AVAILABLE"}]
        context = make_context()
        context.user_data["volumes"] = volumes
        with patch.object(he, "get_boot_volume",
                          return_value={"display-name": "boot-a",
                                        "size-in-gbs": 50,
                                        "lifecycle-state": "AVAILABLE",
                                        "availability-domain": "AD-1"}):
            update = make_update(callback_data="v:detail:0")
            await he.volume_detail_callback(update, context)
        self.assertIn("boot-a", reply_text_of(update))
        self.assertIn("50GB", reply_text_of(update))

        # 选扩容 -> 显示目标容量按钮
        update2 = make_update(callback_data="v:resize:0")
        await he.volume_resize_callback(update2, context)
        self.assertIn("只能扩不能缩", reply_text_of(update2))

        # 选 100GB -> 执行扩容
        with patch.object(he, "resize_boot_volume",
                          return_value=None) as mock_resize:
            update3 = make_update(callback_data="v:volsize:0:100")
            await he.volume_size_callback(update3, context)
        mock_resize.assert_called_once_with(self.cli,
                                            "ocid1.bootvolume.oc1..v1", 100)
        self.assertIn("已扩容到 100GB", reply_text_of(update3))

    async def test_volume_size_smaller_than_current_rejected(self):
        context = make_context()
        context.user_data["volumes"] = [
            {"id": "ocid1.bootvolume.oc1..v1", "display-name": "boot-a",
             "size-in-gbs": 200, "lifecycle-state": "AVAILABLE"}]
        with patch.object(he, "resize_boot_volume") as mock_resize:
            update = make_update(callback_data="v:volsize:0:100")
            await he.volume_size_callback(update, context)
            mock_resize.assert_not_called()
        self.assertIn("只能扩不能缩", reply_text_of(update))

    async def test_volume_delete_callback_shows_warning(self):
        context = make_context()
        context.user_data["volumes"] = [
            {"id": "ocid1.bootvolume.oc1..v1", "display-name": "boot-a",
             "size-in-gbs": 50, "lifecycle-state": "AVAILABLE"}]
        update = make_update(callback_data="v:delete:0")
        await he.volume_delete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("永久丢失", text)
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("c:delvol:0", callbacks)


class TestIpv6Buttons(HandlersExtraTestCase):
    """菜单 4 IPv6 创建/删除按钮（handle_ipv6 + 两个回调 + delipv6 确认）。"""

    def _callbacks_of(self, update):
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        if keyboard is None:
            return []
        return [b.callback_data for row in keyboard.inline_keyboard
                for b in row]

    async def test_handle_ipv6_shows_create_and_delete_buttons(self):
        instances = [
            {"id": "ocid1.instance.oc1..aaa1", "display-name": "web-server",
             "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING"},
            {"id": "ocid1.instance.oc1..bbb2", "display-name": "db-server",
             "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING"},
        ]

        def fake_vnics(_cli, instance_id):
            suffix = "v1" if instance_id.endswith("aaa1") else "v2"
            return [{"id": f"ocid1.vnic.oc1..{suffix}",
                     "is-primary": True}]

        def fake_ipv6(_cli, vnic_id):
            if vnic_id.endswith("v1"):
                return []
            return [{"id": "ocid1.ipv6.oc1..i1",
                     "ip-address": "2001:db8::1",
                     "lifecycle-state": "ASSIGNED"}]

        with patch.object(he, "list_instances",
                          return_value=instances), \
             patch.object(he, "list_vnics", side_effect=fake_vnics), \
             patch.object(he, "list_ipv6_for_vnic", side_effect=fake_ipv6):
            update = make_update()
            context = make_context()
            await he.handle_ipv6(update, context)
        text = reply_text_of(update)
        self.assertIn("web-server：无 IPv6", text)
        self.assertIn("2001:db8::1", text)
        callbacks = self._callbacks_of(update)
        self.assertIn("ipv6_create:0", callbacks)
        self.assertIn("ipv6_delete:0", callbacks)
        self.assertIn("o:menu", callbacks)
        self.assertEqual(
            context.user_data["ipv6_vnics"],
            [{"name": "web-server", "vnic_id": "ocid1.vnic.oc1..v1"},
             {"name": "db-server", "vnic_id": "ocid1.vnic.oc1..v2"}])
        self.assertEqual(
            context.user_data["ipv6_entries"],
            [{"id": "ocid1.ipv6.oc1..i1", "ip": "2001:db8::1",
              "name": "db-server"}])

    async def test_ipv6_create_callback_success(self):
        update = make_update(callback_data="ipv6_create:0")
        context = make_context()
        context.user_data["ipv6_vnics"] = [
            {"name": "web-server", "vnic_id": "ocid1.vnic.oc1..v1"}]
        with patch.object(he, "create_ipv6",
                          return_value={"ip-address": "2001:db8::9",
                                        "id": "ocid1.ipv6.oc1..i9"}) \
                as mock_create:
            await he.ipv6_create_callback(update, context)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args[0][1],
                         "ocid1.vnic.oc1..v1")
        text = reply_text_of(update)
        self.assertIn("2001:db8::9", text)
        self.assertIn("web-server", text)

    async def test_ipv6_create_callback_expired(self):
        update = make_update(callback_data="ipv6_create:0")
        context = make_context()
        await he.ipv6_create_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_ipv6_delete_callback_shows_confirm(self):
        update = make_update(callback_data="ipv6_delete:0")
        context = make_context()
        context.user_data["ipv6_entries"] = [
            {"id": "ocid1.ipv6.oc1..i1", "ip": "2001:db8::1",
             "name": "web-server"}]
        await he.ipv6_delete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("2001:db8::1", text)
        self.assertIn("确认删除", text)
        self.assertIn("c:delipv6:0", self._callbacks_of(update))

    async def test_ipv6_delete_callback_expired(self):
        update = make_update(callback_data="ipv6_delete:0")
        context = make_context()
        await he.ipv6_delete_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_confirm_delipv6(self):
        update = make_update(callback_data="c:delipv6:0")
        context = make_context()
        context.user_data["ipv6_entries"] = [
            {"id": "ocid1.ipv6.oc1..i1", "ip": "2001:db8::1",
             "name": "web-server"}]
        with patch.object(he, "delete_ipv6",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("delipv6", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args[0][1],
                         "ocid1.ipv6.oc1..i1")
        text = reply_text_of(update)
        self.assertIn("已删除", text)
        self.assertIn("2001:db8::1", text)

    async def test_confirm_delipv6_expired(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("delipv6", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("已过期", reply_text_of(update))
        self.cli.run.assert_not_called()


class TestIpManagementButtons(HandlersExtraTestCase):
    """菜单 2 IP 管理：创建保留 IP + 删除未绑定保留 IP。"""

    def _callbacks_of(self, update):
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        if keyboard is None:
            return []
        return [b.callback_data for row in keyboard.inline_keyboard
                for b in row]

    async def test_handle_ip_shows_create_and_delete_buttons(self):
        reserved = [
            {"id": "ocid1.publicip.oc1..p1", "ip-address": "203.0.113.1",
             "lifecycle-state": "AVAILABLE", "assigned-entity": None},
            {"id": "ocid1.publicip.oc1..p2", "ip-address": "203.0.113.2",
             "lifecycle-state": "AVAILABLE",
             "assigned-entity": "ocid1.privateip.oc1..x1"},
            {"id": "ocid1.publicip.oc1..p3", "ip-address": "203.0.113.3",
             "lifecycle-state": "CREATING", "assigned-entity": None},
        ]
        instances = [
            {"id": "ocid1.instance.oc1..aaa1", "display-name": "web-server",
             "lifecycle-state": "RUNNING"},
        ]
        with patch.object(he, "list_public_ips",
                          return_value=reserved), \
             patch.object(he, "list_instances",
                          return_value=instances), \
             patch.object(he, "get_public_ip",
                          return_value="203.0.113.9"):
            update = make_update()
            context = make_context()
            await he.handle_ip(update, context)
        text = reply_text_of(update)
        # 现有列表文案与换 IP 提示保持不变。
        self.assertIn("203.0.113.1", text)
        self.assertIn("203.0.113.2", text)
        self.assertIn("换 IP：请在实例列表里选一台实例", text)
        callbacks = self._callbacks_of(update)
        self.assertIn("ip_create", callbacks)
        # 只有 AVAILABLE 且未绑定的那条挂删除按钮。
        self.assertIn("ip_delete:0", callbacks)
        self.assertNotIn("ip_delete:1", callbacks)
        self.assertIn("o:menu", callbacks)
        self.assertEqual(context.user_data["ip_reserved"], [
            {"id": "ocid1.publicip.oc1..p1", "ip": "203.0.113.1"}])

    async def test_ip_create_callback_success(self):
        update = make_update(callback_data="ip_create")
        context = make_context()
        with patch.object(he, "create_reserved_ip",
                          return_value=("ocid1.publicip.oc1..p9",
                                        "203.0.113.5")) as mock_create:
            await he.ip_create_callback(update, context)
        mock_create.assert_called_once()
        self.assertEqual(mock_create.call_args[0][2],
                         he.RESERVED_IP_DISPLAY_NAME)
        text = reply_text_of(update)
        self.assertIn("已创建保留 IP", text)
        self.assertIn("203.0.113.5", text)

    async def test_ip_create_callback_error(self):
        update = make_update(callback_data="ip_create")
        context = make_context()
        with patch.object(he, "create_reserved_ip",
                          side_effect=Exception("quota boom")):
            await he.ip_create_callback(update, context)
        self.assertIn("❌", reply_text_of(update))

    async def test_ip_delete_callback_shows_confirm(self):
        update = make_update(callback_data="ip_delete:0")
        context = make_context()
        context.user_data["ip_reserved"] = [
            {"id": "ocid1.publicip.oc1..p1", "ip": "203.0.113.1"}]
        await he.ip_delete_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("203.0.113.1", text)
        self.assertIn("确认删除", text)
        self.assertIn("c:delpubip:0", self._callbacks_of(update))

    async def test_ip_delete_callback_expired(self):
        update = make_update(callback_data="ip_delete:0")
        context = make_context()
        await he.ip_delete_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_confirm_delpubip(self):
        update = make_update(callback_data="c:delpubip:0")
        context = make_context()
        context.user_data["ip_reserved"] = [
            {"id": "ocid1.publicip.oc1..p1", "ip": "203.0.113.1"}]
        with patch.object(he, "delete_public_ip",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("delpubip", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args[0][1],
                         "ocid1.publicip.oc1..p1")
        text = reply_text_of(update)
        self.assertIn("已删除保留 IP", text)
        self.assertIn("203.0.113.1", text)

    async def test_confirm_delpubip_expired(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("delpubip", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("已过期", reply_text_of(update))
        self.cli.run.assert_not_called()


class TestUserManagement(HandlersExtraTestCase):
    """菜单 8 云账户管理：用户详情 + 改描述/改邮箱/API 密钥/删除用户。"""

    FAKE_USERS = [
        {"id": "ocid1.user.oc1..u1", "name": "alice",
         "email": "alice@example.com", "lifecycle-state": "ACTIVE",
         "description": "测试用户",
         "time-created": "2026-11-20T00:00:00Z"},
        {"id": "ocid1.user.oc1..u2", "name": "bob",
         "email": None, "lifecycle-state": "ACTIVE",
         "time-created": "2026-11-21T00:00:00Z"},
    ]

    FAKE_KEYS = [
        {"fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
         "lifecycle-state": "ACTIVE",
         "time-created": "2026-11-20T00:00:00Z"},
        {"fingerprint": "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00:11",
         "lifecycle-state": "INACTIVE",
         "time-created": "2026-11-19T00:00:00Z"},
    ]

    def _callbacks_of(self, update):
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        if keyboard is None:
            return []
        return [b.callback_data for row in keyboard.inline_keyboard
                for b in row]

    def _users_context(self):
        context = make_context()
        context.user_data["iam_users"] = [dict(u)
                                          for u in self.FAKE_USERS]
        return context

    async def test_handle_users_shows_detail_buttons(self):
        with patch.object(he, "list_users",
                          return_value=[dict(u)
                                        for u in self.FAKE_USERS]):
            update = make_update()
            context = make_context()
            await he.handle_users(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("bob", text)
        callbacks = self._callbacks_of(update)
        self.assertIn("u:detail:0", callbacks)
        self.assertIn("u:detail:1", callbacks)
        self.assertIn("o:menu", callbacks)
        self.assertEqual(len(context.user_data["iam_users"]), 2)

    async def test_user_detail_shows_actions(self):
        update = make_update(callback_data="u:detail:0")
        context = self._users_context()
        await he.user_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("alice@example.com", text)
        self.assertIn("测试用户", text)
        callbacks = self._callbacks_of(update)
        self.assertIn("u:desc:0", callbacks)
        self.assertIn("u:email:0", callbacks)
        self.assertIn("u:keys:0", callbacks)
        self.assertIn("u:del:0", callbacks)
        self.assertIn("u:list", callbacks)

    async def test_user_detail_expired(self):
        update = make_update(callback_data="u:detail:0")
        context = make_context()
        await he.user_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))

    async def test_user_desc_prompt_sets_pending_input(self):
        update = make_update(callback_data="u:desc:0")
        context = self._users_context()
        await he.user_callback(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "user_desc", "idx": "0"})
        self.assertIn("新描述", reply_text_of(update))

    async def test_user_email_prompt_sets_pending_input(self):
        update = make_update(callback_data="u:email:1")
        context = self._users_context()
        await he.user_callback(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "user_email", "idx": "1"})
        self.assertIn("新邮箱", reply_text_of(update))

    async def test_text_input_user_desc_confirm(self):
        update = make_update()
        update.effective_message.text = "新来的测试描述"
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "user_desc",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["pending_user_desc"],
                         "新来的测试描述")
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("确认把用户「alice」的描述改为",
                      reply_text_of(update))
        self.assertIn("c:userdesc:0", self._callbacks_of(update))

    async def test_text_input_user_desc_empty_cancels(self):
        update = make_update()
        update.effective_message.text = "   "
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "user_desc",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("不能为空", reply_text_of(update))
        self.assertNotIn("pending_user_desc", context.user_data)

    async def test_text_input_user_desc_too_long_cancels(self):
        update = make_update()
        update.effective_message.text = "x" * 256
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "user_desc",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("不能超过", reply_text_of(update))
        self.assertNotIn("pending_user_desc", context.user_data)

    async def test_text_input_user_email_confirm(self):
        update = make_update()
        update.effective_message.text = "new@example.com"
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "user_email",
                                              "idx": "1"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["pending_user_email"],
                         "new@example.com")
        self.assertIn("确认把用户「bob」的邮箱改为",
                      reply_text_of(update))
        self.assertIn("c:useremail:1", self._callbacks_of(update))

    async def test_text_input_user_email_invalid(self):
        update = make_update()
        update.effective_message.text = "not-an-email"
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "user_email",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("邮箱格式不对", reply_text_of(update))
        self.assertNotIn("pending_user_email", context.user_data)

    async def test_confirm_userdesc(self):
        update = make_update(callback_data="c:userdesc:0")
        context = self._users_context()
        context.user_data["pending_user_desc"] = "新来的测试描述"
        with patch.object(he, "update_user",
                          return_value={}) as mock_update:
            handled = await he.handle_confirm("userdesc", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        self.assertEqual(args[1], "ocid1.user.oc1..u1")
        self.assertEqual(kwargs["description"], "新来的测试描述")
        self.assertNotIn("pending_user_desc", context.user_data)
        self.assertIn("描述已更新", reply_text_of(update))

    async def test_confirm_useremail(self):
        update = make_update(callback_data="c:useremail:1")
        context = self._users_context()
        context.user_data["pending_user_email"] = "new@example.com"
        with patch.object(he, "update_user",
                          return_value={}) as mock_update:
            handled = await he.handle_confirm("useremail", "1",
                                              update, context)
        self.assertTrue(handled)
        mock_update.assert_called_once()
        args, kwargs = mock_update.call_args
        self.assertEqual(args[1], "ocid1.user.oc1..u2")
        self.assertEqual(kwargs["email"], "new@example.com")
        self.assertIn("邮箱已改为 new@example.com", reply_text_of(update))

    async def test_user_keys_list(self):
        update = make_update(callback_data="u:keys:0")
        context = self._users_context()
        with patch.object(he, "list_api_keys",
                          return_value=[dict(k) for k in self.FAKE_KEYS]):
            await he.user_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
                      text)
        self.assertIn("11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00:11",
                      text)
        self.assertIn("暂不支持上传", text)
        callbacks = self._callbacks_of(update)
        self.assertIn("u:keydel:0", callbacks)
        self.assertIn("u:keydel:1", callbacks)
        self.assertIn("u:detail:0", callbacks)
        self.assertIn("u:list", callbacks)
        self.assertEqual(len(context.user_data["iam_keys"]), 2)
        self.assertEqual(context.user_data["iam_keys_user"],
                         {"id": "ocid1.user.oc1..u1", "name": "alice"})

    async def test_user_keys_empty(self):
        update = make_update(callback_data="u:keys:1")
        context = self._users_context()
        with patch.object(he, "list_api_keys", return_value=[]):
            await he.user_callback(update, context)
        self.assertIn("没有 API 密钥", reply_text_of(update))

    async def test_user_key_delete_prompt(self):
        update = make_update(callback_data="u:keydel:0")
        context = make_context()
        context.user_data["iam_keys"] = [dict(k)
                                         for k in self.FAKE_KEYS]
        context.user_data["iam_keys_user"] = {"id": "ocid1.user.oc1..u1",
                                              "name": "alice"}
        await he.user_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
                      text)
        self.assertIn("c:delapikey:0", self._callbacks_of(update))

    async def test_confirm_delapikey(self):
        update = make_update(callback_data="c:delapikey:1")
        context = make_context()
        context.user_data["iam_keys"] = [dict(k)
                                         for k in self.FAKE_KEYS]
        context.user_data["iam_keys_user"] = {"id": "ocid1.user.oc1..u1",
                                              "name": "alice"}
        with patch.object(he, "delete_api_key",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("delapikey", "1",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_called_once()
        args, _kwargs = mock_delete.call_args
        self.assertEqual(args[1], "ocid1.user.oc1..u1")
        self.assertEqual(
            args[2], "11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00:11")
        text = reply_text_of(update)
        self.assertIn("已删除", text)
        self.assertIn("alice", text)

    async def test_confirm_delapikey_expired(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("delapikey", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("已过期", reply_text_of(update))
        self.cli.run.assert_not_called()

    async def test_user_delete_prompt_requires_typing(self):
        update = make_update(callback_data="u:del:0")
        context = self._users_context()
        with patch.object(he, "get_profile_user_id", return_value=None):
            await he.user_callback(update, context)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "deluser", "idx": "0"})
        text = reply_text_of(update)
        self.assertIn("完整输入用户名「alice」", text)

    async def test_user_delete_prompt_refuses_current_api_user(self):
        update = make_update(callback_data="u:del:0")
        context = self._users_context()
        with patch.object(he, "get_profile_user_id",
                          return_value="ocid1.user.oc1..u1"):
            await he.user_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        text = reply_text_of(update)
        self.assertIn("不能删除", text)
        self.assertIn("正在使用", text)

    async def test_text_input_deluser_match(self):
        update = make_update()
        update.effective_message.text = "alice"
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "deluser",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("最后确认：删除用户「alice」", reply_text_of(update))
        self.assertIn("c:deluser:0", self._callbacks_of(update))

    async def test_text_input_deluser_mismatch(self):
        update = make_update()
        update.effective_message.text = "bob"
        context = self._users_context()
        context.user_data["pending_input"] = {"kind": "deluser",
                                              "idx": "0"}
        await he.text_input_callback(update, context)
        self.assertIn("与实际不符", reply_text_of(update))
        self.assertNotIn("pending_input", context.user_data)

    async def test_confirm_deluser(self):
        update = make_update(callback_data="c:deluser:1")
        context = self._users_context()
        with patch.object(he, "get_profile_user_id",
                          return_value=None), \
             patch.object(he, "delete_user",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("deluser", "1",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args[0][1],
                         "ocid1.user.oc1..u2")
        self.assertIn("用户「bob」已删除", reply_text_of(update))

    async def test_confirm_deluser_refuses_current_api_user(self):
        update = make_update(callback_data="c:deluser:0")
        context = self._users_context()
        with patch.object(he, "get_profile_user_id",
                          return_value="ocid1.user.oc1..u1"), \
             patch.object(he, "delete_user",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("deluser", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_not_called()
        self.assertIn("不能删除", reply_text_of(update))

    async def test_user_list_back_button(self):
        update = make_update(callback_data="u:list")
        context = make_context()
        with patch.object(he, "list_users",
                          return_value=[dict(u)
                                        for u in self.FAKE_USERS]):
            await he.user_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("bob", text)
        self.assertIn("u:detail:0", self._callbacks_of(update))


class TestUsersWorkflowsRegionsSubscriptionMfa(HandlersExtraTestCase):

    async def test_handle_users(self):
        self.cli.run.return_value = {"data": [
            {"name": "alice", "email": "alice@example.com",
             "lifecycle-state": "ACTIVE", "id": "ocid1.user.oc1..u1"},
            {"name": "bob", "email": None,
             "lifecycle-state": "ACTIVE", "id": "ocid1.user.oc1..u2"},
        ]}
        update = make_update()
        context = make_context()
        await he.handle_users(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("alice@example.com", text)
        self.assertIn("bob", text)

    async def test_handle_emails(self):
        self.cli.run.return_value = {"data": [
            {"name": "alice", "email": "alice@example.com",
             "id": "ocid1.user.oc1..u1"},
            {"name": "bob", "email": None, "id": "ocid1.user.oc1..u2"},
        ]}
        update = make_update()
        context = make_context()
        await he.handle_emails(update, context)
        text = reply_text_of(update)
        self.assertIn("alice@example.com", text)
        self.assertNotIn("bob", text)

    async def test_handle_workflows_marks_failed(self):
        self.cli.run.return_value = {"data": [
            {"operation-type": "CREATE_INSTANCE", "status": "SUCCEEDED",
             "time-accepted": "2026-11-23T01:00:00Z"},
            {"operation-type": "DELETE_VOLUME", "status": "FAILED",
             "time-accepted": "2026-11-23T02:00:00Z"},
        ]}
        update = make_update()
        context = make_context()
        await he.handle_workflows(update, context)
        text = reply_text_of(update)
        self.assertIn("CREATE_INSTANCE：SUCCEEDED", text)
        self.assertIn("❌ DELETE_VOLUME：FAILED", text)

    async def test_handle_regions(self):
        self.cli.run.return_value = {"data": [
            {"region-name": "ap-singapore-1", "is-home-region": True,
             "status": "READY"},
            {"region-name": "us-sanjose-1", "is-home-region": False,
             "status": "READY"},
        ]}
        update = make_update()
        context = make_context()
        await he.handle_regions(update, context)
        text = reply_text_of(update)
        self.assertIn("ap-singapore-1（Home 区域）", text)
        self.assertIn("us-sanjose-1", text)

    async def test_handle_subscription(self):
        self.cli.run.return_value = {"data": {
            "name": "my-tenancy", "home-region-key": "SIN",
            "description": "test tenancy"}}
        update = make_update()
        context = make_context()
        await he.handle_subscription(update, context)
        text = reply_text_of(update)
        self.assertIn("my-tenancy", text)
        self.assertIn("SIN", text)
        self.assertIn("test tenancy", text)

    async def test_handle_mfa_lists_devices(self):
        def fake_run(args, **kwargs):
            if args[:2] == ["iam", "user"]:
                return {"data": [
                    {"name": "alice", "id": "ocid1.user.oc1..u1"}]}
            if args[:2] == ["iam", "mfa-totp-device"]:
                return {"data": [
                    {"id": "ocid1.mfatotpdevice.oc1..d1",
                     "lifecycle-state": "ACTIVE",
                     "time-created": "2026-11-01T00:00:00Z"}]}
            return {"data": []}

        self.cli.run.side_effect = fake_run
        update = make_update()
        context = make_context()
        await he.handle_mfa(update, context)
        text = reply_text_of(update)
        self.assertIn("alice", text)
        self.assertIn("ACTIVE", text)
        _, kwargs = update.effective_message.reply_text.await_args
        keyboard = kwargs.get("reply_markup")
        callbacks = [b.callback_data for row in keyboard.inline_keyboard
                     for b in row]
        self.assertIn("c:delmfa:0", callbacks)

    async def test_handle_mfa_no_devices(self):
        self.cli.run.return_value = {"data": []}
        update = make_update()
        context = make_context()
        await he.handle_mfa(update, context)
        self.assertIn("没有任何 MFA", reply_text_of(update))


class TestMonitorToggles(HandlersExtraTestCase):

    async def test_monitor_toggle_records_chat_id(self):
        update = make_update()
        context = make_context()
        await he.handle_monitor(update, context)
        state = context.bot_data["monitor"]
        self.assertTrue(state["on"])
        self.assertEqual(state["chat_id"], TEST_CHAT_ID)
        self.assertIn("已开启", reply_text_of(update))
        await he.handle_monitor(update, context)
        self.assertFalse(context.bot_data["monitor"]["on"])
        self.assertIn("已关闭", reply_text_of(update))

    async def test_autostart_toggle_also_enables_monitor(self):
        update = make_update()
        context = make_context()
        await he.handle_autostart(update, context)
        state = context.bot_data["monitor"]
        self.assertTrue(state["autostart"])
        self.assertTrue(state["on"])  # 开自启一并开监控
        self.assertEqual(state["chat_id"], TEST_CHAT_ID)

    async def test_daily_toggle(self):
        update = make_update()
        context = make_context()
        await he.handle_daily(update, context)
        state = context.bot_data["daily_report"]
        self.assertTrue(state["on"])
        self.assertEqual(state["chat_id"], TEST_CHAT_ID)


if __name__ == "__main__":
    unittest.main()
