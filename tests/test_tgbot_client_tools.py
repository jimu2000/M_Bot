#!/usr/bin/env python3
"""handlers_client.py 的单元测试（阶段 5 工作包 B）。

覆盖：/command（黑名单/分段/Paramiko 执行）、/pubkey（格式校验/二次确认）、
/oproxy（协议校验/清除确认）、/uploadapi（config 解析/重名拒绝/写入）、
菜单 23/24（内存占用命令构造/PID 记录/杀进程/清缓存）。

全部用 mock，不碰真实服务器与真实 OCI。测试里不出现真实 OCID / IP：
IP 用 203.0.113.0/24 文档网段，OCID 用 ocid1...fake 占位符，
SSH 公钥用现场构造的假 key blob。

运行：cd ~/workspace/rbot-clone && python3 -m unittest tests.test_tgbot_client_tools -v
"""

import base64
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko  # noqa: E402

from tgbot import config as bot_config  # noqa: E402
import tgbot.handlers_client as hc  # noqa: E402
from tgbot.handlers_client import (  # noqa: E402
    CommandTimeoutError,
    RemoteCommandError,
)

# Placeholders (no real OCIDs / IPs appear in tests; 203.0.113.0/24 is a documentation range). 占位符（测试里不出现真实 OCID / IP；203.0.113.0/24 是文档专用网段）。
ALLOWED_USER = 123
DENIED_USER = 999
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..fake"
FAKE_IP = "203.0.113.10"
FAKE_INSTANCE_ID = "ocid1.instance.oc1..fake1"
FAKE_INSTANCES = [
    {"id": FAKE_INSTANCE_ID, "display-name": "web-server",
     "shape": "VM.Standard.A1.Flex", "lifecycle-state": "RUNNING",
     "time-created": "2026-11-20T00:00:00Z"},
    {"id": "ocid1.instance.oc1..fake2", "display-name": "e2-test",
     "shape": "VM.Standard.E2.1.Micro", "lifecycle-state": "STOPPED",
     "time-created": "2026-11-21T00:00:00Z"},
]
FAKE_HOSTS = [
    {"id": "hostkey1", "name": "sg-a1", "host": FAKE_IP, "port": 14422,
     "username": "root", "auth_type": "key",
     "key_path": "/home/user/.ssh/fake-key"},
    {"id": "hostpwd1", "name": "pwd-host", "host": "203.0.113.20",
     "port": 22, "username": "root", "auth_type": "password"},
]
FAKE_CONFIG_TEXT = """[NEWACC]
user = ocid1.user.oc1..fake
fingerprint = aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy = ocid1.tenancy.oc1..fake
region = ap-singapore-1
key_file = /home/user/.oci/fake-key.pem
"""


def fake_pubkey(key_type="ssh-ed25519", comment="user@host"):
    """构造格式合法的假 SSH 公钥（blob 内嵌类型与声明一致）。"""
    raw = key_type.encode()
    blob = struct.pack(">I", len(raw)) + raw + b"\x01" * 32
    return f"{key_type} {base64.b64encode(blob).decode()} {comment}"


def make_message_update(user_id=ALLOWED_USER, text=None):
    """命令/文本消息的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.text = text
    update.effective_message = message
    update.callback_query = None
    return update


def make_callback_update(data, user_id=ALLOWED_USER):
    """回调按钮的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    message = MagicMock()
    message.reply_text = AsyncMock()
    update.effective_message = message
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = message
    update.callback_query = query
    return update


def make_context():
    """mock Context：user_data / bot_data 是真实 dict。"""
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()
    context.bot.delete_message = AsyncMock()
    return context


def all_reply_texts(update):
    """取 update 上 reply_text 全部调用的文本列表。"""
    mock = update.effective_message.reply_text
    return [args[0] for args, _kwargs in mock.await_args_list if args]


def last_reply_text(update):
    """取 reply_text 最后一次调用的文本（没调过返回 None）。"""
    texts = all_reply_texts(update)
    return texts[-1] if texts else None


def last_sent_text(update, context):
    """取最后一条发出的文本：send_message 有调用时取它的最后一条，
    否则取 reply_text 的最后一条（v1.34.0：同一回调里第二次 _reply
    改走 send_message，被点的消息已删，回复它会报 Bad Request）。"""
    send_mock = context.bot.send_message
    if send_mock.await_args_list:
        _args, kwargs = send_mock.await_args_list[-1]
        return kwargs.get("text")
    return last_reply_text(update)


def reply_markup_of(update, call_index=-1):
    """取 reply_text 某次调用的 reply_markup（没调过返回 None）。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[call_index]
    return kwargs.get("reply_markup")


def all_callback_data(keyboard):
    """取 InlineKeyboardMarkup 里全部 callback_data。"""
    if keyboard is None:
        return []
    return [btn.callback_data
            for row in keyboard.inline_keyboard for btn in row]


# ================= /command：黑名单与分段 =================

class TestDangerousCommands(unittest.TestCase):

    def test_blocked_commands(self):
        """黑名单里的危险命令全部被拦截。"""
        blocked = [
            "rm -rf /",
            "rm -rf /*",
            "rm -rfv /",
            "rm -fr /",
            "sudo rm -rf /",
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            "dd of=/dev/sda if=/dev/zero",
            ":(){:|:&};:",
            "shutdown -h now",
            "reboot",
            "init 0",
            "halt",
            "poweroff",
            "echo hi > /dev/sda",
            "cat x >> /dev/sdb1",
            "RM -RF /",
        ]
        for cmd in blocked:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(hc.find_dangerous_pattern(cmd),
                                     f"{cmd!r} 应被拦截")

    def test_allowed_commands(self):
        """正常命令不被误杀。"""
        allowed = [
            "ls -la",
            "df -h; free -h",
            "rm -rf /tmp/foo",
            "rm file.txt",
            "echo rebooting the app",
            "shutdownx",
            "uptime",
            "cat /proc/meminfo",
        ]
        for cmd in allowed:
            with self.subTest(cmd=cmd):
                self.assertIsNone(hc.find_dangerous_pattern(cmd),
                                  f"{cmd!r} 不应被拦截")

    def test_blocked_returns_name(self):
        """命中时返回可读的提示名。"""
        name = hc.find_dangerous_pattern("rm -rf /")
        self.assertIn("rm -rf", name)


class TestChunkOutput(unittest.TestCase):

    def test_short_text_single_chunk(self):
        self.assertEqual(hc.chunk_output("hello"), ["hello"])

    def test_empty_text(self):
        self.assertEqual(hc.chunk_output(""), [""])

    def test_long_text_split(self):
        chunks = hc.chunk_output("x" * 9000)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [4000, 4000, 1000])
        self.assertEqual("".join(chunks), "x" * 9000)

    def test_over_total_truncated(self):
        chunks = hc.chunk_output("y" * 25000)
        joined = "".join(chunks)
        self.assertIn("已截断", joined)
        self.assertLess(len(joined), 25000)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))


# ================= /pubkey：校验与存储 =================

class TestValidateSshPublicKey(unittest.TestCase):

    def test_valid_keys(self):
        for key_type in ("ssh-ed25519", "ssh-rsa",
                         "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                         "ecdsa-sha2-nistp521"):
            with self.subTest(key_type=key_type):
                ok, error = hc.validate_ssh_public_key(
                    fake_pubkey(key_type))
                self.assertTrue(ok, error)

    def test_valid_without_comment(self):
        key = fake_pubkey().rsplit(" ", 1)[0]
        ok, error = hc.validate_ssh_public_key(key)
        self.assertTrue(ok, error)

    def test_rejects_private_key(self):
        ok, error = hc.validate_ssh_public_key(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nxxxx\n"
            "-----END OPENSSH PRIVATE KEY-----")
        self.assertFalse(ok)
        self.assertIn("私钥", error)

    def test_rejects_empty(self):
        ok, _error = hc.validate_ssh_public_key("")
        self.assertFalse(ok)

    def test_rejects_wrong_type(self):
        ok, error = hc.validate_ssh_public_key(
            "ssh-foo " + base64.b64encode(b"xxxx").decode())
        self.assertFalse(ok)
        self.assertIn("不支持", error)

    def test_rejects_bad_base64(self):
        ok, _error = hc.validate_ssh_public_key(
            "ssh-ed25519 !!!not-base64!!!")
        self.assertFalse(ok)

    def test_rejects_blob_type_mismatch(self):
        # Declares ed25519, but the blob embeds ssh-rsa. 声明 ed25519，blob 里嵌的却是 ssh-rsa。
        raw = b"ssh-rsa"
        blob = struct.pack(">I", len(raw)) + raw + b"\x01" * 32
        key = f"ssh-ed25519 {base64.b64encode(blob).decode()}"
        ok, error = hc.validate_ssh_public_key(key)
        self.assertFalse(ok)
        self.assertIn("不符", error)

    def test_rejects_too_short(self):
        ok, _error = hc.validate_ssh_public_key(
            "ssh-ed25519 " + base64.b64encode(b"ab").decode())
        self.assertFalse(ok)

    def test_rejects_missing_blob(self):
        ok, _error = hc.validate_ssh_public_key("ssh-ed25519")
        self.assertFalse(ok)


class TestSshKeyStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "ssh_keys.json")

    def test_load_missing_returns_empty(self):
        self.assertEqual(hc.load_ssh_keys(self.path), [])

    def test_load_corrupt_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(hc.load_ssh_keys(self.path), [])

    def test_add_and_load_roundtrip(self):
        key = fake_pubkey()
        keys, added = hc.add_ssh_key(key, self.path)
        self.assertTrue(added)
        self.assertEqual(len(keys), 1)
        self.assertEqual(hc.load_ssh_keys(self.path)[0]["key"], key)
        self.assertIn("added_at", hc.load_ssh_keys(self.path)[0])

    def test_add_duplicate_rejected(self):
        key = fake_pubkey()
        hc.add_ssh_key(key, self.path)
        _keys, added = hc.add_ssh_key(key, self.path)
        self.assertFalse(added)
        self.assertEqual(len(hc.load_ssh_keys(self.path)), 1)

    def test_remove_by_index(self):
        hc.add_ssh_key(fake_pubkey("ssh-ed25519", "one"), self.path)
        hc.add_ssh_key(fake_pubkey("ssh-rsa", "two"), self.path)
        keys, removed = hc.remove_ssh_key(0, self.path)
        self.assertEqual(len(keys), 1)
        self.assertIn("one", removed["key"])
        self.assertEqual(len(hc.load_ssh_keys(self.path)), 1)

    def test_remove_invalid_index(self):
        hc.add_ssh_key(fake_pubkey(), self.path)
        _keys, removed = hc.remove_ssh_key(5, self.path)
        self.assertIsNone(removed)
        _keys, removed = hc.remove_ssh_key("abc", self.path)
        self.assertIsNone(removed)

    def test_fmt_key_line_hides_full_key(self):
        key = fake_pubkey(comment="my-laptop")
        line = hc.fmt_ssh_key_line({"key": key}, 0)
        self.assertIn("ssh-ed25519", line)
        self.assertIn("my-laptop", line)
        # The full base64 never appears in list rows (only the last 8 chars are kept). 完整 base64 不出现在列表行里（只留尾 8 位）。
        self.assertNotIn(key.split()[1], line)


# ================= /oproxy：校验与存储 =================

class TestProxy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "proxy.json")

    def test_validate_ok_schemes(self):
        for url in ("http://127.0.0.1:7890", "https://proxy.example.com:8080",
                    "socks5://127.0.0.1:1080"):
            with self.subTest(url=url):
                ok, error = hc.validate_proxy_url(url)
                self.assertTrue(ok, error)

    def test_validate_rejects(self):
        for url in ("ftp://127.0.0.1:21", "127.0.0.1:7890", "", "   "):
            with self.subTest(url=url):
                ok, _error = hc.validate_proxy_url(url)
                self.assertFalse(ok)

    def test_save_load_roundtrip(self):
        self.assertIsNone(hc.load_proxy(self.path))
        hc.save_proxy("http://127.0.0.1:7890", self.path)
        self.assertEqual(hc.load_proxy(self.path), "http://127.0.0.1:7890")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f),
                             {"proxy_url": "http://127.0.0.1:7890"})

    def test_clear_writes_empty_object(self):
        hc.save_proxy("http://127.0.0.1:7890", self.path)
        hc.save_proxy(None, self.path)
        self.assertIsNone(hc.load_proxy(self.path))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})

    def test_load_corrupt_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("garbage")
        self.assertIsNone(hc.load_proxy(self.path))


# ================= /uploadapi：解析与写入 =================

class TestParseOciConfig(unittest.TestCase):

    def test_valid_config(self):
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["profile"], "NEWACC")
        self.assertEqual(parsed["missing"], [])
        self.assertEqual(parsed["fields"]["region"], "ap-singapore-1")
        self.assertEqual(parsed["fields"]["key_file"],
                         "/home/user/.oci/fake-key.pem")

    def test_missing_keys_listed(self):
        text = ("[NEWACC]\nuser = ocid1.user.oc1..fake\n"
                "fingerprint = aa:bb\ntenancy = ocid1.tenancy.oc1..fake\n")
        parsed = hc.parse_oci_config_text(text)
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["missing"], ["region", "key_file"])

    def test_no_section(self):
        parsed = hc.parse_oci_config_text("user = ocid1.user.oc1..fake\n")
        self.assertIsNotNone(parsed["error"])
        self.assertIn("PROFILE", parsed["error"])

    def test_empty_text(self):
        parsed = hc.parse_oci_config_text("   \n")
        self.assertIsNotNone(parsed["error"])

    def test_bad_profile_name(self):
        parsed = hc.parse_oci_config_text(
            "[my profile]\nuser = ocid1.user.oc1..fake\n")
        self.assertIsNotNone(parsed["error"])
        self.assertIn("不合法", parsed["error"])

    def test_extra_profiles_noted(self):
        """多个 section 时全部导入（v1.28.0 起不再只取第一个）。"""
        parsed = hc.parse_oci_config_text(
            FAKE_CONFIG_TEXT + "\n[SECOND]\nuser = ocid1.user.oc1..fake\n"
            "fingerprint = aa:bb\ntenancy = ocid1.tenancy.oc1..fake\n"
            "region = us-sanjose-1\nkey_file = /root/2.pem\n")
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["profile"], "NEWACC")
        self.assertEqual([p["profile"] for p in parsed["profiles"]],
                         ["NEWACC", "SECOND"])
        self.assertEqual(parsed["extra_profiles"], [])


class TestCloudflareSection(unittest.TestCase):
    """[cloudflare] 段：解析时跳过 profile、提凭据；保存不覆盖（v1.30.0）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "oci_config")

    def test_parse_skips_cloudflare_section(self):
        parsed = hc.parse_oci_config_text(
            FAKE_CONFIG_TEXT + "\n[cloudflare]\ncf_token = fake-token\n")
        self.assertIsNone(parsed["error"])
        self.assertEqual([p["profile"] for p in parsed["profiles"]],
                         ["NEWACC"])
        self.assertEqual(parsed["cloudflare"]["api_token"], "fake-token")

    def test_parse_cloudflare_key_pair(self):
        parsed = hc.parse_oci_config_text(
            FAKE_CONFIG_TEXT + "\n[cloudflare]\n"
            "cf_account_key = fake-key\ncf_email = user@example.com\n")
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["cloudflare"]["api_key"], "fake-key")
        self.assertEqual(parsed["cloudflare"]["email"], "user@example.com")

    def test_parse_cloudflare_incomplete_ignored(self):
        # Key without an email: the credential is unusable, treated as absent. 只有 Key 没邮箱：凭据不可用，当没有。
        parsed = hc.parse_oci_config_text(
            FAKE_CONFIG_TEXT + "\n[cloudflare]\ncf_key = fake-key\n")
        self.assertIsNone(parsed["error"])
        self.assertIsNone(parsed["cloudflare"])

    def test_parse_only_cloudflare_errors(self):
        parsed = hc.parse_oci_config_text(
            "[cloudflare]\ncf_token = fake-token\n")
        self.assertIsNotNone(parsed["error"])
        self.assertIn("PROFILE", parsed["error"])

    def test_commit_saves_section(self):
        status = hc.commit_cf_section(
            {"api_token": "fake-token", "api_key": "", "email": ""},
            self.path)
        self.assertEqual(status, "saved")
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("[cloudflare]", text)
        self.assertIn("cf_token = fake-token", text)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_commit_existing_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[cloudflare]\ncf_token = old-token\n")
        status = hc.commit_cf_section(
            {"api_token": "new-token", "api_key": "", "email": ""},
            self.path)
        self.assertEqual(status, "exists")
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("old-token", text)
        self.assertNotIn("new-token", text)

    def test_commit_case_insensitive_exists(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[Cloudflare]\ncf_token = old-token\n")
        status = hc.commit_cf_section(
            {"api_token": "new-token", "api_key": "", "email": ""},
            self.path)
        self.assertEqual(status, "exists")

    def test_commit_empty_writes_nothing(self):
        self.assertEqual(hc.commit_cf_section(None, self.path), "empty")
        self.assertEqual(hc.commit_cf_section({}, self.path), "empty")
        self.assertFalse(os.path.exists(self.path))

    def test_confirm_text_mentions_cloudflare(self):
        profiles = [{"profile": "NEWACC",
                     "fields": {"region": "ap-singapore-1",
                                "user": "ocid1.user.oc1..fake",
                                "key_file": "/root/NEWACC.pem"},
                     "original_key_file": "/root/NEWACC.pem"}]
        text = hc.build_oci_profiles_confirm_text(
            profiles, None, {"api_token": "fake-token",
                             "api_key": "", "email": ""}, False)
        self.assertIn("[cloudflare]", text)
        self.assertNotIn("fake-token", text)

    def test_confirm_text_cf_only(self):
        text = hc.build_oci_profiles_confirm_text(
            [], None, {"api_token": "fake-token",
                       "api_key": "", "email": ""}, False)
        self.assertIn("Cloudflare", text)
        self.assertNotIn("个账号", text)
        self.assertNotIn("fake-token", text)

    def test_result_text_cf_saved(self):
        text = hc.build_oci_profiles_result_text(
            [], {}, "/root/oci_config", "saved")
        self.assertIn("已保存到 [cloudflare] 段", text)

    def test_result_text_cf_exists(self):
        text = hc.build_oci_profiles_result_text(
            [], {}, "/root/oci_config", "exists")
        self.assertIn("已存在，未覆盖", text)


class TestAppendOciProfile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "config")

    def test_append_to_new_file(self):
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"], self.path)
        with open(self.path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("[NEWACC]", content)
        self.assertIn("region = ap-singapore-1", content)
        self.assertTrue(hc.profile_exists_in_config("NEWACC", self.path))

    def test_append_preserves_existing(self):
        existing = ("[DEFAULT]\nuser = ocid1.user.oc1..fake\n"
                    "region = us-sanjose-1\n")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(existing)
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"], self.path)
        with open(self.path, encoding="utf-8") as f:
            content = f.read()
        self.assertTrue(content.startswith(existing))
        self.assertIn("[NEWACC]", content)

    def test_append_without_trailing_newline(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..fake")
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"], self.path)
        with open(self.path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("ocid1.user.oc1..fake\n[NEWACC]", content)

    def test_duplicate_profile_refused(self):
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"], self.path)
        with self.assertRaises(ValueError) as ctx:
            hc.append_oci_profile("NEWACC", parsed["fields"], self.path)
        self.assertIn("已存在", str(ctx.exception))

    def test_profile_exists_missing_file(self):
        self.assertFalse(
            hc.profile_exists_in_config("NOPE", self.path))

    def test_default_profile_detected(self):
        """[DEFAULT] 段能被 has_section 认出（旧版 configparser 默认
        把 DEFAULT 当特殊段，has_section 永远 False）。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..fake\n")
        self.assertTrue(hc.profile_exists_in_config("DEFAULT", self.path))


# ================= R-Bot 格式 / 根目录配置 =================

FAKE_RBOT_CONFIG_TEXT = """#在oci=begin和oci=end之间放入你的API配置信息 支持多个配置文件
oci=begin
[sg1]
user=ocid1.user.oc1..fake1
fingerprint=aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy=ocid1.tenancy.oc1..fake1
region=ap-singapore-1
key_file=/root/1.pem

[sg2]
user=ocid1.user.oc1..fake2
fingerprint=11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff:00
tenancy=ocid1.tenancy.oc1..fake2
region=ap-singapore-1
key_file=/root/2.pem
oci=end

#用户信息
username=fakeuser
password=fakepassword

azure=begin

azure=end
"""


class TestExtractOciBlock(unittest.TestCase):

    def test_no_markers_returns_original(self):
        self.assertEqual(hc._extract_oci_block(FAKE_CONFIG_TEXT),
                         FAKE_CONFIG_TEXT)

    def test_markers_extract_inner_only(self):
        block = hc._extract_oci_block(FAKE_RBOT_CONFIG_TEXT)
        self.assertIn("[sg1]", block)
        self.assertIn("[sg2]", block)
        self.assertNotIn("username=", block)
        self.assertNotIn("azure=begin", block)
        self.assertNotIn("oci=begin", block)

    def test_begin_without_end_returns_none(self):
        self.assertIsNone(hc._extract_oci_block("oci=begin\n[sg1]\n"))

    def test_comment_mentioning_markers_not_matched(self):
        """注释行里提到 oci=begin 不算标记（必须整行精确匹配）。"""
        text = "#在oci=begin和oci=end之间放入配置\n" + FAKE_CONFIG_TEXT
        self.assertEqual(hc._extract_oci_block(text), text)


class TestParseRbotConfig(unittest.TestCase):

    def test_rbot_full_paste_parses_two_profiles(self):
        parsed = hc.parse_oci_config_text(FAKE_RBOT_CONFIG_TEXT)
        self.assertIsNone(parsed["error"])
        self.assertEqual([p["profile"] for p in parsed["profiles"]],
                         ["sg1", "sg2"])
        self.assertEqual(parsed["profiles"][0]["fields"]["key_file"],
                         "/root/1.pem")
        self.assertEqual(parsed["profiles"][1]["missing"], [])

    def test_rbot_second_profile_missing_keys(self):
        text = ("oci=begin\n[sg1]\nuser = ocid1.user.oc1..fake\n"
                "fingerprint = aa:bb\ntenancy = ocid1.tenancy.oc1..fake\n"
                "region = ap-singapore-1\nkey_file = /root/1.pem\n"
                "[sg2]\nuser = ocid1.user.oc1..fake\noci=end\n")
        parsed = hc.parse_oci_config_text(text)
        self.assertIsNone(parsed["error"])
        self.assertEqual(parsed["profiles"][1]["missing"],
                         ["fingerprint", "tenancy", "region", "key_file"])

    def test_rbot_bad_name_in_second_profile(self):
        text = ("oci=begin\n[sg1]\nuser = ocid1.user.oc1..fake\n"
                "[bad name]\nuser = ocid1.user.oc1..fake\noci=end\n")
        parsed = hc.parse_oci_config_text(text)
        self.assertIsNotNone(parsed["error"])
        self.assertIn("bad name", parsed["error"])

    def test_begin_without_end_errors(self):
        parsed = hc.parse_oci_config_text("oci=begin\n[sg1]\n")
        self.assertIsNotNone(parsed["error"])
        self.assertIn("oci=end", parsed["error"])

    def test_empty_block_errors(self):
        parsed = hc.parse_oci_config_text("oci=begin\noci=end\n")
        self.assertIsNotNone(parsed["error"])


class TestNormalizeOciKeyFile(unittest.TestCase):

    def test_root_path_kept(self):
        with patch.dict(os.environ, {"HOME": "/root"}):
            fields, original = hc.normalize_oci_key_file(
                "sg1", {"key_file": "/root/1.pem"})
        self.assertEqual(fields["key_file"], "/root/1.pem")
        self.assertEqual(original, "/root/1.pem")

    def test_home_subdir_kept(self):
        """~/ 下的子目录路径也保留（如旧版的 ~/.oci/xxx.pem）。"""
        with patch.dict(os.environ, {"HOME": "/root"}):
            fields, _ = hc.normalize_oci_key_file(
                "sg1", {"key_file": "/root/.oci/old-key.pem"})
        self.assertEqual(fields["key_file"], "/root/.oci/old-key.pem")

    def test_other_absolute_path_normalized(self):
        with patch.dict(os.environ, {"HOME": "/root"}):
            fields, original = hc.normalize_oci_key_file(
                "NEWACC", {"key_file": "/home/user/.oci/fake-key.pem"})
        self.assertEqual(fields["key_file"], "/root/NEWACC.pem")
        self.assertEqual(original, "/home/user/.oci/fake-key.pem")

    def test_relative_path_normalized(self):
        # Use an isolated temp HOME disjoint from the repo checkout:
        # normalize_oci_key_file resolves relative paths against the
        # cwd, and on hosts whose repo lives under HOME (e.g. .160's
        # /root/rbot-clone-test) the relative path would resolve under
        # HOME and be kept as-is instead of normalizing.
        # 用与仓库目录不相交的临时 HOME：归一化对相对路径按 cwd 解析，
        # 仓库在 HOME 下的机器（如 .160 的 /root/rbot-clone-test）上
        # 相对路径会解析到 HOME 里、被原样保留而不归一化。
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}):
                fields, _ = hc.normalize_oci_key_file(
                    "NEWACC", {"key_file": "my-key.pem"})
            self.assertEqual(fields["key_file"],
                             os.path.join(home, "NEWACC.pem"))


class TestMaybeCopyKeyFile(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_target_exists_returns_true(self):
        target = os.path.join(self._tmp.name, "new.pem")
        with open(target, "w") as f:
            f.write("already here")
        self.assertTrue(hc.maybe_copy_key_file("/nonexistent/old.pem",
                                               target))

    def test_original_copied_with_600(self):
        original = os.path.join(self._tmp.name, "old.pem")
        with open(original, "w") as f:
            f.write("fake key content")
        target = os.path.join(self._tmp.name, "new.pem")
        self.assertTrue(hc.maybe_copy_key_file(original, target))
        with open(target) as f:
            self.assertEqual(f.read(), "fake key content")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_neither_exists_returns_false(self):
        target = os.path.join(self._tmp.name, "new.pem")
        self.assertFalse(hc.maybe_copy_key_file(
            os.path.join(self._tmp.name, "missing.pem"), target))


class TestPrepareAndCommitProfiles(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = os.path.join(self._tmp.name, "oci_config")
        self.legacy = os.path.join(self._tmp.name, "legacy_config")
        self.sdk = os.path.join(self._tmp.name, "sdk_config")
        for attr, path in (("OCI_CONFIG_PATH", self.target),
                           ("OCI_CONFIG_LEGACY_PATH", self.legacy),
                           ("OCI_CONFIG_SDK_PATH", self.sdk)):
            p = patch.object(hc, attr, path)
            p.start()
            self.addCleanup(p.stop)

    def test_prepare_skips_existing_and_normalizes(self):
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("[sg1]\nuser = ocid1.user.oc1..fake\n")
        with patch.dict(os.environ, {"HOME": "/root"}):
            parsed = hc.parse_oci_config_text(FAKE_RBOT_CONFIG_TEXT)
            new, skipped, error = hc.prepare_oci_profiles(parsed)
        self.assertEqual(error, "")
        self.assertEqual(skipped, ["sg1"])
        self.assertEqual([p["profile"] for p in new], ["sg2"])
        self.assertEqual(new[0]["fields"]["key_file"], "/root/2.pem")

    def _write_existing(self, profile, tenancy, user):
        with open(self.target, "a", encoding="utf-8") as f:
            f.write(f"[{profile}]\nuser = {user}\n"
                    f"fingerprint = aa:bb\ntenancy = {tenancy}\n"
                    "region = ap-singapore-1\nkey_file = /root/old.pem\n")

    def _profile_text(self, profile, tenancy, user):
        return (f"[{profile}]\nuser = {user}\nfingerprint = cc:dd\n"
                f"tenancy = {tenancy}\nregion = ap-singapore-1\n"
                "key_file = /root/new.pem\n")

    def test_prepare_renames_new_account_with_colliding_name(self):
        """新账号撞了已有的 profile 名时自动改名导入，不丢账号。"""
        self._write_existing("DEFAULT",
                             "ocid1.tenancy.oc1..tenancyA",
                             "ocid1.user.oc1..userA")
        with patch.dict(os.environ, {"HOME": "/root"}):
            parsed = hc.parse_oci_config_text(self._profile_text(
                "DEFAULT", "ocid1.tenancy.oc1..tenancyB",
                "ocid1.user.oc1..userB"))
            new, skipped, error = hc.prepare_oci_profiles(parsed)
        self.assertEqual(error, "")
        self.assertEqual(skipped, [])
        self.assertEqual([p["profile"] for p in new], ["DEFAULT_2"])
        self.assertEqual(new[0]["renamed_from"], "DEFAULT")
        # key_file 已在家目录下时按原样保留（不随改名而改）。
        self.assertEqual(new[0]["fields"]["key_file"], "/root/new.pem")

    def test_prepare_rename_picks_next_free_suffix(self):
        """DEFAULT 与 DEFAULT_2 都被占时，新账号改名为 DEFAULT_3。"""
        self._write_existing("DEFAULT",
                             "ocid1.tenancy.oc1..tenancyA",
                             "ocid1.user.oc1..userA")
        self._write_existing("DEFAULT_2",
                             "ocid1.tenancy.oc1..tenancyC",
                             "ocid1.user.oc1..userC")
        with patch.dict(os.environ, {"HOME": "/root"}):
            parsed = hc.parse_oci_config_text(self._profile_text(
                "DEFAULT", "ocid1.tenancy.oc1..tenancyB",
                "ocid1.user.oc1..userB"))
            new, skipped, error = hc.prepare_oci_profiles(parsed)
        self.assertEqual(error, "")
        self.assertEqual([p["profile"] for p in new], ["DEFAULT_3"])
        self.assertEqual(new[0]["renamed_from"], "DEFAULT")

    def test_prepare_skips_same_identity_under_different_name(self):
        """同一租户+用户换了 profile 名仍是同一个账号，跳过不重复导入。"""
        self._write_existing("DEFAULT",
                             "ocid1.tenancy.oc1..tenancyA",
                             "ocid1.user.oc1..userA")
        with patch.dict(os.environ, {"HOME": "/root"}):
            parsed = hc.parse_oci_config_text(self._profile_text(
                "OTHER", "ocid1.tenancy.oc1..tenancyA",
                "ocid1.user.oc1..userA"))
            new, skipped, error = hc.prepare_oci_profiles(parsed)
        self.assertEqual(error, "")
        self.assertEqual(new, [])
        self.assertEqual(skipped, ["OTHER"])

    def test_prepare_missing_keys_error_names_profile(self):
        text = ("[GOOD]\nuser = ocid1.user.oc1..fake\n"
                "fingerprint = aa:bb\ntenancy = ocid1.tenancy.oc1..fake\n"
                "region = ap-singapore-1\nkey_file = /root/1.pem\n"
                "[BAD]\nuser = ocid1.user.oc1..fake\n")
        parsed = hc.parse_oci_config_text(text)
        new, skipped, error = hc.prepare_oci_profiles(parsed)
        self.assertEqual(new, [])
        self.assertIn("BAD", error)
        self.assertIn("缺少必填键", error)

    def test_append_migrates_legacy_on_first_write(self):
        """目标不存在、旧文件存在时，先迁移旧内容再追加。"""
        with open(self.legacy, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..fake\n")
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"])
        with open(self.target, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("[DEFAULT]", content)
        self.assertIn("[NEWACC]", content)
        with open(self.legacy, encoding="utf-8") as f:
            self.assertIn("[DEFAULT]", f.read())
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode),
                         0o600)

    def test_append_no_migration_when_target_exists(self):
        with open(self.target, "w", encoding="utf-8") as f:
            f.write("[DEFAULT]\nuser = ocid1.user.oc1..fake\n")
        with open(self.legacy, "w", encoding="utf-8") as f:
            f.write("[LEGACYONLY]\nuser = ocid1.user.oc1..fake\n")
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"])
        with open(self.target, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("[LEGACYONLY]", content)

    def test_commit_writes_and_copies_key(self):
        original = os.path.join(self._tmp.name, "old-key.pem")
        with open(original, "w") as f:
            f.write("fake key")
        new_key = os.path.join(self._tmp.name, "ZZ1.pem")
        profiles = [{"profile": "ZZ1",
                     "fields": {"user": "ocid1.user.oc1..fake",
                                "fingerprint": "aa:bb",
                                "tenancy": "ocid1.tenancy.oc1..fake",
                                "region": "ap-singapore-1",
                                "key_file": new_key},
                     "original_key_file": original}]
        results = hc.commit_oci_profiles(profiles)
        self.assertEqual(results[0]["key_ready"], True)
        self.assertTrue(os.path.exists(new_key))
        self.assertTrue(hc.profile_exists_in_config("ZZ1", self.target))

    def test_append_skips_txt_sync_when_ini_target_is_temp(self):
        """回归（2026-11-26）：INI 目标是临时文件时，append 不得同步
        写统一配置文件（门控只在写默认 INI 时才同步）。"""
        tmp_conf = os.path.join(self._tmp.name, "M_bot_config.conf")
        with patch.dict(os.environ, {"HOME": self._tmp.name,
                                     "MBOT_CONFIG_FILE": tmp_conf}):
            parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
            hc.append_oci_profile("NEWACC", parsed["fields"])
        self.assertFalse(os.path.exists(tmp_conf))

    def test_commit_key_missing_reports_not_ready(self):
        profiles = [{"profile": "ZZ2",
                     "fields": {"user": "ocid1.user.oc1..fake",
                                "fingerprint": "aa:bb",
                                "tenancy": "ocid1.tenancy.oc1..fake",
                                "region": "ap-singapore-1",
                                "key_file": "/root/ZZ2.pem"},
                     "original_key_file": "/nonexistent/old.pem"}]
        results = hc.commit_oci_profiles(profiles)
        self.assertEqual(results[0]["key_ready"], False)


class TestAddProfileCompartments(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_file = os.path.join(self._tmp.name, ".env")
        self._env_patch = patch("web.config.ENV_FILE", self.env_file)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def _profiles(self):
        return [{"profile": "SG1",
                 "fields": {"tenancy": "ocid1.tenancy.oc1..fake1"}},
                {"profile": "SG2",
                 "fields": {"tenancy": "ocid1.tenancy.oc1..fake2"}}]

    def test_writes_compartment_vars(self):
        with patch.object(hc, "update_env_file_vars") as mock_write:
            added = hc.add_profile_compartments(self._profiles())
        self.assertEqual(added, {
            "MBOT_COMPARTMENT_SG1": "ocid1.tenancy.oc1..fake1",
            "MBOT_COMPARTMENT_SG2": "ocid1.tenancy.oc1..fake2"})
        mock_write.assert_called_once_with(added)

    def test_skips_var_already_in_env(self):
        with patch.dict(os.environ,
                        {"MBOT_COMPARTMENT_SG1": "ocid1.tenancy.oc1..x"}):
            with patch.object(hc, "update_env_file_vars") as mock_write:
                added = hc.add_profile_compartments(self._profiles())
        self.assertEqual(added, {
            "MBOT_COMPARTMENT_SG2": "ocid1.tenancy.oc1..fake2"})

    def test_skips_var_already_in_env_file(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("MBOT_COMPARTMENT_SG1=ocid1.tenancy.oc1..custom\n")
        with patch.object(hc, "update_env_file_vars") as mock_write:
            added = hc.add_profile_compartments(self._profiles())
        self.assertEqual(added, {
            "MBOT_COMPARTMENT_SG2": "ocid1.tenancy.oc1..fake2"})

    def test_missing_tenancy_skipped(self):
        profiles = [{"profile": "NOTEN", "fields": {}}]
        with patch.object(hc, "update_env_file_vars") as mock_write:
            added = hc.add_profile_compartments(profiles)
        self.assertEqual(added, {})
        mock_write.assert_not_called()


# ================= Paramiko 远程执行 =================

def _mock_ssh_client(stdout_data=b"", stderr_data=b"", exit_code=0,
                      exit_ready=True):
    """构造 mock 的 paramiko.SSHClient（exec_command 返回预设输出）。"""
    client = MagicMock()
    stdout = MagicMock()
    stdout.read.return_value = stdout_data
    stdout.channel.exit_status_ready.return_value = exit_ready
    stdout.channel.recv_exit_status.return_value = exit_code
    stderr = MagicMock()
    stderr.read.return_value = stderr_data
    client.exec_command.return_value = (MagicMock(), stdout, stderr)
    return client


class TestRunRemoteCommand(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._known_hosts = patch.object(
            hc, "KNOWN_HOSTS_FILE",
            os.path.join(self._tmp.name, "known_hosts"))
        self._known_hosts.start()
        self.addCleanup(self._known_hosts.stop)
        self.host = {"host": FAKE_IP, "port": 14422, "username": "root"}

    def test_success_returns_output(self):
        client = _mock_ssh_client(b"hello\n", b"", 0)
        with patch("paramiko.SSHClient", return_value=client):
            code, out, err = hc.run_remote_command(self.host, "echo hello")
        self.assertEqual((code, out, err), (0, "hello\n", ""))
        client.connect.assert_called_once()
        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs["hostname"], FAKE_IP)
        self.assertEqual(kwargs["port"], 14422)
        client.exec_command.assert_called_once_with(
            "echo hello", timeout=hc.COMMAND_TIMEOUT_SECONDS)
        client.close.assert_called_once()

    def test_stderr_returned_separately(self):
        client = _mock_ssh_client(b"out\n", b"warn\n", 0)
        with patch("paramiko.SSHClient", return_value=client):
            code, out, err = hc.run_remote_command(self.host, "cmd")
        self.assertEqual((code, out, err), (0, "out\n", "warn\n"))

    def test_nonzero_exit_code_returned(self):
        client = _mock_ssh_client(b"", b"no such file\n", 3)
        with patch("paramiko.SSHClient", return_value=client):
            code, _out, err = hc.run_remote_command(self.host, "bad-cmd")
        self.assertEqual(code, 3)
        self.assertIn("no such file", err)

    def test_timeout_raises(self):
        client = _mock_ssh_client(exit_ready=False)
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(CommandTimeoutError):
                hc.run_remote_command(self.host, "sleep 100", timeout=0.15)
        client.close.assert_called_once()

    def test_auth_failure(self):
        client = MagicMock()
        client.connect.side_effect = paramiko.AuthenticationException("nope")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.run_remote_command(self.host, "ls")
        self.assertIn("认证失败", str(ctx.exception))

    def test_ssh_failure(self):
        client = MagicMock()
        client.connect.side_effect = paramiko.SSHException("boom")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.run_remote_command(self.host, "ls")
        self.assertIn("连接失败", str(ctx.exception))

    def test_missing_key_file_refused(self):
        host = dict(self.host, key_path="/nonexistent/fake-key")
        with patch("paramiko.SSHClient") as mock_cls:
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.run_remote_command(host, "ls")
        self.assertIn("密钥文件不存在", str(ctx.exception))
        mock_cls.assert_not_called()

    def test_key_auth_uses_key_filename(self):
        key_path = os.path.join(self._tmp.name, "fake-key")
        with open(key_path, "w", encoding="utf-8") as f:
            f.write("fake")
        host = dict(self.host, key_path=key_path)
        client = _mock_ssh_client(b"ok\n", b"", 0)
        with patch("paramiko.SSHClient", return_value=client):
            hc.run_remote_command(host, "ls")
        kwargs = client.connect.call_args.kwargs
        self.assertEqual(kwargs["key_filename"], key_path)
        self.assertFalse(kwargs["look_for_keys"])

    def test_port_fallback_22_to_14422(self):
        """主端口 22 被拒时重试 14422，成功后返回输出。"""
        host = dict(self.host, port=22)
        client = _mock_ssh_client(b"ok\n", b"", 0)
        client.connect.side_effect = [
            ConnectionRefusedError("refused"), None]
        with patch("paramiko.SSHClient", return_value=client):
            code, out, _err = hc.run_remote_command(host, "ls")
        self.assertEqual((code, out), (0, "ok\n"))
        self.assertEqual(client.connect.call_count, 2)
        ports = [c.kwargs["port"] for c in client.connect.call_args_list]
        self.assertEqual(ports, [22, 14422])

    def test_port_fallback_14422_to_22(self):
        """主端口 14422 超时时重试 22，成功后返回输出。"""
        client = _mock_ssh_client(b"ok\n", b"", 0)
        client.connect.side_effect = [socket.timeout("timed out"), None]
        with patch("paramiko.SSHClient", return_value=client):
            code, out, _err = hc.run_remote_command(self.host, "ls")
        self.assertEqual((code, out), (0, "ok\n"))
        self.assertEqual(client.connect.call_count, 2)
        ports = [c.kwargs["port"] for c in client.connect.call_args_list]
        self.assertEqual(ports, [14422, 22])

    def test_port_fallback_no_valid_connections(self):
        """NoValidConnectionsError 也触发端口回退。"""
        from paramiko.ssh_exception import NoValidConnectionsError
        host = dict(self.host, port=22)
        client = _mock_ssh_client(b"ok\n", b"", 0)
        client.connect.side_effect = [
            NoValidConnectionsError({"203.0.113.10": "refused"}), None]
        with patch("paramiko.SSHClient", return_value=client):
            code, _out, _err = hc.run_remote_command(host, "ls")
        self.assertEqual(code, 0)
        self.assertEqual(client.connect.call_count, 2)

    def test_port_fallback_both_fail(self):
        """主备端口都失败时抛连接失败（共尝试两次）。"""
        host = dict(self.host, port=22)
        client = MagicMock()
        client.connect.side_effect = ConnectionRefusedError("refused")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.run_remote_command(host, "ls")
        self.assertIn("连接失败", str(ctx.exception))
        self.assertEqual(client.connect.call_count, 2)

    def test_auth_failure_no_retry(self):
        """认证失败不重试：connect 只调用一次。"""
        client = MagicMock()
        client.connect.side_effect = paramiko.AuthenticationException("nope")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.run_remote_command(self.host, "ls")
        self.assertIn("认证失败", str(ctx.exception))
        self.assertEqual(client.connect.call_count, 1)

    def test_custom_port_no_fallback(self):
        """非 22/14422 的端口不回退：失败一次即报错。"""
        host = dict(self.host, port=2222)
        client = MagicMock()
        client.connect.side_effect = ConnectionRefusedError("refused")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(RemoteCommandError):
                hc.run_remote_command(host, "ls")
        self.assertEqual(client.connect.call_count, 1)


class TestPutRemoteFile(unittest.TestCase):
    """put_remote_file：SFTP 上传内容 + chmod（看门狗安装用）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._known_hosts = patch.object(
            hc, "KNOWN_HOSTS_FILE",
            os.path.join(self._tmp.name, "known_hosts"))
        self._known_hosts.start()
        self.addCleanup(self._known_hosts.stop)
        self.host = {"host": FAKE_IP, "port": 14422, "username": "root"}

    def test_uploads_content_and_sets_mode(self):
        client = _mock_ssh_client()
        sftp = MagicMock()
        remote_file = MagicMock()
        sftp.open.return_value.__enter__.return_value = remote_file
        client.open_sftp.return_value = sftp
        with patch("paramiko.SSHClient", return_value=client):
            hc.put_remote_file(self.host, "hello\n", "/tmp/x.sh", mode=0o755)
        client.open_sftp.assert_called_once()
        sftp.open.assert_called_once_with("/tmp/x.sh", "w")
        remote_file.write.assert_called_once_with("hello\n")
        sftp.chmod.assert_called_once_with("/tmp/x.sh", 0o755)
        sftp.close.assert_called_once()
        client.close.assert_called_once()

    def test_default_mode_644(self):
        client = _mock_ssh_client()
        sftp = MagicMock()
        client.open_sftp.return_value = sftp
        with patch("paramiko.SSHClient", return_value=client):
            hc.put_remote_file(self.host, "cfg", "/tmp/cfg")
        sftp.chmod.assert_called_once_with("/tmp/cfg", 0o644)

    def test_sftp_failure_raises_remote_error(self):
        client = _mock_ssh_client()
        client.open_sftp.side_effect = OSError("sftp down")
        with patch("paramiko.SSHClient", return_value=client):
            with self.assertRaises(hc.RemoteCommandError) as ctx:
                hc.put_remote_file(self.host, "x", "/tmp/x")
        self.assertIn("SFTP", str(ctx.exception))


class TestMemhogEnvOverrides(unittest.TestCase):
    """MEMHOG_SSH_KEY / MEMHOG_SSH_PORT 环境变量覆盖。

    两者是模块级常量（import 时读取），测试用子进程验证：
    子进程带环境变量 import 模块并打印常量值。不能用
    importlib.reload——reload 会让 RemoteCommandError 变成新类，
    本进程后续测试的 assertRaises 就失效了。
    """

    def _read_constants(self, env):
        script = ("import tgbot.handlers_client as hc; "
                  "print(hc.MEMHOG_SSH_KEY); print(hc.MEMHOG_SSH_PORT)")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, cwd=root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key, port = proc.stdout.strip().splitlines()
        return key, int(port)

    def _base_env(self):
        env = dict(os.environ)
        env.pop("MBOT_MEMHOG_SSH_KEY", None)
        env.pop("MBOT_MEMHOG_SSH_PORT", None)
        return env

    def test_env_overrides(self):
        env = self._base_env()
        env["MBOT_MEMHOG_SSH_KEY"] = "/opt/rbot-clone/web/data/bot_ssh_key"
        env["MBOT_MEMHOG_SSH_PORT"] = "14422"
        key, port = self._read_constants(env)
        self.assertEqual(key, "/opt/rbot-clone/web/data/bot_ssh_key")
        self.assertEqual(port, 14422)

    def test_defaults_without_env(self):
        key, port = self._read_constants(self._base_env())
        self.assertEqual(key, os.path.expanduser("~/.ssh/id_ed25519"))
        self.assertEqual(port, 22)

    def test_invalid_port_env_falls_back_to_22(self):
        env = self._base_env()
        env["MBOT_MEMHOG_SSH_PORT"] = "not-a-port"
        _key, port = self._read_constants(env)
        self.assertEqual(port, 22)


# ================= 菜单 23/24：命令构造与存储 =================

class TestMemhogCommands(unittest.TestCase):

    def test_build_memhog_command(self):
        cmd = hc.build_memhog_command()
        self.assertIn("nohup python3 -c", cmd)
        self.assertIn("/proc/meminfo", cmd)
        self.assertIn("0.25", cmd)
        self.assertIn("bytearray", cmd)
        self.assertIn("echo $!", cmd)
        self.assertIn(hc.MEMHOG_LOG_PATH, cmd)

    def test_build_memhog_command_fraction(self):
        self.assertIn("0.5", hc.build_memhog_command(0.5))

    def test_parse_memhog_pid(self):
        self.assertEqual(hc.parse_memhog_pid("12345\n"), 12345)
        self.assertEqual(hc.parse_memhog_pid("12345"), 12345)
        self.assertEqual(hc.parse_memhog_pid("some warning\n678\n"), 678)
        self.assertIsNone(hc.parse_memhog_pid(""))
        self.assertIsNone(hc.parse_memhog_pid("no pid here\n"))

    def test_kill_command(self):
        self.assertEqual(hc.build_kill_command(4242),
                         "kill 4242 2>/dev/null && echo KILLED || echo GONE")
        self.assertTrue(hc.parse_kill_output("KILLED\n"))
        self.assertFalse(hc.parse_kill_output("GONE\n"))

    def test_drop_caches_command(self):
        self.assertEqual(hc.build_drop_caches_command(False),
                         "sync; echo 3 > /proc/sys/vm/drop_caches")
        sudo_cmd = hc.build_drop_caches_command(True)
        # When not root, the redirect must be wrapped in sudo sh -c (sudo echo 3 > ... does not work). 非 root 时必须 sudo sh -c 包住重定向（sudo echo 3 > ... 不生效）。
        self.assertEqual(sudo_cmd,
                         "sync; sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'")


class TestMemHogStorage(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "mem_hog.json")

    def test_load_missing_returns_empty(self):
        self.assertEqual(hc.load_mem_hogs(self.path), {})

    def test_record_format(self):
        records = hc.record_memhog(FAKE_INSTANCE_ID, 4242, FAKE_IP,
                                   self.path)
        entry = records[FAKE_INSTANCE_ID]
        self.assertEqual(entry["pid"], 4242)
        self.assertEqual(entry["host"], FAKE_IP)
        self.assertIn("started", entry)
        # On-disk format, matching the task requirement: {OCID: {pid, started, host}}. 落盘格式与任务要求一致：{OCID: {pid, started, host}}。
        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(set(saved[FAKE_INSTANCE_ID]),
                         {"pid", "started", "host"})

    def test_remove_existing_and_missing(self):
        hc.record_memhog(FAKE_INSTANCE_ID, 4242, FAKE_IP, self.path)
        self.assertTrue(hc.remove_memhog(FAKE_INSTANCE_ID, self.path))
        self.assertEqual(hc.load_mem_hogs(self.path), {})
        self.assertFalse(hc.remove_memhog(FAKE_INSTANCE_ID, self.path))


class TestResolveSshTarget(unittest.TestCase):

    def setUp(self):
        self.cli = MagicMock()
        self.instance = dict(FAKE_INSTANCES[0])

    def test_no_public_ip(self):
        with patch.object(hc, "get_public_ip", return_value=None):
            target, error = hc.resolve_instance_ssh_target(
                self.cli, self.instance, hosts=[])
        self.assertIsNone(target)
        self.assertIn("没有公网 IP", error)

    def test_hosts_match_by_ip(self):
        with patch.object(hc, "get_public_ip", return_value=FAKE_IP):
            target, error = hc.resolve_instance_ssh_target(
                self.cli, self.instance, hosts=FAKE_HOSTS)
        self.assertIsNone(error)
        self.assertEqual(target["host"], FAKE_IP)
        self.assertEqual(target["port"], 14422)
        self.assertEqual(target["username"], "root")
        self.assertEqual(target["key_path"], "/home/user/.ssh/fake-key")

    def test_hosts_match_without_key_falls_back_to_defaults(self):
        hosts = [{"host": FAKE_IP, "username": "opc",
                  "auth_type": "password"}]
        with patch.object(hc, "get_public_ip", return_value=FAKE_IP):
            target, error = hc.resolve_instance_ssh_target(
                self.cli, self.instance, hosts=hosts)
        self.assertIsNone(error)
        self.assertEqual(target["username"], hc.MEMHOG_SSH_USER)
        self.assertEqual(target["key_path"], hc.MEMHOG_SSH_KEY)

    def test_no_match_uses_defaults(self):
        with patch.object(hc, "get_public_ip", return_value=FAKE_IP):
            target, error = hc.resolve_instance_ssh_target(
                self.cli, self.instance, hosts=[])
        self.assertIsNone(error)
        self.assertEqual(target, {
            "host": FAKE_IP,
            "port": hc.MEMHOG_SSH_PORT,
            "username": hc.MEMHOG_SSH_USER,
            "key_path": hc.MEMHOG_SSH_KEY,
        })


class TestStartClearMemhog(unittest.TestCase):

    def setUp(self):
        self.host = {"host": FAKE_IP, "port": 22, "username": "root"}

    def test_start_returns_pid(self):
        with patch.object(hc, "run_remote_command",
                          return_value=(0, "4242\n", "")) as mock_run:
            pid = hc.start_memhog_on_host(self.host)
        self.assertEqual(pid, 4242)
        command = mock_run.call_args.args[1]
        self.assertEqual(command, hc.build_memhog_command())

    def test_start_nonzero_exit_raises(self):
        with patch.object(hc, "run_remote_command",
                          return_value=(1, "", "python3: not found")):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.start_memhog_on_host(self.host)
        self.assertIn("启动占用进程失败", str(ctx.exception))

    def test_start_bad_pid_output_raises(self):
        with patch.object(hc, "run_remote_command",
                          return_value=(0, "no pid\n", "")):
            with self.assertRaises(RemoteCommandError) as ctx:
                hc.start_memhog_on_host(self.host)
        self.assertIn("无法解析", str(ctx.exception))

    def test_clear_with_pid(self):
        calls = [(0, "KILLED\n", ""), (0, "", "")]
        with patch.object(hc, "run_remote_command",
                          side_effect=calls) as mock_run:
            result = hc.clear_memhog_on_host(self.host, pid=4242)
        self.assertEqual(result["killed"], True)
        self.assertTrue(result["drop_caches_ok"])
        commands = [c.args[1] for c in mock_run.call_args_list]
        self.assertEqual(commands[0], hc.build_kill_command(4242))
        self.assertEqual(commands[1], hc.build_drop_caches_command(False))

    def test_clear_pid_already_gone(self):
        calls = [(0, "GONE\n", ""), (0, "", "")]
        with patch.object(hc, "run_remote_command", side_effect=calls):
            result = hc.clear_memhog_on_host(self.host, pid=4242)
        self.assertEqual(result["killed"], False)
        self.assertTrue(result["drop_caches_ok"])

    def test_clear_without_pid_skips_kill(self):
        with patch.object(hc, "run_remote_command",
                          return_value=(0, "", "")) as mock_run:
            result = hc.clear_memhog_on_host(self.host, pid=None)
        self.assertIsNone(result["killed"])
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[1],
                         hc.build_drop_caches_command(False))

    def test_clear_uses_sudo_for_non_root(self):
        calls = [(0, "", "")]
        with patch.object(hc, "run_remote_command",
                          side_effect=calls) as mock_run:
            hc.clear_memhog_on_host(self.host, pid=None, use_sudo=True)
        self.assertEqual(mock_run.call_args.args[1],
                         hc.build_drop_caches_command(True))

    def test_clear_drop_caches_failure_reported(self):
        calls = [(0, "KILLED\n", ""), (1, "", "Permission denied")]
        with patch.object(hc, "run_remote_command", side_effect=calls):
            result = hc.clear_memhog_on_host(self.host, pid=4242)
        self.assertEqual(result["killed"], True)
        self.assertFalse(result["drop_caches_ok"])
        self.assertIn("Permission denied", result["drop_caches_output"])


# ================= Handler 异步测试 =================

class ClientHandlersTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + 数据文件全部指向临时目录 + OCI 边界 mock。"""

    def setUp(self):
        self._allowed = patch.object(
            bot_config, "ALLOWED_USER_IDS", {ALLOWED_USER})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_hosts = os.path.join(self._tmp.name, "hosts.json")
        self.tmp_keys = os.path.join(self._tmp.name, "ssh_keys.json")
        self.tmp_proxy = os.path.join(self._tmp.name, "proxy.json")
        self.tmp_hogs = os.path.join(self._tmp.name, "mem_hog.json")
        self.tmp_oci = os.path.join(self._tmp.name, "oci_config")
        self.tmp_known = os.path.join(self._tmp.name, "known_hosts")
        for attr, path in (
                ("HOSTS_FILE", self.tmp_hosts),
                ("SSH_KEYS_FILE", self.tmp_keys),
                ("PROXY_FILE", self.tmp_proxy),
                ("MEM_HOG_FILE", self.tmp_hogs),
                ("OCI_CONFIG_PATH", self.tmp_oci),
                ("KNOWN_HOSTS_FILE", self.tmp_known)):
            p = patch.object(hc, attr, path)
            p.start()
            self.addCleanup(p.stop)

        self.cli = MagicMock()
        self._cli = patch.object(hc, "get_cli", return_value=self.cli)
        self._cli.start()
        self.addCleanup(self._cli.stop)
        self._compartment = patch.object(
            hc, "get_compartment", return_value=FAKE_COMPARTMENT)
        self._compartment.start()
        self.addCleanup(self._compartment.stop)

    def write_hosts(self, hosts):
        with open(self.tmp_hosts, "w", encoding="utf-8") as f:
            json.dump(hosts, f)

    def write_hogs(self, records):
        with open(self.tmp_hogs, "w", encoding="utf-8") as f:
            json.dump(records, f)


class TestWhitelist(ClientHandlersTestCase):

    async def test_all_commands_deny_non_whitelisted(self):
        """四个顶层命令 + 文本/文件入口都不在白名单时直接拒绝。"""
        handlers = [
            (hc.command_command, make_message_update(DENIED_USER)),
            (hc.pubkey_command, make_message_update(DENIED_USER)),
            (hc.oproxy_command, make_message_update(DENIED_USER)),
            (hc.uploadapi_command, make_message_update(DENIED_USER)),
            (hc.handle_client_text_input,
             make_message_update(DENIED_USER, text="ls")),
            (hc.handle_client_document, make_message_update(DENIED_USER)),
            (hc.handle_memballoon, make_message_update(DENIED_USER)),
            (hc.handle_memclear, make_message_update(DENIED_USER)),
        ]
        for handler, update in handlers:
            with self.subTest(handler=handler.__name__):
                await handler(update, make_context())
                self.assertIn("未授权", last_reply_text(update))


class TestCommandHandlers(ClientHandlersTestCase):

    async def test_command_no_hosts(self):
        update = make_message_update()
        await hc.command_command(update, make_context())
        self.assertIn("还没有已保存的主机", last_reply_text(update))

    async def test_command_lists_hosts(self):
        self.write_hosts(FAKE_HOSTS)
        update = make_message_update()
        context = make_context()
        await hc.command_command(update, context)
        self.assertIn("sg-a1", last_reply_text(update))
        self.assertEqual(context.user_data["cmd_hosts"], FAKE_HOSTS)
        data = all_callback_data(reply_markup_of(update))
        self.assertIn("cmd:host:0", data)
        self.assertIn("cmd:host:1", data)

    async def test_host_pick_password_refused(self):
        self.write_hosts(FAKE_HOSTS)
        context = make_context()
        context.user_data["cmd_hosts"] = FAKE_HOSTS
        update = make_callback_update("cmd:host:1")
        await hc.command_host_callback(update, context)
        self.assertIn("密码认证", last_reply_text(update))
        self.assertNotIn("cmd_awaiting", context.user_data)

    async def test_host_pick_sets_awaiting(self):
        context = make_context()
        context.user_data["cmd_hosts"] = FAKE_HOSTS
        update = make_callback_update("cmd:host:0")
        await hc.command_host_callback(update, context)
        self.assertTrue(context.user_data["cmd_awaiting"])
        self.assertEqual(context.user_data["cmd_host_idx"], 0)
        self.assertIn("请直接发送", last_reply_text(update))

    async def test_host_pick_expired(self):
        update = make_callback_update("cmd:host:0")
        await hc.command_host_callback(update, make_context())
        self.assertIn("已过期", last_reply_text(update))

    async def test_done_clears_state(self):
        context = make_context()
        context.user_data["cmd_awaiting"] = True
        update = make_callback_update("cmd:done")
        await hc.command_host_callback(update, context)
        self.assertNotIn("cmd_awaiting", context.user_data)
        self.assertIn("已结束", last_reply_text(update))

    def _awaiting_context(self):
        context = make_context()
        context.user_data["cmd_hosts"] = FAKE_HOSTS
        context.user_data["cmd_host_idx"] = 0
        context.user_data["cmd_awaiting"] = True
        return context

    async def test_text_dangerous_rejected(self):
        context = self._awaiting_context()
        update = make_message_update(text="rm -rf /")
        with patch.object(hc, "run_remote_command") as mock_run:
            await hc.handle_client_text_input(update, context)
        mock_run.assert_not_called()
        self.assertIn("拒绝执行", last_reply_text(update))
        self.assertTrue(context.user_data["cmd_awaiting"])

    async def test_text_success_chunked(self):
        context = self._awaiting_context()
        update = make_message_update(text="seq 1 9000")
        with patch.object(hc, "run_remote_command",
                          return_value=(0, "x" * 9000, "")):
            await hc.handle_client_text_input(update, context)
        texts = all_reply_texts(update)
        self.assertIn("正在 sg-a1 上执行", texts[0])
        self.assertIn("退出码：0", texts[1])
        chunks = texts[2:]
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), "x" * 9000)

    async def test_text_timeout_reported(self):
        context = self._awaiting_context()
        update = make_message_update(text="sleep 100")
        with patch.object(hc, "run_remote_command",
                          side_effect=CommandTimeoutError("命令执行超时")):
            await hc.handle_client_text_input(update, context)
        self.assertIn("超时", last_reply_text(update))

    async def test_text_remote_error_reported(self):
        context = self._awaiting_context()
        update = make_message_update(text="ls")
        with patch.object(hc, "run_remote_command",
                          side_effect=RemoteCommandError("SSH 认证失败")):
            await hc.handle_client_text_input(update, context)
        self.assertIn("执行失败", last_reply_text(update))

    async def test_text_no_output(self):
        context = self._awaiting_context()
        update = make_message_update(text="true")
        with patch.object(hc, "run_remote_command",
                          return_value=(0, "", "")):
            await hc.handle_client_text_input(update, context)
        self.assertIn("无输出", last_reply_text(update))

    async def test_text_stderr_shown(self):
        context = self._awaiting_context()
        update = make_message_update(text="ls /nonexistent")
        with patch.object(hc, "run_remote_command",
                          return_value=(2, "", "No such file")):
            await hc.handle_client_text_input(update, context)
        self.assertIn("[stderr]", last_reply_text(update))
        self.assertIn("No such file", last_reply_text(update))

    async def test_text_router_ignores_without_state(self):
        update = make_message_update(text="你好")
        await hc.handle_client_text_input(update, make_context())
        update.effective_message.reply_text.assert_not_awaited()

    async def test_text_router_skips_pending_input(self):
        context = self._awaiting_context()
        context.user_data["pending_input"] = {"kind": "rename", "idx": 0}
        update = make_message_update(text="new-name")
        with patch.object(hc, "run_remote_command") as mock_run:
            await hc.handle_client_text_input(update, context)
        mock_run.assert_not_called()
        update.effective_message.reply_text.assert_not_awaited()


class TestPubkeyHandlers(ClientHandlersTestCase):

    async def test_pubkey_empty_list(self):
        update = make_message_update()
        await hc.pubkey_command(update, make_context())
        self.assertIn("还没有保存的公钥", last_reply_text(update))
        self.assertIn("pk:add",
                      all_callback_data(reply_markup_of(update)))

    async def test_pubkey_add_flow(self):
        context = make_context()
        update = make_callback_update("pk:add")
        await hc.pubkey_callback(update, context)
        self.assertTrue(context.user_data["pk_awaiting"])
        self.assertIn("只传公钥", last_reply_text(update))

        update2 = make_message_update(text=fake_pubkey())
        await hc.handle_client_text_input(update2, context)
        self.assertIn("已保存", last_reply_text(update2))
        self.assertNotIn("pk_awaiting", context.user_data)
        self.assertEqual(len(hc.load_ssh_keys(self.tmp_keys)), 1)

    async def test_pubkey_add_invalid_keeps_waiting(self):
        context = make_context()
        context.user_data["pk_awaiting"] = True
        update = make_message_update(text="not a key")
        await hc.handle_client_text_input(update, context)
        self.assertIn("格式不对", last_reply_text(update))
        self.assertTrue(context.user_data["pk_awaiting"])
        self.assertEqual(hc.load_ssh_keys(self.tmp_keys), [])

    async def test_pubkey_delete_requires_confirm(self):
        hc.add_ssh_key(fake_pubkey(), self.tmp_keys)
        context = make_context()
        update = make_callback_update("pk:del:0")
        await hc.pubkey_callback(update, context)
        self.assertIn("确认删除", last_reply_text(update))
        self.assertIn("pk:confirmdel:0",
                      all_callback_data(reply_markup_of(update)))
        self.assertEqual(len(hc.load_ssh_keys(self.tmp_keys)), 1)
        update2 = make_callback_update("pk:confirmdel:0")
        await hc.pubkey_callback(update2, context)
        self.assertIn("已删除", last_reply_text(update2))
        self.assertEqual(hc.load_ssh_keys(self.tmp_keys), [])

    async def test_pubkey_delete_invalid_index(self):
        update = make_callback_update("pk:del:9")
        await hc.pubkey_callback(update, make_context())
        self.assertIn("不存在", last_reply_text(update))


class TestOproxyHandlers(ClientHandlersTestCase):

    async def test_oproxy_view_none(self):
        update = make_message_update()
        await hc.oproxy_command(update, make_context())
        self.assertIn("未设置", last_reply_text(update))
        data = all_callback_data(reply_markup_of(update))
        self.assertIn("px:set", data)
        self.assertNotIn("px:clear", data)

    async def test_oproxy_view_current(self):
        hc.save_proxy("http://127.0.0.1:7890", self.tmp_proxy)
        update = make_message_update()
        await hc.oproxy_command(update, make_context())
        self.assertIn("http://127.0.0.1:7890", last_reply_text(update))
        self.assertIn("px:clear",
                      all_callback_data(reply_markup_of(update)))

    async def test_oproxy_set_flow(self):
        context = make_context()
        update = make_callback_update("px:set")
        await hc.oproxy_callback(update, context)
        self.assertTrue(context.user_data["px_awaiting"])

        update2 = make_message_update(text="socks5://127.0.0.1:1080")
        await hc.handle_client_text_input(update2, context)
        self.assertIn("已设置", last_reply_text(update2))
        self.assertIn("重启机器人服务后生效", last_reply_text(update2))
        self.assertEqual(hc.load_proxy(self.tmp_proxy),
                         "socks5://127.0.0.1:1080")

    async def test_oproxy_set_invalid_keeps_waiting(self):
        context = make_context()
        context.user_data["px_awaiting"] = True
        update = make_message_update(text="ftp://127.0.0.1:21")
        await hc.handle_client_text_input(update, context)
        self.assertIn("http://", last_reply_text(update))
        self.assertTrue(context.user_data["px_awaiting"])
        self.assertIsNone(hc.load_proxy(self.tmp_proxy))

    async def test_oproxy_clear_requires_confirm(self):
        hc.save_proxy("http://127.0.0.1:7890", self.tmp_proxy)
        context = make_context()
        update = make_callback_update("px:clear")
        await hc.oproxy_callback(update, context)
        self.assertIn("确认清除", last_reply_text(update))
        self.assertIn("px:confirmclear",
                      all_callback_data(reply_markup_of(update)))
        self.assertIsNotNone(hc.load_proxy(self.tmp_proxy))

        update2 = make_callback_update("px:confirmclear")
        await hc.oproxy_callback(update2, context)
        self.assertIn("已清除", last_reply_text(update2))
        self.assertIn("重启机器人服务后生效", last_reply_text(update2))
        self.assertIsNone(hc.load_proxy(self.tmp_proxy))


class TestUploadapiHandlers(ClientHandlersTestCase):

    async def test_uploadapi_prompt(self):
        context = make_context()
        update = make_message_update()
        await hc.uploadapi_command(update, context)
        self.assertTrue(context.user_data["ua_awaiting"])
        self.assertIn("私钥文件", last_reply_text(update))

    async def test_uploadapi_text_valid_shows_confirm(self):
        context = make_context()
        context.user_data["ua_awaiting"] = True
        update = make_message_update(text=FAKE_CONFIG_TEXT)
        await hc.handle_client_text_input(update, context)
        text = last_reply_text(update)
        self.assertIn("确认添加", text)
        self.assertIn("NEWACC", text)
        self.assertIn("ap-singapore-1", text)
        # A user OCID shows only its last 4 chars, never the full OCID. 用户 OCID 只显示后 4 位，不贴完整 OCID。
        self.assertNotIn("ocid1.user.oc1..fake", text)
        self.assertIn("ua:confirm",
                      all_callback_data(reply_markup_of(update)))
        self.assertEqual(context.user_data["ua_pending"]["profiles"][0]
                         ["profile"], "NEWACC")

    async def test_uploadapi_text_missing_keys(self):
        context = make_context()
        context.user_data["ua_awaiting"] = True
        update = make_message_update(
            text="[NEWACC]\nuser = ocid1.user.oc1..fake\n")
        await hc.handle_client_text_input(update, context)
        text = last_reply_text(update)
        self.assertIn("缺少必填键", text)
        self.assertIn("region", text)
        self.assertIn("key_file", text)

    async def test_uploadapi_duplicate_profile_refused(self):
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        hc.append_oci_profile("NEWACC", parsed["fields"], self.tmp_oci)
        context = make_context()
        context.user_data["ua_awaiting"] = True
        update = make_message_update(text=FAKE_CONFIG_TEXT)
        await hc.handle_client_text_input(update, context)
        self.assertIn("已存在", last_reply_text(update))
        self.assertIn("改名", last_reply_text(update))

    async def test_uploadapi_confirm_writes_file(self):
        context = make_context()
        parsed = hc.parse_oci_config_text(FAKE_CONFIG_TEXT)
        fields, original = hc.normalize_oci_key_file(
            "NEWACC", parsed["fields"])
        context.user_data["ua_pending"] = {
            "profiles": [{"profile": "NEWACC", "fields": fields,
                          "original_key_file": original}]}
        context.user_data["ua_awaiting"] = True
        update = make_callback_update("ua:confirm")
        with patch.object(hc, "update_env_file_vars") as mock_env:
            await hc.uploadapi_callback(update, context)
        mock_env.assert_called_once()
        self.assertIn("MBOT_COMPARTMENT_NEWACC",
                      mock_env.call_args.args[0])
        self.assertIn("已添加", last_reply_text(update))
        self.assertIn("scp", last_reply_text(update))
        self.assertTrue(hc.profile_exists_in_config("NEWACC", self.tmp_oci))
        self.assertNotIn("ua_pending", context.user_data)
        self.assertNotIn("ua_awaiting", context.user_data)

    async def test_uploadapi_confirm_without_pending(self):
        update = make_callback_update("ua:confirm")
        await hc.uploadapi_callback(update, make_context())
        self.assertIn("已过期", last_reply_text(update))

    async def test_uploadapi_document(self):
        context = make_context()
        context.user_data["ua_awaiting"] = True
        update = make_message_update()
        document = MagicMock()
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(FAKE_CONFIG_TEXT.encode("utf-8")))
        document.get_file = AsyncMock(return_value=tg_file)
        update.effective_message.document = document
        await hc.handle_client_document(update, context)
        self.assertIn("确认添加", last_reply_text(update))
        self.assertEqual(context.user_data["ua_pending"]["profiles"][0]
                         ["profile"], "NEWACC")

    async def test_document_ignored_without_state(self):
        update = make_message_update()
        update.effective_message.document = MagicMock()
        await hc.handle_client_document(update, make_context())
        update.effective_message.reply_text.assert_not_awaited()


class TestMemballoonHandlers(ClientHandlersTestCase):

    def _target(self):
        return {"host": FAKE_IP, "port": 22, "username": "root",
                "key_path": None}, None

    async def test_memballoon_lists_running_only(self):
        context = make_context()
        update = make_message_update()
        with patch.object(hc, "list_instances",
                          return_value=FAKE_INSTANCES):
            await hc.handle_memballoon(update, context)
        text = last_reply_text(update)
        self.assertIn("web-server", text)
        self.assertNotIn("e2-test", text)
        data = all_callback_data(reply_markup_of(update))
        self.assertIn("mh:pick:0", data)
        self.assertEqual(len(context.user_data["instances"]), 1)

    async def test_memballoon_no_running(self):
        context = make_context()
        update = make_message_update()
        with patch.object(hc, "list_instances",
                          return_value=[FAKE_INSTANCES[1]]):
            await hc.handle_memballoon(update, context)
        self.assertIn("没有运行中", last_reply_text(update))

    def _stored_context(self):
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        return context

    async def test_pick_duplicate_refused(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mh:pick:0")
        await hc.memballoon_callback(update, self._stored_context())
        text = last_reply_text(update)
        self.assertIn("已在占用内存中", text)
        self.assertIn("4242", text)
        self.assertNotIn("mh:go:0",
                         all_callback_data(reply_markup_of(update)))

    async def test_pick_shows_confirm(self):
        update = make_callback_update("mh:pick:0")
        await hc.memballoon_callback(update, self._stored_context())
        self.assertIn("确认", last_reply_text(update))
        self.assertIn("25%", last_reply_text(update))
        self.assertIn("mh:go:0",
                      all_callback_data(reply_markup_of(update)))

    async def test_go_success_records_pid(self):
        context = self._stored_context()
        update = make_callback_update("mh:go:0")
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "start_memhog_on_host",
                          return_value=4242) as mock_start:
            await hc.memballoon_callback(update, context)
        mock_start.assert_called_once()
        records = hc.load_mem_hogs(self.tmp_hogs)
        self.assertEqual(records[FAKE_INSTANCE_ID]["pid"], 4242)
        self.assertEqual(records[FAKE_INSTANCE_ID]["host"], FAKE_IP)
        text = last_sent_text(update, context)
        self.assertIn("已在实例", text)
        self.assertIn("4242", text)

    async def test_go_duplicate_refused(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mh:go:0")
        with patch.object(hc, "start_memhog_on_host") as mock_start:
            await hc.memballoon_callback(update, self._stored_context())
        mock_start.assert_not_called()
        self.assertIn("拒绝重复启动", last_reply_text(update))

    async def test_go_no_public_ip(self):
        update = make_callback_update("mh:go:0")
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=(None, "该实例没有公网 IP，无法 SSH 连接。")):
            await hc.memballoon_callback(update, self._stored_context())
        self.assertIn("没有公网 IP", last_reply_text(update))

    async def test_go_start_failure(self):
        update = make_callback_update("mh:go:0")
        context = self._stored_context()
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "start_memhog_on_host",
                          side_effect=RemoteCommandError("SSH 认证失败")):
            await hc.memballoon_callback(update, context)
        self.assertIn("启动失败", last_sent_text(update, context))
        self.assertEqual(hc.load_mem_hogs(self.tmp_hogs), {})


class TestMemclearHandlers(ClientHandlersTestCase):

    def _target(self, username="root"):
        return {"host": FAKE_IP, "port": 22, "username": username,
                "key_path": None}, None

    def _stored_context(self):
        context = make_context()
        context.user_data["instances"] = [FAKE_INSTANCES[0]]
        return context

    async def test_memclear_lists_with_hog_mark(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_message_update()
        with patch.object(hc, "list_instances",
                          return_value=FAKE_INSTANCES):
            await hc.handle_memclear(update, make_context())
        self.assertIn("有占用进程", last_reply_text(update))

    async def test_pick_with_record_mentions_pid(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mc:pick:0")
        await hc.memclear_callback(update, self._stored_context())
        text = last_reply_text(update)
        self.assertIn("4242", text)
        self.assertIn("将被杀掉", text)
        self.assertIn("mc:go:0",
                      all_callback_data(reply_markup_of(update)))

    async def test_pick_without_record(self):
        update = make_callback_update("mc:pick:0")
        await hc.memclear_callback(update, self._stored_context())
        self.assertIn("无占用进程", last_reply_text(update))

    async def test_go_with_record_kills_and_clears(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mc:go:0")
        context = self._stored_context()
        clear_result = {"killed": True, "drop_caches_ok": True,
                        "drop_caches_output": ""}
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "clear_memhog_on_host",
                          return_value=clear_result) as mock_clear:
            await hc.memclear_callback(update, context)
        mock_clear.assert_called_once()
        args = mock_clear.call_args.args
        self.assertEqual(args[1], 4242)
        self.assertEqual(args[2], False)
        self.assertEqual(hc.load_mem_hogs(self.tmp_hogs), {})
        text = last_sent_text(update, context)
        self.assertIn("已杀掉占用进程 PID 4242", text)
        self.assertIn("清缓存成功", text)
        self.assertIn("已删除占用记录", text)

    async def test_go_without_record_only_clears_cache(self):
        update = make_callback_update("mc:go:0")
        context = self._stored_context()
        clear_result = {"killed": None, "drop_caches_ok": True,
                        "drop_caches_output": ""}
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "clear_memhog_on_host",
                          return_value=clear_result) as mock_clear:
            await hc.memclear_callback(update, context)
        self.assertIsNone(mock_clear.call_args.args[1])
        text = last_sent_text(update, context)
        self.assertIn("无占用进程", text)
        self.assertIn("清缓存成功", text)
        self.assertNotIn("已删除占用记录", text)

    async def test_go_non_root_uses_sudo(self):
        update = make_callback_update("mc:go:0")
        clear_result = {"killed": None, "drop_caches_ok": True,
                        "drop_caches_output": ""}
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target(username="opc")), \
             patch.object(hc, "clear_memhog_on_host",
                          return_value=clear_result) as mock_clear:
            await hc.memclear_callback(update, self._stored_context())
        self.assertEqual(mock_clear.call_args.args[2], True)

    async def test_go_pid_already_gone(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mc:go:0")
        context = self._stored_context()
        clear_result = {"killed": False, "drop_caches_ok": True,
                        "drop_caches_output": ""}
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "clear_memhog_on_host",
                          return_value=clear_result):
            await hc.memclear_callback(update, context)
        self.assertIn("已不存在", last_sent_text(update, context))
        self.assertEqual(hc.load_mem_hogs(self.tmp_hogs), {})

    async def test_go_drop_caches_failure_reported(self):
        update = make_callback_update("mc:go:0")
        context = self._stored_context()
        clear_result = {"killed": None, "drop_caches_ok": False,
                        "drop_caches_output": "Permission denied"}
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "clear_memhog_on_host",
                          return_value=clear_result):
            await hc.memclear_callback(update, context)
        text = last_sent_text(update, context)
        self.assertIn("清缓存失败", text)
        self.assertIn("Permission denied", text)

    async def test_go_ssh_failure_keeps_record(self):
        self.write_hogs({FAKE_INSTANCE_ID: {
            "pid": 4242, "started": "2026-11-23T00:00:00+00:00",
            "host": FAKE_IP}})
        update = make_callback_update("mc:go:0")
        context = self._stored_context()
        with patch.object(hc, "resolve_instance_ssh_target",
                          return_value=self._target()), \
             patch.object(hc, "clear_memhog_on_host",
                          side_effect=RemoteCommandError("SSH 连接失败")):
            await hc.memclear_callback(update, context)
        self.assertIn("清除失败", last_sent_text(update, context))
        # On SSH failure the remote state is unknown, so the record is kept (never delete by mistake). SSH 失败时不知道远端状态，记录保留（不误删）。
        self.assertIn(FAKE_INSTANCE_ID, hc.load_mem_hogs(self.tmp_hogs))


class TestDispatchWiring(unittest.TestCase):
    """菜单 23/24 已接入 handlers_extra._DISPATCH。"""

    def test_dispatch_contains_mem_items(self):
        import tgbot.handlers_extra as he

        self.assertIs(he._DISPATCH["memballoon"], hc.handle_memballoon)
        self.assertIs(he._DISPATCH["memclear"], hc.handle_memclear)


if __name__ == "__main__":
    unittest.main()
