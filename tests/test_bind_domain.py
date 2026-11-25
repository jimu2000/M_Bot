#!/usr/bin/env python3
"""实例详情“绑定域名”（Bot + Web 双端，v2.01）的单元测试。

全部用 mock，不碰真实 Cloudflare API 与 OCI。

约定：
  - Bot 侧 mock Update/Context：reply_text / callback_query.answer 用
    AsyncMock；user_data / bot_data 用真实 dict；白名单临时设为
    {TEST_USER_ID}（与 test_tgbot_domain.py 一致）。
  - Bot 侧 Cloudflare 客户端 patch tgbot.bind_domain 命名空间里的
    _get_cf_client。
  - Web 侧用 TestClient + 登录；patch web.routes.bind_domain 命名空间
    里的 get_public_ip / resolve_profile / get_configured_cf_client。
  - 测试数据只用文档保留值（example.com、203.0.113.x、假 zone id），
    绝不出现真实 IP/OCID/Token。

运行：python3 -m unittest tests.test_bind_domain -v
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tgbot import bind_domain as bd
from tgbot import config as bot_config
from tgbot.bind_domain import CloudflareError
from tgbot.keyboards import instance_actions_keyboard
from tests.test_web_dns import TEST_PASSWORD
from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes import bind_domain as web_bd

TEST_USER_ID = 123456
TEST_CHAT_ID = 654321

FAKE_ZONE_ID = "zoneid000000000000000000000001"
FAKE_ZONE_ID_2 = "zoneid000000000000000000000002"
FAKE_INSTANCE_ID = "ocid1.instance.oc1.test000000000000000000000001"
FAKE_IP = "203.0.113.10"
FAKE_OLD_IP = "203.0.113.99"

FAKE_ZONES = [
    {"id": FAKE_ZONE_ID, "name": "example.com", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net"]},
    {"id": FAKE_ZONE_ID_2, "name": "example.org", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net"]},
]


def _cf_error(kind):
    err = CloudflareError("mock cloudflare failure")
    err.kind = kind
    return err


class FakeCF:
    """内存版 Cloudflare 客户端：Zone 与 A 记录存在内存里。"""

    def __init__(self, zones=None, records=None):
        self.zones = list(zones if zones is not None else FAKE_ZONES)
        self.records = list(records or [])
        self.created = []
        self.updated = []

    def list_zones(self):
        return self.zones

    def get_zone(self, zone_id):
        for zone in self.zones:
            if zone.get("id") == zone_id:
                return zone
        raise _cf_error("not_found")

    def list_dns_records(self, zone_id, type=None, name=None):
        return [r for r in self.records
                if (type is None or r.get("type") == type)
                and (name is None or r.get("name") == name)]

    def create_dns_record(self, zone_id, type, name, content,
                          proxied=False, ttl=1):
        record = {"id": "recid000000000000000000000100", "type": type,
                  "name": name, "content": content, "proxied": proxied,
                  "ttl": ttl, "zone_id": zone_id, "zone_name": "example.com"}
        self.records.append(record)
        self.created.append(record)
        return record

    def update_dns_record(self, zone_id, record_id, type, name, content,
                          proxied=False, ttl=1):
        for record in self.records:
            if record.get("id") == record_id:
                record.update({"type": type, "name": name,
                               "content": content})
                self.updated.append(record)
                return record
        raise AssertionError("record not found")


def _existing_record():
    return {"id": "recid000000000000000000000001", "type": "A",
            "name": "www.example.com", "content": FAKE_OLD_IP,
            "proxied": False, "ttl": 1,
            "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"}


# ================= 纯逻辑 =================

class TestValidateSubdomain(unittest.TestCase):
    """二级域名校验：只允许字母数字、-、_、.，或单独一个 @。"""

    def test_valid(self):
        for text, expected in [
                ("www", "www"), ("app", "app"), ("@", "@"),
                ("WWW", "www"), ("  api  ", "api"),
                ("a-b_c.d", "a-b_c.d"), ("www.", "www")]:
            ok, normalized = bd.validate_subdomain(text)
            self.assertTrue(ok, text)
            self.assertEqual(normalized, expected)

    def test_invalid(self):
        for text in ["", "   ", "a b", "a/b", "a$b", "-a", "a-", ".a",
                     "a..b", "a:b", "a?b", "é"]:
            ok, _normalized = bd.validate_subdomain(text)
            self.assertFalse(ok, text)


class TestBuildFqdn(unittest.TestCase):
    """拼完整域名：@ 用 Zone 名本身。"""

    def test_subdomain(self):
        self.assertEqual(bd.build_fqdn("www", "example.com"),
                         "www.example.com")

    def test_root(self):
        self.assertEqual(bd.build_fqdn("@", "example.com"), "example.com")


class TestBindARecord(unittest.TestCase):
    """A 记录创建调用：新建 / 已同指 / 冲突 / 覆盖。"""

    def test_create(self):
        cf = FakeCF()
        action, record = bd.bind_a_record(cf, FAKE_ZONE_ID,
                                          "www.example.com", FAKE_IP)
        self.assertEqual(action, "created")
        self.assertEqual(record["content"], FAKE_IP)
        self.assertEqual(len(cf.created), 1)

    def test_exists_same(self):
        cf = FakeCF(records=[_existing_record()])
        action, record = bd.bind_a_record(cf, FAKE_ZONE_ID,
                                          "www.example.com", FAKE_OLD_IP)
        self.assertEqual(action, "exists_same")
        self.assertEqual(len(cf.created), 0)
        self.assertEqual(len(cf.updated), 0)

    def test_exists_conflict_without_overwrite(self):
        cf = FakeCF(records=[_existing_record()])
        action, record = bd.bind_a_record(cf, FAKE_ZONE_ID,
                                          "www.example.com", FAKE_IP)
        self.assertEqual(action, "exists_conflict")
        self.assertEqual(record["content"], FAKE_OLD_IP)
        self.assertEqual(len(cf.created), 0)
        self.assertEqual(len(cf.updated), 0)

    def test_exists_conflict_with_overwrite(self):
        cf = FakeCF(records=[_existing_record()])
        action, record = bd.bind_a_record(cf, FAKE_ZONE_ID,
                                          "www.example.com", FAKE_IP,
                                          overwrite=True)
        self.assertEqual(action, "updated")
        self.assertEqual(record["content"], FAKE_IP)
        self.assertEqual(len(cf.updated), 1)


# ================= Bot 端 =================

def make_update(callback_data=None, text=None, user_id=TEST_USER_ID):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = TEST_CHAT_ID
    update.effective_message = MagicMock()
    update.effective_message.reply_text = AsyncMock()
    update.effective_message.text = text
    if callback_data is not None:
        query = MagicMock()
        query.data = callback_data
        query.answer = AsyncMock()
        update.callback_query = query
    else:
        update.callback_query = None
    return update


def make_context(instances=None):
    context = MagicMock()
    context.user_data = {}
    if instances is not None:
        context.user_data["instances"] = instances
    context.bot_data = {}
    context.bot = MagicMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context


def reply_text_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    args, _kwargs = mock.await_args_list[-1]
    return args[0] if args else None


def reply_markup_of(update):
    mock = update.effective_message.reply_text
    if not mock.await_args_list:
        return None
    _args, kwargs = mock.await_args_list[-1]
    return kwargs.get("reply_markup")


def _callback_datas(markup):
    datas = []
    for row in (markup.inline_keyboard if markup else []):
        for button in row:
            datas.append(button.callback_data)
    return datas


def _fake_instance(with_ip=True):
    inst = {"id": FAKE_INSTANCE_ID, "display-name": "AMD_2026-11-25",
            "lifecycle-state": "RUNNING", "shape": "VM.Standard.E2.1.Micro"}
    if with_ip:
        inst["public-ip"] = FAKE_IP
    return inst


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    """Bot 测试基类：白名单 + 假 CF 客户端。"""

    def setUp(self):
        self._allowed = patch.object(bot_config, "ALLOWED_USER_IDS",
                                     {TEST_USER_ID})
        self._allowed.start()
        self.addCleanup(self._allowed.stop)
        self.cf = FakeCF()
        self._cf_patch = patch.object(bd, "_get_cf_client",
                                      return_value=self.cf)
        self._cf_patch.start()
        self.addCleanup(self._cf_patch.stop)

    def _start_flow(self):
        """走完“开始 → 选第一个 Zone”，返回 (update, context)。"""
        context = make_context([_fake_instance()])
        update = make_update(callback_data="bd:start:0")
        return update, context

    async def _run_start_and_zone(self, context=None):
        update, context = self._start_flow() if context is None \
            else (make_update(callback_data="bd:start:0"), context)
        await bd.bind_domain_callback(update, context)
        zone_update = make_update(callback_data="bd:zone:0:0")
        zone_update.effective_message = update.effective_message
        await bd.bind_domain_callback(zone_update, context)
        return context


class TestBindDomainKeyboard(unittest.TestCase):
    """实例操作键盘里有“绑定域名”按钮。"""

    def test_action_keyboard_has_bind_domain(self):
        markup = instance_actions_keyboard(0, "RUNNING", lang="zh")
        self.assertIn("bd:start:0", _callback_datas(markup))


class TestBindDomainStart(BotTestCase):
    """入口：无公网 IP 提示 / 列 Zone / 未配凭据提示。"""

    async def test_no_public_ip(self):
        context = make_context([_fake_instance(with_ip=False)])
        update = make_update(callback_data="bd:start:0")
        await bd.bind_domain_callback(update, context)
        self.assertIn("没有公网 IP", reply_text_of(update))
        self.assertNotIn("bind_domain_pending", context.user_data)

    async def test_lists_zones(self):
        update, context = self._start_flow()
        await bd.bind_domain_callback(update, context)
        self.assertIn("先选主域名", reply_text_of(update))
        datas = _callback_datas(reply_markup_of(update))
        self.assertIn("bd:zone:0:0", datas)
        self.assertIn("bd:zone:0:1", datas)
        self.assertEqual(context.user_data["bind_domain_pending"]["ip"],
                         FAKE_IP)

    async def test_no_zones(self):
        self.cf.zones = []
        update, context = self._start_flow()
        await bd.bind_domain_callback(update, context)
        self.assertIn("没有 Zone", reply_text_of(update))

    async def test_no_credentials(self):
        self._cf_patch.stop()
        with patch.object(bd, "_get_cf_client",
                          side_effect=_cf_error("config")):
            update, context = self._start_flow()
            await bd.bind_domain_callback(update, context)
        self.assertIn("凭据未配置", reply_text_of(update))
        datas = _callback_datas(reply_markup_of(update))
        self.assertIn("cf:add", datas)

    async def test_instance_gone(self):
        context = make_context([])
        update = make_update(callback_data="bd:start:0")
        await bd.bind_domain_callback(update, context)
        self.assertIn("不存在或列表已过期", reply_text_of(update))


class TestBindDomainFlow(BotTestCase):
    """完整流程：选 Zone → 输二级域名 → 建记录 / 冲突确认覆盖。"""

    async def test_pick_zone_prompts_subdomain(self):
        context = await self._run_start_and_zone()
        pending = context.user_data["bind_domain_pending"]
        self.assertEqual(pending["zone_id"], FAKE_ZONE_ID)
        self.assertEqual(pending["zone_name"], "example.com")

    async def test_invalid_subdomain_keeps_pending(self):
        context = await self._run_start_and_zone()
        update = make_update(text="a b")
        handled = await bd.bind_domain_text_input(update, context)
        self.assertTrue(handled)
        self.assertIn("格式不对", reply_text_of(update))
        self.assertIn("bind_domain_pending", context.user_data)
        self.assertEqual(len(self.cf.created), 0)

    async def test_text_input_without_pending_ignored(self):
        context = make_context([_fake_instance()])
        update = make_update(text="www")
        handled = await bd.bind_domain_text_input(update, context)
        self.assertFalse(handled)

    async def test_create_record(self):
        context = await self._run_start_and_zone()
        update = make_update(text="www")
        handled = await bd.bind_domain_text_input(update, context)
        self.assertTrue(handled)
        self.assertEqual(len(self.cf.created), 1)
        self.assertEqual(self.cf.created[0]["name"], "www.example.com")
        self.assertEqual(self.cf.created[0]["content"], FAKE_IP)
        text = reply_text_of(update)
        self.assertIn("www.example.com → 203.0.113.10", text)
        self.assertIn("传播", text)
        self.assertNotIn("bind_domain_pending", context.user_data)

    async def test_root_domain(self):
        context = await self._run_start_and_zone()
        update = make_update(text="@")
        await bd.bind_domain_text_input(update, context)
        self.assertEqual(self.cf.created[0]["name"], "example.com")
        self.assertIn("example.com → 203.0.113.10", reply_text_of(update))

    async def test_conflict_prompts_overwrite(self):
        self.cf.records = [_existing_record()]
        context = await self._run_start_and_zone()
        update = make_update(text="www")
        await bd.bind_domain_text_input(update, context)
        self.assertEqual(len(self.cf.created), 0)
        self.assertEqual(len(self.cf.updated), 0)
        text = reply_text_of(update)
        self.assertIn("已有一条 A 记录指向 203.0.113.99", text)
        datas = _callback_datas(reply_markup_of(update))
        self.assertIn("bd:overwrite", datas)
        pending = context.user_data["bind_domain_pending"]
        self.assertEqual(pending["existing_id"],
                         "recid000000000000000000000001")

    async def test_overwrite_updates_record(self):
        self.cf.records = [_existing_record()]
        context = await self._run_start_and_zone()
        update = make_update(text="www")
        await bd.bind_domain_text_input(update, context)
        overwrite_update = make_update(callback_data="bd:overwrite")
        await bd.bind_domain_callback(overwrite_update, context)
        self.assertEqual(len(self.cf.updated), 1)
        self.assertEqual(self.cf.updated[0]["content"], FAKE_IP)
        text = reply_text_of(overwrite_update)
        self.assertIn("已更新：www.example.com → 203.0.113.10", text)
        self.assertIn("传播", text)
        self.assertNotIn("bind_domain_pending", context.user_data)

    async def test_exists_same_noop(self):
        record = _existing_record()
        record["content"] = FAKE_IP
        self.cf.records = [record]
        context = await self._run_start_and_zone()
        update = make_update(text="www")
        await bd.bind_domain_text_input(update, context)
        self.assertEqual(len(self.cf.created), 0)
        self.assertIn("已指向 203.0.113.10", reply_text_of(update))

    async def test_cancel_clears_pending(self):
        context = await self._run_start_and_zone()
        update = make_update(callback_data="bd:cancel")
        await bd.bind_domain_callback(update, context)
        self.assertIn("已取消", reply_text_of(update))
        self.assertNotIn("bind_domain_pending", context.user_data)


# ================= Web 端 =================

class WebBindDomainTestCase(unittest.TestCase):
    """Web API 测试基类：建 app、登录、mock OCI 与 CF。"""

    @classmethod
    def setUpClass(cls):
        cls._hash_patcher = patch("web.config.ADMIN_PASSWORD_HASH",
                                  hash_password(TEST_PASSWORD))
        cls._hash_patcher.start()
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        cls._hash_patcher.stop()

    def setUp(self):
        LOGIN_GUARD.reset()
        SESSIONS.reset()
        self.client = TestClient(self.app)
        self.client.post("/api/auth/login",
                         json={"username": "admin",
                               "password": TEST_PASSWORD})
        self.cf = FakeCF()
        self._cf_patch = patch.object(
            web_bd, "get_configured_cf_client", return_value=self.cf)
        self._cf_patch.start()
        self.addCleanup(self._cf_patch.stop)
        self._ip_patch = patch.object(web_bd, "get_public_ip",
                                      return_value=FAKE_IP)
        self._ip_patch.start()
        self.addCleanup(self._ip_patch.stop)
        self._profile_patch = patch.object(
            web_bd, "resolve_profile",
            return_value=(MagicMock(), "ocid1.compartment.oc1.test"))
        self._profile_patch.start()
        self.addCleanup(self._profile_patch.stop)

    def _post(self, subdomain="www", zone_id=FAKE_ZONE_ID,
              overwrite=False):
        return self.client.post(
            f"/api/instances/{FAKE_INSTANCE_ID}/bind-domain",
            json={"zone_id": zone_id, "subdomain": subdomain,
                  "overwrite": overwrite})


class TestWebBindDomain(WebBindDomainTestCase):
    """POST /api/instances/{id}/bind-domain。"""

    def test_create(self):
        r = self._post()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["action"], "created")
        self.assertEqual(body["fqdn"], "www.example.com")
        self.assertEqual(body["ip"], FAKE_IP)
        self.assertEqual(len(self.cf.created), 1)
        self.assertEqual(self.cf.created[0]["content"], FAKE_IP)

    def test_root_domain(self):
        r = self._post(subdomain="@")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["fqdn"], "example.com")

    def test_exists_same(self):
        record = _existing_record()
        record["content"] = FAKE_IP
        self.cf.records = [record]
        r = self._post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "exists_same")
        self.assertEqual(len(self.cf.created), 0)

    def test_conflict_409(self):
        self.cf.records = [_existing_record()]
        r = self._post()
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["exists"])
        self.assertEqual(body["existing"]["content"], FAKE_OLD_IP)
        self.assertEqual(len(self.cf.created), 0)
        self.assertEqual(len(self.cf.updated), 0)

    def test_overwrite_updates(self):
        self.cf.records = [_existing_record()]
        r = self._post(overwrite=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["action"], "updated")
        self.assertEqual(len(self.cf.updated), 1)
        self.assertEqual(self.cf.updated[0]["content"], FAKE_IP)

    def test_no_public_ip(self):
        self._ip_patch.stop()
        with patch.object(web_bd, "get_public_ip", return_value=None):
            r = self._post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有公网 IP", r.json()["error"])

    def test_invalid_subdomain(self):
        r = self._post(subdomain="a b")
        self.assertEqual(r.status_code, 400)
        self.assertIn("格式不对", r.json()["error"])
        self.assertEqual(len(self.cf.created), 0)

    def test_no_credentials_502(self):
        self._cf_patch.stop()
        with patch.object(web_bd, "get_configured_cf_client",
                          side_effect=_cf_error("config")):
            r = self._post()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "Cloudflare 未配置或认证失败")

    def test_unauthorized_401(self):
        anon = TestClient(self.app)
        r = anon.post(
            f"/api/instances/{FAKE_INSTANCE_ID}/bind-domain",
            json={"zone_id": FAKE_ZONE_ID, "subdomain": "www"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
