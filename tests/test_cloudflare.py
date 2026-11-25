#!/usr/bin/env python3
"""cloudflare.py 的单元测试（httpx.MockTransport，不发真实请求）。

运行：python3 -m unittest tests.test_cloudflare -v

测试数据全部用文档保留值（example.com、203.0.113.10、假 zone id），
绝不出现真实 IP / OCID / Token。
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clouds.cloudflare import (API_BASE_URL, CloudflareClient,
                               CloudflareError, get_client)

# Placeholders (no real values in tests). 占位符（测试里不出现真实值）。
FAKE_TOKEN = "fake-token-xxxxxxxx"
FAKE_KEY = "fake-key-xxxxxxxx"
FAKE_EMAIL = "user@example.com"
FAKE_ZONE_ID = "zone-id-123"
FAKE_RECORD_ID = "record-id-456"


def _ok(result, total_pages=1, page=1):
    """成功响应的 Cloudflare 信封。"""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": result,
        "result_info": {
            "page": page,
            "per_page": 50,
            "total_pages": total_pages,
            "count": len(result) if isinstance(result, list) else 1,
            "total_count": len(result) if isinstance(result, list) else 1,
        },
    }


def _fail(errors, status=200):
    return {
        "success": False,
        "errors": errors,
        "messages": [],
        "result": None,
    }


def _zone(zone_id=FAKE_ZONE_ID, name="example.com"):
    return {
        "id": zone_id,
        "name": name,
        "status": "active",
        "paused": False,
        "name_servers": ["ns1.example.net", "ns2.example.net"],
    }


def _record(record_id=FAKE_RECORD_ID, name="www.example.com",
            content="203.0.113.10", proxied=False, ttl=1):
    return {
        "id": record_id,
        "type": "A",
        "name": name,
        "content": content,
        "proxied": proxied,
        "ttl": ttl,
        "zone_id": FAKE_ZONE_ID,
        "zone_name": "example.com",
    }


def _make_client(handler, **client_kwargs):
    """建一个挂了 MockTransport 的 CloudflareClient，handler 是
    httpx.MockTransport 的回调（request -> httpx.Response）。"""
    kwargs = {"api_token": FAKE_TOKEN}
    kwargs.update(client_kwargs)
    client = CloudflareClient(**kwargs)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers=client._auth_headers,
        timeout=client.timeout,
    )
    return client


class TestAuth(unittest.TestCase):
    def test_token_preferred_over_key(self):
        # When both a Token and Key+Email are given, the Token wins. 同时给 Token 和 Key+Email 时优先 Token。
        client = CloudflareClient(api_token=FAKE_TOKEN, api_key=FAKE_KEY,
                                  email=FAKE_EMAIL)
        self.assertEqual(client.auth_mode, "token")
        self.assertEqual(client._auth_headers,
                         {"Authorization": f"Bearer {FAKE_TOKEN}"})

    def test_api_key_fallback(self):
        client = CloudflareClient(api_key=FAKE_KEY, email=FAKE_EMAIL)
        self.assertEqual(client.auth_mode, "api_key")
        self.assertEqual(client._auth_headers,
                         {"X-Auth-Key": FAKE_KEY,
                          "X-Auth-Email": FAKE_EMAIL})

    def test_api_key_without_email_is_config_error(self):
        # A Key without an Email is not complete auth. 只有 Key 没有 Email 不算完整认证。
        with self.assertRaises(CloudflareError) as ctx:
            CloudflareClient(api_key=FAKE_KEY)
        self.assertEqual(ctx.exception.kind, "config")

    def test_no_credentials_raises_config_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CloudflareError) as ctx:
                CloudflareClient()
        self.assertEqual(ctx.exception.kind, "config")

    def test_env_token(self):
        with patch.dict(os.environ,
                        {"CF_API_TOKEN": FAKE_TOKEN}, clear=True):
            client = CloudflareClient()
        self.assertEqual(client.auth_mode, "token")

    def test_env_key_and_email(self):
        with patch.dict(os.environ,
                        {"CF_API_KEY": FAKE_KEY, "CF_EMAIL": FAKE_EMAIL},
                        clear=True):
            client = CloudflareClient()
        self.assertEqual(client.auth_mode, "api_key")

    def test_token_sent_on_real_request(self):
        # The request header really carries Bearer <redacted>. 请求头里确实带上 Bearer <redacted>。
        def handler(request):
            self.assertEqual(request.headers["Authorization"],
                             f"Bearer {FAKE_TOKEN}")
            return httpx.Response(200, json=_ok(_zone()))

        client = _make_client(handler)
        client.get_zone(FAKE_ZONE_ID)

    def test_api_key_sent_on_real_request(self):
        def handler(request):
            self.assertEqual(request.headers["X-Auth-Key"], FAKE_KEY)
            self.assertEqual(request.headers["X-Auth-Email"], FAKE_EMAIL)
            return httpx.Response(200, json=_ok(_zone()))

        with patch.dict(os.environ, {}, clear=True):
            client = _make_client(handler, api_token=None, api_key=FAKE_KEY,
                                  email=FAKE_EMAIL)
            client.get_zone(FAKE_ZONE_ID)

    def test_get_client_from_env(self):
        with patch.dict(os.environ,
                        {"CF_API_TOKEN": FAKE_TOKEN}, clear=True):
            client = get_client()
        self.assertIsInstance(client, CloudflareClient)
        self.assertEqual(client.auth_mode, "token")


class TestListZones(unittest.TestCase):
    def test_success_parse(self):
        def handler(request):
            return httpx.Response(200, json=_ok([_zone(), _zone("zone-id-789",
                                                                "example.org")]))

        zones = _make_client(handler).list_zones()
        self.assertEqual(len(zones), 2)
        self.assertEqual(zones[0]["id"], FAKE_ZONE_ID)
        self.assertEqual(zones[0]["name"], "example.com")
        self.assertEqual(zones[0]["status"], "active")
        self.assertFalse(zones[0]["paused"])
        self.assertEqual(zones[0]["name_servers"],
                         ["ns1.example.net", "ns2.example.net"])

    def test_pagination(self):
        seen_pages = []

        def handler(request):
            page = int(request.url.params["page"])
            seen_pages.append(page)
            self.assertEqual(request.url.params["per_page"], "50")
            if page == 1:
                return httpx.Response(200, json=_ok([_zone()], total_pages=2,
                                                    page=1))
            return httpx.Response(200, json=_ok([_zone("zone-id-789",
                                                       "example.org")],
                                                total_pages=2, page=2))

        zones = _make_client(handler).list_zones()
        self.assertEqual(seen_pages, [1, 2])
        self.assertEqual([z["name"] for z in zones],
                         ["example.com", "example.org"])

    def test_filter_params_passed_through(self):
        def handler(request):
            self.assertEqual(request.url.params["name"], "example.com")
            self.assertEqual(request.url.params["status"], "active")
            return httpx.Response(200, json=_ok([_zone()]))

        zones = _make_client(handler).list_zones(name="example.com",
                                                 status="active")
        self.assertEqual(len(zones), 1)


class TestGetZone(unittest.TestCase):
    def test_path_and_result(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path,
                             f"/client/v4/zones/{FAKE_ZONE_ID}")
            return httpx.Response(200, json=_ok(_zone()))

        zone = _make_client(handler).get_zone(FAKE_ZONE_ID)
        self.assertEqual(zone["id"], FAKE_ZONE_ID)
        self.assertEqual(zone["name"], "example.com")


class TestListDnsRecords(unittest.TestCase):
    def test_success_parse(self):
        def handler(request):
            return httpx.Response(200, json=_ok([_record()]))

        records = _make_client(handler).list_dns_records(FAKE_ZONE_ID)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["id"], FAKE_RECORD_ID)
        self.assertEqual(r["type"], "A")
        self.assertEqual(r["name"], "www.example.com")
        self.assertEqual(r["content"], "203.0.113.10")
        self.assertFalse(r["proxied"])
        self.assertEqual(r["ttl"], 1)
        self.assertEqual(r["zone_id"], FAKE_ZONE_ID)
        self.assertEqual(r["zone_name"], "example.com")

    def test_pagination(self):
        seen_pages = []

        def handler(request):
            page = int(request.url.params["page"])
            seen_pages.append(page)
            if page == 1:
                return httpx.Response(200, json=_ok([_record()], total_pages=2,
                                                    page=1))
            return httpx.Response(
                200, json=_ok([_record("record-id-789", "api.example.com",
                                       "203.0.113.20")],
                              total_pages=2, page=2))

        records = _make_client(handler).list_dns_records(FAKE_ZONE_ID)
        self.assertEqual(seen_pages, [1, 2])
        self.assertEqual([r["name"] for r in records],
                         ["www.example.com", "api.example.com"])

    def test_filter_params_passed_through(self):
        def handler(request):
            self.assertEqual(request.url.params["type"], "A")
            self.assertEqual(request.url.params["name"], "www.example.com")
            return httpx.Response(200, json=_ok([_record()]))

        records = _make_client(handler).list_dns_records(
            FAKE_ZONE_ID, type="A", name="www.example.com")
        self.assertEqual(len(records), 1)


class TestCreateDnsRecord(unittest.TestCase):
    def test_method_path_and_body(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path,
                             f"/client/v4/zones/{FAKE_ZONE_ID}/dns_records")
            body = json.loads(request.content)
            self.assertEqual(body, {
                "type": "A", "name": "www.example.com",
                "content": "203.0.113.10", "proxied": True, "ttl": 300,
            })
            return httpx.Response(200, json=_ok(_record(proxied=True,
                                                        ttl=300)))

        result = _make_client(handler).create_dns_record(
            FAKE_ZONE_ID, "A", "www.example.com", "203.0.113.10",
            proxied=True, ttl=300)
        self.assertEqual(result["id"], FAKE_RECORD_ID)
        self.assertTrue(result["proxied"])

    def test_defaults_proxied_false_ttl_1(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["proxied"], False)
            self.assertEqual(body["ttl"], 1)
            return httpx.Response(200, json=_ok(_record()))

        _make_client(handler).create_dns_record(
            FAKE_ZONE_ID, "A", "www.example.com", "203.0.113.10")

    def test_invalid_type_raises_value_error(self):
        client = _make_client(lambda r: httpx.Response(200, json=_ok({})))
        with self.assertRaises(ValueError):
            client.create_dns_record(FAKE_ZONE_ID, "INVALID", "www.example.com",
                                     "203.0.113.10")

    def test_empty_name_raises_value_error(self):
        client = _make_client(lambda r: httpx.Response(200, json=_ok({})))
        with self.assertRaises(ValueError):
            client.create_dns_record(FAKE_ZONE_ID, "A", "  ",
                                     "203.0.113.10")

    def test_empty_content_raises_value_error(self):
        client = _make_client(lambda r: httpx.Response(200, json=_ok({})))
        with self.assertRaises(ValueError):
            client.create_dns_record(FAKE_ZONE_ID, "A", "www.example.com", "")

    def test_update_invalid_type_raises_value_error(self):
        client = _make_client(lambda r: httpx.Response(200, json=_ok({})))
        with self.assertRaises(ValueError):
            client.update_dns_record(FAKE_ZONE_ID, FAKE_RECORD_ID, "INVALID",
                                     "www.example.com", "203.0.113.10")


class TestUpdateDeleteSetProxied(unittest.TestCase):
    def test_update_method_path_and_body(self):
        def handler(request):
            self.assertEqual(request.method, "PUT")
            self.assertEqual(
                request.url.path,
                f"/client/v4/zones/{FAKE_ZONE_ID}/dns_records/{FAKE_RECORD_ID}")
            body = json.loads(request.content)
            self.assertEqual(body, {
                "type": "A", "name": "www.example.com",
                "content": "203.0.113.20", "proxied": False, "ttl": 1,
            })
            return httpx.Response(200, json=_ok(_record(content="203.0.113.20")))

        result = _make_client(handler).update_dns_record(
            FAKE_ZONE_ID, FAKE_RECORD_ID, "A", "www.example.com",
            "203.0.113.20")
        self.assertEqual(result["content"], "203.0.113.20")

    def test_delete_returns_true(self):
        def handler(request):
            self.assertEqual(request.method, "DELETE")
            self.assertEqual(
                request.url.path,
                f"/client/v4/zones/{FAKE_ZONE_ID}/dns_records/{FAKE_RECORD_ID}")
            return httpx.Response(200, json=_ok({"id": FAKE_RECORD_ID}))

        self.assertTrue(_make_client(handler).delete_dns_record(
            FAKE_ZONE_ID, FAKE_RECORD_ID))

    def test_set_proxied_gets_then_puts_only_proxied(self):
        calls = []

        def handler(request):
            calls.append(request.method)
            if request.method == "GET":
                self.assertEqual(
                    request.url.path,
                    f"/client/v4/zones/{FAKE_ZONE_ID}/dns_records/{FAKE_RECORD_ID}")
                return httpx.Response(200, json=_ok(_record(proxied=False,
                                                            ttl=300)))
            self.assertEqual(request.method, "PUT")
            body = json.loads(request.content)
            # Only proxied changed; the other fields match the existing record from GET. 只有 proxied 变了，其余字段与 GET 到的现有记录一致。
            self.assertEqual(body, {
                "type": "A", "name": "www.example.com",
                "content": "203.0.113.10", "proxied": True, "ttl": 300,
            })
            return httpx.Response(200, json=_ok(_record(proxied=True,
                                                        ttl=300)))

        result = _make_client(handler).set_proxied(FAKE_ZONE_ID,
                                                   FAKE_RECORD_ID, True)
        self.assertEqual(calls, ["GET", "PUT"])
        self.assertTrue(result["proxied"])


class TestErrorClassification(unittest.TestCase):
    def _expect_kind(self, response, kind):
        client = _make_client(lambda r: response)
        with self.assertRaises(CloudflareError) as ctx:
            client.get_zone(FAKE_ZONE_ID)
        self.assertEqual(ctx.exception.kind, kind)

    def test_401_is_auth(self):
        self._expect_kind(
            httpx.Response(401, json=_fail(
                [{"code": 9109, "message": "Invalid access token"}])),
            "auth")

    def test_403_is_auth(self):
        self._expect_kind(
            httpx.Response(403, json=_fail(
                [{"code": 9109, "message": "Forbidden"}])),
            "auth")

    def test_404_is_not_found(self):
        self._expect_kind(
            httpx.Response(404, json=_fail(
                [{"code": 7000, "message": "No route for that URI"}])),
            "not_found")

    def test_error_code_7003_is_not_found(self):
        # HTTP 200 with error code 7003 (zone/record does not exist) also counts as not_found. HTTP 200 但 error code 7003（zone/record 不存在）也判 not_found。
        self._expect_kind(
            httpx.Response(200, json=_fail(
                [{"code": 7003, "message": "No route for that URI"}])),
            "not_found")

    def test_429_is_rate_limit_with_retry_after(self):
        client = _make_client(
            lambda r: httpx.Response(429, json=_fail(
                [{"code": 429, "message": "Too many requests"}]),
                headers={"Retry-After": "30"}))
        with self.assertRaises(CloudflareError) as ctx:
            client.get_zone(FAKE_ZONE_ID)
        self.assertEqual(ctx.exception.kind, "rate_limit")
        self.assertEqual(ctx.exception.retry_after, "30")

    def test_success_false_is_api(self):
        self._expect_kind(
            httpx.Response(400, json=_fail(
                [{"code": 1004, "message": "DNS Validation Error"}])),
            "api")

    def test_connect_error_is_network(self):
        def handler(request):
            raise httpx.ConnectError("connection refused",
                                     request=request)

        client = _make_client(handler)
        with self.assertRaises(CloudflareError) as ctx:
            client.get_zone(FAKE_ZONE_ID)
        self.assertEqual(ctx.exception.kind, "network")

    def test_list_paged_rate_limit(self):
        # The paginated path must classify 429 the same way. 翻页路径同样要分类 429。
        client = _make_client(
            lambda r: httpx.Response(429, json=_fail(
                [{"code": 429, "message": "Too many requests"}])))
        with self.assertRaises(CloudflareError) as ctx:
            client.list_zones()
        self.assertEqual(ctx.exception.kind, "rate_limit")


class TestApiBaseUrl(unittest.TestCase):
    def test_base_url_constant(self):
        self.assertEqual(API_BASE_URL, "https://api.cloudflare.com/client/v4")


if __name__ == "__main__":
    unittest.main()
