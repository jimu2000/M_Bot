#!/usr/bin/env python3
"""mcp_server.py 的单元测试（mock 底层模块，不碰真实 OCI/CF API）。

运行：python3 -m unittest tests.test_mcp_server -v
"""

import importlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server
from mcp_server import (cf_list_dns_records, cf_list_zones, get_tools,
                        handle_request, oci_free_tier_summary,
                        oci_instance_detail, oci_list_instances, oci_quota,
                        serve_stdio)
from oci.client import NotFoundError, OCIError

# 占位符（测试里不出现真实 OCID / IP / Token）
FAKE_COMPARTMENT = "ocid1.tenancy.oc1..testxxxx"
FAKE_INSTANCE = "ocid1.instance.oc1..testxxxx"
FAKE_ZONE_ID = "zone-testxxxx"
FAKE_IP = "203.0.113.10"

FAKE_LIST_RESPONSE = [
    {"id": FAKE_INSTANCE,
     "display-name": "instance-a1",
     "shape": "VM.Standard.A1.Flex",
     "lifecycle-state": "RUNNING",
     "time-created": "2026-11-23T00:00:00Z"},
    {"id": "ocid1.instance.oc1..testyyyy",
     "display-name": "instance-e2",
     "shape": "VM.Standard.E2.1.Micro",
     "lifecycle-state": "STOPPED",
     "time-created": "2026-11-22T00:00:00Z"},
]

FAKE_DETAIL_RESPONSE = {
    "data": {
        "id": FAKE_INSTANCE,
        "display-name": "instance-a1",
        "shape": "VM.Standard.A1.Flex",
        "lifecycle-state": "RUNNING",
        "availability-domain": "AD-1",
    }
}

FAKE_ZONES = [
    {"id": FAKE_ZONE_ID, "name": "example.com", "status": "active"},
    {"id": "zone-testyyyy", "name": "example.org", "status": "pending"},
]

FAKE_RECORDS = [
    {"id": "rec-testxxxx", "type": "A", "name": "example.com",
     "content": FAKE_IP},
    {"id": "rec-testyyyy", "type": "CNAME", "name": "www.example.com",
     "content": "example.com"},
]

EXPECTED_TOOLS = {
    "oci_list_instances",
    "oci_instance_detail",
    "oci_quota",
    "oci_free_tier_summary",
    "cf_list_zones",
    "cf_list_dns_records",
}


class TestToolRegistration(unittest.TestCase):
    """工具注册：6 个工具全部注册，名字与参数清单正确。"""

    def test_six_tools_registered(self):
        tools = get_tools()
        self.assertEqual(len(tools), 6)
        self.assertEqual({t["name"] for t in tools}, EXPECTED_TOOLS)

    def test_each_tool_has_description_and_schema(self):
        for tool in get_tools():
            self.assertTrue(tool["description"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("properties", schema)
            # 对外清单不暴露内部 handler。
            self.assertNotIn("handler", tool)

    def test_parameter_lists(self):
        by_name = {t["name"]: t for t in get_tools()}
        self.assertEqual(
            set(by_name["oci_list_instances"]["inputSchema"]["properties"]),
            {"profile", "region", "compartment_id"})
        self.assertEqual(
            set(by_name["oci_instance_detail"]["inputSchema"]["properties"]),
            {"instance_id", "profile"})
        self.assertEqual(
            set(by_name["oci_quota"]["inputSchema"]["properties"]),
            {"profile", "compartment_id"})
        self.assertEqual(
            set(by_name["oci_free_tier_summary"]["inputSchema"]["properties"]),
            {"profile", "compartment_id"})
        self.assertEqual(
            set(by_name["cf_list_zones"]["inputSchema"]["properties"]),
            {"name"})
        self.assertEqual(
            set(by_name["cf_list_dns_records"]["inputSchema"]["properties"]),
            {"zone_id", "type"})

    def test_required_fields(self):
        by_name = {t["name"]: t for t in get_tools()}
        self.assertEqual(
            by_name["oci_instance_detail"]["inputSchema"].get("required"),
            ["instance_id"])
        self.assertEqual(
            by_name["cf_list_dns_records"]["inputSchema"].get("required"),
            ["zone_id"])
        for name in ("oci_list_instances", "oci_quota",
                     "oci_free_tier_summary", "cf_list_zones"):
            self.assertNotIn("required", by_name[name]["inputSchema"])


class _OciToolTestBase(unittest.TestCase):
    """OCI 工具测试基类：mock OCIClient 构造。"""

    def setUp(self):
        patcher = patch("mcp_server.OCIClient")
        self.mock_client_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = self.mock_client_cls.return_value


class TestOciListInstances(_OciToolTestBase):

    @patch("mcp_server.oci_instance.list_instances",
           return_value=FAKE_LIST_RESPONSE)
    def test_success_maps_fields(self, mock_list):
        result = oci_list_instances()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], [
            {"id": FAKE_INSTANCE, "name": "instance-a1",
             "shape": "VM.Standard.A1.Flex", "state": "RUNNING",
             "created": "2026-11-23T00:00:00Z"},
            {"id": "ocid1.instance.oc1..testyyyy", "name": "instance-e2",
             "shape": "VM.Standard.E2.1.Micro", "state": "STOPPED",
             "created": "2026-11-22T00:00:00Z"},
        ])

    @patch("mcp_server.oci_instance.list_instances", return_value=[])
    def test_passthrough_profile_region_compartment(self, mock_list):
        oci_list_instances(profile="ACCOUNT2", region="us-sanjose-1",
                           compartment_id=FAKE_COMPARTMENT)
        self.mock_client_cls.assert_called_once_with(
            profile="ACCOUNT2", region="us-sanjose-1")
        mock_list.assert_called_once_with(self.mock_client, FAKE_COMPARTMENT)

    @patch("mcp_server.oci_instance.list_instances", return_value=[])
    def test_defaults_used_when_empty(self, mock_list):
        oci_list_instances(profile="", region="", compartment_id="")
        self.mock_client_cls.assert_called_once_with(
            profile=mcp_server.DEFAULT_PROFILE,
            region=mcp_server.DEFAULT_REGION)
        mock_list.assert_called_once_with(
            self.mock_client, mcp_server.DEFAULT_COMPARTMENT_ID)

    @patch("mcp_server.oci_instance.list_instances",
           side_effect=OCIError("boom"))
    def test_oci_error_returns_ok_false(self, mock_list):
        result = oci_list_instances()
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    @patch("mcp_server.oci_instance.list_instances", return_value=None)
    def test_none_response_becomes_empty_list(self, mock_list):
        result = oci_list_instances()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], [])


class TestOciInstanceDetail(_OciToolTestBase):

    @patch("mcp_server.oci_instance.get_instance",
           return_value=FAKE_DETAIL_RESPONSE)
    def test_success_unwraps_data(self, mock_get):
        result = oci_instance_detail(FAKE_INSTANCE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], FAKE_DETAIL_RESPONSE["data"])
        mock_get.assert_called_once_with(self.mock_client, FAKE_INSTANCE)

    @patch("mcp_server.oci_instance.get_instance",
           return_value=FAKE_DETAIL_RESPONSE)
    def test_profile_passthrough(self, mock_get):
        oci_instance_detail(FAKE_INSTANCE, profile="ACCOUNT3")
        self.mock_client_cls.assert_called_once_with(
            profile="ACCOUNT3", region=mcp_server.DEFAULT_REGION)

    def test_empty_instance_id_returns_ok_false(self):
        result = oci_instance_detail("")
        self.assertFalse(result["ok"])
        self.assertIn("instance_id", result["error"])
        self.mock_client_cls.assert_not_called()

    @patch("mcp_server.oci_instance.get_instance",
           side_effect=NotFoundError("not found"))
    def test_not_found_returns_ok_false(self, mock_get):
        result = oci_instance_detail(FAKE_INSTANCE)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["error"])


class TestOciQuota(_OciToolTestBase):

    @patch("mcp_server.oci_quota_mod.get_boot_volume_usage",
           return_value={"used_gb": 47, "limit_gb": 200})
    @patch("mcp_server.oci_quota_mod.get_e2_usage",
           return_value={"count": 1, "limit": 2})
    @patch("mcp_server.oci_quota_mod.get_a1_usage",
           return_value={"ocpu_used": 3, "ocpu_limit": 4})
    def test_success_returns_three_sections(self, mock_a1, mock_e2, mock_boot):
        result = oci_quota(profile="ACCOUNT2",
                           compartment_id=FAKE_COMPARTMENT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["a1"], {"ocpu_used": 3, "ocpu_limit": 4})
        self.assertEqual(result["data"]["e2"], {"count": 1, "limit": 2})
        self.assertEqual(result["data"]["boot_volume"],
                         {"used_gb": 47, "limit_gb": 200})
        for mock_fn in (mock_a1, mock_e2, mock_boot):
            mock_fn.assert_called_once_with(self.mock_client, FAKE_COMPARTMENT)

    @patch("mcp_server.oci_quota_mod.get_a1_usage",
           side_effect=OCIError("quota boom"))
    def test_error_returns_ok_false(self, mock_a1):
        result = oci_quota()
        self.assertFalse(result["ok"])
        self.assertIn("quota boom", result["error"])


class TestOciFreeTierSummary(_OciToolTestBase):

    @patch("mcp_server.oci_quota_mod.get_free_tier_summary",
           return_value={"overall_ok": True, "a1": {}, "e2": {},
                         "boot_volume": {}})
    def test_success_passthrough(self, mock_summary):
        result = oci_free_tier_summary(profile="ACCOUNT2",
                                       compartment_id=FAKE_COMPARTMENT)
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["overall_ok"])
        mock_summary.assert_called_once_with(self.mock_client, FAKE_COMPARTMENT)
        self.mock_client_cls.assert_called_once_with(
            profile="ACCOUNT2", region=mcp_server.DEFAULT_REGION)

    @patch("mcp_server.oci_quota_mod.get_free_tier_summary",
           side_effect=OCIError("summary boom"))
    def test_error_returns_ok_false(self, mock_summary):
        result = oci_free_tier_summary()
        self.assertFalse(result["ok"])
        self.assertIn("summary boom", result["error"])


class _CfToolTestBase(unittest.TestCase):
    """CF 工具测试基类：注入假的 clouds.cloudflare 模块。"""

    def setUp(self):
        self._orig_cf_module = mcp_server.cf_module
        self.mock_client = MagicMock()
        mcp_server.cf_module = SimpleNamespace(
            get_client=lambda: self.mock_client)

    def tearDown(self):
        mcp_server.cf_module = self._orig_cf_module


class TestCfListZones(_CfToolTestBase):

    def test_success_no_filter(self):
        self.mock_client.list_zones.return_value = FAKE_ZONES
        result = cf_list_zones()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], FAKE_ZONES)
        self.mock_client.list_zones.assert_called_once_with(name=None)

    def test_name_filter_passthrough(self):
        self.mock_client.list_zones.return_value = FAKE_ZONES[:1]
        result = cf_list_zones(name="example.com")
        self.assertTrue(result["ok"])
        self.mock_client.list_zones.assert_called_once_with(name="example.com")

    def test_cloudflare_error_returns_ok_false(self):
        self.mock_client.list_zones.side_effect = (
            mcp_server.CloudflareError("cf boom"))
        result = cf_list_zones()
        self.assertFalse(result["ok"])
        self.assertIn("cf boom", result["error"])

    def test_kind_included_in_error_message(self):
        err = mcp_server.CloudflareError("bad token")
        err.kind = "auth"
        self.mock_client.list_zones.side_effect = err
        result = cf_list_zones()
        self.assertFalse(result["ok"])
        self.assertIn("[auth]", result["error"])
        self.assertIn("bad token", result["error"])


class TestCfListDnsRecords(_CfToolTestBase):

    def test_success_no_type_filter(self):
        self.mock_client.list_dns_records.return_value = FAKE_RECORDS
        result = cf_list_dns_records(FAKE_ZONE_ID)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], FAKE_RECORDS)
        self.mock_client.list_dns_records.assert_called_once_with(
            FAKE_ZONE_ID, type=None)

    def test_type_filter_passthrough(self):
        self.mock_client.list_dns_records.return_value = FAKE_RECORDS[:1]
        result = cf_list_dns_records(FAKE_ZONE_ID, type="A")
        self.assertTrue(result["ok"])
        self.mock_client.list_dns_records.assert_called_once_with(
            FAKE_ZONE_ID, type="A")

    def test_empty_zone_id_returns_ok_false(self):
        result = cf_list_dns_records("")
        self.assertFalse(result["ok"])
        self.assertIn("zone_id", result["error"])
        self.mock_client.list_dns_records.assert_not_called()

    def test_cloudflare_error_returns_ok_false(self):
        self.mock_client.list_dns_records.side_effect = (
            mcp_server.CloudflareError("dns boom"))
        result = cf_list_dns_records(FAKE_ZONE_ID)
        self.assertFalse(result["ok"])
        self.assertIn("dns boom", result["error"])


class TestCfModuleMissing(unittest.TestCase):
    """CF 模块缺失容错：cf 工具返回"未就绪"，服务器本身不受影响。"""

    def setUp(self):
        self._orig_cf_module = mcp_server.cf_module
        mcp_server.cf_module = None

    def tearDown(self):
        mcp_server.cf_module = self._orig_cf_module

    def test_list_zones_not_ready(self):
        result = cf_list_zones()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Cloudflare 模块未就绪")

    def test_list_dns_records_not_ready(self):
        result = cf_list_dns_records(FAKE_ZONE_ID)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Cloudflare 模块未就绪")

    def test_import_error_fallback_via_reload(self):
        # sys.modules 里把 clouds.cloudflare 置 None 会让 import 抛 ImportError，
        # 模拟模块尚未就绪；reload 后 cf_module 应为 None 且模块可正常 import。
        with patch.dict(sys.modules, {"clouds.cloudflare": None}):
            importlib.reload(mcp_server)
            try:
                self.assertIsNone(mcp_server.cf_module)
                result = mcp_server.cf_list_zones()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "Cloudflare 模块未就绪")
                # OCI 工具不受影响（仍可注册）。
                self.assertEqual(len(mcp_server.get_tools()), 6)
            finally:
                importlib.reload(mcp_server)


class TestJsonRpc(unittest.TestCase):
    """JSON-RPC 层：initialize / tools/list / tools/call round-trip。"""

    def test_initialize(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05",
                       "capabilities": {}, "clientInfo": {}}})
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"],
                         mcp_server.PROTOCOL_VERSION)
        self.assertEqual(response["result"]["serverInfo"]["name"], "rbot-clone")
        self.assertEqual(response["result"]["serverInfo"]["version"],
                         mcp_server.SERVER_VERSION)
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in response["result"]["tools"]}
        self.assertEqual(names, EXPECTED_TOOLS)

    @patch("mcp_server.oci_instance.list_instances",
           return_value=FAKE_LIST_RESPONSE)
    @patch("mcp_server.OCIClient")
    def test_tools_call_round_trip(self, mock_client_cls, mock_list):
        response = handle_request({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "oci_list_instances",
                       "arguments": {"profile": "ACCOUNT2"}}})
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"][0]["name"], "instance-a1")
        mock_client_cls.assert_called_once_with(
            profile="ACCOUNT2", region=mcp_server.DEFAULT_REGION)

    @patch("mcp_server.oci_instance.list_instances",
           side_effect=OCIError("boom"))
    @patch("mcp_server.OCIClient")
    def test_tools_call_error_sets_is_error(self, mock_client_cls, mock_list):
        response = handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "oci_list_instances", "arguments": {}}})
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertTrue(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertIn("boom", payload["error"])

    def test_tools_call_unknown_tool(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_tools_call_missing_required_argument(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "oci_instance_detail", "arguments": {}}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_tools_call_non_dict_arguments(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "oci_list_instances", "arguments": ["x"]}})
        self.assertEqual(response["error"]["code"], -32602)

    def test_unknown_method(self):
        response = handle_request({
            "jsonrpc": "2.0", "id": 8, "method": "resources/list"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_notification_returns_none(self):
        response = handle_request({
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(response)

    def test_invalid_request(self):
        response = handle_request({})
        self.assertEqual(response["error"]["code"], -32600)
        response = handle_request(["not", "a", "dict"])
        self.assertEqual(response["error"]["code"], -32600)


class TestServeStdio(unittest.TestCase):
    """stdio 循环：用 StringIO 模拟 stdin/stdout 做端到端 round-trip。"""

    @patch("mcp_server.oci_instance.list_instances",
           return_value=FAKE_LIST_RESPONSE)
    @patch("mcp_server.OCIClient")
    def test_full_session_round_trip(self, mock_client_cls, mock_list):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {}}),
            json.dumps({"jsonrpc": "2.0",
                        "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "oci_list_instances",
                                   "arguments": {}}}),
            "",  # 空行应跳过，不产生响应
        ]
        out = io.StringIO()
        serve_stdio(io.StringIO("\n".join(lines) + "\n"), out)
        responses = [json.loads(line) for line in
                     out.getvalue().strip().split("\n")]
        # 通知与空行不产生响应：4 条输入 -> 3 条响应。
        self.assertEqual(len(responses), 3)
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"],
                         "rbot-clone")
        self.assertEqual(len(responses[1]["result"]["tools"]), 6)
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]), 2)

    def test_parse_error_then_continue(self):
        lines = [
            "this is not json",
            json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"}),
        ]
        out = io.StringIO()
        serve_stdio(io.StringIO("\n".join(lines) + "\n"), out)
        responses = [json.loads(line) for line in
                     out.getvalue().strip().split("\n")]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], 9)
        self.assertIn("tools", responses[1]["result"])


if __name__ == "__main__":
    unittest.main()
