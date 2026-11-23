#!/usr/bin/env python3
"""工作包 D 的 Telegram handler 测试（tgbot.handlers_extra 新增部分）。

覆盖：对象存储（菜单 40）、Email 发件人（菜单 41）、抢机计划（菜单 42）、
开放云面板端口（菜单 11）、客户端管理（菜单 14）、重启机器人（菜单 31）、
升级客户端（菜单 32），以及 handle_confirm 的新增动作与 add_ingress_rule。

全部用 mock，不碰真实 OCI API，不真跑 systemctl / git / pip / ufw。
抢机计划用临时 DATA_DIR，不污染真实计划文件。

运行：python3 -m unittest tests.test_tgbot_handlers_extra_d -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tgbot import config as bot_config
from tgbot.services import TaskRegistry
import tgbot.handlers_extra as he
from web.tasks import PLANS

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"

FAKE_SECURITY_LIST = "ocid1.securitylist.oc1..testxxxx"
FAKE_BUCKETS = [
    {"name": "test-bucket", "time-created": "2026-11-20T00:00:00Z",
     "public-access-type": "NoPublicAccess", "storage-tier": "Standard"},
    {"name": "backup-bucket", "time-created": "2026-11-21T00:00:00Z",
     "public-access-type": "NoPublicAccess", "storage-tier": "Standard"},
]
FAKE_OBJECTS = [
    {"name": "file.txt", "size": 1024,
     "time-created": "2026-11-20T00:00:00Z"},
    {"name": "dir/nested.txt", "size": 2048,
     "time-created": "2026-11-21T00:00:00Z"},
]


def make_update(callback_data=None, text=None):
    """mock Update：effective_message.reply_text 与 callback_query.answer
    都是 AsyncMock；text 不为 None 时设成消息文本（文字输入测试用）。"""
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
    """mock Context：user_data / bot_data 用真实 dict。"""
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


def all_reply_texts(update):
    """取 update 上 reply_text 全部调用的文本列表。"""
    texts = []
    for args, _kwargs in update.effective_message.reply_text.await_args_list:
        texts.append(args[0] if args else None)
    return texts


def reply_markup_of(update):
    """取 update 上 reply_text 最后一次调用的 reply_markup。"""
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[-1]
    return kwargs.get("reply_markup")


def all_callback_data(markup):
    """从 InlineKeyboardMarkup 里取全部 callback_data。"""
    if markup is None:
        return []
    return [button.callback_data
            for row in markup.inline_keyboard for button in row]


class PackageDTestCase(unittest.IsolatedAsyncioTestCase):
    """公共 setUp：白名单放行、新 TASKS、mock cli、临时 DATA_DIR。"""

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
        self._data_dir_patch = patch("web.config.DATA_DIR", self._tmp.name)
        self._data_dir_patch.start()
        self.addCleanup(self._data_dir_patch.stop)
        PLANS.reset()

    def tmp_path(self, name):
        return os.path.join(self._tmp.name, name)


class TestDispatchD(PackageDTestCase):
    """工作包 D 的 7 个菜单项 dispatch 返回 True 且有回复。"""

    async def test_dispatch_new_items(self):
        with patch.object(he, "list_buckets", return_value=[]), \
             patch.object(he, "service_status", return_value="active"):
            for item in ("openport", "clients", "restart", "upgrade",
                         "objectstorage", "email", "plans"):
                with self.subTest(item=item):
                    update = make_update(callback_data=f"o:{item}")
                    context = make_context()
                    handled = await he.dispatch_oracle_action(
                        item, update, context)
                    self.assertTrue(handled, f"{item} 应返回 True")
                    self.assertIsNotNone(reply_text_of(update),
                                         f"{item} 应回复消息")

    async def test_openport_prompt_contains_warning(self):
        """开放端口菜单（已配置安全组时）提示二次确认 + 公网暴露警告。"""
        with patch.object(he, "PANEL_SECURITY_LIST_ID",
                          FAKE_SECURITY_LIST):
            update = make_update(callback_data="o:openport")
            context = make_context()
            await he.dispatch_oracle_action("openport", update, context)
            text = reply_text_of(update)
            self.assertIn("9527", text)
            self.assertIn("暴露到公网", text)
            self.assertIn("c:openport:0",
                          all_callback_data(reply_markup_of(update)))

    async def test_openport_unconfigured_prompts_config(self):
        """未配置安全组 OCID 时提示先配置，不给确认按钮。"""
        update = make_update(callback_data="o:openport")
        context = make_context()
        await he.dispatch_oracle_action("openport", update, context)
        text = reply_text_of(update)
        self.assertIn("未配置面板安全组", text)
        self.assertIn("PANEL_SECURITY_LIST_ID", text)
        self.assertNotIn("c:openport:0",
                         all_callback_data(reply_markup_of(update)))

    async def test_restart_prompt(self):
        update = make_update(callback_data="o:restart")
        context = make_context()
        await he.dispatch_oracle_action("restart", update, context)
        text = reply_text_of(update)
        self.assertIn("确认重启机器人服务", text)
        self.assertIn("c:restartbot:0",
                      all_callback_data(reply_markup_of(update)))

    async def test_upgrade_prompt(self):
        update = make_update(callback_data="o:upgrade")
        context = make_context()
        await he.dispatch_oracle_action("upgrade", update, context)
        text = reply_text_of(update)
        self.assertIn("确认升级客户端", text)
        self.assertIn("约 1 分钟不可用", text)
        self.assertIn("c:upgrade:0",
                      all_callback_data(reply_markup_of(update)))


class TestObjectStorageHandlers(PackageDTestCase):
    async def test_handle_objectstorage_lists_buckets(self):
        with patch.object(he, "list_buckets",
                          return_value=FAKE_BUCKETS) as mock_list:
            update = make_update()
            context = make_context()
            await he.handle_objectstorage(update, context)
            text = reply_text_of(update)
            self.assertIn("test-bucket", text)
            self.assertIn("backup-bucket", text)
            self.assertEqual(context.user_data["os_buckets"], FAKE_BUCKETS)
            mock_list.assert_called_once_with(
                self.cli, bot_config.DEFAULT_COMPARTMENT_ID)
            data = all_callback_data(reply_markup_of(update))
            self.assertIn("os:objects:0", data)
            self.assertIn("os:delbucket:1", data)

    async def test_handle_objectstorage_empty(self):
        with patch.object(he, "list_buckets", return_value=[]):
            update = make_update()
            context = make_context()
            await he.handle_objectstorage(update, context)
            self.assertIn("还没有 Bucket", reply_text_of(update))

    async def test_handle_objectstorage_error(self):
        with patch.object(he, "list_buckets",
                          side_effect=RuntimeError("boom")):
            update = make_update()
            context = make_context()
            await he.handle_objectstorage(update, context)
            self.assertIn("出错了", reply_text_of(update))

    async def test_objects_callback_lists_objects(self):
        with patch.object(he, "list_objects",
                          return_value=FAKE_OBJECTS) as mock_list:
            update = make_update(callback_data="os:objects:0")
            context = make_context()
            context.user_data["os_buckets"] = list(FAKE_BUCKETS)
            await he.objectstorage_callback(update, context)
            text = reply_text_of(update)
            self.assertIn("file.txt", text)
            self.assertIn("dir/nested.txt", text)
            self.assertEqual(context.user_data["os_objects"], FAKE_OBJECTS)
            mock_list.assert_called_once_with(self.cli, "test-bucket")
            data = all_callback_data(reply_markup_of(update))
            self.assertIn("os:delobj:0", data)
            self.assertIn("os:back", data)

    async def test_objects_callback_bucket_expired(self):
        update = make_update(callback_data="os:objects:0")
        context = make_context()
        await he.objectstorage_callback(update, context)
        self.assertIn("不存在或已过期", reply_text_of(update))

    async def test_delbucket_callback_prompts_confirm(self):
        update = make_update(callback_data="os:delbucket:1")
        context = make_context()
        context.user_data["os_buckets"] = list(FAKE_BUCKETS)
        await he.objectstorage_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("backup-bucket", text)
        self.assertIn("无法恢复", text)
        self.assertIn("c:delbucket:1",
                      all_callback_data(reply_markup_of(update)))

    async def test_delobj_callback_prompts_confirm(self):
        update = make_update(callback_data="os:delobj:0")
        context = make_context()
        context.user_data["os_buckets"] = list(FAKE_BUCKETS)
        context.user_data["os_objects"] = list(FAKE_OBJECTS)
        await he.objectstorage_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("file.txt", text)
        self.assertIn("无法恢复", text)
        self.assertIn("c:delobj:0",
                      all_callback_data(reply_markup_of(update)))

    async def test_back_callback_returns_to_buckets(self):
        with patch.object(he, "list_buckets",
                          return_value=FAKE_BUCKETS):
            update = make_update(callback_data="os:back")
            context = make_context()
            await he.objectstorage_callback(update, context)
            self.assertIn("test-bucket", reply_text_of(update))

    async def test_confirm_delbucket(self):
        with patch.object(he, "delete_bucket") as mock_delete:
            update = make_update(callback_data="c:delbucket:0")
            context = make_context()
            context.user_data["os_buckets"] = list(FAKE_BUCKETS)
            handled = await he.handle_confirm("delbucket", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_delete.assert_called_once_with(self.cli, "test-bucket")
            self.assertIn("已删除", reply_text_of(update))

    async def test_confirm_delbucket_expired(self):
        with patch.object(he, "delete_bucket") as mock_delete:
            update = make_update()
            context = make_context()
            handled = await he.handle_confirm("delbucket", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_delete.assert_not_called()
            self.assertIn("不存在或已过期", reply_text_of(update))

    async def test_confirm_delobj(self):
        with patch.object(he, "delete_object") as mock_delete:
            update = make_update(callback_data="c:delobj:1")
            context = make_context()
            context.user_data["os_buckets"] = list(FAKE_BUCKETS)
            context.user_data["os_objects"] = list(FAKE_OBJECTS)
            context.user_data["os_bucket_idx"] = "0"
            handled = await he.handle_confirm("delobj", "1",
                                              update, context)
            self.assertTrue(handled)
            mock_delete.assert_called_once_with(self.cli, "test-bucket",
                                                "dir/nested.txt")
            self.assertIn("已删除", reply_text_of(update))

    async def test_confirm_delobj_expired(self):
        with patch.object(he, "delete_object") as mock_delete:
            update = make_update()
            context = make_context()
            handled = await he.handle_confirm("delobj", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_delete.assert_not_called()

    def test_fmt_bucket_list_pure(self):
        self.assertIn("还没有 Bucket", he.fmt_bucket_list([]))
        text = he.fmt_bucket_list(FAKE_BUCKETS)
        self.assertIn("2 个", text)
        self.assertIn("test-bucket", text)

    def test_fmt_object_list_pure(self):
        self.assertIn("没有对象", he.fmt_object_list("b", []))
        text = he.fmt_object_list("b", FAKE_OBJECTS)
        self.assertIn("2 个", text)
        self.assertIn("3072 字节", text)


class TestEmailHandlers(PackageDTestCase):
    async def test_handle_email_prompts_input(self):
        update = make_update()
        context = make_context()
        await he.handle_email(update, context)
        text = reply_text_of(update)
        self.assertIn("发送发件人邮箱地址", text)
        self.assertIn("验证链接", text)
        self.assertEqual(context.user_data["pending_input"],
                         {"kind": "email_sender"})

    async def test_text_input_valid_email_prompts_confirm(self):
        update = make_update(text="noreply@example.com")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "email_sender"}
        await he.text_input_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认创建发件人 noreply@example.com", text)
        self.assertNotIn("pending_input", context.user_data)
        self.assertEqual(context.user_data["pending_sender"],
                         "noreply@example.com")
        self.assertIn("c:createsender:0",
                      all_callback_data(reply_markup_of(update)))

    async def test_text_input_invalid_email_rejected(self):
        update = make_update(text="not-an-email")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "email_sender"}
        await he.text_input_callback(update, context)
        self.assertIn("邮箱格式不对", reply_text_of(update))
        self.assertNotIn("pending_sender", context.user_data)
        # pending_input 已消费，下条文字不再当邮箱处理。
        self.assertNotIn("pending_input", context.user_data)

    async def test_text_input_no_pending_ignored(self):
        update = make_update(text="hello")
        context = make_context()
        await he.text_input_callback(update, context)
        update.effective_message.reply_text.assert_not_awaited()

    async def test_confirm_createsender(self):
        with patch.object(he, "create_sender") as mock_create:
            update = make_update(callback_data="c:createsender:0")
            context = make_context()
            context.user_data["pending_sender"] = "noreply@example.com"
            handled = await he.handle_confirm("createsender", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_create.assert_called_once_with(
                self.cli, bot_config.DEFAULT_COMPARTMENT_ID,
                "noreply@example.com")
            self.assertIn("已创建", reply_text_of(update))
            self.assertIn("验证链接", reply_text_of(update))
            self.assertNotIn("pending_sender", context.user_data)

    async def test_confirm_createsender_expired(self):
        with patch.object(he, "create_sender") as mock_create:
            update = make_update()
            context = make_context()
            handled = await he.handle_confirm("createsender", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_create.assert_not_called()
            self.assertIn("已过期", reply_text_of(update))


class TestPlanHandlers(PackageDTestCase):
    def _add_plan(self, cron="0 2 * * *", shape="VM.Standard.A1.Flex",
                  ocpus=1, memory_gb=6, enabled=True):
        return PLANS.add(cron, shape, ocpus, memory_gb, enabled=enabled)

    async def test_handle_plans_lists(self):
        self._add_plan()
        self._add_plan(cron="0 3 * * *", shape="VM.Standard.E2.1.Micro",
                       ocpus=1, memory_gb=1, enabled=False)
        update = make_update()
        context = make_context()
        await he.handle_plans(update, context)
        text = reply_text_of(update)
        self.assertIn("p1", text)
        self.assertIn("0 2 * * *", text)
        self.assertIn("A1.Flex 1 核 / 6GB", text)
        self.assertIn("已启用", text)
        self.assertIn("已停用", text)
        data = all_callback_data(reply_markup_of(update))
        self.assertIn("pl:toggle:p1", data)
        self.assertIn("pl:delete:p2", data)

    async def test_handle_plans_empty(self):
        update = make_update()
        context = make_context()
        await he.handle_plans(update, context)
        self.assertIn("还没有抢机计划", reply_text_of(update))

    async def test_plan_toggle(self):
        plan = self._add_plan()
        update = make_update(callback_data="pl:toggle:p1")
        context = make_context()
        await he.plan_callback(update, context)
        texts = all_reply_texts(update)
        self.assertTrue(any("已停用" in t for t in texts))
        self.assertFalse(PLANS.get(plan["id"])["enabled"])
        # 再点一次恢复启用。
        update2 = make_update(callback_data="pl:toggle:p1")
        await he.plan_callback(update2, make_context())
        self.assertTrue(PLANS.get(plan["id"])["enabled"])

    async def test_plan_delete_prompts_confirm(self):
        self._add_plan()
        update = make_update(callback_data="pl:delete:p1")
        context = make_context()
        await he.plan_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认删除抢机计划 p1", text)
        self.assertIn("c:delplan:p1",
                      all_callback_data(reply_markup_of(update)))
        # 只提示，不删除。
        self.assertIsNotNone(PLANS.get("p1"))

    async def test_plan_callback_missing(self):
        update = make_update(callback_data="pl:toggle:p999")
        context = make_context()
        await he.plan_callback(update, context)
        self.assertIn("不存在", reply_text_of(update))

    async def test_confirm_delplan(self):
        self._add_plan()
        update = make_update(callback_data="c:delplan:p1")
        context = make_context()
        handled = await he.handle_confirm("delplan", "p1", update, context)
        self.assertTrue(handled)
        self.assertIn("已删除", reply_text_of(update))
        self.assertIsNone(PLANS.get("p1"))

    async def test_confirm_delplan_missing(self):
        update = make_update()
        context = make_context()
        handled = await he.handle_confirm("delplan", "p999", update, context)
        self.assertTrue(handled)
        self.assertIn("不存在", reply_text_of(update))

    def test_fmt_plan_list_pure(self):
        self.assertIn("还没有抢机计划", he.fmt_plan_list([]))
        text = he.fmt_plan_list([{
            "id": "p1", "cron": "0 2 * * *",
            "shape": "VM.Standard.A1.Flex", "ocpus": 4, "memory_gb": 24,
            "enabled": True}])
        self.assertIn("p1", text)
        self.assertIn("A1.Flex 4 核 / 24GB", text)


class TestAddIngressRule(unittest.TestCase):
    """add_ingress_rule 是纯同步函数，直接测 cli.run 调用。"""

    def _mock_cli(self, rules):
        cli = MagicMock()
        cli.run.side_effect = [
            {"data": {"ingress-security-rules": rules}},
            {"data": {}},
        ]
        return cli

    def test_rule_already_present_returns_false(self):
        existing = [{
            "source": "0.0.0.0/0", "protocol": "6",
            "tcp-options": {"destination-port-range": {"min": 9527,
                                                       "max": 9527}},
        }]
        cli = self._mock_cli(existing)
        self.assertFalse(he.add_ingress_rule(cli, FAKE_SECURITY_LIST, 9527))
        # 只查了一次 get，没调 update。
        self.assertEqual(cli.run.call_count, 1)

    def test_new_rule_appended_with_force(self):
        existing = [{
            "source": "0.0.0.0/0", "protocol": "6",
            "tcp-options": {"destination-port-range": {"min": 443,
                                                       "max": 443}},
        }]
        cli = self._mock_cli(existing)
        self.assertTrue(he.add_ingress_rule(cli, FAKE_SECURITY_LIST, 9527))
        self.assertEqual(cli.run.call_count, 2)
        update_args = cli.run.call_args_list[1][0][0]
        self.assertEqual(update_args[:3],
                         ["network", "security-list", "update"])
        self.assertIn("--force", update_args)
        # JSON 里既有旧规则（443）也有新规则（9527）。
        payload = json.loads(
            update_args[update_args.index("--ingress-security-rules") + 1])
        self.assertEqual(len(payload), 2)
        new_rule = payload[1]
        self.assertEqual(new_rule["source"], "0.0.0.0/0")
        self.assertEqual(new_rule["protocol"], "6")
        self.assertEqual(new_rule["tcp-options"]["destination-port-range"],
                         {"min": 9527, "max": 9527})

    def test_string_protocol_and_ports_match(self):
        """OCI 返回的 protocol/端口可能是字符串，要能识别为已存在。"""
        existing = [{
            "source": "0.0.0.0/0", "protocol": "6",
            "tcp-options": {"destination-port-range": {"min": "9527",
                                                       "max": "9527"}},
        }]
        cli = self._mock_cli(existing)
        self.assertFalse(he.add_ingress_rule(cli, FAKE_SECURITY_LIST, 9527))

    def test_different_source_does_not_match(self):
        existing = [{
            "source": "203.0.113.0/24", "protocol": "6",
            "tcp-options": {"destination-port-range": {"min": 9527,
                                                       "max": 9527}},
        }]
        cli = self._mock_cli(existing)
        self.assertTrue(he.add_ingress_rule(cli, FAKE_SECURITY_LIST, 9527))


class TestOpenportConfirm(PackageDTestCase):
    async def test_confirm_openport_new_rule(self):
        with patch.object(he, "PANEL_SECURITY_LIST_ID",
                          FAKE_SECURITY_LIST), \
             patch.object(he, "add_ingress_rule",
                          return_value=True) as mock_rule, \
             patch.object(he, "ufw_allow_port") as mock_ufw:
            update = make_update(callback_data="c:openport:0")
            context = make_context()
            handled = await he.handle_confirm("openport", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_rule.assert_called_once_with(self.cli, FAKE_SECURITY_LIST,
                                              9527)
            mock_ufw.assert_called_once_with(9527, None)
            text = reply_text_of(update)
            self.assertIn("已放行", text)
            self.assertIn("已新增放行规则", text)
            self.assertIn("暴露到公网", text)
            self.assertIn("请确保密码足够强", text)

    async def test_confirm_openport_rule_exists(self):
        with patch.object(he, "PANEL_SECURITY_LIST_ID",
                          FAKE_SECURITY_LIST), \
             patch.object(he, "add_ingress_rule", return_value=False), \
             patch.object(he, "ufw_allow_port"):
            update = make_update()
            context = make_context()
            await he.handle_confirm("openport", "0", update, context)
            self.assertIn("放行规则已存在", reply_text_of(update))

    async def test_confirm_openport_remote_ufw(self):
        """PANEL_SSH_HOST 不为空时 UFW 走 ssh 到该主机。"""
        with patch.object(he, "PANEL_SECURITY_LIST_ID",
                          FAKE_SECURITY_LIST), \
             patch.object(he, "PANEL_SSH_HOST", "203.0.113.10"), \
             patch.object(he, "add_ingress_rule", return_value=True), \
             patch.object(he, "ufw_allow_port") as mock_ufw:
            update = make_update()
            context = make_context()
            await he.handle_confirm("openport", "0", update, context)
            mock_ufw.assert_called_once_with(9527, "203.0.113.10")

    async def test_confirm_openport_unconfigured(self):
        with patch.object(he, "add_ingress_rule") as mock_rule, \
             patch.object(he, "ufw_allow_port") as mock_ufw:
            update = make_update()
            context = make_context()
            handled = await he.handle_confirm("openport", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_rule.assert_not_called()
            mock_ufw.assert_not_called()
            self.assertIn("未配置面板安全组", reply_text_of(update))

    async def test_confirm_openport_failure(self):
        with patch.object(he, "PANEL_SECURITY_LIST_ID",
                          FAKE_SECURITY_LIST), \
             patch.object(he, "add_ingress_rule",
                          side_effect=RuntimeError("oci down")), \
             patch.object(he, "ufw_allow_port") as mock_ufw:
            update = make_update()
            context = make_context()
            await he.handle_confirm("openport", "0", update, context)
            self.assertIn("出错了", reply_text_of(update))
            mock_ufw.assert_not_called()


class TestClientsHandlers(PackageDTestCase):
    async def test_handle_clients_shows_status(self):
        def fake_status(service):
            return {"rbot-clone-web": "active",
                    "rbot-clone-bot": "inactive"}[service]

        with patch.object(he, "service_status",
                          side_effect=fake_status):
            update = make_update()
            context = make_context()
            await he.handle_clients(update, context)
            text = reply_text_of(update)
            self.assertIn("rbot-clone-web：active", text)
            self.assertIn("rbot-clone-bot：inactive", text)
            data = all_callback_data(reply_markup_of(update))
            self.assertIn("cl:restart:0", data)
            self.assertIn("cl:restart:1", data)

    async def test_client_callback_prompts_confirm(self):
        update = make_update(callback_data="cl:restart:1")
        context = make_context()
        await he.client_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("确认重启服务 rbot-clone-bot", text)
        self.assertIn("c:restartsvc:1",
                      all_callback_data(reply_markup_of(update)))

    async def test_client_callback_invalid_idx(self):
        update = make_update(callback_data="cl:restart:9")
        context = make_context()
        await he.client_callback(update, context)
        self.assertIn("不存在", reply_text_of(update))

    async def test_confirm_restartsvc_web(self):
        """重启面板：先回“正在重启”，执行后回“已重启”。"""
        with patch.object(he, "restart_service") as mock_restart:
            update = make_update(callback_data="c:restartsvc:0")
            context = make_context()
            handled = await he.handle_confirm("restartsvc", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_restart.assert_called_once_with("rbot-clone-web")
            texts = all_reply_texts(update)
            self.assertTrue(any("正在重启" in t for t in texts))
            self.assertTrue(any("已重启" in t for t in texts))

    async def test_confirm_restartsvc_bot(self):
        """重启机器人：只回“正在重启”（进程将被杀掉，不发第二条）。"""
        with patch.object(he, "restart_service") as mock_restart:
            update = make_update(callback_data="c:restartsvc:1")
            context = make_context()
            handled = await he.handle_confirm("restartsvc", "1",
                                              update, context)
            self.assertTrue(handled)
            mock_restart.assert_called_once_with("rbot-clone-bot")
            texts = all_reply_texts(update)
            self.assertEqual(len(texts), 1)
            self.assertIn("正在重启", texts[0])

    async def test_confirm_restartsvc_failure(self):
        with patch.object(he, "restart_service",
                          side_effect=RuntimeError("systemctl failed")):
            update = make_update()
            context = make_context()
            await he.handle_confirm("restartsvc", "0", update, context)
            texts = all_reply_texts(update)
            self.assertTrue(any("正在重启" in t for t in texts))
            self.assertTrue(any("失败" in t for t in texts))

    async def test_confirm_restartsvc_invalid_idx(self):
        with patch.object(he, "restart_service") as mock_restart:
            update = make_update()
            context = make_context()
            handled = await he.handle_confirm("restartsvc", "9",
                                              update, context)
            self.assertTrue(handled)
            mock_restart.assert_not_called()

    def test_fmt_client_status_pure(self):
        text = he.fmt_client_status({"rbot-clone-web": "active",
                                     "rbot-clone-bot": "unknown"})
        self.assertIn("rbot-clone-web：active", text)
        self.assertIn("rbot-clone-bot：unknown", text)


class TestRestartBot(PackageDTestCase):
    async def test_confirm_restartbot(self):
        """重启机器人：先回“正在重启”再执行重启（mock，不真重启）。"""
        with patch.object(he, "restart_service") as mock_restart:
            update = make_update(callback_data="c:restartbot:0")
            context = make_context()
            handled = await he.handle_confirm("restartbot", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_restart.assert_called_once_with("rbot-clone-bot")
            texts = all_reply_texts(update)
            self.assertEqual(len(texts), 1)
            self.assertIn("正在重启", texts[0])
            self.assertIn("rbot-clone-bot", texts[0])


class TestUpgradeHandlers(PackageDTestCase):
    async def test_confirm_upgrade_not_git_repo(self):
        """不是 git 仓库时提示手动升级，不执行升级。"""
        with patch.object(he, "is_git_repo", return_value=False), \
             patch.object(he, "upgrade_client") as mock_upgrade:
            update = make_update(callback_data="c:upgrade:0")
            context = make_context()
            handled = await he.handle_confirm("upgrade", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_upgrade.assert_not_called()
            text = reply_text_of(update)
            self.assertIn("不是 git 仓库", text)
            self.assertIn("手动升级", text)

    async def test_confirm_upgrade_git_repo(self):
        """是 git 仓库时先回“开始升级”再执行。"""
        with patch.object(he, "is_git_repo", return_value=True), \
             patch.object(he, "upgrade_client",
                          return_value="Already up to date.") as mock_upgrade:
            update = make_update(callback_data="c:upgrade:0")
            context = make_context()
            handled = await he.handle_confirm("upgrade", "0",
                                              update, context)
            self.assertTrue(handled)
            mock_upgrade.assert_called_once_with(he.PROJECT_ROOT)
            texts = all_reply_texts(update)
            self.assertTrue(any("开始升级" in t for t in texts))
            self.assertTrue(any("约 1 分钟不可用" in t for t in texts))

    async def test_confirm_upgrade_failure(self):
        with patch.object(he, "is_git_repo", return_value=True), \
             patch.object(he, "upgrade_client",
                          side_effect=RuntimeError("git pull failed")):
            update = make_update()
            context = make_context()
            await he.handle_confirm("upgrade", "0", update, context)
            texts = all_reply_texts(update)
            self.assertTrue(any("开始升级" in t for t in texts))
            self.assertTrue(any("出错了" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
