#!/usr/bin/env python3
"""绑定域名文字输入的分发级测试（v2.02：问题 1 根因回归）。

v2.01 的 bug：三个文字 handler 全在默认 group 0，PTB 每 group 只跑
第一个匹配的 handler，先注册的 text_input_callback 把所有纯文字
消息吞了，bind_domain_text_input 永远跑不到，用户输二级域名后无
任何反馈。本文件用真实 Application + process_update 复现并锁定修
复：文字 handler 分在 group 0/1/2/3，各自只认自己的 user_data 状态。

约定：
  - user_data 按 PTB 规则以 user_id 为键（不是 chat_id）。
  - no_proxy 含带括号的 IPv6 会让 httpx 抛 Invalid port，setUp 里
    先把代理环境变量清干净。
  - 不调 get_me：直接置 app._initialized = True。
"""

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Chat, Message, Update, User
from telegram.ext import MessageHandler

from tgbot import bind_domain as bd
from tgbot import bot as bot_mod
from tgbot import config as bot_config

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
FAKE_ZONE_ID = "zoneid000000000000000000000001"
FAKE_IP = "203.0.113.10"


class FakeCF:
    """只记 create_dns_record 调用，不碰真实 Cloudflare。"""

    def __init__(self):
        self.created = []

    def list_dns_records(self, zone_id, type=None, name=None):
        return []

    def create_dns_record(self, zone_id, type, name, content,
                          proxied=False, ttl=1):
        rec = {"id": "rec1", "type": type, "name": name, "content": content}
        self.created.append(rec)
        return rec


def make_text_update(text, user_id=TEST_USER_ID, chat_id=TEST_CHAT_ID):
    """真实 Update：纯文字消息（set_bot 后 reply 走 bot.send_message）。"""
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=99))
    user = User(id=user_id, first_name="T", is_bot=False)
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=1, date=datetime.now(timezone.utc),
                  chat=chat, from_user=user, text=text)
    msg.set_bot(bot)
    return Update(update_id=1, message=msg), bot


class DispatchTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：清代理环境变量、白名单、建真实 Application。"""

    def setUp(self):
        self._env = {}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
            self._env[key] = os.environ.pop(key, None)
        self._env["NO_PROXY"] = os.environ.get("NO_PROXY")
        self._env["no_proxy"] = os.environ.get("no_proxy")
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
        os.environ["no_proxy"] = "localhost,127.0.0.1"
        self.addCleanup(self._restore_env)
        self._token = patch.object(
            bot_config, "TELEGRAM_BOT_TOKEN",
            "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        self._token.start()
        self.addCleanup(self._token.stop)
        self._allowed = patch.object(
            bot_config, "ALLOWED_USER_IDS", {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self.app = bot_mod.build_application()
        self.app._initialized = True
        self.addCleanup(self._shutdown_app)

    def _restore_env(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def _shutdown_app(self):
        try:
            await self.app.shutdown()
        except Exception:  # noqa: BLE001 - 测试收尾不阻塞
            pass

    def text_handler_groups(self):
        """group -> 文字 handler 回调名。"""
        groups = {}
        for group, handlers in self.app.handlers.items():
            for handler in handlers:
                if isinstance(handler, MessageHandler):
                    filters = getattr(handler, "filters", None)
                    if filters is not None and str(filters).find("TEXT") >= 0:
                        groups.setdefault(group, []).append(
                            handler.callback.__name__)
        return groups


class TestTextHandlerGroups(DispatchTestCase):
    """文字 handler 必须分在不同 group（v2.01 根因）。"""

    def test_text_handlers_in_separate_groups(self):
        groups = self.text_handler_groups()
        flat = {name: group for group, names in groups.items()
                for name in names}
        self.assertEqual(flat.get("text_input_callback"), 0)
        self.assertEqual(flat.get("bind_domain_text_input"), 1)
        self.assertEqual(flat.get("handle_client_text_input"), 2)
        self.assertEqual(flat.get("handle_oci_text_input"), 3)


class TestBindDomainDispatch(DispatchTestCase):
    """绑定域名：输二级域名必须进绑定处理、建记录、有回复。"""

    async def test_subdomain_reaches_bind_domain(self):
        cf = FakeCF()
        self.app.user_data[TEST_USER_ID]["bind_domain_pending"] = {
            "idx": "0", "ip": FAKE_IP, "name": "inst-1",
            "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"}
        update, bot = make_text_update("admin")
        with patch.object(bd, "_get_cf_client", return_value=cf):
            await self.app.process_update(update)
        self.assertEqual(len(cf.created), 1)
        self.assertEqual(cf.created[0]["name"], "admin.example.com")
        self.assertEqual(cf.created[0]["content"], FAKE_IP)
        self.assertTrue(bot.send_message.await_count >= 1)
        sent = bot.send_message.await_args_list[-1].kwargs.get("text", "")
        self.assertIn("已绑定", sent)
        self.assertIn("admin.example.com", sent)

    async def test_text_without_pending_ignored(self):
        cf = FakeCF()
        update, bot = make_text_update("hello")
        with patch.object(bd, "_get_cf_client", return_value=cf):
            await self.app.process_update(update)
        self.assertEqual(cf.created, [])
        self.assertEqual(bot.send_message.await_count, 0)

    async def test_oci_text_reaches_group3(self):
        """oci_awaiting 状态下粘贴的文本能走到 /oci 处理（不再被吞）。"""
        self.app.user_data[TEST_USER_ID]["oci_awaiting"] = True
        update, bot = make_text_update("not a config")
        await self.app.process_update(update)
        self.assertTrue(bot.send_message.await_count >= 1)
        sent = bot.send_message.await_args_list[-1].kwargs.get("text", "")
        self.assertIn("配置格式不对", sent)


if __name__ == "__main__":
    unittest.main()
