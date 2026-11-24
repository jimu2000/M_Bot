#!/usr/bin/env python3
"""tgbot /domain 命令（Cloudflare DNS）的单元测试。

全部用 mock，不碰真实 Cloudflare API。

约定：
  - mock Update/Context：reply_text / callback_query.answer 用 AsyncMock；
    user_data / bot_data 用真实 dict。
  - 白名单：把 tgbot.config.ALLOWED_USER_IDS 临时设为 {TEST_USER_ID}。
  - Cloudflare 客户端 patch tgbot.handlers_extra 命名空间里的 get_client
    （from-import，与 oci 函数的测试约定一致）。
  - 测试数据只用文档保留值（example.com、203.0.113.x、假 zone id），
    绝不出现真实 IP/OCID/Token。

运行：python3 -m unittest tests.test_tgbot_domain -v
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
import tgbot.handlers_extra as he
from tgbot.handlers_extra import CloudflareError
from tgbot.keyboards import domain_records_keyboard, domain_zones_keyboard

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
OTHER_USER_ID = 999999

FAKE_ZONE_ID = "zoneid000000000000000000000001"
FAKE_ZONE_ID_2 = "zoneid000000000000000000000002"

FAKE_ZONES = [
    {"id": FAKE_ZONE_ID, "name": "example.com", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net"]},
    {"id": FAKE_ZONE_ID_2, "name": "example.org", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net"]},
]

FAKE_RECORDS = [
    {"id": "recid000000000000000000000001", "type": "A",
     "name": "www.example.com", "content": "203.0.113.10",
     "proxied": True, "ttl": 1,
     "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"},
    {"id": "recid000000000000000000000002", "type": "TXT",
     "name": "example.com", "content": "v=spf1 -all",
     "proxied": False, "ttl": 3600,
     "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"},
    {"id": "recid000000000000000000000003", "type": "MX",
     "name": "example.com", "content": "mail.example.com",
     "proxied": False, "ttl": 3600,
     "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"},
]


def _cf_error(kind):
    """构造指定 kind 的 CloudflareError（先传 message 再设 kind，
    对正式版与替身版的构造函数都兼容）。"""
    err = CloudflareError("mock cloudflare failure")
    err.kind = kind
    return err


def make_update(callback_data=None, user_id=TEST_USER_ID):
    """mock Update：effective_message.reply_text 与 callback_query.answer
    都是 AsyncMock。"""
    update = MagicMock()
    update.effective_user.id = user_id
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
    """mock Context：user_data / bot_data 用真实 dict，bot.delete_message
    用 AsyncMock（凭据输入流程会删用户发的含凭据消息）。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
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


def button_callback_data(markup):
    """取键盘上全部按钮的 callback_data 列表。"""
    if markup is None:
        return []
    return [btn.callback_data
            for row in markup.inline_keyboard for btn in row]


class DomainTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、mock CF 客户端。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        self.cf = MagicMock()
        self.cf.list_zones.return_value = FAKE_ZONES
        self.cf.list_dns_records.return_value = FAKE_RECORDS
        self._cf_patch = patch.object(he, "get_client",
                                      return_value=self.cf)
        self._cf_patch.start()
        self.addCleanup(self._cf_patch.stop)
        # v1.26.0：_get_cf_client 优先走 tgbot.cf_config 的包装
        # （文件优先，环境变量兜底），这里也要 mock 掉。
        self._cf_cfg_patch = patch.object(he, "get_configured_cf_client",
                                          return_value=self.cf)
        self._cf_cfg_patch.start()
        self.addCleanup(self._cf_cfg_patch.stop)


class TestDomainCommand(DomainTestCase):
    """`/domain` 命令：列 Zone。"""

    async def test_domain_lists_zones(self):
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        text = reply_text_of(update)
        self.assertIsNotNone(text)
        self.assertIn("example.com", text)
        self.assertIn("example.org", text)
        self.assertIn("2 个 Zone", text)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID}", callbacks)
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID_2}", callbacks)
        self.cf.list_zones.assert_called_once_with()
        # Zone 列表存进 user_data，供回调解析 Zone 名。
        self.assertEqual(context.user_data["domain_zones"], FAKE_ZONES)

    async def test_domain_no_zones(self):
        self.cf.list_zones.return_value = []
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        self.assertIn("没有 Zone", reply_text_of(update))

    async def test_domain_non_whitelisted_rejected(self):
        """非白名单用户发 /domain 被拒绝，且不调用 Cloudflare。"""
        update = make_update(user_id=OTHER_USER_ID)
        context = make_context()
        await he.handle_domain(update, context)
        self.assertIn("未授权", reply_text_of(update))
        self.cf.list_zones.assert_not_called()

    async def test_domain_cf_auth_error(self):
        self.cf.list_zones.side_effect = _cf_error("auth")
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        self.assertIn("未配置或认证失败", reply_text_of(update))

    async def test_domain_cf_client_not_ready(self):
        """clouds.cloudflare 与 cf_config 都未就绪时报配置错误。"""
        with patch.object(he, "get_client", None), \
                patch.object(he, "get_configured_cf_client", None):
            update = make_update()
            context = make_context()
            await he.handle_domain(update, context)
        self.assertIn("未配置或认证失败", reply_text_of(update))


class TestDomainCallback(DomainTestCase):
    """`domain:` 回调：查记录 / 返回 Zone 列表。"""

    async def test_zone_callback_lists_records(self):
        update = make_update(callback_data=f"domain:zone:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        text = reply_text_of(update)
        self.assertIsNotNone(text)
        self.assertIn("example.com", text)
        self.assertIn("共 3 条", text)
        self.assertIn("A www.example.com -> 203.0.113.10 🟠", text)
        self.assertIn("TXT example.com -> v=spf1 -all", text)
        self.assertIn("MX example.com -> mail.example.com", text)
        # 只有 proxied 的那条带 🟠。
        self.assertEqual(text.count("🟠"), 1)
        self.cf.list_dns_records.assert_called_once_with(FAKE_ZONE_ID)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("domain:back", callbacks)
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID}", callbacks)

    async def test_zone_callback_resolves_name_from_records(self):
        """user_data 里没存 Zone 列表时，从记录的 zone_name 解析 Zone 名。"""
        update = make_update(callback_data=f"domain:zone:{FAKE_ZONE_ID}")
        context = make_context()  # domain_zones 为空
        await he.domain_callback(update, context)
        self.assertIn("example.com", reply_text_of(update))

    async def test_zone_callback_no_records(self):
        self.cf.list_dns_records.return_value = []
        update = make_update(callback_data=f"domain:zone:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        self.assertIn("没有 DNS 记录", reply_text_of(update))

    async def test_zone_callback_not_found(self):
        self.cf.list_dns_records.side_effect = _cf_error("not_found")
        update = make_update(callback_data="domain:zone:bad-zone-id")
        context = make_context()
        await he.domain_callback(update, context)
        self.assertIn("不存在", reply_text_of(update))

    async def test_zone_callback_non_whitelisted_rejected(self):
        update = make_update(callback_data=f"domain:zone:{FAKE_ZONE_ID}",
                             user_id=OTHER_USER_ID)
        context = make_context()
        await he.domain_callback(update, context)
        self.assertIn("未授权", reply_text_of(update))
        self.cf.list_dns_records.assert_not_called()

    async def test_back_callback_lists_zones(self):
        update = make_update(callback_data="domain:back")
        context = make_context()
        await he.domain_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("example.com", text)
        self.assertIn("2 个 Zone", text)
        self.cf.list_zones.assert_called_once_with()

    async def test_unknown_domain_callback(self):
        update = make_update(callback_data="domain:unknown")
        context = make_context()
        await he.domain_callback(update, context)
        self.assertIn("参数错误", reply_text_of(update))
        self.cf.list_dns_records.assert_not_called()
        self.cf.list_zones.assert_not_called()


class TestDomainFormatting(unittest.TestCase):
    """纯格式化函数（不需要 mock 客户端）。"""

    def test_fmt_domain_records_truncation(self):
        """记录过多时截断到 DOMAIN_RECORDS_LIMIT 条并提示总数。"""
        records = [
            {"type": "A", "name": f"host{i}.example.com",
             "content": f"203.0.113.{i}", "proxied": False}
            for i in range(1, 26)
        ]
        text = he.fmt_domain_records("example.com", records)
        self.assertIn("共 25 条", text)
        self.assertIn("host1.example.com", text)
        self.assertIn(f"host{he.DOMAIN_RECORDS_LIMIT}.example.com", text)
        self.assertNotIn("host21.example.com", text)
        self.assertIn("还有 5 条未显示", text)

    def test_fmt_domain_record_line_proxied_mark(self):
        line = he.fmt_domain_record_line(
            {"type": "A", "name": "www.example.com",
             "content": "203.0.113.10", "proxied": True})
        self.assertEqual(line, "• A www.example.com -> 203.0.113.10 🟠")
        line = he.fmt_domain_record_line(
            {"type": "A", "name": "api.example.com",
             "content": "203.0.113.20", "proxied": False})
        self.assertEqual(line, "• A api.example.com -> 203.0.113.20")

    def test_fmt_cf_error_mapping(self):
        self.assertIn("未配置或认证失败",
                      he.fmt_cf_error(_cf_error("config")))
        self.assertIn("未配置或认证失败",
                      he.fmt_cf_error(_cf_error("auth")))
        self.assertIn("不存在", he.fmt_cf_error(_cf_error("not_found")))
        self.assertIn("限流", he.fmt_cf_error(_cf_error("rate_limit")))
        self.assertIn("调用失败", he.fmt_cf_error(_cf_error("api")))
        # 原始错误细节不进提示文本。
        self.assertNotIn("mock cloudflare failure",
                         he.fmt_cf_error(_cf_error("auth")))


class TestDomainKeyboards(unittest.TestCase):
    """domain 键盘的 callback_data 前缀。"""

    def test_zones_keyboard(self):
        markup = domain_zones_keyboard(FAKE_ZONES)
        callbacks = button_callback_data(markup)
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID}", callbacks)
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID_2}", callbacks)
        # 按钮文本带 Zone 名。
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any("example.com" in label for label in labels))

    def test_records_keyboard(self):
        markup = domain_records_keyboard(FAKE_ZONE_ID, FAKE_RECORDS)
        callbacks = button_callback_data(markup)
        self.assertIn(f"domain:zone:{FAKE_ZONE_ID}", callbacks)
        self.assertIn("domain:back", callbacks)

    def test_callback_data_within_64_bytes(self):
        """domain:zone:<zone_id> 不超 Telegram 64 字节限制。"""
        markup = domain_zones_keyboard(FAKE_ZONES)
        for callback in button_callback_data(markup):
            self.assertLessEqual(len(callback.encode("utf-8")), 64)


# ================= v1.26.0：Cloudflare 凭据管理 =================

FAKE_INSTANCES = [
    {"id": "ocid1.instance.oc1..fake0000000000000000000000000001",
     "display-name": "host-a", "lifecycle-state": "RUNNING",
     "public-ip": "203.0.113.10"},
    {"id": "ocid1.instance.oc1..fake0000000000000000000000000002",
     "display-name": "host-b", "lifecycle-state": "STOPPED",
     "public-ip": "203.0.113.11"},
    {"id": "ocid1.instance.oc1..fake0000000000000000000000000003",
     "display-name": "host-c", "lifecycle-state": "RUNNING",
     "public-ip": None},
]


class TestCfConfigErrorKeyboard(DomainTestCase):
    """`/domain` 未配置/认证失败时，给出添加与提取按钮。"""

    async def test_config_error_shows_add_and_extract(self):
        self.cf.list_zones.side_effect = _cf_error("config")
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        self.assertIn("未配置或认证失败", reply_text_of(update))
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:add", callbacks)
        self.assertIn("cf:extract", callbacks)

    async def test_auth_error_shows_add_and_extract(self):
        self.cf.list_zones.side_effect = _cf_error("auth")
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:add", callbacks)

    async def test_not_found_error_has_no_cf_buttons(self):
        self.cf.list_zones.side_effect = _cf_error("not_found")
        update = make_update()
        context = make_context()
        await he.handle_domain(update, context)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertNotIn("cf:add", callbacks)

    def test_zones_keyboard_has_manage_button(self):
        markup = domain_zones_keyboard(FAKE_ZONES)
        self.assertIn("cf:manage", button_callback_data(markup))


class TestCfCallback(DomainTestCase):
    """`cf:` 回调：添加 / 提取 / 管理 / 删除。"""

    async def test_add_shows_mode_choice(self):
        update = make_update(callback_data="cf:add")
        context = make_context()
        await he.cf_callback(update, context)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:add:token", callbacks)
        self.assertIn("cf:add:key", callbacks)

    async def test_add_token_prompts_input(self):
        update = make_update(callback_data="cf:add:token")
        context = make_context()
        await he.cf_callback(update, context)
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "cf_token")
        self.assertIn("API Token", reply_text_of(update))

    async def test_add_key_prompts_input(self):
        update = make_update(callback_data="cf:add:key")
        context = make_context()
        await he.cf_callback(update, context)
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "cf_key")
        self.assertIn("Global API Key", reply_text_of(update))

    async def test_manage_shows_file_status(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("fake-token", "", "", "file")):
            update = make_update(callback_data="cf:manage")
            context = make_context()
            await he.cf_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("已配置", text)
        self.assertIn("机器人/面板里保存的", text)
        self.assertIn("API Token", text)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:add", callbacks)
        self.assertIn("cf:delete", callbacks)

    async def test_manage_shows_env_status_with_masked_email(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com", "env")):
            update = make_update(callback_data="cf:manage")
            context = make_context()
            await he.cf_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("服务器环境变量里的", text)
        self.assertIn("Global API Key", text)
        self.assertIn("u***@example.com", text)
        self.assertNotIn("user@example.com", text)

    async def test_manage_unconfigured(self):
        with patch.object(he, "resolve_cf_credentials",
                          return_value=("", "", "", None)):
            update = make_update(callback_data="cf:manage")
            context = make_context()
            await he.cf_callback(update, context)
        self.assertIn("未配置", reply_text_of(update))

    async def test_delete_asks_confirm(self):
        update = make_update(callback_data="cf:delete")
        context = make_context()
        await he.cf_callback(update, context)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:delete:yes", callbacks)

    async def test_delete_yes_removes_file_config(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("fake-token", "", "", "file")), \
                patch.object(he, "clear_cf_config") as mock_clear:
            update = make_update(callback_data="cf:delete:yes")
            context = make_context()
            await he.cf_callback(update, context)
        mock_clear.assert_called_once_with()
        self.assertIn("已删除", reply_text_of(update))

    async def test_delete_yes_env_source_refuses(self):
        with patch.object(
                he, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com", "env")), \
                patch.object(he, "clear_cf_config") as mock_clear:
            update = make_update(callback_data="cf:delete:yes")
            context = make_context()
            await he.cf_callback(update, context)
        mock_clear.assert_not_called()
        self.assertIn("没有已保存的凭据文件", reply_text_of(update))

    async def test_cancel_clears_pending(self):
        update = make_update(callback_data="cf:cancel")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "cf_token"}
        context.user_data["cf_draft"] = {"api_key": "fake-key"}
        await he.cf_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        self.assertNotIn("cf_draft", context.user_data)
        self.assertIn("已取消", reply_text_of(update))

    async def test_extract_lists_running_hosts_only(self):
        with patch.object(he, "get_cli", return_value=MagicMock()), \
                patch.object(he, "get_compartment",
                             return_value="ocid1.compartment.oc1..fake"), \
                patch.object(he, "list_instances",
                             return_value=FAKE_INSTANCES), \
                patch.object(he, "attach_public_ips",
                             side_effect=lambda _cli, insts: insts):
            update = make_update(callback_data="cf:extract")
            context = make_context()
            await he.cf_callback(update, context)
        # 只有 host-a（RUNNING + 有公网 IP）进列表。
        hosts = context.user_data["cf_extract_hosts"]
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["display-name"], "host-a")
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:extract:0", callbacks)
        labels = [btn.text for row in reply_markup_of(update).inline_keyboard
                  for btn in row]
        self.assertTrue(any("host-a" in label for label in labels))
        self.assertTrue(any("203.0.113.10" in label for label in labels))

    async def test_extract_no_hosts(self):
        with patch.object(he, "get_cli", return_value=MagicMock()), \
                patch.object(he, "get_compartment",
                             return_value="ocid1.compartment.oc1..fake"), \
                patch.object(he, "list_instances", return_value=[]), \
                patch.object(he, "attach_public_ips",
                             side_effect=lambda _cli, insts: insts):
            update = make_update(callback_data="cf:extract")
            context = make_context()
            await he.cf_callback(update, context)
        self.assertIn("没有运行中", reply_text_of(update))

    async def test_extract_run_success(self):
        context = make_context()
        context.user_data["cf_extract_hosts"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "_guard_host_for_instance",
                          return_value={"host": "203.0.113.10"}), \
                patch.object(he, "extract_cf_from_host",
                             return_value=("fake-key", "user@example.com",
                                           None)), \
                patch.object(he, "validate_and_save",
                             return_value=(True, 6, None)):
            update = make_update(callback_data="cf:extract:0")
            await he.cf_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("已从 host-a 提取", text)
        self.assertIn("6 个 Zone", text)
        self.assertIn("u***@example.com", text)
        self.assertNotIn("fake-key", text)

    async def test_extract_run_ssh_failure(self):
        context = make_context()
        context.user_data["cf_extract_hosts"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "_guard_host_for_instance",
                          return_value={"host": "203.0.113.10"}), \
                patch.object(he, "extract_cf_from_host",
                             return_value=(None, None, "SSH 连接失败：超时")):
            update = make_update(callback_data="cf:extract:0")
            await he.cf_callback(update, context)
        self.assertIn("提取失败", reply_text_of(update))

    async def test_extract_run_validate_failure(self):
        context = make_context()
        context.user_data["cf_extract_hosts"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "_guard_host_for_instance",
                          return_value={"host": "203.0.113.10"}), \
                patch.object(he, "extract_cf_from_host",
                             return_value=("fake-key", "user@example.com",
                                           None)), \
                patch.object(he, "validate_and_save",
                             return_value=(False, 0, "auth")):
            update = make_update(callback_data="cf:extract:0")
            await he.cf_callback(update, context)
        self.assertIn("校验没通过", reply_text_of(update))

    async def test_extract_run_no_guard_key(self):
        context = make_context()
        context.user_data["cf_extract_hosts"] = [FAKE_INSTANCES[0]]
        with patch.object(he, "_guard_host_for_instance", return_value=None):
            update = make_update(callback_data="cf:extract:0")
            await he.cf_callback(update, context)
        self.assertIn("守护 SSH 密钥", reply_text_of(update))

    async def test_extract_run_bad_index(self):
        context = make_context()
        context.user_data["cf_extract_hosts"] = [FAKE_INSTANCES[0]]
        update = make_update(callback_data="cf:extract:9")
        await he.cf_callback(update, context)
        self.assertIn("不在列表里", reply_text_of(update))

    async def test_unknown_cf_callback(self):
        update = make_update(callback_data="cf:unknown")
        context = make_context()
        await he.cf_callback(update, context)
        self.assertIn("参数错误", reply_text_of(update))


class TestCfTextInput(DomainTestCase):
    """text_input_callback 的 cf_token / cf_key / cf_email 三种输入。"""

    def _text_update(self, text):
        update = make_update()
        update.effective_message.text = text
        update.effective_message.chat_id = TEST_CHAT_ID
        update.effective_message.message_id = 777
        return update

    async def test_cf_token_success(self):
        with patch.object(he, "validate_and_save",
                          return_value=(True, 6, None)) as mock_save:
            update = self._text_update("fake-token-123")
            context = make_context()
            context.user_data["pending_input"] = {"kind": "cf_token"}
            await he.text_input_callback(update, context)
        mock_save.assert_called_once_with("fake-token-123", "", "")
        text = reply_text_of(update)
        self.assertIn("已保存并校验通过", text)
        self.assertIn("6 个 Zone", text)
        self.assertNotIn("fake-token-123", text)
        self.assertNotIn("pending_input", context.user_data)
        # 用户发的含凭据消息被删掉。
        context.bot.delete_message.assert_awaited_once()

    async def test_cf_token_validate_failure(self):
        with patch.object(he, "validate_and_save",
                          return_value=(False, 0, "auth")):
            update = self._text_update("bad-token")
            context = make_context()
            context.user_data["pending_input"] = {"kind": "cf_token"}
            await he.text_input_callback(update, context)
        self.assertIn("校验没通过", reply_text_of(update))
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn("cf:add", callbacks)

    async def test_cf_token_blank_cancelled(self):
        update = self._text_update("has space in it")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "cf_token"}
        await he.text_input_callback(update, context)
        self.assertIn("不能为空也不能带空格", reply_text_of(update))

    async def test_cf_key_stores_draft_and_asks_email(self):
        update = self._text_update("fake-key-123")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "cf_key"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["cf_draft"]["api_key"],
                         "fake-key-123")
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "cf_email")
        self.assertIn("账号邮箱", reply_text_of(update))

    async def test_cf_email_invalid_cancels(self):
        update = self._text_update("not-an-email")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "cf_email"}
        context.user_data["cf_draft"] = {"api_key": "fake-key-123"}
        await he.text_input_callback(update, context)
        self.assertIn("邮箱格式不对", reply_text_of(update))
        self.assertNotIn("cf_draft", context.user_data)

    async def test_cf_email_success(self):
        with patch.object(he, "validate_and_save",
                          return_value=(True, 6, None)) as mock_save:
            update = self._text_update("user@example.com")
            context = make_context()
            context.user_data["pending_input"] = {"kind": "cf_email"}
            context.user_data["cf_draft"] = {"api_key": "fake-key-123"}
            await he.text_input_callback(update, context)
        mock_save.assert_called_once_with("", "fake-key-123",
                                          "user@example.com")
        text = reply_text_of(update)
        self.assertIn("已保存并校验通过", text)
        self.assertIn("u***@example.com", text)
        self.assertNotIn("fake-key-123", text)
        self.assertNotIn("cf_draft", context.user_data)

class TestDnsRecordValidation(unittest.TestCase):
    """DNS 记录名/内容校验（纯函数，不碰 API）。"""

    def test_name_root_ok(self):
        self.assertTrue(he._validate_dns_record_name("@"))

    def test_name_hostname_ok(self):
        self.assertTrue(he._validate_dns_record_name("www"))
        self.assertTrue(he._validate_dns_record_name("sub.www"))
        self.assertTrue(he._validate_dns_record_name("a-b_c"))

    def test_name_invalid(self):
        self.assertFalse(he._validate_dns_record_name(""))
        self.assertFalse(he._validate_dns_record_name("-bad"))
        self.assertFalse(he._validate_dns_record_name(".bad"))
        self.assertFalse(he._validate_dns_record_name("bad."))
        self.assertFalse(he._validate_dns_record_name("bad name"))
        self.assertFalse(he._validate_dns_record_name("bad/name"))

    def test_content_a_valid(self):
        self.assertIsNone(he._validate_dns_record_content(
            "A", "203.0.113.10"))

    def test_content_a_invalid(self):
        err = he._validate_dns_record_content("A", "not-an-ip")
        self.assertIn("IPv4", err)

    def test_content_aaaa_valid(self):
        self.assertIsNone(he._validate_dns_record_content(
            "AAAA", "2001:db8::1"))

    def test_content_aaaa_invalid(self):
        err = he._validate_dns_record_content("AAAA", "203.0.113.10")
        self.assertIn("IPv6", err)

    def test_content_cname_valid(self):
        self.assertIsNone(he._validate_dns_record_content(
            "CNAME", "example.com"))

    def test_content_cname_invalid(self):
        err = he._validate_dns_record_content("CNAME", "not a domain")
        self.assertIn("域名", err)

    def test_content_txt_any_text(self):
        self.assertIsNone(he._validate_dns_record_content(
            "TXT", "v=spf1 -all"))

    def test_content_empty(self):
        self.assertIn("不能为空",
                      he._validate_dns_record_content("TXT", ""))

    def test_fmt_add_summary(self):
        draft = {"type": "A", "name": "www",
                 "content": "203.0.113.10"}
        text = he.fmt_dns_add_summary("example.com", draft)
        self.assertIn("example.com", text)
        self.assertIn("类型：A", text)
        self.assertIn("名称：www", text)
        self.assertIn("203.0.113.10", text)

    def test_fmt_add_summary_root(self):
        draft = {"type": "A", "name": "@", "content": "203.0.113.10"}
        self.assertIn("根域名（@）",
                      he.fmt_dns_add_summary("example.com", draft))


class TestDnsAddFlow(DomainTestCase):
    """`domain:add/addtype/add:confirm`：添加记录全流程。"""

    def _text_update(self, text):
        update = make_update()
        update.effective_message.text = text
        update.effective_message.chat_id = TEST_CHAT_ID
        update.effective_message.message_id = 777
        return update

    async def test_records_keyboard_has_add_delete(self):
        """记录列表键盘挂上添加/删除按钮（v1.27.0 的入口）。"""
        update = make_update(callback_data=f"domain:zone:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:add:{FAKE_ZONE_ID}", callbacks)
        self.assertIn(f"domain:delpick:{FAKE_ZONE_ID}", callbacks)
        # 记录存进 user_data，供删除按下标取用。
        self.assertEqual(
            context.user_data["domain_records"][FAKE_ZONE_ID],
            FAKE_RECORDS)

    async def test_add_shows_type_picker(self):
        update = make_update(callback_data=f"domain:add:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        self.assertIn("先选记录类型", reply_text_of(update))
        callbacks = button_callback_data(reply_markup_of(update))
        for rtype in ("A", "AAAA", "CNAME", "TXT", "MX"):
            self.assertIn(f"domain:addtype:{FAKE_ZONE_ID}:{rtype}",
                          callbacks)

    async def test_addtype_asks_name(self):
        update = make_update(
            callback_data=f"domain:addtype:{FAKE_ZONE_ID}:A")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        self.assertIn("新建 A 记录", reply_text_of(update))
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "dns_add_name")
        self.assertEqual(context.user_data["dns_add_draft"],
                         {"zone_id": FAKE_ZONE_ID, "type": "A"})

    async def test_addtype_bad_type_rejected(self):
        update = make_update(
            callback_data=f"domain:addtype:{FAKE_ZONE_ID}:NS")
        context = make_context()
        await he.domain_callback(update, context)
        self.assertIn("不支持的记录类型", reply_text_of(update))

    async def test_name_input_valid_asks_content(self):
        update = self._text_update("www")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "dns_add_name"}
        context.user_data["dns_add_draft"] = {"zone_id": FAKE_ZONE_ID,
                                              "type": "A"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["dns_add_draft"]["name"], "www")
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "dns_add_content")
        self.assertIn("IPv4", reply_text_of(update))

    async def test_name_input_invalid_retries(self):
        update = self._text_update("bad name")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "dns_add_name"}
        context.user_data["dns_add_draft"] = {"zone_id": FAKE_ZONE_ID,
                                              "type": "A"}
        await he.text_input_callback(update, context)
        self.assertIn("格式不对", reply_text_of(update))
        # 格式不对可重发：pending_input 仍在 dns_add_name。
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "dns_add_name")

    async def test_content_input_invalid_retries(self):
        update = self._text_update("not-an-ip")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "dns_add_content"}
        context.user_data["dns_add_draft"] = {"zone_id": FAKE_ZONE_ID,
                                              "type": "A", "name": "www"}
        await he.text_input_callback(update, context)
        self.assertIn("IPv4", reply_text_of(update))
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "dns_add_content")

    async def test_content_input_valid_shows_confirm(self):
        update = self._text_update("203.0.113.10")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "dns_add_content"}
        context.user_data["dns_add_draft"] = {"zone_id": FAKE_ZONE_ID,
                                              "type": "A", "name": "www"}
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.text_input_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认添加", text)
        self.assertIn("203.0.113.10", text)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:add:confirm:{FAKE_ZONE_ID}", callbacks)
        self.assertNotIn("pending_input", context.user_data)

    async def test_confirm_creates_record(self):
        update = make_update(
            callback_data=f"domain:add:confirm:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        context.user_data["dns_add_draft"] = {
            "zone_id": FAKE_ZONE_ID, "type": "A", "name": "www",
            "content": "203.0.113.10"}
        await he.domain_callback(update, context)
        self.cf.create_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, "A", "www", "203.0.113.10")
        self.assertNotIn("dns_add_draft", context.user_data)
        # 成功后刷新记录列表（最后一条消息是记录列表）。
        self.assertIn("共 3 条", reply_text_of(update))

    async def test_confirm_without_draft_expired(self):
        update = make_update(
            callback_data=f"domain:add:confirm:{FAKE_ZONE_ID}")
        context = make_context()
        await he.domain_callback(update, context)
        self.assertIn("已过期", reply_text_of(update))
        self.cf.create_dns_record.assert_not_called()

    async def test_confirm_api_error_reported(self):
        self.cf.create_dns_record.side_effect = _cf_error("api")
        update = make_update(
            callback_data=f"domain:add:confirm:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["dns_add_draft"] = {
            "zone_id": FAKE_ZONE_ID, "type": "TXT", "name": "test",
            "content": "hello"}
        await he.domain_callback(update, context)
        self.assertIn("调用失败", reply_text_of(update))


class TestDnsDeleteFlow(DomainTestCase):
    """`domain:delpick/del/del:yes`：删除记录全流程。"""

    def _stored_context(self):
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        context.user_data["domain_records"] = {
            FAKE_ZONE_ID: list(FAKE_RECORDS)}
        return context

    async def test_delpick_lists_records(self):
        update = make_update(callback_data=f"domain:delpick:{FAKE_ZONE_ID}")
        context = self._stored_context()
        await he.domain_callback(update, context)
        self.assertIn("删除 example.com 的 DNS 记录", reply_text_of(update))
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:del:{FAKE_ZONE_ID}:0", callbacks)
        self.assertIn(f"domain:del:{FAKE_ZONE_ID}:2", callbacks)
        # 有缓存时不再调 API。
        self.cf.list_dns_records.assert_not_called()

    async def test_delpick_refetches_when_no_cache(self):
        update = make_update(callback_data=f"domain:delpick:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        self.cf.list_dns_records.assert_called_once_with(FAKE_ZONE_ID)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:del:{FAKE_ZONE_ID}:0", callbacks)

    async def test_delpick_no_records(self):
        self.cf.list_dns_records.return_value = []
        update = make_update(callback_data=f"domain:delpick:{FAKE_ZONE_ID}")
        context = make_context()
        context.user_data["domain_zones"] = FAKE_ZONES
        await he.domain_callback(update, context)
        self.assertIn("没有 DNS 记录", reply_text_of(update))

    async def test_del_shows_confirm(self):
        update = make_update(callback_data=f"domain:del:{FAKE_ZONE_ID}:0")
        context = self._stored_context()
        await he.domain_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认删除", text)
        self.assertIn("A www.example.com -> 203.0.113.10", text)
        callbacks = button_callback_data(reply_markup_of(update))
        self.assertIn(f"domain:del:yes:{FAKE_ZONE_ID}:0", callbacks)

    async def test_del_bad_index(self):
        update = make_update(callback_data=f"domain:del:{FAKE_ZONE_ID}:9")
        context = self._stored_context()
        await he.domain_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))

    async def test_del_yes_deletes_record(self):
        update = make_update(
            callback_data=f"domain:del:yes:{FAKE_ZONE_ID}:1")
        context = self._stored_context()
        await he.domain_callback(update, context)
        self.cf.delete_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, "recid000000000000000000000002")
        # 成功后刷新记录列表。
        self.assertIn("共 3 条", reply_text_of(update))

    async def test_del_yes_api_error_reported(self):
        self.cf.delete_dns_record.side_effect = _cf_error("not_found")
        update = make_update(
            callback_data=f"domain:del:yes:{FAKE_ZONE_ID}:0")
        context = self._stored_context()
        await he.domain_callback(update, context)
        self.assertIn("不存在", reply_text_of(update))


if __name__ == "__main__":
    unittest.main()
