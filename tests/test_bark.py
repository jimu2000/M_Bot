#!/usr/bin/env python3
"""Bark 通知模块（tgbot/bark.py）与设置菜单的单元测试。

加密用真实 cryptography/openssl 对照（不发真实请求）；
send_bark 的 HTTP 层全部 mock；设置菜单的配置读写 patch
到内存，不碰真实 bark.json。
"""

import base64
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import bark as bark_mod
from tgbot import config as bot_config
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_KEY = "K" * 32
TEST_URL = "https://api.day.app/AbCdEfGh12345678/"
TEST_IV = "Iv1234567890AbCd"


def make_update(callback_data=None, text=None):
    update = MagicMock()
    update.effective_user.id = TEST_USER_ID
    update.effective_chat.id = TEST_CHAT_ID
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    if text is not None:
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
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


def reply_text_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


class TestEncryptPayload(unittest.TestCase):

    def test_matches_openssl(self):
        """cryptography 加密结果与 openssl 命令行逐字节一致。"""
        payload = json.dumps(
            {"title": "测试", "body": "中文 body", "level": "active",
             "group": "m-bot"}, ensure_ascii=False).encode("utf-8")
        ct, iv = bark_mod.encrypt_payload(payload, TEST_KEY, TEST_IV)
        enc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc",
             "-K", TEST_KEY.encode().hex(),
             "-iv", TEST_IV.encode().hex()],
            input=payload, capture_output=True)
        self.assertEqual(
            ct, base64.b64encode(enc.stdout).decode("ascii"))
        self.assertEqual(iv, TEST_IV)

    def test_random_iv_is_16_ascii(self):
        ct, iv = bark_mod.encrypt_payload(b"hello", TEST_KEY)
        self.assertEqual(len(iv), 16)
        self.assertTrue(all(c.isascii() and c.isalnum() for c in iv))
        self.assertTrue(ct)

    def test_bad_key_length_raises(self):
        with self.assertRaises(ValueError):
            bark_mod.encrypt_payload(b"hello", "short")
        with self.assertRaises(ValueError):
            bark_mod.encrypt_payload(b"hello", "K" * 31)

    def test_bad_iv_length_raises(self):
        with self.assertRaises(ValueError):
            bark_mod.encrypt_payload(b"hello", TEST_KEY, "short")


class TestBarkConfig(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "bark.json")

    def test_save_load_roundtrip(self):
        cfg = {"url": TEST_URL, "key": TEST_KEY, "enabled": True,
               "chat_id": TEST_CHAT_ID}
        bark_mod.save_bark_config(cfg, self.path)
        loaded = bark_mod.load_bark_config(self.path)
        self.assertEqual(loaded, cfg)

    def test_saved_file_is_0600(self):
        bark_mod.save_bark_config(
            {"url": TEST_URL, "key": TEST_KEY, "enabled": False}, self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_file_uses_env_defaults(self):
        with patch.dict(os.environ, {"RBOT_BARK_URL": TEST_URL,
                                     "RBOT_BARK_KEY": TEST_KEY}):
            cfg = bark_mod.load_bark_config(self.path)
        self.assertEqual(cfg["url"], TEST_URL)
        self.assertEqual(cfg["key"], TEST_KEY)
        self.assertFalse(cfg["enabled"])
        self.assertIsNone(cfg["chat_id"])

    def test_corrupt_file_returns_defaults(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = bark_mod.load_bark_config(self.path)
        self.assertEqual(cfg["url"], "")
        self.assertFalse(cfg["enabled"])

    def test_file_values_override_env(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"url": "https://example.com/x/",
                       "key": "E" * 32, "enabled": True}, f)
        with patch.dict(os.environ, {"RBOT_BARK_URL": TEST_URL,
                                     "RBOT_BARK_KEY": TEST_KEY}):
            cfg = bark_mod.load_bark_config(self.path)
        self.assertEqual(cfg["url"], "https://example.com/x/")
        self.assertTrue(cfg["enabled"])

    def test_bark_configured(self):
        self.assertTrue(bark_mod.bark_configured(
            {"url": TEST_URL, "key": TEST_KEY}))
        self.assertFalse(bark_mod.bark_configured(
            {"url": TEST_URL, "key": "short"}))
        self.assertFalse(bark_mod.bark_configured(
            {"url": "", "key": TEST_KEY}))


class TestMaskBarkUrl(unittest.TestCase):

    def test_masks_key_in_path(self):
        masked = bark_mod.mask_bark_url(TEST_URL)
        self.assertIn("api.day.app", masked)
        self.assertNotIn("AbCdEfGh12345678", masked)
        self.assertTrue(masked.endswith("5678"))

    def test_empty_returns_not_set(self):
        self.assertEqual(bark_mod.mask_bark_url(""), "未设置")


class TestSendBark(unittest.TestCase):

    def _ok_response(self):
        resp = MagicMock()
        resp.read.return_value = b'{"code": 200, "message": "success"}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_disabled_returns_error(self):
        ok, err = bark_mod.send_bark(
            "t", "b", config={"url": TEST_URL, "key": TEST_KEY,
                             "enabled": False})
        self.assertFalse(ok)
        self.assertIn("未开启", err)

    def test_unconfigured_returns_error(self):
        ok, err = bark_mod.send_bark(
            "t", "b", config={"url": "", "key": "", "enabled": True})
        self.assertFalse(ok)
        self.assertIn("未配置", err)

    def test_success_posts_ciphertext_and_iv(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data.decode("ascii")
            return self._ok_response()
        with patch.object(bark_mod.urllib.request, "urlopen",
                          side_effect=fake_urlopen):
            ok, err = bark_mod.send_bark(
                "标题", "正文", config={"url": TEST_URL, "key": TEST_KEY,
                                       "enabled": True})
        self.assertTrue(ok, err)
        self.assertEqual(captured["url"], TEST_URL)
        self.assertIn("ciphertext=", captured["data"])
        self.assertIn("iv=", captured["data"])
        # 密文里不能出现明文标题/正文/Key。
        self.assertNotIn("标题", captured["data"])
        self.assertNotIn("正文", captured["data"])
        self.assertNotIn(TEST_KEY, captured["data"])

    def test_non_200_code_returns_error(self):
        resp = MagicMock()
        resp.read.return_value = b'{"code": 400, "message": "bad key"}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch.object(bark_mod.urllib.request, "urlopen",
                          return_value=resp):
            ok, err = bark_mod.send_bark(
                "t", "b", config={"url": TEST_URL, "key": TEST_KEY,
                                 "enabled": True})
        self.assertFalse(ok)
        self.assertIn("400", err)

    def test_test_push_ignores_switch(self):
        with patch.object(bark_mod.urllib.request, "urlopen",
                          return_value=self._ok_response()):
            ok, err = bark_mod.send_bark_test(
                config={"url": TEST_URL, "key": TEST_KEY, "enabled": False})
        self.assertTrue(ok, err)

    def test_test_push_requires_config(self):
        ok, err = bark_mod.send_bark_test(
            config={"url": "", "key": "", "enabled": False})
        self.assertFalse(ok)
        self.assertIn("未配置", err)


class TestFmtBarkSettings(unittest.TestCase):

    def test_unconfigured(self):
        text = he.fmt_bark_settings({"url": "", "key": "", "enabled": False})
        self.assertIn("已关闭", text)
        self.assertIn("未设置", text)
        self.assertIn("先设置", text)

    def test_configured_disabled(self):
        text = he.fmt_bark_settings({"url": TEST_URL, "key": TEST_KEY,
                                     "enabled": False})
        self.assertIn("已关闭", text)
        self.assertIn("api.day.app", text)
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn("AbCdEfGh12345678", text)
        self.assertIn("打开开关", text)

    def test_enabled(self):
        text = he.fmt_bark_settings({"url": TEST_URL, "key": TEST_KEY,
                                     "enabled": True})
        self.assertIn("已开启", text)
        self.assertIn("同时推到 Bark 和 Telegram", text)


class BarkHandlerTestCase(unittest.IsolatedAsyncioTestCase):
    """设置菜单 handler 测试：配置读写 patch 到内存。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self.cfg = {"url": "", "key": "", "enabled": False, "chat_id": None}
        self._load = patch.object(
            he, "load_bark_config",
            side_effect=lambda: dict(self.cfg))
        self._load.start()
        self.addCleanup(self._load.stop)
        self._save = patch.object(
            he, "save_bark_config",
            side_effect=lambda c, path=None: self.cfg.update(c))
        self._save.start()
        self.addCleanup(self._save.stop)
        self._prompts = patch.object(
            he, "_delete_tracked_prompts", new=AsyncMock())
        self._prompts.start()
        self.addCleanup(self._prompts.stop)


class TestBarkCallback(BarkHandlerTestCase):

    async def test_url_prompt_sets_pending_input(self):
        update = make_update("bark:url")
        context = make_context()
        await he.bark_callback(update, context)
        self.assertEqual(context.user_data.get("pending_input"),
                         {"kind": "bark_url"})
        self.assertIn("Bark 推送地址", reply_text_of(update))

    async def test_key_prompt_sets_pending_input(self):
        update = make_update("bark:key")
        context = make_context()
        await he.bark_callback(update, context)
        self.assertEqual(context.user_data.get("pending_input"),
                         {"kind": "bark_key"})
        self.assertIn("32 位", reply_text_of(update))

    async def test_test_push_success(self):
        update = make_update("bark:test")
        context = make_context()
        with patch.object(he, "send_bark_test", return_value=(True, "")):
            await he.bark_callback(update, context)
        self.assertIn("已发出", reply_text_of(update))

    async def test_test_push_failure(self):
        update = make_update("bark:test")
        context = make_context()
        with patch.object(he, "send_bark_test",
                          return_value=(False, "Bark 地址或加密 Key 未配置")):
            await he.bark_callback(update, context)
        self.assertIn("失败", reply_text_of(update))

    async def test_toggle_without_config_refused(self):
        update = make_update("bark:toggle")
        context = make_context()
        await he.bark_callback(update, context)
        self.assertIn("还没配齐", reply_text_of(update))
        self.assertFalse(self.cfg["enabled"])

    async def test_toggle_with_config_flips(self):
        self.cfg.update({"url": TEST_URL, "key": TEST_KEY})
        update = make_update("bark:toggle")
        context = make_context()
        await he.bark_callback(update, context)
        self.assertTrue(self.cfg["enabled"])
        self.assertIn("已开启", reply_text_of(update))
        update2 = make_update("bark:toggle")
        await he.bark_callback(update2, context)
        self.assertFalse(self.cfg["enabled"])

    async def test_cancel_input_clears_pending(self):
        update = make_update("bark:cancel_input")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "bark_url"}
        await he.bark_callback(update, context)
        self.assertNotIn("pending_input", context.user_data)
        self.assertIn("Bark 通知设置", reply_text_of(update))


class TestBarkInputHandlers(BarkHandlerTestCase):

    async def test_url_input_valid_saves(self):
        update = make_update(text=TEST_URL)
        context = make_context()
        await he._handle_bark_url_input(update, context, TEST_URL)
        self.assertEqual(self.cfg["url"], TEST_URL)
        self.assertIn("已保存", reply_text_of(update))
        self.assertNotIn("AbCdEfGh12345678", reply_text_of(update))

    async def test_url_input_invalid_rejected(self):
        update = make_update(text="not-a-url")
        context = make_context()
        await he._handle_bark_url_input(update, context, "not-a-url")
        self.assertEqual(self.cfg["url"], "")
        self.assertIn("格式不正确", reply_text_of(update))

    async def test_key_input_valid_saves_without_echo(self):
        update = make_update(text=TEST_KEY)
        context = make_context()
        await he._handle_bark_key_input(update, context, TEST_KEY)
        self.assertEqual(self.cfg["key"], TEST_KEY)
        self.assertIn("已保存", reply_text_of(update))
        self.assertNotIn(TEST_KEY, reply_text_of(update))

    async def test_key_input_wrong_length_rejected(self):
        update = make_update(text="short")
        context = make_context()
        await he._handle_bark_key_input(update, context, "short")
        self.assertEqual(self.cfg["key"], "")
        self.assertIn("32 位", reply_text_of(update))

    async def test_handle_bark_shows_settings(self):
        self.cfg.update({"url": TEST_URL, "key": TEST_KEY, "enabled": True})
        update = make_update("o:bark")
        context = make_context()
        await he.handle_bark(update, context)
        text = reply_text_of(update)
        self.assertIn("Bark 通知设置", text)
        self.assertIn("已开启", text)

    async def test_dispatch_bark_returns_true(self):
        update = make_update("o:bark")
        context = make_context()
        self.assertTrue(await he.dispatch_oracle_action("bark", update,
                                                         context))

    async def test_menu_states_bark(self):
        self.cfg.update({"url": TEST_URL, "key": TEST_KEY, "enabled": True})
        context = make_context()
        with patch.object(he, "load_traffic_guard", return_value={}), \
                patch.object(he, "load_keepalive", return_value={}):
            states = he.get_menu_states(context)
        self.assertTrue(states["bark"])
        self.cfg["enabled"] = False
        with patch.object(he, "load_traffic_guard", return_value={}), \
                patch.object(he, "load_keepalive", return_value={}):
            states = he.get_menu_states(context)
        self.assertFalse(states["bark"])


if __name__ == "__main__":
    unittest.main()
