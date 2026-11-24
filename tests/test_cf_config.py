#!/usr/bin/env python3
"""tgbot.cf_config（Cloudflare 凭据管理，v1.26.0）的单元测试。

全部用 mock 与临时目录，不碰真实 Cloudflare API，不读真实配置文件。

约定：
  - 文件操作一律传 path= 指向临时目录，绝不碰真实 DATA_DIR。
  - 凭据值只用假值（fake-token-xxx / fake-key-xxx / user@example.com），
    绝不出现真实 Token / Key / 邮箱。
  - CloudflareClient 用 patch.object(tgbot.cf_config, "CloudflareClient")
    替换，list_zones 返回假 Zone 列表。

运行：python3 -m unittest tests.test_cf_config -v
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import cf_config
from tgbot.cf_config import CloudflareError

FAKE_TOKEN = "fake-token-0123456789abcdef"
FAKE_KEY = "fake-key-0123456789abcdef"
FAKE_EMAIL = "user@example.com"
FAKE_ZONES = [{"id": "zone1", "name": "example.com", "status": "active"},
              {"id": "zone2", "name": "example.org", "status": "active"}]

SCRIPT_WITH_CREDS = """#!/bin/bash
# 证书管理脚本（假内容）
export CF_Key="fake-key-0123456789abcdef"
export CF_Email="user@example.com"
acme.sh --issue -d example.com --dns dns_cf
"""

SCRIPT_BARE_SINGLE_QUOTES = """CF_Key='fake-key-aaa'
CF_Email='admin@example.org'
"""

SCRIPT_NO_CREDS = """#!/bin/bash
# 没有 CF 凭据的脚本
echo hello
"""


class CfConfigFileTestCase(unittest.TestCase):
    """凭据文件存取（临时目录）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cloudflare.json")

    def test_load_missing_returns_empty(self):
        cfg = cf_config.load_cf_config(path=self.path)
        self.assertEqual(cfg, {"api_token": "", "api_key": "", "email": ""})

    def test_load_corrupt_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        cfg = cf_config.load_cf_config(path=self.path)
        self.assertEqual(cfg["api_token"], "")
        self.assertEqual(cfg["api_key"], "")

    def test_save_and_load_roundtrip(self):
        saved = cf_config.save_cf_config(
            {"api_token": FAKE_TOKEN, "api_key": "", "email": ""},
            path=self.path)
        self.assertEqual(saved["api_token"], FAKE_TOKEN)
        loaded = cf_config.load_cf_config(path=self.path)
        self.assertEqual(loaded["api_token"], FAKE_TOKEN)

    def test_save_only_keeps_three_fields(self):
        cf_config.save_cf_config(
            {"api_token": FAKE_TOKEN, "extra": "should-drop"},
            path=self.path)
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data), {"api_token", "api_key", "email"})

    def test_save_sets_0600(self):
        cf_config.save_cf_config({"api_token": FAKE_TOKEN}, path=self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_clear_removes_file(self):
        cf_config.save_cf_config({"api_token": FAKE_TOKEN}, path=self.path)
        self.assertTrue(os.path.exists(self.path))
        cf_config.clear_cf_config(path=self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_clear_missing_is_noop(self):
        cf_config.clear_cf_config(path=self.path)  # 不存在也不报错
        self.assertFalse(os.path.exists(self.path))


class TestConfiguredAndMode(unittest.TestCase):
    """cf_configured / cf_auth_mode。"""

    def test_token_configured(self):
        cfg = {"api_token": FAKE_TOKEN, "api_key": "", "email": ""}
        self.assertTrue(cf_config.cf_configured(cfg))
        self.assertEqual(cf_config.cf_auth_mode(cfg), "token")

    def test_key_pair_configured(self):
        cfg = {"api_token": "", "api_key": FAKE_KEY, "email": FAKE_EMAIL}
        self.assertTrue(cf_config.cf_configured(cfg))
        self.assertEqual(cf_config.cf_auth_mode(cfg), "api_key")

    def test_key_without_email_not_configured(self):
        cfg = {"api_token": "", "api_key": FAKE_KEY, "email": ""}
        self.assertFalse(cf_config.cf_configured(cfg))
        self.assertIsNone(cf_config.cf_auth_mode(cfg))

    def test_empty_not_configured(self):
        self.assertFalse(cf_config.cf_configured(
            {"api_token": "", "api_key": "", "email": ""}))


class TestResolvePrecedence(unittest.TestCase):
    """resolve：文件优先于环境变量；都没有时 source 为 None。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cloudflare.json")

    def test_file_beats_env(self):
        cf_config.save_cf_config({"api_token": "file-token"}, path=self.path)
        env = {"CF_API_TOKEN": "env-token"}
        with patch.dict(os.environ, env, clear=True):
            token, _key, _email, source = cf_config.resolve_cf_credentials(
                path=self.path)
        self.assertEqual(token, "file-token")
        self.assertEqual(source, "file")

    def test_env_token_used_when_no_file(self):
        env = {"CF_API_TOKEN": "env-token"}
        with patch.dict(os.environ, env, clear=True):
            token, _key, _email, source = cf_config.resolve_cf_credentials(
                path=self.path)
        self.assertEqual(token, "env-token")
        self.assertEqual(source, "env")

    def test_env_key_pair_used(self):
        env = {"CF_API_KEY": FAKE_KEY, "CF_EMAIL": FAKE_EMAIL}
        with patch.dict(os.environ, env, clear=True):
            _t, key, email, source = cf_config.resolve_cf_credentials(
                path=self.path)
        self.assertEqual(key, FAKE_KEY)
        self.assertEqual(email, FAKE_EMAIL)
        self.assertEqual(source, "env")

    def test_env_key_without_email_is_none(self):
        env = {"CF_API_KEY": FAKE_KEY}
        with patch.dict(os.environ, env, clear=True):
            _t, _k, _e, source = cf_config.resolve_cf_credentials(
                path=self.path)
        self.assertIsNone(source)

    def test_nothing_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            token, key, email, source = cf_config.resolve_cf_credentials(
                path=self.path)
        self.assertEqual((token, key, email, source), ("", "", "", None))


class TestMaskEmail(unittest.TestCase):
    """mask_cf_email：只留首字符与域名。"""

    def test_masks_local_part(self):
        self.assertEqual(cf_config.mask_cf_email("user@example.com"),
                         "u***@example.com")

    def test_invalid_returns_stars(self):
        self.assertEqual(cf_config.mask_cf_email(""), "***")
        self.assertEqual(cf_config.mask_cf_email("no-at-sign"), "***")


class TestExtractFromScript(unittest.TestCase):
    """extract_cf_from_script：1.sh 文本解析。"""

    def test_export_double_quotes(self):
        key, email = cf_config.extract_cf_from_script(SCRIPT_WITH_CREDS)
        self.assertEqual(key, FAKE_KEY)
        self.assertEqual(email, FAKE_EMAIL)

    def test_bare_single_quotes(self):
        key, email = cf_config.extract_cf_from_script(
            SCRIPT_BARE_SINGLE_QUOTES)
        self.assertEqual(key, "fake-key-aaa")
        self.assertEqual(email, "admin@example.org")

    def test_no_creds_returns_none_none(self):
        key, email = cf_config.extract_cf_from_script(SCRIPT_NO_CREDS)
        self.assertIsNone(key)
        self.assertIsNone(email)

    def test_partial_returns_only_present(self):
        key, email = cf_config.extract_cf_from_script(
            'export CF_Key="only-key-here"\n')
        self.assertEqual(key, "only-key-here")
        self.assertIsNone(email)

    def test_empty_text(self):
        self.assertEqual(cf_config.extract_cf_from_script(""),
                         (None, None))
        self.assertEqual(cf_config.extract_cf_from_script(None),
                         (None, None))


class TestExtractFromHost(unittest.TestCase):
    """extract_cf_from_host：假 runner，不碰真实 SSH。"""

    def test_success(self):
        runner = MagicMock(return_value=(0, SCRIPT_WITH_CREDS, ""))
        key, email, error = cf_config.extract_cf_from_host(
            {"host": "203.0.113.10"}, run_remote_command=runner)
        self.assertEqual(key, FAKE_KEY)
        self.assertEqual(email, FAKE_EMAIL)
        self.assertIsNone(error)
        # 命令里同时查两个 1.sh 路径。
        command = runner.call_args[0][1]
        self.assertIn("/usr/local/etc/xray/1.sh", command)
        self.assertIn("/root/1.sh", command)

    def test_no_creds_in_output(self):
        runner = MagicMock(return_value=(1, "", ""))
        key, email, error = cf_config.extract_cf_from_host(
            {"host": "203.0.113.10"}, run_remote_command=runner)
        self.assertIsNone(key)
        self.assertIsNone(email)
        self.assertIn("没有找到", error)

    def test_partial_creds_reports_missing(self):
        runner = MagicMock(
            return_value=(0, 'export CF_Key="only-key-here"\n', ""))
        _key, _email, error = cf_config.extract_cf_from_host(
            {"host": "203.0.113.10"}, run_remote_command=runner)
        self.assertIn("CF_Email", error)

    def test_ssh_failure(self):
        runner = MagicMock(side_effect=RuntimeError("connection refused"))
        _key, _email, error = cf_config.extract_cf_from_host(
            {"host": "203.0.113.10"}, run_remote_command=runner)
        self.assertIn("SSH 连接失败", error)


class TestValidate(unittest.TestCase):
    """validate_cf_credentials / validate_and_save（mock 客户端）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cloudflare.json")
        self.client = MagicMock()
        self.client.list_zones.return_value = FAKE_ZONES
        self._patch = patch.object(cf_config, "CloudflareClient",
                                   return_value=self.client)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_validate_token_success(self):
        ok, total, err = cf_config.validate_cf_credentials(FAKE_TOKEN, "", "")
        self.assertTrue(ok)
        self.assertEqual(total, 2)
        self.assertIsNone(err)
        cf_config.CloudflareClient.assert_called_once_with(
            api_token=FAKE_TOKEN, api_key=None, email=None)

    def test_validate_key_pair_success(self):
        ok, total, _err = cf_config.validate_cf_credentials(
            "", FAKE_KEY, FAKE_EMAIL)
        self.assertTrue(ok)
        self.assertEqual(total, 2)

    def test_validate_auth_error(self):
        err = CloudflareError("bad auth")
        err.kind = "auth"
        self.client.list_zones.side_effect = err
        ok, total, kind = cf_config.validate_cf_credentials(FAKE_TOKEN, "", "")
        self.assertFalse(ok)
        self.assertEqual(total, 0)
        self.assertEqual(kind, "auth")

    def test_validate_no_credentials(self):
        ok, _total, kind = cf_config.validate_cf_credentials("", "", "")
        self.assertFalse(ok)
        self.assertEqual(kind, "config")

    def test_validate_and_save_success_writes_file(self):
        ok, total, _err = cf_config.validate_and_save(
            FAKE_TOKEN, "", "", path=self.path)
        self.assertTrue(ok)
        self.assertEqual(total, 2)
        loaded = cf_config.load_cf_config(path=self.path)
        self.assertEqual(loaded["api_token"], FAKE_TOKEN)

    def test_validate_and_save_failure_writes_nothing(self):
        err = CloudflareError("bad auth")
        err.kind = "auth"
        self.client.list_zones.side_effect = err
        ok, _total, kind = cf_config.validate_and_save(
            FAKE_TOKEN, "", "", path=self.path)
        self.assertFalse(ok)
        self.assertEqual(kind, "auth")
        self.assertFalse(os.path.exists(self.path))


class TestGetCfClient(unittest.TestCase):
    """get_cf_client：文件凭据显式传参；都没配时抛 config 错误。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "cloudflare.json")
        self.client = MagicMock()
        self._patch = patch.object(cf_config, "CloudflareClient",
                                   return_value=self.client)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_file_credentials_passed_explicitly(self):
        cf_config.save_cf_config(
            {"api_token": "", "api_key": FAKE_KEY, "email": FAKE_EMAIL},
            path=self.path)
        with patch.dict(os.environ, {}, clear=True):
            client = cf_config.get_cf_client(path=self.path)
        self.assertIs(client, self.client)
        cf_config.CloudflareClient.assert_called_once_with(
            api_token=None, api_key=FAKE_KEY, email=FAKE_EMAIL)

    def test_no_credentials_raises_config(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CloudflareError) as ctx:
                cf_config.get_cf_client(path=self.path)
        self.assertEqual(ctx.exception.kind, "config")

    def test_client_class_missing_raises_config(self):
        with patch.object(cf_config, "CloudflareClient", None):
            with self.assertRaises(CloudflareError) as ctx:
                cf_config.get_cf_client(path=self.path)
        self.assertEqual(ctx.exception.kind, "config")


if __name__ == "__main__":
    unittest.main()
