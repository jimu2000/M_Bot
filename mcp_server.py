#!/usr/bin/env python3
"""MCP 服务器：把 M_Bot 的 OCI / Cloudflare 只读功能暴露给 MCP 客户端。

实现方式：标准库最小 JSON-RPC over stdio，不依赖 MCP Python SDK——
系统 Python 被 PEP 668 锁定，`pip install mcp`（含 --user）装不上；
按任务要求改用标准库实现，不要因此阻塞。行为与 FastMCP 版本一致：
同样的 6 个工具、同样的参数与返回值结构；将来装上 SDK 后可直接把
下面的工具函数挂到 FastMCP 上。

协议：JSON-RPC 2.0 over stdio，每行一条消息。
  - initialize：返回协议版本与 serverInfo。
  - tools/list：返回 6 个工具的清单与 JSON Schema。
  - tools/call：调工具，返回 {"content": [{"type": "text", "text": ...}]}。
  - notifications/*：忽略，不回消息。

用法：
    python3 mcp_server.py          # stdio 模式，等 MCP 客户端连接
    # 在 Claude Desktop 等客户端配置里把本文件当 command 用即可。

安全说明：
  - 本模块是只读的：只暴露查询类工具，不做创建/删除/启停等写操作。
  - 不读私钥、不读 CF 凭据；OCI 认证由官方 OCI CLI 自己处理，
    CF 凭据由 clouds.cloudflare 模块自己从环境变量读。
  - 配置区在文件顶端，真实 OCID / IP 一律用占位符，绝不写进代码。
"""

import json
import os
import sys

# Make the script import project modules (oci.* / clouds.*) from any working directory. 让脚本在任何工作目录下都能 import 项目内模块（oci.* / clouds.*）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ================= 配置区（改这里） =================
# MCP server name and version (returned to the client in initialize). MCP 服务器名与版本（initialize 返回给客户端）。
SERVER_NAME = "mbot"
SERVER_VERSION = "0.1.0"
# MCP protocol version (used for client negotiation; 2024-11-05 is a stable public version). MCP 协议版本（客户端协商用；2024-11-05 是稳定的公开版本）。
PROTOCOL_VERSION = "2024-11-05"
# Default OCI profile / region: environment variables win; the defaults are only examples and can be changed. 默认 OCI profile / region：优先读环境变量，默认值只是示例，可改。
# MBOT_OCI_PROFILE: a profile name in ~/.oci/config, e.g. DEFAULT / ACCOUNT2. MBOT_OCI_PROFILE：~/.oci/config 里的 profile 名，如 DEFAULT / ACCOUNT2。
DEFAULT_PROFILE = os.environ.get("MBOT_OCI_PROFILE", "DEFAULT")
# MBOT_OCI_REGION: e.g. ap-singapore-1 / us-sanjose-1 / us-chicago-1. MBOT_OCI_REGION：如 ap-singapore-1 / us-sanjose-1 / us-chicago-1。
DEFAULT_REGION = os.environ.get("MBOT_OCI_REGION", "ap-singapore-1")
# Default compartment OCID (the root compartment is the tenancy OCID; placeholder). 默认 compartment OCID（根 compartment 即 tenancy OCID，占位符）。
# Pass the real value via the MBOT_OCI_COMPARTMENT_ID environment variable or the compartment_id tool argument. 真实值用 MBOT_OCI_COMPARTMENT_ID 环境变量或工具参数 compartment_id 传入。
DEFAULT_COMPARTMENT_ID = os.environ.get(
    "MBOT_OCI_COMPARTMENT_ID", "ocid1.tenancy.oc1..xxxxxxxx")
# =====================================================

from oci import instance as oci_instance
from oci import quota as oci_quota_mod
from oci.client import OCIClient, OCIError

# A Cloudflare module import failure must not bring the whole server down; the two CF tools return a "module not ready" error in that case. Cloudflare 模块导入失败时不要让整个服务器起不来，CF 两个工具届时返回“模块未就绪”错误。
try:
    from clouds import cloudflare as cf_module
    from clouds.cloudflare import CloudflareError
except ImportError:  # pragma: no cover - 取决于 clouds.cloudflare 是否存在
    cf_module = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的占位异常（与真实类同名，便于统一捕获）。"""


# ---------------- 内部辅助 ----------------

def _make_client(profile="", region=""):
    """构造 OCIClient：空字符串用配置区默认值。"""
    return OCIClient(profile=profile or DEFAULT_PROFILE,
                     region=region or DEFAULT_REGION)


def _resolve_compartment(compartment_id=""):
    """空 compartment_id 用配置区默认值。"""
    return compartment_id or DEFAULT_COMPARTMENT_ID


def _unwrap(data):
    """OCI CLI 的 get/list 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def _error_result(error):
    """把异常转成工具错误返回；CloudflareError 带 .kind 时拼进消息。"""
    kind = getattr(error, "kind", None)
    message = str(error)
    if kind:
        message = f"[{kind}] {message}"
    return {"ok": False, "error": message}


# ---------------- 工具函数（6 个，各自独立定义） ----------------

def oci_list_instances(profile="", region="", compartment_id=""):
    """列出 compartment 下的 OCI 实例（精简字段）。

    Args:
        profile: ~/.oci/config 里的 profile 名，空则用默认。
        region: 区域名，空则用默认。
        compartment_id: compartment OCID，空则用配置区默认值。

    返回 {"ok": true, "data": [{id, name, shape, state, created}, ...]}。
    注意：oci.instance.list_instances 只返回 id / display-name / shape /
    lifecycle-state / time-created 五个字段，没有 public_ip（拿公网 IP
    要再按实例查 VNIC，本工具不做额外调用）。
    """
    try:
        cli = _make_client(profile, region)
        items = oci_instance.list_instances(
            cli, _resolve_compartment(compartment_id))
        return {"ok": True, "data": [
            {"id": item.get("id"),
             "name": item.get("display-name"),
             "shape": item.get("shape"),
             "state": item.get("lifecycle-state"),
             "created": item.get("time-created")}
            for item in (items or [])]}
    except (OCIError, ValueError) as e:
        return _error_result(e)


def oci_instance_detail(instance_id, profile=""):
    """查单个 OCI 实例的完整详情。

    Args:
        instance_id: 实例 OCID（必填）。
        profile: ~/.oci/config 里的 profile 名，空则用默认。

    底层调 oci.instance.get_instance（instance.py 里真实的函数名，
    没有 get_instance_detail 这个名字）。
    """
    try:
        if not instance_id:
            raise ValueError("instance_id 不能为空")
        cli = _make_client(profile)
        detail = oci_instance.get_instance(cli, instance_id)
        return {"ok": True, "data": _unwrap(detail)}
    except (OCIError, ValueError) as e:
        return _error_result(e)


def oci_quota(profile="", compartment_id=""):
    """查 Always Free 三项额度的用量明细（A1 / E2 / 启动盘）。

    Args:
        profile: ~/.oci/config 里的 profile 名，空则用默认。
        compartment_id: compartment OCID，空则用配置区默认值。

    返回三项原始用量（get_a1_usage / get_e2_usage /
    get_boot_volume_usage）；要 used/limit/remaining 汇总与 overall_ok
    请用 oci_free_tier_summary。
    """
    try:
        cli = _make_client(profile)
        compartment = _resolve_compartment(compartment_id)
        return {"ok": True, "data": {
            "a1": oci_quota_mod.get_a1_usage(cli, compartment),
            "e2": oci_quota_mod.get_e2_usage(cli, compartment),
            "boot_volume": oci_quota_mod.get_boot_volume_usage(cli, compartment),
        }}
    except (OCIError, ValueError) as e:
        return _error_result(e)


def oci_free_tier_summary(profile="", compartment_id=""):
    """查 Always Free 额度汇总（used/limit/remaining 与 overall_ok）。

    Args:
        profile: ~/.oci/config 里的 profile 名，空则用默认。
        compartment_id: compartment OCID，空则用配置区默认值。

    底层调 oci.quota.get_free_tier_summary。
    """
    try:
        cli = _make_client(profile)
        summary = oci_quota_mod.get_free_tier_summary(
            cli, _resolve_compartment(compartment_id))
        return {"ok": True, "data": summary}
    except (OCIError, ValueError) as e:
        return _error_result(e)


def cf_list_zones(name=""):
    """列出 Cloudflare 账号下的 Zone（可按域名过滤）。

    Args:
        name: 域名过滤（如 example.com），空则列出全部。

    底层调 clouds.cloudflare.get_client().list_zones(name=...)；
    clouds.cloudflare 模块未就绪时返回 ok:false。
    """
    if cf_module is None:
        return {"ok": False, "error": "Cloudflare 模块未就绪"}
    try:
        zones = cf_module.get_client().list_zones(name=name or None)
        return {"ok": True, "data": zones}
    except (CloudflareError, ValueError) as e:
        return _error_result(e)


def cf_list_dns_records(zone_id, type=""):
    """列出指定 Zone 下的 DNS 记录（可按类型过滤）。

    Args:
        zone_id: Zone ID（必填）。
        type: 记录类型过滤，如 A / AAAA / CNAME，空则列出全部。

    底层调 clouds.cloudflare.get_client().list_dns_records(zone_id, type=...)；
    clouds.cloudflare 模块未就绪时返回 ok:false。
    """
    if cf_module is None:
        return {"ok": False, "error": "Cloudflare 模块未就绪"}
    try:
        if not zone_id:
            raise ValueError("zone_id 不能为空")
        records = cf_module.get_client().list_dns_records(
            zone_id, type=type or None)
        return {"ok": True, "data": records}
    except (CloudflareError, ValueError) as e:
        return _error_result(e)


# ---------------- 工具清单 ----------------

_TOOL_DEFS = [
    {
        "name": "oci_list_instances",
        "description": "列出 OCI compartment 下的实例（id/name/shape/state/created）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string",
                            "description": "OCI profile 名，空则用默认"},
                "region": {"type": "string",
                           "description": "区域名，空则用默认"},
                "compartment_id": {"type": "string",
                                   "description": "compartment OCID，空则用配置区默认值"},
            },
        },
        "handler": oci_list_instances,
    },
    {
        "name": "oci_instance_detail",
        "description": "查单个 OCI 实例的完整详情。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string",
                                "description": "实例 OCID"},
                "profile": {"type": "string",
                            "description": "OCI profile 名，空则用默认"},
            },
            "required": ["instance_id"],
        },
        "handler": oci_instance_detail,
    },
    {
        "name": "oci_quota",
        "description": "查 Always Free 三项额度的用量明细（A1 / E2 / 启动盘）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string",
                            "description": "OCI profile 名，空则用默认"},
                "compartment_id": {"type": "string",
                                   "description": "compartment OCID，空则用配置区默认值"},
            },
        },
        "handler": oci_quota,
    },
    {
        "name": "oci_free_tier_summary",
        "description": "查 Always Free 额度汇总（used/limit/remaining 与 overall_ok）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string",
                            "description": "OCI profile 名，空则用默认"},
                "compartment_id": {"type": "string",
                                   "description": "compartment OCID，空则用配置区默认值"},
            },
        },
        "handler": oci_free_tier_summary,
    },
    {
        "name": "cf_list_zones",
        "description": "列出 Cloudflare 账号下的 Zone（可按域名过滤）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "域名过滤，空则列出全部"},
            },
        },
        "handler": cf_list_zones,
    },
    {
        "name": "cf_list_dns_records",
        "description": "列出指定 Zone 下的 DNS 记录（可按类型过滤）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string",
                            "description": "Zone ID"},
                "type": {"type": "string",
                         "description": "记录类型过滤，如 A / AAAA / CNAME，空则列出全部"},
            },
            "required": ["zone_id"],
        },
        "handler": cf_list_dns_records,
    },
]

_HANDLERS = {tool["name"]: tool["handler"] for tool in _TOOL_DEFS}


def get_tools():
    """返回工具清单（name/description/inputSchema），不启动服务。"""
    return [{"name": tool["name"],
             "description": tool["description"],
             "inputSchema": tool["inputSchema"]}
            for tool in _TOOL_DEFS]


# ---------------- JSON-RPC over stdio ----------------

class _JsonRpcError(Exception):
    """JSON-RPC 错误（带标准错误码）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _result_response(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _call_tool(params):
    """执行 tools/call，返回 MCP 规定的 content 结构。

    工具返回 ok:false 时置 isError=true（MCP 约定：工具级错误走
    isError，而不是 JSON-RPC 层错误）。
    """
    if not isinstance(params, dict):
        raise _JsonRpcError(-32602, "tools/call 的 params 必须是对象")
    name = params.get("name", "")
    handler = _HANDLERS.get(name)
    if handler is None:
        raise _JsonRpcError(-32602, f"Unknown tool: {name}")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise _JsonRpcError(-32602, "arguments 必须是对象")
    try:
        result = handler(**arguments)
    except TypeError as e:
        raise _JsonRpcError(-32602, f"Invalid params for {name}: {e}")
    return {
        "content": [{"type": "text",
                     "text": json.dumps(result, ensure_ascii=False)}],
        "isError": not result.get("ok", True),
    }


def handle_request(request):
    """处理一条 JSON-RPC 请求 dict，返回响应 dict；通知类请求返回 None。"""
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _error_response(None, -32600, "Invalid Request")
    method = request.get("method", "")
    request_id = request.get("id")
    # Notifications (notifications/*) get no response. 通知（notifications/*）不回响应。
    if method.startswith("notifications/"):
        return None
    params = request.get("params") or {}
    try:
        if method == "initialize":
            return _result_response(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME,
                               "version": SERVER_VERSION},
            })
        if method == "tools/list":
            return _result_response(request_id, {"tools": get_tools()})
        if method == "tools/call":
            return _result_response(request_id, _call_tool(params))
        raise _JsonRpcError(-32601, f"Method not found: {method}")
    except _JsonRpcError as e:
        return _error_response(request_id, e.code, e.message)
    except Exception as e:  # An unexpected error inside a tool: do not let the server crash. 工具内部未预料的异常：不让服务器崩掉。
        return _error_response(request_id, -32603, f"Internal error: {e}")


def serve_stdio(in_stream=None, out_stream=None):
    """stdio 循环：每行一条 JSON 请求，逐条处理并回写响应。

    in_stream/out_stream 可注入（测试用 StringIO 模拟）；默认用
    sys.stdin/sys.stdout。收到 EOF 时退出。
    """
    in_stream = in_stream if in_stream is not None else sys.stdin
    out_stream = out_stream if out_stream is not None else sys.stdout
    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _error_response(None, -32700, "Parse error")
        else:
            response = handle_request(request)
        if response is None:
            continue
        out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
        out_stream.flush()


def main():
    """入口：stdio 模式运行。"""
    serve_stdio()


if __name__ == "__main__":
    main()
