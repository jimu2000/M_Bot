#!/usr/bin/env python3
"""菜单 25 多账号费用 + 菜单 8 增加管理员账号测试（计划 v1.19.0）。

全部用 mock，不碰真实 OCI API。
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tgbot import config as bot_config
import tgbot.handlers_extra as he

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321
TEST_COMPARTMENT = "ocid1.tenancy.oc1..test"
FAKE_USER = "ocid1.user.oc1..xxxxxxxx"
FAKE_GROUP = "ocid1.group.oc1..bbbbbbbb"


def make_update(callback_data=None, text=None):
    """mock Update：reply_text / answer / edit_message_text 都是 AsyncMock。"""
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
        query.message = MagicMock()
        query.message.delete = AsyncMock()
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


def callback_datas(markup):
    """把 InlineKeyboardMarkup 里的 callback_data 全取出来。"""
    if markup is None:
        return []
    return [btn.callback_data
            for row in markup.inline_keyboard for btn in row]


def button_texts(markup):
    """把 InlineKeyboardMarkup 里的按钮文字全取出来。"""
    if markup is None:
        return []
    return [btn.text for row in markup.inline_keyboard for btn in row]


class CostAdminTestCase(unittest.IsolatedAsyncioTestCase):
    """基类：白名单 + mock cli/compartment。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self._cli = patch.object(he, "get_cli",
                                 return_value=MagicMock())
        self._cli.start()
        self._compartment = patch.object(he, "get_compartment",
                                         return_value=TEST_COMPARTMENT)
        self._compartment.start()

    def tearDown(self):
        self._allowed.stop()
        self._cli.stop()
        self._compartment.stop()


class TestFmtUsageTotals(unittest.TestCase):
    """fmt_usage_totals：多账号费用汇总格式。"""

    def test_multi_account_with_total(self):
        entries = [
            ("新加坡（3 个区）",
             [{"computed-amount": "13.46", "currency": "SGD"}]),
            ("圣何塞", []),
            ("芝加哥",
             [{"computed-amount": "0.50", "currency": "SGD"},
              {"computed-amount": "0.25", "currency": "SGD"}]),
        ]
        text = he.fmt_usage_totals(entries)
        self.assertIn("3 个账号", text)
        self.assertIn("新加坡（3 个区）：13.46 SGD", text)
        self.assertIn("圣何塞：0.00", text)
        self.assertIn("芝加哥：0.75 SGD", text)
        self.assertIn("合计：14.21 SGD", text)

    def test_failed_account_shown(self):
        entries = [("新加坡（3 个区）", None),
                   ("圣何塞", [{"computed-amount": "1.00",
                                "currency": "SGD"}])]
        text = he.fmt_usage_totals(entries)
        self.assertIn("新加坡（3 个区）：查询失败", text)
        self.assertIn("合计：1.00 SGD", text)

    def test_blank_currency_ignored(self):
        # An OCI zero-cost item's currency is sometimes a single space (seen in real data): it must not override SGD. OCI 的 0 元项币种有时是单个空格（真实数据实测）：不能覆盖 SGD。
        entries = [
            ("新加坡（3 个区）",
             [{"computed-amount": "13.46", "currency": "SGD"}]),
            ("圣何塞",
             [{"computed-amount": "0.0", "currency": " "},
              {"computed-amount": "32.49", "currency": "SGD"}]),
        ]
        text = he.fmt_usage_totals(entries)
        self.assertIn("圣何塞：32.49 SGD", text)
        self.assertIn("合计：45.95 SGD", text)

    def test_no_accounts(self):
        self.assertIn("没有配置任何账号", he.fmt_usage_totals([]))


class TestHandleCost(CostAdminTestCase):
    """菜单 25：默认一次查全部账号，单账号失败不影响其他。"""

    async def test_queries_all_profiles(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }
        usage = {
            "ocid1.tenancy.oc1..aaa": [
                {"computed-amount": "13.46", "currency": "SGD"}],
            "ocid1.tenancy.oc1..bbb": [],
        }

        def fake_fetch(cli, compartment_id, granularity):
            return usage[compartment_id]

        update = make_update()
        context = make_context()
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 3, "ACCOUNT2": 1}), \
                patch.object(bot_config, "get_oci_client",
                             return_value=MagicMock()), \
                patch.object(he, "fetch_usage_summary",
                             side_effect=fake_fetch):
            await he.handle_cost(update, context)
        text = reply_text_of(update)
        self.assertIn("2 个账号", text)
        self.assertIn("13.46 SGD", text)
        self.assertIn("合计：13.46 SGD", text)

    async def test_one_account_failure_tolerated(self):
        profiles = {
            "DEFAULT": {"region": "ap-singapore-1",
                        "compartment_id": "ocid1.tenancy.oc1..aaa"},
            "ACCOUNT2": {"region": "us-sanjose-1",
                         "compartment_id": "ocid1.tenancy.oc1..bbb"},
        }

        def fake_fetch(cli, compartment_id, granularity):
            if compartment_id.endswith("..aaa"):
                raise RuntimeError("usage api down")
            return [{"computed-amount": "2.00", "currency": "SGD"}]

        update = make_update()
        context = make_context()
        with patch.object(bot_config, "PROFILES", profiles), \
                patch.object(he, "get_profile_region_counts",
                             return_value={"DEFAULT": 3, "ACCOUNT2": 1}), \
                patch.object(bot_config, "get_oci_client",
                             return_value=MagicMock()), \
                patch.object(he, "fetch_usage_summary",
                             side_effect=fake_fetch):
            await he.handle_cost(update, context)
        text = reply_text_of(update)
        self.assertIn("查询失败", text)
        self.assertIn("合计：2.00 SGD", text)


class TestAddAdmin(CostAdminTestCase):
    """菜单 8 增加管理员账号：用户名 → 邮箱 → 确认 → 创建 + 加组。"""

    async def test_user_list_has_add_button(self):
        update = make_update()
        context = make_context()
        users = [{"id": FAKE_USER, "name": "api_user",
                  "email": "a@example.com", "lifecycle-state": "ACTIVE"}]
        with patch.object(he, "list_users", return_value=users):
            await he.handle_users(update, context)
        datas = callback_datas(reply_markup_of(update))
        self.assertIn("u:add", datas)
        self.assertIn("➕ 增加管理员账号",
                      button_texts(reply_markup_of(update)))

    async def test_add_prompts_for_name(self):
        update = make_update(callback_data="u:add")
        context = make_context()
        context.user_data["iam_users"] = []
        await he.user_callback(update, context)
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "add_admin_name")
        self.assertIn("用户名", reply_text_of(update))

    async def test_name_with_space_cancelled(self):
        update = make_update(text="bad name")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "add_admin_name"}
        await he.text_input_callback(update, context)
        self.assertIn("不能带空格", reply_text_of(update))
        self.assertNotIn("pending_input", context.user_data)

    async def test_name_ok_then_email_prompt(self):
        update = make_update(text="newadmin")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "add_admin_name"}
        await he.text_input_callback(update, context)
        self.assertEqual(context.user_data["add_admin"],
                         {"name": "newadmin"})
        self.assertEqual(context.user_data["pending_input"]["kind"],
                         "add_admin_email")
        self.assertIn("邮箱", reply_text_of(update))

    async def test_bad_email_cancelled(self):
        update = make_update(text="not-an-email")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "add_admin_email"}
        context.user_data["add_admin"] = {"name": "newadmin"}
        await he.text_input_callback(update, context)
        self.assertIn("邮箱格式不对", reply_text_of(update))

    async def test_email_ok_shows_confirm(self):
        update = make_update(text="newadmin@example.com")
        context = make_context()
        context.user_data["pending_input"] = {"kind": "add_admin_email"}
        context.user_data["add_admin"] = {"name": "newadmin"}
        await he.text_input_callback(update, context)
        text = reply_text_of(update)
        self.assertIn("newadmin", text)
        self.assertIn("newadmin@example.com", text)
        self.assertIn("Administrators", text)
        self.assertIn("c:addadmin:0",
                      callback_datas(reply_markup_of(update)))

    async def test_confirm_creates_and_adds_to_group(self):
        update = make_update(callback_data="c:addadmin:0")
        context = make_context()
        context.user_data["add_admin"] = {"name": "newadmin",
                                          "email": "newadmin@example.com"}
        with patch.object(he, "create_user",
                          return_value={"id": FAKE_USER,
                                        "name": "newadmin"}) as create, \
                patch.object(he, "get_group_id_by_name",
                             return_value=FAKE_GROUP) as find_group, \
                patch.object(he, "add_user_to_group") as add:
            handled = await he.handle_confirm("addadmin", "0",
                                              update, context)
        self.assertTrue(handled)
        create.assert_called_once()
        self.assertEqual(create.call_args.args[2], "newadmin")
        self.assertEqual(create.call_args.args[3], "newadmin@example.com")
        find_group.assert_called_once()
        self.assertEqual(find_group.call_args.args[2], "Administrators")
        add.assert_called_once()
        self.assertEqual(add.call_args.args[1], FAKE_USER)
        self.assertEqual(add.call_args.args[2], FAKE_GROUP)
        text = reply_text_of(update)
        self.assertIn("已创建", text)
        self.assertIn("Administrators", text)
        self.assertNotIn("add_admin", context.user_data)

    async def test_confirm_group_missing_warns(self):
        update = make_update(callback_data="c:addadmin:0")
        context = make_context()
        context.user_data["add_admin"] = {"name": "newadmin",
                                          "email": "newadmin@example.com"}
        with patch.object(he, "create_user",
                          return_value={"id": FAKE_USER}), \
                patch.object(he, "get_group_id_by_name",
                             return_value=None), \
                patch.object(he, "add_user_to_group") as add:
            await he.handle_confirm("addadmin", "0", update, context)
            add.assert_not_called()
        text = reply_text_of(update)
        self.assertIn("已创建", text)
        self.assertIn("找不到", text)

    async def test_confirm_expired(self):
        update = make_update(callback_data="c:addadmin:0")
        context = make_context()
        handled = await he.handle_confirm("addadmin", "0", update, context)
        self.assertTrue(handled)
        self.assertIn("已过期", reply_text_of(update))


if __name__ == "__main__":
    unittest.main()
