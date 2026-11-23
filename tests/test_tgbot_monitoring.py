#!/usr/bin/env python3
"""监控增强的 Telegram handler / JobQueue 测试（阶段 5 工作包 C）。

全部用 mock，不碰真实 OCI API，不做真实 TLS 连接。
配置读写一律用临时 DATA_DIR（patch tgbot.config.DATA_DIR）。
"""

import unittest
from datetime import datetime, timedelta, timezone
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import config as bot_config
from tgbot import handlers as core_handlers
from tgbot import monitoring as mon
import tgbot.handlers_extra as he
from tgbot.keyboards import oracle_menu_keyboard

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..aaa1fake", "display-name": "web-server",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-20T00:00:00Z"},
    {"id": "ocid1.instance.oc1..bbb2fake", "display-name": "e2-test",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-21T00:00:00Z"},
]
RUNNING_ID = FAKE_INSTANCES[0]["id"]
STOPPED_ID = FAKE_INSTANCES[1]["id"]


def make_update(callback_data=None, text=None):
    """mock Update：reply_text / answer 都是 AsyncMock；text 用于文字输入。"""
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


def button_texts(keyboard):
    """取 InlineKeyboardMarkup 里全部按钮文字。"""
    if keyboard is None:
        return []
    return [btn.text for row in keyboard.inline_keyboard for btn in row]


def button_data(keyboard):
    """取 InlineKeyboardMarkup 里全部 callback_data。"""
    if keyboard is None:
        return []
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


def sent_texts(context):
    """取 bot.send_message 全部调用的文本列表。"""
    return [kwargs["text"] for _a, kwargs
            in context.bot.send_message.await_args_list]


def usage_items(quantity_gb):
    """本月出站流量 quantity_gb 的用量 API items（mock 用）。"""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return [{"service": "Data Transfer",
             "time-usage-started": f"{month}-10T00:00:00Z",
             "computed-quantity": quantity_gb,
             "computed-amount": 0, "currency": "SGD"}]


class MonitoringBotTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、临时 DATA_DIR、mock cli。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._data_dir = patch.object(bot_config, "DATA_DIR", self._tmp.name)
        self._data_dir.start()
        self.addCleanup(self._data_dir.stop)

        self.cli = MagicMock()
        self.cli.run.return_value = {"data": []}
        self._cli_patch = patch.object(bot_config, "get_oci_client",
                                       return_value=self.cli)
        self._cli_patch.start()
        self.addCleanup(self._cli_patch.stop)


# ================= 动态菜单状态 =================

class TestGetMenuStates(MonitoringBotTestCase):

    def test_all_off_by_default(self):
        context = make_context()
        self.assertEqual(he.get_menu_states(context), {
            "monitor": False, "autostart": False,
            "traffic_guard": False, "keepalive": False, "bark": False,
        })

    def test_reads_monitor_and_files(self):
        context = make_context()
        context.bot_data["monitor"] = {"on": True, "autostart": True,
                                       "chat_id": TEST_CHAT_ID}
        mon.save_traffic_guard({"enabled": True, "threshold_gb": 10240,
                                "action": "notify", "last_triggered": None,
                                "chat_id": TEST_CHAT_ID})
        mon.save_keepalive({STOPPED_ID: True, RUNNING_ID: False})
        self.assertEqual(he.get_menu_states(context), {
            "monitor": True, "autostart": True,
            "traffic_guard": True, "keepalive": True, "bark": False,
        })

    def test_keepalive_off_when_no_instance_enabled(self):
        context = make_context()
        mon.save_keepalive({STOPPED_ID: False})
        self.assertFalse(he.get_menu_states(context)["keepalive"])


class TestOracleMenuDynamicStates(MonitoringBotTestCase):

    def test_states_none_shows_plain_text(self):
        texts = button_texts(oracle_menu_keyboard(0))
        self.assertIn("9. 实例状态监控通知", texts)
        self.assertIn("10. 实例状态监控自启", texts)
        self.assertNotIn("9. 实例状态监控通知 (开启)", texts)

    def test_monitor_states_on_page_zero(self):
        states = {"monitor": True, "autostart": False,
                  "traffic_guard": True, "keepalive": False}
        texts = button_texts(oracle_menu_keyboard(0, states=states))
        self.assertIn("9. 实例状态监控通知 (开启)", texts)
        self.assertIn("10. 实例状态监控自启 (关闭)", texts)
        # 非监控项不带后缀。
        self.assertIn("6. 实例管理", texts)
        self.assertIn("16. 监控(自动换ip)", texts)

    def test_guard_keepalive_states_on_last_page(self):
        states = {"monitor": False, "autostart": False,
                  "traffic_guard": True, "keepalive": True}
        texts = button_texts(oracle_menu_keyboard(2, states=states))
        self.assertIn("37. 流量守护 (开启)", texts)
        self.assertIn("38. 停机保活 (开启)", texts)
        # SSL 监控没有总开关，不带后缀。
        self.assertIn("39. SSL 监控", texts)

    def test_non_dict_states_ignored(self):
        texts = button_texts(oracle_menu_keyboard(0, states="bogus"))
        self.assertIn("9. 实例状态监控通知", texts)

    def test_new_menu_items_exist(self):
        data = button_data(oracle_menu_keyboard(2))
        self.assertIn("o:traffic_guard", data)
        self.assertIn("o:keepalive", data)
        self.assertIn("o:sslwatch", data)

    def test_account_switch_row(self):
        """v1.7.1：传 current_account 时菜单顶行有切换账号按钮。"""
        kb = oracle_menu_keyboard(0, current_account="新加坡（3 个区）")
        first = kb.inline_keyboard[0][0]
        self.assertEqual(first.text, "🔄 切换账号：新加坡（3 个区）")
        self.assertEqual(first.callback_data, "o:profiles")
        # 不传时没有切换行（第一行是菜单项）
        kb2 = oracle_menu_keyboard(0)
        texts2 = button_texts(kb2)
        self.assertTrue(all(not t.startswith("🔄 切换账号")
                            for t in texts2))


class TestMonitorToggleRefresh(MonitoringBotTestCase):

    async def test_monitor_toggle_replies_refreshed_menu(self):
        update = make_update(callback_data="o:monitor")
        context = make_context()
        await he.handle_monitor(update, context)
        self.assertIn("已开启", reply_text_of(update))
        texts = button_texts(reply_markup_of(update))
        self.assertIn("9. 实例状态监控通知 (开启)", texts)
        # 再切一次：按钮变回关闭。
        await he.handle_monitor(update, context)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("9. 实例状态监控通知 (关闭)", texts)

    async def test_autostart_toggle_refreshes_both_buttons(self):
        update = make_update(callback_data="o:autostart")
        context = make_context()
        await he.handle_autostart(update, context)
        texts = button_texts(reply_markup_of(update))
        # 开自启时监控也一并开启，两个按钮都显示开启。
        self.assertIn("9. 实例状态监控通知 (开启)", texts)
        self.assertIn("10. 实例状态监控自启 (开启)", texts)


class TestHandlersMenuStates(MonitoringBotTestCase):
    """handlers.py 生成菜单时带动态状态。"""

    def test_menu_states_helper(self):
        context = make_context()
        context.bot_data["monitor"] = {"on": True, "autostart": False,
                                       "chat_id": TEST_CHAT_ID}
        states = core_handlers._menu_states(context)
        self.assertEqual(states["monitor"], True)
        self.assertEqual(states["autostart"], False)

    async def test_oracle_command_shows_states(self):
        update = make_update()
        context = make_context()
        context.bot_data["monitor"] = {"on": True, "autostart": True,
                                       "chat_id": TEST_CHAT_ID}
        await core_handlers.oracle_command(update, context)
        texts = button_texts(reply_markup_of(update))
        self.assertIn("9. 实例状态监控通知 (开启)", texts)
        self.assertIn("10. 实例状态监控自启 (开启)", texts)


# ================= 流量守护 =================

class TestTrafficGuardMenu(MonitoringBotTestCase):

    def _patch_fetch(self, value=(1234.5, "2 台可达")):
        return patch.object(he, "_fetch_guard_monthly_gb",
                            new=AsyncMock(return_value=value))

    async def test_menu_shows_config_and_usage(self):
        update = make_update(callback_data="o:traffic_guard")
        context = make_context()
        mon.save_traffic_guard({"enabled": True, "threshold_gb": 200,
                                "action": "block",
                                "last_triggered": None,
                                "chat_id": TEST_CHAT_ID})
        with self._patch_fetch():
            await he.handle_traffic_guard(update, context)
        text = reply_text_of(update)
        self.assertIn("✅ 开启", text)
        self.assertIn("200GB", text)
        self.assertIn("阻断断网", text)
        self.assertIn("1234.50GB", text)
        self.assertIn("2 台可达", text)
        data = button_data(reply_markup_of(update))
        texts = button_texts(reply_markup_of(update))
        self.assertIn("g:toggle", data)
        self.assertIn("g:action", data)
        self.assertIn("g:threshold:9216", data)
        self.assertIn("g:unblock", data)
        self.assertIn("阈值 9TB", texts)

    async def test_menu_survives_usage_failure(self):
        update = make_update(callback_data="o:traffic_guard")
        context = make_context()
        with self._patch_fetch((None, "全部实例都连不上")):
            await he.handle_traffic_guard(update, context)
        text = reply_text_of(update)
        self.assertIn("⬜ 关闭", text)
        self.assertIn("全部实例都连不上", text)

    async def test_menu_shows_no_key_note(self):
        """未配 SSH 密钥时（测试环境默认）菜单直接显示提示，不调 SSH。"""
        update = make_update(callback_data="o:traffic_guard")
        context = make_context()
        await he.handle_traffic_guard(update, context)
        self.assertIn("未配置 SSH 密钥", reply_text_of(update))


class TestTrafficGuardCallback(MonitoringBotTestCase):

    async def test_toggle_enables_and_records_chat_id(self):
        update = make_update(callback_data="g:toggle")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        cfg = mon.load_traffic_guard()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["chat_id"], TEST_CHAT_ID)
        self.assertIn("✅ 开启", reply_text_of(update))

    async def test_toggle_twice_disables(self):
        update = make_update(callback_data="g:toggle")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        await he.traffic_guard_callback(update, context)
        self.assertFalse(mon.load_traffic_guard()["enabled"])

    async def test_action_cycles_three_ways(self):
        """动作三选一循环：仅通知 → 阻断断网 → 限速200K → 仅通知。"""
        update = make_update(callback_data="g:action")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        self.assertEqual(mon.load_traffic_guard()["action"], "block")
        await he.traffic_guard_callback(update, context)
        self.assertEqual(mon.load_traffic_guard()["action"], "limit")
        await he.traffic_guard_callback(update, context)
        self.assertEqual(mon.load_traffic_guard()["action"], "notify")

    async def test_threshold_change(self):
        update = make_update(callback_data="g:threshold:20")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        self.assertEqual(mon.load_traffic_guard()["threshold_gb"], 20)

    async def test_bad_threshold_rejected(self):
        update = make_update(callback_data="g:threshold:abc")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        self.assertIn("参数错误", reply_text_of(update))
        self.assertEqual(mon.load_traffic_guard()["threshold_gb"], 200)

    async def test_unknown_action_rejected(self):
        update = make_update(callback_data="g:bogus")
        context = make_context()
        await he.traffic_guard_callback(update, context)
        self.assertIn("参数错误", reply_text_of(update))

    async def test_unblock_runs_on_running_instances(self):
        """解除限制：在运行中实例上跑解除命令，汇报结果后重渲染菜单。"""
        update = make_update(callback_data="g:unblock")
        context = make_context()
        instances = [dict(FAKE_INSTANCES[0], **{"public-ip": "203.0.113.10"}),
                     dict(FAKE_INSTANCES[1])]
        with patch.object(he, "list_instances",
                          return_value=instances), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts), \
             patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          return_value=(0, "GUARD-UNBLOCKED", "")) as mock_run:
            await he.traffic_guard_callback(update, context)
        mock_run.assert_called()
        # 第 1 次调用是解除命令（第 2 次是重渲染菜单时的 vnstat 查询）。
        _host, command = mock_run.call_args_list[0].args
        self.assertIn("TRAFFIC_GUARD", command)
        self.assertIn("tc qdisc del", command)
        # 结果消息是倒数第二条（最后一条是重渲染的菜单）。
        texts = [a.args[0] for a in
                 update.effective_message.reply_text.await_args_list]
        self.assertTrue(any("已解除" in t and "web-server" in t
                            for t in texts))


class TestTrafficGuardStep(MonitoringBotTestCase):

    def _enable_guard(self, action="notify", threshold=200, **over):
        cfg = {"enabled": True, "threshold_gb": threshold, "action": action,
               "last_triggered": None, "chat_id": TEST_CHAT_ID}
        cfg.update(over)
        mon.save_traffic_guard(cfg)
        return cfg

    def _patch_fetch(self, value=(250.0, "2 台可达")):
        return patch.object(he, "_fetch_guard_monthly_gb",
                            new=AsyncMock(return_value=value))

    def _running_instances(self):
        return [dict(FAKE_INSTANCES[0], **{"public-ip": "203.0.113.10"}),
                dict(FAKE_INSTANCES[1])]

    async def test_disabled_does_nothing(self):
        context = make_context()
        with self._patch_fetch() as mock_fetch:
            await he._traffic_guard_step(context)
            mock_fetch.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_no_chat_id_does_nothing(self):
        context = make_context()
        self._enable_guard(chat_id=None)
        with self._patch_fetch() as mock_fetch:
            await he._traffic_guard_step(context)
            mock_fetch.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_monitor_chat_id_is_fallback(self):
        context = make_context()
        context.bot_data["monitor"] = {"on": True, "autostart": False,
                                       "chat_id": TEST_CHAT_ID}
        self._enable_guard(chat_id=None)
        with self._patch_fetch():
            await he._traffic_guard_step(context)
        context.bot.send_message.assert_awaited_once()
        _, kwargs = context.bot.send_message.await_args
        self.assertEqual(kwargs["chat_id"], TEST_CHAT_ID)

    async def test_below_threshold_does_nothing(self):
        context = make_context()
        self._enable_guard()
        with self._patch_fetch((100.0, "2 台可达")):
            await he._traffic_guard_step(context)
        context.bot.send_message.assert_not_awaited()
        self.assertIsNone(mon.load_traffic_guard()["last_triggered"])

    async def test_notify_action_only_notifies(self):
        context = make_context()
        self._enable_guard(action="notify")
        with self._patch_fetch(), \
             patch.object(he, "run_remote_command") as mock_run:
            await he._traffic_guard_step(context)
            mock_run.assert_not_called()
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("流量守护提醒", texts[0])
        self.assertIn("250.00GB", texts[0])
        self.assertIn("仅通知", texts[0])
        # 触发时间已记录。
        self.assertIsNotNone(mon.load_traffic_guard()["last_triggered"])

    async def test_block_blocks_running_only(self):
        context = make_context()
        self._enable_guard(action="block")
        with self._patch_fetch(), \
             patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          return_value=(0, "GUARD-BLOCKED", "")) as mock_run:
            await he._traffic_guard_step(context,
                                         instances=self._running_instances())
        mock_run.assert_called_once()
        host, command = mock_run.call_args.args
        self.assertEqual(host["host"], "203.0.113.10")
        self.assertEqual(host["port"], 14422)
        self.assertIn("TRAFFIC_GUARD", command)
        self.assertIn("--sport", command)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("阻断断网", texts[0])
        self.assertIn("web-server", texts[0])

    async def test_limit_limits_running_only(self):
        context = make_context()
        self._enable_guard(action="limit")
        with self._patch_fetch(), \
             patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          return_value=(0, "GUARD-LIMITED", "")) as mock_run:
            await he._traffic_guard_step(context,
                                         instances=self._running_instances())
        mock_run.assert_called_once()
        _host, command = mock_run.call_args.args
        self.assertIn("htb", command)
        self.assertIn("1600kbit", command)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("限速 200K", texts[0])
        self.assertIn("web-server", texts[0])

    async def test_block_fetches_instances_when_none_passed(self):
        context = make_context()
        self._enable_guard(action="block")
        with self._patch_fetch(), \
             patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "list_instances",
                          return_value=self._running_instances()), \
             patch.object(he, "attach_public_ips",
                          side_effect=lambda cli, insts: insts), \
             patch.object(he, "run_remote_command",
                          return_value=(0, "GUARD-BLOCKED", "")) as mock_run:
            await he._traffic_guard_step(context)
        mock_run.assert_called_once()

    async def test_block_failure_reported_per_instance(self):
        context = make_context()
        self._enable_guard(action="block")
        with self._patch_fetch(), \
             patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          side_effect=RuntimeError("boom")):
            await he._traffic_guard_step(context,
                                         instances=self._running_instances())
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("未执行", texts[0])
        self.assertIn("web-server", texts[0])

    async def test_cooldown_blocks_repeat_within_24h(self):
        context = make_context()
        now = datetime.now(timezone.utc)
        self._enable_guard(action="notify",
                           last_triggered=now.isoformat())
        with self._patch_fetch(), \
             patch.object(he, "run_remote_command") as mock_run:
            await he._traffic_guard_step(context,
                                         instances=self._running_instances())
            mock_run.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_triggers_again_after_cooldown(self):
        context = make_context()
        old = (datetime.now(timezone.utc)
               - timedelta(hours=25)).isoformat()
        self._enable_guard(action="notify", last_triggered=old)
        with self._patch_fetch():
            await he._traffic_guard_step(context)
        context.bot.send_message.assert_awaited_once()

    async def test_usage_failure_notifies(self):
        context = make_context()
        self._enable_guard()
        with self._patch_fetch((None, "全部实例都连不上")):
            await he._traffic_guard_step(context)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("流量守护检查失败", texts[0])


class TestTrafficGuardCommands(MonitoringBotTestCase):
    """v1.7.2：动作/阈值辅助函数 + 远程命令生成 + vnstat 解析。"""

    def test_fmt_threshold_gb(self):
        self.assertEqual(mon.fmt_threshold_gb(10), "10GB")
        self.assertEqual(mon.fmt_threshold_gb(200), "200GB")
        self.assertEqual(mon.fmt_threshold_gb(9216), "9TB")
        self.assertEqual(mon.fmt_threshold_gb(1024), "1TB")
        self.assertEqual(mon.fmt_threshold_gb(2048), "2TB")

    def test_action_labels_and_cycle(self):
        self.assertEqual(mon.traffic_guard_action_label("notify"), "仅通知")
        self.assertEqual(mon.traffic_guard_action_label("block"),
                         "阻断断网（保留SSH）")
        self.assertEqual(mon.traffic_guard_action_label("limit"), "限速200K")
        self.assertEqual(mon.traffic_guard_action_label("bogus"), "仅通知")
        self.assertEqual(mon.next_traffic_guard_action("notify"), "block")
        self.assertEqual(mon.next_traffic_guard_action("block"), "limit")
        self.assertEqual(mon.next_traffic_guard_action("limit"), "notify")
        self.assertEqual(mon.next_traffic_guard_action("bogus"), "notify")

    def test_old_shutdown_action_falls_back_to_notify(self):
        """旧版存的 shutdown 动作读出来回退到仅通知（不再有关机动作）。"""
        mon.save_traffic_guard({"enabled": True, "threshold_gb": 200,
                                "action": "shutdown", "last_triggered": None,
                                "chat_id": TEST_CHAT_ID})
        self.assertEqual(mon.load_traffic_guard()["action"], "notify")

    def test_defaults_follow_script(self):
        """默认阈值 200GB、预设 10/20/200/9216，照 auto_limit.sh。"""
        self.assertEqual(mon.TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB, 200)
        self.assertEqual(mon.TRAFFIC_GUARD_THRESHOLD_PRESETS,
                         [10, 20, 200, 9216])
        self.assertEqual(mon.default_traffic_guard()["threshold_gb"], 200)
        self.assertEqual(mon.TRAFFIC_GUARD_RATE_LIMIT, "1600kbit")

    def test_block_command_keeps_ssh_open(self):
        """阻断命令：放行回环/已建立连接/SSH 端口/内网，外网 DROP。"""
        cmd = mon.guard_block_command()
        self.assertIn("TRAFFIC_GUARD", cmd)
        self.assertIn("-o lo -j ACCEPT", cmd)
        self.assertIn("ESTABLISHED,RELATED", cmd)
        self.assertIn('--sport "$SSH_PORT"', cmd)
        self.assertIn("-d 10.0.0.0/8 -j ACCEPT", cmd)
        self.assertIn('-o "$IF" -j DROP', cmd)
        self.assertIn("GUARD-BLOCKED", cmd)
        self.assertIn("GUARD-ALREADY-BLOCKED", cmd)

    def test_limit_command_htb_1600kbit(self):
        cmd = mon.guard_limit_command()
        self.assertIn("htb", cmd)
        self.assertIn("1600kbit", cmd)
        self.assertIn("GUARD-LIMITED", cmd)
        self.assertIn("GUARD-ALREADY-LIMITED", cmd)
        self.assertIn("800kbit", mon.guard_limit_command("800kbit"))

    def test_unblock_command_removes_both(self):
        cmd = mon.guard_unblock_command()
        self.assertIn("iptables -D OUTPUT -j TRAFFIC_GUARD", cmd)
        self.assertIn("iptables -X TRAFFIC_GUARD", cmd)
        self.assertIn("tc qdisc del", cmd)
        self.assertIn("GUARD-UNBLOCKED", cmd)

    def test_vnstat_command_and_parser(self):
        cmd = mon.guard_vnstat_command()
        self.assertIn("vnstat", cmd)
        self.assertIn("--json m", cmd)
        sample = ('{"interfaces": [{"name": "enp0s6", "traffic": {"month": ['
                  '{"date": {"year": 2026, "month": 10}, '
                  '"rx": 1073741824, "tx": 2147483648}, '
                  '{"date": {"year": 2026, "month": 11}, '
                  '"rx": 11967212635, "tx": 7227582128}]}}]}')
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        rx, tx = mon.parse_vnstat_monthly_gb(sample, now=now)
        self.assertAlmostEqual(rx, 11.15, places=2)
        self.assertAlmostEqual(tx, 6.73, places=2)
        # 没有当月条目时取最后一条。
        now_oct = datetime(2026, 10, 5, tzinfo=timezone.utc)
        rx2, tx2 = mon.parse_vnstat_monthly_gb(sample, now=now_oct)
        self.assertAlmostEqual(rx2, 1.0, places=2)
        self.assertAlmostEqual(tx2, 2.0, places=2)
        # 解析失败 / 无数据返回 None。
        self.assertIsNone(mon.parse_vnstat_monthly_gb("not json"))
        self.assertIsNone(mon.parse_vnstat_monthly_gb(
            '{"interfaces": [{"traffic": {"month": []}}]}'))

    def test_fetch_city_for_ip(self):
        resp = MagicMock()
        resp.read.return_value = (
            b'{"status": "success", "city": "\\u65b0\\u52a0\\u5761"}')
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch.object(mon.urllib.request, "urlopen",
                          return_value=resp) as urlopen_mock:
            self.assertEqual(mon.fetch_city_for_ip("203.0.113.11"),
                             "新加坡")
        url = urlopen_mock.call_args[0][0].full_url
        self.assertIn("ip-api.com/json/203.0.113.11", url)
        self.assertIn("lang=zh-CN", url)

    def test_fetch_city_for_ip_failures(self):
        self.assertEqual(mon.fetch_city_for_ip(None), "未知地区")
        self.assertEqual(mon.fetch_city_for_ip(""), "未知地区")
        with patch.object(mon.urllib.request, "urlopen",
                          side_effect=RuntimeError("timeout")):
            self.assertEqual(mon.fetch_city_for_ip("1.2.3.4"), "未知地区")
        resp = MagicMock()
        resp.read.return_value = b'{"status": "fail"}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch.object(mon.urllib.request, "urlopen",
                          return_value=resp):
            self.assertEqual(mon.fetch_city_for_ip("1.2.3.4"), "未知地区")


class TestGuardFetch(MonitoringBotTestCase):
    """v1.7.2：_fetch_guard_monthly_gb 与 _run_guard_command_on_instances。"""

    def _vnstat_json(self, tx_gb):
        import json as _json
        tx_bytes = int(tx_gb * 1024 * 1024 * 1024)
        return _json.dumps({"interfaces": [{"name": "enp0s6", "traffic": {
            "month": [{"date": {"year": 2026, "month": 11},
                       "rx": 0, "tx": tx_bytes}]}}]})

    async def test_fetch_sums_running_instances(self):
        context = make_context()
        instances = [dict(FAKE_INSTANCES[0],
                          **{"public-ip": "203.0.113.10"}),
                     dict(FAKE_INSTANCES[1],
                          **{"public-ip": "203.0.113.11"})]
        with patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          return_value=(0, self._vnstat_json(6.73), "")):
            total, note = await he._fetch_guard_monthly_gb(
                context, instances=instances)
        # 只有 RUNNING 的 web-server 被查（STOPPED 的 e2-test 跳过）。
        self.assertAlmostEqual(total, 6.73, places=2)
        self.assertIn("1 台可达", note)

    async def test_fetch_no_key_returns_none(self):
        context = make_context()
        total, note = await he._fetch_guard_monthly_gb(
            context, instances=self._running_instances())
        self.assertIsNone(total)
        self.assertIn("未配置 SSH 密钥", note)

    def _running_instances(self):
        return [dict(FAKE_INSTANCES[0], **{"public-ip": "203.0.113.10"})]

    async def test_run_command_skips_stopped_and_reports_failure(self):
        instances = [dict(FAKE_INSTANCES[0],
                          **{"public-ip": "203.0.113.10"}),
                     dict(FAKE_INSTANCES[1],
                          **{"public-ip": "203.0.113.11"})]
        with patch.object(bot_config, "GUARD_SSH_KEY", "/tmp/fake-key"), \
             patch.object(he, "run_remote_command",
                          side_effect=RuntimeError("ssh down")):
            ok_names, failed = await he._run_guard_command_on_instances(
                "echo hi", instances)
        self.assertEqual(ok_names, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0][0], "web-server")
        self.assertIn("ssh down", failed[0][1])


class TestCheckInstancesJobIntegration(MonitoringBotTestCase):
    """流量守护 / 停机保活不依赖监控开关；自启与保活不重复启动。"""

    async def test_guard_runs_when_monitor_off(self):
        context = make_context()  # 监控开关没开
        mon.save_traffic_guard({"enabled": True, "threshold_gb": 200,
                                "action": "notify", "last_triggered": None,
                                "chat_id": TEST_CHAT_ID})
        with patch.object(he, "_fetch_guard_monthly_gb",
                          new=AsyncMock(return_value=(250.0, "2 台可达"))), \
             patch.object(he, "list_instances") as mock_list:
            await he.check_instances_job(context)
            mock_list.assert_not_called()  # 仅通知不需要实例列表
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("流量守护提醒", texts[0])

    async def test_keepalive_runs_when_monitor_off(self):
        context = make_context()
        mon.save_keepalive({STOPPED_ID: True})
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {}, "gave_up": []})
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "start_instance",
                          return_value=None) as mock_start:
            await he.check_instances_job(context)
        mock_start.assert_called_once_with(self.cli, STOPPED_ID)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("停机保活", texts[0])
        self.assertIn("已自动启动", texts[0])

    async def test_autostart_and_keepalive_do_not_double_start(self):
        context = make_context()
        context.bot_data["monitor"] = {"on": True, "autostart": True,
                                       "chat_id": TEST_CHAT_ID}
        context.bot_data["last_states"] = {RUNNING_ID: "RUNNING",
                                           STOPPED_ID: "STOPPED"}
        mon.save_keepalive({STOPPED_ID: True})
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {}, "gave_up": []})
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES), \
             patch.object(he, "start_instance",
                          return_value=None) as mock_start:
            await he.check_instances_job(context)
        # 自启与保活都盯着同一台 STOPPED 实例，只启动一次。
        mock_start.assert_called_once_with(self.cli, STOPPED_ID)


# ================= 停机保活 =================

class TestKeepaliveMenu(MonitoringBotTestCase):

    async def test_menu_lists_instances_with_switches(self):
        update = make_update(callback_data="o:keepalive")
        context = make_context()
        mon.save_keepalive({STOPPED_ID: True})
        with patch.object(he, "list_instances",
                          return_value=FAKE_INSTANCES):
            await he.handle_keepalive(update, context)
        text = reply_text_of(update)
        self.assertIn("✅ e2-test（STOPPED）", text)
        self.assertIn("⬜ web-server（RUNNING）", text)
        data = button_data(reply_markup_of(update))
        self.assertIn("k:toggle:0", data)
        self.assertIn("k:toggle:1", data)

    async def test_toggle_enables_and_records_chat_id(self):
        update = make_update(callback_data="k:toggle:1")
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        await he.keepalive_callback(update, context)
        self.assertEqual(mon.load_keepalive(), {STOPPED_ID: True})
        self.assertEqual(mon.load_keepalive_state()["chat_id"], TEST_CHAT_ID)
        self.assertIn("✅ e2-test", reply_text_of(update))

    async def test_toggle_twice_disables(self):
        update = make_update(callback_data="k:toggle:1")
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        await he.keepalive_callback(update, context)
        await he.keepalive_callback(update, context)
        self.assertEqual(mon.load_keepalive(), {STOPPED_ID: False})

    async def test_toggle_clears_gave_up(self):
        update = make_update(callback_data="k:toggle:1")
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        mon.save_keepalive({STOPPED_ID: True})
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {STOPPED_ID: 3},
                                  "gave_up": [STOPPED_ID]})
        # 关掉再打开：停试标记清掉。
        await he.keepalive_callback(update, context)
        await he.keepalive_callback(update, context)
        state = mon.load_keepalive_state()
        self.assertEqual(state["gave_up"], [])
        self.assertEqual(state["failures"], {})
        self.assertTrue(mon.load_keepalive()[STOPPED_ID])

    async def test_toggle_invalid_index(self):
        update = make_update(callback_data="k:toggle:9")
        context = make_context()
        context.user_data["instances"] = list(FAKE_INSTANCES)
        await he.keepalive_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))


class TestKeepaliveStep(MonitoringBotTestCase):

    def _enable(self, *ocids):
        mon.save_keepalive({ocid: True for ocid in ocids})
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {}, "gave_up": []})

    async def test_nothing_enabled_does_nothing(self):
        context = make_context()
        with patch.object(he, "list_instances") as mock_list:
            await he._keepalive_step(context)
            mock_list.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_stopped_enabled_instance_is_started(self):
        context = make_context()
        self._enable(STOPPED_ID)
        with patch.object(he, "start_instance",
                          return_value=None) as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
        mock_start.assert_called_once_with(self.cli, STOPPED_ID)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("停机保活", texts[0])
        self.assertIn("已自动启动实例 e2-test", texts[0])

    async def test_running_enabled_instance_is_ignored(self):
        context = make_context()
        self._enable(RUNNING_ID)
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
            mock_start.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_not_enabled_stopped_instance_is_ignored(self):
        context = make_context()
        self._enable(RUNNING_ID)  # 只开 RUNNING 那台
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
            mock_start.assert_not_called()

    async def test_missing_instance_is_skipped(self):
        context = make_context()
        self._enable("ocid1.instance.oc1..deletedfake")
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
            mock_start.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_skip_ids_are_not_started(self):
        context = make_context()
        self._enable(STOPPED_ID)
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES,
                                     skip_ids={STOPPED_ID})
            mock_start.assert_not_called()

    async def test_failure_count_and_give_up_after_three(self):
        context = make_context()
        self._enable(STOPPED_ID)
        with patch.object(he, "start_instance",
                          side_effect=RuntimeError("boom")):
            for _ in range(3):
                await he._keepalive_step(context, instances=FAKE_INSTANCES)
        state = mon.load_keepalive_state()
        self.assertEqual(state["failures"][STOPPED_ID], 3)
        self.assertIn(STOPPED_ID, state["gave_up"])
        texts = sent_texts(context)
        self.assertEqual(len(texts), 3)
        self.assertIn("第 1 次", texts[0])
        self.assertIn("第 2 次", texts[1])
        self.assertIn("连续 3 次", texts[2])
        self.assertIn("已停止尝试", texts[2])
        # 第 4 次不再尝试。
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
            mock_start.assert_not_called()
        self.assertEqual(len(sent_texts(context)), 3)

    async def test_success_clears_failure_count(self):
        context = make_context()
        self._enable(STOPPED_ID)
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {STOPPED_ID: 2},
                                  "gave_up": []})
        with patch.object(he, "start_instance", return_value=None):
            await he._keepalive_step(context, instances=FAKE_INSTANCES)
        self.assertEqual(mon.load_keepalive_state()["failures"], {})

    async def test_running_instance_clears_failure_count(self):
        context = make_context()
        self._enable(STOPPED_ID)
        mon.save_keepalive_state({"chat_id": TEST_CHAT_ID,
                                  "failures": {STOPPED_ID: 1},
                                  "gave_up": []})
        running_now = [dict(FAKE_INSTANCES[1], **{"lifecycle-state": "RUNNING"})]
        with patch.object(he, "start_instance") as mock_start:
            await he._keepalive_step(context, instances=running_now)
            mock_start.assert_not_called()
        self.assertEqual(mon.load_keepalive_state()["failures"], {})

    async def test_list_failure_notifies(self):
        context = make_context()
        self._enable(STOPPED_ID)
        with patch.object(he, "list_instances",
                          side_effect=RuntimeError("api down")):
            await he._keepalive_step(context)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("停机保活检查失败", texts[0])


# ================= SSL 监控 =================

class TestSslWatchMenu(MonitoringBotTestCase):

    def _ssl_result(self, days_left, domain="example.com"):
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=days_left)).date().isoformat()
        return {"domain": domain, "expires": expires, "days_left": days_left}

    async def test_empty_menu(self):
        update = make_update(callback_data="o:sslwatch")
        context = make_context()
        with patch.object(he, "check_ssl_expiry") as mock_ssl:
            await he.handle_sslwatch(update, context)
            mock_ssl.assert_not_called()
        self.assertIn("未监控任何域名", reply_text_of(update))
        self.assertIn("s:add", button_data(reply_markup_of(update)))

    async def test_menu_shows_days_left(self):
        update = make_update(callback_data="o:sslwatch")
        context = make_context()
        mon.save_ssl_watch({"domains": ["example.com", "www.example.org"],
                            "reminded": {}, "chat_id": TEST_CHAT_ID})
        with patch.object(he, "check_ssl_expiry",
                          side_effect=lambda d: self._ssl_result(
                              45 if d == "example.com" else 0, d)):
            await he.handle_sslwatch(update, context)
        text = reply_text_of(update)
        self.assertIn("example.com：还有 45 天到期", text)
        self.assertIn("www.example.org：⚠️ 今天到期", text)
        data = button_data(reply_markup_of(update))
        self.assertIn("s:del:0", data)
        self.assertIn("s:del:1", data)
        self.assertIn("s:refresh", data)

    async def test_menu_shows_expired_and_failed(self):
        update = make_update(callback_data="o:sslwatch")
        context = make_context()
        mon.save_ssl_watch({"domains": ["old.example.com", "down.example.com"],
                            "reminded": {}, "chat_id": TEST_CHAT_ID})

        def fake_ssl(domain):
            if domain == "down.example.com":
                raise RuntimeError("connect fail")
            return self._ssl_result(-3, domain)

        with patch.object(he, "check_ssl_expiry", side_effect=fake_ssl):
            await he.handle_sslwatch(update, context)
        text = reply_text_of(update)
        self.assertIn("old.example.com：⚠️ 已过期 3 天", text)
        self.assertIn("down.example.com：检查失败", text)

    async def test_add_prompts_for_text(self):
        update = make_update(callback_data="s:add")
        context = make_context()
        await he.ssl_watch_callback(update, context)
        self.assertEqual(context.user_data.get("pending_input"),
                         {"kind": "ssl_domain"})
        self.assertIn("请发送要监控的域名", reply_text_of(update))
        self.assertIn("s:add_cancel", button_data(reply_markup_of(update)))

    async def test_add_cancel_clears_pending(self):
        update = make_update(callback_data="s:add_cancel")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "ssl_domain"}
        with patch.object(he, "check_ssl_expiry") as mock_ssl:
            await he.ssl_watch_callback(update, context)
            mock_ssl.assert_not_called()
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("未监控任何域名", reply_text_of(update))

    async def test_delete_removes_domain(self):
        update = make_update(callback_data="s:del:0")
        context = make_context()
        mon.save_ssl_watch({"domains": ["example.com", "www.example.org"],
                            "reminded": {"example.com": [30]},
                            "chat_id": None})
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(45, "www.example.org")):
            await he.ssl_watch_callback(update, context)
        cfg = mon.load_ssl_watch()
        self.assertEqual(cfg["domains"], ["www.example.org"])
        self.assertNotIn("example.com", cfg["reminded"])
        self.assertEqual(cfg["chat_id"], TEST_CHAT_ID)

    async def test_delete_invalid_index_ignored(self):
        update = make_update(callback_data="s:del:5")
        context = make_context()
        mon.save_ssl_watch({"domains": ["example.com"],
                            "reminded": {}, "chat_id": None})
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(45)):
            await he.ssl_watch_callback(update, context)
        self.assertEqual(mon.load_ssl_watch()["domains"], ["example.com"])

    async def test_unknown_action_rejected(self):
        update = make_update(callback_data="s:bogus")
        context = make_context()
        await he.ssl_watch_callback(update, context)
        self.assertIn("参数错误", reply_text_of(update))


class TestSslDomainInput(MonitoringBotTestCase):

    def _ssl_result(self, days_left, domain="example.com"):
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=days_left)).date().isoformat()
        return {"domain": domain, "expires": expires, "days_left": days_left}

    async def test_add_valid_domain_via_text(self):
        update = make_update(text="https://Example.COM/path")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "ssl_domain"}
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(45)):
            await he.text_input_callback(update, context)
        cfg = mon.load_ssl_watch()
        self.assertEqual(cfg["domains"], ["example.com"])
        self.assertEqual(cfg["chat_id"], TEST_CHAT_ID)
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("example.com：还有 45 天到期", reply_text_of(update))

    async def test_add_invalid_domain_rejected(self):
        update = make_update(text="not a domain")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "ssl_domain"}
        await he.text_input_callback(update, context)
        self.assertEqual(mon.load_ssl_watch()["domains"], [])
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("域名格式不正确", reply_text_of(update))

    async def test_add_duplicate_domain(self):
        update = make_update(text="example.com")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "ssl_domain"}
        mon.save_ssl_watch({"domains": ["example.com"],
                            "reminded": {}, "chat_id": TEST_CHAT_ID})
        await he.text_input_callback(update, context)
        self.assertEqual(mon.load_ssl_watch()["domains"], ["example.com"])
        self.assertIn("已在 SSL 监控列表里", reply_text_of(update))

    async def test_text_without_pending_is_ignored(self):
        update = make_update(text="example.com")
        context = make_context()
        await he.text_input_callback(update, context)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(mon.load_ssl_watch()["domains"], [])


class TestSslCheckJob(MonitoringBotTestCase):

    def _watch(self, domains, reminded=None, chat_id=TEST_CHAT_ID):
        mon.save_ssl_watch({"domains": domains,
                            "reminded": reminded or {},
                            "chat_id": chat_id})

    def _ssl_result(self, days_left, domain="example.com"):
        expires = (datetime.now(timezone.utc)
                   + timedelta(days=days_left)).date().isoformat()
        return {"domain": domain, "expires": expires, "days_left": days_left}

    async def test_empty_domains_does_nothing(self):
        context = make_context()
        with patch.object(he, "check_ssl_expiry") as mock_ssl:
            await he.ssl_check_job(context)
            mock_ssl.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_no_chat_id_does_nothing(self):
        context = make_context()
        self._watch(["example.com"], chat_id=None)
        with patch.object(he, "check_ssl_expiry") as mock_ssl:
            await he.ssl_check_job(context)
            mock_ssl.assert_not_called()
        context.bot.send_message.assert_not_awaited()

    async def test_reminder_at_30_days_then_no_repeat(self):
        context = make_context()
        self._watch(["example.com"])
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(25)):
            await he.ssl_check_job(context)
            await he.ssl_check_job(context)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("SSL 证书到期提醒", texts[0])
        self.assertIn("example.com", texts[0])
        self.assertIn("还有 25 天到期", texts[0])
        self.assertEqual(mon.load_ssl_watch()["reminded"],
                         {"example.com": [30]})

    async def test_next_threshold_reminds_again(self):
        context = make_context()
        self._watch(["example.com"], reminded={"example.com": [30]})
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(14)):
            await he.ssl_check_job(context)
        self.assertEqual(len(sent_texts(context)), 1)
        self.assertEqual(mon.load_ssl_watch()["reminded"],
                         {"example.com": [14, 30]})

    async def test_late_added_domain_sends_one_message(self):
        context = make_context()
        self._watch(["example.com"])
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(5)):
            await he.ssl_check_job(context)
        # 30/14/7 都已跨过，但只发一条提醒。
        self.assertEqual(len(sent_texts(context)), 1)
        self.assertEqual(mon.load_ssl_watch()["reminded"],
                         {"example.com": [7, 14, 30]})

    async def test_far_from_expiry_no_message(self):
        context = make_context()
        self._watch(["example.com"])
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(45)):
            await he.ssl_check_job(context)
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(mon.load_ssl_watch()["reminded"], {})

    async def test_renewal_prunes_reminded(self):
        context = make_context()
        self._watch(["example.com"], reminded={"example.com": [30, 14]})
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(90)):
            await he.ssl_check_job(context)
        context.bot.send_message.assert_not_awaited()
        self.assertEqual(mon.load_ssl_watch()["reminded"],
                         {"example.com": []})

    async def test_expired_cert_reminds(self):
        context = make_context()
        self._watch(["example.com"])
        with patch.object(he, "check_ssl_expiry",
                          return_value=self._ssl_result(-2)):
            await he.ssl_check_job(context)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("已过期 2 天", texts[0])

    async def test_check_failure_notified_others_still_checked(self):
        context = make_context()
        self._watch(["down.example.com", "example.com"])

        def fake_ssl(domain):
            if domain == "down.example.com":
                raise RuntimeError("无法获取 down.example.com 的 SSL 证书")
            return self._ssl_result(45, domain)

        with patch.object(he, "check_ssl_expiry", side_effect=fake_ssl):
            await he.ssl_check_job(context)
        texts = sent_texts(context)
        self.assertEqual(len(texts), 1)
        self.assertIn("down.example.com", texts[0])
        self.assertIn("检查失败", texts[0])


# ================= 菜单分发 =================

class TestDispatchMonitoring(MonitoringBotTestCase):

    async def test_dispatch_new_items_return_true(self):
        for item in ("traffic_guard", "keepalive", "sslwatch"):
            with self.subTest(item=item):
                update = make_update(callback_data=f"o:{item}")
                context = make_context()
                with patch.object(he, "fetch_usage_summary",
                                  return_value=[]), \
                     patch.object(he, "list_instances",
                                  return_value=[]), \
                     patch.object(he, "check_ssl_expiry",
                                  return_value={"domain": "example.com",
                                                "expires": "2027-01-01",
                                                "days_left": 45}):
                    result = await he.dispatch_oracle_action(
                        item, update, context)
                self.assertTrue(result)
                self.assertIsNotNone(reply_text_of(update))


if __name__ == "__main__":
    unittest.main()
