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
    """mock Context：user_data / bot_data 用真实 dict。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
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
        """clouds.cloudflare 未就绪时（get_client 为 None）报配置错误。"""
        with patch.object(he, "get_client", None):
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


if __name__ == "__main__":
    unittest.main()
