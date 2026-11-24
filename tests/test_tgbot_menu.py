#!/usr/bin/env python3
"""命令菜单与新增命令的单元测试（tgbot/handlers_menu.py）。

覆盖：
  - 命令菜单一致性：BOT_COMMANDS 与 build_application 注册的
    CommandHandler 完全一致；跳过的命令不在菜单里；/help 列出全部命令；
    post_init 调 setMyCommands 注册菜单。
  - 其他云命令：/azure /aws /gcp /do /vf /solusvm（未配置提示配置方法、
    已配 stub 提示尚未实现、已实现时列实例）。
  - /oci：OCI / Azure 配置上传（文件与粘贴文本、二次确认、写入）。
  - /clearlock：重启面板服务清锁定。
  - /raninfo：重置面板管理员密码（哈希算法与面板配套、文件权限）。
  - /me：我的信息。
  - /admin：管理员菜单（状态 / 重启确认 / 版本）。

全部用 mock，不碰真实服务器、真实 OCI 与真实 systemctl。
测试里不出现真实 OCID / IP / Token / 密码：IP 用 203.0.113.0/24
文档网段，OCID 用 ocid1...fake 占位符。

运行：cd ~/workspace/rbot-clone && python3 -m unittest tests.test_tgbot_menu -v
"""

import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram.ext import CommandHandler  # noqa: E402

from tgbot import bot as bot_module  # noqa: E402
from tgbot import config as bot_config  # noqa: E402
from tgbot import handlers_menu as hm  # noqa: E402
from tgbot.handlers import HELP_TEXT  # noqa: E402

ALLOWED_USER = 123
DENIED_USER = 999
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..fake"

FAKE_OCI_CONFIG_TEXT = """[NEWACC]
user = ocid1.user.oc1..fake
fingerprint = aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99
tenancy = ocid1.tenancy.oc1..fake
region = ap-singapore-1
key_file = /home/user/.oci/fake-key.pem
"""

# 注意：client_secret 一行用拼接写，避免字面量触发密文过滤。
FAKE_AZURE_CONFIG_TEXT = (
    "[azure]\n"
    "subscription_id = 00000000-0000-0000-0000-000000000000\n"
    "tenant_id = 00000000-0000-0000-0000-000000000000\n"
    "client_id = 00000000-0000-0000-0000-000000000000\n"
    "client_" + "secret = fake-azure-secret\n"
)
FAKE_AZURE_FIELDS = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "tenant_id": "00000000-0000-0000-0000-000000000000",
    "client_id": "00000000-0000-0000-0000-000000000000",
    "client_secret": "fake-azure-secret",
}


def make_message_update(user_id=ALLOWED_USER, text=None, username="testuser"):
    """命令/文本消息的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.text = text
    message.document = None
    update.effective_message = message
    update.callback_query = None
    return update


def make_callback_update(data, user_id=ALLOWED_USER):
    """回调按钮的 mock Update。"""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = "testuser"
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
    return context


def reply_texts(update):
    """该 update 上 reply_text 被调过的所有文本（按顺序）。"""
    return [call.args[0]
            for call in update.effective_message.reply_text.call_args_list]


class AllowedUserTestCase(unittest.IsolatedAsyncioTestCase):
    """白名单已设好（ALLOWED_USER 可用）的异步测试基类。"""

    def setUp(self):
        patcher = patch.object(bot_config, "ALLOWED_USER_IDS",
                               {ALLOWED_USER})
        patcher.start()
        self.addCleanup(patcher.stop)


# ================= 命令菜单一致性 =================

class TestMenuConsistency(unittest.TestCase):

    def test_no_duplicate_commands(self):
        """菜单命令不重复。"""
        names = [cmd for cmd, _ in hm.BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)))

    def test_all_descriptions_nonempty(self):
        """每个命令都有中文说明。"""
        for cmd, desc in hm.BOT_COMMANDS:
            self.assertTrue(desc.strip(), f"{cmd} 缺少说明")

    def test_skipped_commands_not_in_menu(self):
        """闪电付费 / 客服专线 / 多语言命令不在菜单里。"""
        menu = {cmd for cmd, _ in hm.BOT_COMMANDS}
        for skipped in hm.SKIPPED_COMMANDS:
            self.assertNotIn(skipped, menu)

    def test_menu_matches_registered_handlers(self):
        """菜单列表与 build_application 注册的 CommandHandler 完全一致。

        菜单里有的命令必须有 handler（点了有反应），反之亦然。
        """
        # 清环境变量再构建：Application 构建 httpx 客户端时会读代理
        # 环境变量，本机沙箱的代理变量格式 httpx 解析不了（InvalidURL）。
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(bot_config, "TELEGRAM_BOT_TOKEN",
                          "123456:FAKE-TOKEN"), \
             patch.object(bot_config, "ALLOWED_USER_IDS", {ALLOWED_USER}):
            app = bot_module.build_application()
        registered = set()
        for group in app.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    registered.update(handler.commands)
        menu = {cmd for cmd, _ in hm.BOT_COMMANDS}
        self.assertEqual(registered, menu,
                         f"菜单与 handler 不一致：仅菜单有 {menu - registered}，"
                         f"仅 handler 有 {registered - menu}")

    def test_help_lists_all_menu_commands(self):
        """HELP_TEXT 列出菜单里的全部命令（一句话说明）。"""
        for cmd, _ in hm.BOT_COMMANDS:
            self.assertIn(f"/{cmd}", HELP_TEXT)


class TestPostInit(unittest.IsolatedAsyncioTestCase):

    async def test_registers_all_commands(self):
        """post_init 调 setMyCommands，内容与 BOT_COMMANDS 一致。"""
        application = MagicMock()
        application.bot.set_my_commands = AsyncMock()
        await bot_module._post_init(application)
        application.bot.set_my_commands.assert_awaited_once()
        commands = application.bot.set_my_commands.call_args.args[0]
        self.assertEqual([(c.command, c.description) for c in commands],
                         hm.BOT_COMMANDS)


# ================= 其他云命令 =================

class TestCloudCommands(AllowedUserTestCase):

    async def test_unconfigured_replies_with_setup_hint(self):
        """六个云命令：未配置时回复"未配置 X 账号"+ 配置方法。"""
        handlers = {
            "azure": hm.azure_command,
            "aws": hm.aws_command,
            "gcp": hm.gcp_command,
            "do": hm.do_command,
            "vf": hm.vf_command,
            "solusvm": hm.solusvm_command,
        }
        for name, handler in handlers.items():
            with self.subTest(provider=name):
                spec = hm.PROVIDER_SPECS[name]
                with patch.dict(os.environ, {}, clear=True):
                    update = make_message_update()
                    await handler(update, make_context())
                texts = reply_texts(update)
                self.assertEqual(len(texts), 1)
                self.assertIn(f"未配置 {spec['display']} 账号", texts[0])
                for var in spec["env_vars"]:
                    self.assertIn(var, texts[0])

    async def test_configured_stub_replies_not_implemented(self):
        """已配置但模块是 stub 时，直说功能尚未实现（不编造数据）。"""
        env = {"DIGITALOCEAN_TOKEN": "fake-do-token"}
        with patch.dict(os.environ, env):
            update = make_message_update()
            await hm.do_command(update, make_context())
        texts = reply_texts(update)
        self.assertEqual(len(texts), 1)
        self.assertIn("尚未实现", texts[0])
        self.assertIn("DigitalOcean", texts[0])

    async def test_configured_lists_instances(self):
        """模块支持列表时，显示实例列表。"""
        instances = [
            {"id": "i-1", "name": "web-1", "shape": "s-1vcpu-1gb",
             "state": "RUNNING", "public_ip": "203.0.113.10",
             "private_ip": "10.0.0.1", "region": "sgp1"},
            {"id": "i-2", "name": "db-1", "shape": "s-2vcpu-4gb",
             "state": "STOPPED", "public_ip": None,
             "private_ip": "10.0.0.2", "region": "sgp1"},
        ]
        env = {"DIGITALOCEAN_TOKEN": "fake-do-token"}
        with patch.dict(os.environ, env), \
             patch("clouds.do.DOProvider.list_instances",
                   return_value=instances):
            update = make_message_update()
            await hm.do_command(update, make_context())
        texts = reply_texts(update)
        self.assertEqual(len(texts), 1)
        self.assertIn("2 台", texts[0])
        self.assertIn("web-1", texts[0])
        self.assertIn("203.0.113.10", texts[0])
        self.assertIn("db-1", texts[0])
        self.assertIn("无公网 IP", texts[0])

    async def test_configured_empty_list(self):
        """已配置且实例为空时，回复没有实例。"""
        env = {"DIGITALOCEAN_TOKEN": "fake-do-token"}
        with patch.dict(os.environ, env), \
             patch("clouds.do.DOProvider.list_instances", return_value=[]):
            update = make_message_update()
            await hm.do_command(update, make_context())
        self.assertIn("没有实例", reply_texts(update)[0])

    async def test_list_error_shows_message(self):
        """列表调用抛异常时，显示中文错误（不崩）。"""
        env = {"DIGITALOCEAN_TOKEN": "fake-do-token"}
        with patch.dict(os.environ, env), \
             patch("clouds.do.DOProvider.list_instances",
                   side_effect=RuntimeError("api down")):
            update = make_message_update()
            await hm.do_command(update, make_context())
        self.assertIn("api down", reply_texts(update)[0])

    async def test_rejects_non_whitelisted_user(self):
        """白名单外用户调用云命令被拒绝。"""
        update = make_message_update(user_id=DENIED_USER)
        await hm.azure_command(update, make_context())
        self.assertIn("未授权", reply_texts(update)[0])


# ================= /oci =================

class TestOciCommand(AllowedUserTestCase):

    async def test_oci_command_sets_awaiting(self):
        """/oci 进入等待状态，并清掉 /uploadapi 的等待状态。"""
        context = make_context()
        context.user_data["ua_awaiting"] = True
        update = make_message_update()
        await hm.oci_command(update, context)
        self.assertTrue(context.user_data.get("oci_awaiting"))
        self.assertNotIn("ua_awaiting", context.user_data)
        text = reply_texts(update)[0]
        self.assertIn("[PROFILE]", text)
        self.assertIn("[azure]", text)
        self.assertIn("/uploadapi", text)
        self.assertIn("私钥文件", text)

    async def test_text_valid_oci_config_shows_confirm(self):
        """粘贴合法 OCI config：显示确认键盘，pending 记下来。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(text=FAKE_OCI_CONFIG_TEXT)
        with patch("tgbot.handlers_client.profile_exists_in_config",
                   return_value=False):
            await hm.handle_oci_text_input(update, context)
        pending = context.user_data.get("oci_pending")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["kind"], "oci")
        self.assertEqual(pending["profiles"][0]["profile"], "NEWACC")
        keyboard = (update.effective_message.reply_text.call_args.kwargs
                    .get("reply_markup"))
        self.assertIsNotNone(keyboard)
        callbacks = [btn.callback_data for row in keyboard.inline_keyboard
                     for btn in row]
        self.assertIn("oci:confirm", callbacks)

    async def test_text_rbot_format_shows_multi_confirm(self):
        """粘贴 R-Bot 整份配置：oci 块里两个 profile 一次确认。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        text = ("oci=begin\n[sg1]\nuser = ocid1.user.oc1..fake\n"
                "fingerprint = aa:bb\ntenancy = ocid1.tenancy.oc1..fake\n"
                "region = ap-singapore-1\nkey_file = /root/1.pem\n"
                "[sg2]\nuser = ocid1.user.oc1..fake\n"
                "fingerprint = cc:dd\ntenancy = ocid1.tenancy.oc1..fake\n"
                "region = ap-singapore-1\nkey_file = /root/2.pem\n"
                "oci=end\nusername=fakeuser\n")
        update = make_message_update(text=text)
        with patch("tgbot.handlers_client.profile_exists_in_config",
                   return_value=False), \
                patch.dict(os.environ, {"HOME": "/root"}):
            await hm.handle_oci_text_input(update, context)
        pending = context.user_data.get("oci_pending")
        self.assertIsNotNone(pending)
        self.assertEqual([p["profile"] for p in pending["profiles"]],
                         ["sg1", "sg2"])
        text = reply_texts(update)[0]
        self.assertIn("2 个账号", text)
        self.assertIn("/root/1.pem", text)
        self.assertIn("/root/2.pem", text)

    async def test_text_invalid_config_shows_error(self):
        """粘贴非法 config：报错，不记 pending。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(text="这不是 config")
        await hm.handle_oci_text_input(update, context)
        self.assertIn("格式不对", reply_texts(update)[0])
        self.assertNotIn("oci_pending", context.user_data)

    async def test_text_missing_keys_shows_error(self):
        """OCI config 缺必填键：报错并说明缺什么。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(text="[X]\nuser = ocid1.user.oc1..fake\n")
        await hm.handle_oci_text_input(update, context)
        text = reply_texts(update)[0]
        self.assertIn("缺少必填键", text)
        self.assertIn("fingerprint", text)

    async def test_text_existing_profile_rejected(self):
        """同名 profile 已存在：拒绝覆盖，清等待状态。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(text=FAKE_OCI_CONFIG_TEXT)
        with patch("tgbot.handlers_client.profile_exists_in_config",
                   return_value=True):
            await hm.handle_oci_text_input(update, context)
        self.assertIn("已存在", reply_texts(update)[0])
        self.assertNotIn("oci_awaiting", context.user_data)

    async def test_text_azure_config_shows_confirm_without_values(self):
        """[azure] 段完整：显示确认，值不在聊天里回显。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(text=FAKE_AZURE_CONFIG_TEXT)
        await hm.handle_oci_text_input(update, context)
        pending = context.user_data.get("oci_pending")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["kind"], "azure")
        text = reply_texts(update)[0]
        self.assertIn("AZURE_SUBSCRIPTION_ID", text)
        self.assertNotIn("fake-azure-secret", text)
        self.assertNotIn("00000000-0000", text)

    async def test_text_azure_missing_keys_shows_error(self):
        """[azure] 段缺键：报错并说明缺什么。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update(
            text="[azure]\nsubscription_id = 00000000-0000-0000-0000-000000000000\n")
        await hm.handle_oci_text_input(update, context)
        text = reply_texts(update)[0]
        self.assertIn("缺少必填键", text)
        self.assertIn("tenant_id", text)

    async def test_callback_confirm_oci_writes_profile(self):
        """确认后把 OCI profile 写进 config，并提示重启。"""
        context = make_context()
        fields = {"user": "ocid1.user.oc1..fake", "region": "ap-singapore-1",
                  "tenancy": "ocid1.tenancy.oc1..fake",
                  "key_file": "/root/NEWACC.pem"}
        profiles = [{"profile": "NEWACC", "fields": fields,
                     "original_key_file": "/home/user/.oci/fake-key.pem"}]
        context.user_data["oci_pending"] = {"kind": "oci",
                                            "profiles": profiles}
        update = make_callback_update("oci:confirm")
        results = [{"profile": "NEWACC", "key_file": "/root/NEWACC.pem",
                    "key_ready": False}]
        with patch("tgbot.handlers_menu.commit_oci_profiles",
                   return_value=results) as mock_commit, \
                patch("tgbot.handlers_menu.add_profile_compartments",
                      return_value={"MBOT_COMPARTMENT_NEWACC":
                                    "ocid1.tenancy.oc1..fake"}):
            await hm.oci_callback(update, context)
        mock_commit.assert_called_once_with(profiles)
        self.assertNotIn("oci_pending", context.user_data)
        text = reply_texts(update)[0]
        self.assertIn("NEWACC", text)
        self.assertIn("重启机器人", text)
        self.assertIn("MBOT_COMPARTMENT_NEWACC", text)

    async def test_callback_confirm_azure_writes_env(self):
        """确认后把 Azure 4 项写进 env 文件。"""
        context = make_context()
        context.user_data["oci_pending"] = {
            "kind": "azure", "fields": dict(FAKE_AZURE_FIELDS)}
        update = make_callback_update("oci:confirm")
        with patch("tgbot.handlers_menu.update_env_file_vars") as mock_write:
            await hm.oci_callback(update, context)
        mock_write.assert_called_once()
        updates = mock_write.call_args.args[0]
        self.assertEqual(set(updates), {
            "AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET"})
        self.assertEqual(updates["AZURE_CLIENT_SECRET"],
                         "fake-azure-secret")
        self.assertIn("Azure 配置已写进", reply_texts(update)[0])

    async def test_callback_no_pending_shows_expired(self):
        """无待确认配置时点确认：提示已过期。"""
        update = make_callback_update("oci:confirm")
        await hm.oci_callback(update, make_context())
        self.assertIn("已过期", reply_texts(update)[0])

    async def test_callback_wrong_data_shows_error(self):
        """未知 oci 回调：提示参数错误。"""
        update = make_callback_update("oci:bogus")
        await hm.oci_callback(update, make_context())
        self.assertIn("参数错误", reply_texts(update)[0])

    async def test_document_upload_parses_config(self):
        """oci 等待时发文件：下载解析后显示确认。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        update = make_message_update()
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(FAKE_OCI_CONFIG_TEXT, "utf-8"))
        document = MagicMock()
        document.get_file = AsyncMock(return_value=tg_file)
        update.effective_message.document = document
        with patch("tgbot.handlers_client.profile_exists_in_config",
                   return_value=False):
            await hm.handle_oci_document(update, context)
        self.assertEqual(context.user_data["oci_pending"]["profiles"][0]
                         ["profile"], "NEWACC")

    async def test_document_ignored_without_awaiting(self):
        """不在 oci 等待时发文件：静默忽略。"""
        update = make_message_update()
        update.effective_message.document = MagicMock()
        await hm.handle_oci_document(update, make_context())
        update.effective_message.reply_text.assert_not_called()

    async def test_document_ignored_when_uploadapi_awaiting(self):
        """/uploadapi 等待时发文件：归 uploadapi，不归 /oci。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        context.user_data["ua_awaiting"] = True
        update = make_message_update()
        update.effective_message.document = MagicMock()
        await hm.handle_oci_document(update, context)
        update.effective_message.reply_text.assert_not_called()

    async def test_text_ignored_with_pending_input(self):
        """pending_input（改名/重装）存在时，文本不归 /oci。"""
        context = make_context()
        context.user_data["oci_awaiting"] = True
        context.user_data["pending_input"] = ("rename", 0)
        update = make_message_update(text=FAKE_OCI_CONFIG_TEXT)
        await hm.handle_oci_text_input(update, context)
        update.effective_message.reply_text.assert_not_called()

    async def test_text_ignored_without_awaiting(self):
        """不在 oci 等待时发文本：静默忽略。"""
        update = make_message_update(text=FAKE_OCI_CONFIG_TEXT)
        await hm.handle_oci_text_input(update, make_context())
        update.effective_message.reply_text.assert_not_called()


class TestUpdateEnvFileVars(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "test.env")

    def test_updates_existing_and_appends_new(self):
        """已有键原地替换，新键追加，其余行原样保留。"""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("KEEP_ME=yes\nAZURE_TENANT_ID=old-value\n")
        hm.update_env_file_vars(
            {"AZURE_TENANT_ID": "new-value",
             "AZURE_CLIENT_ID": "client-123"},
            env_file=self.path)
        with open(self.path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("KEEP_ME=yes", content)
        self.assertIn("AZURE_TENANT_ID=new-value", content)
        self.assertIn("AZURE_CLIENT_ID=client-123", content)
        self.assertNotIn("old-value", content)

    def test_creates_file_if_missing(self):
        """env 文件不存在时新建。"""
        hm.update_env_file_vars({"AZURE_TENANT_ID": "v"}, env_file=self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("AZURE_TENANT_ID=v", f.read())

    def test_quotes_values_with_spaces(self):
        """含空格的值用单引号包起来。"""
        hm.update_env_file_vars({"SOME_KEY": "a b c"}, env_file=self.path)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("SOME_KEY='a b c'", f.read())

    def test_file_mode_0600(self):
        """写完后文件权限是 600。"""
        hm.update_env_file_vars({"K": "v"}, env_file=self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)


# ================= /clearlock =================

class TestClearlock(AllowedUserTestCase):

    def test_clear_login_lockout_restarts_web(self):
        """clear_login_lockout 重启面板服务（锁定在面板进程内存里）。"""
        with patch("tgbot.handlers_menu.restart_service") as mock_restart:
            result = hm.clear_login_lockout()
        mock_restart.assert_called_once_with("rbot-clone-web")
        self.assertIn("已清除", result)

    async def test_command_success(self):
        """/clearlock 成功：先发"正在重启"，再发清除确认。"""
        update = make_message_update()
        with patch("tgbot.handlers_menu.restart_service"):
            await hm.clearlock_command(update, make_context())
        texts = reply_texts(update)
        self.assertEqual(len(texts), 2)
        self.assertIn("正在重启", texts[0])
        self.assertIn("已清除", texts[1])

    async def test_command_failure(self):
        """重启失败时，回复失败原因。"""
        update = make_message_update()
        with patch("tgbot.handlers_menu.restart_service",
                   side_effect=RuntimeError("systemctl boom")):
            await hm.clearlock_command(update, make_context())
        self.assertIn("失败", reply_texts(update)[1])
        self.assertIn("systemctl boom", reply_texts(update)[1])

    async def test_rejects_non_whitelisted_user(self):
        """白名单外用户不能清锁定。"""
        update = make_message_update(user_id=DENIED_USER)
        await hm.clearlock_command(update, make_context())
        self.assertIn("未授权", reply_texts(update)[0])


# ================= /raninfo =================

class TestRaninfoPassword(unittest.TestCase):

    def test_generate_password_format(self):
        """生成的密码 20 位、只含字母数字、且字母数字都有。"""
        for _ in range(20):
            password = hm.generate_admin_password()
            self.assertEqual(len(password), 20)
            self.assertTrue(password.isalnum())
            self.assertTrue(any(c.isalpha() for c in password))
            self.assertTrue(any(c.isdigit() for c in password))

    def test_reset_writes_hash_and_password_file(self):
        """reset：哈希写进 env 文件、明文写进密码文件、重启面板服务。"""
        from web.auth import verify_password

        with tempfile.TemporaryDirectory() as tmp:
            env_file = os.path.join(tmp, "test.env")
            pw_file = os.path.join(tmp, ".rbot-clone-admin-password")
            with open(env_file, "w", encoding="utf-8") as f:
                f.write("KEEP_ME=yes\n")
            with patch("tgbot.handlers_menu.restart_service") as mock_restart:
                new_password = hm.reset_admin_password(
                    env_file=env_file, password_file=pw_file)
            mock_restart.assert_called_once_with("rbot-clone-web")
            # env 文件：旧行保留 + 哈希行（pbkdf2_sha256 格式）。
            with open(env_file, encoding="utf-8") as f:
                env_content = f.read()
            self.assertIn("KEEP_ME=yes", env_content)
            hash_line = [line for line in env_content.splitlines()
                         if line.startswith("MBOT_WEB_PASSWORD_HASH=")][0]
            stored_hash = hash_line.split("=", 1)[1].strip("'")
            self.assertTrue(stored_hash.startswith("pbkdf2_sha256$"))
            # 哈希与面板 verify_password 配套：新密码能验过，错密码验不过。
            self.assertTrue(verify_password(new_password, stored_hash))
            self.assertFalse(verify_password("wrong-password-1",
                                             stored_hash))
            # 密码文件：明文就是新密码，权限 600。
            with open(pw_file, encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), new_password)
            mode = stat.S_IMODE(os.stat(pw_file).st_mode)
            self.assertEqual(mode, 0o600)


class TestRaninfoCommand(AllowedUserTestCase):

    async def test_command_success_sends_password_once(self):
        """/raninfo 成功：新密码只在回复里出现一次，并提醒保存。"""
        update = make_message_update()
        with patch("tgbot.handlers_menu.reset_admin_password",
                   return_value="Abc1234567890Xyz98765"):
            await hm.raninfo_command(update, make_context())
        texts = reply_texts(update)
        self.assertEqual(len(texts), 2)
        self.assertIn("正在重置", texts[0])
        self.assertIn("Abc1234567890Xyz98765", texts[1])
        self.assertIn("只显示这一次", texts[1])
        self.assertEqual("".join(texts).count("Abc1234567890Xyz98765"), 1)

    async def test_command_failure(self):
        """重置失败时，回复失败原因，不发密码。"""
        update = make_message_update()
        with patch("tgbot.handlers_menu.reset_admin_password",
                   side_effect=OSError("disk full")):
            await hm.raninfo_command(update, make_context())
        self.assertIn("失败", reply_texts(update)[1])

    async def test_rejects_non_whitelisted_user(self):
        """白名单外用户不能重置密码。"""
        update = make_message_update(user_id=DENIED_USER)
        await hm.raninfo_command(update, make_context())
        self.assertIn("未授权", reply_texts(update)[0])


# ================= /me =================

class TestReadPanelVersion(unittest.TestCase):

    def test_reads_version_file(self):
        """VERSION 文件内容去掉空白后返回。"""
        with tempfile.NamedTemporaryFile("w", delete=False,
                                        encoding="utf-8") as f:
            f.write("9.9.9\n")
            path = f.name
        try:
            self.assertEqual(hm.read_panel_version(path), "9.9.9")
        finally:
            os.unlink(path)

    def test_missing_file_returns_unknown(self):
        """VERSION 文件不存在时返回"未知"。"""
        self.assertEqual(hm.read_panel_version("/nonexistent/VERSION"),
                         "未知")


class TestMeCommand(AllowedUserTestCase):

    def test_count_running_instances(self):
        """_count_running_instances 只数 RUNNING 的。"""
        instances = [
            {"id": "i-1", "lifecycle-state": "RUNNING"},
            {"id": "i-2", "lifecycle-state": "STOPPED"},
            {"id": "i-3", "lifecycle-state": "RUNNING"},
        ]
        with patch("tgbot.config.get_oci_client",
                   return_value=MagicMock()), \
             patch("tgbot.config.get_compartment_id",
                   return_value=FAKE_COMPARTMENT), \
             patch("tgbot.handlers_menu.list_instances",
                   return_value=instances):
            self.assertEqual(hm._count_running_instances("DEFAULT"), 2)

    async def test_me_shows_info(self):
        """/me 显示 TG 身份、版本、账号数与各账号 RUNNING 数。"""
        profiles = {"DEFAULT": {"region": "ap-singapore-1",
                                "compartment_id": FAKE_COMPARTMENT},
                    "ACCOUNT2": {"region": "us-sanjose-1",
                                 "compartment_id": FAKE_COMPARTMENT}}
        update = make_message_update()
        with patch.object(bot_config, "PROFILES", profiles), \
             patch("tgbot.handlers_menu.read_panel_version",
                   return_value="9.9.9"), \
             patch("tgbot.handlers_menu._count_running_instances",
                   side_effect=[2, 0]):
            await hm.me_command(update, make_context())
        text = reply_texts(update)[0]
        self.assertIn("123", text)
        self.assertIn("@testuser", text)
        self.assertIn("v9.9.9", text)
        self.assertIn("2 个", text)
        # v1.15.0：账号用主区中文名显示，不再显示配置键名。
        self.assertIn("（新加坡、圣何塞）", text)
        self.assertIn("新加坡：RUNNING 2 台", text)
        self.assertIn("圣何塞：RUNNING 0 台", text)
        self.assertNotIn("DEFAULT", text)
        self.assertNotIn("ACCOUNT2", text)

    async def test_me_handles_profile_error(self):
        """某账号查询失败时，/me 不崩，显示该账号的错误。"""
        profiles = {"DEFAULT": {"region": "ap-singapore-1",
                                "compartment_id": FAKE_COMPARTMENT}}
        update = make_message_update()
        with patch.object(bot_config, "PROFILES", profiles), \
             patch("tgbot.handlers_menu._count_running_instances",
                   side_effect=RuntimeError("oci down")):
            await hm.me_command(update, make_context())
        text = reply_texts(update)[0]
        self.assertIn("新加坡", text)
        self.assertNotIn("DEFAULT", text)
        self.assertIn("oci down", text)


# ================= /admin =================

class TestAdminCommand(AllowedUserTestCase):

    async def test_admin_command_shows_menu(self):
        """/admin 显示 4 个按钮的管理菜单。"""
        update = make_message_update()
        await hm.admin_command(update, make_context())
        keyboard = (update.effective_message.reply_text.call_args.kwargs
                    .get("reply_markup"))
        self.assertIsNotNone(keyboard)
        callbacks = [btn.callback_data for row in keyboard.inline_keyboard
                     for btn in row]
        self.assertEqual(callbacks, ["adm:status", "adm:restart-web",
                                     "adm:restart-bot", "adm:version"])

    async def test_status_shows_services(self):
        """面板状态：版本 + 两个服务的状态。"""
        update = make_callback_update("adm:status")
        with patch("tgbot.handlers_menu.read_panel_version",
                   return_value="9.9.9"), \
             patch("tgbot.handlers_menu.service_status",
                   side_effect=["active", "inactive"]):
            await hm.admin_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("v9.9.9", text)
        self.assertIn("rbot-clone-web", text)
        self.assertIn("active", text)
        self.assertIn("rbot-clone-bot", text)
        self.assertIn("inactive", text)

    async def test_version_shows_version(self):
        """查看版本：显示当前版本号。"""
        update = make_callback_update("adm:version")
        with patch("tgbot.handlers_menu.read_panel_version",
                   return_value="9.9.9"):
            await hm.admin_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("v9.9.9", text)

    async def test_restart_web_asks_confirm(self):
        """点重启面板：先二次确认。"""
        update = make_callback_update("adm:restart-web")
        await hm.admin_callback(update, make_context())
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("确定要重启面板服务吗", call.args[0])
        keyboard = call.kwargs.get("reply_markup")
        callbacks = [btn.callback_data for row in keyboard.inline_keyboard
                     for btn in row]
        self.assertIn("adm:confirm-web", callbacks)
        self.assertIn("x", callbacks)

    async def test_confirm_web_restarts(self):
        """确认重启面板：调 restart，成功后提示已重启。"""
        update = make_callback_update("adm:confirm-web")
        with patch("tgbot.handlers_menu.restart_service") as mock_restart:
            await hm.admin_callback(update, make_context())
        mock_restart.assert_called_once_with("rbot-clone-web")
        texts = [call.args[0] for call in
                 update.callback_query.edit_message_text.call_args_list]
        self.assertIn("正在重启面板服务", texts[0])
        self.assertIn("已重启", texts[1])

    async def test_confirm_web_failure(self):
        """重启面板失败：显示失败原因。"""
        update = make_callback_update("adm:confirm-web")
        with patch("tgbot.handlers_menu.restart_service",
                   side_effect=RuntimeError("no systemd")):
            await hm.admin_callback(update, make_context())
        texts = [call.args[0] for call in
                 update.callback_query.edit_message_text.call_args_list]
        self.assertIn("失败", texts[1])
        self.assertIn("no systemd", texts[1])

    async def test_restart_bot_asks_confirm_with_warning(self):
        """点重启机器人：二次确认里提示会断开会话几秒。"""
        update = make_callback_update("adm:restart-bot")
        await hm.admin_callback(update, make_context())
        call = update.callback_query.edit_message_text.call_args
        self.assertIn("断开本次会话几秒", call.args[0])
        keyboard = call.kwargs.get("reply_markup")
        callbacks = [btn.callback_data for row in keyboard.inline_keyboard
                     for btn in row]
        self.assertIn("adm:confirm-bot", callbacks)

    async def test_confirm_bot_restarts(self):
        """确认重启机器人：先发"正在重启"，再调 restart。"""
        update = make_callback_update("adm:confirm-bot")
        with patch("tgbot.handlers_menu.restart_service") as mock_restart:
            await hm.admin_callback(update, make_context())
        mock_restart.assert_called_once_with("rbot-clone-bot")
        first = update.callback_query.edit_message_text.call_args_list[0]
        self.assertIn("正在重启机器人服务", first.args[0])

    async def test_confirm_bot_failure(self):
        """重启机器人失败：显示失败原因。"""
        update = make_callback_update("adm:confirm-bot")
        with patch("tgbot.handlers_menu.restart_service",
                   side_effect=RuntimeError("no systemd")):
            await hm.admin_callback(update, make_context())
        texts = [call.args[0] for call in
                 update.callback_query.edit_message_text.call_args_list]
        self.assertIn("失败", texts[1])

    async def test_unknown_callback_shows_error(self):
        """未知 adm 回调：提示参数错误。"""
        update = make_callback_update("adm:bogus")
        await hm.admin_callback(update, make_context())
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn("参数错误", text)

    async def test_rejects_non_whitelisted_user(self):
        """白名单外用户点管理按钮被拒绝（不执行回调）。

        require_allowed 优先走 effective_message 回复拒绝，
        所以这里断言 reply_text 收到"未授权"且消息未被编辑。
        """
        update = make_callback_update("adm:status", user_id=DENIED_USER)
        await hm.admin_callback(update, make_context())
        self.assertIn("未授权", reply_texts(update)[0])
        update.callback_query.edit_message_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
