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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import config as bot_config
from tgbot.keyboards import (profile_keyboard, quota_accounts_keyboard,
                             quota_regions_keyboard)
from tgbot.services import TaskRegistry
import tgbot.handlers_extra as he
from oci.grabber import GrabCancelledError, GrabResult

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

IMPLEMENTED_ITEMS = [
    "boot", "ip", "resume", "ipv6", "resize", "volumes", "users",
    "monitor", "autostart", "workflows", "profiles", "health",
    "presets", "subscription", "traffic", "quota", "tasks", "load",
    "cost", "mfa", "daily", "emails", "regions", "logs",
    "memballoon", "memclear",
    "openport", "clients", "restart", "upgrade",
    "objectstorage", "email", "plans", "bark", "autoip",
]
NOT_IMPLEMENTED_ITEMS = [
    "delapi", "disable", "instances", "nosuchkey",
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
    都是 AsyncMock；回调时 query.edit_message_text 也是 AsyncMock，
    query.message.message_id 固定为 777（切换流程编辑/删除断言用）。"""
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
    """mock Context：user_data / bot_data 用真实 dict，bot.send_message
    与 bot.delete_message 用 AsyncMock。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def edit_text_of(update):
    """取 update 上 query.edit_message_text 最后一次调用的文本。"""
    query = update.callback_query
    if query is None:
        return None
    mock = query.edit_message_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def edit_button_texts_of(update):
    """取 update 上 query.edit_message_text 最后一次调用的键盘按钮文字。"""
    query = update.callback_query
    if query is None:
        return []
    mock = query.edit_message_text
    if not mock.await_args_list:
        return []
    _args, kwargs = mock.await_args_list[-1]
    keyboard = kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [btn.text
            for row in keyboard.inline_keyboard for btn in row]


def edit_callback_data_of(update):
    """取 update 上 query.edit_message_text 最后一次调用的键盘 callback_data。"""
    query = update.callback_query
    if query is None:
        return []
    mock = query.edit_message_text
    if not mock.await_args_list:
        return []
    _args, kwargs = mock.await_args_list[-1]
    keyboard = kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


def reply_text_of(update):
    """取 update 上 reply_text 最后一次调用的文本（没调过返回 None）。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def reply_button_texts_of(update):
    """取 update 上 reply_text 最后一次调用的键盘按钮文字。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return []
    _args, kwargs = mock.await_args_list[-1]
    keyboard = kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [btn.text
            for row in keyboard.inline_keyboard for btn in row]


def reply_callback_data_of(update):
    """取 update 上 reply_text 最后一次调用的键盘 callback_data。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return []
    _args, kwargs = mock.await_args_list[-1]
    keyboard = kwargs.get("reply_markup")
    if keyboard is None:
        return []
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


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
                        context.user_data["instances"] = [FAKE_INSTANCES[1]]
                        context.user_data["pending_idx"] = 0
                    handled = await he.dispatch_oracle_action(
                        item, update, context)
                    self.assertTrue(handled, f"{item} 应返回 True")
                    self.assertIsNotNone(
                        reply_text_of(update) or edit_text_of(update),
                        f"{item} 应回复消息")

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

    async def test_quota_shows_account_list(self):
        """菜单 20 改版（v1.7.1）：先出账号列表，每个账号点开看详情。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 3, "ACCOUNT2": 1}):
            update = make_update()
            context = make_context()
            await he.handle_quota(update, context)
        self.assertIn("点下面账号查看", reply_text_of(update))
        texts = reply_button_texts_of(update)
        self.assertIn("新加坡（3 个区）", texts)
        self.assertIn("圣何塞", texts)
        self.assertIn("q:acct:DEFAULT", reply_callback_data_of(update))
        self.assertIn("q:acct:ACCOUNT2", reply_callback_data_of(update))

    async def test_handle_quota_answers_before_slow_lookup(self):
        """额度入口先应答再查 OCI：按钮不等查询完成就停转。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        update = make_update(callback_data="o:quota")
        context = make_context()
        seen = []

        def _slow_counts(*args, **kwargs):
            seen.append(update.callback_query.answer.await_count)
            return {"DEFAULT": 3, "ACCOUNT2": 1}

        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             side_effect=_slow_counts):
            await he.handle_quota(update, context)
        self.assertEqual(seen, [1])
        self.assertIn("点下面账号查看", reply_text_of(update))

    async def test_traffic_shows_vnstat_entries(self):
        """菜单 19 改版（v1.8.0）：vnstat 本月流量，不再用用量 API。"""
        entries = [
            {"name": "web-server-1", "public_ip": "203.0.113.11",
             "city": "新加坡", "rx_gb": 3.21, "tx_gb": 6.73,
             "error": None},
            {"name": "e2-test-1", "public_ip": "203.0.113.12",
             "city": "新加坡", "rx_gb": None, "tx_gb": None,
             "error": "SSH 连不上该实例"},
        ]
        update = make_update()
        context = make_context()
        with patch.object(he, "_fetch_instance_traffic",
                          return_value=entries):
            await he.handle_traffic(update, context)
        text = reply_text_of(update)
        self.assertIn("本月流量（vnstat）", text)
        self.assertIn("IP: 203.0.113.11（新加坡）", text)
        self.assertIn("当月入站: 3.21 GiB / 当月出站: 6.73 GiB", text)
        self.assertIn("IP: 203.0.113.12（新加坡）", text)
        self.assertIn("查不到（SSH 连不上该实例）", text)
        self.assertIn("合计：入站 3.21 GiB / 出站 6.73 GiB（1 台）", text)

    async def test_traffic_no_instances(self):
        update = make_update()
        context = make_context()
        with patch.object(he, "_fetch_instance_traffic", return_value=[]):
            await he.handle_traffic(update, context)
        self.assertIn("没有运行中的实例", reply_text_of(update))

    async def test_cost_total(self):
        items = [
            {"computed-amount": 13.46, "currency": "SGD"},
            {"computed-amount": 0.0, "currency": "SGD"},
        ]
        self.cli.run.return_value = {"data": {"items": items}}
        update = make_update()
        context = make_context()
        # The test only checks the cost-total logic, so pin the account set to one Singapore account. 测试只测费用汇总逻辑，账号集固定为 1 个新加坡账号。
        profiles = {"DEFAULT": {"region": "ap-singapore-1",
                                "compartment_id": "ocid1.tenancy.oc1..aaa"}}
        with patch.object(bot_config, "PROFILES", profiles):
            await he.handle_cost(update, context)
        text = reply_text_of(update)
        self.assertIn("• 新加坡：13.46 SGD", text)
        self.assertIn("合计：13.46 SGD", text)
        called_args = self.cli.run.call_args[0][0]
        # Cost totals are also summed at MONTHLY granularity (the usage API has no TOTAL granularity; the real API reports "Unknown Granularity"). 费用汇总也用 MONTHLY 粒度再求和（用量 API 没有 TOTAL 粒度，真实 API 实测报 "Unknown Granularity"）。
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
        # Same as test_cost_total: pin the account set so it does not vary with the local oci_config. 同 test_cost_total：账号集固定，避免随本机 oci_config 变化。
        profiles = {"DEFAULT": {"region": "ap-singapore-1",
                                "compartment_id": "ocid1.tenancy.oc1..aaa"}}
        with patch.object(bot_config, "PROFILES", profiles):
            await he.handle_cost(update, context)
        text = reply_text_of(update)
        self.assertIn("• 新加坡：0.00", text)
        self.assertIn("合计：0.00", text)

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


class TestInstanceTraffic(HandlersExtraTestCase):

    VNSTAT_SAMPLE = ('{"interfaces": [{"name": "enp0s6", "traffic": '
                     '{"month": [{"date": {"year": 2026, "month": 11}, '
                     '"rx": 3446363443, "tx": 7227582128}]}}]}')

    def test_fmt_instance_traffic_pure(self):
        self.assertIn("没有运行中的实例", he.fmt_instance_traffic([]))
        text = he.fmt_instance_traffic([
            {"name": "a", "public_ip": "203.0.113.4", "city": "新加坡",
             "rx_gb": 1.0, "tx_gb": 2.0, "error": None},
            {"name": "b", "public_ip": None, "city": "未知地区",
             "rx_gb": None, "tx_gb": None, "error": "无公网 IP"},
        ])
        self.assertIn("IP: 203.0.113.4（新加坡）", text)
        self.assertIn("当月入站: 1.00 GiB / 当月出站: 2.00 GiB", text)
        self.assertIn("IP: 无公网 IP（未知地区）", text)
        self.assertIn("查不到（无公网 IP）", text)
        self.assertIn("合计：入站 1.00 GiB / 出站 2.00 GiB（1 台）", text)

    async def test_fetch_instance_traffic(self):
        """每台查城市 + SSH vnstat，失败的台单独标 error。"""
        instances = [
            {"display-name": "web", "lifecycle-state": "RUNNING",
             "public-ip": "203.0.113.4"},
            {"display-name": "dead", "lifecycle-state": "RUNNING",
             "public-ip": "5.6.7.8"},
            {"display-name": "stopped", "lifecycle-state": "STOPPED",
             "public-ip": "9.9.9.9"},
        ]
        # The fixture has only one month of data (2026-11); the parser defaults to it (falling back to the last entry when there is no current month), so this test is stable whenever it runs. 示例只有 2026-11 一条月份数据，解析器默认取该条（无当月时取最后一条，所以任何时间跑这个测试都稳定）。
        context = make_context()
        with patch.object(bot_config, "GUARD_SSH_KEY", "/root/.ssh/rbot_guard"), \
                patch.object(he, "fetch_city_for_ip",
                             side_effect=lambda ip: {"203.0.113.4": "新加坡",
                                                     "5.6.7.8": "圣何塞"}.get(ip, "未知地区")), \
                patch.object(he, "run_remote_command") as mock_cmd:
            def _cmd(host, command):
                if host["host"] == "203.0.113.4":
                    return (0, self.VNSTAT_SAMPLE, "")
                raise RuntimeError("ssh refused")
            mock_cmd.side_effect = _cmd
            entries = await he._fetch_instance_traffic(context, instances)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["city"], "新加坡")
        self.assertAlmostEqual(entries[0]["rx_gb"], 3.21, places=2)
        self.assertAlmostEqual(entries[0]["tx_gb"], 6.73, places=2)
        self.assertIsNone(entries[0]["error"])
        self.assertEqual(entries[1]["city"], "圣何塞")
        self.assertIsNone(entries[1]["rx_gb"])
        self.assertEqual(entries[1]["error"], "SSH 连不上该实例")

    async def test_fetch_instance_traffic_no_key(self):
        """未配 SSH 密钥时每台都标“未配置 SSH 密钥”，不尝试 SSH。"""
        instances = [{"display-name": "web", "lifecycle-state": "RUNNING",
                      "public-ip": "203.0.113.4"}]
        context = make_context()
        with patch.object(bot_config, "GUARD_SSH_KEY", ""), \
                patch.object(he, "fetch_city_for_ip",
                             return_value="新加坡"), \
                patch.object(he, "run_remote_command") as mock_cmd:
            entries = await he._fetch_instance_traffic(context, instances)
        mock_cmd.assert_not_called()
        self.assertEqual(entries[0]["error"], "未配置 SSH 密钥")

    async def test_fetch_traffic_auto_installs_vnstat(self):
        """v1.16.0：vnstat 没装时自动安装后重试，成功后标 installed。"""
        instances = [{"display-name": "new", "lifecycle-state": "RUNNING",
                      "public-ip": "203.0.113.4"}]
        context = make_context()
        calls = []
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "fetch_city_for_ip",
                             return_value="首尔"), \
                patch.object(he, "run_remote_command") as mock_cmd:
            def _cmd(host, command, timeout=30):
                calls.append(command)
                if "apt-get install" in command:
                    return (0, "", "")
                if len([c for c in calls if "apt-get" not in c]) == 1:
                    return (127, "", "sh: 1: vnstat: not found")
                return (0, self.VNSTAT_SAMPLE, "")
            mock_cmd.side_effect = _cmd
            entries = await he._fetch_instance_traffic(context, instances)
        self.assertEqual(len(calls), 4)
        self.assertTrue(any("apt-get install -y vnstat python3 curl" in c
                            for c in calls))
        self.assertTrue(entries[0]["installed"])
        self.assertIsNone(entries[0]["error"])
        self.assertAlmostEqual(entries[0]["rx_gb"], 3.21, places=2)
        text = he.fmt_instance_traffic(entries)
        self.assertIn("vnstat 刚自动安装", text)

    async def test_fetch_traffic_install_failure(self):
        """v1.16.0：自动安装失败时标明原因，不再重试查询。"""
        instances = [{"display-name": "new", "lifecycle-state": "RUNNING",
                      "public-ip": "203.0.113.4"}]
        context = make_context()
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "fetch_city_for_ip",
                             return_value="首尔"), \
                patch.object(he, "run_remote_command") as mock_cmd:
            def _cmd(host, command, timeout=30):
                if "apt-get install" in command:
                    return (100, "", "E: Unable to fetch archives")
                return (127, "", "vnstat: command not found")
            mock_cmd.side_effect = _cmd
            entries = await he._fetch_instance_traffic(context, instances)
        self.assertEqual(mock_cmd.call_count, 2)
        self.assertIsNone(entries[0].get("installed"))
        self.assertEqual(entries[0]["error"], "vnstat 未安装，自动安装失败")

    async def test_fetch_traffic_other_failure_no_install(self):
        """v1.16.0：非“没装”原因的查询失败不触发自动安装。"""
        instances = [{"display-name": "web", "lifecycle-state": "RUNNING",
                      "public-ip": "203.0.113.4"}]
        context = make_context()
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "fetch_city_for_ip",
                             return_value="新加坡"), \
                patch.object(he, "run_remote_command",
                             return_value=(1, "", "GUARD-ERROR no-interface")) as mock_cmd:
            entries = await he._fetch_instance_traffic(context, instances)
        mock_cmd.assert_called_once()
        self.assertEqual(entries[0]["error"], "vnstat 查询失败")

    def test_fmt_instance_traffic_guard_line(self):
        """v1.21.0：每台流量下面带看门狗状态行。"""
        text = he.fmt_instance_traffic([
            {"name": "a", "public_ip": "203.0.113.4", "city": "新加坡",
             "rx_gb": 1.0, "tx_gb": 2.0, "error": None,
             "guard": {"installed": True, "auto": True, "enabled": True,
                       "mode": 0, "tripped": False, "limited": False}},
            {"name": "b", "public_ip": "5.6.7.8", "city": "圣何塞",
             "rx_gb": 0.5, "tx_gb": 1.0, "error": None,
             "guard": {"installed": False, "auto": False, "enabled": False,
                       "mode": None, "tripped": False, "limited": False}},
        ])
        self.assertIn("看门狗：已开启（阻断模式）", text)
        self.assertIn("看门狗：未安装", text)

    def test_traffic_keyboard_toggle_labels(self):
        """v1.21.0：开关按钮按当前状态反着显示动作。"""
        entries = [
            {"name": "web", "public_ip": "203.0.113.4",
             "guard": {"installed": True, "enabled": True}},
            {"name": "e2", "public_ip": "5.6.7.8",
             "guard": {"installed": False, "enabled": False}},
            {"name": "noip", "public_ip": None, "guard": None},
        ]
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"):
            keyboard = he.traffic_keyboard(entries)
        rows = keyboard.inline_keyboard
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0].text, "🛡️ web：关闭看门狗")
        self.assertEqual(rows[0][0].callback_data, "wd:toggle:0")
        self.assertEqual(rows[1][0].text, "🛡️ e2：开启看门狗")
        self.assertEqual(rows[1][0].callback_data, "wd:toggle:1")
        self.assertEqual(rows[2][0].callback_data, "o:menu")

    def test_traffic_keyboard_no_key(self):
        """未配 SSH 密钥时不给开关按钮（装不了），只剩返回按钮。"""
        entries = [{"name": "web", "public_ip": "203.0.113.4",
                    "guard": {"installed": False, "enabled": False}}]
        with patch.object(bot_config, "GUARD_SSH_KEY", ""):
            keyboard = he.traffic_keyboard(entries)
        self.assertEqual(len(keyboard.inline_keyboard), 1)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data,
                         "o:menu")


class TestWatchdogCallback(HandlersExtraTestCase):
    """v1.21.0：菜单 19 看门狗开关回调。"""

    def _entries(self, enabled=False):
        return [{"name": "web", "public_ip": "203.0.113.11",
                 "city": "新加坡", "rx_gb": 1.0, "tx_gb": 2.0,
                 "error": None,
                 "guard": {"installed": True, "auto": True,
                           "enabled": enabled, "mode": 0,
                           "tripped": False, "limited": False}}]

    async def test_enable_installs_and_refreshes(self):
        update = make_update(callback_data="wd:toggle:0")
        context = make_context()
        context.user_data["traffic_entries"] = self._entries(enabled=False)
        fresh = self._entries(enabled=True)
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "install_watchdog",
                             return_value={"action": "installed",
                                           "test_ok": True,
                                           "mode": 0}) as mock_install, \
                patch.object(he, "_fetch_instance_traffic",
                             return_value=fresh):
            await he.watchdog_callback(update, context)
        mock_install.assert_called_once()
        text = edit_text_of(update)
        self.assertIn("已安装并开启", text)
        self.assertIn("看门狗：已开启（阻断模式）", text)
        self.assertEqual(context.user_data["traffic_entries"], fresh)

    async def test_reenable_says_reenabled(self):
        update = make_update(callback_data="wd:toggle:0")
        context = make_context()
        context.user_data["traffic_entries"] = self._entries(enabled=False)
        fresh = self._entries(enabled=True)
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "install_watchdog",
                             return_value={"action": "enabled",
                                           "test_ok": True,
                                           "mode": 0}) as mock_install, \
                patch.object(he, "_fetch_instance_traffic",
                             return_value=fresh):
            await he.watchdog_callback(update, context)
        mock_install.assert_called_once()
        text = edit_text_of(update)
        self.assertIn("已重新开启", text)
        self.assertNotIn("已升级", text)

    async def test_disable_closes_and_refreshes(self):
        update = make_update(callback_data="wd:toggle:0")
        context = make_context()
        context.user_data["traffic_entries"] = self._entries(enabled=True)
        fresh = self._entries(enabled=False)
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "disable_watchdog",
                             return_value={"action": "disabled"}) as mock_off, \
                patch.object(he, "_fetch_instance_traffic",
                             return_value=fresh):
            await he.watchdog_callback(update, context)
        mock_off.assert_called_once()
        text = edit_text_of(update)
        self.assertIn("已关闭", text)

    async def test_bark_not_configured_shows_error(self):
        update = make_update(callback_data="wd:toggle:0")
        context = make_context()
        context.user_data["traffic_entries"] = self._entries(enabled=False)
        from tgbot.watchdog import WatchdogError
        with patch.object(bot_config, "GUARD_SSH_KEY",
                          "/root/.ssh/rbot_guard"), \
                patch.object(he, "install_watchdog",
                             side_effect=WatchdogError(
                                 "机器人还没配 Bark（菜单 43 配好地址和 Key 再来）")):
            await he.watchdog_callback(update, context)
        text = edit_text_of(update)
        self.assertIn("操作失败", text)
        self.assertIn("菜单 43", text)

    async def test_expired_list(self):
        update = make_update(callback_data="wd:toggle:3")
        context = make_context()
        context.user_data["traffic_entries"] = self._entries()
        await he.watchdog_callback(update, context)
        self.assertIn("已过期", edit_text_of(update))


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
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "get_profile_region_counts",
                          return_value={"DEFAULT": 3, "ACCOUNT2": 1}):
            update = make_update()
            context = make_context()
            await he.handle_health(update, context)
        text = reply_text_of(update)
        self.assertIn("新加坡（3 个区）：2 台实例，1 台运行中", text)
        self.assertIn("圣何塞", text)
        self.assertIn("auth failed", text)
        self.assertNotIn("DEFAULT", text)
        self.assertNotIn("ACCOUNT2", text)

    async def test_tasks_list_and_cancel(self):
        task_id = self.tasks.register("grab", detail="抢机测试中")
        update = make_update()
        context = make_context()
        await he.handle_tasks(update, context)
        text = reply_text_of(update)
        self.assertIn(task_id, text)
        self.assertIn("抢机测试中", text)

        update2 = make_update(callback_data=f"t:cancel:{task_id}")
        await he.task_cancel_callback(update2, make_context())
        self.assertIn("已取消", reply_text_of(update2))
        self.assertEqual(self.tasks.get(task_id)["status"], "cancelled")

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
        counts = {"DEFAULT": 3, "ACCOUNT2": 1}
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value=counts):
            update = make_update()
            context = make_context()
            await he.handle_profiles(update, context)
            self.assertIn("新加坡（3 个区）", reply_text_of(update))
            self.assertNotIn("DEFAULT", reply_text_of(update))
            buttons = reply_button_texts_of(update)
            self.assertIn("✅ 新加坡、3 个区", buttons)
            self.assertIn("圣何塞", buttons)

            update2 = make_update(callback_data="p:ACCOUNT2")
            await he.profile_callback(update2, context)
            self.assertEqual(context.user_data["profile"], "ACCOUNT2")
            self.assertIn("已切换到账号", edit_text_of(update2))
            self.assertIn("圣何塞", edit_text_of(update2))
            self.assertIn("Oracle 菜单", edit_text_of(update2))
            self.assertIsNone(reply_text_of(update2))
            self.assertIn("🔄 切换账号：圣何塞",
                          edit_button_texts_of(update2))

            update3 = make_update(callback_data="p:NOPE")
            await he.profile_callback(update3, context)
            self.assertIn("切换失败", reply_text_of(update3))

    async def test_profile_switch_deletes_tracked_messages(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        regions = {
            "DEFAULT": [{"region-name": "ap-singapore-1",
                         "is-home-region": True},
                        {"region-name": "ap-seoul-1",
                         "is-home-region": False}],
            "ACCOUNT2": [{"region-name": "us-sanjose-1",
                          "is-home-region": True}],
        }
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_regions",
                             return_value=regions), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 2, "ACCOUNT2": 1}):
            context = make_context()
            context.user_data["profile"] = "ACCOUNT2"
            msg1 = MagicMock()
            msg1.message_id = 101
            update1 = make_update()
            update1.effective_message.reply_text = AsyncMock(
                return_value=msg1)
            await he.handle_profiles(update1, context)
            update2 = make_update(callback_data="p:DEFAULT")
            await he.profile_callback(update2, context)
            self.assertIn("切换工作区域", edit_text_of(update2))
            self.assertIsNone(reply_text_of(update2))
            update3 = make_update(callback_data="pr:DEFAULT:ap-seoul-1")
            await he.profile_region_callback(update3, context)
            deleted = [call.kwargs.get("message_id")
                       for call in context.bot.delete_message.await_args_list]
            self.assertIn(101, deleted)
            self.assertNotIn(777, deleted)
            self.assertIn("已切换到账号", edit_text_of(update3))
            self.assertIn("首尔", edit_text_of(update3))
            self.assertEqual(context.user_data.get("switch_msg_ids"), None)

    async def test_profile_switch_duplicate_click_ignored(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        regions = {"DEFAULT": [{"region-name": "ap-singapore-1",
                                "is-home-region": True}],
                   "ACCOUNT2": [{"region-name": "us-sanjose-1",
                                 "is-home-region": True}]}
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_regions",
                             return_value=regions):
            context = make_context()
            update1 = make_update(callback_data="p:ACCOUNT2")
            await he.profile_callback(update1, context)
            self.assertIn("已切换到账号", edit_text_of(update1))
            update2 = make_update(callback_data="p:ACCOUNT2")
            await he.profile_callback(update2, context)
            self.assertIsNone(edit_text_of(update2))
            self.assertIsNone(reply_text_of(update2))
            update2.callback_query.answer.assert_awaited_once()

    async def test_handle_profiles_clears_stale_switch_messages(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
        }
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 1}):
            context = make_context()
            context.user_data["switch_msg_ids"] = [55]
            update = make_update()
            await he.handle_profiles(update, context)
            deleted = [call.kwargs.get("message_id")
                       for call in context.bot.delete_message.await_args_list]
            self.assertIn(55, deleted)

    async def test_profile_switch_resets_region_to_home(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        regions = {"DEFAULT": [{"region-name": "ap-singapore-1",
                                "is-home-region": True}],
                   "ACCOUNT2": [{"region-name": "us-sanjose-1",
                                 "is-home-region": True}]}
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_regions",
                             return_value=regions):
            context = make_context()
            context.user_data["profile"] = "DEFAULT"
            context.user_data["region"] = "ap-seoul-1"
            update = make_update(callback_data="p:ACCOUNT2")
            await he.profile_callback(update, context)
            self.assertEqual(context.user_data["profile"], "ACCOUNT2")
            self.assertEqual(context.user_data["region"], "us-sanjose-1")


class TestProfileRegions(HandlersExtraTestCase):
    """区域二级菜单：多区账号点开出区域菜单，点区域切换工作区域。"""

    PROFILES = {
        "DEFAULT": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..aaa"},
        "ACCOUNT2": {"region": "us-sanjose-1",
                     "compartment_id": "ocid1.tenancy.oc1..bbb"},
    }
    REGIONS = {
        "DEFAULT": [{"region-name": "ap-singapore-1", "is-home-region": True},
                    {"region-name": "ap-seoul-1", "is-home-region": False},
                    {"region-name": "us-sanjose-1", "is-home-region": False}],
        "ACCOUNT2": [{"region-name": "us-sanjose-1", "is-home-region": True}],
    }

    async def test_multi_region_opens_region_menu(self):
        with patch.object(bot_config, "PROFILES", self.PROFILES), \
                patch.object(he, "get_profile_regions",
                             return_value=self.REGIONS):
            context = make_context()
            context.user_data["profile"] = "ACCOUNT2"
            update = make_update(callback_data="p:DEFAULT")
            await he.profile_callback(update, context)
            self.assertEqual(context.user_data["profile"], "ACCOUNT2")
            self.assertIn("切换工作区域", edit_text_of(update))
            callbacks = edit_callback_data_of(update)
            self.assertIn("pr:DEFAULT:ap-singapore-1", callbacks)
            self.assertIn("pr:DEFAULT:ap-seoul-1", callbacks)
            self.assertIn("pr:DEFAULT:us-sanjose-1", callbacks)
            self.assertIn("o:profiles", callbacks)
            buttons = edit_button_texts_of(update)
            self.assertIn("新加坡（Home）", buttons)
            self.assertIn("首尔", buttons)

    async def test_region_switch_sets_profile_and_region(self):
        with patch.object(bot_config, "PROFILES", self.PROFILES), \
                patch.object(he, "get_profile_regions",
                             return_value=self.REGIONS):
            context = make_context()
            context.user_data["profile"] = "ACCOUNT2"
            update = make_update(callback_data="pr:DEFAULT:ap-seoul-1")
            await he.profile_region_callback(update, context)
            self.assertEqual(context.user_data["profile"], "DEFAULT")
            self.assertEqual(context.user_data["region"], "ap-seoul-1")
            self.assertIn("已切换到账号", edit_text_of(update))
            self.assertIn("首尔", edit_text_of(update))
            self.assertIn("Oracle 菜单", edit_text_of(update))
            self.assertIsNone(reply_text_of(update))

    async def test_region_switch_to_home_region(self):
        with patch.object(bot_config, "PROFILES", self.PROFILES), \
                patch.object(he, "get_profile_regions",
                             return_value=self.REGIONS):
            context = make_context()
            context.user_data["profile"] = "DEFAULT"
            context.user_data["region"] = "ap-seoul-1"
            update = make_update(
                callback_data="pr:DEFAULT:ap-singapore-1")
            await he.profile_region_callback(update, context)
            self.assertEqual(context.user_data["region"], "ap-singapore-1")
            self.assertIn("已切换到账号", edit_text_of(update))
            self.assertIn("Oracle 菜单", edit_text_of(update))
            self.assertIsNone(reply_text_of(update))

    async def test_region_switch_rejects_unsubscribed(self):
        with patch.object(bot_config, "PROFILES", self.PROFILES), \
                patch.object(he, "get_profile_regions",
                             return_value=self.REGIONS):
            context = make_context()
            update = make_update(callback_data="pr:DEFAULT:eu-frankfurt-1")
            await he.profile_region_callback(update, context)
            self.assertIn("未订阅该区域", reply_text_of(update))
            self.assertNotEqual(context.user_data.get("region"),
                                "eu-frankfurt-1")


class TestUniquifyDisplayName(unittest.TestCase):
    """实例名去重（日期名同日再开会重名，加 -2、-3 后缀）。"""

    def test_no_conflict_unchanged(self):
        self.assertEqual(
            he.uniquify_display_name("AMD_2026-01-01", 1, set()),
            "AMD_2026-01-01")

    def test_single_conflict_gets_suffix(self):
        self.assertEqual(
            he.uniquify_display_name("AMD_2026-01-01", 1,
                                     {"AMD_2026-01-01"}),
            "AMD_2026-01-01-2")

    def test_double_conflict_gets_next_suffix(self):
        self.assertEqual(
            he.uniquify_display_name("AMD_2026-01-01", 1,
                                     {"AMD_2026-01-01",
                                      "AMD_2026-01-01-2"}),
            "AMD_2026-01-01-3")

    def test_batch_checks_all_suffixed_names(self):
        self.assertEqual(
            he.uniquify_display_name("ARM_2026-01-01", 2,
                                     {"ARM_2026-01-01-1"}),
            "ARM_2026-01-01-2")

    def test_batch_skips_taken_second_group(self):
        self.assertEqual(
            he.uniquify_display_name("ARM_2026-01-01", 2,
                                     {"ARM_2026-01-01-1",
                                      "ARM_2026-01-01-2-2"}),
            "ARM_2026-01-01-3")


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
                self.assertIn("E2.1.Micro", text)
                self.assertNotIn(task_id, text)
                self.assertEqual(registered[0]["label"],
                                 "E2.1.Micro（新加坡）· e2-micro")
                # Wait for the daemon thread to finish (the mocked Grabber returns immediately). 等 daemon 线程跑完（mock Grabber 立即返回）。
                # The whole wait must stay inside the PRESETS_FILE patch, or save_preset in the thread writes to the real default path. 注意：整个等待过程必须留在 PRESETS_FILE 的 patch 里，否则线程里的 save_preset 会写到真实默认路径。
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
                # The quick-boot config was saved after success (status is set to done before the file write, so poll until the file is written instead of reading immediately). 成功后保存了快捷开机配置（状态先置 done、再写文件，所以要轮询等文件写好，不能立即读）。
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
            # The wait must stay inside the Grabber patch: if the patch exits before the thread builds its Grabber, the thread gets the real Grabber and the task fails. 等待必须留在 Grabber 的 patch 里：线程构造 Grabber 时 patch 已退出就会拿到真实 Grabber，任务变 failed。
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


class TestBatchActionEscape(HandlersExtraTestCase):
    """批量终止二次确认框的实例名 HTML 转义（v1.45.0 修 10）。"""

    async def test_batch_terminate_confirm_escapes_html_in_names(self):
        evil = [
            {"id": "ocid1.instance.oc1..aaaa",
             "display-name": 'test<b>&"x"',
             "lifecycle-state": "RUNNING"},
            {"id": "ocid1.instance.oc1..bbbb",
             "display-name": "ok-name",
             "lifecycle-state": "STOPPED"},
        ]
        context = make_context()
        context.user_data["instances"] = evil
        context.user_data["batch_selected"] = {"0", "1"}
        update = make_update("ba:action:terminate")
        await he.batch_action_callback(update, context)
        text = reply_text_of(update)
        self.assertIsNotNone(text)
        self.assertIn("确认批量删除", text)
        self.assertIn("test&lt;b&gt;&amp;", text)
        self.assertNotIn("<b>&", text)
        self.assertIn("ok-name", text)
        self.assertIn("<b>删除后将永久删除各实例的引导卷", text)
        _args, kwargs = update.effective_message.reply_text.await_args_list[-1]
        self.assertEqual(kwargs.get("parse_mode"), "HTML")


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
            "ocid1.instance.oc1..bbb2": "RUNNING",
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
            "ocid1.instance.oc1..bbb2": "STOPPED",
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
        self.cli.sdk_list_ipv6s.return_value = [
            {"ip-address": "2603:c024:c021:1d01::1",
             "lifecycle-state": "ASSIGNED"}]
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "list_vnics", return_value=vnics):
            update = make_update()
            context = make_context()
            await he.handle_ipv6(update, context)
        text = reply_text_of(update)
        self.assertIn("2603:c024:c021:1d01::1", text)
        self.cli.sdk_list_ipv6s.assert_called_once_with(
            "ocid1.vnic.oc1..v1")

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
                          return_value=FAKE_INSTANCES), \
                patch.object(he, "attach_public_ips",
                             side_effect=lambda cli, insts: insts):
            update = make_update()
            context = make_context()
            await he.handle_resume(update, context)
        texts = reply_button_texts_of(update)
        self.assertIn("e2-test · E2 · 已停止", texts)
        self.assertNotIn("web-server", " ".join(texts))
        self.assertEqual(len(context.user_data["instances"]), 1)

    async def test_handle_resume_enriches_ip(self):
        # 203.0.113.10 is a TEST-NET-3 documentation-reserved address, not a real IP. 203.0.113.10 是 TEST-NET-3 文档保留地址，非真实 IP。
        enriched = [dict(FAKE_INSTANCES[1],
                         **{"public-ip": "203.0.113.10"})]
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
                patch.object(he, "attach_public_ips",
                             return_value=enriched) as mock_attach:
            update = make_update()
            context = make_context()
            await he.handle_resume(update, context)
        mock_attach.assert_called_once()
        self.assertEqual(context.user_data["instances"], enriched)
        # The IP is on the matching button (the text keeps only the title). IP 在对应按钮上（文字只剩标题）。
        self.assertIn("e2-test · E2 · 203.0.113.10",
                      reply_button_texts_of(update))
        self.assertNotIn("203.0.113.10", reply_text_of(update))

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
        self.assertIn("🟢 可用 2 块（150GB）", text)
        self.assertNotIn("boot-a", text)
        self.assertIn("⚠️ 孤儿盘 1 块（共 100GB）", text)
        self.assertIn("boot-b（100GB） 🗑️", reply_button_texts_of(update))
        self.assertEqual(len(context.user_data["volumes"]), 2)

    async def test_handle_volumes_hides_terminated(self):
        volumes = [
            {"id": "ocid1.bootvolume.oc1..v1", "display-name": "boot-a",
             "size-in-gbs": 50, "lifecycle-state": "AVAILABLE",
             "availability-domain": "AD-1"},
            {"id": "ocid1.bootvolume.oc1..dead", "display-name": "old-test",
             "size-in-gbs": 47, "lifecycle-state": "TERMINATED",
             "availability-domain": "AD-1"},
            {"id": "ocid1.bootvolume.oc1..dying", "display-name": "dying",
             "size-in-gbs": 47, "lifecycle-state": "TERMINATING",
             "availability-domain": "AD-1"},
        ]
        with patch.object(he, "list_boot_volumes",
                          return_value=volumes), \
             patch.object(he, "list_orphan_boot_volumes",
                          return_value=[]), \
             patch.object(he, "total_boot_volume_gb", return_value=50):
            update = make_update()
            context = make_context()
            await he.handle_volumes(update, context)
        text = reply_text_of(update)
        self.assertIn("🟢 可用 1 块（50GB）", text)
        self.assertNotIn("已删除", text)
        self.assertNotIn("删除中", text)
        buttons = reply_button_texts_of(update)
        self.assertIn("boot-a（50GB）", buttons)
        self.assertNotIn("old-test（47GB）", buttons)
        self.assertNotIn("dying（47GB）", buttons)
        self.assertEqual(len(context.user_data["volumes"]), 1)
        self.assertEqual(context.user_data["volumes"][0]["display-name"],
                         "boot-a")

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

        update2 = make_update(callback_data="v:resize:0")
        await he.volume_resize_callback(update2, context)
        self.assertIn("只能扩不能缩", reply_text_of(update2))

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
        callbacks = reply_callback_data_of(update)
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
        self.assertIn("c:delipv6:0", reply_callback_data_of(update))

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
        self.assertIn("203.0.113.1", text)
        self.assertIn("203.0.113.2", text)
        self.assertIn("换 IP：请在实例列表里选一台实例", text)
        callbacks = reply_callback_data_of(update)
        self.assertIn("ip_create", callbacks)
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
        self.assertIn("c:delpubip:0", reply_callback_data_of(update))

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
        self.assertIn("云账户（2 个用户）", text)
        self.assertNotIn("alice", text)
        buttons = reply_button_texts_of(update)
        self.assertIn("👤 alice", buttons)
        self.assertIn("👤 bob", buttons)
        callbacks = reply_callback_data_of(update)
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
        callbacks = reply_callback_data_of(update)
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
        self.assertIn("c:userdesc:0", reply_callback_data_of(update))

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
        self.assertIn("c:useremail:1", reply_callback_data_of(update))

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
        self.assertIn("可点用户详情中的“新增 API Key”自动生成并上传新密钥",
                      text)
        callbacks = reply_callback_data_of(update)
        self.assertIn("u:keydel:0", callbacks)
        self.assertIn("u:keydel:1", callbacks)
        self.assertIn("u:detail:0", callbacks)
        self.assertIn("u:list", callbacks)
        self.assertEqual(len(context.user_data["iam_keys"]), 2)
        self.assertEqual(context.user_data["iam_keys_user"],
                         {"id": "ocid1.user.oc1..u1", "name": "alice",
                          "profile": "DEFAULT",
                          "region": "ap-singapore-1"})

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
        self.assertIn("c:delapikey:0", reply_callback_data_of(update))

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
        self.assertIn("c:deluser:0", reply_callback_data_of(update))

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

    async def test_confirm_deluser_refuses_last_admin(self):
        update = make_update(callback_data="c:deluser:0")
        context = self._users_context()
        with patch.object(he, "get_profile_user_id",
                          return_value="ocid1.user.oc1..u2"), \
             patch.object(he, "is_last_admin",
                          return_value=True), \
             patch.object(he, "delete_user",
                          return_value=None) as mock_delete:
            handled = await he.handle_confirm("deluser", "0",
                                              update, context)
        self.assertTrue(handled)
        mock_delete.assert_not_called()
        self.assertIn("最后一个管理员", reply_text_of(update))

    async def test_user_list_back_button(self):
        update = make_update(callback_data="u:list")
        context = make_context()
        with patch.object(he, "list_users",
                          return_value=[dict(u)
                                        for u in self.FAKE_USERS]):
            await he.user_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("云账户（2 个用户）", text)
        self.assertNotIn("alice", text)
        self.assertIn("👤 alice", reply_button_texts_of(update))
        self.assertIn("u:detail:0", reply_callback_data_of(update))


class TestUsersWorkflowsRegionsSubscriptionMfa(HandlersExtraTestCase):

    async def test_handle_users(self):
        self.cli.sdk_list_users.return_value = [
            {"name": "alice", "email": "alice@example.com",
             "lifecycle-state": "ACTIVE", "id": "ocid1.user.oc1..u1"},
            {"name": "bob", "email": None,
             "lifecycle-state": "ACTIVE", "id": "ocid1.user.oc1..u2"},
        ]
        update = make_update()
        context = make_context()
        await he.handle_users(update, context)
        text = reply_text_of(update)
        self.assertIn("云账户（2 个用户）", text)
        self.assertNotIn("alice", text)
        self.assertNotIn("alice@example.com", text)
        buttons = reply_button_texts_of(update)
        self.assertIn("👤 alice", buttons)
        self.assertIn("👤 bob", buttons)

    async def test_handle_emails(self):
        self.cli.sdk_list_users.return_value = [
            {"name": "alice", "email": "alice@example.com",
             "id": "ocid1.user.oc1..u1"},
            {"name": "bob", "email": None, "id": "ocid1.user.oc1..u2"},
        ]
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
        """菜单 18：有几个账号就显示几个（v1.7.1）。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }

        def fake_run(args, **kwargs):
            tenancy_id = args[args.index("--tenancy-id") + 1]
            if tenancy_id.endswith("aaa"):
                return {"data": {"name": "my-tenancy",
                                 "home-region-key": "SIN",
                                 "description": "test tenancy"}}
            return {"data": {"name": "other-tenancy",
                             "home-region-key": "SJC"}}

        self.cli.run.side_effect = fake_run
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 3, "ACCOUNT2": 1}):
            update = make_update()
            context = make_context()
            await he.handle_subscription(update, context)
        text = reply_text_of(update)
        self.assertIn("订阅信息（2 个账号）", text)
        self.assertIn("新加坡（3 个区）", text)
        self.assertIn("my-tenancy", text)
        self.assertIn("SIN", text)
        self.assertIn("test tenancy", text)
        self.assertIn("圣何塞", text)
        self.assertIn("other-tenancy", text)
        self.assertIn("SJC", text)

    async def test_handle_mfa_lists_devices(self):
        self.cli.sdk_list_users.return_value = [
            {"name": "alice", "id": "ocid1.user.oc1..u1"}]

        def fake_run(args, **kwargs):
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
        self.assertTrue(state["on"])
        self.assertEqual(state["chat_id"], TEST_CHAT_ID)

    async def test_daily_toggle(self):
        update = make_update()
        context = make_context()
        await he.handle_daily(update, context)
        state = context.bot_data["daily_report"]
        self.assertTrue(state["on"])
        self.assertEqual(state["chat_id"], TEST_CHAT_ID)


class TestDisplayNames(HandlersExtraTestCase):
    """v1.7.1 显示修复：用户列表标题化、账号按主区中文名显示。"""

    def setUp(self):
        super().setUp()
        # Clear the subscription-region cache before each test to keep tests from interfering with each other. 每次测试前清空订阅区域缓存，避免测试间互相干扰。
        he._profile_regions = {}
        he._profile_regions_at = None

    def test_fmt_user_list_header_only(self):
        """用户列表文字只剩标题 + 数量，不再逐条列邮箱（与实例列表同款）。"""
        users = [
            {"name": "mbot-test", "email": "a@example.com",
             "lifecycle-state": "ACTIVE"},
            {"name": "api_user", "email": "api@example.com",
             "lifecycle-state": "ACTIVE"},
            {"name": "root", "email": "root@example.com",
             "lifecycle-state": "INACTIVE"},
        ]
        text = he.fmt_user_list(users)
        self.assertIn("云账户（3 个用户）", text)
        self.assertIn("点下面按钮查看详情或操作", text)
        self.assertNotIn("a@example.com", text)
        self.assertNotIn("mbot-test", text)
        self.assertEqual(he.fmt_user_list([]), "该租户下没有 IAM 用户。")

    def test_user_button_text(self):
        """用户按钮：ACTIVE 只显示名；非 ACTIVE 后缀中文状态词。"""
        self.assertEqual(
            he._user_button_text({"name": "mbot-test",
                                  "lifecycle-state": "ACTIVE"}),
            "👤 mbot-test")
        self.assertEqual(
            he._user_button_text({"name": "old-user",
                                  "lifecycle-state": "INACTIVE"}),
            "👤 old-user · 已停用")
        self.assertEqual(
            he._user_button_text({"name": "x", "lifecycle-state": "WEIRD"}),
            "👤 x · WEIRD")

    async def test_handle_users_buttons_are_the_only_list(self):
        """菜单 8：文字是标题，按钮是唯一的列表（邮箱在详情页）。"""
        users = [
            {"id": "ocid1.user.oc1..aaa", "name": "mbot-test",
             "email": "a@example.com", "lifecycle-state": "ACTIVE"},
            {"id": "ocid1.user.oc1..bbb", "name": "old-user",
             "email": "b@example.com", "lifecycle-state": "INACTIVE"},
        ]
        with patch.object(he, "list_users", return_value=users):
            update = make_update()
            context = make_context()
            await he.handle_users(update, context)
        self.assertIn("云账户（2 个用户）", reply_text_of(update))
        self.assertNotIn("a@example.com", reply_text_of(update))
        buttons = reply_button_texts_of(update)
        self.assertIn("👤 mbot-test", buttons)
        self.assertIn("👤 old-user · 已停用", buttons)

    def test_profile_display_name(self):
        """账号显示名：主区中文名；多区账号后缀（N 个区）。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
            "ACCOUNT3": {"region": "us-chicago-1",
                         "compartment_id": "ocid1.tenancy.oc1..ccc"},
        }
        with patch.object(bot_config, "PROFILES", profiles):
            self.assertEqual(
                bot_config.profile_display_name("DEFAULT", 3),
                "新加坡（3 个区）")
            self.assertEqual(
                bot_config.profile_display_name("ACCOUNT2", 1), "圣何塞")
            self.assertEqual(
                bot_config.profile_display_name("ACCOUNT3", None), "芝加哥")
            self.assertEqual(
                bot_config.profile_display_name("DEFAULT", 0), "新加坡")

    def test_profile_display_name_unknown_region(self):
        """未知区域显示原代码，不崩。"""
        profiles = {"X": {"region": "ap-tokyo-1",
                          "compartment_id": "ocid1.tenancy.oc1..xxx"}}
        with patch.object(bot_config, "PROFILES", profiles):
            self.assertEqual(bot_config.profile_display_name("X", 2),
                             "ap-tokyo-1（2 个区）")

    def test_profile_display_name_list(self):
        """切换列表显示名：多区账号用顿号后缀（新加坡、3 个区），
        单区账号不加后缀。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
            "ACCOUNT3": {"region": "us-chicago-1",
                         "compartment_id": "ocid1.tenancy.oc1..ccc"},
        }
        with patch.object(bot_config, "PROFILES", profiles):
            self.assertEqual(
                bot_config.profile_display_name_list("DEFAULT", 3),
                "新加坡、3 个区")
            self.assertEqual(
                bot_config.profile_display_name_list("ACCOUNT2", 1),
                "圣何塞")
            self.assertEqual(
                bot_config.profile_display_name_list("ACCOUNT3", None),
                "芝加哥")
            self.assertEqual(
                bot_config.profile_display_name_list("DEFAULT", 0),
                "新加坡")

    def test_profile_keyboard_labels(self):
        """切换键盘：按钮显示显示名，回调仍是 profile 名。"""
        kb = profile_keyboard(["DEFAULT", "ACCOUNT2"], "DEFAULT",
                              {"DEFAULT": "新加坡（3 个区）",
                               "ACCOUNT2": "圣何塞"})
        texts = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertIn("✅ 新加坡（3 个区）", texts)
        self.assertIn("圣何塞", texts)
        datas = [btn.callback_data
                 for row in kb.inline_keyboard for btn in row]
        self.assertIn("p:DEFAULT", datas)
        self.assertIn("p:ACCOUNT2", datas)
        kb2 = profile_keyboard(["DEFAULT"], "DEFAULT")
        self.assertEqual(kb2.inline_keyboard[0][0].text, "✅ DEFAULT")

    def test_get_profile_region_counts_caches(self):
        """区域数查一次后缓存 1 小时，第二次不再调 OCI。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        fake_cli = MagicMock()
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(bot_config, "get_oci_client",
                             return_value=fake_cli) as get_cli, \
                patch.object(he, "list_regions",
                             return_value=[{}, {}, {}]) as list_regions:
            counts = he.get_profile_region_counts()
            self.assertEqual(counts, {"DEFAULT": 3, "ACCOUNT2": 3})
            self.assertEqual(get_cli.call_count, 2)
            counts2 = he.get_profile_region_counts()
            self.assertEqual(counts2, counts)
            self.assertEqual(get_cli.call_count, 2)
            self.assertEqual(list_regions.call_count, 2)

    def test_get_profile_region_counts_skips_failed_profile(self):
        """单个账号查失败时跳过，不影响其他账号。"""
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }

        def _list_regions(cli, compartment_id):
            if compartment_id.endswith("aaa"):
                raise RuntimeError("boom")
            return [{}]

        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(bot_config, "get_oci_client",
                             return_value=MagicMock()), \
                patch.object(he, "list_regions",
                             side_effect=_list_regions):
            counts = he.get_profile_region_counts()
            self.assertEqual(counts, {"ACCOUNT2": 1})

    def test_fmt_tenancies(self):
        """多账号订阅信息：有几个账号显示几个，失败的账号单列。"""
        text = he.fmt_tenancies([
            ("新加坡（3 个区）",
             {"name": "example-tenant-sg", "home-region-key": "SIN",
              "description": "example-tenant-sg"}),
            ("圣何塞", {"name": "example-tenant-sj", "home-region-key": "SJC"}),
            ("芝加哥", None),
        ])
        self.assertIn("订阅信息（3 个账号）", text)
        self.assertIn("新加坡（3 个区）", text)
        self.assertIn("example-tenant-sg", text)
        self.assertIn("圣何塞", text)
        self.assertIn("example-tenant-sj", text)
        self.assertIn("芝加哥", text)
        self.assertIn("查询失败", text)
        self.assertEqual(he.fmt_tenancies([]), "没有配置任何账号。")


class TestQuotaDrillDown(HandlersExtraTestCase):
    """v1.7.1 额度页三级导航：账号列表 ->（多区）区域菜单 -> 额度详情。"""

    _PROFILES = {
        "DEFAULT": {"region": "ap-singapore-1",
                    "compartment_id": "ocid1.tenancy.oc1..aaa"},
        "ACCOUNT2": {"region": "us-sanjose-1",
                     "compartment_id": "ocid1.tenancy.oc1..bbb"},
    }
    _REGIONS_3 = [
        {"region-name": "ap-singapore-1", "is-home-region": True,
         "status": "READY"},
        {"region-name": "ap-seoul-1", "is-home-region": False,
         "status": "READY"},
        {"region-name": "us-sanjose-1", "is-home-region": False,
         "status": "READY"},
    ]

    def setUp(self):
        super().setUp()
        he._profile_regions = {}
        he._profile_regions_at = None

    async def test_quota_account_single_region_shows_summary(self):
        """单区账号点开直接显示该区额度，不出二级菜单。"""
        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(he, "get_profile_regions", return_value={
                    "ACCOUNT2": [{"region-name": "us-sanjose-1",
                                  "is-home-region": True,
                                  "status": "READY"}]}), \
                patch.object(he, "get_free_tier_summary",
                             return_value=FAKE_SUMMARY):
            update = make_update(callback_data="q:acct:ACCOUNT2")
            context = make_context()
            await he.quota_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("[圣何塞] 免费额度", text)
        self.assertIn("A1.Flex：3/4 核，12/24GB", text)
        self.assertIn("E2.1.Micro：1/2 台", text)
        self.assertIn("启动盘：100/200GB", text)
        self.assertIn("q:list", reply_callback_data_of(update))

    async def test_quota_account_multi_region_shows_region_menu(self):
        """多区账号点开出二级区域菜单，Home 区域标（Home）。"""
        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(he, "get_profile_regions", return_value={
                    "DEFAULT": self._REGIONS_3}):
            update = make_update(callback_data="q:acct:DEFAULT")
            context = make_context()
            await he.quota_callback(update, context)
        self.assertIn("新加坡（3 个区）", reply_text_of(update))
        texts = reply_button_texts_of(update)
        self.assertIn("新加坡（Home）", texts)
        self.assertIn("首尔", texts)
        self.assertIn("圣何塞", texts)
        callbacks = reply_callback_data_of(update)
        self.assertIn("q:region:DEFAULT:ap-singapore-1", callbacks)
        self.assertIn("q:region:DEFAULT:ap-seoul-1", callbacks)
        self.assertIn("q:region:DEFAULT:us-sanjose-1", callbacks)
        self.assertIn("q:list", callbacks)

    async def test_quota_region_shows_summary_with_region_label(self):
        """点开非主区：标题带“主区 · 该区”，OCI 客户端用该区域。"""
        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(bot_config, "get_oci_client",
                             return_value=self.cli) as mock_get_cli, \
                patch.object(he, "get_free_tier_summary",
                             return_value=FAKE_SUMMARY):
            update = make_update(
                callback_data="q:region:DEFAULT:ap-seoul-1")
            context = make_context()
            await he.quota_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("[新加坡 · 首尔] 免费额度", text)
        self.assertIn("A1.Flex：3/4 核，12/24GB", text)
        mock_get_cli.assert_called_once_with(profile="DEFAULT",
                                             region="ap-seoul-1")
        self.assertIn("q:acct:DEFAULT", reply_callback_data_of(update))

    async def test_quota_region_error_shows_friendly_message(self):
        """额度查询失败时显示中文错误提示。"""
        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(he, "get_free_tier_summary",
                             side_effect=RuntimeError("boom")):
            update = make_update(callback_data="q:region:DEFAULT:ap-seoul-1")
            context = make_context()
            await he.quota_callback(update, context)
        self.assertIn("出错了", reply_text_of(update))

    async def test_quota_list_back_to_accounts(self):
        """q:list 回到账号列表。"""
        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 3, "ACCOUNT2": 1}):
            update = make_update(callback_data="q:list")
            context = make_context()
            await he.quota_callback(update, context)
        self.assertIn("点下面账号查看", reply_text_of(update))
        self.assertIn("q:acct:DEFAULT", reply_callback_data_of(update))

    async def test_quota_callback_answers_before_slow_lookup(self):
        """点开区域先应答再查额度：按钮不等查询完成就停转。"""
        update = make_update(callback_data="q:region:DEFAULT:ap-seoul-1")
        context = make_context()
        seen = []

        def _slow_summary(*args, **kwargs):
            seen.append(update.callback_query.answer.await_count)
            return FAKE_SUMMARY

        with patch.object(bot_config, "PROFILES", self._PROFILES), \
                patch.object(bot_config, "get_oci_client",
                             return_value=self.cli), \
                patch.object(he, "get_free_tier_summary",
                             side_effect=_slow_summary):
            await he.quota_callback(update, context)
        self.assertEqual(seen, [1])
        self.assertIn("[新加坡 · 首尔] 免费额度", reply_text_of(update))

    def test_get_cached_profile_display_name(self):
        """菜单顶行显示名：缓存有订阅数时带后缀，否则只有主区名。"""
        he._profile_regions = {"DEFAULT": self._REGIONS_3}
        self.assertEqual(he.get_cached_profile_display_name("DEFAULT"),
                         "新加坡（3 个区）")
        he._profile_regions = {}
        self.assertEqual(he.get_cached_profile_display_name("DEFAULT"),
                         "新加坡")

    def test_quota_keyboards(self):
        """额度页键盘：账号列表与区域二级菜单的按钮文本与回调。"""
        kb = quota_accounts_keyboard({"DEFAULT": "新加坡（3 个区）",
                                      "ACCOUNT2": "圣何塞"})
        texts = [b.text for row in kb.inline_keyboard for b in row]
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual(texts[0], "新加坡（3 个区）")
        self.assertEqual(datas[0], "q:acct:DEFAULT")
        self.assertIn("o:menu", datas)
        kb2 = quota_regions_keyboard("DEFAULT", self._REGIONS_3)
        texts2 = [b.text for row in kb2.inline_keyboard for b in row]
        datas2 = [b.callback_data for row in kb2.inline_keyboard
                  for b in row]
        self.assertIn("新加坡（Home）", texts2)
        self.assertIn("首尔", texts2)
        self.assertIn("q:region:DEFAULT:ap-seoul-1", datas2)
        self.assertIn("q:list", datas2)


class TestMessageCleanup(HandlersExtraTestCase):
    """v1.7.1 消息清理：改完了只留结果这一条信息。"""

    async def test_reply_returns_message(self):
        """_reply 返回新发送的消息（供调用方记录 ID 以便清理）。"""
        update = make_update()
        sent = MagicMock(message_id=555)
        update.effective_message.reply_text = AsyncMock(
            return_value=sent)
        context = make_context()
        result = await he._reply(update, context, "hi")
        self.assertIs(result, sent)

    async def test_reply_prompt_deletes_old_and_tracks_new(self):
        """发新提示前先删旧提示（防重复点按钮攒多条），新提示记 ID。"""
        update = make_update()
        update.effective_message.reply_text = AsyncMock(
            return_value=MagicMock(message_id=777))
        context = make_context()
        context.bot.delete_message = AsyncMock()
        context.user_data["pending_prompt_ids"] = [111, 222]
        msg = await he._reply_prompt(update, context, "请输入：")
        deleted = [kwargs["message_id"] for _a, kwargs
                   in context.bot.delete_message.await_args_list]
        self.assertEqual(deleted, [111, 222])
        self.assertEqual(context.user_data["pending_prompt_ids"], [777])
        self.assertEqual(msg.message_id, 777)

    async def test_text_input_deletes_tracked_prompts(self):
        """用户回复文字后，提示消息被删除，只留确认/结果消息。"""
        update = make_update()
        update.effective_message.text = "new-name"
        context = make_context()
        context.bot.delete_message = AsyncMock()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        context.user_data["pending_input"] = {"kind": "rename", "idx": "0"}
        context.user_data["pending_prompt_ids"] = [111, 222]
        await he.text_input_callback(update, context)
        deleted = [kwargs["message_id"] for _a, kwargs
                   in context.bot.delete_message.await_args_list]
        self.assertEqual(deleted, [111, 222])
        self.assertNotIn("pending_prompt_ids", context.user_data)
        self.assertIn("确认把实例", reply_text_of(update))

    async def test_handle_confirm_deletes_confirmation_message(self):
        """确认执行后，确认消息被删除，只留结果消息。"""
        update = make_update(callback_data="c:changeip:0")
        update.callback_query.message.message_id = 999
        context = make_context()
        context.bot.delete_message = AsyncMock()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "change_public_ip",
                          return_value="203.0.113.10"):
            handled = await he.handle_confirm("changeip", "0",
                                              update, context)
        self.assertTrue(handled)
        self.assertIn("203.0.113.10", reply_text_of(update))
        deleted = [(kwargs.get("chat_id"), kwargs.get("message_id"))
                   for _a, kwargs
                   in context.bot.delete_message.await_args_list]
        self.assertIn((TEST_CHAT_ID, 999), deleted)

    async def test_handle_confirm_unknown_action_no_delete(self):
        """未知 action 不处理，也不删消息。"""
        update = make_update(callback_data="c:explode:0")
        context = make_context()
        context.bot.delete_message = AsyncMock()
        handled = await he.handle_confirm("explode", "0", update, context)
        self.assertFalse(handled)
        context.bot.delete_message.assert_not_awaited()


class TestVolumeGrouping(HandlersExtraTestCase):
    """磁盘列表分组（v1.8.1）：可用在前、已删除在后单独分组；
    文字只留标题 + 各组数量，按钮是唯一的列表。"""

    VOLUMES = [
        {"id": "ocid1.bootvolume.oc1..dead1", "display-name": "old-test-1",
         "size-in-gbs": 47, "lifecycle-state": "TERMINATED"},
        {"id": "ocid1.bootvolume.oc1..live1", "display-name": "prod-1",
         "size-in-gbs": 47, "lifecycle-state": "AVAILABLE"},
        {"id": "ocid1.bootvolume.oc1..dead2", "display-name": "old-test-2",
         "size-in-gbs": 50, "lifecycle-state": "TERMINATED"},
        {"id": "ocid1.bootvolume.oc1..live2", "display-name": "prod-2",
         "size-in-gbs": 47, "lifecycle-state": "AVAILABLE"},
    ]

    def test_sort_volumes_by_group_order_and_indices(self):
        ordered = he.sort_volumes_by_group(self.VOLUMES)
        self.assertEqual(
            [name for _i, v in ordered for name in [v["display-name"]]],
            ["prod-1", "prod-2", "old-test-1", "old-test-2"])
        # Indexes are indexes into the original list (callbacks use them to fetch back), not post-grouping indexes. 下标是原列表下标（回调按它取回），不是分组后的下标。
        self.assertEqual([i for i, _v in ordered], [1, 3, 0, 2])

    def test_volume_group_title_unknown_is_other(self):
        self.assertEqual(he.volume_group_title("AVAILABLE"), "🟢 可用")
        self.assertEqual(he.volume_group_title("TERMINATED"), "⚫ 已删除")
        self.assertEqual(he.volume_group_title("WEIRD"), "❓ 其他")

    def test_fmt_volume_list_counts_only_no_per_volume_lines(self):
        text = he.fmt_volume_list(self.VOLUMES, [], 94)
        self.assertIn("🟢 可用 2 块（94GB）", text)
        self.assertIn("⚫ 已删除 2 块", text)
        self.assertNotIn("prod-1", text)
        self.assertNotIn("old-test-1", text)

    def test_fmt_volume_list_empty_and_orphan_warning(self):
        self.assertIn("无启动盘", he.fmt_volume_list([], [], 0))
        orphans = [{"id": "ocid1.bootvolume.oc1..live2", "size-in-gbs": 47}]
        text = he.fmt_volume_list(self.VOLUMES, orphans, 94)
        self.assertIn("⚠️ 孤儿盘 1 块（共 47GB）", text)

    def test_volume_list_keyboard_grouped_with_original_indices(self):
        markup = he.volume_list_keyboard(self.VOLUMES)
        texts = [b.text for row in markup.inline_keyboard for b in row]
        datas = [b.callback_data
                 for row in markup.inline_keyboard for b in row]
        self.assertEqual(texts[0], "🟢 可用（2 块）：")
        self.assertEqual(datas[0], "v:noop")
        self.assertIn("v:detail:1", datas)
        self.assertIn("v:detail:0", datas)
        self.assertLess(datas.index("v:detail:1"),
                        datas.index("v:detail:0"))
        self.assertIn("bb:list", datas)

    def test_volume_list_keyboard_marks_orphan(self):
        orphans = [{"id": "ocid1.bootvolume.oc1..live2", "size-in-gbs": 47}]
        markup = he.volume_list_keyboard(self.VOLUMES, orphans)
        texts = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("prod-2（47GB） 🗑️", texts)
        self.assertNotIn("prod-1（47GB） 🗑️", texts)

    def test_volume_actions_keyboard_terminated_has_no_actions(self):
        markup = he.volume_actions_keyboard("0", "TERMINATED")
        datas = [b.callback_data
                 for row in markup.inline_keyboard for b in row]
        self.assertEqual(datas, ["o:volumes"])
        markup = he.volume_actions_keyboard("0", "AVAILABLE")
        datas = [b.callback_data
                 for row in markup.inline_keyboard for b in row]
        self.assertIn("v:resize:0", datas)
        self.assertIn("v:backup:0", datas)
        self.assertIn("v:delete:0", datas)

    async def test_volume_detail_callback_terminated_no_actions(self):
        update = make_update(callback_data="v:detail:0")
        context = make_context()
        context.user_data["volumes"] = list(self.VOLUMES)
        detail = dict(self.VOLUMES[0])
        with patch.object(he, "get_boot_volume", return_value=detail):
            await he.volume_detail_callback(update, context)
        datas = reply_callback_data_of(update)
        self.assertNotIn("v:resize:0", datas)
        self.assertNotIn("v:backup:0", datas)
        self.assertNotIn("v:delete:0", datas)
        self.assertIn("o:volumes", datas)

    async def test_volume_resize_callback_terminated_blocked(self):
        update = make_update(callback_data="v:resize:0")
        context = make_context()
        context.user_data["volumes"] = list(self.VOLUMES)
        await he.volume_resize_callback(update, context)
        self.assertIn("已删除", reply_text_of(update))




class TestFmtCfStatus(unittest.TestCase):
    """fmt_cf_status：来源文案按 source 区分（v1.30.0 加 oci_config）。"""

    def test_oci_config_source(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com",
                              "oci_config")), patch.object(
                he, "mask_cf_email", return_value="u***@example.com"):
            text = he.fmt_cf_status()
        self.assertIn("OCI 配置文件", text)
        self.assertIn("Global API Key", text)
        self.assertIn("u***@example.com", text)
        self.assertNotIn("fake-key", text)
        self.assertNotIn("user@example.com", text)

    def test_file_source_fallback_line(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("fake-token", "", "", "file")), patch.object(
                he, "mask_cf_email", return_value="***"):
            text = he.fmt_cf_status()
        self.assertIn("机器人/面板里保存的", text)
        self.assertIn("API Token", text)
        self.assertIn("OCI 配置或环境变量", text)


if __name__ == "__main__":
    unittest.main()
