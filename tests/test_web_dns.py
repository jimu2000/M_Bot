#!/usr/bin/env python3
"""Web 面板 DNS 管理 API 与页面的测试（TestClient + mock CF 客户端）。

运行：python3 -m unittest tests.test_web_dns -v

约定：
  - Cloudflare 客户端 mock：patch web.routes.dns 命名空间里的 get_client
    （dns.py 用 from-import，与 handlers_extra 的测试约定一致）。
  - 测试数据只用文档保留值（example.com、203.0.113.x、假 zone id），
    绝不出现真实 IP/OCID/Token。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from web.app import create_app
from web.auth import LOGIN_GUARD, SESSIONS, hash_password
from web.routes import dns as dns_routes
from web.routes.dns import CloudflareError

TEST_PASSWORD = "test-password"

FAKE_ZONE_ID = "zoneid000000000000000000000001"
FAKE_ZONE_ID_2 = "zoneid000000000000000000000002"
FAKE_RECORD_ID = "recid000000000000000000000001"

FAKE_ZONES = [
    {"id": FAKE_ZONE_ID, "name": "example.com", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net", "ns2.example.net"]},
    {"id": FAKE_ZONE_ID_2, "name": "example.org", "status": "active",
     "paused": False, "name_servers": ["ns1.example.net", "ns2.example.net"]},
]

FAKE_RECORDS = [
    {"id": FAKE_RECORD_ID, "type": "A", "name": "www.example.com",
     "content": "203.0.113.10", "proxied": True, "ttl": 1,
     "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"},
    {"id": "recid000000000000000000000002", "type": "TXT",
     "name": "example.com", "content": "v=spf1 -all", "proxied": False,
     "ttl": 3600, "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"},
]

FAKE_CREATED = {"id": "recid000000000000000000000003", "type": "A",
                "name": "api.example.com", "content": "203.0.113.20",
                "proxied": False, "ttl": 1,
                "zone_id": FAKE_ZONE_ID, "zone_name": "example.com"}


def _cf_error(kind):
    """构造指定 kind 的 CloudflareError（先传 message 再设 kind，
    对正式版与替身版的构造函数都兼容）。"""
    err = CloudflareError("mock cloudflare failure: RAW-DETAIL-MUST-NOT-LEAK")
    err.kind = kind
    return err


class DnsApiTestCase(unittest.TestCase):
    """DNS API 测试基类：建 app、登录、mock CF 客户端。"""

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
        self.anon = TestClient(self.app)
        self._login()

        self.cf = MagicMock()
        self.cf.list_zones.return_value = FAKE_ZONES
        self.cf.list_dns_records.return_value = FAKE_RECORDS
        self.cf.create_dns_record.return_value = FAKE_CREATED
        self.cf.update_dns_record.return_value = dict(
            FAKE_CREATED, content="203.0.113.21")
        self.cf.delete_dns_record.return_value = True
        self.cf.set_proxied.return_value = dict(FAKE_CREATED, proxied=True)
        self._cf_patch = patch.object(dns_routes, "get_client",
                                      return_value=self.cf)
        self._cf_patch.start()
        self.addCleanup(self._cf_patch.stop)
        # v1.26.0：_get_client 优先走 tgbot.cf_config 的包装
        # （文件优先，环境变量兜底），这里也要 mock 掉。
        self._cf_cfg_patch = patch.object(
            dns_routes, "get_configured_cf_client", return_value=self.cf)
        self._cf_cfg_patch.start()
        self.addCleanup(self._cf_cfg_patch.stop)

    def _login(self, username="admin", password=TEST_PASSWORD):
        return self.client.post("/api/auth/login",
                                json={"username": username,
                                      "password": password})


class TestDnsZones(DnsApiTestCase):
    """GET /api/dns/zones。"""

    def test_zones_list(self):
        r = self.client.get("/api/dns/zones")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["zones"]), 2)
        self.assertEqual(body["zones"][0]["name"], "example.com")
        self.cf.list_zones.assert_called_once_with()

    def test_zones_unauthorized_401(self):
        r = self.anon.get("/api/dns/zones")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.json()["ok"])

    def test_zones_cf_auth_error_502(self):
        self.cf.list_zones.side_effect = _cf_error("auth")
        r = self.client.get("/api/dns/zones")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(r.json()["error"], "Cloudflare 未配置或认证失败")

    def test_zones_cf_config_error_502(self):
        self.cf.list_zones.side_effect = _cf_error("config")
        r = self.client.get("/api/dns/zones")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "Cloudflare 未配置或认证失败")

    def test_cf_client_not_ready_502(self):
        """clouds.cloudflare 与 cf_config 都未就绪时接口报 502。"""
        with patch.object(dns_routes, "get_client", None), \
                patch.object(dns_routes, "get_configured_cf_client", None):
            r = self.client.get("/api/dns/zones")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "Cloudflare 未配置或认证失败")

    def test_error_does_not_leak_raw_detail(self):
        """错误响应不含 Cloudflare API 原始错误细节。"""
        self.cf.list_zones.side_effect = _cf_error("api")
        r = self.client.get("/api/dns/zones")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("RAW-DETAIL-MUST-NOT-LEAK", r.text)


class TestDnsRecords(DnsApiTestCase):
    """记录列表 / 创建 / 更新 / 删除 / 代理切换。"""

    def test_records_list(self):
        r = self.client.get(f"/api/dns/zones/{FAKE_ZONE_ID}/records")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["zone_id"], FAKE_ZONE_ID)
        self.assertEqual(len(body["records"]), 2)
        self.assertEqual(body["records"][0]["content"], "203.0.113.10")
        self.cf.list_dns_records.assert_called_once_with(
            FAKE_ZONE_ID, type=None, name=None)

    def test_records_list_with_filters(self):
        r = self.client.get(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records?type=A&name=www")
        self.assertEqual(r.status_code, 200)
        self.cf.list_dns_records.assert_called_once_with(
            FAKE_ZONE_ID, type="A", name="www")

    def test_records_not_found_404(self):
        self.cf.list_dns_records.side_effect = _cf_error("not_found")
        r = self.client.get("/api/dns/zones/nonexistent-zone/records")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.json()["ok"])

    def test_records_rate_limit_429(self):
        self.cf.list_dns_records.side_effect = _cf_error("rate_limit")
        r = self.client.get(f"/api/dns/zones/{FAKE_ZONE_ID}/records")
        self.assertEqual(r.status_code, 429)
        self.assertFalse(r.json()["ok"])

    def test_create_record_success(self):
        body = {"type": "A", "name": "api.example.com",
                "content": "203.0.113.20", "proxied": False, "ttl": 1}
        r = self.client.post(f"/api/dns/zones/{FAKE_ZONE_ID}/records",
                             json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["record"]["name"], "api.example.com")
        self.cf.create_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, "A", "api.example.com", "203.0.113.20",
            proxied=False, ttl=1)

    def test_create_record_defaults(self):
        """proxied / ttl 不传时用默认值（False / 1）。"""
        body = {"type": "TXT", "name": "example.com",
                "content": "v=spf1 -all"}
        r = self.client.post(f"/api/dns/zones/{FAKE_ZONE_ID}/records",
                             json=body)
        self.assertEqual(r.status_code, 200)
        self.cf.create_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, "TXT", "example.com", "v=spf1 -all",
            proxied=False, ttl=1)

    def test_create_record_invalid_type_422(self):
        body = {"type": "SRV", "name": "example.com",
                "content": "203.0.113.30"}
        r = self.client.post(f"/api/dns/zones/{FAKE_ZONE_ID}/records",
                             json=body)
        self.assertEqual(r.status_code, 422)
        self.assertFalse(r.json()["ok"])
        self.cf.create_dns_record.assert_not_called()

    def test_create_record_missing_field_422(self):
        body = {"type": "A", "name": "example.com"}  # 缺 content
        r = self.client.post(f"/api/dns/zones/{FAKE_ZONE_ID}/records",
                             json=body)
        self.assertEqual(r.status_code, 422)

    def test_create_record_unauthorized_401(self):
        body = {"type": "A", "name": "example.com",
                "content": "203.0.113.30"}
        r = self.anon.post(f"/api/dns/zones/{FAKE_ZONE_ID}/records",
                           json=body)
        self.assertEqual(r.status_code, 401)

    def test_update_record(self):
        body = {"type": "A", "name": "api.example.com",
                "content": "203.0.113.21", "proxied": True, "ttl": 300}
        r = self.client.put(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/{FAKE_RECORD_ID}",
            json=body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["record"]["content"], "203.0.113.21")
        self.cf.update_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, FAKE_RECORD_ID, "A", "api.example.com",
            "203.0.113.21", proxied=True, ttl=300)

    def test_update_record_not_found_404(self):
        self.cf.update_dns_record.side_effect = _cf_error("not_found")
        body = {"type": "A", "name": "api.example.com",
                "content": "203.0.113.21"}
        r = self.client.put(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/nonexistent",
            json=body)
        self.assertEqual(r.status_code, 404)

    def test_delete_record(self):
        r = self.client.delete(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/{FAKE_RECORD_ID}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["deleted"])
        self.cf.delete_dns_record.assert_called_once_with(
            FAKE_ZONE_ID, FAKE_RECORD_ID)

    def test_delete_record_unauthorized_401(self):
        r = self.anon.delete(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/{FAKE_RECORD_ID}")
        self.assertEqual(r.status_code, 401)

    def test_proxied_toggle(self):
        r = self.client.post(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/{FAKE_RECORD_ID}/proxied",
            json={"proxied": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertTrue(r.json()["record"]["proxied"])
        self.cf.set_proxied.assert_called_once_with(
            FAKE_ZONE_ID, FAKE_RECORD_ID, True)

    def test_proxied_toggle_missing_body_422(self):
        r = self.client.post(
            f"/api/dns/zones/{FAKE_ZONE_ID}/records/{FAKE_RECORD_ID}/proxied",
            json={})
        self.assertEqual(r.status_code, 422)


class TestDnsPage(DnsApiTestCase):
    """GET /dns 页面与侧边栏。"""

    def test_dns_page_200_contains_dns_js(self):
        r = self.client.get("/dns")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/static/js/dns.js", r.text)
        self.assertIn("DNS 管理", r.text)
        self.assertIn('id="zone-select"', r.text)
        self.assertIn('id="dns-tbody"', r.text)
        self.assertIn('id="record-modal"', r.text)

    def test_dns_page_requires_login(self):
        r = self.anon.get("/dns", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["location"])

    def test_sidebar_has_dns_link(self):
        """侧边栏含 DNS 管理链接（/dns 页与 /overview 页都有）。"""
        for path in ("/dns", "/overview"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertIn('href="/dns"', r.text)
                self.assertIn('data-page="dns"', r.text)
                self.assertIn("DNS 管理", r.text)

    def test_dns_page_sidebar_active(self):
        """/dns 页上 DNS 管理链接带 active 高亮。"""
        r = self.client.get("/dns")
        self.assertIn('href="/dns" data-page="dns" class="active"', r.text)


# ================= v1.26.0：凭据管理接口 =================

class TestDnsCredentials(DnsApiTestCase):
    """GET/POST/DELETE /api/dns/credentials 与提取接口。"""

    def test_get_credentials_file_token(self):
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("fake-token", "", "", "file")):
            r = self.client.get("/api/dns/credentials")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["source"], "file")
        self.assertEqual(body["auth_mode"], "token")
        self.assertIsNone(body["email_masked"])
        # 响应里绝不出现凭据值。
        self.assertNotIn("fake-token", r.text)

    def test_get_credentials_env_key_masked_email(self):
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com", "env")):
            r = self.client.get("/api/dns/credentials")
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["source"], "env")
        self.assertEqual(body["auth_mode"], "api_key")
        self.assertEqual(body["email_masked"], "u***@example.com")
        self.assertNotIn("fake-key", r.text)
        self.assertNotIn("user@example.com", r.text)

    def test_get_credentials_oci_config(self):
        # v1.30.0：凭据来自 OCI 配置 [cloudflare] 段。
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com",
                              "oci_config")):
            r = self.client.get("/api/dns/credentials")
        body = r.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["source"], "oci_config")
        self.assertEqual(body["auth_mode"], "api_key")
        self.assertEqual(body["email_masked"], "u***@example.com")
        self.assertNotIn("fake-key", r.text)

    def test_get_credentials_unconfigured(self):
        with patch.object(dns_routes, "resolve_cf_credentials",
                          return_value=("", "", "", None)):
            r = self.client.get("/api/dns/credentials")
        body = r.json()
        self.assertFalse(body["configured"])
        self.assertIsNone(body["source"])
        self.assertIsNone(body["auth_mode"])

    def test_get_credentials_unauthorized_401(self):
        r = self.anon.get("/api/dns/credentials")
        self.assertEqual(r.status_code, 401)

    def test_save_token_success(self):
        with patch.object(dns_routes, "validate_and_save",
                          return_value=(True, 6, None)) as mock_save:
            r = self.client.post("/api/dns/credentials",
                                 json={"mode": "token",
                                       "api_token": "fake-token-123"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["zones"], 6)
        mock_save.assert_called_once_with("fake-token-123", "", "")
        self.assertNotIn("fake-token-123", r.text)

    def test_save_key_success(self):
        with patch.object(dns_routes, "validate_and_save",
                          return_value=(True, 6, None)) as mock_save:
            r = self.client.post("/api/dns/credentials",
                                 json={"mode": "key", "api_key": "fake-key",
                                       "email": "user@example.com"})
        self.assertEqual(r.status_code, 200)
        mock_save.assert_called_once_with("", "fake-key",
                                          "user@example.com")

    def test_save_token_validate_failure_502(self):
        with patch.object(dns_routes, "validate_and_save",
                          return_value=(False, 0, "auth")):
            r = self.client.post("/api/dns/credentials",
                                 json={"mode": "token",
                                       "api_token": "bad-token"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("校验没通过", r.json()["error"])

    def test_save_empty_token_400(self):
        r = self.client.post("/api/dns/credentials",
                             json={"mode": "token", "api_token": "   "})
        self.assertEqual(r.status_code, 400)

    def test_save_key_bad_email_400(self):
        r = self.client.post("/api/dns/credentials",
                             json={"mode": "key", "api_key": "fake-key",
                                   "email": "not-an-email"})
        self.assertEqual(r.status_code, 400)

    def test_delete_file_config(self):
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("fake-token", "", "", "file")), \
                patch.object(dns_routes,
                             "clear_cf_config") as mock_clear:
            r = self.client.delete("/api/dns/credentials")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        mock_clear.assert_called_once_with()

    def test_delete_env_source_400(self):
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com", "env")):
            r = self.client.delete("/api/dns/credentials")
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有已保存的凭据文件", r.json()["error"])

    def test_delete_oci_config_source_400(self):
        # v1.30.0：凭据在 OCI 配置文件里时，删除也要说清来源。
        with patch.object(
                dns_routes, "resolve_cf_credentials",
                return_value=("", "fake-key", "user@example.com",
                              "oci_config")):
            r = self.client.delete("/api/dns/credentials")
        self.assertEqual(r.status_code, 400)
        self.assertIn("OCI 配置文件", r.json()["error"])

    def test_extract_success(self):
        with patch.object(dns_routes, "get_oci_client",
                          return_value=MagicMock()), \
                patch.object(dns_routes, "get_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(dns_routes, "host_for_ip",
                             return_value={"host": "203.0.113.10"}), \
                patch.object(dns_routes, "extract_cf_from_host",
                             return_value=("fake-key", "user@example.com",
                                           None)), \
                patch.object(dns_routes, "validate_and_save",
                             return_value=(True, 6, None)):
            r = self.client.post("/api/dns/credentials/extract",
                                 json={"instance_id":
                                       "ocid1.instance.oc1..fake"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["zones"], 6)
        self.assertEqual(body["email_masked"], "u***@example.com")
        self.assertNotIn("fake-key", r.text)

    def test_extract_no_public_ip_400(self):
        with patch.object(dns_routes, "get_oci_client",
                          return_value=MagicMock()), \
                patch.object(dns_routes, "get_public_ip",
                             return_value=None):
            r = self.client.post("/api/dns/credentials/extract",
                                 json={"instance_id":
                                       "ocid1.instance.oc1..fake"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("没有公网 IP", r.json()["error"])

    def test_extract_host_failure_502(self):
        with patch.object(dns_routes, "get_oci_client",
                          return_value=MagicMock()), \
                patch.object(dns_routes, "get_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(dns_routes, "host_for_ip",
                             return_value={"host": "203.0.113.10"}), \
                patch.object(dns_routes, "extract_cf_from_host",
                             return_value=(None, None, "该服务器上没有找到 1.sh")):
            r = self.client.post("/api/dns/credentials/extract",
                                 json={"instance_id":
                                       "ocid1.instance.oc1..fake"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("提取失败", r.json()["error"])

    def test_extract_validate_failure_502(self):
        with patch.object(dns_routes, "get_oci_client",
                          return_value=MagicMock()), \
                patch.object(dns_routes, "get_public_ip",
                             return_value="203.0.113.10"), \
                patch.object(dns_routes, "host_for_ip",
                             return_value={"host": "203.0.113.10"}), \
                patch.object(dns_routes, "extract_cf_from_host",
                             return_value=("fake-key", "user@example.com",
                                           None)), \
                patch.object(dns_routes, "validate_and_save",
                             return_value=(False, 0, "auth")):
            r = self.client.post("/api/dns/credentials/extract",
                                 json={"instance_id":
                                       "ocid1.instance.oc1..fake"})
        self.assertEqual(r.status_code, 502)
        self.assertIn("校验没通过", r.json()["error"])

    def test_dns_page_has_credentials_card(self):
        """/dns 页含凭据卡片（状态、手动表单、提取下拉）。"""
        r = self.client.get("/dns")
        self.assertEqual(r.status_code, 200)
        for marker in ("cf-status", "cf-mode", "cf-token", "cf-key",
                       "cf-email", "btn-cf-save", "cf-extract-instance",
                       "btn-cf-extract", "btn-cf-delete"):
            with self.subTest(marker=marker):
                self.assertIn(marker, r.text)


if __name__ == "__main__":
    unittest.main()
