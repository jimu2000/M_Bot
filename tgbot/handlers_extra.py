#!/usr/bin/env python3
"""Telegram 机器人扩展命令（/oracle 菜单扩展项）。

本模块实现 /oracle 菜单里除核心实例管理之外的扩展项：开机（抢机）、
配额、流量/费用、订阅信息、一键测活、任务、profile、IP / IPv6、
被暂停开机、升降配、磁盘、用户/邮箱、工作流、区域、本机负载、日志、
快捷开机、监控/自启/每日报告、MFA、批量操作、A1 配置体检，
以及实例详情操作：改名、重装系统、串行控制台。
监控增强（流量守护、停机保活、SSL 监控）也在本模块，纯逻辑与
配置持久化在 tgbot/monitoring.py 里。
阶段 5 工作包 D 新增：对象存储、Email 发件人、抢机计划、
开放云面板端口、客户端管理、重启机器人、升级客户端。

调用关系：
  - handlers.py 收到 "o:<key>" 回调后，先处理核心项（实例管理等），
    其余转发给本模块的 dispatch_oracle_action(item, update, context)；
    返回 True 表示已处理，False 表示本模块未实现（handlers.py 再决定
    如何提示）。
  - "c:<action>:<idx>" 确认回调由 handlers.py 转发给
    handle_confirm(action, idx, update, context)。
  - 其余回调（b:/t:/p:/r:/v:/ba:/g:/k:/s:/os:/pl:/cl:）由 bot.py 直接注册
    到本模块的对应函数，不经过 handlers.py。
  - 改名 / 重装 / SSL 添加域名 / Email 发件人需要用户回复文字：bot.py
    注册 text_input_callback（MessageHandler，filters.TEXT & ~filters.COMMAND），
    只在 user_data["pending_input"] 存在时处理，否则忽略。
  - 顶层命令 /domain（Cloudflare DNS 管理）也在本模块：
    bot.py 直接注册 handle_domain 与 domain_callback（"domain:" 前缀）。
  - bot.py 在启动时无条件注册 check_instances_job（每 10 分钟）、
    daily_report_job（每天一次）与 ssl_check_job（每天一次）；
    开关逻辑在 job 函数内部判断（监控开关读 bot_data，流量守护 /
    停机保活 / SSL 监控读各自的配置文件）。

安全说明：
  - 全部脱敏：真实 OCID / Token / IP 绝不进代码，配置区一律用占位符。
  - 写操作（换 IP、升降配、删盘、删 MFA、抢机、删 Bucket/对象、
    创建发件人、开放端口、重启服务、升级）只在用户明确确认后执行，
    且本模块的单元测试一律用 mock，不碰真实 API。
  - 所有 handler 都加 @require_allowed（白名单）。handle_confirm 由
    handlers.py 的已鉴权回调调用，且参数顺序不同，不加该装饰器。

导入规范（为了可测试）：oci 函数一律 from-import，测试直接 patch
本模块命名空间里的名字；Grabber / client_ops / PLANS 同样 from-import
后 patch。
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from tgbot import config as bot_config
from tgbot import handlers_client  # 菜单 23/24（内存占用/清除）由该模块实现
from tgbot.keyboards import (
    back_to_oracle_keyboard,
    batch_action_keyboard,
    batch_select_keyboard,
    boot_confirm_keyboard,
    boot_preset_keyboard,
    confirm_keyboard,
    domain_records_keyboard,
    domain_zones_keyboard,
    instance_list_keyboard,
    keepalive_keyboard,
    oracle_menu_keyboard,
    profile_keyboard,
    ssl_add_cancel_keyboard,
    ssl_watch_keyboard,
    task_list_keyboard,
    traffic_guard_keyboard,
)
from tgbot.monitoring import (
    KEEPALIVE_MAX_FAILURES,
    current_month_transfer_gb,
    is_valid_domain,
    keepalive_enabled_instances,
    keepalive_record_failure,
    keepalive_record_success,
    keepalive_reset_instance,
    load_keepalive,
    load_keepalive_state,
    load_ssl_watch,
    load_traffic_guard,
    normalize_domain,
    record_traffic_guard_trigger,
    save_keepalive,
    save_keepalive_state,
    save_ssl_watch,
    save_traffic_guard,
    should_trigger_traffic_guard,
    ssl_reminder_due,
)
from tgbot.services import (
    TASKS,
    fmt_duration,
    fmt_error,
    fmt_instance_list,
    fmt_quota_summary,
    fmt_task_list,
    get_cli,
    get_compartment,
    get_current_profile,
    require_allowed,
    resolve_instance,
    resolve_volume,
    run_sync,
    set_current_profile,
    store_instances,
    store_volumes,
)
from oci.client import QuotaExceededError
from oci.email import create_sender, is_valid_email
from oci.grabber import TARGET_CPU, Grabber, GrabCancelledError
from oci.instance import (
    MAX_DISPLAY_NAME_LEN,
    batch_restart,
    batch_start,
    batch_stop,
    batch_terminate,
    get_instance,
    get_serial_console,
    list_instances,
    reinstall_instance,
    rename_instance,
    start_instance,
    stop_instance,
)
from oci.network import (
    change_public_ip,
    get_public_ip,
    list_public_ips,
    list_vnics,
)
from oci.objectstorage import (
    delete_bucket,
    delete_object,
    list_buckets,
    list_objects,
)
from oci.quota import a1_health_check, can_launch, get_free_tier_summary
from oci.volume import (
    delete_boot_volume,
    get_boot_volume,
    list_boot_volumes,
    list_orphan_boot_volumes,
    resize_boot_volume,
    total_boot_volume_gb,
)
from tgbot.client_ops import (
    BOT_SERVICE,
    PROJECT_ROOT,
    SERVICES,
    WEB_SERVICE,
    is_git_repo,
    restart_service,
    service_status,
    ufw_allow_port,
    upgrade_client,
)
from web.tasks import PLANS

try:
    # Cloudflare DNS 客户端：clouds/cloudflare.py 由另一工人并行编写。
    # 约定接口：get_client() -> CloudflareClient；
    # list_zones() / list_dns_records(zone_id, type=None, name=None)；
    # CloudflareError 有 .kind 字段（config/auth/not_found/rate_limit/
    # api/network）。
    from clouds.cloudflare import (
        CloudflareError,
        check_ssl_expiry,
        get_client,
    )
except ImportError:  # 未落地时 /domain 报配置错误，不让机器人崩掉
    get_client = None
    check_ssl_expiry = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（约定接口：有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind


logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# 开机预设：key 为 e2 / a1small / a1full。
# image_id / subnet_id / availability_domain / ssh_keys_file 都是占位符，
# 部署时改成真实值（镜像 ID 用 `oci compute image list` 查，子网 ID 在
# OCI 控制台 VCN 里看，可用域如 "xxxx:AP-SINGAPORE-1-AD-1"）。
BOOT_PRESETS = {
    "e2": {
        "shape": "VM.Standard.E2.1.Micro",
        "ocpus": 1,
        "memory_gb": 1,
        "target_cpu": TARGET_CPU,  # 只留 AMD EPYC 7J13 的
        "display_name": "e2-micro",
        "image_id": "ocid1.image.oc1..xxxxxxxx",
        "subnet_id": "ocid1.subnet.oc1..xxxxxxxx",
        "availability_domain": "xxxx:AP-SINGAPORE-1-AD-1",
        "ssh_keys_file": "/path/to/keys.pub",
    },
    "a1small": {
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 1,
        "memory_gb": 6,
        "target_cpu": None,  # A1 不筛 CPU，抢到就留
        "display_name": "a1-small",
        "image_id": "ocid1.image.oc1..xxxxxxxx",
        "subnet_id": "ocid1.subnet.oc1..xxxxxxxx",
        "availability_domain": "xxxx:AP-SINGAPORE-1-AD-1",
        "ssh_keys_file": "/path/to/keys.pub",
    },
    "a1full": {
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 4,
        "memory_gb": 24,
        "target_cpu": None,
        "display_name": "a1-full",
        "image_id": "ocid1.image.oc1..xxxxxxxx",
        "subnet_id": "ocid1.subnet.oc1..xxxxxxxx",
        "availability_domain": "xxxx:AP-SINGAPORE-1-AD-1",
        "ssh_keys_file": "/path/to/keys.pub",
    },
}
# 用量查询回溯的月数（流量 / 费用菜单）。
USAGE_MONTHS = 3
# 日志菜单读取的尾部行数。
LOG_TAIL_LINES = 30
# 单条 Telegram 消息的正文上限（留余量，日志超长时截断）。
LOG_MAX_CHARS = 3500
# 工作流菜单列出的最近条数。
WORKFLOW_LIST_LIMIT = 10
# A1 升/降配预设：(OCPU, 内存 GB)。
RESIZE_PRESETS = [(1, 6), (2, 12), (4, 24)]
# 启动盘扩容目标容量（GB）。
VOLUME_RESIZE_TARGETS = [100, 200]
# 规格名（升降配时判断用）。
SHAPE_E2_MICRO = "VM.Standard.E2.1.Micro"
SHAPE_A1_FLEX = "VM.Standard.A1.Flex"
# /domain 命令：单条消息最多显示的 DNS 记录数，超出时截断并提示总数。
DOMAIN_RECORDS_LIMIT = 20
# 批量操作的动作中文名（确认提示与结果通知共用）。
_BATCH_ACTION_TEXT = {
    "start": "开机",
    "stop": "关机",
    "restart": "重启",
    "terminate": "删除",
}
# A1 体检建议的 severity -> 展示符号。
_SEVERITY_MARK = {
    "error": "❌",
    "warning": "⚠️",
    "info": "💡",
    "ok": "✅",
}
# ---- 阶段 5 工作包 D 新增配置 ----
# 云面板端口（开放云面板端口功能放行的端口，与 web/config.py 的 PORT 一致）。
PANEL_PORT = 9527
# 面板服务器所在子网的安全组 OCID（部署时用环境变量
# RBOT_PANEL_SECURITY_LIST_ID 填真实值；查法：OCI 控制台 → 网络 →
# 安全列表，或 oci network security-list list --compartment-id <compartment OCID>）。
# 未配置（仍是占位符）时“开放云面板端口”菜单会提示先配置，不执行。
PANEL_SECURITY_LIST_ID = os.environ.get(
    "RBOT_PANEL_SECURITY_LIST_ID", "ocid1.securitylist.oc1..xxxxxxxx")
# 面板服务器 SSH 地址（UFW 放行用）：空字符串 = 面板就在本机
# （机器人与面板跑在同一台服务器上，部署现状如此），直接执行 ufw；
# 否则 ssh 到该主机执行（端口 14422，root 密钥登录，见 client_ops）。
PANEL_SSH_HOST = ""
# =====================================================


# ---------------- 通用小工具 ----------------

def _unwrap(result):
    """OCI CLI 的 get/list 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


async def _reply(update, text, reply_markup=None, parse_mode=None):
    """统一回复：回调时先 answer（防按钮转圈），再用消息回复。"""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, reply_markup=reply_markup,
                                 parse_mode=parse_mode)


def _callback_data(update):
    """取回调的 callback_data；不是回调时返回空字符串。"""
    query = update.callback_query
    return query.data if query is not None else ""


# ---------------- 快捷开机配置（presets.json） ----------------

def load_presets(path=None):
    """读已保存的快捷开机配置列表；文件不存在或损坏时返回 []。"""
    path = path or bot_config.PRESETS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_preset(preset, path=None):
    """把一次开机配置追加保存到 presets.json，返回保存后的完整列表。"""
    path = path or bot_config.PRESETS_FILE
    presets = load_presets(path)
    entry = dict(preset)
    entry["saved_at"] = datetime.now(timezone.utc).isoformat()
    presets.append(entry)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    return presets


# ---------------- 用量 API（流量 / 费用） ----------------

def usage_time_window(months=USAGE_MONTHS):
    """用量查询的时间窗口：(开始, 结束)，RFC3339 格式（UTC）。

    开始 = 今天往前推 months*30 天的 00:00；结束 = 今天 00:00。
    注意：用量 API 要求日期精度到天（时/分/秒必须为 0），
    否则报 InvalidParameter（2026-11-23 真实 API 实测）。
    """
    end = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30 * months)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_usage_summary(cli, compartment_id, granularity,
                        months=USAGE_MONTHS):
    """调用量 API，返回 items 列表（每项含 computed-amount / currency /
    service / time-usage-started / computed-quantity）。

    granularity：DAILY / HOURLY / MONTHLY（用量 API 只认这三个；
    没有 TOTAL——2026-11-23 真实 API 实测报 "Unknown Granularity"）。
    费用汇总也用 MONTHLY，再由 fmt_usage_total 求和。
    注意 tenant-id 传的是 tenancy OCID（根 compartment 即 tenancy）。
    """
    start, end = usage_time_window(months)
    result = cli.run(["usage-api", "usage-summary",
                      "request-summarized-usages",
                      "--tenant-id", compartment_id,
                      "--time-usage-started", start,
                      "--time-usage-ended", end,
                      "--granularity", granularity])
    data = _unwrap(result) or {}
    if isinstance(data, dict):
        return data.get("items") or []
    return []


def fmt_usage_monthly(items, months=USAGE_MONTHS):
    """按月汇总费用与流量（service 含 "Data Transfer" 的
    computed-quantity，按 API 返回值以 GB 计）。"""
    if not items:
        return f"近 {months} 个月没有用量记录。"
    by_month = {}
    total = 0.0
    currency = ""
    for item in items:
        month = (item.get("time-usage-started") or "")[:7] or "未知"
        amount = float(item.get("computed-amount") or 0)
        currency = item.get("currency") or currency
        entry = by_month.setdefault(month, {"amount": 0.0, "transfer": 0.0})
        entry["amount"] += amount
        if "Data Transfer" in (item.get("service") or ""):
            entry["transfer"] += float(item.get("computed-quantity") or 0)
        total += amount
    lines = [f"📈 近 {months} 个月流量与费用：", ""]
    for month in sorted(by_month):
        entry = by_month[month]
        lines.append(f"• {month}：费用 {entry['amount']:.2f} {currency}，"
                     f"出站流量 {entry['transfer']:.2f} GB")
    lines.append("")
    lines.append(f"合计：{total:.2f} {currency}")
    return "\n".join(lines)


def fmt_usage_total(items, months=USAGE_MONTHS):
    """汇总近 N 个月的总费用与币种。"""
    if not items:
        return f"近 {months} 个月没有用量记录。"
    total = sum(float(i.get("computed-amount") or 0) for i in items)
    currency = next((i.get("currency") for i in items if i.get("currency")),
                    "")
    return f"💰 近 {months} 个月总花费：{total:.2f} {currency}"


# ---------------- 订阅 / 用户 / 工作流 / 区域（cli.run 直调） ----------------

def get_tenancy(cli, compartment_id):
    """查租户订阅信息（iam tenancy get），返回解包后的字典。"""
    return _unwrap(cli.run(["iam", "tenancy", "get",
                            "--tenancy-id", compartment_id])) or {}


def fmt_tenancy(data):
    """订阅信息全文。"""
    if not isinstance(data, dict) or not data:
        return "订阅信息：无法解析返回结果。"
    lines = [
        "📄 订阅信息：",
        "",
        f"名称：{data.get('name', '?')}",
        f"Home 区域：{data.get('home-region-key', '?')}",
    ]
    if data.get("description"):
        lines.append(f"描述：{data['description']}")
    return "\n".join(lines)


def list_users(cli, compartment_id):
    """只读列出租户下的 IAM 用户（iam user list）。"""
    result = cli.run(["iam", "user", "list",
                      "--compartment-id", compartment_id])
    return _unwrap(result) or []


def fmt_user_list(users):
    """用户列表全文（只显示名字/邮箱/状态，不显示 OCID）。"""
    if not users:
        return "该租户下没有 IAM 用户。"
    lines = [f"👥 云账户（{len(users)} 个用户）：", ""]
    for user in users:
        email = user.get("email") or "无邮箱"
        state = user.get("lifecycle-state", "?")
        lines.append(f"• {user.get('name', '?')}（{email}，{state}）")
    return "\n".join(lines)


def fmt_email_list(users):
    """邮箱列表全文（users 是 list_users 的返回值）。"""
    emails = [u.get("email") for u in users if u.get("email")]
    if not emails:
        return "没有查到任何用户邮箱。"
    lines = [f"📧 用户邮箱（{len(emails)} 个）：", ""]
    lines.extend(f"• {email}" for email in emails)
    return "\n".join(lines)


def list_work_requests(cli, compartment_id, limit=WORKFLOW_LIST_LIMIT):
    """列最近的工作请求（work-requests work-request list）。"""
    result = cli.run(["work-requests", "work-request", "list",
                      "--compartment-id", compartment_id,
                      "--limit", str(limit)])
    return _unwrap(result) or []


def fmt_work_requests(requests):
    """工作流列表全文，FAILED 的标 ❌。"""
    if not requests:
        return "最近没有工作请求记录。"
    lines = [f"🔧 最近的工作请求（{len(requests)} 条）：", ""]
    for req in requests:
        status = req.get("status", "?")
        mark = "❌" if status == "FAILED" else "•"
        op = req.get("operation-type", "?")
        accepted = (req.get("time-accepted") or "")[:19].replace("T", " ")
        lines.append(f"{mark} {op}：{status}（{accepted}）")
    return "\n".join(lines)


def list_regions(cli, compartment_id):
    """列已订阅区域（iam region-subscription list）。"""
    result = cli.run(["iam", "region-subscription", "list",
                      "--tenancy-id", compartment_id])
    return _unwrap(result) or []


def fmt_regions(regions):
    """已订阅区域列表全文。"""
    if not regions:
        return "没有查到已订阅区域。"
    lines = [f"🌍 已订阅区域（{len(regions)} 个）：", ""]
    for region in regions:
        name = region.get("region-name") or region.get("region-key", "?")
        home = "（Home 区域）" if region.get("is-home-region") else ""
        status = region.get("status", "?")
        lines.append(f"• {name}{home}：{status}")
    return "\n".join(lines)


# ---------------- IPv6（cli.run 直调，阶段 1 未封装） ----------------

def list_ipv6_for_vnic(cli, vnic_id):
    """查某 VNIC 上的 IPv6 列表（network ipv6 list --vnic-id）。"""
    result = cli.run(["network", "ipv6", "list", "--vnic-id", vnic_id])
    return _unwrap(result) or []


def create_ipv6(cli, vnic_id, ip_address=None):
    """给 VNIC 分配一个 IPv6（network ipv6 create）。

    写操作：调用方应在用户确认后才调用。ip_address 可选，不传由 OCI 分配。
    返回创建后的 IPv6 详情字典。
    """
    args = ["network", "ipv6", "create", "--vnic-id", vnic_id]
    if ip_address:
        args += ["--ip-address", ip_address]
    return _unwrap(cli.run(args))


def delete_ipv6(cli, ipv6_id):
    """删除（解绑）一个 IPv6（network ipv6 delete --force）。

    写操作：调用方应在用户确认后才调用。成功时 OCI CLI 无输出，返回 None。
    """
    return cli.run(["network", "ipv6", "delete",
                    "--ipv6-id", ipv6_id, "--force"])


# ---------------- MFA 设备（cli.run 直调） ----------------

def list_mfa_devices(cli, compartment_id):
    """列出全部用户的 MFA TOTP 设备。

    返回 [{"user_id", "user_name", "device": {...}}, ...]；
    device 含 id / lifecycle-state / time-created 等。
    """
    devices = []
    for user in list_users(cli, compartment_id):
        result = cli.run(["iam", "mfa-totp-device", "list",
                          "--user-id", user["id"]])
        for device in _unwrap(result) or []:
            devices.append({
                "user_id": user["id"],
                "user_name": user.get("name", "?"),
                "device": device,
            })
    return devices


def delete_mfa_device(cli, user_id, device_id):
    """删除一个 MFA TOTP 设备（--force 跳过交互确认）。

    写操作：调用方应在用户双重确认后才调用。
    """
    return cli.run(["iam", "mfa-totp-device", "delete",
                    "--user-id", user_id,
                    "--mfa-totp-device-id", device_id, "--force"])


def fmt_mfa_devices(devices):
    """MFA 设备列表全文。"""
    if not devices:
        return "没有任何 MFA TOTP 设备。"
    lines = [f"🔐 MFA TOTP 设备（{len(devices)} 个）：", ""]
    for entry in devices:
        device = entry["device"]
        state = device.get("lifecycle-state", "?")
        created = (device.get("time-created") or "")[:10]
        lines.append(f"• {entry['user_name']}：{state}（{created}）")
    return "\n".join(lines)


# ---------------- IP / 磁盘 / 负载 / 日志的格式化 ----------------

def fmt_ip_overview(reserved_ips, instance_ips):
    """IP 管理全文。

    reserved_ips：list_public_ips 的返回值（保留 IP）。
    instance_ips：[(实例名, 公网 IP 或 None), ...]。
    """
    lines = ["🌐 IP 管理：", "", "保留 IP："]
    if not reserved_ips:
        lines.append("• 无保留 IP")
    for ip in reserved_ips:
        addr = ip.get("ip-address", "?")
        bound = "已绑定" if ip.get("assigned-entity") else "未绑定"
        lines.append(f"• {addr}（{ip.get('lifecycle-state', '?')}，{bound}）")
    lines.append("")
    lines.append("实例公网 IP：")
    if not instance_ips:
        lines.append("• 无实例")
    for name, addr in instance_ips:
        lines.append(f"• {name}：{addr or '无公网 IP'}")
    lines.append("")
    lines.append("换 IP：请在实例列表里选一台实例，点“换 IP”。")
    return "\n".join(lines)


def fmt_volume_list(volumes, orphans, total_gb):
    """磁盘管理全文。"""
    lines = [f"💾 启动盘（{len(volumes)} 块，共 {total_gb}GB）：", ""]
    if not volumes:
        lines.append("• 无启动盘")
    orphan_ids = {v.get("id") for v in orphans}
    for vol in volumes:
        name = vol.get("display-name") or "?"
        size = vol.get("size-in-gbs", "?")
        state = vol.get("lifecycle-state", "?")
        mark = " 🗑️孤儿盘" if vol.get("id") in orphan_ids else ""
        lines.append(f"• {name}：{size}GB（{state}）{mark}")
    if orphans:
        orphan_gb = sum(v.get("size-in-gbs") or 0 for v in orphans)
        lines.append("")
        lines.append(f"⚠️ 孤儿盘 {len(orphans)} 块（共 {orphan_gb}GB），"
                     "可清理释放额度。")
    lines.append("")
    lines.append("点下面按钮查看详情、扩容或删除。")
    return "\n".join(lines)


def fmt_volume_detail(volume):
    """单个启动盘详情全文。volume 是 get_boot_volume 的返回值。"""
    if not isinstance(volume, dict) or not volume:
        return "启动盘详情：无法解析返回结果。"
    lines = [
        f"💾 {volume.get('display-name', '?')}",
        f"容量：{volume.get('size-in-gbs', '?')}GB",
        f"状态：{volume.get('lifecycle-state', '?')}",
        f"可用域：{volume.get('availability-domain', '?')}",
    ]
    created = (volume.get("time-created") or "")[:19].replace("T", " ")
    if created:
        lines.append(f"创建时间：{created}")
    return "\n".join(lines)


def read_local_load():
    """读本机负载与内存（/proc/loadavg 与 /proc/meminfo）。

    返回 {"load1","load5","load15","mem_total_mb","mem_available_mb"}；
    读不到时对应字段为 None。
    """
    data = {"load1": None, "load5": None, "load15": None,
            "mem_total_mb": None, "mem_available_mb": None}
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            parts = f.read().split()
        data["load1"], data["load5"], data["load15"] = parts[0], parts[1], parts[2]
    except (OSError, ValueError, IndexError):
        pass
    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                meminfo[key.strip()] = value.strip().split()[0]
        data["mem_total_mb"] = int(meminfo["MemTotal"]) // 1024
        data["mem_available_mb"] = int(meminfo["MemAvailable"]) // 1024
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return data


def fmt_local_load(data):
    """本机负载全文。"""
    load = "、".join(str(data.get(k) or "?")
                     for k in ("load1", "load5", "load15"))
    total = data.get("mem_total_mb")
    avail = data.get("mem_available_mb")
    if total is not None and avail is not None:
        used = total - avail
        mem = f"{used}MB / {total}MB（可用 {avail}MB）"
    else:
        mem = "?"
    return (f"📊 客户端负载：\n\n"
            f"负载（1/5/15 分钟）：{load}\n"
            f"内存已用/总量：{mem}")


def read_log_tail(path=None, lines=LOG_TAIL_LINES):
    """读日志文件尾部 N 行；文件不存在返回 None。"""
    path = path or bot_config.LOG_FILE
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return None


# ---------------- 本模块自用的小键盘（keyboards.py 里没有的） ----------------

def resize_preset_keyboard(idx):
    """A1 升/降配预设按钮。

    注意：预设按钮的 callback 是 "r:<ocpus>:<mem>:<idx>"（先选目标规格，
    再弹确认键盘 "c:resize:<idx>"）。不能直接用 "c:resize:<idx>" 做预设
    按钮——三个预设的 callback_data 会完全相同，无法区分选了哪档。
    """
    rows = []
    for ocpus, memory_gb in RESIZE_PRESETS:
        rows.append([InlineKeyboardButton(
            f"{ocpus} 核 / {memory_gb}GB",
            callback_data=f"r:{ocpus}:{memory_gb}:{idx}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


def volume_list_keyboard(volumes):
    """磁盘列表键盘：每块盘一个详情按钮。"""
    rows = []
    for idx, vol in enumerate(volumes):
        name = vol.get("display-name") or (vol.get("id") or "")[-4:]
        rows.append([InlineKeyboardButton(
            f"{name}（{vol.get('size-in-gbs')}GB）",
            callback_data=f"v:detail:{idx}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def volume_actions_keyboard(idx):
    """单个磁盘的操作键盘：扩容 / 删除。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 扩容", callback_data=f"v:resize:{idx}"),
         InlineKeyboardButton("🗑️ 删除", callback_data=f"v:delete:{idx}")],
        [InlineKeyboardButton("◀ 磁盘列表", callback_data="o:volumes")],
    ])


def volume_resize_keyboard(idx):
    """启动盘扩容目标容量按钮（100/200GB）。"""
    rows = []
    for size_gb in VOLUME_RESIZE_TARGETS:
        rows.append([InlineKeyboardButton(
            f"扩到 {size_gb}GB",
            callback_data=f"v:volsize:{idx}:{size_gb}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


# ================= 菜单项 handler（每项一个，全部加 @require_allowed） =================

# ---- 1. 开机（刷机） ----

def fmt_boot_preset(preset, ok, reason):
    """开机预设的配置摘要 + 配额预检结果。"""
    shape = preset["shape"].replace("VM.Standard.", "")
    lines = [
        "🚀 开机配置：",
        "",
        f"规格：{shape}（{preset['ocpus']} 核 / {preset['memory_gb']}GB）",
        f"实例名：{preset['display_name']}",
    ]
    if preset.get("target_cpu"):
        lines.append(f"目标 CPU：只留含 {preset['target_cpu']} 的，"
                     "不符就释放继续抢")
    lines.append("")
    lines.append(f"配额预检：{'✅ ' if ok else '❌ '}{reason}")
    if not ok:
        lines.append("配额不足，抢机也会停下，请先释放资源或换账号。")
    return "\n".join(lines)


@require_allowed
async def handle_boot(update, context):
    """菜单 1：发开机预设选择键盘。"""
    await _reply(update, "🚀 开机（刷机）：选一个预设配置：",
                 reply_markup=boot_preset_keyboard())


@require_allowed
async def boot_preset_callback(update, context):
    """回调 "b:boot:<preset>"：存预设 + 显示摘要与配额预检。"""
    data = _callback_data(update)
    preset_key = data.split(":")[-1] if ":" in data else ""
    preset = BOOT_PRESETS.get(preset_key)
    if preset is None:
        await _reply(update, f"未知的开机预设：{preset_key}")
        return
    context.user_data["pending_boot"] = dict(preset)
    try:
        ok, reason = await run_sync(
            can_launch, get_cli(context), get_compartment(context),
            preset["shape"], preset["ocpus"], preset["memory_gb"])
    except Exception as exc:  # noqa: BLE001 - 预检失败也要让用户看到
        ok, reason = False, fmt_error(exc)
    text = fmt_boot_preset(preset, ok, reason)
    await _reply(update, text, reply_markup=boot_confirm_keyboard())


def _run_grabber_thread(task_id, cli, compartment_id, preset):
    """抢机线程体（daemon 线程里跑，不要在线程里直接调 Telegram API）。

    进度通过 on_event 回调写进任务 detail；结束时更新任务状态。
    成功后把本次配置追加保存到快捷开机（presets.json）。
    """
    def on_event(_event_type, message):
        TASKS.update(task_id, detail=message)

    grabber = Grabber(
        cli, compartment_id,
        preset["availability_domain"], preset["shape"],
        preset["image_id"], preset["subnet_id"],
        preset["display_name"], preset["ssh_keys_file"],
        target_cpu=preset.get("target_cpu"),
        on_event=on_event,
        stop_event=TASKS.stop_event(task_id))
    try:
        result = grabber.run()
    except GrabCancelledError:
        TASKS.update(task_id, status="cancelled", detail="抢机已取消。")
        return
    except QuotaExceededError as exc:
        TASKS.update(task_id, status="failed",
                     detail=f"配额不足，停止抢机：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要落到任务状态
        TASKS.update(task_id, status="failed", detail=f"抢机失败：{exc}")
        return
    TASKS.update(
        task_id, status="done",
        detail=(f"抢到实例 …{result.instance_id[-4:]}，"
                f"尝试 {result.attempts} 次，"
                f"释放 {result.releases} 台 CPU 不符，"
                f"总耗时 {fmt_duration(result.elapsed_seconds)}。"))
    save_preset(preset)


@require_allowed
async def boot_go_callback(update, context):
    """回调 "b:go"：注册抢机任务，开 daemon 线程跑 Grabber.run。"""
    preset = context.user_data.get("pending_boot")
    if not preset:
        await _reply(update, "请先选一个开机预设（菜单 1）。")
        return
    task_id = TASKS.register(
        "grab", detail=f"抢机准备中：{preset['display_name']}…")
    thread = threading.Thread(
        target=_run_grabber_thread,
        args=(task_id, get_cli(context), get_compartment(context), preset),
        daemon=True)
    thread.start()
    await _reply(
        update,
        f"🚀 抢机已启动，任务 ID {task_id}，"
        "用菜单 21（查看运行中的任务）查看进度。")


# ---- 20. 查询配额 ----

@require_allowed
async def handle_quota(update, context):
    """菜单 20：免费额度汇总。"""
    try:
        summary = await run_sync(get_free_tier_summary,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001 - 任何 OCI 异常都转成中文提示
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_quota_summary(summary, get_current_profile(context)),
                 reply_markup=back_to_oracle_keyboard())


# ---- 19. 近 3 月流量查询 / 25. 近 3 月花费查询 ----

@require_allowed
async def handle_traffic(update, context):
    """菜单 19：用量 API 按月汇总费用与出站流量。"""
    try:
        items = await run_sync(fetch_usage_summary,
                               get_cli(context), get_compartment(context),
                               "MONTHLY")
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_usage_monthly(items),
                 reply_markup=back_to_oracle_keyboard())


@require_allowed
async def handle_cost(update, context):
    """菜单 25：用量 API 汇总近 3 个月总费用。"""
    try:
        items = await run_sync(fetch_usage_summary,
                               get_cli(context), get_compartment(context),
                               "MONTHLY")
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_usage_total(items),
                 reply_markup=back_to_oracle_keyboard())


# ---- 18. 订阅信息 ----

@require_allowed
async def handle_subscription(update, context):
    """菜单 18：租户名称 / Home 区域 / 描述。"""
    try:
        tenancy = await run_sync(get_tenancy,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_tenancy(tenancy),
                 reply_markup=back_to_oracle_keyboard())


# ---- 15. 一键测活 ----

@require_allowed
async def handle_health(update, context):
    """菜单 15：遍历全部 profile，各查实例数与运行状态，异常单列。"""
    lines = ["❤️ 一键测活：", ""]
    for name in bot_config.PROFILES:
        try:
            cfg = bot_config.get_profile_config(name)
            cli = bot_config.get_oci_client(profile=name)
            instances = await run_sync(list_instances, cli,
                                       cfg["compartment_id"])
            running = sum(1 for i in instances
                          if i.get("lifecycle-state") == "RUNNING")
            lines.append(f"• {name}：{len(instances)} 台实例，"
                         f"{running} 台运行中 ✅")
        except Exception as exc:  # noqa: BLE001 - 单账号报错不影响其他账号
            lines.append(f"• {name}：{fmt_error(exc)}")
    await _reply(update, "\n".join(lines),
                 reply_markup=back_to_oracle_keyboard())


# ---- 21. 查看运行中的任务 ----

@require_allowed
async def handle_tasks(update, context):
    """菜单 21：任务列表 + 取消按钮。"""
    tasks = TASKS.list_all()
    await _reply(update, fmt_task_list(tasks),
                 reply_markup=task_list_keyboard(tasks))


@require_allowed
async def task_cancel_callback(update, context):
    """回调 "t:cancel:<id>"：取消任务。"""
    data = _callback_data(update)
    task_id = data.split(":")[-1] if ":" in data else ""
    if TASKS.cancel(task_id):
        await _reply(update, f"✅ 任务 {task_id} 已取消。")
    else:
        await _reply(update, f"⚠️ 任务 {task_id} 不存在或已结束，无法取消。")


# ---- 13. profile 管理 ----

@require_allowed
async def handle_profiles(update, context):
    """菜单 13：profile 切换键盘。"""
    current = get_current_profile(context)
    await _reply(update, f"当前账号（profile）：{current}\n点下面切换账号：",
                 reply_markup=profile_keyboard(list(bot_config.PROFILES),
                                               current))


@require_allowed
async def profile_callback(update, context):
    """回调 "p:<name>"：切换当前 profile。"""
    data = _callback_data(update)
    name = data.split(":", 1)[1] if ":" in data else ""
    try:
        set_current_profile(context, name)
    except ValueError as exc:
        await _reply(update, f"❌ 切换失败：{exc}")
        return
    await _reply(update, f"✅ 已切换到账号（profile）：{name}")


# ---- 2. ip 管理 ----

@require_allowed
async def handle_changeip_prompt(update, context):
    """实例操作“换 IP”的确认提示（dispatch 项 "changeip"）。

    由 handlers.py 的 "i:ip:<idx>" 回调转发（pending_idx 已写入）。
    显示当前公网 IP + 警告，点确认后走 handle_confirm("changeip")。
    注意与菜单 2 的 handle_ip（IP 总览）不同：总览里不挂换 IP 按钮。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "请先在实例列表里选一台实例，"
                     "再点“换 IP”。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or "?"
    try:
        current_ip = await run_sync(get_public_ip, get_cli(context),
                                    inst["id"])
    except Exception:  # noqa: BLE001 - 查不到当前 IP 也不阻塞换 IP
        current_ip = None
    text = (f"🔁 确认给实例 {name} 换公网 IP？\n\n"
            f"当前 IP：{current_ip or '无公网 IP'}\n"
            "⚠️ 换 IP 后旧 IP 立即释放，无法找回；"
            "请同步更新 DNS 记录、反代与客户端配置。")
    await _reply(update, text,
                 reply_markup=confirm_keyboard("changeip", idx, "🔁 确认换 IP"))


@require_allowed
async def handle_ip(update, context):
    """菜单 2：保留 IP 列表 + 各实例公网 IP。

    换 IP 流程：实例详情里点“换 IP”→ handle_changeip_prompt 弹确认
    → handle_confirm("changeip") 执行。本总览里不挂换 IP 按钮。
    """
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        reserved = await run_sync(list_public_ips, cli, compartment)
        instances = await run_sync(list_instances, cli, compartment)
        instance_ips = []
        for inst in instances:
            if inst.get("lifecycle-state") != "RUNNING":
                continue
            ip = await run_sync(get_public_ip, cli, inst["id"])
            instance_ips.append((inst.get("display-name") or "?", ip))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_ip_overview(reserved, instance_ips),
                 reply_markup=back_to_oracle_keyboard())


# ---- 4. ipv6 管理 ----

@require_allowed
async def handle_ipv6(update, context):
    """菜单 4：按实例查 IPv6 列表（主 VNIC -> ipv6 list）。

    创建 / 删除 IPv6 只提供函数（create_ipv6 / delete_ipv6，mock 测试），
    菜单里暂不挂按钮——IPv6 分配依赖子网已启用 IPv6，误操作会让实例
    多出不可达地址，留给后续版本再接交互流程。
    """
    cli = get_cli(context)
    try:
        instances = await run_sync(list_instances, cli,
                                   get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    lines = ["🌐 IPv6 管理：", ""]
    any_running = False
    for inst in instances:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        any_running = True
        name = inst.get("display-name") or "?"
        try:
            vnics = await run_sync(list_vnics, cli, inst["id"])
            primary = next((v for v in vnics if v.get("is-primary")), None)
            if primary is None:
                lines.append(f"• {name}：无主 VNIC")
                continue
            ipv6s = await run_sync(list_ipv6_for_vnic, cli, primary["id"])
            if not ipv6s:
                lines.append(f"• {name}：无 IPv6")
            else:
                addrs = ", ".join(v.get("ip-address", "?") for v in ipv6s)
                lines.append(f"• {name}：{addrs}")
        except Exception as exc:  # noqa: BLE001 - 单台报错不影响其他台
            lines.append(f"• {name}：{fmt_error(exc)}")
    if not any_running:
        lines.append("没有运行中的实例。")
    await _reply(update, "\n".join(lines),
                 reply_markup=back_to_oracle_keyboard())


# ---- 3. 被暂停开机 ----

@require_allowed
async def handle_resume(update, context):
    """菜单 3：列出 STOPPED 状态实例，点进详情可启动。"""
    try:
        instances = await run_sync(list_instances,
                                   get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    stopped = [i for i in instances
               if i.get("lifecycle-state") == "STOPPED"]
    if not stopped:
        await _reply(update, "没有被暂停（STOPPED）的实例。",
                     reply_markup=back_to_oracle_keyboard())
        return
    store_instances(context, stopped)
    await _reply(update, fmt_instance_list(stopped, get_current_profile(context)),
                 reply_markup=instance_list_keyboard(stopped))


# ---- 5. 升/降级实例 ----

@require_allowed
async def handle_resize(update, context):
    """菜单 5：从 user_data["pending_idx"] 取实例。

    E2 固定规格直接回复不可升降配；A1 显示当前配置 + 预设按钮。
    pending_idx 由 handlers.py 在用户从实例列表选实例后写入。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "请先在实例列表里选一台实例，"
                     "再从菜单选 5. 升/降级实例。",
                     reply_markup=back_to_oracle_keyboard())
        return
    shape = inst.get("shape") or ""
    name = inst.get("display-name") or "?"
    if shape == SHAPE_E2_MICRO:
        await _reply(update,
                     f"🖥️ {name} 是 E2.1.Micro（固定 1 核 / 1GB），"
                     "不支持升/降配。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if shape != SHAPE_A1_FLEX:
        await _reply(update,
                     f"🖥️ {name} 的规格是 {shape}，"
                     "本机器人只支持 A1.Flex 升/降配。",
                     reply_markup=back_to_oracle_keyboard())
        return
    current = "未知"
    try:
        full = await run_sync(get_instance, get_cli(context), inst["id"])
        cfg = (_unwrap(full) or {}).get("shape-config") or {}
        if cfg.get("ocpus") is not None:
            current = f"{cfg.get('ocpus')} 核 / {cfg.get('memory-in-gbs')}GB"
    except Exception:  # noqa: BLE001 - 查不到当前配置也不阻塞选预设
        pass
    await _reply(update,
                 f"📐 {name} 当前配置：{current}\n选目标规格：",
                 reply_markup=resize_preset_keyboard(idx))


@require_allowed
async def resize_preset_callback(update, context):
    """回调 "r:<ocpus>:<mem>:<idx>"：存目标规格，弹二次确认键盘。"""
    parts = _callback_data(update).split(":")
    if len(parts) != 4:
        await _reply(update, "参数错误，请重新选择。")
        return
    _, ocpus, memory_gb, idx = parts
    try:
        target = {"ocpus": int(ocpus), "memory_gb": int(memory_gb)}
    except ValueError:
        await _reply(update, "参数错误，请重新选择。")
        return
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "实例不存在或已过期，请重新打开实例列表。")
        return
    context.user_data["pending_resize"] = target
    context.user_data["pending_idx"] = idx
    name = inst.get("display-name") or "?"
    await _reply(
        update,
        f"⚠️ 确认把 {name} 改为 {target['ocpus']} 核 / "
        f"{target['memory_gb']}GB？\n改规格时实例可能会重启。",
        reply_markup=confirm_keyboard("resize", idx, "✅ 确认改规格"))


# ---- 7. 磁盘管理 ----

@require_allowed
async def handle_volumes(update, context):
    """菜单 7：启动盘列表 + 孤儿盘 + 总容量。"""
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        volumes = await run_sync(list_boot_volumes, cli, compartment)
        orphans = await run_sync(list_orphan_boot_volumes, cli, compartment)
        total = await run_sync(total_boot_volume_gb, cli, compartment)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    store_volumes(context, volumes)
    await _reply(update, fmt_volume_list(volumes, orphans, total),
                 reply_markup=volume_list_keyboard(volumes))


@require_allowed
async def volume_detail_callback(update, context):
    """回调 "v:detail:<idx>"：磁盘详情 + 扩容/删除按钮。"""
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    try:
        detail = await run_sync(get_boot_volume, get_cli(context), vol["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_volume_detail(detail),
                 reply_markup=volume_actions_keyboard(idx))


@require_allowed
async def volume_resize_callback(update, context):
    """回调 "v:resize:<idx>"：显示扩容目标容量按钮（100/200GB）。"""
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    name = vol.get("display-name") or "?"
    await _reply(update,
                 f"📐 {name} 当前 {vol.get('size-in-gbs')}GB，"
                 "选目标容量（只能扩不能缩）：",
                 reply_markup=volume_resize_keyboard(idx))


@require_allowed
async def volume_size_callback(update, context):
    """回调 "v:volsize:<idx>:<gb>"：执行扩容。"""
    parts = _callback_data(update).split(":")
    if len(parts) != 4:
        await _reply(update, "参数错误，请重新选择。")
        return
    _, _, idx, size = parts
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    try:
        target_gb = int(size)
    except ValueError:
        await _reply(update, "参数错误，请重新选择。")
        return
    current_gb = vol.get("size-in-gbs") or 0
    if target_gb <= current_gb:
        await _reply(update,
                     f"目标容量 {target_gb}GB 必须大于当前容量 {current_gb}GB，"
                     "启动盘只能扩不能缩。")
        return
    try:
        await run_sync(resize_boot_volume, get_cli(context),
                       vol["id"], target_gb)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    name = vol.get("display-name") or "?"
    await _reply(update,
                 f"✅ {name} 已扩容到 {target_gb}GB。\n"
                 "注意：系统里还要跑 resize2fs（ext4）扩文件系统，"
                 "df 看到的容量才会变大。")


@require_allowed
async def volume_delete_callback(update, context):
    """回调 "v:delete:<idx>"：删除警告 + 二次确认键盘。

    真正执行在 handle_confirm("delvol")。
    """
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    name = vol.get("display-name") or "?"
    await _reply(
        update,
        f"⚠️ 即将删除启动盘 {name}（{vol.get('size-in-gbs')}GB）！\n"
        "删除后数据永久丢失，无法恢复。\n"
        "挂在实例上的盘删不掉，必须先终止实例（或先分离）。",
        reply_markup=confirm_keyboard("delvol", idx, "🗑️ 确认删除"))


# ---- 8. 云账户管理 / 30. 批量查询邮箱 ----

@require_allowed
async def handle_users(update, context):
    """菜单 8：只读列出 IAM 用户。"""
    try:
        users = await run_sync(list_users,
                               get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_user_list(users),
                 reply_markup=back_to_oracle_keyboard())


@require_allowed
async def handle_emails(update, context):
    """菜单 30：列出全部用户的邮箱。"""
    try:
        users = await run_sync(list_users,
                               get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_email_list(users),
                 reply_markup=back_to_oracle_keyboard())


# ---- 12. 工作流错误 ----

@require_allowed
async def handle_workflows(update, context):
    """菜单 12：列最近的工作请求，FAILED 的标 ❌。"""
    try:
        requests = await run_sync(list_work_requests,
                                  get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_work_requests(requests),
                 reply_markup=back_to_oracle_keyboard())


# ---- 33. 自动拓展子区域（列已订阅区域） ----

@require_allowed
async def handle_regions(update, context):
    """菜单 33：列出已订阅区域。

    注：R-Bot 的“自动拓展子区域”是订阅新区域；订阅区域是租户级
    操作且不可逆，本版本只做只读列表，不提供一键订阅。
    """
    try:
        regions = await run_sync(list_regions,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_regions(regions),
                 reply_markup=back_to_oracle_keyboard())


# ---- 22. 查看客户端负载 ----

@require_allowed
async def handle_load(update, context):
    """菜单 22：本机负载与内存（读 /proc，不调 OCI）。"""
    await _reply(update, fmt_local_load(read_local_load()),
                 reply_markup=back_to_oracle_keyboard())


# ---- 34. 查询最新日志 ----

@require_allowed
async def handle_logs(update, context):
    """菜单 34：读机器人日志尾部 30 行，代码块回复。"""
    tail = read_log_tail()
    if tail is None:
        await _reply(update,
                     f"日志文件不存在：{bot_config.LOG_FILE}\n"
                     "机器人还没写过日志。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if not tail.strip():
        await _reply(update, "日志文件为空。",
                     reply_markup=back_to_oracle_keyboard())
        return
    text = tail[-LOG_MAX_CHARS:].replace("`", "'")
    await _reply(update, f"📋 最新日志（尾部 {LOG_TAIL_LINES} 行）：\n\n```\n{text}\n```",
                 reply_markup=back_to_oracle_keyboard(),
                 parse_mode="Markdown")


# ---- 17. 快捷开机 ----

@require_allowed
async def handle_presets(update, context):
    """菜单 17：列出已保存的快捷开机配置。

    抢机成功后自动保存本次配置（见 _run_grabber_thread）。
    """
    presets = load_presets()
    if not presets:
        await _reply(update,
                     "还没有保存的快捷开机配置。\n"
                     "抢机成功后会自动保存本次配置到这里。",
                     reply_markup=back_to_oracle_keyboard())
        return
    lines = [f"⚡ 快捷开机配置（{len(presets)} 条）：", ""]
    for preset in presets:
        shape = (preset.get("shape") or "?").replace("VM.Standard.", "")
        lines.append(f"• {preset.get('display_name', '?')}：{shape} "
                     f"（{preset.get('ocpus')} 核 / "
                     f"{preset.get('memory_gb')}GB）")
    await _reply(update, "\n".join(lines),
                 reply_markup=back_to_oracle_keyboard())


# ---- 9. 实例状态监控通知 / 10. 实例状态监控自启 / 29. 每日报告 ----

def _monitor_state(context):
    """bot_data 里的监控开关（job 不属于某个用户，不能存 user_data）。

    结构：bot_data["monitor"] = {"on": bool, "autostart": bool,
    "chat_id": int|None}。
    """
    return context.bot_data.setdefault(
        "monitor", {"on": False, "autostart": False, "chat_id": None})


def _daily_state(context):
    """bot_data 里的每日报告开关。

    结构：bot_data["daily_report"] = {"on": bool, "chat_id": int|None}。
    """
    return context.bot_data.setdefault(
        "daily_report", {"on": False, "chat_id": None})


@require_allowed
async def handle_monitor(update, context):
    """菜单 9：开关实例状态监控通知（状态变化时发消息）。

    切换后附带刷新后的 /oracle 菜单，按钮上的开关状态同步更新。
    """
    state = _monitor_state(context)
    state["on"] = not state["on"]
    if update.effective_chat is not None:
        state["chat_id"] = update.effective_chat.id
    await _reply(update,
                 f"🔔 实例状态监控通知已{'开启' if state['on'] else '关闭'}。"
                 + ("（每 10 分钟检查一次）" if state["on"] else ""),
                 reply_markup=oracle_menu_keyboard(
                     0, states=get_menu_states(context)))


@require_allowed
async def handle_autostart(update, context):
    """菜单 10：开关监控自启（发现 STOPPED 的实例自动启动）。

    开自启时一并开监控——job 先判断监控开关，监控关了自启不会跑。
    切换后附带刷新后的 /oracle 菜单，按钮上的开关状态同步更新。
    """
    state = _monitor_state(context)
    state["autostart"] = not state["autostart"]
    if state["autostart"]:
        state["on"] = True
    if update.effective_chat is not None:
        state["chat_id"] = update.effective_chat.id
    if state["autostart"]:
        text = "▶️ 实例状态监控自启已开启（监控通知也已一并开启）。"
    else:
        text = "⏸️ 实例状态监控自启已关闭（监控通知保持原状态）。"
    await _reply(update, text,
                 reply_markup=oracle_menu_keyboard(
                     0, states=get_menu_states(context)))


@require_allowed
async def handle_daily(update, context):
    """菜单 29：开关每日报告（每天发配额 + 费用摘要）。"""
    state = _daily_state(context)
    state["on"] = not state["on"]
    if update.effective_chat is not None:
        state["chat_id"] = update.effective_chat.id
    await _reply(update,
                 f"📰 每日报告已{'开启' if state['on'] else '关闭'}。"
                 + ("（每天发一次配额 + 费用摘要）" if state["on"] else ""))


# ---- 监控增强（阶段 5 工作包 C）：流量守护 / 停机保活 / SSL 监控 ----

def get_menu_states(context):
    """取 /oracle 菜单按钮的动态开关状态（handlers.py 生成菜单时调用）。

    返回 {"monitor", "autostart", "traffic_guard", "keepalive"}，
    值都是 bool。流量守护读文件配置；停机保活为 True 当且仅当
    至少一台实例开启了保活。
    """
    monitor = context.bot_data.get("monitor") or {}
    guard = load_traffic_guard()
    keepalive = load_keepalive()
    return {
        "monitor": bool(monitor.get("on")),
        "autostart": bool(monitor.get("autostart")),
        "traffic_guard": bool(guard.get("enabled")),
        "keepalive": bool(keepalive_enabled_instances(keepalive)),
    }


def fmt_traffic_guard(cfg, used_gb=None):
    """流量守护菜单全文。used_gb 为 None 表示本月流量查询失败。"""
    lines = ["🛡️ 流量守护：", ""]
    lines.append(f"状态：{'✅ 开启' if cfg.get('enabled') else '⬜ 关闭'}")
    lines.append(f"阈值：{cfg.get('threshold_gb')}GB / 月")
    action = "自动关机" if cfg.get("action") == "shutdown" else "仅通知"
    lines.append(f"超阈值动作：{action}")
    if used_gb is not None:
        lines.append(f"本月已用出站流量：{used_gb:.2f}GB")
    else:
        lines.append("本月已用出站流量：查询失败")
    last = cfg.get("last_triggered")
    if last:
        lines.append(f"上次触发：{str(last)[:19].replace('T', ' ')}")
    lines.append("")
    lines.append("阈值默认 10240GB（OCI 免费出站流量额度）；"
                "触发后 24 小时内不再重复执行。")
    return "\n".join(lines)


@require_allowed
async def handle_traffic_guard(update, context):
    """菜单 37：流量守护——开关、阈值、动作。

    本月流量复用用量 API（与菜单 19 同一查询方式），查不到也不影响改配置。
    """
    cfg = load_traffic_guard()
    used_gb = None
    try:
        items = await run_sync(fetch_usage_summary,
                               get_cli(context), get_compartment(context),
                               "MONTHLY", 1)
        used_gb = current_month_transfer_gb(items)
    except Exception:  # noqa: BLE001 - 流量查不到也不影响改配置
        used_gb = None
    await _reply(update, fmt_traffic_guard(cfg, used_gb),
                 reply_markup=traffic_guard_keyboard(cfg))


@require_allowed
async def traffic_guard_callback(update, context):
    """回调 "g:toggle" / "g:action" / "g:threshold:<gb>"。

    每次操作都记录通知 chat_id（job 触发时往这里发消息），
    保存后重新渲染菜单。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    cfg = load_traffic_guard()
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    if action == "toggle":
        cfg["enabled"] = not cfg.get("enabled")
    elif action == "action":
        cfg["action"] = ("notify" if cfg.get("action") == "shutdown"
                         else "shutdown")
    elif action == "threshold" and len(parts) == 3:
        try:
            threshold = int(parts[2])
        except ValueError:
            await _reply(update, "阈值参数错误，请重新选择。")
            return
        if threshold <= 0:
            await _reply(update, "阈值必须大于 0，请重新选择。")
            return
        cfg["threshold_gb"] = threshold
    else:
        await _reply(update, "参数错误，请重新打开流量守护菜单。")
        return
    save_traffic_guard(cfg)
    await handle_traffic_guard(update, context)


def fmt_keepalive(instances, cfg, state):
    """停机保活菜单全文。"""
    lines = ["💤 停机保活：", "",
             "开启的实例停机（STOPPED）时，监控会自动启动它；",
             "连续 3 次开机失败后停止尝试，需手动重新开启。", ""]
    if not instances:
        lines.append("当前账号下没有实例。")
        return "\n".join(lines)
    gave_up = set((state or {}).get("gave_up") or [])
    failures = (state or {}).get("failures") or {}
    for inst in instances:
        ocid = inst.get("id")
        name = inst.get("display-name") or "?"
        status = inst.get("lifecycle-state", "?")
        mark = "✅" if cfg.get(ocid) else "⬜"
        extra = ""
        if ocid in gave_up:
            extra = " ⚠️连续 3 次开机失败，已停试"
        elif failures.get(ocid):
            extra = f"（开机失败 {failures[ocid]} 次）"
        lines.append(f"{mark} {name}（{status}）{extra}")
    return "\n".join(lines)


def _keepalive_menu_text_and_keyboard(context):
    """由已存的实例列表 + 配置拼停机保活菜单（不调 OCI）。"""
    instances = context.user_data.get("instances") or []
    cfg = load_keepalive()
    state = load_keepalive_state()
    return (fmt_keepalive(instances, cfg, state),
            keepalive_keyboard(instances, cfg, state))


@require_allowed
async def handle_keepalive(update, context):
    """菜单 38：停机保活——列实例 + 每台开关。"""
    try:
        instances = await run_sync(list_instances,
                                   get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    store_instances(context, instances)
    text, keyboard = _keepalive_menu_text_and_keyboard(context)
    await _reply(update, text, reply_markup=keyboard)


@require_allowed
async def keepalive_callback(update, context):
    """回调 "k:toggle:<idx>"：切换某台实例的停机保活开关。

    开 / 关都清掉该实例的失败计数与停试标记（重新开启即重新尝试），
    并记录通知 chat_id。
    """
    parts = _callback_data(update).split(":")
    if len(parts) != 3 or parts[1] != "toggle":
        await _reply(update, "参数错误，请重新打开停机保活菜单。")
        return
    inst = resolve_instance(context, parts[2])
    if inst is None:
        await _reply(update, "实例不存在或已过期，请重新打开停机保活菜单。")
        return
    instance_id = inst.get("id")
    cfg = load_keepalive()
    cfg[instance_id] = not cfg.get(instance_id, False)
    save_keepalive(cfg)
    state = load_keepalive_state()
    keepalive_reset_instance(state, instance_id)
    if update.effective_chat is not None:
        state["chat_id"] = update.effective_chat.id
    save_keepalive_state(state)
    text, keyboard = _keepalive_menu_text_and_keyboard(context)
    await _reply(update, text, reply_markup=keyboard)


def fmt_ssl_watch(results):
    """SSL 监控菜单全文。results 是 [(域名, check_ssl_expiry 返回值或 None)]。"""
    lines = ["🔒 SSL 监控：", ""]
    if not results:
        lines.append("未监控任何域名，点“➕ 添加域名”开始监控。")
    for domain, result in results:
        if result is None:
            lines.append(f"• {domain}：检查失败")
            continue
        days = result["days_left"]
        expires = result["expires"]
        if days < 0:
            lines.append(f"• {domain}：⚠️ 已过期 {-days} 天！（{expires}）")
        elif days == 0:
            lines.append(f"• {domain}：⚠️ 今天到期！（{expires}）")
        else:
            lines.append(f"• {domain}：还有 {days} 天到期（{expires}）")
    lines.append("")
    lines.append("到期前 30/14/7/1 天会各提醒一次。")
    return "\n".join(lines)


@require_allowed
async def handle_sslwatch(update, context):
    """菜单 39：SSL 监控——各域名剩余天数 + 添加/删除。"""
    cfg = load_ssl_watch()
    results = []
    for domain in cfg.get("domains") or []:
        if check_ssl_expiry is None:
            results.append((domain, None))
            continue
        try:
            results.append((domain, await run_sync(check_ssl_expiry, domain)))
        except Exception:  # noqa: BLE001 - 单个域名失败显示为检查失败
            results.append((domain, None))
    await _reply(update, fmt_ssl_watch(results),
                 reply_markup=ssl_watch_keyboard(cfg.get("domains") or []))


@require_allowed
async def ssl_watch_callback(update, context):
    """回调 "s:add" / "s:add_cancel" / "s:del:<idx>" / "s:refresh"。

    添加域名走 text_input_callback（pending_input kind="ssl_domain"）。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "add":
        context.user_data["pending_input"] = {"kind": "ssl_domain"}
        await _reply(update,
                     "请发送要监控的域名（例如 example.com）：\n"
                     "直接发文字消息即可。",
                     reply_markup=ssl_add_cancel_keyboard())
        return
    if action == "add_cancel":
        context.user_data.pop("pending_input", None)
        await handle_sslwatch(update, context)
        return
    if action == "del" and len(parts) == 3:
        cfg = load_ssl_watch()
        try:
            idx = int(parts[2])
        except ValueError:
            idx = -1
        domains = cfg.get("domains") or []
        if 0 <= idx < len(domains):
            removed = domains.pop(idx)
            (cfg.get("reminded") or {}).pop(removed, None)
            if update.effective_chat is not None:
                cfg["chat_id"] = update.effective_chat.id
            save_ssl_watch(cfg)
        await handle_sslwatch(update, context)
        return
    if action == "refresh":
        await handle_sslwatch(update, context)
        return
    await _reply(update, "参数错误，请重新打开 SSL 监控菜单。")


async def _handle_ssl_domain_input(update, context, text):
    """text_input_callback 的 ssl_domain 分支：添加监控域名。

    域名格式校验 + 去重；添加成功后重新渲染 SSL 监控菜单
    （新域名的剩余天数在菜单里直接显示）。
    """
    domain = normalize_domain(text)
    if not is_valid_domain(domain):
        await _reply(update,
                     f"域名格式不正确：{text}\n"
                     "请重新点“➕ 添加域名”再试（例如 example.com）。",
                     reply_markup=back_to_oracle_keyboard())
        return
    cfg = load_ssl_watch()
    domains = cfg.get("domains") or []
    if domain in domains:
        await _reply(update, f"{domain} 已在 SSL 监控列表里。",
                     reply_markup=ssl_watch_keyboard(domains))
        return
    domains.append(domain)
    cfg["domains"] = domains
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    save_ssl_watch(cfg)
    await handle_sslwatch(update, context)


# ---- 26. 清除所有 2FA 设备 ----

@require_allowed
async def handle_mfa(update, context):
    """菜单 26：列出全部用户的 MFA TOTP 设备 + 双重确认删除。"""
    try:
        devices = await run_sync(list_mfa_devices,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    if not devices:
        await _reply(update, "没有任何 MFA TOTP 设备，无需清除。",
                     reply_markup=back_to_oracle_keyboard())
        return
    await _reply(
        update,
        fmt_mfa_devices(devices)
        + "\n\n⚠️ 点确认将删除上面全部 MFA 设备，"
          "用户下次登录控制台需重新绑定验证器。",
        reply_markup=confirm_keyboard("delmfa", 0, "🔐 确认清除全部 MFA"))


# ---- 35. 批量操作 ----

def _batch_select_text(instances, selected):
    """批量操作多选页的正文（显示已选台数）。"""
    return ("🔀 批量操作：点实例切换选中状态，选好后点「下一步」。\n\n"
            f"已选 {len(selected)} 台。")


@require_allowed
async def handle_batch(update, context):
    """菜单 35：批量操作——拉实例列表，显示多选键盘。

    流程：多选实例（ba:toggle）→ 下一步选动作（ba:next/ba:action）
    → 二次确认（c:batch*）→ handle_confirm 执行并汇报每台结果。
    """
    try:
        instances = await run_sync(list_instances, get_cli(context),
                                   get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    if not instances:
        await _reply(update, "该账号下没有实例，无需批量操作。",
                     reply_markup=back_to_oracle_keyboard())
        return
    store_instances(context, instances)
    context.user_data["batch_selected"] = set()
    await _reply(update, _batch_select_text(instances, set()),
                 reply_markup=batch_select_keyboard(instances, set()))


@require_allowed
async def batch_toggle_callback(update, context):
    """回调 "ba:toggle:<idx>"：切换选中状态，编辑消息刷新键盘。"""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - answer 失败不影响切换
            pass
    parts = _callback_data(update).split(":")
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "实例不存在或已过期，请重新打开批量操作。")
        return
    selected = context.user_data.setdefault("batch_selected", set())
    idx_int = int(idx)
    if idx_int in selected:
        selected.discard(idx_int)
    else:
        selected.add(idx_int)
    instances = context.user_data.get("instances") or []
    text = _batch_select_text(instances, selected)
    keyboard = batch_select_keyboard(instances, selected)
    if query is not None and query.message is not None:
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
            return
        except Exception:  # noqa: BLE001 - 编辑失败时改发新消息
            pass
    await _reply(update, text, reply_markup=keyboard)


@require_allowed
async def batch_next_callback(update, context):
    """回调 "ba:next"：有选中时显示动作选择键盘。"""
    selected = context.user_data.get("batch_selected") or set()
    if not selected:
        await _reply(update, "请先选至少一台实例（点实例名切换选中）。")
        return
    names = []
    for idx in sorted(selected):
        inst = resolve_instance(context, idx)
        if inst is not None:
            names.append(inst.get("display-name") or "?")
    if not names:
        await _reply(update, "选中的实例已过期，请重新打开批量操作。")
        return
    await _reply(update,
                 "已选实例：\n" + "\n".join(f"• {name}" for name in names)
                 + "\n\n选批量动作：",
                 reply_markup=batch_action_keyboard())


@require_allowed
async def batch_action_callback(update, context):
    """回调 "ba:action:<act>"：显示二次确认键盘。

    确认后走 handle_confirm("batch<act>")，idx 无用，传 0。
    """
    parts = _callback_data(update).split(":")
    action = parts[2] if len(parts) >= 3 else ""
    if action not in _BATCH_ACTION_TEXT:
        await _reply(update, "未知的批量动作，请重新选择。")
        return
    selected = context.user_data.get("batch_selected") or set()
    instances = [resolve_instance(context, idx) for idx in sorted(selected)]
    instances = [inst for inst in instances if inst is not None]
    if not instances:
        await _reply(update, "选中的实例已过期，请重新打开批量操作。")
        return
    context.user_data["pending_batch_action"] = action
    names = "\n".join(f"• {inst.get('display-name') or '?'}"
                      for inst in instances)
    verb = _BATCH_ACTION_TEXT[action]
    if action == "terminate":
        await _reply(
            update,
            f"⚠️ 确认批量删除以下 {len(instances)} 台实例？\n\n{names}\n\n"
            "<b>删除后将永久删除各实例的引导卷，数据无法恢复！</b>",
            parse_mode="HTML",
            reply_markup=confirm_keyboard("batchterminate", 0,
                                          "🗑️ 确认批量删除"))
        return
    await _reply(update,
                 f"确认批量{verb}以下 {len(instances)} 台实例？\n\n{names}",
                 reply_markup=confirm_keyboard("batch" + action, 0,
                                               f"✅ 确认批量{verb}"))


def fmt_batch_result(verb, result, names):
    """批量操作结果全文：成功列表 + 失败列表（含错误原因）。

    names 是 {instance_id: display_name}，供按 id 查名字。
    """
    succeeded = result.get("succeeded") or []
    failed = result.get("failed") or []
    lines = [f"🔀 批量{verb}完成：成功 {len(succeeded)} 台，"
             f"失败 {len(failed)} 台。", ""]
    for instance_id in succeeded:
        lines.append(f"✅ {names.get(instance_id, instance_id)}")
    for entry in failed:
        entry_id = entry.get("id")
        lines.append(f"❌ {names.get(entry_id, entry_id)}："
                     f"{entry.get('error', '?')}")
    return "\n".join(lines)


# ---- 实例改名 / 重装系统 / 串行控制台（实例详情操作） ----

@require_allowed
async def handle_rename_prompt(update, context):
    """实例操作“改名”的文字输入提示（dispatch 项 "rename"）。

    由 handlers.py 的 "i:rename:<idx>" 回调转发（pending_idx 已写入）。
    用户回复文字后由 text_input_callback 处理，再弹二次确认。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "请先在实例列表里选一台实例，再点“改名”。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or "?"
    context.user_data["pending_input"] = {"kind": "rename", "idx": idx}
    await _reply(update,
                 f"✏️ 请回复这条消息，输入实例「{name}」的新名字：\n"
                 "（直接发文字即可，不用加任何命令）")


@require_allowed
async def handle_reinstall_prompt(update, context):
    """实例操作“重装系统”的确认提示（dispatch 项 "reinstall"）。

    由 handlers.py 的 "i:reinstall:<idx>" 回调转发。用户需回复
    完整实例名（防误点），再弹二次确认，走 handle_confirm("reinstall")。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "请先在实例列表里选一台实例，再点“重装系统”。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or "?"
    context.user_data["pending_input"] = {"kind": "reinstall", "idx": idx}
    await _reply(update,
                 f"⚠️ 重装系统将清空实例「{name}」系统盘的全部数据，"
                 "不可恢复；实例 OCID 与 IP 地址保留不变。\n\n"
                 f"请回复这条消息，完整输入实例名「{name}」确认重装：")


@require_allowed
async def handle_console(update, context):
    """实例操作“串行控制台”（dispatch 项 "console"）。

    由 handlers.py 的 "i:console:<idx>" 回调转发。创建串行控制台
    连接并返回 ssh 连接命令（只读展示，无需二次确认）。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "请先在实例列表里选一台实例，再点“串行控制台”。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or "?"
    try:
        console = await run_sync(get_serial_console, get_cli(context),
                                 inst["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_serial_console(name, console),
                 reply_markup=back_to_oracle_keyboard())


def fmt_serial_console(name, console):
    """串行控制台连接信息全文（连接命令单独成行，方便复制）。"""
    lines = [f"🖥️ {name} 的串行控制台：", ""]
    lines.append("连接命令：")
    lines.append(console.get("connection-string") or "（未返回连接命令）")
    state = console.get("lifecycle-state")
    if state:
        lines.append(f"状态：{state}")
    fingerprint = console.get("fingerprint")
    if fingerprint:
        lines.append(f"指纹：{fingerprint}")
    lines.append("")
    lines.append("用实例的 SSH 密钥连接；首次连接需接受主机密钥。")
    return "\n".join(lines)


@require_allowed
async def text_input_callback(update, context):
    """处理用户回复的纯文字输入（改名 / 重装确认 / SSL 添加域名 /
    Email 发件人）。

    只在 user_data["pending_input"] 存在时处理；否则忽略
    （机器人平时不处理自由文字）。改名：校验后弹二次确认；
    重装：输入必须与实例名完全一致，否则取消；
    SSL 域名：校验格式后加入监控列表；
    Email 发件人：校验邮箱格式后弹二次确认。
    """
    pending = context.user_data.get("pending_input")
    if not pending:
        return
    text = (update.effective_message.text or "").strip()
    kind = pending.get("kind")
    if kind == "ssl_domain":
        # SSL 监控添加域名：不关联实例，直接处理。
        context.user_data.pop("pending_input", None)
        await _handle_ssl_domain_input(update, context, text)
        return
    if kind == "email_sender":
        # Email 发件人：不关联实例，校验邮箱格式后弹二次确认。
        context.user_data.pop("pending_input", None)
        if not is_valid_email(text):
            await _reply(update,
                         "❌ 邮箱格式不对，请重新从菜单进入并发送正确的"
                         "邮箱地址（例如 noreply@example.com）。")
            return
        context.user_data["pending_sender"] = text
        await _reply(update,
                     f"📧 确认创建发件人 {text}？\n"
                     "创建后 OCI 会向该邮箱发送验证邮件，"
                     "需点击链接后才能使用。",
                     reply_markup=confirm_keyboard(
                         "createsender", 0, "📧 确认创建发件人"))
        return
    idx = pending.get("idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        context.user_data.pop("pending_input", None)
        await _reply(update, "实例不存在或已过期，请重新打开实例列表。")
        return
    name = inst.get("display-name") or "?"
    if kind == "rename":
        context.user_data.pop("pending_input", None)
        if not text:
            await _reply(update, "新实例名不能为空，改名已取消。")
            return
        if len(text) > MAX_DISPLAY_NAME_LEN:
            await _reply(update,
                         f"新实例名不能超过 {MAX_DISPLAY_NAME_LEN} 字符，"
                         "改名已取消。")
            return
        context.user_data["pending_rename_name"] = text
        await _reply(update,
                     f"确认把实例「{name}」改名为「{text}」？",
                     reply_markup=confirm_keyboard("rename", idx,
                                                   "✅ 确认改名"))
        return
    if kind == "reinstall":
        context.user_data.pop("pending_input", None)
        if text != name:
            await _reply(update,
                         "输入的实例名与实际不符，重装已取消。\n"
                         "请重新点“重装系统”再试。")
            return
        await _reply(update,
                     f"⚠️ 最后确认：重装实例「{name}」？\n"
                     "系统盘数据将全部清空，不可恢复；"
                     "实例 OCID 与 IP 地址保留不变。",
                     reply_markup=confirm_keyboard("reinstall", idx,
                                                   "💿 确认重装"))
        return
    context.user_data.pop("pending_input", None)


# ---- 36. A1 配置体检 ----

@require_allowed
async def handle_a1_health(update, context):
    """菜单 36：A1 配置体检（超限检查 + 降配建议 + 启动盘检查）。"""
    try:
        result = await run_sync(a1_health_check,
                                get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    await _reply(update, fmt_a1_health(result, get_current_profile(context)),
                 reply_markup=back_to_oracle_keyboard())


def fmt_a1_health(result, profile):
    """A1 体检结果全文（每条建议按 severity 标符号）。"""
    suggestions = result.get("suggestions") or []
    overall = "✅ 未发现问题" if result.get("overall_ok") else "⚠️ 发现问题"
    lines = [f"[{profile}] A1 配置体检（{overall}）：", ""]
    for item in suggestions:
        mark = _SEVERITY_MARK.get(item.get("severity"), "•")
        lines.append(f"{mark} {item.get('message', '')}")
    return "\n".join(lines)


# ================= 二次确认执行（"c:<action>:<idx>"） =================

async def handle_confirm(action, idx, update, context):
    """执行二次确认后的危险操作。

    由 handlers.py 的 "c:<action>:<idx>" 回调转发（该回调已过白名单，
    本函数参数顺序与 handler 不同，不加 @require_allowed）。
    返回 True 表示已处理，False 表示未知 action。

    action：
      - changeip：换实例公网 IP（旧 IP 释放，DNS 需同步更新）。
      - resize：A1 升/降配（目标规格在 user_data["pending_resize"]）。
      - delvol：删除启动盘。
      - delmfa：删除全部 MFA TOTP 设备（idx 无用，传 0）。
      - rename：改实例名（新名在 user_data["pending_rename_name"]）。
      - reinstall：重装系统（用当前镜像重建引导卷）。
      - batchstart/batchstop/batchrestart/batchterminate：批量操作
        （选中实例在 user_data["batch_selected"]，idx 无用，传 0）。
      - delbucket：删除 Bucket（idx 是 user_data["os_buckets"] 的下标；
        Bucket 必须为空，否则 OCI 报错）。
      - delobj：删除对象（idx 是 user_data["os_objects"] 的下标，
        Bucket 下标在 user_data["os_bucket_idx"]）。
      - createsender：创建 Email 发件人（邮箱在
        user_data["pending_sender"]，idx 无用，传 0）。
      - delplan：删除抢机计划（idx 是计划 id，如 "p1"）。
      - restartsvc：重启某 systemd 服务（idx 是 SERVICES 下标：0=面板，
        1=机器人；重启机器人时先回复“正在重启”再执行）。
      - restartbot：重启机器人服务（idx 无用，传 0；同 restartsvc:1）。
      - openport：开放云面板端口（idx 无用，传 0；安全组 + UFW 放行）。
      - upgrade：升级客户端（idx 无用，传 0；git pull + pip + 重启
        两个服务；不是 git 仓库时提示手动升级）。
    """
    if action == "changeip":
        inst = resolve_instance(context, idx)
        if inst is None:
            await _reply(update, "实例不存在或已过期，请重新打开实例列表。")
            return True
        try:
            new_ip = await run_sync(
                change_public_ip, get_cli(context),
                get_compartment(context), inst["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        name = inst.get("display-name") or "?"
        await _reply(
            update,
            f"✅ {name} 已换 IP，新公网 IP：{new_ip}\n"
            "⚠️ 旧 IP 已释放，无法找回；"
            "请同步更新 DNS 记录、反代与客户端配置。")
        return True

    if action == "resize":
        inst = resolve_instance(context, idx)
        target = context.user_data.get("pending_resize")
        if inst is None or not target:
            await _reply(update, "实例或目标规格已过期，请重新选择。")
            return True
        shape_config = json.dumps({
            "ocpus": target["ocpus"],
            "memoryInGBs": target["memory_gb"],
        })
        try:
            await run_sync(
                get_cli(context).run,
                ["compute", "instance", "update",
                 "--instance-id", inst["id"],
                 "--shape-config", shape_config,
                 # --force 必加：不加时 OCI CLI 会交互确认
                 # "Are you sure you want to continue? [y/N]"，
                 # subprocess 无 stdin 应答会一直挂到超时
                 # （2026-11-23 真实验证：不加 --force 超时 120 秒）。
                 "--force"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        context.user_data.pop("pending_resize", None)
        name = inst.get("display-name") or "?"
        await _reply(
            update,
            f"✅ {name} 已改为 {target['ocpus']} 核 / "
            f"{target['memory_gb']}GB。\n"
            "改规格时实例可能会重启，请稍后查看状态。")
        return True

    if action == "delvol":
        vol = resolve_volume(context, idx)
        if vol is None:
            await _reply(update, "磁盘不存在或已过期，请重新打开磁盘列表。")
            return True
        try:
            await run_sync(delete_boot_volume, get_cli(context), vol["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        name = vol.get("display-name") or "?"
        await _reply(update, f"✅ 启动盘 {name} 已删除。")
        return True

    if action == "delmfa":
        try:
            devices = await run_sync(
                list_mfa_devices, get_cli(context), get_compartment(context))
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        if not devices:
            await _reply(update, "没有任何 MFA 设备，无需清除。")
            return True
        cli = get_cli(context)
        deleted, failed = 0, 0
        for entry in devices:
            try:
                await run_sync(delete_mfa_device, cli,
                               entry["user_id"], entry["device"]["id"])
                deleted += 1
            except Exception:  # noqa: BLE001 - 单个失败不影响其他
                failed += 1
        text = f"🔐 MFA 清除完成：成功 {deleted} 个"
        if failed:
            text += f"，失败 {failed} 个（请在控制台手动处理）"
        text += "。"
        await _reply(update, text)
        return True

    if action == "rename":
        inst = resolve_instance(context, idx)
        new_name = context.user_data.get("pending_rename_name")
        if inst is None or not new_name:
            await _reply(update, "实例或新名字已过期，请重新改名。")
            return True
        try:
            await run_sync(rename_instance, get_cli(context),
                           inst["id"], new_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        context.user_data.pop("pending_rename_name", None)
        old_name = inst.get("display-name") or "?"
        await _reply(update, f"✅ 实例「{old_name}」已改名为「{new_name}」。")
        return True

    if action == "reinstall":
        inst = resolve_instance(context, idx)
        if inst is None:
            await _reply(update, "实例不存在或已过期，请重新打开实例列表。")
            return True
        try:
            await run_sync(reinstall_instance, get_cli(context), inst["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        name = inst.get("display-name") or "?"
        await _reply(update,
                     f"✅ 实例「{name}」重装已提交，系统盘正在重建。\n"
                     "请等待实例重启完成后再连接。")
        return True

    if action in ("batchstart", "batchstop", "batchrestart", "batchterminate"):
        selected = context.user_data.get("batch_selected") or set()
        instances = [resolve_instance(context, i) for i in sorted(selected)]
        instances = [inst for inst in instances if inst is not None]
        if not instances:
            await _reply(update, "选中的实例已过期，请重新打开批量操作。")
            return True
        ids = [inst["id"] for inst in instances]
        names = {inst["id"]: (inst.get("display-name") or "?")
                 for inst in instances}
        verb = _BATCH_ACTION_TEXT[action[len("batch"):]]
        try:
            # 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
            if action == "batchstart":
                result = await run_sync(batch_start, get_cli(context), ids)
            elif action == "batchstop":
                result = await run_sync(batch_stop, get_cli(context), ids)
            elif action == "batchrestart":
                result = await run_sync(batch_restart, get_cli(context), ids)
            else:
                result = await run_sync(batch_terminate, get_cli(context), ids)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        context.user_data.pop("batch_selected", None)
        context.user_data.pop("pending_batch_action", None)
        await _reply(update, fmt_batch_result(verb, result, names))
        return True

    if action == "delbucket":
        buckets = context.user_data.get("os_buckets") or []
        try:
            bucket = buckets[int(idx)]
        except (TypeError, ValueError, IndexError):
            bucket = None
        if bucket is None:
            await _reply(update, "Bucket 不存在或已过期，请重新打开对象存储列表。")
            return True
        name = bucket.get("name") or "?"
        try:
            await run_sync(delete_bucket, get_cli(context), name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        await _reply(update, f"✅ Bucket {name} 已删除。")
        return True

    if action == "delobj":
        buckets = context.user_data.get("os_buckets") or []
        objects = context.user_data.get("os_objects") or []
        try:
            bucket = buckets[int(context.user_data.get("os_bucket_idx", -1))]
            obj = objects[int(idx)]
        except (TypeError, ValueError, IndexError):
            bucket, obj = None, None
        if bucket is None or obj is None:
            await _reply(update, "对象不存在或已过期，请重新打开对象列表。")
            return True
        bucket_name = bucket.get("name") or "?"
        object_name = obj.get("name") or "?"
        try:
            await run_sync(delete_object, get_cli(context),
                           bucket_name, object_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        await _reply(update, f"✅ 对象 {object_name} 已删除。")
        return True

    if action == "createsender":
        email = context.user_data.get("pending_sender")
        if not email:
            await _reply(update, "发件人邮箱已过期，请重新从菜单进入。")
            return True
        try:
            await run_sync(create_sender, get_cli(context),
                           get_compartment(context), email)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        context.user_data.pop("pending_sender", None)
        await _reply(update,
                     f"✅ 发件人 {email} 已创建。\n"
                     "请到该邮箱点击 OCI 的验证链接，验证后才能使用。")
        return True

    if action == "delplan":
        plan_id = str(idx)
        if not PLANS.delete(plan_id):
            await _reply(update, f"抢机计划 {plan_id} 不存在，可能已被删除。")
            return True
        await _reply(update, f"✅ 抢机计划 {plan_id} 已删除。")
        return True

    if action in ("restartsvc", "restartbot"):
        if action == "restartbot":
            service = BOT_SERVICE
        else:
            try:
                service = SERVICES[int(idx)]
            except (TypeError, ValueError, IndexError):
                await _reply(update, "服务不存在，请重新打开客户端管理。")
                return True
        # 重启机器人会杀掉当前进程：先把“正在重启”发出去再执行。
        await _reply(update, f"🔄 正在重启服务 {service}…")
        try:
            await run_sync(restart_service, service)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, f"❌ 重启 {service} 失败：{exc}")
            return True
        if service != BOT_SERVICE:
            await _reply(update, f"✅ 服务 {service} 已重启。")
        # 重启机器人时进程即将被 systemd 杀掉，不再发第二条消息；
        # 用户已收到“正在重启”，服务恢复后即代表重启完成。
        return True

    if action == "openport":
        if "xxxxxxxx" in PANEL_SECURITY_LIST_ID:
            await _reply(update,
                         "⚠️ 未配置面板安全组：请先在 "
                         "tgbot/handlers_extra.py 的 PANEL_SECURITY_LIST_ID "
                         "里填入面板服务器所在子网的安全组 OCID，"
                         "再使用本功能。")
            return True
        try:
            added = await run_sync(
                add_ingress_rule, get_cli(context),
                PANEL_SECURITY_LIST_ID, PANEL_PORT)
            await run_sync(ufw_allow_port, PANEL_PORT,
                           PANEL_SSH_HOST or None)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        rule_text = "已新增放行规则" if added else "放行规则已存在"
        await _reply(
            update,
            f"✅ {PANEL_PORT} 端口已放行（OCI 安全组{rule_text} + UFW）。\n"
            f"⚠️ {PANEL_PORT} 端口已暴露到公网，任何人可访问登录页，"
            "请确保密码足够强。")
        return True

    if action == "upgrade":
        if not await run_sync(is_git_repo, PROJECT_ROOT):
            await _reply(
                update,
                "⚠️ 当前目录不是 git 仓库，无法自动升级，请手动升级：\n"
                "git pull && pip install -r requirements.txt，"
                "然后重启 rbot-clone-web 与 rbot-clone-bot 两个服务。")
            return True
        # 升级最后会重启机器人（杀掉当前进程）：先把消息发出去再执行。
        await _reply(update,
                     "⬆️ 开始升级：拉取最新代码并安装依赖，"
                     "随后重启面板与机器人，约 1 分钟不可用…")
        try:
            await run_sync(upgrade_client, PROJECT_ROOT)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return True
        # 正常情况下 upgrade_client 最后重启机器人，本行可能发不出去；
        # 用户已收到“开始升级”消息，服务恢复后即代表升级完成。
        await _reply(update, "✅ 升级完成，面板与机器人正在重启。")
        return True

    return False


# ================= /domain 命令（Cloudflare DNS 管理） =================

def _get_cf_client():
    """取 Cloudflare 客户端；clouds.cloudflare 未就绪时抛 config 错误。"""
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


def fmt_cf_error(exc):
    """把 CloudflareError 转成中文提示（不泄露 API 原始错误细节）。"""
    kind = getattr(exc, "kind", "api")
    if kind in ("config", "auth"):
        return "❌ Cloudflare 未配置或认证失败：请检查 Token 配置。"
    if kind == "not_found":
        return "❌ Zone 或 DNS 记录不存在，请刷新列表后再试。"
    if kind == "rate_limit":
        return "❌ Cloudflare 限流（429）：请求太频繁，请稍后再试。"
    return "❌ Cloudflare 调用失败，请稍后再试。"


def fmt_domain_zones(zones):
    """Zone 列表全文（Zone 名也出现在内联键盘按钮上）。"""
    if not zones:
        return "🌐 Cloudflare 里没有 Zone。"
    lines = [f"🌐 DNS 域名（{len(zones)} 个 Zone）：", ""]
    for zone in zones:
        status = zone.get("status") or ""
        mark = "" if status == "active" else f"（{status}）"
        lines.append(f"• {zone.get('name', '?')}{mark}")
    lines.append("")
    lines.append("点下面按钮查看某 Zone 的 DNS 记录。")
    return "\n".join(lines)


def fmt_domain_record_line(record):
    """记录列表里的一行：类型 名称 -> 内容，proxied 的标 🟠。"""
    rtype = record.get("type", "?")
    name = record.get("name", "?")
    content = record.get("content", "?")
    mark = " 🟠" if record.get("proxied") else ""
    return f"• {rtype} {name} -> {content}{mark}"


def fmt_domain_records(zone_name, records, limit=DOMAIN_RECORDS_LIMIT):
    """某 Zone 的记录列表全文；超过 limit 条时截断并提示总数。"""
    if not records:
        return f"🌐 {zone_name} 下没有 DNS 记录。"
    lines = [f"🌐 {zone_name} 的 DNS 记录（共 {len(records)} 条）：", ""]
    shown = records[:limit]
    lines.extend(fmt_domain_record_line(r) for r in shown)
    if len(records) > limit:
        lines.append("")
        lines.append(f"…还有 {len(records) - limit} 条未显示，"
                     "请到 Web 面板查看完整列表。")
    return "\n".join(lines)


def _resolve_zone_name(context, zone_id, records):
    """由 zone_id 解析 Zone 名：先看已存的 Zone 列表，再看记录的 zone_name。"""
    for zone in context.user_data.get("domain_zones") or []:
        if zone.get("id") == zone_id:
            return zone.get("name") or zone_id
    for record in records or []:
        if record.get("zone_name"):
            return record["zone_name"]
    return zone_id


@require_allowed
async def handle_domain(update, context):
    """顶层命令 /domain：列出 Cloudflare Zone（内联键盘，点 Zone 看记录）。"""
    try:
        client = _get_cf_client()
        zones = await run_sync(client.list_zones)
    except CloudflareError as exc:
        await _reply(update, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001 - 其他异常也转成提示，不崩机器人
        await _reply(update, f"❌ 出错了：{exc}")
        return
    context.user_data["domain_zones"] = list(zones)
    await _reply(update, fmt_domain_zones(zones),
                 reply_markup=domain_zones_keyboard(zones))


@require_allowed
async def domain_callback(update, context):
    """回调 "domain:zone:<zone_id>" / "domain:back"。

    zone：查该 Zone 的 DNS 记录列表；back：重新列 Zone。
    """
    data = _callback_data(update)
    parts = data.split(":", 2)
    if len(parts) >= 2 and parts[1] == "back":
        await handle_domain(update, context)
        return
    if len(parts) != 3 or parts[1] != "zone":
        await _reply(update, "参数错误，请重新发送 /domain。")
        return
    zone_id = parts[2]
    try:
        client = _get_cf_client()
        records = await run_sync(client.list_dns_records, zone_id)
    except CloudflareError as exc:
        await _reply(update, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await _reply(update, f"❌ 出错了：{exc}")
        return
    zone_name = _resolve_zone_name(context, zone_id, records)
    await _reply(update, fmt_domain_records(zone_name, records),
                 reply_markup=domain_records_keyboard(zone_id, records))


# ================= JobQueue 任务（bot.py 注册，开关在 bot_data） =================

async def check_instances_job(context):
    """每 10 分钟跑一次：实例状态变化通知 + 自启 + 流量守护 + 停机保活。

    状态通知与自启的开关在 bot_data["monitor"]（{"on", "autostart",
    "chat_id"}）；流量守护与停机保活有各自的文件开关
    （见 tgbot.monitoring），不依赖监控开关，各自开启即生效。
    状态基线存在 bot_data["last_states"]
    （{instance_id: lifecycle_state}），首次跑只记基线、不通知。
    只监控默认 profile 的实例。
    """
    monitor = context.bot_data.get("monitor") or {}
    instances = None
    started_ids = set()
    if monitor.get("on") and monitor.get("chat_id"):
        chat_id = monitor["chat_id"]
        profile = bot_config.DEFAULT_PROFILE
        cli = bot_config.get_oci_client(profile=profile)
        compartment = bot_config.get_compartment_id(profile)
        try:
            instances = await run_sync(list_instances, cli, compartment)
        except Exception as exc:  # noqa: BLE001 - 检查失败也要通知用户
            await context.bot.send_message(
                chat_id=chat_id, text=f"监控检查失败：{fmt_error(exc)}")
            instances = None
        else:
            last_states = context.bot_data.setdefault("last_states", {})
            changes = []
            for inst in instances:
                instance_id = inst.get("id")
                state = inst.get("lifecycle-state")
                previous = last_states.get(instance_id)
                if previous is not None and previous != state:
                    changes.append((inst, previous, state))
                last_states[instance_id] = state
            if changes:
                lines = ["🔔 实例状态变化：", ""]
                for inst, previous, state in changes:
                    name = inst.get("display-name") or "?"
                    lines.append(f"• {name}：{previous} → {state}")
                await context.bot.send_message(chat_id=chat_id,
                                               text="\n".join(lines))
            if monitor.get("autostart"):
                for inst in instances:
                    if inst.get("lifecycle-state") != "STOPPED":
                        continue
                    name = inst.get("display-name") or "?"
                    try:
                        await run_sync(start_instance, cli, inst["id"])
                    except Exception as exc:  # noqa: BLE001 - 单台启动失败不影响其他
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"自动启动 {name} 失败：{fmt_error(exc)}")
                    else:
                        started_ids.add(inst["id"])
                        await context.bot.send_message(
                            chat_id=chat_id, text=f"▶️ 已自动启动实例：{name}")
    # 流量守护与停机保活：各自的开关，不依赖监控开关；某一步异常
    # 不能影响另一步。
    try:
        await _traffic_guard_step(context, instances)
    except Exception as exc:  # noqa: BLE001 - job 内部异常只记日志
        logger.warning("流量守护检查异常：%s", exc)
    try:
        await _keepalive_step(context, instances, skip_ids=started_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("停机保活检查异常：%s", exc)


async def _traffic_guard_step(context, instances=None):
    """流量守护检查（check_instances_job 的一步）。

    已开启时查本月出站流量（复用用量 API，与菜单 19 同一查询方式），
    超阈值且距上次触发超过 24 小时才动作：notify 只发提醒；
    shutdown 停掉该账号下全部 RUNNING 实例并通知。
    instances 是 job 已查好的列表（可为 None，shutdown 需要时再查）。
    """
    cfg = load_traffic_guard()
    if not cfg.get("enabled"):
        return
    chat_id = cfg.get("chat_id") or (context.bot_data.get("monitor")
                                     or {}).get("chat_id")
    if not chat_id:
        logger.warning("流量守护已开启但未记录通知 chat_id，跳过本次检查")
        return
    profile = bot_config.DEFAULT_PROFILE
    cli = bot_config.get_oci_client(profile=profile)
    compartment = bot_config.get_compartment_id(profile)
    try:
        items = await run_sync(fetch_usage_summary, cli, compartment,
                               "MONTHLY", 1)
    except Exception as exc:  # noqa: BLE001 - 查询失败也要通知用户
        await context.bot.send_message(
            chat_id=chat_id, text=f"流量守护检查失败：{fmt_error(exc)}")
        return
    used_gb = current_month_transfer_gb(items)
    if not should_trigger_traffic_guard(cfg, used_gb):
        return
    record_traffic_guard_trigger(cfg)
    threshold = cfg.get("threshold_gb")
    if cfg.get("action") != "shutdown":
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"⚠️ 流量守护提醒：本月出站流量 {used_gb:.2f}GB，"
                  f"已超过阈值 {threshold}GB。\n"
                  "当前动作是“仅通知”，未关机；"
                  "如需自动关机请到 /oracle 菜单 37. 流量守护里切换。"))
        return
    if instances is None:
        try:
            instances = await run_sync(list_instances, cli, compartment)
        except Exception as exc:  # noqa: BLE001
            await context.bot.send_message(
                chat_id=chat_id,
                text="流量守护：流量已超阈值，但查实例列表失败，"
                     f"未能自动关机：{fmt_error(exc)}")
            return
    stopped, failed = [], []
    for inst in instances or []:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        name = inst.get("display-name") or "?"
        try:
            await run_sync(stop_instance, cli, inst["id"])
        except Exception:  # noqa: BLE001 - 单台失败不影响其他台
            failed.append(name)
        else:
            stopped.append(name)
    lines = [f"🛡️ 流量守护：本月出站流量 {used_gb:.2f}GB 已超过阈值 "
             f"{threshold}GB，已自动关机。"]
    if stopped:
        lines.append(f"已停止：{', '.join(stopped)}")
    if failed:
        lines.append(f"停止失败：{', '.join(failed)}（请手动处理）")
    if not stopped and not failed:
        lines.append("当前没有运行中的实例，无需关机。")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def _keepalive_step(context, instances=None, skip_ids=None):
    """停机保活检查（check_instances_job 的一步）。

    只对 keepalive.json 里明确开启的实例自动开机；skip_ids 是本轮
    监控自启已启动过的实例（避免重复启动）。连续 3 次开机失败的
    实例加入停试名单并通知用户，需手动重新开启。
    """
    cfg = load_keepalive()
    targets = keepalive_enabled_instances(cfg)
    if not targets:
        return
    state = load_keepalive_state()
    chat_id = state.get("chat_id") or (context.bot_data.get("monitor")
                                       or {}).get("chat_id")
    if not chat_id:
        logger.warning("停机保活有开启的实例但未记录通知 chat_id，跳过本次检查")
        return
    gave_up = set(state.get("gave_up") or [])
    targets = {t for t in targets if t not in gave_up}
    targets -= set(skip_ids or ())
    if not targets:
        return
    profile = bot_config.DEFAULT_PROFILE
    cli = bot_config.get_oci_client(profile=profile)
    compartment = bot_config.get_compartment_id(profile)
    if instances is None:
        try:
            instances = await run_sync(list_instances, cli, compartment)
        except Exception as exc:  # noqa: BLE001
            await context.bot.send_message(
                chat_id=chat_id, text=f"停机保活检查失败：{fmt_error(exc)}")
            return
    by_id = {inst.get("id"): inst for inst in instances or []}
    changed = False
    for instance_id in sorted(targets):
        inst = by_id.get(instance_id)
        if inst is None:
            continue  # 实例已删除或不在本账号下，跳过
        if inst.get("lifecycle-state") != "STOPPED":
            if instance_id in (state.get("failures") or {}):
                keepalive_record_success(state, instance_id)
                changed = True
            continue
        name = inst.get("display-name") or f"…{instance_id[-4:]}"
        try:
            await run_sync(start_instance, cli, instance_id)
        except Exception as exc:  # noqa: BLE001 - 单台失败不影响其他台
            count, gave_up_now = keepalive_record_failure(state, instance_id)
            changed = True
            if gave_up_now:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ 停机保活：{name} 连续 {KEEPALIVE_MAX_FAILURES} 次"
                          "自动开机失败，已停止尝试，请手动处理或到菜单里"
                          "重新开启保活。"))
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(f"⚠️ 停机保活：{name} 自动开机失败"
                          f"（第 {count} 次）：{fmt_error(exc)}"))
        else:
            keepalive_record_success(state, instance_id)
            changed = True
            await context.bot.send_message(
                chat_id=chat_id, text=f"▶️ 停机保活：已自动启动实例 {name}")
    if changed:
        save_keepalive_state(state)


async def daily_report_job(context):
    """每天跑一次：发配额 + 费用摘要。

    开关在 bot_data["daily_report"]（{"on", "chat_id"}），关闭时直接返回。
    只汇报默认 profile。
    """
    daily = context.bot_data.get("daily_report") or {}
    if not daily.get("on"):
        return
    chat_id = daily.get("chat_id")
    if not chat_id:
        return
    profile = bot_config.DEFAULT_PROFILE
    cli = bot_config.get_oci_client(profile=profile)
    compartment = bot_config.get_compartment_id(profile)
    try:
        summary = await run_sync(get_free_tier_summary, cli, compartment)
        items = await run_sync(fetch_usage_summary, cli, compartment, "MONTHLY")
    except Exception as exc:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=chat_id, text=f"每日报告生成失败：{fmt_error(exc)}")
        return
    text = ("📰 每日报告\n\n"
            + fmt_quota_summary(summary, profile)
            + "\n\n" + fmt_usage_total(items))
    await context.bot.send_message(chat_id=chat_id, text=text)


async def ssl_check_job(context):
    """每天跑一次：检查 SSL 监控列表里各域名的证书到期日。

    开关就是域名列表本身：列表为空时直接返回。到期前 30/14/7/1 天
    各提醒一次（已提醒的阈值记在 ssl_watch.json 的 reminded 里）；
    证书续期后（剩余天数回升）对应阈值可重新提醒。
    """
    cfg = load_ssl_watch()
    domains = cfg.get("domains") or []
    if not domains:
        return
    chat_id = cfg.get("chat_id") or (context.bot_data.get("monitor")
                                     or {}).get("chat_id")
    if not chat_id:
        logger.warning("SSL 监控有域名但未记录通知 chat_id，跳过本次检查")
        return
    if check_ssl_expiry is None:
        await context.bot.send_message(
            chat_id=chat_id, text="SSL 监控：证书检查功能未就绪，请检查部署。")
        return
    reminded = dict(cfg.get("reminded") or {})
    changed = False
    for domain in domains:
        try:
            result = await run_sync(check_ssl_expiry, domain)
        except Exception as exc:  # noqa: BLE001 - 单个域名失败不影响其他
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ SSL 监控：{domain} 证书检查失败：{exc}")
            continue
        days_left = result["days_left"]
        due, new_reminded = ssl_reminder_due(days_left,
                                             reminded.get(domain) or [])
        if new_reminded != (reminded.get(domain) or []):
            reminded[domain] = new_reminded
            changed = True
        if not due:
            continue
        if days_left < 0:
            when = f"已过期 {-days_left} 天"
        elif days_left == 0:
            when = "今天到期"
        else:
            when = f"还有 {days_left} 天到期"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"🔒 SSL 证书到期提醒：{domain} {when}"
                  f"（到期日 {result['expires']}），请及时续期。"))
    if changed:
        cfg["reminded"] = reminded
        save_ssl_watch(cfg)


# ================= 工作包 D 新增（阶段 5） =================
# 对象存储（菜单 40）、Email 发件人（菜单 41）、抢机计划（菜单 42）、
# 开放云面板端口（菜单 11）、客户端管理（菜单 14）、重启（菜单 31）、
# 升级客户端（菜单 32）。


# ---------------- 安全组放行（开放云面板端口，cli.run 直调） ----------------

def add_ingress_rule(cli, security_list_id, port, source="0.0.0.0/0"):
    """给安全组加一条 TCP 入站放行规则（oci network security-list
    get/update）。

    先查现有规则：同 source + 同端口的规则已存在时返回 False（不重复加）；
    否则追加一条 {"source", "protocol": "6", "tcp-options":
    {"destination-port-range": {"min": port, "max": port}}} 并 update
    （带 --force），返回 True。

    写操作：调用方应在用户二次确认后才调用。
    """
    current = _unwrap(cli.run(["network", "security-list", "get",
                               "--security-list-id", security_list_id])) or {}
    rules = list(current.get("ingress-security-rules") or [])
    for rule in rules:
        if rule.get("source") != source:
            continue
        if str(rule.get("protocol")) != "6":
            continue
        tcp = rule.get("tcp-options") or {}
        port_range = tcp.get("destination-port-range") or {}
        try:
            if (int(port_range.get("min")) == int(port)
                    and int(port_range.get("max")) == int(port)):
                return False
        except (TypeError, ValueError):
            continue
    rules.append({
        "source": source,
        "protocol": "6",
        "is-stateless": False,
        "tcp-options": {"destination-port-range": {"min": int(port),
                                                   "max": int(port)}},
    })
    cli.run(["network", "security-list", "update",
             "--security-list-id", security_list_id,
             "--ingress-security-rules", json.dumps(rules),
             # --force 必加：不加时 OCI CLI 会交互确认，
             # subprocess 无 stdin 应答会一直挂到超时。
             "--force"])
    return True


# ---------------- 对象存储 / 计划 / 客户端的格式化 ----------------

def fmt_bucket_list(buckets):
    """Bucket 列表全文（只读列表 + 删除入口在键盘上）。"""
    if not buckets:
        return "🪣 对象存储：还没有 Bucket，请到 Web 面板创建。"
    lines = [f"🪣 对象存储 Bucket（{len(buckets)} 个）：", ""]
    for bucket in buckets:
        name = bucket.get("name") or "?"
        created = (bucket.get("time-created") or "")[:10]
        tier = bucket.get("storage-tier") or ""
        lines.append(f"• {name}（{tier}，{created}）")
    lines.append("")
    lines.append("点下面按钮查看某 Bucket 的对象，或删除 Bucket。")
    return "\n".join(lines)


def fmt_object_list(bucket_name, objects):
    """某 Bucket 的对象列表全文。"""
    if not objects:
        return f"🪣 Bucket {bucket_name} 里没有对象。"
    total = sum(o.get("size") or 0 for o in objects)
    lines = [f"🪣 {bucket_name} 的对象（{len(objects)} 个，"
             f"共 {total} 字节）：", ""]
    for obj in objects:
        name = obj.get("name") or "?"
        size = obj.get("size") or 0
        lines.append(f"• {name}（{size} 字节）")
    lines.append("")
    lines.append("点下面按钮删除某个对象。")
    return "\n".join(lines)


def fmt_plan_list(plans):
    """抢机计划列表全文。"""
    if not plans:
        return ("📅 还没有抢机计划。\n"
                "请到 Web 面板任务页添加（定时表达式 + 目标形状/配置）。")
    lines = [f"📅 抢机计划（{len(plans)} 条）：", ""]
    for plan in plans:
        shape = (plan.get("shape") or "?").replace("VM.Standard.", "")
        state = "✅已启用" if plan.get("enabled") else "⏸️已停用"
        lines.append(f"• {plan.get('id')}：cron `{plan.get('cron')}`，"
                     f"{shape} {plan.get('ocpus')} 核 / "
                     f"{plan.get('memory_gb')}GB，{state}")
    lines.append("")
    lines.append("点下面按钮开关或删除某条计划。")
    return "\n".join(lines)


def fmt_client_status(statuses):
    """两个 systemd 服务的状态全文。statuses 是 {服务名: 状态}。"""
    lines = ["🖥️ 客户端管理：", ""]
    for service in SERVICES:
        state = statuses.get(service, "unknown")
        mark = "✅" if state == "active" else "❌"
        lines.append(f"{mark} {service}：{state}")
    lines.append("")
    lines.append("点下面按钮重启某个服务。")
    return "\n".join(lines)


# ---------------- 工作包 D 的小键盘 ----------------

def bucket_list_keyboard(buckets):
    """Bucket 列表键盘：每 Bucket 一个查看对象按钮 + 一个删除按钮。"""
    rows = []
    for idx, bucket in enumerate(buckets):
        name = bucket.get("name") or (bucket.get("id") or "")[-4:]
        rows.append([InlineKeyboardButton(
            f"🪣 {name}", callback_data=f"os:objects:{idx}"),
            InlineKeyboardButton("🗑️ 删除",
                                 callback_data=f"os:delbucket:{idx}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def object_list_keyboard(objects):
    """对象列表键盘：每对象一个删除按钮 + 返回 Bucket 列表。"""
    rows = []
    for idx, obj in enumerate(objects):
        name = obj.get("name") or "?"
        rows.append([InlineKeyboardButton(
            f"🗑️ {name}", callback_data=f"os:delobj:{idx}")])
    rows.append([InlineKeyboardButton("◀ Bucket 列表", callback_data="os:back")])
    return InlineKeyboardMarkup(rows)


def plan_list_keyboard(plans):
    """抢机计划列表键盘：每计划一个开关按钮 + 一个删除按钮。"""
    rows = []
    for plan in plans:
        plan_id = plan.get("id")
        toggle_text = f"⏸️ 停用 {plan_id}" if plan.get("enabled") else f"▶️ 启用 {plan_id}"
        rows.append([InlineKeyboardButton(
            toggle_text, callback_data=f"pl:toggle:{plan_id}"),
            InlineKeyboardButton("🗑️ 删除",
                                 callback_data=f"pl:delete:{plan_id}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def client_actions_keyboard():
    """客户端管理键盘：重启面板 / 重启机器人。"""
    rows = []
    for idx, service in enumerate(SERVICES):
        label = "面板" if service == WEB_SERVICE else "机器人"
        rows.append([InlineKeyboardButton(
            f"🔄 重启{label}（{service}）",
            callback_data=f"cl:restart:{idx}")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


# ---- 40. 对象存储 ----

@require_allowed
async def handle_objectstorage(update, context):
    """菜单 40：Bucket 只读列表 + 查看对象/删除按钮。"""
    try:
        buckets = await run_sync(list_buckets,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, fmt_error(exc))
        return
    context.user_data["os_buckets"] = list(buckets)
    await _reply(update, fmt_bucket_list(buckets),
                 reply_markup=bucket_list_keyboard(buckets))


@require_allowed
async def objectstorage_callback(update, context):
    """回调 "os:objects:<idx>" / "os:delbucket:<idx>" /
    "os:delobj:<idx>" / "os:back"。

    objects：列该 Bucket 的对象；delbucket/delobj：删除警告 + 二次确认
    键盘（真正执行在 handle_confirm）；back：返回 Bucket 列表。
    """
    data = _callback_data(update)
    parts = data.split(":")
    if len(parts) >= 2 and parts[1] == "back":
        await handle_objectstorage(update, context)
        return
    if len(parts) != 3:
        await _reply(update, "参数错误，请重新打开对象存储列表。")
        return
    _, action, idx = parts
    buckets = context.user_data.get("os_buckets") or []
    try:
        bucket = buckets[int(idx)] if action != "delobj" else None
    except (TypeError, ValueError, IndexError):
        bucket = None
    if action == "delobj":
        objects = context.user_data.get("os_objects") or []
        try:
            obj = objects[int(idx)]
        except (TypeError, ValueError, IndexError):
            obj = None
        if obj is None:
            await _reply(update, "对象不存在或已过期，请重新打开对象列表。")
            return
        name = obj.get("name") or "?"
        await _reply(
            update,
            f"⚠️ 即将删除对象 {name}！\n删除后无法恢复。",
            reply_markup=confirm_keyboard("delobj", idx, "🗑️ 确认删除对象"))
        return
    if bucket is None:
        await _reply(update, "Bucket 不存在或已过期，请重新打开对象存储列表。")
        return
    bucket_name = bucket.get("name") or "?"
    if action == "objects":
        try:
            objects = await run_sync(list_objects, get_cli(context),
                                     bucket_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, fmt_error(exc))
            return
        context.user_data["os_objects"] = list(objects)
        context.user_data["os_bucket_idx"] = idx
        await _reply(update, fmt_object_list(bucket_name, objects),
                     reply_markup=object_list_keyboard(objects))
        return
    if action == "delbucket":
        await _reply(
            update,
            f"⚠️ 即将删除 Bucket {bucket_name}！\n"
            "删除后无法恢复；Bucket 里还有对象时删除会失败，"
            "请先清空对象（或到 Web 面板勾选“同时删除全部对象”）。",
            reply_markup=confirm_keyboard("delbucket", idx,
                                          "🗑️ 确认删除 Bucket"))
        return
    await _reply(update, "未知操作，请重新打开对象存储列表。")


# ---- 41. Email 发件人 ----

@require_allowed
async def handle_email(update, context):
    """菜单 41：创建 Email Delivery 发件人——提示输入邮箱地址。

    用户回复文字后由 text_input_callback（pending_input kind=
    "email_sender"）校验格式并弹二次确认；真正执行在
    handle_confirm("createsender")。
    SMTP 凭据只在 Web 面板生成（密码只显示一次，不适合发聊天记录里）。
    """
    context.user_data["pending_input"] = {"kind": "email_sender"}
    await _reply(update,
                 "📧 创建 Email Delivery 发件人：\n\n"
                 "请直接发送发件人邮箱地址（例如 noreply@example.com）。\n"
                 "创建后需点击 OCI 发到该邮箱的验证链接，发件人才能使用。\n"
                 "SMTP 凭据请到 Web 面板 Email 页生成（密码只显示一次）。")


# ---- 42. 抢机计划 ----

@require_allowed
async def handle_plans(update, context):
    """菜单 42：抢机计划列表 + 开关/删除按钮。

    计划与 Web 面板共用 DATA_DIR/grabber_plans.json（web.tasks.PLANS）；
    新增计划请到 Web 面板任务页添加。
    """
    plans = PLANS.list()
    await _reply(update, fmt_plan_list(plans),
                 reply_markup=plan_list_keyboard(plans))


@require_allowed
async def plan_callback(update, context):
    """回调 "pl:toggle:<id>" / "pl:delete:<id>"。

    toggle：开关计划（立即生效，面板调度器 1 分钟内同步）；
    delete：删除警告 + 二次确认键盘（执行在 handle_confirm）。
    """
    data = _callback_data(update)
    parts = data.split(":", 2)
    if len(parts) != 3:
        await _reply(update, "参数错误，请重新打开抢机计划列表。")
        return
    _, action, plan_id = parts
    plan = PLANS.get(plan_id)
    if plan is None:
        await _reply(update, f"抢机计划 {plan_id} 不存在，可能已被删除。")
        return
    if action == "toggle":
        updated = PLANS.set_enabled(plan_id, not plan.get("enabled"))
        state = "已启用" if updated.get("enabled") else "已停用"
        await _reply(update, f"✅ 抢机计划 {plan_id} {state}。")
        await handle_plans(update, context)
        return
    if action == "delete":
        shape = (plan.get("shape") or "?").replace("VM.Standard.", "")
        await _reply(
            update,
            f"⚠️ 确认删除抢机计划 {plan_id}？\n"
            f"cron `{plan.get('cron')}`，{shape} "
            f"{plan.get('ocpus')} 核 / {plan.get('memory_gb')}GB。\n"
            "删除后不再按该计划定时抢机。",
            reply_markup=confirm_keyboard("delplan", plan_id,
                                          "🗑️ 确认删除计划"))
        return
    await _reply(update, "未知操作，请重新打开抢机计划列表。")


# ---- 11. 开放云面板端口 ----

@require_allowed
async def handle_openport(update, context):
    """菜单 11：开放云面板端口（二次确认）。

    未配置 PANEL_SECURITY_LIST_ID 时提示先配置，不执行。
    真正执行在 handle_confirm("openport")：OCI 安全组放行 + UFW 放行。
    """
    if "xxxxxxxx" in PANEL_SECURITY_LIST_ID:
        await _reply(update,
                     "⚠️ 未配置面板安全组：请先在 tgbot/handlers_extra.py 的 "
                     "PANEL_SECURITY_LIST_ID 里填入面板服务器所在子网的"
                     "安全组 OCID，再使用本功能。\n"
                     "查法：oci network security-list list "
                     "--compartment-id <你的 compartment OCID>",
                     reply_markup=back_to_oracle_keyboard())
        return
    await _reply(update,
                 f"⚠️ 确认开放云面板端口 {PANEL_PORT}？\n\n"
                 "将执行两步：\n"
                 f"1. OCI 安全组放行 {PANEL_PORT}/tcp（来源 0.0.0.0/0）；\n"
                 f"2. 面板服务器 UFW 放行 {PANEL_PORT}/tcp。\n\n"
                 f"⚠️ {PANEL_PORT} 端口将暴露到公网，任何人可访问登录页，"
                 "请确保密码足够强。",
                 reply_markup=confirm_keyboard("openport", 0,
                                               "🌐 确认开放端口"))


# ---- 14. 客户端管理 ----

@require_allowed
async def handle_clients(update, context):
    """菜单 14：两个 systemd 服务状态 + 重启按钮。"""
    statuses = {}
    for service in SERVICES:
        statuses[service] = await run_sync(service_status, service)
    await _reply(update, fmt_client_status(statuses),
                 reply_markup=client_actions_keyboard())


@require_allowed
async def client_callback(update, context):
    """回调 "cl:restart:<idx>"：重启某服务的二次确认提示。

    真正执行在 handle_confirm("restartsvc")。
    """
    parts = _callback_data(update).split(":")
    if len(parts) != 3 or parts[1] != "restart":
        await _reply(update, "参数错误，请重新打开客户端管理。")
        return
    try:
        service = SERVICES[int(parts[2])]
    except (TypeError, ValueError, IndexError):
        await _reply(update, "服务不存在，请重新打开客户端管理。")
        return
    label = "面板" if service == WEB_SERVICE else "机器人"
    await _reply(update,
                 f"⚠️ 确认重启服务 {service}？\n"
                 f"重启期间{label}会短暂不可用（约几秒）；"
                 "重启机器人后它会自动恢复。",
                 reply_markup=confirm_keyboard(
                     "restartsvc", parts[2], f"🔄 确认重启 {service}"))


# ---- 31. 重启机器人 ----

@require_allowed
async def handle_restart(update, context):
    """菜单 31：重启机器人服务（二次确认）。

    真正执行在 handle_confirm("restartbot")：先回复“正在重启”再执行，
    systemd 会自动拉起机器人。
    """
    await _reply(update,
                 "⚠️ 确认重启机器人服务（rbot-clone-bot）？\n"
                 "重启期间机器人约 10 秒无响应，systemd 会自动拉起。",
                 reply_markup=confirm_keyboard("restartbot", 0,
                                               "🔄 确认重启机器人"))


# ---- 32. 升级客户端 ----

@require_allowed
async def handle_upgrade(update, context):
    """菜单 32：升级客户端（二次确认）。

    真正执行在 handle_confirm("upgrade")：git pull + pip install +
    重启两个服务；不是 git 仓库时提示手动升级。
    """
    await _reply(update,
                 "⚠️ 确认升级客户端？\n"
                 "升级会拉取最新代码、安装依赖，然后重启面板与机器人，"
                 "约 1 分钟不可用。",
                 reply_markup=confirm_keyboard("upgrade", 0, "⬆️ 确认升级"))


# ================= /oracle 菜单分发 =================

# 本模块实现的菜单项 key -> handler（"changeip"/"rename"/"reinstall"/
# "console" 不是菜单项，是实例操作 "i:<act>:<idx>" 的转发项）。
# 未列出的项 dispatch 返回 False：
#   - instances：核心项，由 handlers.py 处理，不在本模块；
#   - autoip / disable / delapi：不实现（原因见 docs/phase2-todo.md；
#     其中 delapi 是故意不做——会删机器人自己的 API 密钥）。
# 菜单 23/24（memballoon / memclear）由 tgbot.handlers_client 实现，
# 分发表里直接指向该模块的 handler。
# 阶段 5 工作包 D 已实现 openport / clients / restart / upgrade /
# objectstorage / email / plans（之前在“不实现”列表里，现已实现）。
_DISPATCH = {
    "boot": handle_boot,
    "ip": handle_ip,
    "changeip": handle_changeip_prompt,
    "resume": handle_resume,
    "ipv6": handle_ipv6,
    "resize": handle_resize,
    "volumes": handle_volumes,
    "users": handle_users,
    "monitor": handle_monitor,
    "autostart": handle_autostart,
    "openport": handle_openport,
    "workflows": handle_workflows,
    "profiles": handle_profiles,
    "clients": handle_clients,
    "health": handle_health,
    "presets": handle_presets,
    "subscription": handle_subscription,
    "traffic": handle_traffic,
    "quota": handle_quota,
    "tasks": handle_tasks,
    "load": handle_load,
    "cost": handle_cost,
    "mfa": handle_mfa,
    "daily": handle_daily,
    "emails": handle_emails,
    "restart": handle_restart,
    "upgrade": handle_upgrade,
    "regions": handle_regions,
    "logs": handle_logs,
    "batch": handle_batch,
    "a1health": handle_a1_health,
    "traffic_guard": handle_traffic_guard,
    "keepalive": handle_keepalive,
    "sslwatch": handle_sslwatch,
    "objectstorage": handle_objectstorage,
    "email": handle_email,
    "plans": handle_plans,
    "rename": handle_rename_prompt,
    "reinstall": handle_reinstall_prompt,
    "console": handle_console,
    "memballoon": handlers_client.handle_memballoon,
    "memclear": handlers_client.handle_memclear,
}


async def dispatch_oracle_action(item, update, context):
    """按菜单项 key 路由到本模块的 handler。

    返回 True 表示已处理，False 表示本模块未实现该项
    （handlers.py 可再决定如何提示用户）。
    """
    handler = _DISPATCH.get(item)
    if handler is None:
        return False
    await handler(update, context)
    return True
