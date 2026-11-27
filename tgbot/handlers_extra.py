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
  - 其余回调（b:/t:/p:/r:/v:/bb:/ba:/g:/k:/s:/os:/pl:/cl:/ip_create/ip_delete:/u:）
    由 bot.py 直接注册到本模块的对应函数，不经过 handlers.py。
    开机表单矩阵的回调（bf: 前缀）与快捷开机一键启动（b:load:）也在
    本模块，表单状态机与键盘在 tgbot/boot_form.py 里。
  - 改名 / 重装 / SSL 添加域名 / Email 发件人 / 用户改描述 / 用户改邮箱 /
    用户删除需要用户回复文字：bot.py
    注册 text_input_callback（MessageHandler，filters.TEXT & ~filters.COMMAND），
    只在 user_data["pending_input"] 存在时处理，否则忽略。
    开机表单的自定义值（boot_*）与开机脚本粘贴也走这里；
    开机脚本文件上传走 boot_form_document_callback（Document 消息）。
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

import html
import ipaddress
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from tgbot import config as bot_config
from tgbot import handlers_client
from tgbot import i18n as bot_i18n
from tgbot import menu_tracker
from tgbot.rescue import handle_rescue
from tgbot.i18n import lang_of
from tgbot.bark import (
    bark_configured,
    generate_bark_key,
    load_bark_config,
    make_key_qr_png,
    mask_bark_url,
    save_bark_config,
    send_bark_test,
)
from tgbot.billing import (
    FREE_SHAPES,
    check_account_billing,
    collect_billing_alerts,
    fmt_billing_alerts,
    load_billing_state,
    save_billing_state,
)
from tgbot.boot_form import (
    SCRIPT_MAX_CHARS,
    boot_form_keyboard,
    build_preset,
    build_user_data,
    default_amd_form,
    deploy_region_mismatch,
    fmt_boot_form_summary,
    fmt_form_text,
    form_from_preset,
    generate_root_password,
    get_deploy_defaults,
    new_form,
    new_user_form,
    set_custom,
    set_script,
    toggle_option,
    validate_form,
)
from tgbot.keyboards import (
    DOMAIN_RECORDS_LIMIT,
    DNS_ADD_TYPES,
    back_to_oracle_keyboard,
    bark_genkey_confirm_keyboard,
    bark_input_cancel_keyboard,
    bark_keyboard,
    batch_action_keyboard,
    batch_select_keyboard,
    boot_confirm_keyboard,
    boot_preset_keyboard,
    cf_add_mode_keyboard,
    cf_cancel_keyboard,
    cf_config_keyboard,
    cf_delete_confirm_keyboard,
    cf_extract_hosts_keyboard,
    cf_manage_keyboard,
    confirm_keyboard,
    domain_add_cancel_keyboard,
    domain_add_confirm_keyboard,
    domain_add_type_keyboard,
    domain_delete_confirm_keyboard,
    domain_delete_pick_keyboard,
    domain_records_keyboard,
    domain_zones_keyboard,
    gfw_input_cancel_keyboard,
    gfw_keyboard,
    instance_list_keyboard,
    keepalive_keyboard,
    oracle_menu_keyboard,
    profile_keyboard,
    profile_regions_keyboard,
    quota_accounts_keyboard,
    quota_regions_keyboard,
    ssl_add_cancel_keyboard,
    ssl_watch_keyboard,
    task_list_keyboard,
    traffic_guard_keyboard,
)
from tgbot.gfw import (
    GFW_CHECK_INTERVAL_MINUTES,
    is_gfw_configured,
    load_gfw_config,
    mask_api_key,
    record_last_check,
    run_monitor_cycle,
    set_api_key,
    set_instance_enabled,
)
from tgbot.cpu_policy import (combined_boot_keys,
                              get_allowed_cpu_models, make_cpu_check_hook)
from tgbot import task_store
from tgbot.task_store import start_cancel_watcher
from tgbot.handlers_client import put_remote_file, run_remote_command
from tgbot.watchdog import (
    WatchdogError,
    disable_watchdog,
    guard_status_label,
    install_watchdog,
    parse_probe_output,
    probe_command,
)
from tgbot.notify import (DEFAULT_BOOT_DISK_GB, fmt_notify_outcome,
                          fmt_paid_instance_alert, notify_boot_success,
                          notify_dual, start_ip_followup)
from tgbot.backup_watch import start_backup_watch
from tgbot.paid_profiles import is_paid_profile, set_paid_profile
from tgbot.monitoring import (
    KEEPALIVE_MAX_FAILURES,
    TRAFFIC_GUARD_RATE_LIMIT,
    current_month_transfer_gb,
    fetch_city_for_ip,
    fmt_threshold_gb,
    GUARD_ENSURE_INSTALL_TIMEOUT_SECONDS,
    guard_block_command,
    guard_ensure_vnstat_command,
    guard_limit_command,
    guard_unblock_command,
    guard_vnstat_command,
    is_valid_domain,
    keepalive_enabled_instances,
    keepalive_record_failure,
    keepalive_record_success,
    keepalive_reset_instance,
    load_keepalive,
    load_keepalive_state,
    load_ssl_watch,
    load_traffic_guard,
    next_traffic_guard_action,
    normalize_domain,
    parse_vnstat_monthly_gb,
    record_traffic_guard_trigger,
    save_keepalive,
    save_keepalive_state,
    save_ssl_watch,
    save_traffic_guard,
    should_trigger_traffic_guard,
    ssl_reminder_due,
    traffic_guard_action_label,
)
from tgbot.services import (
    TASKS,
    fmt_duration,
    fmt_shape_spec,
    fmt_error,
    fmt_instance_list,
    fmt_quota_summary,
    fmt_task_list,
    get_cli,
    get_compartment,
    get_current_profile,
    get_current_region,
    require_allowed,
    resolve_boot_backup,
    resolve_instance,
    resolve_volume,
    run_sync,
    set_current_profile,
    set_current_region,
    store_boot_backups,
    store_instances,
    store_volumes,
    sort_volumes_by_group,
    volume_group_title,
    VOLUME_GROUP_ORDER,
    VOLUME_GROUP_TITLES,
    VOLUME_GROUP_OTHER_TITLE,
)
from oci.boot_backup import (
    DEFAULT_RESTORE_MEMORY_GB,
    DEFAULT_RESTORE_OCPUS,
    DEFAULT_RESTORE_SHAPE,
    check_backup_quota,
    create_boot_volume_backup,
    create_boot_volume_backup_with_limit_retry,
    delete_boot_volume_backup,
    get_source_instance_shape,
    list_boot_volume_backups,
    restore_boot_volume_backup,
)
from oci.boot_swap import (
    BootSwapError,
    check_boot_volume_space,
    get_boot_volume_attachment,
    swap_boot_volume,
)
from oci.client import OCIClient, QuotaExceededError
from oci.dd_init import DD_SSH_PORT, start_dd_init
from oci.email import create_sender, is_valid_email
from oci.grabber import TARGET_CPU, Grabber, GrabCancelledError, GrabResult
from oci.iam import (
    MAX_DESCRIPTION_LEN,
    add_user_to_group,
    create_api_key,
    create_bot_permission_user,
    create_or_reset_ui_password,
    create_user,
    create_user_in_admin_group,
    delete_api_key,
    delete_user,
    get_group_id_by_name,
    get_profile_api_fingerprint,
    get_profile_tenancy_id,
    get_profile_user_id,
    is_last_admin,
    list_api_keys,
    update_user,
    upload_api_key,
)
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
    attach_public_ips,
    change_public_ip,
    create_ipv6,
    create_reserved_ip,
    delete_ipv6,
    delete_public_ip,
    get_public_ip,
    list_ipv6_for_vnic,
    list_public_ips,
    list_vnics,
    wait_for_public_ip,
)
from oci.objectstorage import (
    delete_bucket,
    delete_object,
    list_buckets,
    list_objects,
)
from oci.quota import (
    SHAPE_A1_FLEX,
    a1_health_check,
    can_launch,
    can_launch_batch,
    check_a1_single_instance_cap,
    get_free_tier_summary,
)
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
from web.tasks import PLAN_DEFAULTS, PLANS

try:
    # Cloudflare DNS client: clouds/cloudflare.py, written by a parallel worker. Cloudflare DNS 客户端：clouds/cloudflare.py 由另一工人并行编写。
    # Agreed interface: get_client() -> CloudflareClient; 约定接口：get_client() -> CloudflareClient；
    # list_zones() / list_dns_records(zone_id, type=None, name=None); list_zones() / list_dns_records(zone_id, type=None, name=None)；
    # CloudflareError has a .kind field (config/auth/not_found/rate_limit/api/network). CloudflareError 有 .kind 字段（config/auth/not_found/rate_limit/api/network）。
    from clouds.cloudflare import (
        CloudflareError,
        check_ssl_expiry,
        get_client,
    )
except ImportError:
    get_client = None
    check_ssl_expiry = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（约定接口：有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind


try:
    # Cloudflare credential management: config file first, env vars as fallback; manual add and extraction from the server's 1.sh both go through here. Cloudflare 凭据管理：配置文件优先，环境变量兜底；手动添加 / 从服务器 1.sh 提取都走这里。
    from tgbot.cf_config import (
        clear_cf_config,
        extract_cf_from_host,
        get_cf_client as get_configured_cf_client,
        mask_cf_email,
        resolve_cf_credentials,
        validate_and_save,
    )
except ImportError:
    clear_cf_config = None
    extract_cf_from_host = None
    get_configured_cf_client = None
    mask_cf_email = None
    resolve_cf_credentials = None
    validate_and_save = None


logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# Boot presets: keys are e2 / a1small / a1full. 开机预设：key 为 e2 / a1small / a1full。
# image_id / subnet_id / availability_domain / ssh_keys_file are placeholders; replace with real values at deploy time (image ID via `oci compute image list`, subnet ID in the OCI console under VCN, availability domain like "xxxx:AP-SINGAPORE-1-AD-1"). image_id / subnet_id / availability_domain / ssh_keys_file 都是占位符，部署时改成真实值（镜像 ID 用 `oci compute image list` 查，子网 ID 在 OCI 控制台 VCN 里看，可用域如 "xxxx:AP-SINGAPORE-1-AD-1"）。
BOOT_PRESETS = {
    "e2": {
        "shape": "VM.Standard.E2.1.Micro",
        "ocpus": 1,
        "memory_gb": 1,
        "target_cpu": TARGET_CPU,  # Keep only AMD EPYC 7J13. 只留 AMD EPYC 7J13 的。
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
        "target_cpu": None,  # A1 does not filter by CPU; keep whatever is grabbed. A1 不筛 CPU，抢到就留。
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
# Months to look back for usage queries (traffic / cost menus). 用量查询回溯的月数（流量 / 费用菜单）。
USAGE_MONTHS = 3
# Tail lines read by the log menu. 日志菜单读取的尾部行数。
LOG_TAIL_LINES = 30
# Body limit for a single Telegram message (headroom kept; long logs are truncated). 单条 Telegram 消息的正文上限（留余量，日志超长时截断）。
LOG_MAX_CHARS = 3500
# Recent entries listed by the workflow menu. 工作流菜单列出的最近条数。
WORKFLOW_LIST_LIMIT = 10
# A1 resize presets: (OCPU, memory GB). A1 升/降配预设：(OCPU, 内存 GB)。
RESIZE_PRESETS = [(1, 6), (2, 12), (4, 24)]
# Boot volume resize target size (GB). 启动盘扩容目标容量（GB）。
VOLUME_RESIZE_TARGETS = [100, 200]
# IP management: display name for a reserved IP created from the menu (required by OCI). IP 管理：从菜单创建保留 IP 时的显示名（OCI 要求必填）。
RESERVED_IP_DISPLAY_NAME = "bot-reserved-ip"
# Shape names (used to judge resize). 规格名（升降配时判断用）。
SHAPE_E2_MICRO = "VM.Standard.E2.1.Micro"
SHAPE_A1_FLEX = "VM.Standard.A1.Flex"
# Chinese names of batch actions (shared by confirm prompts and result notices). 批量操作的动作中文名（确认提示与结果通知共用）。
_BATCH_ACTION_TEXT = {
    "start": "开机",
    "stop": "关机",
    "restart": "重启",
    "terminate": "删除",
}
# A1 health-check severity -> display symbol. A1 体检建议的 severity -> 展示符号。
_SEVERITY_MARK = {
    "error": "❌",
    "warning": "⚠️",
    "info": "💡",
    "ok": "✅",
}
# Cloud panel port (the port opened by the open-panel-port feature; must match web/config.py PORT). 云面板端口（开放云面板端口功能放行的端口，与 web/config.py 的 PORT 一致）。
PANEL_PORT = 9527
# Security list OCID of the panel server's subnet (fill the real value at deploy time via env var MBOT_PANEL_SECURITY_LIST_ID; find it in the OCI console under Networking -> Security Lists, or via oci network security-list list --compartment-id <compartment OCID>). 面板服务器所在子网的安全组 OCID（部署时用环境变量 MBOT_PANEL_SECURITY_LIST_ID 填真实值；查法：OCI 控制台 → 网络 → 安全列表，或 oci network security-list list --compartment-id <compartment OCID>）。
# While still a placeholder (unconfigured), the open-panel-port menu prompts for configuration first and does nothing. 未配置（仍是占位符）时“开放云面板端口”菜单会提示先配置，不执行。
PANEL_SECURITY_LIST_ID = os.environ.get(
    "MBOT_PANEL_SECURITY_LIST_ID", "ocid1.securitylist.oc1..xxxxxxxx")
# Panel server SSH address (for UFW allow): empty string = the panel runs on this machine (bot and panel on the same server, the current deploy setup), so run ufw directly; otherwise ssh to that host (port 14422, root key login, see client_ops). 面板服务器 SSH 地址（UFW 放行用）：空字符串 = 面板就在本机（机器人与面板跑在同一台服务器上，部署现状如此），直接执行 ufw；否则 ssh 到该主机执行（端口 14422，root 密钥登录，见 client_ops）。
PANEL_SSH_HOST = ""
# ---- 开机表单矩阵（菜单 1，tgbot/boot_form.py） ----
# Prompt text shown after tapping "Custom" (kind is the pending_input kind). 点“自定义”后提示用户回复的文字（kind 是 pending_input 的 kind）。
_BOOT_CUSTOM_PROMPTS = {
    "boot_cpu": "请输入形状名（例如 VM.Standard.A1.Flex）：",
    "boot_os": "请输入镜像 OCID（例如 ocid1.image.oc1.ap-singapore-1.……）：",
    "boot_ocpus": "请输入核心数（1–80 的整数）：",
    "boot_mem": "请输入内存 GB 数（1–1024 的整数）：",
    "boot_disk": "请输入硬盘 GB 数（47–200 的整数）：",
    "boot_count": "请输入要创建的台数（1–10 的整数）：",
    "boot_delay": "请输入延迟秒数（1–3600 的整数）：",
    "boot_script": ("请直接粘贴开机脚本内容，或发送 .sh/.txt 文件"
                    f"（{SCRIPT_MAX_CHARS // 1024}KB 以内）。\n"
                    "脚本以 #! 开头会被当作 Shell 脚本执行；以 #cloud-config "
                    "开头会被当作 cloud-init 配置（此时不能同时开 root "
                    "密码登录）。"),
}
# =====================================================


# ---------------- 通用小工具 ----------------

def _unwrap(result):
    """OCI CLI 的 get/list 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


async def _reply(update, context, text, reply_markup=None,
               parse_mode=None):
    """统一回复：tgbot.menu_tracker.reply 的薄包装（带选择框清理，
    每聊天只留一个活动选择框；点完删掉多余的选择框，只留结果）。

    返回新发送的消息（Message），供调用方记录 ID 以便后续清理；
    没有可回复的消息时返回 None。
    """
    return await menu_tracker.reply(
        update, context, text, reply_markup=reply_markup,
        parse_mode=parse_mode)


# Cleanup of in-flow messages (keep only the final result message). 流程中间消息清理（只留这一条结果信息）。
# The text-input prompt message ID lives in user_data["pending_prompt_ids"]; 文字输入提示消息的 ID 存在 user_data["pending_prompt_ids"]；
# once the user replies, the prompt is useless and text_input_callback deletes it on receipt; 用户回复文字后提示已无用，text_input_callback 收到文字即删；
# the confirm dialog is deleted by handle_confirm after the action completes. 二次确认框由 handle_confirm 包装在操作完成后删除。
_PROMPT_IDS_KEY = "pending_prompt_ids"


async def _delete_message_safe(context, chat_id, message_id):
    """删一条消息；已删/无权限等失败时只记 debug 日志，不影响主流程。"""
    if not message_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id,
                                         message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
        logger.debug("删除消息 %s 失败：%s", message_id, exc)


async def _delete_tracked_prompts(context, chat_id):
    """删除本次流程攒下的全部文字输入提示消息，并清空记录。"""
    ids = context.user_data.pop(_PROMPT_IDS_KEY, None) or []
    for message_id in ids:
        await _delete_message_safe(context, chat_id, message_id)
    # A prompt with a cancel keyboard is itself a live selection box: delete its tracking too after deleting it, or the next reply will try to delete an already-deleted message. 带取消键盘的提示本身就是活动选择框，删了之后跟踪也要清掉，否则下次回复还会去删一条已删的消息。
    live_id = context.user_data.get(menu_tracker.LIVE_MENU_KEY)
    if live_id is not None and live_id in ids:
        menu_tracker.forget_live_menu(context)


# In-flow messages of the account/region switch (account list / region list): deleted together once the switch completes, leaving only the main menu message. 切换账号/区域流程的中间消息（账号列表 / 区域列表）：切换完成后统一删除，只留主菜单一条消息。ID 存在 user_data 里。
_SWITCH_IDS_KEY = "switch_msg_ids"
# Anti-double-click window (seconds) for switch callbacks: a double tap sends two callbacks, and without this guard the user gets two "switched to account" messages. 切换回调的防重复点击窗口（秒）：用户双击按钮时 Telegram 会发两个回调，不拦会连发两条“已切换到账号”。
_SWITCH_DEDUP_SECONDS = 5


def _track_switch_message(context, message):
    """记下切换流程里新发的消息 ID（账号列表 / 区域列表）。"""
    if message is not None and getattr(message, "message_id", None):
        context.user_data.setdefault(_SWITCH_IDS_KEY, []).append(
            message.message_id)


async def _delete_tracked_switch_messages(context, chat_id, except_id=None):
    """删除切换流程攒下的中间消息（except_id 的那条不删——调用方要
    把它就地编辑成主菜单），并清空记录。"""
    ids = context.user_data.pop(_SWITCH_IDS_KEY, None) or []
    for message_id in ids:
        if except_id is not None and message_id == except_id:
            continue
        await _delete_message_safe(context, chat_id, message_id)


def _is_duplicate_switch(context, data):
    """同一切换回调在窗口期内重复到达时返回 True（双击去重）。

    首次到达时记录 (data, 时间)，返回 False；重复到达时返回 True。
    """
    now = time.monotonic()
    last = context.user_data.get("_last_switch")
    if last and last[0] == data and now - last[1] < _SWITCH_DEDUP_SECONDS:
        return True
    context.user_data["_last_switch"] = (data, now)
    return False


def _switch_menu_account_label(context):
    """主菜单顶行用的当前账号显示名（与 handlers._current_account_label
    同逻辑：账号名 + 非主区时后缀工作区域）。"""
    profile = get_current_profile(context)
    label = get_cached_profile_display_name(profile)
    current_region = get_current_region(context)
    home_region = bot_config.get_region(profile)
    if current_region != home_region:
        label += f" · {bot_config.get_region_label(current_region)}"
    return label


async def _show_oracle_menu_after_switch(update, context, confirm_line):
    """切换成功后：删掉切换流程的中间消息，把当前消息就地编辑成
    /oracle 主菜单（第 1 页），确认行放在菜单文字最上面。

    编辑失败时（消息已被删等）退回新发一条主菜单，不让用户卡在
    旧页面。
    """
    query = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else None
    current_id = None
    if query is not None and query.message is not None:
        current_id = query.message.message_id
    if chat_id is not None:
        await _delete_tracked_switch_messages(context, chat_id,
                                              except_id=current_id)
    from tgbot.handlers import _oracle_menu_text  # Deferred import to avoid a circular import. 延迟导入，防循环引用。
    lang = lang_of(update, context)
    text = f"{confirm_line}\n\n{_oracle_menu_text(lang)}"
    keyboard = oracle_menu_keyboard(
        0, states=get_menu_states(context),
        current_account=_switch_menu_account_label(context), lang=lang)
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await menu_tracker.safe_edit(query, text, reply_markup=keyboard)
            return
        except Exception as exc:  # noqa: BLE001 - 编辑失败就新发一条
            logger.debug("切换后编辑主菜单失败，改发新消息：%s", exc)
    await _reply(update, context, text, reply_markup=keyboard)


async def _reply_prompt(update, context, text, reply_markup=None):
    """发一条文字输入提示：先删掉之前未清理的旧提示（防重复点按钮
    攒出多条），再发新的并记录 ID（用户回复后由 text_input_callback
    删除；开机表单的提示不走这里——表单自己编辑提示消息）。

    返回新发送的消息。
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        await _delete_tracked_prompts(context, chat_id)
    msg = await _reply(update, context, text, reply_markup=reply_markup)
    if msg is not None and getattr(msg, "message_id", None):
        context.user_data.setdefault(_PROMPT_IDS_KEY, []).append(
            msg.message_id)
    return msg


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


def update_preset(idx, preset, path=None):
    """原地更新 presets.json 第 idx 条（编辑保存用），返回完整列表。

    下标越界时返回原列表不写盘。saved_at 刷新为现在。
    """
    path = path or bot_config.PRESETS_FILE
    presets = load_presets(path)
    if idx < 0 or idx >= len(presets):
        return presets
    entry = dict(preset)
    entry["saved_at"] = datetime.now(timezone.utc).isoformat()
    presets[idx] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    return presets


def delete_preset(idx, path=None):
    """删掉 presets.json 第 idx 条（删除确认后用），返回完整列表。

    下标越界时返回原列表不写盘。
    """
    path = path or bot_config.PRESETS_FILE
    presets = load_presets(path)
    if idx < 0 or idx >= len(presets):
        return presets
    del presets[idx]
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


def _usage_currency(items, fallback=""):
    """从用量 items 里取第一个非空币种。

    OCI 有时在 0 元项里返回单个空格当币种（" " 是真值，直接用会
    覆盖掉真正的 SGD，2026-01-01 真实数据实测 ACCOUNT2 的 0 元项
    带 " "）——必须 strip 后再判断。
    """
    for item in items:
        currency = str(item.get("currency") or "").strip()
        if currency:
            return currency
    return fallback


def fmt_usage_monthly(items, months=USAGE_MONTHS, lang="zh"):
    """按月汇总费用与流量（service 含 "Data Transfer" 的
    computed-quantity，按 API 返回值以 GB 计）。"""
    if not items:
        return bot_i18n.t(lang, "cost.no_usage", months=months)
    by_month = {}
    total = 0.0
    currency = ""
    for item in items:
        month = (item.get("time-usage-started") or "")[:7] or "未知"
        amount = float(item.get("computed-amount") or 0)
        currency = _usage_currency([item], currency)
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


def fmt_usage_total(items, months=USAGE_MONTHS, lang="zh"):
    """汇总近 N 个月的总费用与币种。"""
    if not items:
        return bot_i18n.t(lang, "cost.no_usage", months=months)
    total = sum(float(i.get("computed-amount") or 0) for i in items)
    currency = _usage_currency(items)
    return bot_i18n.t(lang, "cost.total", months=months, total=total,
                      currency=currency)


def fmt_usage_totals(entries, months=USAGE_MONTHS, lang="zh"):
    """多账号近 N 个月总费用：entries 是 [(显示名, items 或 None)]。

    显示名是账号的主区中文名（多区账号后缀区数），与订阅信息页一致；
    查失败的账号（items 为 None）显示“查询失败”，不影响其他账号；
    最后附全部账号合计（用户要求 2026-01-01：默认一次查全部账号）。
    """
    if not entries:
        return bot_i18n.t(lang, "cost.no_accounts")
    sections = [bot_i18n.t(lang, "cost.totals_title", months=months,
                           count=len(entries))]
    grand_total = 0.0
    currency = ""
    for label, items in entries:
        if items is None:
            sections.append(f"• {label}：{bot_i18n.t(lang, 'cost.failed')}")
            continue
        total = sum(float(i.get("computed-amount") or 0) for i in items)
        currency = _usage_currency(items, currency)
        grand_total += total
        sections.append(f"• {label}：{total:.2f} {currency}".rstrip())
    sections.append("")
    sections.append(bot_i18n.t(lang, "cost.grand_total",
                               total=grand_total, currency=currency).rstrip())
    return "\n".join(sections)


# ---------------- 订阅 / 用户 / 工作流 / 区域（cli.run 直调） ----------------

def get_tenancy(cli, compartment_id):
    """查租户订阅信息（iam tenancy get），返回解包后的字典。"""
    return _unwrap(cli.run(["iam", "tenancy", "get",
                            "--tenancy-id", compartment_id])) or {}


def fmt_tenancy(data, lang="zh"):
    """订阅信息全文。"""
    if not isinstance(data, dict) or not data:
        return bot_i18n.t(lang, "subscription.unparseable")
    lines = [
        bot_i18n.t(lang, "subscription.title_single") + "：",
        "",
        f"{bot_i18n.t(lang, 'subscription.name')}：{data.get('name', '?')}",
        f"{bot_i18n.t(lang, 'subscription.home_region')}："
        f"{data.get('home-region-key', '?')}",
    ]
    if data.get("description"):
        lines.append(f"{bot_i18n.t(lang, 'subscription.description')}："
                     f"{data['description']}")
    return "\n".join(lines)


def fmt_tenancies(entries, lang="zh"):
    """多账号订阅信息全文：entries 是 [(显示名, tenancy dict 或 None)]。

    显示名是账号的主区中文名（多区账号后缀区数），与切换菜单一致；
    查失败的账号（data 为 None）显示“查询失败”，不影响其他账号。
    有几个账号就显示几个（用户要求 2026-01-01）。
    """
    if not entries:
        return bot_i18n.t(lang, "subscription.no_accounts")
    sections = [bot_i18n.t(lang, "subscription.title_count",
                           count=len(entries))]
    for label, data in entries:
        if not isinstance(data, dict) or not data:
            body = bot_i18n.t(lang, "subscription.failed")
        else:
            parts = [f"{bot_i18n.t(lang, 'subscription.name')}："
                     f"{data.get('name', '?')}",
                     f"{bot_i18n.t(lang, 'subscription.home_region')}："
                     f"{data.get('home-region-key', '?')}"]
            if data.get("description"):
                parts.append(f"{bot_i18n.t(lang, 'subscription.description')}："
                             f"{data['description']}")
            body = "\n".join(parts)
        sections.append(f"{label}\n{body}")
    return "\n\n".join(sections)


def list_users(cli, compartment_id):
    """只读列出租户下的 IAM 用户（SDK 直调，避免启动 OCI CLI 子进程）。"""
    return cli.sdk_list_users(compartment_id) or []


def fmt_user_list(users):
    """用户列表标题（只剩标题 + 数量；逐个用户信息在按钮里，详情页看邮箱）。

    与实例列表同款：按钮是唯一的列表，文字不再逐条重复（用户反馈
    2026-01-01：原来文字列表和按钮列表同时出现，邮箱换行看着乱）。
    """
    if not users:
        return "该租户下没有 IAM 用户。"
    return (f"👥 云账户（{len(users)} 个用户）：\n\n"
            "点下面按钮查看详情或操作。")


def _user_button_text(user):
    """用户按钮文本：👤 用户名；非 ACTIVE 状态后缀中文状态词。

    邮箱不在按钮里（太长会换行），点进详情页看。
    """
    name = user.get("name", "?")
    state = user.get("lifecycle-state", "ACTIVE")
    if state == "ACTIVE":
        return f"👤 {name}"
    label = {"INACTIVE": "已停用"}.get(state, state)
    return f"👤 {name} · {label}"


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


# Subscription-region cache: region subscriptions rarely change, so cache for 1 hour instead of calling OCI once per account every time the switch menu / quota page opens. Store the full list; the region count (switch menu suffix) and region names (quota page submenu) both derive from it. 账号订阅区域缓存：region-subscription 很少变，缓存 1 小时，避免每次打开切换菜单/额度页都对每个账号调一次 OCI。存完整列表，区数（切换菜单后缀）与区域名（额度页二级菜单）都从这里派生。
PROFILE_REGION_COUNT_TTL = timedelta(hours=1)
_profile_regions = {}
_profile_regions_at = None


def get_profile_regions():
    """返回 {profile: [region-subscription dict, ...]}；单个账号查失败时跳过。

    结果缓存 1 小时；缓存命中时直接返回副本（调用方不要改原 dict）。
    """
    global _profile_regions, _profile_regions_at
    now = datetime.now(timezone.utc)
    if (_profile_regions and _profile_regions_at is not None
            and now - _profile_regions_at < PROFILE_REGION_COUNT_TTL):
        return {k: list(v) for k, v in _profile_regions.items()}
    regions = {}
    for name in bot_config.PROFILES:
        try:
            cli = bot_config.get_oci_client(profile=name)
            cfg = bot_config.get_profile_config(name)
            regions[name] = list_regions(cli, cfg["compartment_id"])
        except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
            continue
    _profile_regions = regions
    _profile_regions_at = now
    return {k: list(v) for k, v in regions.items()}


def get_profile_region_counts():
    """返回 {profile: 订阅区域数}；由 get_profile_regions 派生，共享缓存。"""
    return {name: len(regs)
            for name, regs in get_profile_regions().items()}


def get_cached_profile_display_name(profile):
    """返回账号显示名，只读缓存，不调 OCI。

    缓存里有该账号的订阅列表时带“（N 个区）”后缀，否则只有主区名。
    供 /oracle 菜单顶行用（菜单每次翻页都调，调 OCI 太慢）；缓存由
    切换菜单/额度页填充，机器人启动时也会后台预热一次。
    """
    count = None
    if _profile_regions:
        regs = _profile_regions.get(profile)
        if regs is not None:
            count = len(regs)
    return bot_config.profile_display_name(profile, count)


# ---------------- IPv6 ----------------


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

def fmt_ip_overview(reserved_ips, instance_ips, lang="zh"):
    """IP 管理全文。

    reserved_ips：list_public_ips 的返回值（保留 IP）。
    instance_ips：[(实例名, 公网 IP 或 None), ...]。
    """
    lines = [bot_i18n.t(lang, "ip.overview_title"), "",
             bot_i18n.t(lang, "ip.reserved")]
    if not reserved_ips:
        lines.append(bot_i18n.t(lang, "ip.no_reserved"))
    for ip in reserved_ips:
        addr = ip.get("ip-address", "?")
        bound = bot_i18n.t(lang, "ip.bound" if ip.get("assigned-entity")
                           else "ip.unbound")
        lines.append(f"• {addr}（{ip.get('lifecycle-state', '?')}，{bound}）")
    lines.append("")
    lines.append(bot_i18n.t(lang, "ip.instance_ips"))
    if not instance_ips:
        lines.append(bot_i18n.t(lang, "ip.no_instances"))
    for name, addr in instance_ips:
        lines.append(f"• {name}：{addr or bot_i18n.t(lang, 'instances.no_public_ip')}")
    lines.append("")
    lines.append(bot_i18n.t(lang, "ip.change_hint"))
    return "\n".join(lines)


def fmt_volume_list(volumes, orphans, total_gb):
    """磁盘管理全文：只剩标题 + 各组数量 + 孤儿盘提示。

    每块盘的名字与容量都在按钮上（见 volume_list_keyboard），
    文字不再逐块重复，否则文字列表和按钮列表会同时出现两份
    （2026-01-01 用户截图反馈）。total_gb 只统计未删除的盘
    （见 oci.volume.total_boot_volume_gb），所以容量标在可用组
    后面，不跟总数混在一起。空组不显示。
    """
    if not volumes:
        return "💾 启动盘：\n\n• 无启动盘"
    ordered = sort_volumes_by_group(volumes)
    counts = {}
    for _idx, vol in ordered:
        title = volume_group_title(vol.get("lifecycle-state"))
        counts[title] = counts.get(title, 0) + 1
    parts = []
    for state in VOLUME_GROUP_ORDER:
        title = VOLUME_GROUP_TITLES[state]
        if title not in counts:
            continue
        suffix = f"（{total_gb}GB）" if state == "AVAILABLE" else ""
        parts.append(f"{title} {counts[title]} 块{suffix}")
    other = counts.get(VOLUME_GROUP_OTHER_TITLE)
    if other:
        parts.append(f"{VOLUME_GROUP_OTHER_TITLE} {other} 块")
    lines = ["💾 启动盘：", " · ".join(parts)]
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


def _fmt_backup_time(value):
    """备份 ISO 时间转 "YYYY-MM-DD HH:MM"（显示用）。"""
    text = (value or "")[:16].replace("T", " ")
    return text or "未知时间"


def fmt_boot_backup_list(backups, count, limit, full, paid=False):
    """启动卷备份列表全文（M_Bot 独有功能）。

    paid=True 时是付费账号：备份不限 5 个，标题不再提免费额度。
    """
    if paid:
        lines = [f"💾 启动卷备份（付费账号，备份不限 5 个，共 {count} 个）：",
                 ""]
    else:
        lines = [f"💾 启动卷备份（免费额度 {count}/{limit}"
                 f"{'，已满' if full else ''}）：", ""]
    if not backups:
        lines.append("• 暂无备份")
    for backup in backups:
        name = backup.get("display-name") or "?"
        size = backup.get("size-in-gbs", "?")
        state = backup.get("lifecycle-state", "?")
        created = _fmt_backup_time(backup.get("time-created"))
        lines.append(f"• {name}：{size}GB（{state}，{created}）")
    lines.append("")
    lines.append("点下面按钮恢复（开一台新实例，原机不动）或删除。")
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


def volume_list_keyboard(volumes, orphans=None):
    """磁盘列表键盘：按状态分组，每组一行标题按钮 + 每块盘一个按钮。

    分组顺序与 fmt_volume_list 一致（AVAILABLE → TERMINATING →
    TERMINATED → 其他），共用 sort_volumes_by_group。标题按钮的
    callback_data 是 "v:noop"（点它只应答，不做任何事）；磁盘按钮
    的 callback_data 带原列表下标（不是分组后的下标），因为
    store_volumes 存的是原始顺序列表，回调按原下标取回。
    孤儿盘（在 orphans 里）的按钮带 🗑️ 标记。
    """
    orphan_ids = {v.get("id") for v in (orphans or [])}
    ordered = sort_volumes_by_group(volumes)
    counts = {}
    for _idx, vol in ordered:
        title = volume_group_title(vol.get("lifecycle-state"))
        counts[title] = counts.get(title, 0) + 1
    rows = []
    current_group = None
    for idx, vol in ordered:
        group = volume_group_title(vol.get("lifecycle-state"))
        if group != current_group:
            current_group = group
            rows.append([InlineKeyboardButton(
                f"{group}（{counts[group]} 块）：", callback_data="v:noop")])
        name = vol.get("display-name") or (vol.get("id") or "")[-4:]
        mark = " 🗑️" if vol.get("id") in orphan_ids else ""
        rows.append([InlineKeyboardButton(
            f"{name}（{vol.get('size-in-gbs')}GB）{mark}",
            callback_data=f"v:detail:{idx}")])
    rows.append([InlineKeyboardButton("💾 启动卷备份", callback_data="bb:list")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


def volume_actions_keyboard(idx, state=None):
    """单个磁盘的操作键盘：扩容 / 备份 / 删除。

    已删除（TERMINATED）或删除中（TERMINATING）的盘是墓碑记录，
    扩容/备份/删除都无效，只给返回按钮。
    """
    if (state or "").upper() in ("TERMINATED", "TERMINATING"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ 磁盘列表", callback_data="o:volumes")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 扩容", callback_data=f"v:resize:{idx}"),
         InlineKeyboardButton("💾 备份启动卷", callback_data=f"v:backup:{idx}"),
         InlineKeyboardButton("🗑️ 删除", callback_data=f"v:delete:{idx}")],
        [InlineKeyboardButton("◀ 磁盘列表", callback_data="o:volumes")],
    ])


def boot_backup_list_keyboard(backups, paid=False):
    """启动卷备份列表键盘：每个备份一行恢复/删除按钮。

    只有 AVAILABLE 状态的备份才能恢复，非 AVAILABLE 的备份只给删除按钮。
    底部：多选删除入口（有备份时才显示）+ 付费账号开关 + 返回磁盘列表。
    """
    rows = []
    for idx, backup in enumerate(backups):
        name = backup.get("display-name") or (backup.get("id") or "")[-4:]
        row = []
        if (backup.get("lifecycle-state") or "").upper() == "AVAILABLE":
            row.append(InlineKeyboardButton(
                f"♻️ 恢复「{name}」", callback_data=f"bb:restore:{idx}"))
        row.append(InlineKeyboardButton(
            f"🗑️ 删除「{name}」", callback_data=f"bb:delete:{idx}"))
        rows.append(row)
    if backups:
        rows.append([InlineKeyboardButton("🗑️ 多选删除",
                                         callback_data="bb:ms")])
    rows.append([InlineKeyboardButton(
        "🆓 设为免费账号" if paid else "💳 设为付费账号",
        callback_data="bb:paid")])
    rows.append([InlineKeyboardButton("◀ 磁盘列表", callback_data="o:volumes")])
    return InlineKeyboardMarkup(rows)


def boot_backup_multiselect_keyboard(backups, selected, prefix="bb"):
    """备份多选删除键盘：每行一个勾选按钮 + 删除所选 + 退出多选。

    selected 是已选备份下标的集合。删除所选按钮带已选数量，
    未选任何备份时不显示（防误删空选）。prefix 是回调前缀：
    全部备份列表用 "bb"，本实例备份列表用 "ib"。
    """
    rows = []
    for idx, backup in enumerate(backups):
        name = backup.get("display-name") or (backup.get("id") or "")[-4:]
        mark = "☑️" if idx in selected else "☐"
        rows.append([InlineKeyboardButton(
            f"{mark} {name}", callback_data=f"{prefix}:mst:{idx}")])
    if selected:
        rows.append([InlineKeyboardButton(
            f"🗑️ 删除所选（{len(selected)} 个）",
            callback_data=f"{prefix}:msdel")])
    rows.append([InlineKeyboardButton("✖️ 退出多选",
                                     callback_data=f"{prefix}:mscancel")])
    return InlineKeyboardMarkup(rows)


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

def fmt_boot_preset(preset, ok, reason, lang="zh"):
    """开机预设的配置摘要 + 配额预检结果。"""
    lines = [
        bot_i18n.t(lang, "boot.preset_title"),
        "",
        f"{bot_i18n.t(lang, 'boot.label_spec')}："
        f"{fmt_shape_spec(preset['shape'], preset['ocpus'], preset['memory_gb'], lang=lang)}",
        f"{bot_i18n.t(lang, 'boot.preset_name')}{preset['display_name']}",
    ]
    if preset.get("shape") == SHAPE_E2_MICRO:
        if preset.get("target_cpu"):
            lines.append(bot_i18n.t(lang, "boot.summary_target_cpu_only",
                                    models="/".join(get_allowed_cpu_models())))
        else:
            lines.append(bot_i18n.t(lang, "boot.summary_target_cpu_any"))
    lines.append("")
    lines.append(bot_i18n.t(lang, "boot.summary_quota_check",
                            result="✅ " if ok else "❌ ", reason=reason))
    if not ok:
        lines.append(bot_i18n.t(lang, "boot.summary_quota_short"))
    return "\n".join(lines)


@require_allowed
async def handle_boot(update, context):
    """菜单 1：M_Bot 风格按钮矩阵创建表单（见 tgbot/boot_form.py）。

    旧的预设选择键盘（boot_preset_keyboard / b:boot:）仍保留注册，
    仅供旧消息上的按钮回退使用；菜单 1 入口已改成表单。
    """
    context.user_data.pop("editing_preset_idx", None)
    context.user_data["boot_form"] = new_user_form()
    form = context.user_data["boot_form"]
    await _reply(update, context, fmt_form_text(form, lang=lang_of(update, context)),
                 reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))


async def _edit_boot_form(update, context, notice=None):
    """就地刷新开机表单（编辑当前消息，不刷屏）。

    notice 非空时用弹窗提示（例如延迟最多选两个）。编辑失败时静默
    （例如内容没变），下次点选时再刷新。
    """
    form = context.user_data.get("boot_form")
    query = update.callback_query
    if form is None or query is None:
        return
    try:
        await query.answer(text=notice or "", show_alert=bool(notice))
    except Exception:
        pass
    try:
        await menu_tracker.safe_edit(query, 
            fmt_form_text(form, lang=lang_of(update, context)), reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
    except Exception:
        pass


def uniquify_display_name(base_name, count, existing_names):
    """实例名去重：返回一个与已有实例名不冲突的基础名。

    实例名是日期格式（AMD_2026-01-01）后，同日同类型再开机会重名。
    count == 1 时检查基础名本身；count > 1 时检查 base-1..base-N（
    抢机线程会给批量加 -1、-2 后缀）。冲突时依次试 base-2、base-3…，
    直到整组名字都不冲突。existing_names 是已有实例显示名集合。
    纯函数，可直接单元测试。
    """
    existing = set(existing_names or [])
    candidate = base_name
    suffix = 1
    while True:
        if count <= 1:
            names = [candidate]
        else:
            names = [f"{candidate}-{i + 1}" for i in range(count)]
        if not any(name in existing for name in names):
            return candidate
        suffix += 1
        candidate = f"{base_name}-{suffix}"


async def _boot_form_confirm(update, context, form):
    """表单"确定开机"：校验 -> 配额预检 -> 配置摘要确认页。

    部署值按当前工作区域取（region_plans.json 有该区域配置时用该区域
    的可用域 / 子网 / 镜像，否则用主区默认值）。
    """
    deploy = get_deploy_defaults(region=get_current_region(context),
                                 profile=get_current_profile(context))
    errors = validate_form(form, deploy,
                           region=get_current_region(context),
                           profile=get_current_profile(context))
    if errors:
        await _reply(update, context, bot_i18n.tr(
            update, context, "boot.form_incomplete",
            errors="\n".join(f"• {error}" for error in errors)),
            reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    try:
        preset = build_preset(form, deploy)
    except ValueError as exc:
        await _reply(update, context, f"❌ {exc}", reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    # The root password is generated on the confirm page (UUID, see boot_form.generate_root_password): shown to the user there, then reused by boot_go_callback from the same copy (kept only in pending_boot, in memory; never written to presets.json). root 密码在确认页就生成（UUID，见 boot_form.generate_root_password）：确认页显示给用户看，点“开始抢机”后 boot_go_callback 复用同一份（只存在 pending_boot 里，内存；不进 presets.json）。
    if preset.get("root_login"):
        preset["root_password"] = generate_root_password()
    context.user_data["pending_boot"] = preset
    context.user_data["pending_boot_summary"] = "form"
    try:
        ok, reason = await run_sync(
            can_launch_batch, get_cli(context), get_compartment(context),
            preset["shape"], preset["ocpus"], preset["memory_gb"],
            preset.get("count", 1), preset.get("boot_volume_size_gbs"))
    except Exception as exc:  # noqa: BLE001 - 预检失败也要让用户看到
        ok, reason = False, fmt_error(exc, lang=lang_of(update, context))
    await _reply(update, context, fmt_boot_form_summary(preset, ok, reason, lang=lang_of(update, context)),
                 reply_markup=boot_confirm_keyboard(preset, lang=lang_of(update, context)))


async def _boot_form_save(update, context, form):
    """表单"保存为快捷开机"：校验通过后存进 presets.json，菜单 17 可用。

    保存时按当前工作区域取部署值（与确认开机一致）：以前这里不传
    区域，存的永远是主区部署值，在别的区用快捷开机就会跨区报错。
    从菜单 17 点"编辑"进来的表单（user_data 里有 editing_preset_idx）
    保存时原地更新那条预设（名字保持原样），不是追加一条新的。
    """
    deploy = get_deploy_defaults(region=get_current_region(context),
                                 profile=get_current_profile(context))
    errors = validate_form(form, deploy,
                           region=get_current_region(context),
                           profile=get_current_profile(context))
    if errors:
        await _reply(update, context, bot_i18n.tr(
            update, context, "boot.form_incomplete",
            errors="\n".join(f"• {error}" for error in errors)),
            reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    try:
        preset = build_preset(form, deploy)
    except ValueError as exc:
        await _reply(update, context, f"❌ {exc}", reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    shape = preset["shape"].replace("VM.Standard.", "")
    editing_idx = context.user_data.pop("editing_preset_idx", None)
    if editing_idx is not None:
        presets = load_presets()
        if 0 <= editing_idx < len(presets):
            # Editing changes the config, not the name: build_preset recomputes the name (by today's date), so write the original name back here. 编辑是改配置不是改名：build_preset 会重算名字（按今天日期），这里写回原名。
            preset["display_name"] = (
                presets[editing_idx].get("display_name")
                or preset["display_name"])
            update_preset(editing_idx, preset)
            await _reply(update, context,
                         f"✏️ 已更新快捷开机配置：{preset['display_name']}"
                         f"（{shape}，{preset['ocpus']} 核 / "
                         f"{preset['memory_gb']}GB）。\n"
                         "菜单 17（快捷开机）里可以一键用它开机。",
                         reply_markup=back_to_oracle_keyboard())
            return
        # The entry being edited was deleted elsewhere: fall back to appending a new save. 正在编辑的那条已被删掉（另一端删的）：退化为追加保存。
    save_preset(preset)
    await _reply(update, context,
                 f"💾 已保存为快捷开机：{preset['display_name']}（{shape}，"
                 f"{preset['ocpus']} 核 / {preset['memory_gb']}GB）。\n"
                 "菜单 17（快捷开机）里可以一键用它开机。",
                 reply_markup=back_to_oracle_keyboard())


@require_allowed
async def boot_form_callback(update, context):
    """回调 "bf:..."：开机表单矩阵点选 / 自定义输入 / 确认 / 保存。

    点选切换就地刷新表单消息；"自定义"与"上传脚本"设 pending_input
    等用户回复文字/文件；"确定开机"进摘要确认页；"保存为快捷开机"
    存进 presets.json。
    """
    data = _callback_data(update)
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "noop":
        if update.callback_query is not None:
            try:
                await update.callback_query.answer()
            except Exception:
                pass
        return
    if action == "cancel_input":
        # Cancel custom input: clear pending_input and re-post the form to continue. 取消自定义输入：清 pending_input，重发表单继续填。
        # (The old form was already deleted by the selection-box cleanup when the prompt went out, so it cannot just be edited to say "form unchanged"; re-posting matches what happens after input completes, and the prompt message is deleted by _reply's cleanup.) （提示发出时旧表单已被选择框清理删掉，不能再像以前那样只编辑提示说“表单未变”；重发表单与输入完成后的做法一致，提示消息由 _reply 的清理删掉。）
        context.user_data.pop("pending_input", None)
        form = context.user_data.get("boot_form")
        if form is None:
            await _reply(update, context,
                         "表单已过期，请重新打开菜单 1（开机）。",
                         reply_markup=back_to_oracle_keyboard())
            return
        await _reply(update, context, fmt_form_text(form, lang=lang_of(update, context)),
                     reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    if action == "oneclick":
        # One-click boot (default AMD): prefill the form and go straight to the confirm page (the old menu-1 preset one-click mode), without overwriting a form the user is already filling in. 一键开机（默认 AMD）：预填表单直接进确认页（原菜单 1 预设一键的模式），不覆盖用户正在填的表单。
        await _boot_form_confirm(update, context, default_amd_form())
        return
    form = context.user_data.get("boot_form")
    if form is None:
        await _reply(update, context, "表单已过期，请重新打开菜单 1（开机）。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if action == "confirm":
        await _boot_form_confirm(update, context, form)
        return
    if action == "save":
        await _boot_form_save(update, context, form)
        return
    if action == "script" and len(parts) >= 3 and parts[2] == "upload":
        context.user_data["pending_input"] = {"kind": "boot_script"}
        await _reply(update, context, _BOOT_CUSTOM_PROMPTS["boot_script"],
                     reply_markup=InlineKeyboardMarkup([[
                         InlineKeyboardButton("❌ 取消",
                                              callback_data="bf:cancel_input")]]))
        return
    if len(parts) >= 3 and parts[2] == "custom":
        kind = f"boot_{action}"
        prompt = _BOOT_CUSTOM_PROMPTS.get(kind)
        if prompt is None:
            await _reply(update, context, "未知的自定义项。")
            return
        context.user_data["pending_input"] = {"kind": kind}
        await _reply(update, context, prompt,
                     reply_markup=InlineKeyboardMarkup([[
                         InlineKeyboardButton("❌ 取消",
                                              callback_data="bf:cancel_input")]]))
        return
    if len(parts) < 3:
        await _reply(update, context, "无效的表单操作。")
        return
    changed, notice = toggle_option(form, action, parts[2])
    if changed or notice:
        await _edit_boot_form(update, context, notice=notice)
    elif update.callback_query is not None:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


@require_allowed
async def boot_preset_load_callback(update, context):
    """回调 "b:load:<idx>"：菜单 17 里一键用已保存的配置开机。

    读 presets.json 第 idx 条进 pending_boot，配额预检后显示
    配置摘要确认页（点"开始抢机"后走 boot_go_callback）。
    旧版保存的预设缺新字段时补默认值（兼容）。
    """
    data = _callback_data(update)
    try:
        idx = int(data.split(":")[-1])
    except ValueError:
        idx = -1
    presets = load_presets()
    if idx < 0 or idx >= len(presets):
        await _reply(update, context, "该配置已不存在，请重新打开菜单 17。",
                     reply_markup=back_to_oracle_keyboard())
        return
    preset = dict(presets[idx])
    preset.setdefault("count", 1)
    preset.setdefault("assign_public_ip", True)
    preset.setdefault("assign_ipv6", False)
    preset.setdefault("root_login", False)
    # A saved config may belong to another region (old saves carry no region); when the deploy values do not match the current working region, booting would fail with Oracle CannotParseRequest, so block it here. 已保存的配置可能是在别的区存的（旧版保存时不带区域），部署值与当前工作区域对不上时直接开机 Oracle 会报 CannotParseRequest，这里先拦住。
    mismatch = deploy_region_mismatch(
        {"availability_domain": preset.get("availability_domain")},
        get_current_region(context), get_current_profile(context))
    if mismatch is not None:
        await _reply(update, context, f"❌ {mismatch}",
                     reply_markup=back_to_oracle_keyboard())
        return
    if preset.get("root_login"):
        preset["root_password"] = generate_root_password()
    context.user_data["pending_boot"] = preset
    context.user_data["pending_boot_summary"] = "form"
    try:
        ok, reason = await run_sync(
            can_launch_batch, get_cli(context), get_compartment(context),
            preset["shape"], preset.get("ocpus", 0),
            preset.get("memory_gb", 0), preset.get("count", 1),
            preset.get("boot_volume_size_gbs"))
    except Exception as exc:  # noqa: BLE001 - 预检失败也要让用户看到
        ok, reason = False, fmt_error(exc, lang=lang_of(update, context))
    await _reply(update, context, fmt_boot_form_summary(preset, ok, reason, lang=lang_of(update, context)),
                 reply_markup=boot_confirm_keyboard(preset, lang=lang_of(update, context)))


@require_allowed
async def boot_preset_edit_callback(update, context):
    """回调 "b:edit:<idx>"：菜单 17 里编辑一条快捷开机配置。

    把预设反向填进开机表单（form_from_preset），记下
    editing_preset_idx 后显示表单；点"保存为快捷开机"时原地更新
    （见 _boot_form_save），点"确定开机"则按改后的值开机。
    """
    data = _callback_data(update)
    try:
        idx = int(data.split(":")[-1])
    except ValueError:
        idx = -1
    presets = load_presets()
    if idx < 0 or idx >= len(presets):
        await _reply(update, context, "该配置已不存在，请重新打开菜单 17。",
                     reply_markup=back_to_oracle_keyboard())
        return
    context.user_data["boot_form"] = form_from_preset(presets[idx])
    context.user_data["editing_preset_idx"] = idx
    form = context.user_data["boot_form"]
    await _reply(update, context,
                 f"✏️ 正在编辑快捷开机配置："
                 f"{presets[idx].get('display_name', '?')}\n"
                 "改好后点“保存为快捷开机”原地更新，或点“确定开机”"
                 "直接用改后的配置开机。\n\n" + fmt_form_text(form, lang=lang_of(update, context)),
                 reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))


@require_allowed
async def boot_preset_delete_callback(update, context):
    """回调 "b:del:<idx>"：菜单 17 里删除一条快捷开机配置（先确认）。"""
    data = _callback_data(update)
    try:
        idx = int(data.split(":")[-1])
    except ValueError:
        idx = -1
    presets = load_presets()
    if idx < 0 or idx >= len(presets):
        await _reply(update, context, "该配置已不存在，请重新打开菜单 17。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = presets[idx].get("display_name", "?")
    await _reply(update, context,
                 f"🗑️ 确定删除快捷开机配置「{name}」吗？\n"
                 "只删这条配置，不影响已开出的实例。",
                 reply_markup=InlineKeyboardMarkup([
                     [InlineKeyboardButton("🗑️ 确认删除",
                                           callback_data=f"b:delok:{idx}")],
                     [InlineKeyboardButton("❌ 取消",
                                           callback_data="o:presets")],
                 ]))


@require_allowed
async def boot_preset_delete_confirm_callback(update, context):
    """回调 "b:delok:<idx>"：确认删除后删掉配置并刷新菜单 17 列表。"""
    data = _callback_data(update)
    try:
        idx = int(data.split(":")[-1])
    except ValueError:
        idx = -1
    presets = load_presets()
    if idx < 0 or idx >= len(presets):
        await _reply(update, context, "该配置已不存在，请重新打开菜单 17。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = presets[idx].get("display_name", "?")
    delete_preset(idx)
    await handle_presets(update, context,
                         notice=f"🗑️ 已删除快捷开机配置「{name}」。")


async def _handle_boot_form_input(update, context, kind, text):
    """开机表单的自定义值输入（text_input_callback 转发来）。

    kind 是 boot_cpu / boot_os / boot_ocpus / boot_mem / boot_disk /
    boot_count / boot_delay / boot_script。校验通过后更新表单并
    重新发送表单消息；失败时提示并同样重发表单（可重新点"自定义"）。
    """
    form = context.user_data.get("boot_form")
    if form is None:
        await _reply(update, context, "表单已过期，请重新打开菜单 1（开机）。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if kind == "boot_script":
        if not text:
            await _reply(update, context, "脚本内容为空，已取消上传。",
                         reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
            return
        if len(text) > SCRIPT_MAX_CHARS:
            await _reply(update, context,
                         f"脚本太长（{len(text)} 字符，上限 "
                         f"{SCRIPT_MAX_CHARS}），已取消上传。",
                         reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
            return
        set_script(form, text, name="粘贴的脚本")
        await _reply(update, context, fmt_form_text(form, lang=lang_of(update, context)),
                     reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    row = kind[len("boot_"):]
    ok, error = set_custom(form, row, text)
    if not ok:
        await _reply(update, context, f"❌ {error}\n请重新点“自定义”再输入。",
                     reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    await _reply(update, context, fmt_form_text(form, lang=lang_of(update, context)),
                 reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))


@require_allowed
async def boot_form_document_callback(update, context):
    """开机表单的脚本文件上传（Document 消息）。

    只在 pending_input 是 boot_script 时处理，否则静默忽略
    （与 handlers_client.handle_client_document 共存，各自只认
    自己的状态）。文件超过上限或读取失败时取消上传。
    """
    pending = context.user_data.get("pending_input") or {}
    if pending.get("kind") != "boot_script":
        return
    message = update.effective_message
    document = message.document if message is not None else None
    if document is None:
        return
    form = context.user_data.get("boot_form")
    if form is None:
        context.user_data.pop("pending_input", None)
        await _reply(update, context, "表单已过期，请重新打开菜单 1（开机）。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if (document.file_size or 0) > SCRIPT_MAX_CHARS:
        context.user_data.pop("pending_input", None)
        await _reply(update, context,
                     f"文件太大（上限 {SCRIPT_MAX_CHARS // 1024}KB），"
                     "已取消上传。",
                     reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    try:
        tg_file = await document.get_file()
        data = await tg_file.download_as_bytearray()
        content = bytes(data).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 下载/解码失败都要提示
        context.user_data.pop("pending_input", None)
        await _reply(update, context, f"❌ 读取文件失败：{exc}",
                     reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))
        return
    context.user_data.pop("pending_input", None)
    set_script(form, content, name=document.file_name or "上传的脚本")
    await _reply(update, context, fmt_form_text(form, lang=lang_of(update, context)),
                 reply_markup=boot_form_keyboard(form, lang=lang_of(update, context)))


@require_allowed
async def boot_preset_callback(update, context):
    """回调 "b:boot:<preset>"：存预设 + 显示摘要与配额预检。"""
    data = _callback_data(update)
    preset_key = data.split(":")[-1] if ":" in data else ""
    preset = BOOT_PRESETS.get(preset_key)
    if preset is None:
        await _reply(update, context, f"未知的开机预设：{preset_key}")
        return
    context.user_data["pending_boot"] = dict(preset)
    context.user_data["pending_boot_summary"] = "preset"
    try:
        ok, reason = await run_sync(
            can_launch, get_cli(context), get_compartment(context),
            preset["shape"], preset["ocpus"], preset["memory_gb"])
    except Exception as exc:  # noqa: BLE001 - 预检失败也要让用户看到
        ok, reason = False, fmt_error(exc, lang=lang_of(update, context))
    text = fmt_boot_preset(preset, ok, reason)
    await _reply(update, context, text, reply_markup=boot_confirm_keyboard(preset, lang=lang_of(update, context)))


def _run_grabber_thread(task_id, cli, compartment_id, preset, notify=None,
                        resume_spec=None):
    """抢机线程体（daemon 线程里跑，不要在线程里直接调 Telegram API）。

    进度通过 on_event 回调写进任务 detail；结束时更新任务状态。
    成功后把本次配置追加保存到快捷开机（presets.json），保存前去掉
    user_data / root_password（user-data 里含 root 密码，绝不落盘）。
    count > 1 时逐台抢，实例名加 -1、-2… 后缀；中途配额不足或取消时
    停下并在详情里写已抢到的台数。
    新参数（表单/快捷开机的新字段）一律用 .get 兜底，旧版预设照常跑。
    注意：target_cpu 非空时按 tgbot/cpu_policy.py 的规则筛 CPU（只留
    配置里的型号，默认 7J13）：开机时把 guard 公钥拼进启动
    SSH keys，Grabber 注入 cpu_check_hook，抢到先 SSH 验 CPU，
    不符释放（永删引导卷）继续抢；guard 密钥未配置时筛不了，
    任务详情里写明并抢到即留。
    notify（可选）：{"token", "chat_id", "profile_label", "region_label",
    "root_password"}，成功后双通道推送开机成功通知（TG + Bark，
    Bark 未开启时只推 TG；root_password 为空时不带密码行）。
    通知失败不影响任务状态，只记日志（密码绝不进日志）。
    持久化：启动时把完整抢机规格写进共享任务记录（grab_spec），
    每抢到一台就更新已抢列表；服务重启后 resume_interrupted_grabs
    按它恢复抢机（resume_spec 传进来时就是恢复启动，已抢的不重抢）。
    规格写盘失败只记日志，不影响抢机本身。
    """
    cpu_unverified = False
    filter_warning = None

    def on_event(_event_type, message):
        nonlocal cpu_unverified
        if _event_type in ("cpu_check_failed", "cpu_filter_unavailable"):
            cpu_unverified = True
        TASKS.update(task_id, detail=message)

    # Cross-process cancel listener: when the web side cancels, the other side writes cancel_requested into a shared file; polling here sets stop_event and the grab loop stops. 跨进程取消监听：网页端点取消时，对方把 cancel_requested 写进共享文件，这里轮询到后置 stop_event，抢机循环随即停下。
    start_cancel_watcher(TASKS.tasks_file, task_id,
                         TASKS.stop_event(task_id))

    count = preset.get("count", 1) or 1
    base_name = preset["display_name"]
    if resume_spec is not None:
        spec = resume_spec
        successes = [task_store.grab_success_from_dict(s)
                     for s in spec.get("successes", [])]
    else:
        spec = None
        successes = []
        if TASKS.tasks_file:
            try:
                spec = task_store.build_grab_spec(
                    cli.profile, cli.region, compartment_id, preset,
                    notify)
                task_store.update_record(
                    TASKS.tasks_file, task_id, fields={"grab_spec": spec})
            except Exception as exc:  # noqa: BLE001 - 规格写盘失败不拖垮抢机
                logger.warning("抢机规格写盘失败（任务 %s）：%s",
                               task_id, exc)
                spec = None
    total_attempts = sum(r.attempts for r in successes)
    total_releases = sum(r.releases for r in successes)
    total_elapsed = sum(r.elapsed_seconds for r in successes)
    filtering = bool(preset.get("target_cpu"))
    try:
        with combined_boot_keys(preset.get("ssh_keys_file"),
                                include_guard=filtering) as boot_keys:
            if filtering and not boot_keys.guard_included:
                filter_warning = "未配置 guard 密钥，CPU 筛选未生效"
                on_event("cpu_filter_unavailable",
                         f"{filter_warning}，抢到即留。")
                target_cpus = None
                hook = None
            elif filtering:
                target_cpus = get_allowed_cpu_models()
                hook = make_cpu_check_hook(cli, run_remote_command)
            else:
                target_cpus = None
                hook = None
            for i in range(len(successes), count):
                display_name = base_name if count == 1 else f"{base_name}-{i + 1}"
                grabber = Grabber(
                    cli, compartment_id,
                    preset["availability_domain"], preset["shape"],
                    preset["image_id"], preset["subnet_id"],
                    display_name, boot_keys.path,
                    target_cpu=target_cpus,
                    on_event=on_event,
                    stop_event=TASKS.stop_event(task_id),
                    ocpus=preset.get("ocpus"),
                    memory_gb=preset.get("memory_gb"),
                    boot_volume_size_gbs=preset.get("boot_volume_size_gbs"),
                    assign_public_ip=preset.get("assign_public_ip", True),
                    assign_ipv6=preset.get("assign_ipv6", False),
                    user_data=preset.get("user_data"),
                    min_delay=preset.get("min_delay"),
                    max_delay=preset.get("max_delay"))
                if hook is not None:
                    grabber.cpu_check_hook = hook
                result = grabber.run()
                successes.append(result)
                total_attempts += result.attempts
                total_releases += result.releases
                total_elapsed += result.elapsed_seconds
                # Update the grabbed list in the spec on every grab: after a mid-run restart, the recovery thread computes the remaining count from it and never re-grabs what is already grabbed. 每抢到一台就更新规格里的已抢列表：中途重启时恢复线程按它算剩余台数，已抢的不重抢。
                if spec is not None and TASKS.tasks_file:
                    try:
                        spec["successes"] = [
                            task_store.grab_success_to_dict(r)
                            for r in successes]
                        task_store.update_record(
                            TASKS.tasks_file, task_id,
                            fields={"grab_spec": spec})
                    except Exception as exc:  # noqa: BLE001 - 写盘失败不拖垮抢机
                        logger.warning("抢机进度写盘失败（任务 %s）：%s",
                                       task_id, exc)
    except GrabCancelledError:
        TASKS.update(task_id, status="cancelled", detail="抢机已取消。")
        return
    except QuotaExceededError as exc:
        detail = f"配额不足，停止抢机：{exc}"
        if successes:
            detail += f"（已抢到 {len(successes)}/{count} 台）"
        TASKS.update(task_id, status="failed", detail=detail)
        return
    except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要落到任务状态
        TASKS.update(task_id, status="failed", detail=f"抢机失败：{exc}")
        return
    if count == 1:
        result = successes[0]
        detail = (f"抢到实例 …{result.instance_id[-4:]}，"
                  f"尝试 {result.attempts} 次，"
                  f"释放 {result.releases} 台 CPU 不符，"
                  f"总耗时 {fmt_duration(result.elapsed_seconds)}。")
    else:
        ids = "、".join(f"…{r.instance_id[-4:]}" for r in successes)
        detail = (f"抢到 {len(successes)} 台实例：{ids}，"
                  f"共尝试 {total_attempts} 次，"
                  f"释放 {total_releases} 台 CPU 不符，"
                  f"总耗时 {fmt_duration(total_elapsed)}。")
    if filter_warning:
        detail += f"（{filter_warning}，抢到即留）"
    if cpu_unverified:
        detail += "（CPU 未验明，请人工核对）"
    TASKS.update(task_id, status="done", detail=detail)
    saved = {key: value for key, value in preset.items()
             if key not in ("user_data", "root_password")}
    save_preset(saved)
    if notify:
        notify_with_flag = dict(notify)
        notify_with_flag["cpu_unverified"] = cpu_unverified
        _notify_boot_success(cli, preset, successes, notify_with_flag,
                             task_id=task_id)


def _start_dd_init(preset, instances, notify, task_id=None):
    """DD 重装开时，为每台有公网 IP 的实例起 DD 初始化后台任务。

    preset 须含 dd=True、root_password（DD 密码，确认页生成的 UUID）。
    startup_script（初始化脚本原文）可空：没脚本也跟踪 DD 完成并通知
    （2026-11-28 用户要求“立即改”——没脚本就不跟踪、不通知是个缺口）。
    DD 完成 / 初始化完成 / 失败时经 notify_dual 双通道推送。无公网 IP
    的实例起不了任务（SSH 没地址可连），记日志提示人工处理。
    """
    if not preset.get("dd"):
        return
    init_script = preset.get("startup_script")
    password = preset.get("root_password")
    if not password:
        logger.warning("DD 已开但缺 root 密码，未起 DD 初始化。")
        return
    token = notify.get("token")
    chat_id = notify.get("chat_id")
    for inst in instances:
        name = inst.get("name") or "?"
        public_ip = inst.get("public_ip")
        if not public_ip:
            logger.warning("实例 %s 暂无公网 IP，未起 DD 初始化。", name)
            continue

        def _on_event(event_type, message, _name=name, _ip=public_ip):
            # 只在关键节点双通道推送；等待过程不刷屏。
            if event_type == "dd_done":
                # dd_done 的 message 已含“有/无初始化脚本”的说明，直接发。
                notify_dual(token, chat_id, "🔄 DD 完成", message)
            elif event_type == "init_done":
                # init_done 的 message 已含验证结果（密码登录可用与否），
                # 直接发双通道通知（Telegram + Bark）。
                notify_dual(token, chat_id, "✅ 初始化完成", message)
            elif event_type in ("dd_timeout", "init_timeout", "init_failed"):
                notify_dual(token, chat_id, "❌ DD 初始化失败",
                            f"实例 {_name}（{_ip}）{message}")
            if task_id is not None:
                TASKS.append_log(task_id, message)

        start_dd_init(public_ip, password, init_script,
                      instance_name=name, port=DD_SSH_PORT,
                      on_event=_on_event)
        logger.info("已起 DD 初始化任务：实例 %s（%s）", name, public_ip)


def _notify_boot_success(cli, preset, successes, notify, task_id=None):
    """抢机成功后的双通道推送（线程内调用）。

    每台等公网 IP（刚开出来 VNIC 可能还没 IP，最长等 60 秒）；
    60 秒还没 IP 就先发通知写"获取中"，再起跟进线程后台继续等，
    IP 一出来补发一条 Telegram 消息（2026-01-01 用户实问：只等
    9 秒经常拿不到 IP，之后再也不补发）。
    通知函数本身不抛异常；这里再包一层，绝不让通知拖垮线程。
    无论成败都留一行结果：记 logger（进 journal）+ 写任务日志
    （task_id 传了才写），推送丢了有据可查。
    """
    try:
        instances = []
        count = preset.get("count", 1) or 1
        base_name = preset["display_name"]
        for i, result in enumerate(successes):
            name = base_name if count == 1 else f"{base_name}-{i + 1}"
            public_ip = wait_for_public_ip(cli, result.instance_id)
            if not public_ip:
                start_ip_followup(
                    lambda iid=result.instance_id: get_public_ip(cli, iid),
                    name, notify.get("token"), notify.get("chat_id"))
            instances.append({
                "name": name,
                "public_ip": public_ip,
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
            })
        shape = preset.get("shape") or ""
        ocpus = preset.get("ocpus")
        memory_gb = preset.get("memory_gb")
        if ocpus is None:
            ocpus = 1
        if memory_gb is None:
            memory_gb = 1 if shape == "VM.Standard.E2.1.Micro" else 6
        cpu_model = next(
            (getattr(r, "cpu_model", None) for r in successes
             if getattr(r, "cpu_model", None)), None)
        result = notify_boot_success(
            notify.get("token"), notify.get("chat_id"),
            notify.get("profile_label") or "?",
            notify.get("region_label") or "?",
            shape, ocpus, memory_gb,
            preset.get("boot_volume_size_gbs") or DEFAULT_BOOT_DISK_GB,
            instances,
            root_password=notify.get("root_password"),
            region_code=notify.get("region_code"),
            ssh_key_set=bool(preset.get("ssh_keys_file")),
            cpu_unverified=notify.get("cpu_unverified", False),
            cpu_model=cpu_model)
        if result.get("errors"):
            line = "开机成功通知部分失败：" + "; ".join(result["errors"])
            logger.warning("%s", line)
        else:
            line = fmt_notify_outcome(result)
            logger.info("%s", line)
        if task_id is not None:
            TASKS.append_log(task_id, line)
        # DD 重装开时：开机成功通知已发，接着为每台有公网 IP 的实例
        # 起 DD 初始化后台任务（等 DD 完成再经 SSH 跑初始化脚本）。
        _start_dd_init(preset, instances, notify, task_id=task_id)
        # Paid-shape instance: send an extra pinned reminder + Bark. Free shapes (A1.Flex / E2.1.Micro) do not get one. 付费规格实例：额外发一条置顶提醒 + Bark。免费形状（A1.Flex / E2.1.Micro）不发。
        if shape not in FREE_SHAPES:
            paid = notify_dual(
                notify.get("token"), notify.get("chat_id"),
                "⚠️ 付费实例提醒",
                fmt_paid_instance_alert(
                    notify.get("profile_label") or "?",
                    notify.get("region_label") or "?",
                    shape, instances),
                group="paid-instance", pin=True)
            if paid.get("errors"):
                logger.warning("付费实例提醒部分失败：%s",
                               "; ".join(paid["errors"]))
    except Exception:  # noqa: BLE001 - 通知绝不能拖垮抢机线程
        logger.exception("开机成功通知异常")


def _resume_notify(spec):
    """恢复用的 notify 字典：规格里存的字段 + 现读的 token。"""
    notify = dict(spec.get("notify") or {})
    notify["token"] = bot_config.TELEGRAM_BOT_TOKEN
    return notify


def _relaunch_grab(record, spec):
    """按规格重新拉起一个抢机线程（启动恢复用）。"""
    task_id = record["id"]
    params = spec["params"]
    preset = dict(params)
    try:
        cli = OCIClient(profile=spec.get("profile") or "DEFAULT",
                        region=spec.get("region") or "ap-singapore-1")
    except Exception as exc:  # noqa: BLE001 - 账号配不上时任务标失败，不炸启动
        logger.warning("恢复抢机 %s：建 OCI 客户端失败：%s", task_id, exc)
        if TASKS.attach(task_id):
            TASKS.update(task_id, status="failed",
                         detail=f"重启后恢复抢机失败：建 OCI 客户端失败：{exc}")
        return
    if not TASKS.attach(task_id):
        return
    TASKS.append_log(task_id, "🔄 重启后恢复抢机")
    remaining = (preset.get("count", 1) or 1) - len(spec.get("successes", []))
    logger.info("恢复抢机 %s：%s，剩余 %d 台",
                task_id, preset.get("display_name"), remaining)
    thread = threading.Thread(
        target=_run_grabber_thread,
        args=(task_id, cli, spec.get("compartment_id"), preset,
              _resume_notify(spec)),
        kwargs={"resume_spec": spec},
        daemon=True)
    thread.start()


def _finish_resumed_grab(record, spec, successes):
    """已抢满的恢复任务：标记完成、保存快捷开机、后台补发成功通知。

    状态还是 running 说明成功通知还没发出去（线程里的顺序是
    先标 done 再通知），所以这里要补发；补发在后台线程里做
    （等公网 IP 最长 60 秒，不能堵机器人启动）。
    """
    task_id = record["id"]
    params = spec["params"]
    preset = dict(params)
    count = preset.get("count", 1) or 1
    if count == 1:
        result = successes[0]
        detail = (f"抢到实例 …{result.instance_id[-4:]}，"
                  f"尝试 {result.attempts} 次，"
                  f"释放 {result.releases} 台 CPU 不符，"
                  f"总耗时 {fmt_duration(result.elapsed_seconds)}。")
    else:
        ids = "、".join(f"…{r.instance_id[-4:]}" for r in successes)
        detail = (f"抢到 {len(successes)} 台实例：{ids}，"
                  f"共尝试 {sum(r.attempts for r in successes)} 次，"
                  f"释放 {sum(r.releases for r in successes)} 台 CPU 不符，"
                  f"总耗时 {fmt_duration(sum(r.elapsed_seconds for r in successes))}。")
    detail += "（服务重启后恢复，抢机已完成）"
    if not TASKS.attach(task_id):
        return
    TASKS.update(task_id, status="done", detail=detail)
    saved = {key: value for key, value in preset.items()
             if key not in ("user_data", "root_password")}
    try:
        save_preset(saved)
    except Exception:  # noqa: BLE001 - 保存失败不影响完成状态
        pass

    def _notify_in_background():
        try:
            cli = OCIClient(profile=spec.get("profile") or "DEFAULT",
                            region=spec.get("region") or "ap-singapore-1")
        except Exception as exc:  # noqa: BLE001 - 客户端建不出就不补通知
            logger.warning("恢复抢机 %s：补发通知时建 OCI 客户端失败：%s",
                           task_id, exc)
            return
        _notify_boot_success(cli, preset, successes, _resume_notify(spec),
                             task_id=task_id)

    threading.Thread(target=_notify_in_background, daemon=True).start()


def resume_interrupted_grabs():
    """启动恢复（bot main() 调用，代替 TASKS.recover()）。

    机器人进程重启时：本来源还在跑的抢机任务按任务记录里的
    grab_spec 恢复重跑（剩余台数 = count - 已抢到），其余本来源
    未结束任务按老规矩标为已中断；网页端的任务绝不碰。
    已抢满台数的任务标记完成并补发成功通知；无规格或非本来源
    的抢机任务不恢复。
    """
    path = TASKS.tasks_file
    if not path:
        TASKS.recover()
        return
    records = task_store.load_records(path)
    resumable = []
    for record in records:
        if record.get("origin", "web") != TASKS.origin:
            continue
        if record.get("kind") != "grab":
            continue
        if record.get("status") not in ("pending", "running"):
            continue
        spec = record.get("grab_spec")
        if not isinstance(spec, dict) or not spec.get("params"):
            continue
        resumable.append((record, spec))
    relaunch = []
    for record, spec in resumable:
        params = spec["params"]
        count = params.get("count", 1) or 1
        successes = [task_store.grab_success_from_dict(s)
                     for s in spec.get("successes", [])]
        if len(successes) >= count:
            _finish_resumed_grab(record, spec, successes)
        else:
            relaunch.append((record, spec))
    task_store.recover_interrupted(
        path, TASKS.origin, keep_ids=[r["id"] for r, _ in relaunch])
    for record, spec in relaunch:
        _relaunch_grab(record, spec)


@require_allowed
async def boot_cpu_callback(update, context):
    """回调 "b:cpu:<7j13|any>"：确认页切换目标 CPU，刷新摘要。

    只改 pending_boot 里的 target_cpu（7J13 或 None），配额预检
    重跑一次（快，且保证摘要里的预检结果是新鲜的），摘要按
    pending_boot_summary 记的 kind（form/preset）重绘，就地编辑
    当前消息；编辑失败时（内容没变）静默。
    """
    data = _callback_data(update)
    choice = data.split(":")[-1] if ":" in data else ""
    preset = context.user_data.get("pending_boot")
    query = update.callback_query
    if preset is None:
        await _reply(update, context, bot_i18n.tr(
            update, context, "boot.config_expired"),
            reply_markup=back_to_oracle_keyboard(
                lang=lang_of(update, context)))
        return
    if choice == "7j13":
        preset["target_cpu"] = TARGET_CPU
    elif choice == "any":
        preset["target_cpu"] = None
    else:
        return
    try:
        ok, reason = await run_sync(
            can_launch_batch, get_cli(context), get_compartment(context),
            preset["shape"], preset.get("ocpus", 0),
            preset.get("memory_gb", 0), preset.get("count", 1),
            preset.get("boot_volume_size_gbs"))
    except Exception as exc:  # noqa: BLE001 - 预检失败也要让用户看到
        ok, reason = False, fmt_error(exc, lang=lang_of(update, context))
    if context.user_data.get("pending_boot_summary") == "preset":
        text = fmt_boot_preset(preset, ok, reason)
    else:
        text = fmt_boot_form_summary(preset, ok, reason, lang=lang_of(update, context))
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await menu_tracker.safe_edit(query, 
                text, reply_markup=boot_confirm_keyboard(preset, lang=lang_of(update, context)))
        except Exception:
            pass
        return
    await _reply(update, context, text, reply_markup=boot_confirm_keyboard(preset, lang=lang_of(update, context)))


@require_allowed
async def boot_go_callback(update, context):
    """回调 "b:go"：注册抢机任务，开 daemon 线程跑 Grabber.run。

    root 密码在确认页就已生成（UUID，存在 pending_boot 里，仅内存），
    这里优先复用确认页那份；旧路径（b:boot: 遗留预设）没有预生成
    密码时现场生成。密码经 user-data 设进实例；只在启动回复里
    显示一次，随后 pending_boot 即被清除，不进任务详情、不进日志、
    不进 presets.json（见 _run_grabber_thread 的保存脱敏）。密码同时
    传给抢机线程（仅内存），抢机成功后在成功通知（TG + Bark 双通道）
    里带上，通知完即丢。
    """
    preset = context.user_data.get("pending_boot")
    if not preset:
        await _reply(update, context, bot_i18n.tr(
            update, context, "boot.need_form"))
        return
    # Last line of defense: after the confirm page appears the user may have switched account/region, but pending_boot still holds the old region's deploy values; booting directly would fail with CannotParseRequest, so block it here. 最后一道防线：确认页出来后用户可能切了账号/区域，pending_boot 里还是旧区的部署值，直接开机会报 CannotParseRequest，这里拦住。
    mismatch = deploy_region_mismatch(
        {"availability_domain": preset.get("availability_domain")},
        get_current_region(context), get_current_profile(context))
    if mismatch is not None:
        await _reply(update, context, f"❌ {mismatch}",
                     reply_markup=back_to_oracle_keyboard())
        return
    root_password = None
    if preset.get("root_login"):
        # Prefer reusing the password generated on the confirm page (the user already saw that same one); generate one on the spot when the old path has none. 优先复用确认页生成的密码（用户已在确认页看到同一份），旧路径没有预生成时现场生成。
        root_password = preset.get("root_password") or generate_root_password()
    script = preset.get("startup_script")
    if root_password or script or preset.get("dd"):
        try:
            preset["user_data"] = build_user_data(
                root_password, script, dd=bool(preset.get("dd")))
        except ValueError as exc:
            await _reply(update, context, f"❌ {exc}")
            return
    # Instance name dedup: date-format names (AMD_YYYY-MM-DD) collide when booting twice on the same day, so check existing names first and append -2, -3 on conflict. A failed lookup does not block booting (the Grabber's 15-minute anti-duplicate window is the backstop). 实例名去重：日期格式（AMD_YYYY-MM-DD）同日再开会重名，先查已有实例名，冲突时加 -2、-3 后缀。查不到也不阻塞开机（Grabber 的 15 分钟防重复建窗口是兜底）。
    try:
        existing = await run_sync(list_instances, get_cli(context),
                                  get_compartment(context))
        names = {inst.get("display-name") for inst in existing or []}
        preset["display_name"] = uniquify_display_name(
            preset["display_name"], preset.get("count", 1) or 1, names)
    except Exception:  # noqa: BLE001 - 去重失败不阻塞开机
        pass
    # Task description: what machine is being grabbed (shape + region + instance name); menu 21 and the cancel button use it, and internal IDs/types like g1 / grab are no longer exposed. 任务说明：抢什么机器（规格 + 区域 + 实例名），菜单 21 与取消按钮都用它，不再暴露 g1 / grab 这类内部编号与类型。
    shape_short = preset["shape"].replace("VM.Standard.", "")
    region_label = bot_config.get_region_label(get_current_region(context))
    grab_label = (f"{shape_short}（{region_label}）· "
                  f"{preset['display_name']}")
    task_id = TASKS.register(
        "grab", detail=f"抢机准备中：{preset['display_name']}…",
        label=grab_label)
    # Success notice payload (passed to the thread in memory only: the password is never written to disk and is dropped right after the notice). 成功通知信息（只在内存里传给线程：密码不落盘，通知完即丢）。
    notify = {
        "token": bot_config.TELEGRAM_BOT_TOKEN,
        "chat_id": update.effective_chat.id if update.effective_chat else None,
        "profile_label": bot_config.profile_display_name(
            get_current_profile(context)),
        "region_label": region_label,
        "region_code": get_current_region(context),
        "root_password": root_password,
    }
    thread = threading.Thread(
        target=_run_grabber_thread,
        args=(task_id, get_cli(context), get_compartment(context), preset,
              notify),
        daemon=True)
    thread.start()
    # Clear pending_boot right after boot starts (it holds password-bearing user-data; do not keep it in memory). 启动后立即清除 pending_boot（里面含带密码的 user-data，不留内存）。
    context.user_data.pop("pending_boot", None)
    # When a root password exists, the whole message goes through Markdown parsing (password in a code span): underscores in the instance name (AMD_2026-01-02) would be read as italic markers and the whole send fails, so escape the dynamic parts first; without a password use plain text, no escaping. 有 root 密码时整条消息走 Markdown 解析（密码放代码段）：实例名里的下划线（AMD_2026-01-02）会被当成斜体标记、整条消息发送失败，先转义动态部分；无密码时走纯文本，不转义。
    label_text = (escape_markdown(grab_label, version=1)
                  if root_password else grab_label)
    text = bot_i18n.tr(update, context, "boot.grab_started",
                       label=label_text)
    parse_mode = None
    if root_password:
        # Password in a triple-backtick code block (Telegram shows a copy button; tapping it copies the bare password with no trailing space/newline — same as the /raninfo reset password). 密码放三重反引号代码块（Telegram 给复制按钮，点一下拷到的就是纯密码，不带尾空格/换行——和 /raninfo 重置密码一样）。
        # The password charset has no backticks, so it is safe inside a code block. 密码字符集不含反引号，可安全放进代码块。
        text += ("\n\n" + bot_i18n.tr(update, context,
                                      "boot.root_password_once") + "\n"
                 f"```\n{root_password}\n```\n"
                 + bot_i18n.tr(update, context,
                               "boot.root_password_after"))
        parse_mode = "Markdown"
    await _reply(update, context, text, parse_mode=parse_mode)


# ---- 20. 查询配额 ----

@require_allowed
async def handle_quota(update, context):
    """菜单 20：免费额度汇总——先出账号列表，每个账号点开看详情。

    先应答再查 OCI：额度查询要 1.3 秒以上，不先 answer 的话按钮
    转圈要转满整个查询（原来 query.answer() 只在最后的 _reply
    里才发生）。
    """
    query = update.callback_query
    if query is not None:
        await query.answer()
    counts = await run_sync(get_profile_region_counts)
    labels = {name: bot_config.profile_display_name(name, counts.get(name))
              for name in bot_config.PROFILES}
    await _reply(update, context, bot_i18n.tr(update, context,
                                              "quota.choose_account"),
                 reply_markup=quota_accounts_keyboard(
                     labels, lang=lang_of(update, context)))


@require_allowed
async def quota_callback(update, context):
    """回调 q:list / q:acct:<profile> / q:region:<profile>:<region>。

    额度页三级导航：账号列表 ->（多区账号）区域二级菜单 -> 额度详情。
    单区账号点开直接显示该区额度，不出二级菜单。

    先应答再查 OCI（同 handle_quota）：点开账号/区域要查额度，
    不先 answer 转圈转满整个查询。
    """
    query = update.callback_query
    if query is not None:
        await query.answer()
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "list":
        await handle_quota(update, context)
        return
    if action == "acct" and len(parts) == 3:
        await _quota_account(update, context, parts[2])
        return
    if action == "region" and len(parts) == 4:
        await _quota_region(update, context, parts[2], parts[3])
        return
    await _reply(update, context, bot_i18n.tr(update, context,
                                              "quota.invalid_op"),
                 reply_markup=back_to_oracle_keyboard(
                     lang=lang_of(update, context)))


async def _quota_account(update, context, profile):
    """点开账号：多区订阅出二级区域菜单；单区直接显示该区额度。"""
    if profile not in bot_config.PROFILES:
        await _reply(update, context, bot_i18n.tr(update, context,
                                                  "quota.no_account"),
                     reply_markup=back_to_oracle_keyboard(
                         lang=lang_of(update, context)))
        return
    regions = (await run_sync(get_profile_regions)).get(profile)
    if not regions:
        # Subscription list unreachable (API failure): show the quota by home region directly. 查不到订阅列表（API 失败）：直接按主区显示额度。
        cfg = bot_config.get_profile_config(profile)
        await _show_quota(update, context, profile, cfg["region"],
                          multi=False)
        return
    if len(regions) == 1:
        code = regions[0].get("region-name") or regions[0].get("region-key")
        await _show_quota(update, context, profile, code, multi=False)
        return
    label = bot_config.profile_display_name(profile, len(regions))
    await _reply(update, context, bot_i18n.tr(update, context,
                                              "quota.choose_region",
                                              label=label),
                 reply_markup=quota_regions_keyboard(
                     profile, regions, lang=lang_of(update, context)))


async def _quota_region(update, context, profile, region):
    """点开区域：显示该账号在该区域的免费额度。"""
    if profile not in bot_config.PROFILES:
        await _reply(update, context, bot_i18n.tr(update, context,
                                                  "quota.no_account"),
                     reply_markup=back_to_oracle_keyboard(
                         lang=lang_of(update, context)))
        return
    await _show_quota(update, context, profile, region, multi=True)


async def _show_quota(update, context, profile, region, multi):
    """查并显示某账号在某区域的免费额度（multi 时返回按钮回区域菜单）。"""
    lang = lang_of(update, context)
    try:
        cfg = bot_config.get_profile_config(profile)
        cli = bot_config.get_oci_client(profile=profile, region=region)
        summary = await run_sync(get_free_tier_summary, cli,
                                 cfg["compartment_id"])
    except Exception as exc:  # noqa: BLE001 - 任何 OCI 异常都转成提示
        await _reply(update, context, fmt_error(exc, lang=lang))
        return
    label = bot_config.get_region_label(cfg["region"])
    if region != cfg["region"]:
        label = f"{label} · {bot_config.get_region_label(region)}"
    rows = [[InlineKeyboardButton(
        bot_i18n.t(lang, "quota.back_regions" if multi
                   else "quota.back_accounts"),
        callback_data=(f"q:acct:{profile}" if multi else "q:list"))],
        [InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                              callback_data="o:menu")]]
    await _reply(update, context, fmt_quota_summary(summary, label, lang=lang),
                 reply_markup=InlineKeyboardMarkup(rows))


# ---- 19. 近 3 月流量查询 / 25. 近 3 月花费查询 ----

def fmt_instance_traffic(entries, lang="zh"):
    """本月流量全文（vnstat 自然月统计，格式照用户自己的脚本）。

    entries：每台 {"name", "public_ip", "city", "rx_gb", "tx_gb",
    "error"}；rx_gb/tx_gb 为 None 表示查不到（error 说明原因）。
    无运行中实例时返回提示。
    """
    if not entries:
        return bot_i18n.t(lang, "traffic.empty")
    lines = [bot_i18n.t(lang, "traffic.title_line"), ""]
    total_rx = 0.0
    total_tx = 0.0
    counted = 0
    for entry in entries:
        ip = entry.get("public_ip") or bot_i18n.t(lang, "instances.no_public_ip")
        city = entry.get("city") or bot_i18n.t(lang, "traffic.unknown_city")
        lines.append(f"IP: {ip}（{city}）")
        if entry.get("rx_gb") is not None and entry.get("tx_gb") is not None:
            lines.append(bot_i18n.t(lang, "traffic.in_out",
                                    rx=entry['rx_gb'], tx=entry['tx_gb']))
            total_rx += entry["rx_gb"]
            total_tx += entry["tx_gb"]
            counted += 1
        else:
            lines.append(bot_i18n.t(lang, "traffic.no_data", reason=(
                entry.get("error") or bot_i18n.t(lang, "traffic.unknown_reason"))))
        if entry.get("guard") is not None:
            lines.append(guard_status_label(entry["guard"]))
        if entry.get("installed"):
            # A freshly auto-installed vnstat only counts from the moment its service started. 刚自动装的 vnstat：数据从服务启动那一刻开始统计。
            lines.append(bot_i18n.t(lang, "traffic.vnstat_fresh"))
        lines.append("")
    if counted:
        lines.append(bot_i18n.t(lang, "traffic.total", rx=total_rx,
                                tx=total_tx, count=counted))
    return "\n".join(lines).rstrip("\n")


def _vnstat_not_installed(exit_code, stderr):
    """vnstat 查询失败是否因为没装 vnstat。

    精确信号：stderr 里同时出现 vnstat 与 not found（bash：
    “vnstat: command not found”；dash：“sh: 1: vnstat: not found”）。
    只认这个信号，其他失败（如网卡识别失败）不触发自动安装。
    """
    text = (stderr or "").lower()
    return "vnstat" in text and "not found" in text


async def _fetch_instance_traffic(context, instances=None):
    """查运行中实例的本月流量（vnstat，SSH 到实例上查）。

    返回 fmt_instance_traffic 用的 entries 列表。instances 不传时现查
    （并补公网 IP）；城市名经 ip-api.com 查（本机直接查，不走 SSH）。
    单台失败不影响其他台（该台 error 说明原因）。
    某台没装 vnstat 时自动安装依赖（vnstat / python3 / curl）并启用
    vnstat 服务，再重试一次查询（用户要求：缺了就默认安装）。
    """
    if instances is None:
        instances = await run_sync(
            list_instances, get_cli(context), get_compartment(context))
        instances = await run_sync(
            attach_public_ips, get_cli(context), instances)
    entries = []
    for inst in instances or []:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        public_ip = inst.get("public-ip")
        entry = {
            "name": inst.get("display-name"),
            "public_ip": public_ip,
            "city": await run_sync(fetch_city_for_ip, public_ip),
            "rx_gb": None,
            "tx_gb": None,
            "error": None,
        }
        host = _guard_host_for_instance(public_ip)
        if host is None:
            entry["error"] = ("未配置 SSH 密钥" if not bot_config.GUARD_SSH_KEY
                              else "无公网 IP")
            entries.append(entry)
            continue
        try:
            exit_code, out, err = await run_sync(
                run_remote_command, host, guard_vnstat_command())
        except Exception:  # noqa: BLE001 - 单台失败不影响其他台
            entry["error"] = "SSH 连不上该实例"
            entries.append(entry)
            continue
        if exit_code != 0 and _vnstat_not_installed(exit_code, err):
            try:
                install_code, _o, install_err = await run_sync(
                    run_remote_command, host,
                    guard_ensure_vnstat_command(),
                    GUARD_ENSURE_INSTALL_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001 - 安装超时也算安装失败
                logger.warning("自动安装 vnstat 失败（%s）：%s", public_ip, exc)
                entry["error"] = "vnstat 未安装，自动安装失败"
                entries.append(entry)
                continue
            if install_code != 0:
                logger.warning("自动安装 vnstat 失败（%s）：%s",
                               public_ip, (install_err or "").strip()[-200:])
                entry["error"] = "vnstat 未安装，自动安装失败"
                entries.append(entry)
                continue
            entry["installed"] = True
            try:
                exit_code, out, err = await run_sync(
                    run_remote_command, host, guard_vnstat_command())
            except Exception:  # noqa: BLE001 - 单台失败不影响其他台
                entry["error"] = "SSH 连不上该实例"
                entries.append(entry)
                continue
        if exit_code != 0:
            entry["error"] = "vnstat 查询失败"
            entries.append(entry)
            continue
        try:
            _code, probe_out, _err = await run_sync(
                run_remote_command, host, probe_command())
            entry["guard"] = parse_probe_output(probe_out)
        except Exception:  # noqa: BLE001 - 看门狗状态查不到不影响流量展示
            entry["guard"] = None
        parsed = parse_vnstat_monthly_gb(out)
        if parsed is None:
            entry["error"] = "vnstat 无本月数据"
        else:
            entry["rx_gb"], entry["tx_gb"] = parsed
        entries.append(entry)
    return entries


@require_allowed
async def handle_traffic(update, context):
    """菜单 19：本月流量——vnstat 自然月统计（SSH 到实例上查）。

    原先查 OCI 用量接口的出站流量恒为 0.00 GB（接口不给真实出站数），
    改用用户自己的 vnstat 口径：每台 IP + 中文城市 + 当月入站/出站。
    每台下面带看门狗状态行与开关按钮（v1.21.0：点开启安装用户自己的
    流量看门狗脚本，点关闭摘 cron 并解除熔断/限速）。
    """
    try:
        entries = await _fetch_instance_traffic(context)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(
            exc, lang=lang_of(update, context)))
        return
    context.user_data["traffic_entries"] = entries
    await _reply(update, context, fmt_instance_traffic(
        entries, lang=lang_of(update, context)),
        reply_markup=traffic_keyboard(entries,
                                      lang=lang_of(update, context)))


def traffic_keyboard(entries, lang="zh"):
    """菜单 19 键盘：每台一个看门狗开关按钮 + 返回按钮。

    有公网 IP 且配了 SSH 密钥的才给按钮（没法 SSH 的装不了）；
    按钮文字按当前状态反着显示动作（已开启显示“关闭”，否则“开启”）。
    """
    rows = []
    for idx, entry in enumerate(entries or []):
        if not entry.get("public_ip") or not bot_config.GUARD_SSH_KEY:
            continue
        guard = entry.get("guard") or {}
        name = entry.get("name") or entry.get("public_ip") or f"实例 {idx + 1}"
        action = bot_i18n.t(lang, "traffic.guard_off" if guard.get("enabled")
                            else "traffic.guard_on")
        rows.append([InlineKeyboardButton(
            bot_i18n.t(lang, "traffic.guard_toggle", name=name,
                       action=action),
            callback_data=f"wd:toggle:{idx}")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    return InlineKeyboardMarkup(rows)


@require_allowed
async def watchdog_callback(update, context):
    """菜单 19 看门狗开关（wd:toggle:<idx>）。

    已开启 → 关闭（摘 cron + 解除熔断/限速，脚本保留）；
    未开启 → 安装并开启（传脚本 + 写 Bark 配置 + 挂 cron + 测试推送）。
    完成后刷新整条流量消息（重新查流量与状态）。
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[1] != "toggle":
        return
    try:
        idx = int(parts[2])
    except ValueError:
        return
    entries = context.user_data.get("traffic_entries") or []
    if idx < 0 or idx >= len(entries):
        await menu_tracker.safe_edit(query, 
            "列表已过期，请重新打开菜单 19（本月流量）。",
            reply_markup=back_to_oracle_keyboard())
        return
    entry = entries[idx]
    host = _guard_host_for_instance(entry.get("public_ip"))
    if host is None:
        await query.answer("这台没有公网 IP 或未配置 SSH 密钥。",
                           show_alert=True)
        return
    name = entry.get("name") or entry.get("public_ip")
    guard = entry.get("guard") or {}
    try:
        if guard.get("enabled"):
            await menu_tracker.safe_edit(query, f"正在关闭 {name} 的看门狗…")
            result = await run_sync(
                disable_watchdog, host, run_remote_command)
            done_text = ("已经是关闭状态。" if result.get("action") == "already_off"
                         else "已关闭（cron 已摘，熔断/限速已解除）。")
        else:
            await menu_tracker.safe_edit(query, f"正在给 {name} 安装看门狗…")
            result = await run_sync(
                install_watchdog, host, load_bark_config(),
                run_remote_command, put_remote_file,
                guard_ensure_vnstat_command())
            if result.get("action") == "already":
                done_text = "已经是开启状态。"
            elif result.get("action") == "upgraded":
                done_text = "已升级到自动识别端口版并开启。"
            elif result.get("action") == "enabled":
                done_text = "已重新开启。"
            else:
                done_text = "已安装并开启。"
            if not result.get("test_ok"):
                done_text += "（测试推送没发出去，请检查 Bark 配置。）"
    except WatchdogError as exc:
        await menu_tracker.safe_edit(query, 
            f"❌ {name} 看门狗操作失败：{exc}",
            reply_markup=traffic_keyboard(entries))
        return
    except Exception as exc:  # noqa: BLE001 - SSH 失败等统一展示
        await menu_tracker.safe_edit(query, 
            f"❌ {name} 看门狗操作失败：{fmt_error(exc)}",
            reply_markup=traffic_keyboard(entries))
        return
    try:
        fresh = await _fetch_instance_traffic(context)
    except Exception as exc:  # noqa: BLE001 - 刷新失败时只报操作结果
        await menu_tracker.safe_edit(query, 
            f"{name} 看门狗{done_text}\n（列表刷新失败：{fmt_error(exc)}）",
            reply_markup=traffic_keyboard(entries))
        return
    context.user_data["traffic_entries"] = fresh
    await menu_tracker.safe_edit(query, 
        f"{name} 看门狗{done_text}\n\n{fmt_instance_traffic(fresh)}",
        reply_markup=traffic_keyboard(fresh))


@require_allowed
async def handle_cost(update, context):
    """菜单 25：全部账号近 3 个月总费用（默认一次查全部账号）。

    与订阅信息 / 一键测活同款：遍历全部 profile，单账号失败不影响
    其他账号，最后附合计。
    """
    counts = await run_sync(get_profile_region_counts)
    entries = []
    for name in bot_config.PROFILES:
        label = bot_config.profile_display_name(name, counts.get(name))
        try:
            cfg = bot_config.get_profile_config(name)
            cli = bot_config.get_oci_client(profile=name)
            items = await run_sync(fetch_usage_summary, cli,
                                   cfg["compartment_id"], "MONTHLY")
            entries.append((label, items))
        except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
            entries.append((label, None))
    await _reply(update, context, fmt_usage_totals(
        entries, lang=lang_of(update, context)),
        reply_markup=back_to_oracle_keyboard(
            lang=lang_of(update, context)))


# ---- 18. 订阅信息 ----

@require_allowed
async def handle_subscription(update, context):
    """菜单 18：全部账号的租户名称 / Home 区域 / 描述（有几个账号显示几个）。"""
    counts = await run_sync(get_profile_region_counts)
    entries = []
    for name in bot_config.PROFILES:
        label = bot_config.profile_display_name(name, counts.get(name))
        try:
            cfg = bot_config.get_profile_config(name)
            cli = bot_config.get_oci_client(profile=name)
            tenancy = await run_sync(get_tenancy, cli,
                                     cfg["compartment_id"])
            entries.append((label, tenancy))
        except Exception:  # noqa: BLE001 - 单账号失败不影响其他账号
            entries.append((label, None))
    await _reply(update, context, fmt_tenancies(
        entries, lang=lang_of(update, context)),
        reply_markup=back_to_oracle_keyboard(
            lang=lang_of(update, context)))


# ---- 15. 一键测活 ----

@require_allowed
async def handle_health(update, context):
    """菜单 15：遍历全部 profile，各查实例数与运行状态，异常单列。

    账号用主区中文名显示（多区账号后缀区数，与订阅信息页一致；
    v1.15.0 修：原来直接显示 DEFAULT / ACCOUNT2 等配置键名）。
    """
    counts = await run_sync(get_profile_region_counts)
    lang = lang_of(update, context)
    lines = [bot_i18n.t(lang, "health.title_line"), ""]
    for name in bot_config.PROFILES:
        label = bot_config.profile_display_name(name, counts.get(name))
        try:
            cfg = bot_config.get_profile_config(name)
            cli = bot_config.get_oci_client(profile=name)
            instances = await run_sync(list_instances, cli,
                                       cfg["compartment_id"])
            running = sum(1 for i in instances
                          if i.get("lifecycle-state") == "RUNNING")
            lines.append(bot_i18n.t(lang, "health.line", label=label,
                                    total=len(instances), running=running))
        except Exception as exc:  # noqa: BLE001 - 单账号报错不影响其他账号
            lines.append(f"• {label}：{fmt_error(exc, lang=lang)}")
    await _reply(update, context, "\n".join(lines),
                 reply_markup=back_to_oracle_keyboard(lang=lang))


# ---- 21. 查看运行中的任务 ----

@require_allowed
async def handle_tasks(update, context):
    """菜单 21：只列运行中的任务 + 取消按钮。

    标题写的是“运行中的任务”，所以只取 TASKS.running()，
    不再用 list_all() 把已结束（done/failed/cancelled）的
    历史任务也列出来（之前列表又长又乱就是这个原因）。
    """
    tasks = TASKS.running()
    lang = lang_of(update, context)
    await _reply(update, context, fmt_task_list(tasks, lang=lang),
                 reply_markup=task_list_keyboard(tasks, lang=lang))


@require_allowed
async def task_cancel_callback(update, context):
    """回调 "t:cancel:<id>"：取消任务。"""
    data = _callback_data(update)
    task_id = data.split(":")[-1] if ":" in data else ""
    task = TASKS.get(task_id)
    label = (task or {}).get("label") or task_id
    if TASKS.cancel(task_id):
        await _reply(update, context, bot_i18n.tr(
            update, context, "tasks.cancelled", label=label))
    else:
        await _reply(update, context, bot_i18n.tr(
            update, context, "tasks.cancel_failed", label=label))


# ---- profile 管理（账号切换，/oracle 菜单顶行按钮） ----

@require_allowed
async def handle_profiles(update, context):
    """账号切换键盘（列表按钮用“、N 个区”格式，单区账号不加后缀）。"""
    current = get_current_profile(context)
    counts = await run_sync(get_profile_region_counts)
    labels = {name: bot_config.profile_display_name_list(
        name, counts.get(name)) for name in bot_config.PROFILES}
    current_region = get_current_region(context)
    home_region = bot_config.get_region(current)
    current_label = bot_config.profile_display_name(
        current, counts.get(current))
    if current_region != home_region:
        current_label += f" · {bot_config.get_region_label(current_region)}"
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        await _delete_tracked_switch_messages(context, chat_id)
    text = bot_i18n.tr(update, context, "profiles.current",
                       label=current_label)
    keyboard = profile_keyboard(
        list(bot_config.PROFILES), current, labels,
        lang=lang_of(update, context))
    # Edit the same message instead of popping a new one; when there is no callback message (triggered by command) or the edit fails, fall back to sending a new one. 在原来那条消息里改，不弹新框；没有回调消息（命令触发）或编辑失败时退回新发一条。
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
        try:
            await menu_tracker.safe_edit(query, text, reply_markup=keyboard)
            return
        except Exception as exc:  # noqa: BLE001 - 编辑失败就新发一条
            logger.debug("切换账号列表就地编辑失败，改发新消息：%s", exc)
    msg = await _reply(update, context, text, reply_markup=keyboard)
    _track_switch_message(context, msg)


@require_allowed
async def profile_callback(update, context):
    """回调 "p:<name>"：切换当前 profile。

    多区账号（订阅区域 > 1 个）先出二级区域菜单，点区域才切换工作
    区域；单区账号直接切换，切换成功后删掉流程中间消息并回到
    /oracle 主菜单（不再单独发一条确认消息）。
    """
    data = _callback_data(update)
    if _is_duplicate_switch(context, data):
        query = update.callback_query
        if query is not None:
            try:
                await query.answer()
            except Exception:
                pass
        return
    name = data.split(":", 1)[1] if ":" in data else ""
    try:
        bot_config.get_profile_config(name)
    except ValueError as exc:
        await _reply(update, context, bot_i18n.tr(
            update, context, "profiles.switch_failed", detail=exc))
        return
    regions = await run_sync(get_profile_regions)
    subs = regions.get(name) or []
    if len(subs) > 1:
        current_region = get_current_region(context)
        current = (current_region if get_current_profile(context) == name
                   else None)
        label = bot_config.profile_display_name(name, len(subs))
        text = bot_i18n.tr(update, context, "profiles.choose_region2",
                           label=label)
        keyboard = profile_regions_keyboard(
            name, subs, current, lang=lang_of(update, context))
        query = update.callback_query
        if query is not None:
            try:
                await menu_tracker.safe_edit(query, text, reply_markup=keyboard)
                return
            except Exception as exc:  # noqa: BLE001 - 编辑失败就新发一条
                logger.debug("区域列表就地编辑失败，改发新消息：%s", exc)
        msg = await _reply(update, context, text, reply_markup=keyboard)
        _track_switch_message(context, msg)
        return
    try:
        set_current_profile(context, name)
    except ValueError as exc:
        await _reply(update, context, bot_i18n.tr(
            update, context, "profiles.switch_failed", detail=exc))
        return
    label = get_cached_profile_display_name(name)
    await _show_oracle_menu_after_switch(
        update, context, bot_i18n.tr(update, context, "profiles.switched",
                                     account=label))


@require_allowed
async def profile_region_callback(update, context):
    """回调 "pr:<profile>:<region>"：切换账号 + 工作区域。

    区域必须在该账号的订阅列表里（否则拒绝）；先切账号（会重置区域
    为主区），再切区域。切换成功后删掉流程中间消息并回到 /oracle
    主菜单（不再单独发一条确认消息）。
    """
    data = _callback_data(update)
    if _is_duplicate_switch(context, data):
        query = update.callback_query
        if query is not None:
            try:
                await query.answer()
            except Exception:
                pass
        return
    parts = data.split(":")
    if len(parts) != 3:
        await _reply(update, context, bot_i18n.tr(
            update, context, "profiles.invalid_region"))
        return
    name, region = parts[1], parts[2]
    regions = await run_sync(get_profile_regions)
    subs = regions.get(name) or []
    region_ids = {sub.get("region-name") for sub in subs}
    if subs and region not in region_ids:
        await _reply(update, context, bot_i18n.tr(
            update, context, "profiles.region_not_subscribed",
            region=region))
        return
    try:
        set_current_profile(context, name)
        set_current_region(context, region)
    except ValueError as exc:
        await _reply(update, context, bot_i18n.tr(
            update, context, "profiles.switch_failed", detail=exc))
        return
    label = get_cached_profile_display_name(name)
    region_label = bot_config.get_region_label(region)
    home = bot_config.get_region(name)
    if region == home:
        confirm = bot_i18n.tr(update, context, "profiles.switched",
                              account=label)
    else:
        confirm = bot_i18n.tr(update, context, "profiles.region_switched",
                              account=label, region=region_label)
    await _show_oracle_menu_after_switch(update, context, confirm)


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
        await _reply(update, context, bot_i18n.tr(
            update, context, "changeip.pick_first"),
            reply_markup=back_to_oracle_keyboard(
                lang=lang_of(update, context)))
        return
    name = inst.get("display-name") or "?"
    try:
        current_ip = await run_sync(get_public_ip, get_cli(context),
                                    inst["id"])
    except Exception:  # noqa: BLE001 - 查不到当前 IP 也不阻塞换 IP
        current_ip = None
    text = bot_i18n.tr(update, context, "changeip.confirm_detail",
                       name=name,
                       ip=current_ip or bot_i18n.tr(
                           update, context, "instances.no_public_ip"))
    await _reply(update, context, text,
                 reply_markup=confirm_keyboard(
                     "changeip", idx,
                     bot_i18n.tr(update, context,
                                 "changeip.confirm_button"),
                     lang=lang_of(update, context)))


@require_allowed
async def handle_ip(update, context):
    """菜单 2：保留 IP 列表 + 各实例公网 IP，并挂创建/删除按钮。

    换 IP 流程：实例详情里点“换 IP”→ handle_changeip_prompt 弹确认
    → handle_confirm("changeip") 执行。本总览里不挂换 IP 按钮。
    可删除的保留 IP（AVAILABLE 且未绑定）存
    user_data["ip_reserved"]（{id, ip}），回调按下标取回；
    已绑定的不挂删除按钮。
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
        await _reply(update, context, fmt_error(
            exc, lang=lang_of(update, context)))
        return
    deletable = [{"id": ip.get("id"), "ip": ip.get("ip-address", "?")}
                 for ip in reserved
                 if ip.get("lifecycle-state") == "AVAILABLE"
                 and not ip.get("assigned-entity")]
    context.user_data["ip_reserved"] = deletable
    lang = lang_of(update, context)
    rows = [[InlineKeyboardButton(bot_i18n.t(lang, "ip.create_button"),
                                  callback_data="ip_create")]]
    for idx, entry in enumerate(deletable):
        rows.append([InlineKeyboardButton(
            bot_i18n.t(lang, "ip.delete_button", ip=entry['ip']),
            callback_data=f"ip_delete:{idx}")])
    rows.append([InlineKeyboardButton(bot_i18n.t(lang, "common.back_oracle"),
                                      callback_data="o:menu")])
    await _reply(update, context, fmt_ip_overview(reserved, instance_ips,
                                                 lang=lang),
                 reply_markup=InlineKeyboardMarkup(rows))


@require_allowed
async def ip_create_callback(update, context):
    """ip_create：直接创建一个保留公网 IP 并回显新地址。"""
    try:
        _ip_id, new_ip = await run_sync(
            create_reserved_ip, get_cli(context),
            get_compartment(context), RESERVED_IP_DISPLAY_NAME)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc),
                     reply_markup=back_to_oracle_keyboard())
        return
    await _reply(update, context,
                 f"✅ 已创建保留 IP：{new_ip}\n"
                 "⚠️ 保留 IP 是计费资源，不用了请及时删除。",
                 reply_markup=back_to_oracle_keyboard())


@require_allowed
async def ip_delete_callback(update, context):
    """ip_delete:<idx>：删除保留 IP 先弹二次确认
    （确认按钮 c:delpubip:<idx>，由 handle_confirm 执行）。"""
    parts = _callback_data(update).split(":")
    try:
        idx = int(parts[1]) if len(parts) >= 2 else -1
    except ValueError:
        idx = -1
    entries = context.user_data.get("ip_reserved") or []
    if idx < 0 or idx >= len(entries):
        await _reply(update, context, "目标已过期，请重新打开 IP 管理。",
                     reply_markup=back_to_oracle_keyboard())
        return
    entry = entries[idx]
    await _reply(update, context,
                 f"🗑️ 确认删除保留 IP {entry['ip']}？\n\n"
                 "删除后该 IP 立即释放，无法找回。",
                 reply_markup=confirm_keyboard("delpubip", idx,
                                               "🗑️ 确认删除"))


# ---- 4. ipv6 管理 ----

@require_allowed
async def handle_ipv6(update, context):
    """菜单 4：按实例查 IPv6 列表（主 VNIC -> ipv6 list），并挂
    创建/删除按钮。

    无 IPv6 的实例显示"➕ 创建 IPv6"（直接执行并回显新地址）；
    已有 IPv6 的每条地址显示"🗑 删除"（先二次确认）。
    目标列表存 user_data["ipv6_vnics"]（创建用：{name, vnic_id}）
    与 user_data["ipv6_entries"]（删除用：{id, ip, name}），
    回调按下标取回——实例 OCID 超过 Telegram callback_data
    64 字节上限，不能直接放回调里。
    """
    cli = get_cli(context)
    try:
        instances = await run_sync(list_instances, cli,
                                   get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    lines = ["🌐 IPv6 管理：", ""]
    vnics = []
    entries = []
    rows = []
    for inst in instances:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        name = inst.get("display-name") or "?"
        try:
            inst_vnics = await run_sync(list_vnics, cli, inst["id"])
            primary = next((v for v in inst_vnics if v.get("is-primary")),
                           None)
            if primary is None:
                lines.append(f"• {name}：无主 VNIC")
                continue
            vnic_idx = len(vnics)
            vnics.append({"name": name, "vnic_id": primary["id"]})
            ipv6s = await run_sync(list_ipv6_for_vnic, cli, primary["id"])
            if not ipv6s:
                lines.append(f"• {name}：无 IPv6")
                rows.append([InlineKeyboardButton(
                    f"➕ {name} 创建 IPv6",
                    callback_data=f"ipv6_create:{vnic_idx}")])
            else:
                addrs = ", ".join(v.get("ip-address", "?") for v in ipv6s)
                lines.append(f"• {name}：{addrs}")
                for v in ipv6s:
                    entries.append({
                        "id": v.get("id"),
                        "ip": v.get("ip-address", "?"),
                        "name": name,
                    })
                    rows.append([InlineKeyboardButton(
                        f"🗑 删除 {name} {v.get('ip-address', '?')}",
                        callback_data=f"ipv6_delete:{len(entries) - 1}")])
        except Exception as exc:  # noqa: BLE001 - 单台报错不影响其他台
            lines.append(f"• {name}：{fmt_error(exc)}")
    if not vnics:
        lines.append("没有运行中的实例。")
    context.user_data["ipv6_vnics"] = vnics
    context.user_data["ipv6_entries"] = entries
    rows.append([InlineKeyboardButton("◀ Oracle 菜单",
                                      callback_data="o:menu")])
    await _reply(update, context, "\n".join(lines),
                 reply_markup=InlineKeyboardMarkup(rows))


@require_allowed
async def ipv6_create_callback(update, context):
    """ipv6_create:<idx>：给 user_data["ipv6_vnics"][idx] 的 VNIC
    分配一个 IPv6，直接执行并回显新地址。"""
    parts = _callback_data(update).split(":")
    try:
        idx = int(parts[1]) if len(parts) >= 2 else -1
    except ValueError:
        idx = -1
    targets = context.user_data.get("ipv6_vnics") or []
    if idx < 0 or idx >= len(targets):
        await _reply(update, context, "目标已过期，请重新打开 IPv6 管理。",
                     reply_markup=back_to_oracle_keyboard())
        return
    target = targets[idx]
    try:
        created = await run_sync(create_ipv6, get_cli(context),
                                 target["vnic_id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc),
                     reply_markup=back_to_oracle_keyboard())
        return
    addr = (created or {}).get("ip-address", "?")
    await _reply(update, context, f"✅ {target['name']} 已分配 IPv6：{addr}",
                 reply_markup=back_to_oracle_keyboard())


@require_allowed
async def ipv6_delete_callback(update, context):
    """ipv6_delete:<idx>：删除 IPv6 先弹二次确认
    （确认按钮 c:delipv6:<idx>，由 handle_confirm 执行）。"""
    parts = _callback_data(update).split(":")
    try:
        idx = int(parts[1]) if len(parts) >= 2 else -1
    except ValueError:
        idx = -1
    entries = context.user_data.get("ipv6_entries") or []
    if idx < 0 or idx >= len(entries):
        await _reply(update, context, "目标已过期，请重新打开 IPv6 管理。",
                     reply_markup=back_to_oracle_keyboard())
        return
    entry = entries[idx]
    text = (f"🗑️ 确认删除 {entry['name']} 的 IPv6 {entry['ip']}？\n\n"
            "删除后该地址立即不可达。")
    await _reply(update, context, text,
                 reply_markup=confirm_keyboard("delipv6", idx,
                                               "🗑️ 确认删除"))


# ---- 3. 被暂停开机 ----

@require_allowed
async def handle_resume(update, context):
    """菜单 3：列出 STOPPED 状态实例，点进详情可启动。"""
    try:
        instances = await run_sync(list_instances,
                                   get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    stopped = [i for i in instances
               if i.get("lifecycle-state") == "STOPPED"]
    if not stopped:
        await _reply(update, context, "没有被暂停（STOPPED）的实例。",
                     reply_markup=back_to_oracle_keyboard())
        return
    stopped = await run_sync(attach_public_ips, get_cli(context), stopped)
    store_instances(context, stopped)
    await _reply(update, context, fmt_instance_list(stopped,
                                           _switch_menu_account_label(context)),
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
        await _reply(update, context, "请先在实例列表里选一台实例，"
                     "再从菜单选 5. 升/降级实例。",
                     reply_markup=back_to_oracle_keyboard())
        return
    shape = inst.get("shape") or ""
    name = inst.get("display-name") or "?"
    if shape == SHAPE_E2_MICRO:
        await _reply(update, context,
                     f"🖥️ {name} 是 E2.1.Micro（固定 1 核 / 1GB），"
                     "不支持升/降配。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if shape != SHAPE_A1_FLEX:
        await _reply(update, context,
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
    await _reply(update, context,
                 f"📐 {name} 当前配置：{current}\n选目标规格：",
                 reply_markup=resize_preset_keyboard(idx))


@require_allowed
async def resize_preset_callback(update, context):
    """回调 "r:<ocpus>:<mem>:<idx>"：存目标规格，弹二次确认键盘。"""
    parts = _callback_data(update).split(":")
    if len(parts) != 4:
        await _reply(update, context, "参数错误，请重新选择。")
        return
    _, ocpus, memory_gb, idx = parts
    try:
        target = {"ocpus": int(ocpus), "memory_gb": int(memory_gb)}
    except ValueError:
        await _reply(update, context, "参数错误，请重新选择。")
        return
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
        return
    context.user_data["pending_resize"] = target
    context.user_data["pending_idx"] = idx
    name = inst.get("display-name") or "?"
    await _reply(
        update, context,
        f"⚠️ 确认把 {name} 改为 {target['ocpus']} 核 / "
        f"{target['memory_gb']}GB？\n改规格时实例可能会重启。",
        reply_markup=confirm_keyboard("resize", idx, "✅ 确认改规格"))


# ---- 7. 磁盘管理 ----

@require_allowed
async def handle_volumes(update, context):
    """菜单 7：启动盘列表 + 孤儿盘 + 总容量。

    已删除 / 删除中的盘一律不显示（用户要求 2026-01-01：已删除的
    都不显示，屏蔽掉）；过滤放在这里（存列表之前），文字、按钮与
    回调下标都按过滤后的列表来，不会错位。
    """
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        volumes = await run_sync(list_boot_volumes, cli, compartment)
        orphans = await run_sync(list_orphan_boot_volumes, cli, compartment)
        total = await run_sync(total_boot_volume_gb, cli, compartment)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    volumes = [v for v in volumes
               if (v.get("lifecycle-state") or "").upper()
               not in ("TERMINATED", "TERMINATING")]
    store_volumes(context, volumes)
    await _reply(update, context, fmt_volume_list(volumes, orphans, total),
                 reply_markup=volume_list_keyboard(volumes, orphans))


@require_allowed
async def volume_detail_callback(update, context):
    """回调 "v:detail:<idx>"：磁盘详情 + 扩容/删除按钮。"""
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    try:
        detail = await run_sync(get_boot_volume, get_cli(context), vol["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    state = (detail.get("lifecycle-state") if isinstance(detail, dict)
             else None) or vol.get("lifecycle-state")
    await _reply(update, context, fmt_volume_detail(detail),
                 reply_markup=volume_actions_keyboard(idx, state))


@require_allowed
async def volume_resize_callback(update, context):
    """回调 "v:resize:<idx>"：显示扩容目标容量按钮（100/200GB）。"""
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    if (vol.get("lifecycle-state") or "").upper() in ("TERMINATED", "TERMINATING"):
        await _reply(update, context, "该启动盘已删除，无法操作。")
        return
    name = vol.get("display-name") or "?"
    await _reply(update, context,
                 f"📐 {name} 当前 {vol.get('size-in-gbs')}GB，"
                 "选目标容量（只能扩不能缩）：",
                 reply_markup=volume_resize_keyboard(idx))


@require_allowed
async def volume_size_callback(update, context):
    """回调 "v:volsize:<idx>:<gb>"：执行扩容。"""
    parts = _callback_data(update).split(":")
    if len(parts) != 4:
        await _reply(update, context, "参数错误，请重新选择。")
        return
    _, _, idx, size = parts
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    try:
        target_gb = int(size)
    except ValueError:
        await _reply(update, context, "参数错误，请重新选择。")
        return
    current_gb = vol.get("size-in-gbs") or 0
    if target_gb <= current_gb:
        await _reply(update, context,
                     f"目标容量 {target_gb}GB 必须大于当前容量 {current_gb}GB，"
                     "启动盘只能扩不能缩。")
        return
    try:
        await run_sync(resize_boot_volume, get_cli(context),
                       vol["id"], target_gb)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    name = vol.get("display-name") or "?"
    await _reply(update, context,
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
        await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    if (vol.get("lifecycle-state") or "").upper() in ("TERMINATED", "TERMINATING"):
        await _reply(update, context, "该启动盘已删除，无法操作。")
        return
    name = vol.get("display-name") or "?"
    await _reply(
        update, context,
        f"⚠️ 即将删除启动盘 {name}（{vol.get('size-in-gbs')}GB）！\n"
        "删除后数据永久丢失，无法恢复。\n"
        "挂在实例上的盘删不掉，必须先终止实例（或先分离）。",
        reply_markup=confirm_keyboard("delvol", idx, "🗑️ 确认删除"))


# ---- 7b. 启动卷备份与恢复（M_Bot 独有） ----

@require_allowed
async def boot_backups_callback(update, context):
    """回调 "bb:list"：启动卷备份列表 + 额度（付费账号不限 5 个）。"""
    cli = get_cli(context)
    compartment = get_compartment(context)
    paid = is_paid_profile(get_current_profile(context))
    try:
        backups = await run_sync(list_boot_volume_backups, cli, compartment)
        count, limit, full, _oldest = await run_sync(
            check_backup_quota, cli, compartment)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    store_boot_backups(context, backups)
    await _reply(update, context, fmt_boot_backup_list(backups, count, limit, full,
                                              paid),
                 reply_markup=boot_backup_list_keyboard(backups, paid))


async def _edit_boot_backups(query, context):
    """就地刷新备份列表消息（付费切换 / 退出多选后用）。"""
    cli = get_cli(context)
    compartment = get_compartment(context)
    paid = is_paid_profile(get_current_profile(context))
    try:
        backups = await run_sync(list_boot_volume_backups, cli, compartment)
        count, limit, full, _oldest = await run_sync(
            check_backup_quota, cli, compartment)
    except Exception as exc:  # noqa: BLE001
        await menu_tracker.safe_edit(query, fmt_error(exc))
        return
    store_boot_backups(context, backups)
    context.user_data.pop("bb_ms_selected", None)
    await menu_tracker.safe_edit(query, 
        fmt_boot_backup_list(backups, count, limit, full, paid),
        reply_markup=boot_backup_list_keyboard(backups, paid))


@require_allowed
async def boot_backup_paid_callback(update, context):
    """回调 "bb:paid"：切换当前账号的付费/免费标记，就地刷新列表。"""
    query = update.callback_query
    profile = get_current_profile(context)
    paid = set_paid_profile(profile, not is_paid_profile(profile))
    await query.answer(f"已设为{'付费' if paid else '免费'}账号")
    await _edit_boot_backups(query, context)


def _start_backup_watch(update, cli, backup, name):
    """备份创建成功后起后台线程：等备份 AVAILABLE 后双通道通知。

    chat_id 拿不到（无聊天上下文）或备份无 id 时静默跳过。
    """
    chat_id = update.effective_chat.id if update.effective_chat else None
    backup_id = (backup or {}).get("id")
    if chat_id is None or not backup_id:
        return
    start_backup_watch(cli, backup_id, name,
                       backup.get("display-name") or "?",
                       bot_config.TELEGRAM_BOT_TOKEN, chat_id)


@require_allowed
async def volume_backup_callback(update, context):
    """回调 "v:backup:<idx>"：给该启动卷做 FULL 全量备份。

    付费账号：不限 5 个，直接创建。
    免费账号：未满直接创建；满了自动删除最早备份再创建（用户要求
    2026-01-01：不用等确认），结果消息里说明删了哪个。
    """
    idx = _callback_data(update).split(":")[-1]
    vol = resolve_volume(context, idx)
    if vol is None:
        await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
        return
    if (vol.get("lifecycle-state") or "").upper() in ("TERMINATED", "TERMINATING"):
        await _reply(update, context, "该启动盘已删除，无法操作。")
        return
    cli = get_cli(context)
    compartment = get_compartment(context)
    paid = is_paid_profile(get_current_profile(context))
    name = vol.get("display-name") or "?"
    deleted_oldest = None
    try:
        if paid:
            backup = await run_sync(create_boot_volume_backup,
                                    cli, vol["id"])
        else:
            count, limit, full, oldest = await run_sync(
                check_backup_quota, cli, compartment)
            if full:
                if oldest is None:
                    await _reply(update, context,
                                 "免费备份已满，但未找到可删除的最早备份，"
                                 "请手动清理。")
                    return
                await run_sync(delete_boot_volume_backup, cli, oldest["id"])
                deleted_oldest = oldest.get("display-name") or "?"
                # Creating right after a delete can hit OCI quota-sync lag (LimitExceeded), so create with retries. 删完立即创建可能撞上 OCI 配额同步延迟（LimitExceeded），用带重试的创建。
                backup = await run_sync(
                    create_boot_volume_backup_with_limit_retry,
                    cli, vol["id"])
            else:
                backup = await run_sync(create_boot_volume_backup,
                                        cli, vol["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    backup_name = backup.get("display-name") or "?"
    if paid:
        await _reply(update, context,
                     f"✅ {name} 的备份「{backup_name}」创建中，"
                     "几分钟后可用（付费账号，备份不限 5 个）。")
    elif deleted_oldest is not None:
        await _reply(update, context,
                     f"✅ 免费备份已满，已自动删除最早备份"
                     f"「{deleted_oldest}」，新备份「{backup_name}」"
                     "创建中，几分钟后可用。")
    else:
        await _reply(update, context,
                     f"✅ {name} 的备份「{backup_name}」创建中，"
                     f"几分钟后可用（免费额度 {count + 1}/{limit}）。")
    _start_backup_watch(update, cli, backup, name)


@require_allowed
async def boot_backup_restore_callback(update, context):
    """回调 "bb:restore:<idx>"：从备份恢复——先让用户输入新实例名。

    只有 AVAILABLE 状态的备份才能恢复；新实例名由用户回复文字
    （发"默认"则用"<备份名>-restored"），见 text_input_callback
    的 "restore_name" 分支；真正执行在 handle_confirm("restorebb")。
    形状默认用备份来源实例的形状（查不到用 A1 1核/6GB）。
    """
    idx = _callback_data(update).split(":")[-1]
    backup = resolve_boot_backup(context, idx)
    if backup is None:
        await _reply(update, context, "备份不存在或已过期，请重新打开启动卷备份列表。")
        return
    if (backup.get("lifecycle-state") or "").upper() != "AVAILABLE":
        await _reply(update, context,
                     f"备份「{backup.get('display-name') or '?'}」当前状态是 "
                     f"{backup.get('lifecycle-state') or '未知'}，"
                     "只有 AVAILABLE 状态的备份才能恢复。")
        return
    context.user_data["pending_input"] = {"kind": "restore_name", "idx": idx}
    await _reply_prompt(
        update, context,
        f"♻️ 从备份「{backup.get('display-name') or '?'}」恢复出一台"
        "新实例（原机不动）。\n"
        "请回复新实例名，或直接发送“默认”使用 "
        f"「{backup.get('display-name') or 'backup'}-restored」。\n"
        "形状默认用备份来源实例的形状（查不到用 A1 1核/6GB）；"
        "恢复前会预检免费额度，额度不够会拦截。")


@require_allowed
async def boot_backup_delete_callback(update, context):
    """回调 "bb:delete:<idx>"：删除备份警告 + 二次确认键盘。

    真正执行在 handle_confirm("delbb")。
    """
    idx = _callback_data(update).split(":")[-1]
    backup = resolve_boot_backup(context, idx)
    if backup is None:
        await _reply(update, context, "备份不存在或已过期，请重新打开启动卷备份列表。")
        return
    name = backup.get("display-name") or "?"
    await _reply(
        update, context,
        f"⚠️ 即将删除备份 {name}（{backup.get('size-in-gbs')}GB）！\n"
        "删除后无法再用它恢复实例，不可恢复。",
        reply_markup=confirm_keyboard("delbb", idx, "🗑️ 确认删除备份"))


# ---------------- 实例详情：引导卷备份与删除历史备份 ----------------

@require_allowed
async def instance_boot_backup_callback(update, context):
    """实例操作“引导卷备份”（dispatch 项 "bootbackup"）。

    由 handlers.py 的 "i:bootbackup:<idx>" 回调转发（pending_idx 已写入）。
    付费账号直接创建；免费账号满 5 个时不自动删，提示先删除再创建，
    并给“删除历史备份”按钮（用户要求 2026-01-01）；未满直接创建。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
        return
    name = inst.get("display-name") or "?"
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        attachment = await run_sync(get_boot_volume_attachment, cli,
                                    compartment, inst["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    if attachment is None:
        await _reply(update, context, f"找不到实例「{name}」当前挂载的启动盘。")
        return
    boot_volume_id = attachment.get("boot-volume-id")
    paid = is_paid_profile(get_current_profile(context))
    count = limit = 0
    try:
        if not paid:
            count, limit, full, _oldest = await run_sync(
                check_backup_quota, cli, compartment)
            if full:
                await _reply(
                    update, context,
                    f"⚠️ 备份已满 {count}/{limit} 个（免费额度），请先删除"
                    "旧备份再创建。",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "🗑️ 删除历史备份",
                            callback_data=f"i:bootbackup-del:{idx}")],
                        [InlineKeyboardButton(
                            "◀ 返回实例详情",
                            callback_data=f"i:detail:{idx}")],
                    ]))
                return
        backup = await run_sync(create_boot_volume_backup, cli,
                                boot_volume_id)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    backup_name = backup.get("display-name") or "?"
    if paid:
        await _reply(update, context,
                     f"✅ {name} 的备份「{backup_name}」创建中，"
                     "几分钟后可用（付费账号，备份不限 5 个）。")
    else:
        await _reply(update, context,
                     f"✅ {name} 的备份「{backup_name}」创建中，"
                     f"几分钟后可用（免费额度 {count + 1}/{limit}）。")
    _start_backup_watch(update, cli, backup, name)


@require_allowed
async def instance_boot_backup_delete_callback(update, context):
    """实例操作“删除历史备份”（dispatch 项 "bootbackup-del"）。

    列出本实例当前启动盘的备份（未删除的），多选删除（与菜单 7 的
    多选模式同款，回调前缀 "ib"）。由 handlers.py 的
    "i:bootbackup-del:<idx>" 回调转发（pending_idx 已写入）。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
        return
    name = inst.get("display-name") or "?"
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        attachment = await run_sync(get_boot_volume_attachment, cli,
                                    compartment, inst["id"])
        if attachment is None:
            await _reply(update, context, f"找不到实例「{name}」当前挂载的启动盘。")
            return
        backups = await run_sync(
            list_boot_volume_backups, cli, compartment,
            boot_volume_id=attachment.get("boot-volume-id"))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    if not backups:
        await _reply(update, context, f"实例「{name}」的启动盘还没有备份。")
        return
    context.user_data["ib_backups"] = backups
    context.user_data["ib_idx"] = idx
    context.user_data["ib_title"] = f"「{name}」的历史备份"
    context.user_data["ib_ms_selected"] = set()
    await _reply(update, context,
                 _fmt_boot_backup_multiselect(
                     backups, set(), title=f"「{name}」的历史备份"),
                 reply_markup=boot_backup_multiselect_keyboard(
                     backups, set(), prefix="ib"))


@require_allowed
async def instance_boot_backup_mstoggle_callback(update, context):
    """回调 "ib:mst:<idx>"：勾选 / 取消勾选一个本实例备份。"""
    query = update.callback_query
    backups = context.user_data.get("ib_backups") or []
    try:
        idx = int(_callback_data(update).split(":")[-1])
    except ValueError:
        await query.answer("参数错误")
        return
    if not 0 <= idx < len(backups):
        await query.answer("备份已过期，请重新打开列表")
        return
    selected = context.user_data.get("ib_ms_selected")
    if not isinstance(selected, set):
        selected = set()
        context.user_data["ib_ms_selected"] = selected
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await menu_tracker.safe_edit(query, 
        _fmt_boot_backup_multiselect(
            backups, selected,
            title=context.user_data.get("ib_title")),
        reply_markup=boot_backup_multiselect_keyboard(backups, selected,
                                                      prefix="ib"))


@require_allowed
async def instance_boot_backup_msdelete_callback(update, context):
    """回调 "ib:msdel"：多选删除二次确认（真正执行在 handle_confirm
    的 "delibbsel"，idx 是逗号分隔的下标，如 "0,2,3"）。"""
    selected = context.user_data.get("ib_ms_selected") or set()
    backups = context.user_data.get("ib_backups") or []
    if not selected:
        await update.callback_query.answer("还没选任何备份")
        return
    names = []
    for idx in sorted(selected):
        if 0 <= idx < len(backups):
            names.append(backups[idx].get("display-name") or "?")
    if not names:
        await _reply(update, context, "所选备份已过期，请重新打开列表。")
        return
    idx_arg = ",".join(str(i) for i in sorted(selected))
    await _reply(
        update, context,
        f"⚠️ 即将删除 {len(names)} 个备份：\n"
        + "\n".join(f"• {n}" for n in names)
        + "\n删除后无法再用它们恢复实例，不可恢复。",
        reply_markup=confirm_keyboard(
            "delibbsel", idx_arg, f"🗑️ 确认删除 {len(names)} 个备份"))


@require_allowed
async def instance_boot_backup_mscancel_callback(update, context):
    """回调 "ib:mscancel"：退出多选删除模式，回到实例详情入口。"""
    query = update.callback_query
    idx = context.user_data.get("ib_idx")
    context.user_data.pop("ib_ms_selected", None)
    rows = []
    if idx is not None:
        rows.append([InlineKeyboardButton(
            "◀ 返回实例详情", callback_data=f"i:detail:{idx}")])
    rows.append([InlineKeyboardButton("◀ 实例列表", callback_data="i:list")])
    await menu_tracker.safe_edit(query, "已退出多选。",
                                  reply_markup=InlineKeyboardMarkup(rows))


# ---------------- 引导卷恢复（停机换盘，原地恢复） ----------------

# Set of instance OCIDs currently being disk-swapped (only one swap task per instance at a time). 正在换盘的实例 OCID 集合（同一实例同时只允许一个换盘任务）。
_BOOTSWAP_RUNNING = set()
_BOOTSWAP_LOCK = threading.Lock()


@require_allowed
async def handle_bootswap(update, context):
    """实例操作“引导卷恢复”的备份选择（dispatch 项 "bootswap"）。

    由 handlers.py 的 "i:bootswap:<idx>" 回调转发（pending_idx 已写入）。
    列出该实例当前启动盘的 AVAILABLE 备份，点备份进确认页
    （bootswap_callback），真正执行在 handle_confirm("bootswap")。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, "请先在实例列表里选一台实例，"
                     "再点“引导卷恢复”。",
                     reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or "?"
    state = (inst.get("lifecycle-state") or "").upper()
    if state not in ("RUNNING", "STOPPED"):
        await _reply(update, context,
                     f"实例「{name}」当前状态是 {state or '未知'}，"
                     "只有运行中或已停止的实例才能恢复引导卷。")
        return
    cli = get_cli(context)
    compartment = get_compartment(context)
    try:
        attachment = await run_sync(get_boot_volume_attachment, cli,
                                    compartment, inst["id"])
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        await _reply(update, context, fmt_error(exc))
        return
    if attachment is None:
        await _reply(update, context,
                     f"找不到实例「{name}」当前挂载的启动盘，无法恢复引导卷。")
        return
    try:
        backups = await run_sync(
            list_boot_volume_backups, cli, compartment,
            boot_volume_id=attachment.get("boot-volume-id"))
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        await _reply(update, context, fmt_error(exc))
        return
    available = [b for b in backups
                 if (b.get("lifecycle-state") or "").upper() == "AVAILABLE"]
    if not available:
        # When the current disk has no backup, also check whether the account still has a usable backup of another disk (e.g. a backup of the old disk from before the swap): if yes, say so, so the user does not think deleting the disk also deleted the backups. A failed lookup does not affect the main prompt. 当前盘没备份时，顺便看一眼账号里是否还有别的盘的可用备份（比如换盘前旧盘的备份）：有就说明白，免得用户以为删盘把备份也删了。查不到也不影响主提示。
        other_note = ""
        try:
            all_backups = await run_sync(
                list_boot_volume_backups, cli, compartment)
            others = [b for b in all_backups
                      if (b.get("lifecycle-state") or "").upper()
                      == "AVAILABLE"]
            if others:
                other_note = (
                    f"\n注：账号里还有 {len(others)} 个其他启动盘的可用备份"
                    "（比如换盘前旧盘的备份），删盘不会删备份；原地恢复只列"
                    "当前启动盘的备份，旧备份可从备份页恢复成新实例。")
        except Exception:  # noqa: BLE001 - 查不到旧备份也不影响主提示
            other_note = ""
        await _reply(
            update, context,
            f"实例「{name}」的启动盘还没有可用备份（AVAILABLE）。\n"
            "请先在菜单 7（启动盘）里给它创建一个备份，几分钟后可用。"
            f"{other_note}",
            reply_markup=back_to_oracle_keyboard())
        return
    context.user_data["bootswap_backups"] = available
    context.user_data["bootswap_idx"] = idx
    rows = []
    for i, backup in enumerate(available):
        created = (backup.get("time-created") or "")[:16].replace("T", " ")
        rows.append([InlineKeyboardButton(
            f"{backup.get('display-name') or '?'}"
            f"（{backup.get('size-in-gbs')}GB，{created}）",
            callback_data=f"bs:pick:{i}")])
    rows.append([InlineKeyboardButton("◀ 返回实例",
                                      callback_data=f"i:detail:{idx}")])
    await _reply(
        update, context,
        f"💾 引导卷恢复：给实例「{name}」选一个备份：\n\n"
        "恢复流程：从备份建新启动盘 → 停机 → 换盘 → 开机 → 删除旧盘。\n"
        "恢复成功后旧启动盘会自动删除，不留旧盘占额度。",
        reply_markup=InlineKeyboardMarkup(rows))


@require_allowed
async def bootswap_callback(update, context):
    """回调 "bs:pick:<bidx>"：选备份后出确认页。

    真正执行在 handle_confirm("bootswap")。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action != "pick":
        return
    try:
        bidx = int(parts[2]) if len(parts) > 2 else -1
    except ValueError:
        bidx = -1
    backups = context.user_data.get("bootswap_backups") or []
    inst = resolve_instance(context, context.user_data.get("bootswap_idx"))
    if bidx < 0 or bidx >= len(backups) or inst is None:
        await _reply(update, context,
                     "备份选择已过期，请重新打开实例详情再点“引导卷恢复”。")
        return
    backup = backups[bidx]
    name = inst.get("display-name") or "?"
    backup_name = backup.get("display-name") or "?"
    created = (backup.get("time-created") or "")[:16].replace("T", " ")
    text = (
        f"⚠️ 确认用备份恢复实例「{name}」的引导卷？\n\n"
        f"备份：{backup_name}（{backup.get('size-in-gbs')}GB，{created}）\n\n"
        "恢复流程：\n"
        "① 从备份创建新启动盘\n"
        "② 实例停机\n"
        "③ 拆下旧启动盘，挂上新盘\n"
        "④ 重新开机\n"
        "⑤ 删除旧启动盘\n\n"
        "⚠️ 恢复后系统盘回到备份时的状态，备份之后的数据会丢失；\n"
        "恢复成功后旧启动盘会自动删除，不留旧盘占额度。\n"
        "整个过程约几分钟，完成后会发通知。"
    )
    await _reply(update, context, text,
                 reply_markup=confirm_keyboard("bootswap", bidx, "💾 确认恢复"))


def _bootswap_thread(token, chat_id, profile, region, compartment,
                     instance_id, instance_name, backup_id, backup_name):
    """引导卷恢复的后台线程体：跑换盘，进度与结果走 notify_dual 发。

    换盘要几分钟（建盘 + 停机 + 换盘 + 开机），不能在回调里等；
    结束时把实例从 _BOOTSWAP_RUNNING 里放掉。
    """
    try:
        cli = bot_config.get_oci_client(profile=profile, region=region)

        def on_step(text):
            notify_dual(token, chat_id,
                        f"💾 引导卷恢复「{instance_name}」", text,
                        group="bootswap")

        result = swap_boot_volume(cli, compartment, instance_id, backup_id,
                                  on_step=on_step)
        old_tail = (result.get("old_boot_volume_id") or "")[-4:]
        new_tail = (result.get("new_boot_volume_id") or "")[-4:]
        old_note = ("已删除，额度已释放" if result.get("old_volume_deleted")
                    else "已拆下，自动删除失败，请手动删除")
        body = (
            f"✅ 实例「{instance_name}」引导卷恢复成功。\n"
            f"备份：{backup_name}\n"
            f"新启动盘：…{new_tail}\n"
            f"旧启动盘：…{old_tail}（{old_note}）\n"
            + ("实例已重新开机。"
               if result.get("was_running") else "实例保持停止状态。"))
        notify_dual(token, chat_id, "💾 引导卷恢复成功", body,
                    group="bootswap")
    except BootSwapError as exc:
        body = (
            f"❌ 实例「{instance_name}」引导卷恢复失败：{exc}\n"
            + ("已换回旧盘。" if exc.rolled_back
               else "回滚未完成，请在控制台手动处理。"))
        notify_dual(token, chat_id, "💾 引导卷恢复失败", body,
                    group="bootswap")
    except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要通知用户
        notify_dual(token, chat_id, "💾 引导卷恢复失败",
                    f"❌ 实例「{instance_name}」引导卷恢复失败：{exc}",
                    group="bootswap")
    finally:
        with _BOOTSWAP_LOCK:
            _BOOTSWAP_RUNNING.discard(instance_id)


def _fmt_boot_backup_multiselect(backups, selected, title=None):
    """多选删除模式的列表文字：标题 + 已选数量 + 操作提示。"""
    head = title or "启动卷备份"
    return (f"💾 {head}（多选删除，共 {len(backups)} 个）：\n"
            f"已选 {len(selected)} 个，点备份勾选 / 取消勾选，"
            "再点「删除所选」。")


@require_allowed
async def boot_backup_multiselect_callback(update, context):
    """回调 "bb:ms"：进入多选删除模式（就地把键盘换成勾选键盘）。"""
    query = update.callback_query
    backups = context.user_data.get("boot_backups") or []
    if not backups:
        await query.answer("没有可删除的备份")
        return
    context.user_data["bb_ms_selected"] = set()
    await menu_tracker.safe_edit(query, 
        _fmt_boot_backup_multiselect(backups, set()),
        reply_markup=boot_backup_multiselect_keyboard(backups, set()))


@require_allowed
async def boot_backup_mstoggle_callback(update, context):
    """回调 "bb:mst:<idx>"：勾选 / 取消勾选一个备份，只刷新键盘与文字。"""
    query = update.callback_query
    backups = context.user_data.get("boot_backups") or []
    try:
        idx = int(_callback_data(update).split(":")[-1])
    except ValueError:
        await query.answer("参数错误")
        return
    if not 0 <= idx < len(backups):
        await query.answer("备份已过期，请重新打开列表")
        return
    selected = context.user_data.get("bb_ms_selected")
    if not isinstance(selected, set):
        selected = set()
        context.user_data["bb_ms_selected"] = selected
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await menu_tracker.safe_edit(query, 
        _fmt_boot_backup_multiselect(backups, selected),
        reply_markup=boot_backup_multiselect_keyboard(backups, selected))


@require_allowed
async def boot_backup_msdelete_callback(update, context):
    """回调 "bb:msdel"：多选删除二次确认（真正执行在 handle_confirm
    的 "delbbsel"，idx 是逗号分隔的下标，如 "0,2,3"）。"""
    selected = context.user_data.get("bb_ms_selected") or set()
    backups = context.user_data.get("boot_backups") or []
    if not selected:
        await update.callback_query.answer("还没选任何备份")
        return
    names = []
    for idx in sorted(selected):
        if 0 <= idx < len(backups):
            names.append(backups[idx].get("display-name") or "?")
    if not names:
        await _reply(update, context, "所选备份已过期，请重新打开列表。")
        return
    idx_arg = ",".join(str(i) for i in sorted(selected))
    await _reply(
        update, context,
        f"⚠️ 即将删除 {len(names)} 个备份：\n"
        + "\n".join(f"• {n}" for n in names)
        + "\n删除后无法再用它们恢复实例，不可恢复。",
        reply_markup=confirm_keyboard(
            "delbbsel", idx_arg, f"🗑️ 确认删除 {len(names)} 个备份"))


@require_allowed
async def boot_backup_mscancel_callback(update, context):
    """回调 "bb:mscancel"：退出多选删除模式，就地恢复正常列表。"""
    query = update.callback_query
    await _edit_boot_backups(query, context)


# ---- 8. 云账户管理 / 30. 批量查询邮箱 ----

def _get_iam_cli(context):
    """IAM 是租户级服务，固定使用当前 profile 配置的主区域。"""
    profile = get_current_profile(context)
    return bot_config.get_oci_client(
        profile=profile, region=bot_config.get_region(profile))


def _get_iam_tenancy_id(context):
    """读取当前 profile 的 tenancy OCID，不能误用 compute 子 compartment。"""
    profile = get_current_profile(context)
    return (get_profile_tenancy_id(profile)
            or get_compartment(context))


@require_allowed
async def handle_users(update, context):
    """菜单 8：列出 IAM 用户，每个用户挂详情按钮，详情页可修改。

    用户列表存 user_data["iam_users"]（完整 dict），回调按下标取回——
    用户 OCID 超过 Telegram callback_data 64 字节上限，不能直接放回调里。
    """
    try:
        users = await run_sync(list_users,
                               _get_iam_cli(context), _get_iam_tenancy_id(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    context.user_data["iam_users"] = users
    context.user_data["iam_users_profile"] = get_current_profile(context)
    context.user_data["iam_users_region"] = get_current_region(context)
    rows = [[InlineKeyboardButton(_user_button_text(u),
                                  callback_data=f"u:detail:{idx}")]
            for idx, u in enumerate(users)]
    rows.append([InlineKeyboardButton("➕ 增加普通用户",
                                      callback_data="u:adduser")])
    rows.append([InlineKeyboardButton("➕ 增加管理员账号",
                                      callback_data="u:add")])
    rows.append([InlineKeyboardButton("🤖 创建 Bot API 权限用户",
                                      callback_data="u:botuser")])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单",
                                      callback_data="o:menu")])
    await _reply(update, context, fmt_user_list(users),
                 reply_markup=InlineKeyboardMarkup(rows))


def _resolve_iam_user(context, idx):
    """按下标取用户；列表属于其他 profile/region 时视为过期。"""
    if (context.user_data.get("iam_users_profile") not in
            (None, get_current_profile(context))
            or context.user_data.get("iam_users_region") not in
            (None, get_current_region(context))):
        return None
    try:
        idx_int = int(idx)
    except (TypeError, ValueError):
        return None
    users = context.user_data.get("iam_users") or []
    if idx_int < 0 or idx_int >= len(users):
        return None
    return users[idx_int]


def _is_current_api_user(context, user_id):
    """目标用户是不是机器人当前 profile 正在用的 API 用户。

    从 OCI 配置文件（~/oci_config）里读当前 profile 的 user OCID 比对；读不到时
    返回 False（配置缺失时 OCI 调用本身也会失败，删操作到不了 OCI）。
    """
    if not user_id:
        return False
    current = get_profile_user_id(get_current_profile(context))
    return current is not None and current == user_id


def _is_current_api_key(context, user_id, fingerprint):
    """目标 API Key 是否正是当前 profile 正在签名使用的密钥。"""
    if not user_id or not fingerprint or not _is_current_api_user(context, user_id):
        return False
    current_fp = get_profile_api_fingerprint(get_current_profile(context))
    return bool(current_fp and current_fp == fingerprint.strip().lower())


def fmt_user_detail(user):
    """单个 IAM 用户详情全文（不显示完整 OCID）。"""
    lines = [
        f"👤 {user.get('name', '?')}",
        f"邮箱：{user.get('email') or '无邮箱'}",
        f"状态：{user.get('lifecycle-state', '?')}",
    ]
    desc = user.get("description")
    if desc:
        lines.append(f"描述：{desc}")
    created = (user.get("time-created") or "")[:19].replace("T", " ")
    if created:
        lines.append(f"创建时间：{created}")
    return "\n".join(lines)


def _user_detail_keyboard(idx):
    """用户详情页的操作键盘。

    注意：OCI 不允许改用户名（UpdateUser API 没有 name 字段），
    所以这里提供的是改描述，不是改名。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ 改描述", callback_data=f"u:desc:{idx}"),
         InlineKeyboardButton("📧 改邮箱", callback_data=f"u:email:{idx}")],
        [InlineKeyboardButton("🔑 API 密钥", callback_data=f"u:keys:{idx}"),
         InlineKeyboardButton("➕ 新增 API Key", callback_data=f"u:addkey:{idx}")],
        [InlineKeyboardButton("📋 粘贴公钥添加", callback_data=f"u:uploadkey:{idx}"),
         InlineKeyboardButton("🔐 重置控制台密码", callback_data=f"u:resetpwd:{idx}")],
        [InlineKeyboardButton("🗑️ 删除用户", callback_data=f"u:del:{idx}"),
         InlineKeyboardButton("◀ 返回云账户", callback_data="u:list")],
    ])


@require_allowed
async def user_callback(update, context):
    """u: 前缀回调总入口（云账户管理详情页的操作）。

    u:detail:<idx> 用户详情；u:desc:<idx> 改描述（文字输入）；
    u:email:<idx> 改邮箱（文字输入）；u:keys:<idx> API 密钥列表；
    u:del:<idx> 删除用户（输入用户名确认）；u:keydel:<idx> 删 API
    密钥（二次确认）；u:addkey:<idx> 创建 API Key；
    u:resetpwd:<idx> 重置控制台临时密码；u:adduser 增加普通用户；
    u:add 增加管理员账号；u:botuser 创建 Bot 权限用户；u:list 返回列表。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    if action == "list":
        await handle_users(update, context)
        return
    if action == "adduser":
        context.user_data["iam_add_user_target"] = (
            get_current_profile(context), get_current_region(context))
        context.user_data["pending_input"] = {"kind": "add_user_name"}
        context.user_data.pop("add_iam_user", None)
        await _reply_prompt(update, context,
                            "➕ 增加普通 IAM 用户：请输入用户名（不能带空格）：")
        return
    if action == "botuser":
        context.user_data["iam_bot_target"] = (
            get_current_profile(context), get_current_region(context))
        await _reply(update, context,
                     "🤖 将在当前 Oracle 账号创建专用 Bot API 用户：\n\n"
                     "用户名：api_user\n邮箱：api@example.com\n"
                     "组：Group_for_Api_used\n策略：Api_User_Policy\n\n"
                     "权限：manage instance-family、volume-family、"
                     "virtual-network-family（tenancy 级）。\n"
                     "将自动生成 RSA API Key，并把私钥作为文件发送给你。\n"
                     "确认后会在当前账号创建/复用这些 IAM 资源。",
                     reply_markup=confirm_keyboard("createbotuser", 0,
                                                   "🤖 确认创建 Bot 用户"))
        return
    if action == "add":
        context.user_data["pending_input"] = {"kind": "add_admin_name"}
        context.user_data.pop("add_admin", None)
        await _reply_prompt(
            update, context,
            "➕ 增加管理员账号：先输入新用户的用户名（字母/数字/下划线，"
            "不要带空格）：\n"
            "（直接发文字即可，不用加任何命令）")
        return
    if action == "keydel":
        await _user_key_delete_prompt(update, context, idx)
        return
    user = _resolve_iam_user(context, idx)
    if user is None:
        await _reply(update, context, "用户不存在或已过期，请重新打开云账户管理。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if action == "addkey":
        await _reply(update, context,
                     f"确认给用户「{user.get('name', '?')}」新增 OCI API Key？\n"
                     "会生成新的 RSA 私钥并上传公钥；旧密钥不会删除。",
                     reply_markup=confirm_keyboard("createapikey", idx,
                                                   "🔑 确认新增 API Key"))
        return
    if action == "uploadkey":
        context.user_data["pending_input"] = {"kind": "upload_key",
                                              "idx": idx}
        await _reply_prompt(
            update, context,
            f"📋 请回复这条消息，粘贴用户「{user.get('name', '?')}」的 RSA 公钥"
            "（PEM 格式，以 -----BEGIN PUBLIC KEY----- 开头）：\n"
            "（直接发文字即可，不用加任何命令；已有密钥对的公钥可直接粘贴，"
            "不会生成新私钥）")
        return
    if action == "resetpwd":
        await _reply(update, context,
                     f"确认重置用户「{user.get('name', '?')}」的 OCI 控制台密码？\n"
                     "旧密码/登录状态可能失效；OCI 会生成一次性密码，或向用户邮箱发送重置链接。",
                     reply_markup=confirm_keyboard("resetpwd", idx,
                                                   "🔐 确认重置密码"))
        return
    if action == "detail":
        await _reply(update, context, fmt_user_detail(user),
                     reply_markup=_user_detail_keyboard(idx))
    elif action == "desc":
        context.user_data["pending_input"] = {"kind": "user_desc",
                                              "idx": idx}
        await _reply_prompt(update, context,
                            f"✏️ 请回复这条消息，输入用户「{user.get('name', '?')}」"
                            f"的新描述（最多 {MAX_DESCRIPTION_LEN} 字符）：\n"
                            "（直接发文字即可，不用加任何命令）")
    elif action == "email":
        context.user_data["pending_input"] = {"kind": "user_email",
                                              "idx": idx}
        await _reply_prompt(update, context,
                            f"📧 请回复这条消息，输入用户「{user.get('name', '?')}」"
                            "的新邮箱：\n"
                            "（直接发文字即可，不用加任何命令）")
    elif action == "keys":
        await _user_keys_list(update, context, user, idx)
    elif action == "del":
        await _user_delete_prompt(update, context, user, idx)
    else:
        await _reply(update, context, "未知操作，请重新打开云账户管理。",
                     reply_markup=back_to_oracle_keyboard())


async def _user_keys_list(update, context, user, idx):
    """u:keys:<idx>：列出该用户的 API 密钥，每个密钥挂删除按钮。

    密钥列表存 user_data["iam_keys"]，所属用户存
    user_data["iam_keys_user"]（{id, name}），删除确认时取回。
    可从用户详情页新增 API Key；新密钥只返回指纹，私钥在创建时一次性交付。
    """
    name = user.get("name") or "?"
    try:
        keys = await run_sync(list_api_keys, _get_iam_cli(context), user["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc),
                     reply_markup=back_to_oracle_keyboard())
        return
    context.user_data["iam_keys"] = keys
    context.user_data["iam_keys_user"] = {
        "id": user.get("id"), "name": name,
        "profile": get_current_profile(context),
        "region": get_current_region(context),
    }
    if not keys:
        lines = [f"🔑 {name} 没有 API 密钥。"]
    else:
        lines = [f"🔑 {name} 的 API 密钥（{len(keys)} 个）：", ""]
        for key in keys:
            created = (key.get("time-created") or "")[:10]
            lines.append(f"• {key.get('fingerprint', '?')}"
                         f"（{key.get('lifecycle-state', '?')}"
                         f"{'，' + created if created else ''}）")
    lines.append("")
    lines.append("可点用户详情中的“新增 API Key”自动生成并上传新密钥。")
    rows = []
    for key_idx, key in enumerate(keys):
        fp = key.get("fingerprint") or "?"
        short = fp[:11] + "…" if len(fp) > 11 else fp
        rows.append([InlineKeyboardButton(
            f"🗑 删除密钥 {short}",
            callback_data=f"u:keydel:{key_idx}")])
    rows.append([InlineKeyboardButton("◀ 返回用户详情",
                                      callback_data=f"u:detail:{idx}")])
    rows.append([InlineKeyboardButton("◀ 返回云账户",
                                      callback_data="u:list")])
    await _reply(update, context, "\n".join(lines),
                 reply_markup=InlineKeyboardMarkup(rows))


async def _user_key_delete_prompt(update, context, idx):
    """u:keydel:<idx>：删 API 密钥先弹二次确认
    （确认按钮 c:delapikey:<idx>，由 handle_confirm 执行）。"""
    try:
        idx_int = int(idx)
    except (TypeError, ValueError):
        idx_int = -1
    keys = context.user_data.get("iam_keys") or []
    owner = context.user_data.get("iam_keys_user") or {}
    if (owner.get("profile") not in (None, get_current_profile(context))
            or owner.get("region") not in (None, get_current_region(context))):
        await _reply(update, context,
                     "API Key 列表属于其他 Oracle 账号/区域，请重新打开对应账号的用户详情。")
        return
    if idx_int < 0 or idx_int >= len(keys):
        await _reply(update, context, "密钥不存在或已过期，请重新打开 API 密钥列表。",
                     reply_markup=back_to_oracle_keyboard())
        return
    key = keys[idx_int]
    if _is_current_api_key(context, owner.get("id"),
                           key.get("fingerprint")):
        await _reply(update, context,
                     "❌ 不能删除当前 M_Bot 正在使用的 API Key；删除后机器人将无法再访问该 Oracle 账号。")
        return
    await _reply(update, context,
                 f"🗑️ 确认删除用户「{owner.get('name', '?')}」的 API 密钥？\n\n"
                 f"指纹：{key.get('fingerprint', '?')}\n"
                 "删除后用该密钥的 API 调用立即失效。",
                 reply_markup=confirm_keyboard("delapikey", idx_int,
                                               "🗑️ 确认删除"))


async def _user_delete_prompt(update, context, user, idx):
    """u:del:<idx>：删除用户先做安全检查，再要求输入用户名确认
    （像重装系统一样防误点），最后走 handle_confirm("deluser")。"""
    name = user.get("name") or "?"
    if _is_current_api_user(context, user.get("id")):
        await _reply(update, context,
                     f"❌ 不能删除用户「{name}」：它是机器人当前账号 "
                     f"（{get_cached_profile_display_name(get_current_profile(context))}）"
                     "正在使用的 API 用户，"
                     "删了机器人会立刻失联。请先换 profile 或换 API 密钥。",
                     reply_markup=back_to_oracle_keyboard())
        return
    try:
        last_admin = await run_sync(
            is_last_admin, _get_iam_cli(context),
            _get_iam_tenancy_id(context), user.get("id"))
    except Exception:  # noqa: BLE001 - 查不到管理员状态时不拦，放行到确认页再查
        last_admin = False
    if last_admin:
        await _reply(update, context,
                     f"❌ 不能删除用户「{name}」：它是租户里最后一个管理员 "
                     "（Administrators 组只剩它一人），删了整个租户就没人能管了。"
                     "请先把另一个用户加进 Administrators 组，再删除它。",
                     reply_markup=back_to_oracle_keyboard())
        return
    context.user_data["pending_input"] = {"kind": "deluser", "idx": idx}
    await _reply_prompt(update, context,
                        f"⚠️ 删除用户「{name}」将同时删除它的全部 API 密钥与 "
                        "MFA 设备，不可恢复。\n\n"
                        f"请回复这条消息，完整输入用户名「{name}」确认删除：")


@require_allowed
async def handle_emails(update, context):
    """菜单 30：列出全部用户的邮箱。"""
    try:
        users = await run_sync(list_users,
                               _get_iam_cli(context), _get_iam_tenancy_id(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    await _reply(update, context, fmt_email_list(users),
                 reply_markup=back_to_oracle_keyboard())


# ---- 12. 工作流错误 ----

@require_allowed
async def handle_workflows(update, context):
    """菜单 12：列最近的工作请求，FAILED 的标 ❌。"""
    try:
        requests = await run_sync(list_work_requests,
                                  get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    await _reply(update, context, fmt_work_requests(requests),
                 reply_markup=back_to_oracle_keyboard())


# ---- 33. 自动拓展子区域（列已订阅区域） ----

@require_allowed
async def handle_regions(update, context):
    """菜单 33：列出已订阅区域。

    注：“自动拓展子区域”是订阅新区域；订阅区域是租户级
    操作且不可逆，本版本只做只读列表，不提供一键订阅。
    """
    try:
        regions = await run_sync(list_regions,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    await _reply(update, context, fmt_regions(regions),
                 reply_markup=back_to_oracle_keyboard())


# ---- 22. 查看客户端负载 ----

@require_allowed
async def handle_load(update, context):
    """菜单 22：本机负载与内存（读 /proc，不调 OCI）。"""
    await _reply(update, context, fmt_local_load(read_local_load()),
                 reply_markup=back_to_oracle_keyboard())


# ---- 34. 查询最新日志 ----

@require_allowed
async def handle_logs(update, context):
    """菜单 34：读机器人日志尾部 30 行，代码块回复。"""
    tail = read_log_tail()
    if tail is None:
        await _reply(update, context,
                     f"日志文件不存在：{bot_config.LOG_FILE}\n"
                     "机器人还没写过日志。",
                     reply_markup=back_to_oracle_keyboard())
        return
    if not tail.strip():
        await _reply(update, context, "日志文件为空。",
                     reply_markup=back_to_oracle_keyboard())
        return
    text = tail[-LOG_MAX_CHARS:].replace("`", "'")
    await _reply(update, context, f"📋 最新日志（尾部 {LOG_TAIL_LINES} 行）：\n\n```\n{text}\n```",
                 reply_markup=back_to_oracle_keyboard(),
                 parse_mode="Markdown")


# ---- 17. 快捷开机 ----

@require_allowed
async def handle_presets(update, context, notice=None):
    """菜单 17：列出已保存的快捷开机配置，每条一行三个按钮。

    🚀 一键开机（进配置摘要确认页，确认才开始抢机）；✏️ 编辑
    （把配置填进开机表单，保存后原地更新）；🗑️ 删除（先确认再删）。
    抢机成功后自动保存本次配置（见 _run_grabber_thread）；菜单 1
    的表单也可点"保存为快捷开机"主动保存。
    notice 非空时作为首行提示（如删除成功）一起显示。
    """
    presets = load_presets()
    if not presets:
        text = ("还没有保存的快捷开机配置。\n"
                "抢机成功后会自动保存本次配置到这里，"
                "也可以在菜单 1 的表单里点“保存为快捷开机”。")
        if notice:
            text = notice + "\n\n" + text
        await _reply(update, context, text,
                     reply_markup=back_to_oracle_keyboard())
        return
    lines = []
    if notice:
        lines.extend([notice, ""])
    lines.extend([f"⚡ 快捷开机配置（{len(presets)} 条）：", ""])
    rows = []
    for idx, preset in enumerate(presets):
        shape = (preset.get("shape") or "?").replace("VM.Standard.", "")
        count = preset.get("count", 1) or 1
        count_text = f" ×{count}台" if count > 1 else ""
        lines.append(f"• {preset.get('display_name', '?')}：{shape} "
                     f"（{preset.get('ocpus')} 核 / "
                     f"{preset.get('memory_gb')}GB）{count_text}")
        rows.append([
            InlineKeyboardButton(
                f"🚀 {preset.get('display_name', '?')}",
                callback_data=f"b:load:{idx}"),
            InlineKeyboardButton("✏️", callback_data=f"b:edit:{idx}"),
            InlineKeyboardButton("🗑️", callback_data=f"b:del:{idx}"),
        ])
    rows.append([InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")])
    await _reply(update, context, "\n".join(lines),
                 reply_markup=InlineKeyboardMarkup(rows))


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
    await _reply(update, context,
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
    await _reply(update, context, text,
                 reply_markup=oracle_menu_keyboard(
                     0, states=get_menu_states(context)))


@require_allowed
async def handle_daily(update, context):
    """菜单 29：开关每日报告（每天发配额 + 费用摘要）。"""
    state = _daily_state(context)
    state["on"] = not state["on"]
    if update.effective_chat is not None:
        state["chat_id"] = update.effective_chat.id
    await _reply(update, context,
                 f"📰 每日报告已{'开启' if state['on'] else '关闭'}。"
                 + ("（每天发一次配额 + 费用摘要）" if state["on"] else ""))


# ---- 监控增强（阶段 5 工作包 C）：流量守护 / 停机保活 / SSL 监控 ----

def get_menu_states(context):
    """取 /oracle 菜单按钮的动态开关状态（handlers.py 生成菜单时调用）。

    返回 {"monitor", "autostart", "traffic_guard", "keepalive", "bark"}，
    值都是 bool。流量守护读文件配置；停机保活为 True 当且仅当
    至少一台实例开启了保活；Bark 为 True 当且仅当开关开启且配齐。
    """
    monitor = context.bot_data.get("monitor") or {}
    guard = load_traffic_guard()
    keepalive = load_keepalive()
    bark_cfg = load_bark_config()
    return {
        "monitor": bool(monitor.get("on")),
        "autostart": bool(monitor.get("autostart")),
        "traffic_guard": bool(guard.get("enabled")),
        "keepalive": bool(keepalive_enabled_instances(keepalive)),
        "bark": bool(bark_cfg.get("enabled")) and bark_configured(bark_cfg),
    }


def fmt_traffic_guard(cfg, used_gb=None, used_note=None):
    """流量守护菜单全文。

    used_gb 为 None 表示本月流量查不到（未配 SSH 密钥或实例全连不上）；
    used_note 是流量行的补充说明（如“2 台可达，1 台连不上”）。
    """
    lines = ["🛡️ 流量守护：", ""]
    lines.append(f"状态：{'✅ 开启' if cfg.get('enabled') else '⬜ 关闭'}")
    lines.append(f"阈值：{fmt_threshold_gb(cfg.get('threshold_gb'))} / 月")
    lines.append(f"超阈值动作：{traffic_guard_action_label(cfg.get('action'))}")
    if used_gb is not None:
        line = f"本月已用出站流量：{used_gb:.2f}GB"
        if used_note:
            line += f"（{used_note}）"
        lines.append(line)
    else:
        lines.append("本月已用出站流量："
                     + (used_note or "查询失败"))
    last = cfg.get("last_triggered")
    if last:
        lines.append(f"上次触发：{str(last)[:19].replace('T', ' ')}")
    lines.append("")
    lines.append("流量按 vnstat 自然月统计（SSH 到实例上查）；阈值默认 200GB，"
                "封顶 9TB；触发后 24 小时内不再重复执行。")
    lines.append("阻断断网：iptables 熔断，切断外网出站，SSH 逃生门保留；"
                "限速200K：tc 限流到 200KB/s，不断网。")
    return "\n".join(lines)


def _guard_host_for_instance(public_ip):
    """流量守护的 SSH 主机记录（实例公网 IP + 专用密钥）。

    未配置 MBOT_GUARD_SSH_KEY 或无公网 IP 时返回 None。
    """
    if not public_ip or not bot_config.GUARD_SSH_KEY:
        return None
    return {
        "host": public_ip,
        "port": bot_config.GUARD_SSH_PORT,
        "username": bot_config.GUARD_SSH_USER,
        "key_path": bot_config.GUARD_SSH_KEY,
    }


async def _run_guard_command_on_instances(command, instances):
    """在多台实例上各跑一条守护命令（SSH），返回 (成功名, 失败名+原因)。

    instances 是 list_instances 的精简列表，只对 RUNNING 且有公网 IP
    的实例执行；跳过 / 失败的都进失败列表并注明原因。命令的 stdout
    最后一行是 GUARD-* 状态行，失败时（非 0 退出码或无状态行）取
    stderr 末行作原因。
    """
    ok_names, failed = [], []
    for inst in instances or []:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        name = inst.get("display-name") or "?"
        host = _guard_host_for_instance(inst.get("public-ip"))
        if host is None:
            failed.append((name, "无公网 IP 或未配置 SSH 密钥"))
            continue
        try:
            exit_code, out, err = await run_sync(
                run_remote_command, host, command)
        except Exception as exc:  # noqa: BLE001 - 单台失败不影响其他台
            failed.append((name, fmt_error(exc)))
            continue
        last_line = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if exit_code == 0 and last_line.startswith("GUARD-"):
            ok_names.append(name)
        else:
            reason = (err or "").strip().splitlines()
            failed.append((name, reason[-1] if reason else
                           (f"退出码 {exit_code}" if exit_code else "无返回")))
    return ok_names, failed


async def _fetch_guard_monthly_gb(context, instances=None):
    """查 DEFAULT 账号下运行中实例的本月出站流量总和（vnstat）。

    返回 (出站总 GiB 或 None, 说明文字)。instances 不传时现查
    （并补公网 IP）；未配 SSH 密钥时返回 (None, "未配置 SSH 密钥")；
    部分实例连不上时只计可达的，并在说明里注明。
    """
    if not bot_config.GUARD_SSH_KEY:
        return None, "未配置 SSH 密钥，查不到流量"
    if instances is None:
        try:
            instances = await run_sync(
                list_instances, get_cli(context), get_compartment(context))
            instances = await run_sync(
                attach_public_ips, get_cli(context), instances)
        except Exception as exc:  # noqa: BLE001
            return None, f"查实例列表失败：{fmt_error(exc)}"
    elif any(inst.get("lifecycle-state") == "RUNNING"
             and "public-ip" not in inst for inst in instances or []):
        # job 传进来的列表没补公网 IP（check_instances_job 的原始列表），
        # 这里补上（原地修改，调用方后续动作也能用上）。
        try:
            instances = await run_sync(
                attach_public_ips, get_cli(context), instances)
        except Exception:  # noqa: BLE001 - 补 IP 失败时按原列表走
            pass
    total_tx = 0.0
    ok_count, fail_count = 0, 0
    for inst in instances or []:
        if inst.get("lifecycle-state") != "RUNNING":
            continue
        host = _guard_host_for_instance(inst.get("public-ip"))
        if host is None:
            fail_count += 1
            continue
        try:
            exit_code, out, _err = await run_sync(
                run_remote_command, host, guard_vnstat_command())
        except Exception:  # noqa: BLE001 - 单台失败不影响其他台
            fail_count += 1
            continue
        parsed = (parse_vnstat_monthly_gb(out)
                  if exit_code == 0 else None)
        if parsed is None:
            fail_count += 1
            continue
        total_tx += parsed[1]
        ok_count += 1
    if ok_count == 0:
        return None, "全部实例都连不上"
    note = f"{ok_count} 台可达" if fail_count == 0 else (
        f"{ok_count} 台可达，{fail_count} 台连不上")
    return total_tx, note


@require_allowed
async def handle_traffic_guard(update, context):
    """菜单 37：流量守护——开关、阈值、动作、解除限制。

    本月流量用 vnstat（SSH 到实例上查），查不到也不影响改配置。
    """
    cfg = load_traffic_guard()
    used_gb, used_note = await _fetch_guard_monthly_gb(context)
    await _reply(update, context, fmt_traffic_guard(cfg, used_gb, used_note),
                 reply_markup=traffic_guard_keyboard(cfg))


@require_allowed
async def traffic_guard_callback(update, context):
    """回调 "g:toggle" / "g:action" / "g:threshold:<gb>" / "g:unblock"。

    每次操作都记录通知 chat_id（job 触发时往这里发消息），
    保存后重新渲染菜单；解除限制先执行再汇报结果。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    cfg = load_traffic_guard()
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    if action == "toggle":
        cfg["enabled"] = not cfg.get("enabled")
    elif action == "action":
        cfg["action"] = next_traffic_guard_action(cfg.get("action"))
    elif action == "threshold" and len(parts) == 3:
        try:
            threshold = int(parts[2])
        except ValueError:
            await _reply(update, context, "阈值参数错误，请重新选择。")
            return
        if threshold <= 0:
            await _reply(update, context, "阈值必须大于 0，请重新选择。")
            return
        cfg["threshold_gb"] = threshold
    elif action == "unblock":
        save_traffic_guard(cfg)
        await _guard_unblock(update, context)
        return
    else:
        await _reply(update, context, "参数错误，请重新打开流量守护菜单。")
        return
    save_traffic_guard(cfg)
    await handle_traffic_guard(update, context)


async def _guard_unblock(update, context):
    """解除限制：在运行中实例上删阻断链与限速规则，汇报结果后重渲染菜单。"""
    try:
        instances = await run_sync(list_instances, get_cli(context),
                                   get_compartment(context))
        instances = await run_sync(
            attach_public_ips, get_cli(context), instances)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, f"❌ 查实例列表失败：{fmt_error(exc)}")
        return
    ok_names, failed = await _run_guard_command_on_instances(
        guard_unblock_command(), instances)
    lines = ["🔓 解除限制："]
    if ok_names:
        lines.append(f"已解除：{', '.join(ok_names)}")
    for name, reason in failed:
        lines.append(f"{name}：未解除（{reason}）")
    if not ok_names and not failed:
        lines.append("当前没有运行中的实例，无需解除。")
    await _reply(update, context, "\n".join(lines))
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
        await _reply(update, context, fmt_error(exc))
        return
    store_instances(context, instances)
    text, keyboard = _keepalive_menu_text_and_keyboard(context)
    await _reply(update, context, text, reply_markup=keyboard)


@require_allowed
async def keepalive_callback(update, context):
    """回调 "k:toggle:<idx>"：切换某台实例的停机保活开关。

    开 / 关都清掉该实例的失败计数与停试标记（重新开启即重新尝试），
    并记录通知 chat_id。
    """
    parts = _callback_data(update).split(":")
    if len(parts) != 3 or parts[1] != "toggle":
        await _reply(update, context, "参数错误，请重新打开停机保活菜单。")
        return
    inst = resolve_instance(context, parts[2])
    if inst is None:
        await _reply(update, context, "实例不存在或已过期，请重新打开停机保活菜单。")
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
    await _reply(update, context, text, reply_markup=keyboard)


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
    await _reply(update, context, fmt_ssl_watch(results),
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
        await _reply_prompt(update, context,
                            "请发送要监控的域名（例如 example.com）：\n"
                            "直接发文字消息即可。",
                            reply_markup=ssl_add_cancel_keyboard())
        return
    if action == "add_cancel":
        context.user_data.pop("pending_input", None)
        chat_id = update.effective_chat.id \
            if update.effective_chat else None
        if chat_id is not None:
            await _delete_tracked_prompts(context, chat_id)
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
    await _reply(update, context, "参数错误，请重新打开 SSL 监控菜单。")


async def _handle_ssl_domain_input(update, context, text):
    """text_input_callback 的 ssl_domain 分支：添加监控域名。

    域名格式校验 + 去重；添加成功后重新渲染 SSL 监控菜单
    （新域名的剩余天数在菜单里直接显示）。
    """
    domain = normalize_domain(text)
    if not is_valid_domain(domain):
        await _reply(update, context,
                     f"域名格式不正确：{text}\n"
                     "请重新点“➕ 添加域名”再试（例如 example.com）。",
                     reply_markup=back_to_oracle_keyboard())
        return
    cfg = load_ssl_watch()
    domains = cfg.get("domains") or []
    if domain in domains:
        await _reply(update, context, f"{domain} 已在 SSL 监控列表里。",
                     reply_markup=ssl_watch_keyboard(domains))
        return
    domains.append(domain)
    cfg["domains"] = domains
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    save_ssl_watch(cfg)
    await handle_sslwatch(update, context)


# ---- 43. Bark 通知设置 ----

def fmt_bark_settings(cfg):
    """Bark 通知设置页全文（地址脱敏显示，Key 只显示是否已设）。"""
    cfg = cfg or {}
    enabled = bool(cfg.get("enabled"))
    configured = bark_configured(cfg)
    lines = [
        "🔔 Bark 通知设置：",
        "",
        f"推送开关：{'✅ 已开启' if enabled else '❌ 已关闭'}",
        f"Bark 地址：{mask_bark_url(cfg.get('url') or '')}",
        f"加密 Key：{'✅ 已设置（32 位）' if len(cfg.get('key') or '') == 32 else '❌ 未设置'}",
        "",
    ]
    if not configured:
        lines.append("先设置 Bark 地址和 32 位加密 Key，再点“测试推送”"
                     "确认能收到，最后打开开关。没有 Key 可以点"
                     "“🎲 自动生成 Key”随机生成一个（生成后记得同步"
                     "到 Bark App 里）。")
        lines.append("开启后，开机成功、账单超支、付费实例被停等通知"
                     "会同时推到 Bark 和 Telegram。")
    elif not enabled:
        lines.append("配置已配齐，打开开关后开始推送。")
    else:
        lines.append("Bark 推送已开启：开机成功、账单超支等通知会"
                     "同时推到 Bark 和 Telegram。")
    return "\n".join(lines)


@require_allowed
async def handle_bark(update, context):
    """菜单 43：Bark 通知设置——地址 / 加密 Key / 测试推送 / 开关。"""
    cfg = load_bark_config()
    await _reply(update, context, fmt_bark_settings(cfg),
                 reply_markup=bark_keyboard(cfg.get("enabled"),
                                            bark_configured(cfg)))


@require_allowed
async def bark_command(update, context):
    """顶层命令 /bark：直达 Bark 通知设置（与菜单 43 同一页）。

    v1.11.0 新增：用户反馈 Bark 设置藏在菜单第 3 页找不到，
    加顶层命令直达；网页版也有对应的 Bark 设置页（/bark）。
    """
    await handle_bark(update, context)


@require_allowed
async def bark_callback(update, context):
    """回调 bark:url / bark:key / bark:test / bark:toggle /
    bark:cancel_input / bark:genkey[/go/cancel]。

    地址与 Key 走 text_input_callback（pending_input kind
    \"bark_url\" / \"bark_key\"）。测试推送不依赖开关。
    自动生成 Key 先出确认页（生成后旧 Key 立即失效），确定后
    生成 32 位随机 Key 并只显示一次（用户必须同步到 Bark App）。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "url":
        context.user_data["pending_input"] = {"kind": "bark_url"}
        await _reply_prompt(
            update, context,
            "请发送 Bark 推送地址（例如 https://api.day.app/你的设备Key/）：\n"
            "直接发文字消息即可。",
            reply_markup=bark_input_cancel_keyboard())
        return
    if action == "key":
        context.user_data["pending_input"] = {"kind": "bark_key"}
        await _reply_prompt(
            update, context,
            "请发送 Bark 加密 Key（32 位，与 Bark App 里加密设置中的 Key 一致）：\n"
            "直接发文字消息即可。",
            reply_markup=bark_input_cancel_keyboard())
        return
    if action == "genkey":
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "go":
            new_key = generate_bark_key()
            cfg = load_bark_config()
            cfg["key"] = new_key
            if update.effective_chat is not None:
                cfg["chat_id"] = update.effective_chat.id
            save_bark_config(cfg)
            chat_id = update.effective_chat.id if update.effective_chat else None
            qr_png = make_key_qr_png(new_key)
            if qr_png and chat_id is not None:
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id, photo=BytesIO(qr_png),
                        caption="Bark 加密 Key 二维码（用 Bark App 扫码添加）")
                except Exception:  # noqa: BLE001 - 二维码发不出时文字 Key 照样可用
                    pass
            await _reply(
                update, context,
                "🎲 新的 Bark 加密 Key 已生成并保存（32 位）：\n"
                f"{new_key}\n\n"
                "⚠️ 请把这个 Key 同步到 Bark App 的加密设置里：直接用 "
                "App 扫上面的二维码就能添加（也可以手动复制）。App 里的 "
                "Key 必须和它一致，否则推送解不开。机器人只显示这一次。\n"
                "另外：旧 Key 已失效，账单脚本、各主机流量脚本、邮件转发器"
                "还在用旧 Key，需要同步换掉（告诉我一声，我来同步）。",
                reply_markup=bark_keyboard(cfg.get("enabled"),
                                           bark_configured(cfg)))
            return
        if sub == "cancel":
            await handle_bark(update, context)
            return
        await _reply(
            update, context,
            "🎲 确定生成新的 Bark 加密 Key 吗？\n\n"
            "生成后旧 Key 立即失效：Bark App 里要同步改成新 Key，"
            "否则推送解不开；账单脚本、各主机流量脚本、邮件转发器"
            "还在用旧 Key，也要同步更新。",
            reply_markup=bark_genkey_confirm_keyboard())
        return
    if action == "cancel_input":
        context.user_data.pop("pending_input", None)
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            await _delete_tracked_prompts(context, chat_id)
        await handle_bark(update, context)
        return
    if action == "test":
        ok, err = await run_sync(send_bark_test)
        if ok:
            await _reply(update, context, "✅ 测试推送已发出，请查看手机 Bark 是否收到。",
                         reply_markup=bark_keyboard(
                             load_bark_config().get("enabled"),
                             bark_configured()))
        else:
            await _reply(update, context, f"❌ 测试推送失败：{err}",
                         reply_markup=bark_keyboard(
                             load_bark_config().get("enabled"),
                             bark_configured()))
        return
    if action == "toggle":
        cfg = load_bark_config()
        if not cfg.get("enabled") and not bark_configured(cfg):
            await _reply(update, context,
                         "❌ 还没配齐：请先设置 Bark 地址和 32 位加密 Key。",
                         reply_markup=bark_keyboard(False, False))
            return
        cfg["enabled"] = not cfg.get("enabled")
        if update.effective_chat is not None:
            cfg["chat_id"] = update.effective_chat.id
        save_bark_config(cfg)
        await handle_bark(update, context)
        return


async def _handle_bark_url_input(update, context, text):
    """text_input_callback 的 bark_url 分支：校验后保存 Bark 地址。"""
    if not (text.startswith("http://") or text.startswith("https://")):
        await _reply(update, context,
                     "地址格式不正确：请以 http:// 或 https:// 开头。\n"
                     "请重新点“🔗 设置 Bark 地址”再试。",
                     reply_markup=back_to_oracle_keyboard())
        return
    cfg = load_bark_config()
    cfg["url"] = text
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    save_bark_config(cfg)
    await _reply(update, context,
                 f"✅ Bark 地址已保存：{mask_bark_url(text)}",
                 reply_markup=bark_keyboard(cfg.get("enabled"),
                                            bark_configured(cfg)))


async def _handle_bark_key_input(update, context, text):
    """text_input_callback 的 bark_key 分支：校验 32 位后保存 Key。

    Key 值绝不回显（只在消息里确认已保存）。
    """
    if len(text) != 32:
        await _reply(update, context,
                     f"加密 Key 必须是 32 位（你发的是 {len(text)} 位）。\n"
                     "请重新点“🔑 设置加密 Key”再试。",
                     reply_markup=back_to_oracle_keyboard())
        return
    cfg = load_bark_config()
    cfg["key"] = text
    if update.effective_chat is not None:
        cfg["chat_id"] = update.effective_chat.id
    save_bark_config(cfg)
    await _reply(update, context,
                 "✅ 加密 Key 已保存（32 位）。",
                 reply_markup=bark_keyboard(cfg.get("enabled"),
                                            bark_configured(cfg)))


# ---- 16. GFW 自动换 IP ----

def _gfw_last_check_line(cfg, instance_id):
    """某实例最近一次检查的一行摘要（无记录时返回空串）。"""
    entry = (cfg.get("last_check") or {}).get(instance_id)
    if not entry:
        return ""
    status = entry.get("status") or ""
    mark = {"healthy": "✅ 正常", "recovered": "🔁 已换 IP 恢复",
            "exhausted": "⚠️ 换满仍被墙", "unknown": "❓ 检测无结论",
            "error": "❌ 换 IP 失败", "skipped": "⏭️ 跳过",
            }.get(status, status)
    detail = entry.get("detail") or ""
    at = (entry.get("at") or "")[:16].replace("T", " ")
    return f"  上次检查：{mark}（{at} UTC）{detail}"


def fmt_gfw_status(cfg, instances):
    """GFW 自动换 IP 设置页全文（Key 脱敏显示，绝不回显原文）。"""
    cfg = cfg or {}
    enabled = cfg.get("enabled") or {}
    configured = is_gfw_configured(cfg)
    lines = [
        "🛡️ GFW 自动换 IP：",
        "",
        f"RIPE Atlas API Key："
        f"{'✅ 已设置（' + mask_api_key(cfg.get('api_key')) + '）' if configured else '❌ 未设置'}",
        f"已开启监控：{len(enabled)} 台，每 {GFW_CHECK_INTERVAL_MINUTES} 分钟检查一次",
        "",
    ]
    if not configured:
        lines.append("先设置 API Key 才能检测：在 atlas.ripe.net 注册账号，"
                     "领 5 万免费积分，在 Keys 页面创建一个"
                     "“create a new user-defined measurement”类型的 Key，"
                     "发给机器人（Key 只存本机，不回显）。")
        lines.append("检测从中国大陆探针 ping 实例公网 IP，多数探针不通"
                     "就判定被墙，自动换 IP 再检测，直到换到可用的为止；"
                     "每次换 IP 都发通知（TG + Bark，没设 Bark 就只发 TG）。")
    elif not instances:
        lines.append("当前账号/区域没有运行中的实例。")
    else:
        lines.append("点实例开关监控；开启后定时检查，被墙自动换 IP 并通知。")
        for inst in instances:
            line = _gfw_last_check_line(cfg, inst.get("id"))
            if line:
                lines.append(line)
    return "\n".join(lines)


@require_allowed
async def handle_autoip(update, context):
    """菜单 16：GFW 自动换 IP——Atlas Key 设置 + 逐台开关监控。"""
    try:
        instances = await run_sync(
            list_instances, get_cli(context), get_compartment(context))
        instances = await run_sync(
            attach_public_ips, get_cli(context), instances)
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    running = [i for i in (instances or [])
               if i.get("lifecycle-state") == "RUNNING"
               and i.get("public-ip")]
    context.user_data["gfw_instances"] = running
    cfg = load_gfw_config()
    await _reply(update, context, fmt_gfw_status(cfg, running),
                 reply_markup=gfw_keyboard(
                     running, set((cfg.get("enabled") or {}).keys()),
                     is_gfw_configured(cfg)))


def _notify_chat_id(context):
    """通知用的 chat_id（与账单预警同一优先级：Bark 配置里记的 >
    监控/每日报告里记的 > 白名单第一个用户 ID）。"""
    chat_id = (load_bark_config().get("chat_id")
               or (context.bot_data.get("monitor") or {}).get("chat_id")
               or (context.bot_data.get("daily_report") or {}).get("chat_id"))
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    return chat_id


def _gfw_check_thread(token, chat_id):
    """手动“立即检查”的后台线程体：跑一轮监控，完事后发汇总通知。

    检测本身慢（每台约 1-2 分钟），不能在回调里等；结果走
    notify_dual 发（换 IP 的过程通知由循环里直接发，这里只发汇总）。
    """
    results = run_monitor_cycle(
        lambda title, body: notify_dual(token, chat_id, title, body,
                                        group="gfw-check"))
    if not results:
        notify_dual(token, chat_id, "🛡️ GFW 检查",
                    "没有可检查的实例（未设置 API Key 或未开启任何实例）。",
                    group="gfw-check")
        return
    lines = ["🛡️ GFW 检查完成：", ""]
    for res in results:
        name = (res.get("name") or res.get("instance_id") or "?")
        status = res.get("status")
        if status == "healthy":
            lines.append(f"✅ {name}：正常（{res.get('detail')}）")
        elif status == "recovered":
            lines.append(f"🔁 {name}：换了 {res.get('changes')} 次 IP 已恢复，"
                         f"现 IP {res.get('ip')}")
        elif status == "exhausted":
            lines.append(f"⚠️ {name}：换了 {res.get('changes')} 次仍被墙，"
                         f"已暂停，下个周期继续")
        elif status == "unknown":
            lines.append(f"❓ {name}：检测无结论（{res.get('detail')}）")
        elif status == "error":
            lines.append(f"❌ {name}：{res.get('detail')}")
        else:
            lines.append(f"⏭️ {name}：{res.get('detail')}")
    notify_dual(token, chat_id, "🛡️ GFW 检查完成", "\n".join(lines),
                group="gfw-check")


@require_allowed
async def gfw_callback(update, context):
    """回调 gfw:toggle:<idx> / gfw:check / gfw:key / gfw:cancel_input。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "key":
        context.user_data["pending_input"] = {"kind": "gfw_key"}
        await _reply_prompt(
            update, context,
            "请发送 RIPE Atlas API Key（在 atlas.ripe.net 注册账号后，"
            "领 5 万免费积分，在 Keys 页面创建"
            "“create a new user-defined measurement”类型的 Key）：\n"
            "直接发文字消息即可（Key 只存本机，不回显）。",
            reply_markup=gfw_input_cancel_keyboard())
        return
    if action == "cancel_input":
        context.user_data.pop("pending_input", None)
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            await _delete_tracked_prompts(context, chat_id)
        await handle_autoip(update, context)
        return
    if action == "check":
        if not is_gfw_configured():
            await _reply(update, context, "❌ 还没设置 RIPE Atlas API Key，"
                         "请先点“🔑 设置 API Key”。")
            return
        cfg = load_gfw_config()
        if not cfg.get("enabled"):
            await _reply(update, context, "❌ 还没开启任何实例的监控，"
                         "请先点实例打开开关。")
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        await _reply(update, context, "🔍 已开始检查（每台约 1-2 分钟），"
                     "完成后发通知告诉你。")
        if chat_id is not None:
            thread = threading.Thread(
                target=_gfw_check_thread,
                args=(bot_config.TELEGRAM_BOT_TOKEN, chat_id),
                daemon=True)
            thread.start()
        return
    if action == "toggle":
        try:
            idx = int(parts[2]) if len(parts) > 2 else -1
        except ValueError:
            idx = -1
        instances = context.user_data.get("gfw_instances") or []
        if idx < 0 or idx >= len(instances):
            await _reply(update, context, "实例列表已过期，请重新打开菜单 16。")
            return
        inst = instances[idx]
        inst_id = inst.get("id")
        cfg = load_gfw_config()
        enabled = bool((cfg.get("enabled") or {}).get(inst_id))
        set_instance_enabled(
            inst_id, not enabled,
            profile=get_current_profile(context),
            region=get_current_region(context),
            name=inst.get("display-name"),
            ip=inst.get("public-ip"))
        await handle_autoip(update, context)
        return


async def _handle_gfw_key_input(update, context, text):
    """text_input_callback 的 gfw_key 分支：校验后保存 Atlas Key。

    Key 值绝不回显（只显示脱敏后的首尾）。Atlas Key 是 UUID 格式
    （36 位含横杠），这里只做宽松校验：去空白后长度至少 8 位且
    不含空白字符；真正的有效性由第一次检测验证（失败会记进
    last_check，不换 IP）。
    """
    key = text.strip()
    if len(key) < 8 or any(ch.isspace() for ch in key):
        await _reply(update, context,
                     "API Key 格式不正确（太短或含有空白字符）。\n"
                     "请重新点“🔑 设置 API Key”再试。",
                     reply_markup=back_to_oracle_keyboard())
        return
    masked = set_api_key(key)
    await _reply(update, context,
                 f"✅ API Key 已保存（{masked}）。\n"
                 "点实例打开监控开关，每 30 分钟自动检查一次；"
                 "也可以点“🔍 立即检查”马上测一次。",
                 reply_markup=back_to_oracle_keyboard())


async def gfw_check_job(context):
    """每 30 分钟跑一次：对已开启的实例做 GFW 检测，被墙自动换 IP。

    没配 Key 或没开启任何实例时静默跳过；本来就正常的实例不发
    通知（只记 last_check），换 IP / 换满暂停 / 换 IP 失败时才由
    循环里发双通道通知（TG + Bark，没设 Bark 就只发 TG）。
    """
    cfg = load_gfw_config()
    if not is_gfw_configured(cfg) or not cfg.get("enabled"):
        return
    chat_id = _notify_chat_id(context)
    if not chat_id:
        logger.warning("GFW 检查有开启的实例但未找到通知 chat_id，跳过")
        return
    await run_sync(
        run_monitor_cycle,
        lambda title, body: notify_dual(
            bot_config.TELEGRAM_BOT_TOKEN, chat_id, title, body,
            group="gfw-check"))


# ---- 26. 清除所有 2FA 设备 ----

@require_allowed
async def handle_mfa(update, context):
    """菜单 26：列出全部用户的 MFA TOTP 设备 + 双重确认删除。"""
    try:
        devices = await run_sync(list_mfa_devices,
                                 get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(exc))
        return
    if not devices:
        await _reply(update, context, "没有任何 MFA TOTP 设备，无需清除。",
                     reply_markup=back_to_oracle_keyboard())
        return
    await _reply(
        update, context,
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
        await _reply(update, context, fmt_error(exc))
        return
    if not instances:
        await _reply(update, context, "该账号下没有实例，无需批量操作。",
                     reply_markup=back_to_oracle_keyboard())
        return
    store_instances(context, instances)
    context.user_data["batch_selected"] = set()
    await _reply(update, context, _batch_select_text(instances, set()),
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
        await _reply(update, context, "实例不存在或已过期，请重新打开批量操作。")
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
            await menu_tracker.safe_edit(query, text, reply_markup=keyboard)
            return
        except Exception:  # noqa: BLE001 - 编辑失败时改发新消息
            pass
    await _reply(update, context, text, reply_markup=keyboard)


@require_allowed
async def batch_next_callback(update, context):
    """回调 "ba:next"：有选中时显示动作选择键盘。"""
    selected = context.user_data.get("batch_selected") or set()
    if not selected:
        await _reply(update, context, "请先选至少一台实例（点实例名切换选中）。")
        return
    names = []
    for idx in sorted(selected):
        inst = resolve_instance(context, idx)
        if inst is not None:
            names.append(inst.get("display-name") or "?")
    if not names:
        await _reply(update, context, "选中的实例已过期，请重新打开批量操作。")
        return
    await _reply(update, context,
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
        await _reply(update, context, "未知的批量动作，请重新选择。")
        return
    selected = context.user_data.get("batch_selected") or set()
    instances = [resolve_instance(context, idx) for idx in sorted(selected)]
    instances = [inst for inst in instances if inst is not None]
    if not instances:
        await _reply(update, context, "选中的实例已过期，请重新打开批量操作。")
        return
    context.user_data["pending_batch_action"] = action
    # Instance names are user-chosen and may contain <>&: with parse_mode="HTML" they must be escaped (same as the single-terminate confirm; quote=False escapes only &<>). 实例名是用户起的，可能含 <>&：parse_mode="HTML" 时必须转义（与单个终止确认同理，quote=False 只转义 &<>）。
    names = "\n".join(f"• {html.escape(inst.get('display-name') or '?', quote=False)}"
                      for inst in instances)
    verb = _BATCH_ACTION_TEXT[action]
    if action == "terminate":
        await _reply(
            update, context,
            f"⚠️ 确认批量删除以下 {len(instances)} 台实例？\n\n{names}\n\n"
            "<b>删除后将永久删除各实例的引导卷，数据无法恢复！</b>",
            parse_mode="HTML",
            reply_markup=confirm_keyboard("batchterminate", 0,
                                          "🗑️ 确认批量删除"))
        return
    await _reply(update, context,
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
        await _reply(update, context, bot_i18n.tr(
            update, context, "rename.pick_first"),
            reply_markup=back_to_oracle_keyboard(
                lang=lang_of(update, context)))
        return
    name = inst.get("display-name") or "?"
    context.user_data["pending_input"] = {"kind": "rename", "idx": idx}
    await _reply_prompt(update, context, bot_i18n.tr(
        update, context, "rename.prompt", name=name))


@require_allowed
async def handle_reinstall_prompt(update, context):
    """实例操作“重装系统”的确认提示（dispatch 项 "reinstall"）。

    由 handlers.py 的 "i:reinstall:<idx>" 回调转发。用户需回复
    完整实例名（防误点），再弹二次确认，走 handle_confirm("reinstall")。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, bot_i18n.tr(
            update, context, "reinstall.pick_first"),
            reply_markup=back_to_oracle_keyboard(
                lang=lang_of(update, context)))
        return
    name = inst.get("display-name") or "?"
    context.user_data["pending_input"] = {"kind": "reinstall", "idx": idx}
    await _reply_prompt(update, context, bot_i18n.tr(
        update, context, "reinstall.prompt", name=name))


@require_allowed
async def handle_console(update, context):
    """实例操作“串行控制台”（dispatch 项 "console"）。

    由 handlers.py 的 "i:console:<idx>" 回调转发。创建串行控制台
    连接并返回 ssh 连接命令（只读展示，无需二次确认）。
    """
    idx = context.user_data.get("pending_idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, bot_i18n.tr(
            update, context, "console.pick_first"),
            reply_markup=back_to_oracle_keyboard(
                lang=lang_of(update, context)))
        return
    name = inst.get("display-name") or "?"
    try:
        console = await run_sync(get_serial_console, get_cli(context),
                                 inst["id"])
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, fmt_error(
            exc, lang=lang_of(update, context)))
        return
    await _reply(update, context, fmt_serial_console(
        name, console, lang=lang_of(update, context)),
        reply_markup=back_to_oracle_keyboard(
            lang=lang_of(update, context)))


def fmt_serial_console(name, console, lang="zh"):
    """串行控制台连接信息全文（连接命令单独成行，方便复制）。"""
    lines = [bot_i18n.t(lang, "console.title", name=name), ""]
    lines.append(bot_i18n.t(lang, "console.command"))
    lines.append(console.get("connection-string")
                 or bot_i18n.t(lang, "console.no_command"))
    state = console.get("lifecycle-state")
    if state:
        lines.append(f"{bot_i18n.t(lang, 'console.state')}{state}")
    fingerprint = console.get("fingerprint")
    if fingerprint:
        lines.append(f"{bot_i18n.t(lang, 'console.fingerprint')}{fingerprint}")
    lines.append("")
    lines.append(bot_i18n.t(lang, "console.hint"))
    return "\n".join(lines)


@require_allowed
async def text_input_callback(update, context):
    """处理用户回复的纯文字输入（改名 / 重装确认 / SSL 添加域名 /
    Email 发件人 / 用户改描述 / 用户改邮箱 / 用户删除确认 /
    从备份恢复的新实例名 / 开机表单自定义值 / 开机脚本粘贴）。

    只在 user_data["pending_input"] 存在时处理；否则忽略
    （机器人平时不处理自由文字）。改名：校验后弹二次确认；
    重装：输入必须与实例名完全一致，否则取消；
    SSL 域名：校验格式后加入监控列表；
    Email 发件人：校验邮箱格式后弹二次确认；
    用户改描述 / 改邮箱：校验后弹二次确认；
    用户删除：输入必须与用户名完全一致，否则取消。
    开机表单自定义值：校验后写回表单并重发表单消息；
    开机脚本：粘贴的文本存进表单（文件上传走
    boot_form_document_callback）。
    """
    pending = context.user_data.get("pending_input")
    if not pending:
        return
    # 绑定域名流程优先：bind_domain_pending 已选 Zone 时，这条文字归
    # 绑定域名处理（group=1），这里不消费，避免双重处理。
    bd_pending = context.user_data.get("bind_domain_pending") or {}
    if bd_pending.get("zone_id"):
        return
    text = (update.effective_message.text or "").strip()
    kind = pending.get("kind")
    if kind and kind.startswith("boot_"):
        context.user_data.pop("pending_input", None)
        await _handle_boot_form_input(update, context, kind, text)
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        await _delete_tracked_prompts(context, chat_id)
    if kind == "ssl_domain":
        context.user_data.pop("pending_input", None)
        await _handle_ssl_domain_input(update, context, text)
        return
    if kind == "bark_url":
        context.user_data.pop("pending_input", None)
        await _handle_bark_url_input(update, context, text)
        return
    if kind == "bark_key":
        context.user_data.pop("pending_input", None)
        await _handle_bark_key_input(update, context, text)
        return
    if kind == "gfw_key":
        context.user_data.pop("pending_input", None)
        await _handle_gfw_key_input(update, context, text)
        return
    if kind == "email_sender":
        context.user_data.pop("pending_input", None)
        if not is_valid_email(text):
            await _reply(update, context,
                         "❌ 邮箱格式不对，请重新从菜单进入并发送正确的"
                         "邮箱地址（例如 noreply@example.com）。")
            return
        context.user_data["pending_sender"] = text
        await _reply(update, context,
                     f"📧 确认创建发件人 {text}？\n"
                     "创建后 OCI 会向该邮箱发送验证邮件，"
                     "需点击链接后才能使用。",
                     reply_markup=confirm_keyboard(
                         "createsender", 0, "📧 确认创建发件人"))
        return
    if kind in ("user_desc", "user_email", "deluser"):
        # Cloud account management: user-related input (not tied to an instance; handled directly). 云账户管理：用户相关输入（不关联实例，直接处理）。
        context.user_data.pop("pending_input", None)
        user = _resolve_iam_user(context, pending.get("idx"))
        if user is None:
            await _reply(update, context, "用户不存在或已过期，请重新打开云账户管理。")
            return
        name = user.get("name") or "?"
        if kind == "user_desc":
            if not text:
                await _reply(update, context, "新描述不能为空，改描述已取消。")
                return
            if len(text) > MAX_DESCRIPTION_LEN:
                await _reply(update, context,
                             f"新描述不能超过 {MAX_DESCRIPTION_LEN} 字符，"
                             "改描述已取消。")
                return
            context.user_data["pending_user_desc"] = text
            await _reply(update, context,
                         f"确认把用户「{name}」的描述改为「{text}」？",
                         reply_markup=confirm_keyboard(
                             "userdesc", pending.get("idx"), "✅ 确认修改"))
            return
        if kind == "user_email":
            if not is_valid_email(text):
                await _reply(update, context,
                             "❌ 邮箱格式不对，改邮箱已取消。"
                             "请重新点“改邮箱”并发送正确的邮箱地址。")
                return
            context.user_data["pending_user_email"] = text
            await _reply(update, context,
                         f"确认把用户「{name}」的邮箱改为 {text}？",
                         reply_markup=confirm_keyboard(
                             "useremail", pending.get("idx"), "✅ 确认修改"))
            return
        if kind == "upload_key":
            # 粘贴公钥添加 API Key：直接上传已有 RSA 公钥，不生成新私钥。
            pem = (text or "").strip()
            if not pem.startswith("-----BEGIN PUBLIC KEY-----"):
                await _reply(update, context,
                             "❌ 公钥格式不对：必须是 PEM 格式的 RSA 公钥，"
                             "以 -----BEGIN PUBLIC KEY----- 开头。添加已取消，"
                             "请重新点“粘贴公钥添加”并发送正确的公钥。")
                return
            try:
                key = await run_sync(upload_api_key, _get_iam_cli(context),
                                     user.get("id"), pem)
            except Exception as exc:  # noqa: BLE001
                await _reply(update, context, fmt_error(exc))
                return
            await _reply(update, context,
                         f"✅ 已为用户「{name}」添加 API Key（已有公钥）。\n"
                         f"Fingerprint：`{key.get('fingerprint', '?')}`\n"
                         f"API Key 状态：{key.get('lifecycle_state', 'CREATING')}")
            return
        # deluser: the input must match the username exactly, otherwise cancel. deluser：输入必须与用户名完全一致，否则取消。
        if text != name:
            await _reply(update, context,
                         "输入的用户名与实际不符，删除已取消。\n"
                         "请重新点“删除用户”再试。")
            return
        await _reply(update, context,
                     f"⚠️ 最后确认：删除用户「{name}」？\n"
                     "它的全部 API 密钥与 MFA 设备将一并删除，不可恢复。",
                     reply_markup=confirm_keyboard(
                         "deluser", pending.get("idx"), "🗑️ 确认删除"))
        return
    if kind in ("cf_token", "cf_key", "cf_email"):
        # Manual Cloudflare credential add: Token in one step, Key + email in two steps. Cloudflare 凭据手动添加：Token 一步，Key + 邮箱两步。
        # Credential values are never echoed back; the user's message is deleted as far as possible so no plain text remains. 凭据值绝不回显；用户那条消息尽力删掉，不留明文。
        context.user_data.pop("pending_input", None)
        await _delete_secret_message(update, context)
        if kind == "cf_token":
            if not text or any(ch.isspace() for ch in text):
                await _reply(update, context,
                             "❌ Token 不能为空也不能带空格，已取消。"
                             "请重新点“手动添加凭据”。")
                return
            ok, zones_total, _err = await run_sync(validate_and_save,
                                                   text, "", "")
            if not ok:
                await _reply(
                    update, context,
                    "❌ Token 校验没通过（无效或权限不足），没有保存。\n"
                    "请检查 Token 是否正确、是否有 Zone:Read 与 DNS:Edit "
                    "权限，再重新添加。",
                    reply_markup=cf_config_keyboard())
                return
            await _reply(
                update, context,
                f"✅ Cloudflare API Token 已保存并校验通过"
                f"（{zones_total} 个 Zone）。",
                reply_markup=cf_manage_keyboard())
            return
        if kind == "cf_key":
            if not text or any(ch.isspace() for ch in text):
                await _reply(update, context,
                             "❌ Key 不能为空也不能带空格，已取消。"
                             "请重新点“手动添加凭据”。")
                return
            context.user_data["cf_draft"] = {"api_key": text}
            context.user_data["pending_input"] = {"kind": "cf_email"}
            await _reply_prompt(
                update, context,
                "Key 已记下。再输入该 Key 对应的账号邮箱：\n"
                "（直接发文字即可，不用加任何命令）")
            return
        if not is_valid_email(text):
            context.user_data.pop("cf_draft", None)
            await _reply(update, context,
                         "❌ 邮箱格式不对，已取消。"
                         "请重新点“手动添加凭据”。")
            return
        draft = context.user_data.pop("cf_draft", None) or {}
        api_key = draft.get("api_key") or ""
        if not api_key:
            await _reply(update, context, "会话已过期，请重新点“手动添加凭据”。")
            return
        ok, zones_total, _err = await run_sync(validate_and_save, "",
                                               api_key, text)
        if not ok:
            await _reply(
                update, context,
                "❌ Key 或邮箱校验没通过（无效或不匹配），没有保存。\n"
                "请检查后重新添加。",
                reply_markup=cf_config_keyboard())
            return
        masked = mask_cf_email(text) if mask_cf_email is not None else "***"
        await _reply(
            update, context,
            f"✅ Cloudflare 凭据已保存并校验通过"
            f"（账号邮箱 {masked}，{zones_total} 个 Zone）。",
            reply_markup=cf_manage_keyboard())
        return
    if kind in ("dns_add_name", "dns_add_content"):
        context.user_data.pop("pending_input", None)
        if kind == "dns_add_name":
            await _handle_dns_add_name_input(update, context, text)
        else:
            await _handle_dns_add_content_input(update, context, text)
        return
    if kind in ("add_user_name", "add_user_email"):
        context.user_data.pop("pending_input", None)
        if kind == "add_user_name":
            if not text or not re.fullmatch(r"[A-Za-z0-9._+@-]+", text):
                await _reply(update, context,
                             "❌ 用户名只能包含字母、数字、点、下划线、加号、@ 或连字符，已取消。")
                return
            context.user_data["add_iam_user"] = {"name": text}
            context.user_data["pending_input"] = {"kind": "add_user_email"}
            await _reply_prompt(update, context,
                                f"用户名「{text}」已记录，请输入邮箱：")
            return
        if not is_valid_email(text):
            context.user_data.pop("add_iam_user", None)
            await _reply(update, context, "❌ 邮箱格式不正确，创建用户已取消。")
            return
        draft = context.user_data.get("add_iam_user") or {}
        draft["email"] = text
        context.user_data["add_iam_user"] = draft
        await _reply(update, context,
                     f"确认在当前 Oracle 账号创建普通 IAM 用户？\n"
                     f"用户名：{draft.get('name')}\n邮箱：{text}\n"
                     "不会自动加入 Administrators 组。",
                     reply_markup=confirm_keyboard("addiamuser", 0,
                                                   "➕ 确认创建普通用户"))
        return
    if kind in ("add_admin_name", "add_admin_email"):
        context.user_data.pop("pending_input", None)
        if kind == "add_admin_name":
            if not text or any(ch.isspace() for ch in text):
                await _reply(update, context,
                             "❌ 用户名不能为空也不能带空格，增加管理员"
                             "账号已取消。请重新点“增加管理员账号”。")
                return
            context.user_data["add_admin"] = {"name": text}
            context.user_data["pending_input"] = {
                "kind": "add_admin_email"}
            await _reply_prompt(
                update, context,
                f"用户名「{text}」已记下。再输入新用户的邮箱：\n"
                "（直接发文字即可，不用加任何命令）")
            return
        if not is_valid_email(text):
            await _reply(update, context,
                         "❌ 邮箱格式不对，增加管理员账号已取消。"
                         "请重新点“增加管理员账号”。")
            return
        admin = context.user_data.get("add_admin") or {}
        admin["email"] = text
        context.user_data["add_admin"] = admin
        name = admin.get("name") or "?"
        await _reply(update, context,
                     f"确认增加管理员账号？\n"
                     f"用户名：{name}\n"
                     f"邮箱：{text}\n"
                     "创建后会把该用户加进 Administrators 组（租户管理员）；"
                     "控制台密码需另行设置（控制台或临时密码命令）。",
                     reply_markup=confirm_keyboard(
                         "addadmin", 0, "➕ 确认增加管理员账号"))
        return
    if kind == "restore_name":
        # Restore from backup: input a new instance name (send "默认" to use "<backup-name>-restored"). 从备份恢复：输入新实例名（发“默认”用“<备份名>-restored”）。
        context.user_data.pop("pending_input", None)
        backup = resolve_boot_backup(context, pending.get("idx"))
        if backup is None:
            await _reply(update, context, "备份不存在或已过期，请重新打开启动卷备份列表。")
            return
        backup_name = backup.get("display-name") or "backup"
        if text in ("默认", "default"):
            new_name = f"{backup_name}-restored"
        else:
            if not text:
                await _reply(update, context, "新实例名不能为空，恢复已取消。")
                return
            if len(text) > MAX_DISPLAY_NAME_LEN:
                await _reply(update, context,
                             f"新实例名不能超过 {MAX_DISPLAY_NAME_LEN} 字符，"
                             "恢复已取消。")
                return
            new_name = text
        context.user_data["pending_restore_name"] = new_name
        await _reply(update, context,
                     f"确认从备份「{backup_name}」恢复出新实例"
                     f"「{new_name}」？\n"
                     "原机不动，恢复会占用免费实例与启动盘额度。",
                     reply_markup=confirm_keyboard(
                         "restorebb", pending.get("idx"), "✅ 确认恢复"))
        return
    idx = pending.get("idx")
    inst = resolve_instance(context, idx)
    if inst is None:
        context.user_data.pop("pending_input", None)
        await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
        return
    name = inst.get("display-name") or "?"
    if kind == "rename":
        context.user_data.pop("pending_input", None)
        if not text:
            await _reply(update, context, bot_i18n.tr(
                update, context, "rename.empty"))
            return
        if len(text) > MAX_DISPLAY_NAME_LEN:
            await _reply(update, context, bot_i18n.tr(
                update, context, "rename.too_long",
                max=MAX_DISPLAY_NAME_LEN))
            return
        context.user_data["pending_rename_name"] = text
        await _reply(update, context, bot_i18n.tr(
            update, context, "rename.confirm", name=name, new_name=text),
            reply_markup=confirm_keyboard(
                "rename", idx,
                bot_i18n.tr(update, context, "rename.confirm_button"),
                lang=lang_of(update, context)))
        return
    if kind == "reinstall":
        context.user_data.pop("pending_input", None)
        if text != name:
            await _reply(update, context, bot_i18n.tr(
                update, context, "reinstall.mismatch"))
            return
        await _reply(update, context, bot_i18n.tr(
            update, context, "reinstall.final_confirm", name=name),
            reply_markup=confirm_keyboard(
                "reinstall", idx,
                bot_i18n.tr(update, context, "reinstall.confirm_button"),
                lang=lang_of(update, context)))
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
        await _reply(update, context, fmt_error(exc))
        return
    await _reply(update, context, fmt_a1_health(
        result, get_cached_profile_display_name(get_current_profile(context))),
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
    """执行二次确认后的危险操作（包装）。

    操作完成后删除确认消息本身（只留结果消息；用户要求
    2026-01-01：改完了只留这一条信息）。确认消息是回调消息，
    结果消息是 _reply 新发的，两条是独立消息，删确认不影响结果。
    重启类操作（restartsvc/restartbot/upgrade）执行时进程可能被杀，
    清理跑不到，确认消息会残留——可接受的小例外。
    """
    handled = await _handle_confirm_inner(action, idx, update, context)
    if not handled:
        return False
    query = update.callback_query
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        if query is not None and query.message is not None:
            await _delete_message_safe(
                context, chat_id, query.message.message_id)
        await _delete_tracked_prompts(context, chat_id)
    return True


async def _handle_confirm_inner(action, idx, update, context):
    """执行二次确认后的危险操作。

    由 handlers.py 的 "c:<action>:<idx>" 回调转发（该回调已过白名单，
    本函数参数顺序与 handler 不同，不加 @require_allowed）。
    返回 True 表示已处理，False 表示未知 action。

    action：
      - changeip：换实例公网 IP（旧 IP 释放，DNS 需同步更新）。
      - resize：A1 升/降配（目标规格在 user_data["pending_resize"]）。
      - delvol：删除启动盘。
      - delbb：删除启动卷备份。
      - deloldestbb：删除最早备份后再给该启动盘创建新备份（idx 是
        user_data["volumes"] 的下标；额度满了时"删除最早备份并继续"）。
      - restorebb：从备份恢复出新实例（新实例名在
        user_data["pending_restore_name"]，idx 是
        user_data["boot_backups"] 的下标）。
      - bootswap：用备份替换实例的启动盘（原地恢复；idx 是
        user_data["bootswap_backups"] 的下标，实例下标在
        user_data["bootswap_idx"]；后台线程跑换盘，结果走通知）。
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
      - delipv6：删除 IPv6（idx 是 user_data["ipv6_entries"] 的下标）。
      - delpubip：删除保留 IP（idx 是 user_data["ip_reserved"] 的下标）。
      - userdesc：改用户描述（新描述在 user_data["pending_user_desc"]，
        idx 是 user_data["iam_users"] 的下标）。
      - useremail：改用户邮箱（新邮箱在 user_data["pending_user_email"]，
        idx 是 user_data["iam_users"] 的下标）。
      - delapikey：删除 API 密钥（idx 是 user_data["iam_keys"] 的下标，
        所属用户在 user_data["iam_keys_user"]）。
      - deluser：删除 IAM 用户（idx 是 user_data["iam_users"] 的下标；
        执行前再次检查目标不是当前 profile 的 API 用户）。
      - addadmin：增加管理员账号（用户名+邮箱在 user_data["add_admin"]）。
      - addiamuser：增加普通 IAM 用户（用户名+邮箱在 user_data["add_iam_user"]）。
      - resetpwd：重置 IAM 用户控制台临时密码。
      - createapikey：为已有 IAM 用户生成并上传 API Key。
      - createbotuser：创建专用 Bot IAM 用户、组、策略并生成 API Key。
    """
    if action == "changeip":
        inst = resolve_instance(context, idx)
        if inst is None:
            await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
            return True
        try:
            new_ip = await run_sync(
                change_public_ip, get_cli(context),
                get_compartment(context), inst["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(
                exc, lang=lang_of(update, context)))
            return True
        name = inst.get("display-name") or "?"
        await _reply(
            update, context,
            bot_i18n.tr(update, context, "changeip.done", name=name,
                        ip=new_ip))
        return True

    if action == "resize":
        inst = resolve_instance(context, idx)
        target = context.user_data.get("pending_resize")
        if inst is None or not target:
            await _reply(update, context, bot_i18n.tr(
                update, context, "resize.expired"))
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
                 # --force is required: without it the OCI CLI asks interactively "Are you sure you want to continue? [y/N]", and a subprocess with no stdin to answer hangs until timeout. --force 必加：不加时 OCI CLI 会交互确认 "Are you sure you want to continue? [y/N]"，subprocess 无 stdin 应答会一直挂到超时。
                 "--force"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(
                exc, lang=lang_of(update, context)))
            return True
        context.user_data.pop("pending_resize", None)
        name = inst.get("display-name") or "?"
        await _reply(
            update, context,
            bot_i18n.tr(update, context, "resize.done", name=name,
                        ocpus=target['ocpus'],
                        memory=target['memory_gb']))
        return True

    if action == "delvol":
        vol = resolve_volume(context, idx)
        if vol is None:
            await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
            return True
        try:
            await run_sync(delete_boot_volume, get_cli(context), vol["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        name = vol.get("display-name") or "?"
        await _reply(update, context, f"✅ 启动盘 {name} 已删除。")
        return True

    if action == "delbb":
        backup = resolve_boot_backup(context, idx)
        if backup is None:
            await _reply(update, context, "备份不存在或已过期，请重新打开启动卷备份列表。")
            return True
        try:
            await run_sync(delete_boot_volume_backup, get_cli(context),
                           backup["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        name = backup.get("display-name") or "?"
        await _reply(update, context, f"✅ 备份 {name} 已删除。")
        return True

    if action == "delbbsel":
        # idx is comma-separated indexes like "0,2,3". Delete one by one; a single failure does not affect the others; afterwards clear the multi-select state and report success/failure counts. idx 是逗号分隔的下标，如 "0,2,3"。逐个删除，单个失败不影响其他；结束后清多选状态并汇报成功/失败数。
        try:
            indices = [int(p) for p in str(idx).split(",") if p.strip()]
        except ValueError:
            await _reply(update, context, "参数错误，请重新选择要删除的备份。")
            return True
        backups = context.user_data.get("boot_backups") or []
        context.user_data.pop("bb_ms_selected", None)
        deleted, failed = [], []
        for i in indices:
            if not 0 <= i < len(backups):
                failed.append(f"下标 {i}（已过期）")
                continue
            backup = backups[i]
            try:
                await run_sync(delete_boot_volume_backup, get_cli(context),
                               backup["id"])
                deleted.append(backup.get("display-name") or "?")
            except Exception as exc:  # noqa: BLE001 - 单个失败继续删其余
                failed.append(f"{backup.get('display-name') or '?'}"
                              f"（{exc}）")
        lines = []
        if deleted:
            lines.append(f"✅ 已删除 {len(deleted)} 个备份："
                         + "、".join(deleted))
        if failed:
            lines.append(f"⚠️ {len(failed)} 个删除失败："
                         + "、".join(failed))
        await _reply(update, context, "\n".join(lines) or "没有删除任何备份。")
        return True

    if action == "delibbsel":
        try:
            indices = [int(p) for p in str(idx).split(",") if p.strip()]
        except ValueError:
            await _reply(update, context, "参数错误，请重新选择要删除的备份。")
            return True
        backups = context.user_data.get("ib_backups") or []
        context.user_data.pop("ib_ms_selected", None)
        deleted, failed = [], []
        for i in indices:
            if not 0 <= i < len(backups):
                failed.append(f"下标 {i}（已过期）")
                continue
            backup = backups[i]
            try:
                await run_sync(delete_boot_volume_backup, get_cli(context),
                               backup["id"])
                deleted.append(backup.get("display-name") or "?")
            except Exception as exc:  # noqa: BLE001 - 单个失败继续删其余
                failed.append(f"{backup.get('display-name') or '?'}"
                              f"（{exc}）")
        lines = []
        if deleted:
            lines.append(f"✅ 已删除 {len(deleted)} 个备份："
                         + "、".join(deleted))
        if failed:
            lines.append(f"⚠️ {len(failed)} 个删除失败："
                         + "、".join(failed))
        await _reply(update, context, "\n".join(lines) or "没有删除任何备份。")
        return True

    if action == "deloldestbb":
        vol = resolve_volume(context, idx)
        if vol is None:
            await _reply(update, context, "磁盘不存在或已过期，请重新打开磁盘列表。")
            return True
        cli = get_cli(context)
        compartment = get_compartment(context)
        try:
            # Re-check the quota and take the currently earliest backup (it may have changed while the user was confirming). 重新查一次配额，取当前最早的备份（用户确认期间可能有变化）。
            _count, _limit, _full, oldest = await run_sync(
                check_backup_quota, cli, compartment)
            if oldest is None:
                await _reply(update, context, "没有可删除的备份，请重新点备份。")
                return True
            await run_sync(delete_boot_volume_backup, cli, oldest["id"])
            backup = await run_sync(
                create_boot_volume_backup_with_limit_retry, cli, vol["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        old_name = oldest.get("display-name") or "?"
        new_name = backup.get("display-name") or "?"
        await _reply(update, context,
                     f"✅ 已删除最早备份「{old_name}」，"
                     f"新备份「{new_name}」创建中，几分钟后可用。")
        _start_backup_watch(update, cli, backup,
                            vol.get("display-name") or "?")
        return True

    if action == "restorebb":
        backup = resolve_boot_backup(context, idx)
        if backup is None:
            await _reply(update, context, "备份不存在或已过期，请重新打开启动卷备份列表。")
            return True
        display_name = context.user_data.pop("pending_restore_name", None)
        if not display_name:
            await _reply(update, context, "新实例名已过期，请重新点恢复并输入实例名。")
            return True
        cli = get_cli(context)
        compartment = get_compartment(context)
        # Shape: the shape of the instance the backup came from; fall back to A1 1 OCPU / 6 GB when unknowable. 形状：备份来源实例的形状，查不到用默认 A1 1核/6GB。
        try:
            detected = await run_sync(get_source_instance_shape, cli,
                                      compartment, backup)
        except Exception:  # noqa: BLE001 - 来源查不到就兜底默认形状
            detected = None
        if detected:
            shape, ocpus, memory_gb = detected
        else:
            shape, ocpus, memory_gb = (DEFAULT_RESTORE_SHAPE,
                                       DEFAULT_RESTORE_OCPUS,
                                       DEFAULT_RESTORE_MEMORY_GB)
        if shape.endswith(".Flex"):
            if ocpus is None:
                ocpus = DEFAULT_RESTORE_OCPUS
            if memory_gb is None:
                memory_gb = DEFAULT_RESTORE_MEMORY_GB
        # A1 per-instance cap + free-tier pre-check (boot disk counted at the backup's actual size). A1 单台上限 + 免费额度预检（启动盘按备份实际大小算）。
        if shape == SHAPE_A1_FLEX:
            cap_error = check_a1_single_instance_cap(ocpus, memory_gb)
            if cap_error:
                await _reply(update, context, f"❌ {cap_error}")
                return True
        try:
            ok, reason = await run_sync(
                can_launch_batch, cli, compartment, shape,
                ocpus=ocpus or 0, memory_gb=memory_gb or 0, count=1,
                boot_volume_size_gbs=backup.get("size-in-gbs") or 47)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        if not ok:
            await _reply(update, context, f"❌ 配额预检不通过：{reason}")
            return True
        availability_domain = PLAN_DEFAULTS["availability_domain"]
        subnet_id = PLAN_DEFAULTS["subnet_id"]
        ssh_keys_file = PLAN_DEFAULTS["ssh_keys_file"]
        if availability_domain in (None, "", "xxxx:AP-SINGAPORE-1-AD-1"):
            await _reply(update, context,
                         "❌ 未配置可用域：请在部署环境变量 MBOT_PLAN_AD 里配置。")
            return True
        if subnet_id in (None, "", "ocid1.subnet.oc1..xxxxxxxx"):
            await _reply(update, context,
                         "❌ 未配置子网：请在部署环境变量 "
                         "MBOT_PLAN_SUBNET_ID 里配置。")
            return True
        if ssh_keys_file in (None, "", "/path/to/keys.pub"):
            # A restored system keeps the original machine's authorized_keys, so the original key still logs in; the SSH public key file is therefore optional, and an empty string is passed when unconfigured. 恢复出来的系统保留原机的 authorized_keys，原密钥照样能登录，所以 SSH 公钥文件不是必须的；没配就传空字符串。
            ssh_keys_file = ""
        await _reply(update, context,
                     f"♻️ 正在从备份重建启动盘并开机「{display_name}」，"
                     "需要几分钟，请稍候…")
        try:
            result = await run_sync(
                restore_boot_volume_backup, cli, compartment, backup["id"],
                availability_domain=availability_domain,
                subnet_id=subnet_id, display_name=display_name,
                ssh_keys_file=ssh_keys_file, shape=shape, ocpus=ocpus,
                memory_gb=memory_gb, assign_public_ip=True)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        instance_id = result.get("instance_id") or ""
        await _reply(update, context,
                     f"✅ 恢复成功，新实例「{display_name}」已开机"
                     f"（{shape}）。\n实例 ID：…{instance_id[-12:]}")
        return True

    if action == "bootswap":
        backups = context.user_data.get("bootswap_backups") or []
        inst = resolve_instance(context,
                                context.user_data.get("bootswap_idx"))
        try:
            bidx = int(idx)
        except (TypeError, ValueError):
            bidx = -1
        if inst is None or bidx < 0 or bidx >= len(backups):
            await _reply(update, context,
                         "备份选择已过期，请重新打开实例详情再点“引导卷恢复”。")
            return True
        backup = backups[bidx]
        cli = get_cli(context)
        compartment = get_compartment(context)
        try:
            ok, remaining = await run_sync(
                check_boot_volume_space, cli, compartment,
                backup.get("size-in-gbs") or 0)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        if not ok:
            await _reply(
                update, context,
                f"❌ 启动盘额度不足：还剩 {remaining}GB，"
                f"新盘需要 {backup.get('size-in-gbs')}GB。"
                "请先删除不用的启动盘再恢复。")
            return True
        instance_id = inst["id"]
        with _BOOTSWAP_LOCK:
            if instance_id in _BOOTSWAP_RUNNING:
                await _reply(update, context, "这台实例正在换盘，请等它完成后再试。")
                return True
            _BOOTSWAP_RUNNING.add(instance_id)
        name = inst.get("display-name") or "?"
        await _reply(
            update, context,
            f"♻️ 已开始恢复实例「{name}」的引导卷（约几分钟），"
            "完成后会发通知告诉你。")
        chat_id = (update.effective_chat.id
                   if update.effective_chat else _notify_chat_id(context))
        try:
            thread = threading.Thread(
                target=_bootswap_thread,
                args=(bot_config.TELEGRAM_BOT_TOKEN, chat_id,
                      get_current_profile(context),
                      get_current_region(context),
                      compartment, instance_id, name,
                      backup["id"], backup.get("display-name") or "?"),
                daemon=True)
            thread.start()
        except Exception:  # noqa: BLE001 - 线程起不来就放掉锁并报错
            with _BOOTSWAP_LOCK:
                _BOOTSWAP_RUNNING.discard(instance_id)
            await _reply(update, context, "❌ 启动恢复任务失败，请稍后再试。")
        return True

    if action == "delmfa":
        try:
            devices = await run_sync(
                list_mfa_devices, get_cli(context), get_compartment(context))
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        if not devices:
            await _reply(update, context, "没有任何 MFA 设备，无需清除。")
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
        await _reply(update, context, text)
        return True

    if action == "rename":
        inst = resolve_instance(context, idx)
        new_name = context.user_data.get("pending_rename_name")
        if inst is None or not new_name:
            await _reply(update, context, bot_i18n.tr(
                update, context, "rename.expired"))
            return True
        try:
            await run_sync(rename_instance, get_cli(context),
                           inst["id"], new_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(
                exc, lang=lang_of(update, context)))
            return True
        context.user_data.pop("pending_rename_name", None)
        old_name = inst.get("display-name") or "?"
        await _reply(update, context, bot_i18n.tr(
            update, context, "rename.done", old_name=old_name,
            new_name=new_name))
        return True

    if action == "reinstall":
        inst = resolve_instance(context, idx)
        if inst is None:
            await _reply(update, context, bot_i18n.tr(
                update, context, "common.instance_gone_short"))
            return True
        try:
            await run_sync(reinstall_instance, get_cli(context), inst["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(
                exc, lang=lang_of(update, context)))
            return True
        name = inst.get("display-name") or "?"
        await _reply(update, context, bot_i18n.tr(
            update, context, "reinstall.done", name=name))
        return True

    if action in ("batchstart", "batchstop", "batchrestart", "batchterminate"):
        selected = context.user_data.get("batch_selected") or set()
        instances = [resolve_instance(context, i) for i in sorted(selected)]
        instances = [inst for inst in instances if inst is not None]
        if not instances:
            await _reply(update, context, "选中的实例已过期，请重新打开批量操作。")
            return True
        ids = [inst["id"] for inst in instances]
        names = {inst["id"]: (inst.get("display-name") or "?")
                 for inst in instances}
        verb = _BATCH_ACTION_TEXT[action[len("batch"):]]
        try:
            # Call the module-level imported function directly (do not pre-build a dict), so tests can patch it. 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
            if action == "batchstart":
                result = await run_sync(batch_start, get_cli(context), ids)
            elif action == "batchstop":
                result = await run_sync(batch_stop, get_cli(context), ids)
            elif action == "batchrestart":
                result = await run_sync(batch_restart, get_cli(context), ids)
            else:
                result = await run_sync(batch_terminate, get_cli(context), ids)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        context.user_data.pop("batch_selected", None)
        context.user_data.pop("pending_batch_action", None)
        await _reply(update, context, fmt_batch_result(verb, result, names))
        return True

    if action == "delbucket":
        buckets = context.user_data.get("os_buckets") or []
        try:
            bucket = buckets[int(idx)]
        except (TypeError, ValueError, IndexError):
            bucket = None
        if bucket is None:
            await _reply(update, context, "Bucket 不存在或已过期，请重新打开对象存储列表。")
            return True
        name = bucket.get("name") or "?"
        try:
            await run_sync(delete_bucket, get_cli(context), name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context, f"✅ Bucket {name} 已删除。")
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
            await _reply(update, context, "对象不存在或已过期，请重新打开对象列表。")
            return True
        bucket_name = bucket.get("name") or "?"
        object_name = obj.get("name") or "?"
        try:
            await run_sync(delete_object, get_cli(context),
                           bucket_name, object_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context, f"✅ 对象 {object_name} 已删除。")
        return True

    if action == "createsender":
        email = context.user_data.get("pending_sender")
        if not email:
            await _reply(update, context, "发件人邮箱已过期，请重新从菜单进入。")
            return True
        try:
            await run_sync(create_sender, get_cli(context),
                           get_compartment(context), email)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        context.user_data.pop("pending_sender", None)
        await _reply(update, context,
                     f"✅ 发件人 {email} 已创建。\n"
                     "请到该邮箱点击 OCI 的验证链接，验证后才能使用。")
        return True

    if action == "delplan":
        plan_id = str(idx)
        if not PLANS.delete(plan_id):
            await _reply(update, context, f"抢机计划 {plan_id} 不存在，可能已被删除。")
            return True
        await _reply(update, context, f"✅ 抢机计划 {plan_id} 已删除。")
        return True

    if action in ("restartsvc", "restartbot"):
        if action == "restartbot":
            service = BOT_SERVICE
        else:
            try:
                service = SERVICES[int(idx)]
            except (TypeError, ValueError, IndexError):
                await _reply(update, context, "服务不存在，请重新打开客户端管理。")
                return True
        # Restarting the bot kills the current process: send the "restarting" message before executing. 重启机器人会杀掉当前进程：先把“正在重启”发出去再执行。
        await _reply(update, context, f"🔄 正在重启服务 {service}…")
        try:
            await run_sync(restart_service, service)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, f"❌ 重启 {service} 失败：{exc}")
            return True
        if service != BOT_SERVICE:
            await _reply(update, context, f"✅ 服务 {service} 已重启。")
        # When the bot restarts, the process is about to be killed by systemd, so no second message is sent; the user already got "restarting", and the service coming back up means the restart completed. 重启机器人时进程即将被 systemd 杀掉，不再发第二条消息；用户已收到“正在重启”，服务恢复后即代表重启完成。
        return True

    if action == "openport":
        if "xxxxxxxx" in PANEL_SECURITY_LIST_ID:
            await _reply(update, context,
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
            await _reply(update, context, fmt_error(exc))
            return True
        rule_text = "已新增放行规则" if added else "放行规则已存在"
        await _reply(
            update, context,
            f"✅ {PANEL_PORT} 端口已放行（OCI 安全组{rule_text} + UFW）。\n"
            f"⚠️ {PANEL_PORT} 端口已暴露到公网，任何人可访问登录页，"
            "请确保密码足够强。")
        return True

    if action == "upgrade":
        if not await run_sync(is_git_repo, PROJECT_ROOT):
            await _reply(
                update, context,
                "⚠️ 当前目录不是 git 仓库，无法自动升级，请手动升级：\n"
                "git pull && pip install -r requirements.txt，"
                "然后重启 rbot-clone-web 与 rbot-clone-bot 两个服务。")
            return True
        # The upgrade restarts the bot at the end (killing the current process): send the message before executing. 升级最后会重启机器人（杀掉当前进程）：先把消息发出去再执行。
        await _reply(update, context,
                     "⬆️ 开始升级：拉取最新代码并安装依赖，"
                     "随后重启面板与机器人，约 1 分钟不可用…")
        try:
            await run_sync(upgrade_client, PROJECT_ROOT)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context, "✅ 升级完成，面板与机器人正在重启。")
        return True

    if action == "delipv6":
        entries = context.user_data.get("ipv6_entries") or []
        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            idx_int = -1
        if idx_int < 0 or idx_int >= len(entries):
            await _reply(update, context, "目标已过期，请重新打开 IPv6 管理。")
            return True
        entry = entries[idx_int]
        try:
            await run_sync(delete_ipv6, get_cli(context), entry["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(
            update, context,
            f"✅ 已删除 {entry['name']} 的 IPv6 {entry['ip']}。")
        return True

    if action == "delpubip":
        entries = context.user_data.get("ip_reserved") or []
        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            idx_int = -1
        if idx_int < 0 or idx_int >= len(entries):
            await _reply(update, context, "目标已过期，请重新打开 IP 管理。")
            return True
        entry = entries[idx_int]
        try:
            await run_sync(delete_public_ip, get_cli(context), entry["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context, f"✅ 已删除保留 IP {entry['ip']}。")
        return True

    if action == "userdesc":
        user = _resolve_iam_user(context, idx)
        new_desc = context.user_data.get("pending_user_desc")
        if user is None or new_desc is None:
            await _reply(update, context, "用户或新描述已过期，请重新操作。")
            return True
        try:
            await run_sync(update_user, _get_iam_cli(context), user["id"],
                           description=new_desc)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        context.user_data.pop("pending_user_desc", None)
        await _reply(update, context,
                     f"✅ 用户「{user.get('name', '?')}」描述已更新。")
        return True

    if action == "useremail":
        user = _resolve_iam_user(context, idx)
        new_email = context.user_data.get("pending_user_email")
        if user is None or new_email is None:
            await _reply(update, context, "用户或新邮箱已过期，请重新操作。")
            return True
        try:
            await run_sync(update_user, _get_iam_cli(context), user["id"],
                           email=new_email)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        context.user_data.pop("pending_user_email", None)
        await _reply(update, context,
                     f"✅ 用户「{user.get('name', '?')}」邮箱已改为 {new_email}。")
        return True

    if action == "delapikey":
        keys = context.user_data.get("iam_keys") or []
        owner = context.user_data.get("iam_keys_user") or {}
        if (owner.get("profile") not in (None, get_current_profile(context))
                or owner.get("region") not in (None, get_current_region(context))):
            await _reply(update, context,
                         "API Key 列表属于其他 Oracle 账号/区域，已取消删除。")
            return True
        try:
            idx_int = int(idx)
        except (TypeError, ValueError):
            idx_int = -1
        if idx_int < 0 or idx_int >= len(keys) or not owner.get("id"):
            await _reply(update, context, "密钥不存在或已过期，请重新打开 API 密钥列表。")
            return True
        key = keys[idx_int]
        if _is_current_api_key(context, owner.get("id"),
                               key.get("fingerprint")):
            await _reply(update, context,
                         "❌ 不能删除当前 M_Bot 正在使用的 API Key。")
            return True
        try:
            await run_sync(delete_api_key, _get_iam_cli(context),
                           owner["id"], key.get("fingerprint"))
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context,
                     f"✅ 已删除用户「{owner.get('name', '?')}」的 API 密钥 "
                     f"{key.get('fingerprint', '?')}。")
        return True

    if action == "deluser":
        user = _resolve_iam_user(context, idx)
        if user is None:
            await _reply(update, context, "用户不存在或已过期，请重新打开云账户管理。")
            return True
        if _is_current_api_user(context, user.get("id")):
            await _reply(update, context,
                         f"❌ 不能删除用户「{user.get('name', '?')}」：它是机器人"
                         f"当前账号（{get_cached_profile_display_name(get_current_profile(context))}）"
                         "正在使用的 API 用户，删了机器人会立刻失联。")
            return True
        try:
            last_admin = await run_sync(
                is_last_admin, _get_iam_cli(context),
                _get_iam_tenancy_id(context), user.get("id"))
        except Exception:  # noqa: BLE001 - 查不到时不拦，让删除继续走 OCI 报错
            last_admin = False
        if last_admin:
            await _reply(update, context,
                         f"❌ 不能删除用户「{user.get('name', '?')}」：它是租户里"
                         "最后一个管理员（Administrators 组只剩它一人），"
                         "删了整个租户就没人能管了。请先把另一个用户加进 "
                         "Administrators 组，再删除它。")
            return True
        try:
            await run_sync(delete_user, _get_iam_cli(context), user["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        await _reply(update, context,
                     f"✅ 用户「{user.get('name', '?')}」已删除。")
        return True

    if action == "addiamuser":
        target = context.user_data.pop("iam_add_user_target", None)
        if target is None or target != (get_current_profile(context),
                                        get_current_region(context)):
            context.user_data.pop("add_iam_user", None)
            await _reply(update, context,
                         "当前 Oracle 账号/区域已切换，已取消创建。请在目标账号重新操作。")
            return True
        draft = context.user_data.pop("add_iam_user", None) or {}
        name, email = draft.get("name"), draft.get("email")
        if not name or not email:
            await _reply(update, context, "普通用户创建信息已过期，请重新操作。")
            return True
        try:
            result = await run_sync(create_user_in_admin_group,
                                    _get_iam_cli(context),
                                    _get_iam_tenancy_id(context), name,
                                    email, "Created by M_Bot")
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        user = (result or {}).get("user") or {}
        group_error = (result or {}).get("group_error")
        if group_error:
            await _reply(update, context,
                         f"⚠️ 用户「{name}」已创建，但加进 Administrators 组"
                         f"失败（{group_error}），目前无权限。请在控制台"
                         "手动加组。")
        else:
            await _reply(update, context,
                         f"✅ 用户「{name}」已创建并加进 Administrators 组，"
                         "拥有最高管理权限。\n邮箱："
                         f"{email}\n该用户尚未设置控制台密码，如需登录请"
                         "重置临时密码。")
        return True

    if action == "resetpwd":
        user = _resolve_iam_user(context, idx)
        if user is None:
            await _reply(update, context, "用户列表已过期，请重新打开云账户管理。")
            return True
        try:
            # 不按“当前 API 用户”或控制台能力过滤；所有列表用户均可发起，
            # OCI 若有策略/Identity Domain 限制，会返回真实错误或邮件重置流程。
            password = await run_sync(create_or_reset_ui_password,
                                      _get_iam_cli(context), user["id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        if password:
            await _reply(update, context,
                         f"🔐 用户「{user.get('name', '?')}」的 OCI 控制台一次性密码已生成：\n\n"
                         f"{password}\n\n"
                         "⚠️ 这是敏感凭据，请立即复制并安全保存；首次登录时 OCI 会要求修改。")
        else:
            await _reply(update, context,
                         f"✅ 已为用户「{user.get('name', '?')}」发起控制台密码重置。\n"
                         "该租户可能启用了 Identity Domains，OCI 会向用户邮箱发送重置链接；"
                         "请让用户检查邮箱（包括垃圾邮件）。")
        return True

    if action == "createapikey":
        user = _resolve_iam_user(context, idx)
        if user is None:
            await _reply(update, context, "用户列表已过期，请重新打开云账户管理。")
            return True
        key_data = None
        try:
            key_data = await run_sync(create_api_key, _get_iam_cli(context),
                                      user["id"])
            pem = key_data["private_key_pem"]
            doc = BytesIO(pem)
            doc.name = "oci_api_key.pem"
            chat_id = update.effective_chat.id
            await context.bot.send_document(
                chat_id=chat_id, document=doc,
                filename="oci_api_key.pem",
                caption="OCI API 私钥。仅此一次发送，请下载后妥善保管，"
                        "不要转发或上传到公开仓库。")
        except Exception as exc:  # noqa: BLE001
            if key_data and key_data.get("fingerprint"):
                try:
                    await run_sync(delete_api_key, _get_iam_cli(context),
                                   user["id"], key_data["fingerprint"])
                except Exception:
                    logger.exception("API Key 交付失败后回滚密钥失败")
            await _reply(update, context,
                         "❌ API Key 创建或私钥文件交付失败。"
                         "如上传已成功，系统已尝试删除该 Key。\n"
                         + fmt_error(exc))
            return True
        region = get_current_region(context)
        await _reply(update, context,
                     f"✅ 已为用户「{user.get('name', '?')}」新增 API Key。\n"
                     f"Fingerprint：`{key_data['fingerprint']}`\n"
                     f"API Key 状态：{key_data.get('lifecycle_state', 'CREATING')}\n\n"
                     "OCI config 片段（建议单独保存为 ~/.oci/api_user_config，"
                     "不要覆盖 M_Bot 主配置）：\n"
                     "```ini\n[DEFAULT]\n"
                     f"user={user['id']}\n"
                     f"fingerprint={key_data['fingerprint']}\n"
                     "key_file=~/.oci/oci_api_key.pem\n"
                     f"tenancy={_get_iam_tenancy_id(context)}\n"
                     f"region={region}\n```\n"
                     "注意：如果该 profile 已有 DEFAULT 段，请合并字段，"
                     "不要覆盖其他账号配置。",
                     parse_mode="Markdown")
        return True

    if action == "createbotuser":
        target = context.user_data.pop("iam_bot_target", None)
        profile = get_current_profile(context)
        region = get_current_region(context)
        if target is None or target != (profile, region):
            await _reply(update, context,
                         "当前 Oracle 账号/区域已切换，已取消创建。请在目标账号重新操作。")
            return True
        tenancy_id = _get_iam_tenancy_id(context)
        cli = _get_iam_cli(context)
        try:
            result = await run_sync(
                create_bot_permission_user, cli, tenancy_id,
                "Group_for_Api_used", "api_user", "api@example.com",
                "Api_User_Policy")
            key_data = await run_sync(create_api_key, cli, result["user_id"])
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context,
                         "❌ Bot 权限用户创建/配置失败。可能已创建部分 IAM 资源，"
                         "请重新执行（流程支持复用已存在资源）。\n" + fmt_error(exc))
            return True
        try:
            doc = BytesIO(key_data["private_key_pem"])
            doc.name = "oci_api_key.pem"
            await context.bot.send_document(
                chat_id=update.effective_chat.id, document=doc,
                filename="oci_api_key.pem",
                caption="Bot API 用户私钥。请立即下载并妥善保管，"
                        "不要发送给他人或提交到 GitHub。")
        except Exception as exc:  # noqa: BLE001
            try:
                await run_sync(delete_api_key, cli, result["user_id"],
                               key_data["fingerprint"])
            except Exception:
                logger.exception("Bot API Key 交付失败后回滚密钥失败")
            await _reply(update, context,
                         "❌ 私钥文件发送失败，已尝试撤销刚创建的 API Key。\n"
                         + fmt_error(exc))
            return True
        await _reply(update, context,
                     "✅ Bot API 权限用户已配置。\n"
                     f"账号：{profile}\n用户名：api_user\n"
                     "组：Group_for_Api_used\n策略：Api_User_Policy\n"
                     f"Fingerprint：`{key_data['fingerprint']}`\n"
                     f"API Key 状态：{key_data.get('lifecycle_state', 'CREATING')}\n\n"
                     "权限语句：\n"
                     "```text\n"
                     "Allow group Group_for_Api_used to manage instance-family in tenancy\n"
                     "Allow group Group_for_Api_used to manage volume-family in tenancy\n"
                     "Allow group Group_for_Api_used to manage virtual-network-family in tenancy\n```\n"
                     "Bot 用户不含 manage users 权限，不能改别人密码；IAM 用户管理请走管理员。\n"
                     "OCI config 片段（建议单独保存为 ~/.oci/api_user_config，"
                     "不要覆盖 M_Bot 主配置）：\n"
                     "```ini\n[DEFAULT]\n"
                     f"user={result['user_id']}\n"
                     f"fingerprint={key_data['fingerprint']}\n"
                     "key_file=~/.oci/oci_api_key.pem\n"
                     f"tenancy={tenancy_id}\nregion={region}\n```\n"
                     "⚠️ 新建用户没有控制台密码；如需登录控制台，请在用户详情页重置临时密码。",
                     parse_mode="Markdown")
        return True

    if action == "addadmin":
        # Add admin account: username + email are in user_data["add_admin"]. 增加管理员账号：用户名+邮箱在 user_data["add_admin"] 里。
        # Create the user first, then add them to the Administrators group; if adding to the group fails the user already exists, so report honestly (visible in the list; can be added to the group or deleted manually). 先创建用户，再加进 Administrators 组；加组失败时用户已创建，如实汇报（用户在列表里可见，可手动加组或删除）。
        admin = context.user_data.pop("add_admin", None) or {}
        name = admin.get("name") or ""
        email = admin.get("email") or ""
        if not name or not email:
            await _reply(update, context,
                         "增加管理员账号的信息已过期，请重新点"
                         "“增加管理员账号”。")
            return True
        cli = _get_iam_cli(context)
        compartment = _get_iam_tenancy_id(context)
        try:
            user = await run_sync(create_user, cli, compartment, name,
                                  email)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return True
        user_id = (user or {}).get("id")
        group_id = None
        try:
            group_id = await run_sync(get_group_id_by_name, cli,
                                      compartment, "Administrators")
        except Exception as exc:  # noqa: BLE001 - 查组失败也如实汇报
            await _reply(update, context,
                         f"⚠️ 用户「{name}」已创建，但查 Administrators 组"
                         f"失败（{exc}），未加进管理员组。请在控制台手动"
                         "加组。")
            return True
        if group_id is None:
            await _reply(update, context,
                         f"⚠️ 用户「{name}」已创建，但租户下找不到 "
                         "Administrators 组，未加进管理员组。请在控制台"
                         "手动加组。")
            return True
        try:
            await run_sync(add_user_to_group, cli, user_id, group_id)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context,
                         f"⚠️ 用户「{name}」已创建，但加进 Administrators "
                         f"组失败（{exc}）。请在控制台手动加组。")
            return True
        await _reply(update, context,
                     f"✅ 管理员账号「{name}」已创建（邮箱 {email}），"
                     "已加进 Administrators 组。\n"
                     "控制台密码尚未设置：请在控制台给该用户设置临时密码，"
                     "或用 `oci iam user create-or-reset-ui-password` 生成。")
        return True

    return False


# ================= /domain 命令（Cloudflare DNS 管理） =================

def _get_cf_client():
    """取 Cloudflare 客户端：配置文件优先，环境变量兜底。

    tgbot.cf_config 未就绪时回落到 clouds 的 get_client()
    （纯环境变量方式）；两者都没有时抛 config 错误。
    """
    if get_configured_cf_client is not None:
        return get_configured_cf_client()
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


def fmt_cf_status():
    """凭据管理页全文：来源 / 方式 / 脱敏邮箱（绝不显示凭据值）。"""
    if resolve_cf_credentials is None:
        return "⚙️ Cloudflare 凭据管理未就绪，请稍后再试。"
    api_token, _api_key, email, source = resolve_cf_credentials()
    if source is None:
        return ("⚙️ Cloudflare 凭据：未配置。\n\n"
                "手动添加 API Token（推荐）或 Global API Key + 邮箱，"
                "也可以从服务器的证书脚本（1.sh）自动提取。")
    mode = "API Token" if api_token else "Global API Key"
    where = {"file": "机器人/面板里保存的",
             "oci_config": "OCI 配置文件 ~/oci_config 里的",
             "env": "服务器环境变量里的"}.get(source, "服务器环境变量里的")
    lines = [f"⚙️ Cloudflare 凭据：已配置（{where}，{mode}）。"]
    if email and mask_cf_email is not None:
        lines.append(f"账号邮箱：{mask_cf_email(email)}")
    if source == "file":
        lines.append("删除已保存的凭据后，会回落到 OCI 配置或环境变量（如果里面有）。")
    return "\n".join(lines)


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


def fmt_domain_record_line(record, bound_name=None):
    """记录列表里的一行：类型 名称 -> 内容，proxied 的标 🟠，已绑定到实例的标 🔗。"""
    rtype = record.get("type", "?")
    name = record.get("name", "?")
    content = record.get("content", "?")
    mark = " 🟠" if record.get("proxied") else ""
    bound = f" 🔗 已绑定到实例 {bound_name}" if bound_name else ""
    return f"• {rtype} {name} -> {content}{mark}{bound}"


def bound_map_from_instances(instances):
    """由实例列表建公网 IP -> 实例名的映射，只认未终止且有公网 IP 的实例。"""
    bound = {}
    for inst in instances or []:
        if (inst.get("lifecycle-state") or "").upper() in (
                "TERMINATED", "TERMINATING"):
            continue
        ip = inst.get("public-ip")
        if ip:
            bound[ip] = (inst.get("display-name")
                         or inst.get("id") or "?")
    return bound


def bound_map(context):
    """已绑定映射：由缓存的实例列表算出，不调 OCI。"""
    return bound_map_from_instances(context.user_data.get("instances"))


async def ensure_instances_cached(context):
    """实例缓存为空时尽力从 OCI 拉一次；失败只记日志、不阻塞 DNS 列表。"""
    if context.user_data.get("instances"):
        return True
    try:
        cli = get_cli(context)
        compartment = get_compartment(context)
        instances = await run_sync(list_instances, cli, compartment)
        instances = await run_sync(attach_public_ips, cli, instances)
        store_instances(context, instances)
        return True
    except Exception:  # noqa: BLE001 - 降级为无标注，不阻塞列表
        logger.warning("为 DNS 列表拉取实例失败，已降级为无标注", exc_info=True)
        return False


def fmt_domain_records(zone_name, records, limit=DOMAIN_RECORDS_LIMIT,
                       bound_map=None):
    """某 Zone 的记录列表全文；超过 limit 条时截断并提示总数。

    bound_map（公网 IP -> 实例名）给出时，A 记录且内容 IP 在映射里
    的行标出已绑定的实例，并在末尾汇总已绑定条数。
    """
    if not records:
        return f"🌐 {zone_name} 下没有 DNS 记录。"
    lines = [f"🌐 {zone_name} 的 DNS 记录（共 {len(records)} 条）：", ""]
    shown = records[:limit]
    bound_count = 0
    for record in shown:
        bound_name = None
        if (bound_map and record.get("type") == "A"
                and record.get("content") in bound_map):
            bound_name = bound_map[record["content"]]
            bound_count += 1
        lines.append(fmt_domain_record_line(record, bound_name))
    if bound_count:
        lines.append("")
        lines.append(f"其中 {bound_count} 条已绑定到实例，"
                     "删前请到「🗑️ 删除记录」里确认。")
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
        if getattr(exc, "kind", "") in ("config", "auth"):
            await _reply(
                update, context,
                fmt_cf_error(exc)
                + "\n\n可以在这里添加凭据，或从服务器的证书脚本自动提取。",
                reply_markup=cf_config_keyboard())
        else:
            await _reply(update, context, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001 - 其他异常也转成提示，不崩机器人
        await _reply(update, context, f"❌ 出错了：{exc}")
        return
    context.user_data["domain_zones"] = list(zones)
    await _reply(update, context, fmt_domain_zones(zones),
                 reply_markup=domain_zones_keyboard(zones))


def _dns_content_hint(rtype):
    """添加记录时按类型的输入提示。"""
    hints = {
        "A": "内容填 IPv4 地址，例如 203.0.113.10",
        "AAAA": "内容填 IPv6 地址，例如 2001:db8::1",
        "CNAME": "内容填目标域名，例如 example.com",
        "TXT": "内容填任意文本，例如 v=spf1 -all",
        "MX": "内容填邮件服务器域名，例如 mail.example.com",
    }
    return hints.get(rtype, "内容不能为空")


def _validate_dns_record_name(name):
    """记录名校验：@（根域名）或主机名（字母数字、-、_、.）。"""
    if name == "@":
        return True
    if not name or len(name) > 253:
        return False
    if name.startswith(("-", ".")) or name.endswith("."):
        return False
    return re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None


def _validate_dns_record_content(rtype, content):
    """按类型校验记录内容；通过返回 None，否则返回错误文案。"""
    if not content:
        return "内容不能为空。"
    if rtype == "A":
        try:
            ipaddress.IPv4Address(content)
        except ValueError:
            return "A 记录的内容必须是 IPv4 地址，例如 203.0.113.10。"
    elif rtype == "AAAA":
        try:
            ipaddress.IPv6Address(content)
        except ValueError:
            return "AAAA 记录的内容必须是 IPv6 地址，例如 2001:db8::1。"
    elif rtype in ("CNAME", "MX"):
        if not is_valid_domain(content.rstrip(".")):
            return f"{rtype} 记录的内容必须是一个域名，例如 mail.example.com。"
    return None


def fmt_dns_add_summary(zone_name, draft):
    """添加记录确认页全文。"""
    name = draft.get("name") or "?"
    shown_name = "根域名（@）" if name == "@" else name
    return (
        "➕ 确认添加 DNS 记录？\n\n"
        f"Zone：{zone_name}\n"
        f"类型：{draft.get('type')}\n"
        f"名称：{shown_name}\n"
        f"内容：{draft.get('content')}\n"
        "TTL：自动，代理：关闭\n\n"
        "（如需开启 Cloudflare 代理或改 TTL，"
        "请到 Web 面板 DNS 页编辑。）"
    )


async def _show_domain_records(update, context, zone_id):
    """查某 Zone 的记录并显示；记录存进 user_data 供删除按下标取用。"""
    try:
        client = _get_cf_client()
        records = await run_sync(client.list_dns_records, zone_id)
    except CloudflareError as exc:
        await _reply(update, context, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, f"❌ 出错了：{exc}")
        return
    stored = context.user_data.setdefault("domain_records", {})
    stored[zone_id] = list(records)
    zone_name = _resolve_zone_name(context, zone_id, records)
    await ensure_instances_cached(context)
    bmap = bound_map(context)
    await _reply(update, context,
                 fmt_domain_records(zone_name, records, bound_map=bmap),
                 reply_markup=domain_records_keyboard(zone_id, records))


def _domain_stored_records(context, zone_id):
    """取 user_data 里存的某 Zone 记录列表（删除流程按下标取用）。"""
    stored = context.user_data.get("domain_records") or {}
    return stored.get(zone_id)


async def _dns_add_do(update, context, zone_id):
    """确认添加：调 Cloudflare 创建记录，成功后回记录列表。"""
    draft = context.user_data.get("dns_add_draft") or {}
    if (draft.get("zone_id") != zone_id or not draft.get("type")
            or not draft.get("name") or not draft.get("content")):
        await _reply(update, context, "添加信息已过期，请重新点“添加记录”。")
        return
    try:
        client = _get_cf_client()
        await run_sync(client.create_dns_record, zone_id, draft["type"],
                       draft["name"], draft["content"])
    except CloudflareError as exc:
        await _reply(update, context, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, f"❌ 出错了：{exc}")
        return
    context.user_data.pop("dns_add_draft", None)
    zone_name = _resolve_zone_name(
        context, zone_id, _domain_stored_records(context, zone_id))
    await _reply(update, context,
                 f"✅ 已添加：{draft['type']} {draft['name']} "
                 f"-> {draft['content']}（{zone_name}）")
    await _show_domain_records(update, context, zone_id)


async def _dns_delete_pick(update, context, zone_id):
    """删除记录：列出当前 Zone 的记录供选一条（无记录时提示）。"""
    records = _domain_stored_records(context, zone_id)
    if records is None:
        try:
            client = _get_cf_client()
            records = await run_sync(client.list_dns_records, zone_id)
        except CloudflareError as exc:
            await _reply(update, context, fmt_cf_error(exc))
            return
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, f"❌ 出错了：{exc}")
            return
        context.user_data.setdefault("domain_records", {})[zone_id] = \
            list(records)
    if not records:
        await _reply(update, context, "这个 Zone 下没有 DNS 记录，无需删除。",
                     reply_markup=domain_records_keyboard(zone_id, records))
        return
    zone_name = _resolve_zone_name(context, zone_id, records)
    await ensure_instances_cached(context)
    bmap = bound_map(context)
    await _reply(update, context,
                 f"🗑️ 删除 {zone_name} 的 DNS 记录：\n"
                 "已绑定到实例的记录排在前面（带 🔗）。\n"
                 "点一条记录进入确认，只删选中的那一条。",
                 reply_markup=domain_delete_pick_keyboard(
                     zone_id, records, bmap))


async def _dns_delete_confirm(update, context, zone_id, idx_text):
    """删除确认页：显示要删的记录全文，二次确认。"""
    records = _domain_stored_records(context, zone_id) or []
    try:
        idx = int(idx_text)
        record = records[idx]
    except (ValueError, IndexError):
        await _reply(update, context, "记录不存在或已过期，请刷新列表后再试。")
        return
    zone_name = _resolve_zone_name(context, zone_id, records)
    bmap = bound_map(context)
    bound_name = None
    if record.get("type") == "A" and record.get("content") in bmap:
        bound_name = bmap[record["content"]]
    warning = ""
    if bound_name:
        warning = (f"\n⚠️ 这条记录已绑定到实例 {bound_name}，"
                   "删除后该实例将失去这个域名指向。")
    elif record.get("type") not in ("A", "AAAA"):
        warning = (f"\n⚠️ 这是一条 {record.get('type')} 记录"
                   "（不是实例绑定），删除可能影响邮件、验证等服务，"
                   "请确认这条确实不要了。")
    await _reply(
        update, context,
        "⚠️ 确认删除这条 DNS 记录？\n\n"
        f"Zone：{zone_name}\n"
        f"{fmt_domain_record_line(record, bound_name)}\n"
        f"{warning}\n"
        "删除后立即生效，不可恢复。",
        reply_markup=domain_delete_confirm_keyboard(zone_id, idx))


async def _dns_delete_do(update, context, zone_id, idx_text):
    """确认删除：调 Cloudflare 删除记录，成功后回记录列表。"""
    records = _domain_stored_records(context, zone_id) or []
    try:
        idx = int(idx_text)
        record = records[idx]
    except (ValueError, IndexError):
        await _reply(update, context, "记录不存在或已过期，请刷新列表后再试。")
        return
    try:
        client = _get_cf_client()
        await run_sync(client.delete_dns_record, zone_id, record.get("id"))
    except CloudflareError as exc:
        await _reply(update, context, fmt_cf_error(exc))
        return
    except Exception as exc:  # noqa: BLE001
        await _reply(update, context, f"❌ 出错了：{exc}")
        return
    await _reply(update, context, f"🗑️ 已删除：{fmt_domain_record_line(record)}")
    await _show_domain_records(update, context, zone_id)


async def _handle_dns_add_name_input(update, context, text):
    """添加记录第一步：收记录名，校验后问内容（格式不对可重发）。"""
    draft = context.user_data.get("dns_add_draft") or {}
    zone_id = draft.get("zone_id")
    if not zone_id or not draft.get("type"):
        await _reply(update, context, "添加信息已过期，请重新点“添加记录”。")
        return
    if not _validate_dns_record_name(text):
        context.user_data["pending_input"] = {"kind": "dns_add_name"}
        await _reply_prompt(
            update, context,
            "❌ 记录名格式不对：只能是 @（根域名）或主机名"
            "（字母、数字、-、_、.，例如 www 或 sub.www）。"
            "请重新发送。",
            reply_markup=domain_add_cancel_keyboard(zone_id))
        return
    draft["name"] = text
    context.user_data["dns_add_draft"] = draft
    context.user_data["pending_input"] = {"kind": "dns_add_content"}
    await _reply_prompt(
        update, context,
        f"记录名：{text}\n{_dns_content_hint(draft['type'])}。",
        reply_markup=domain_add_cancel_keyboard(zone_id))


async def _handle_dns_add_content_input(update, context, text):
    """添加记录第二步：收内容，按类型校验后弹确认页（格式不对可重发）。"""
    draft = context.user_data.get("dns_add_draft") or {}
    zone_id = draft.get("zone_id")
    if not zone_id or not draft.get("type") or not draft.get("name"):
        await _reply(update, context, "添加信息已过期，请重新点“添加记录”。")
        return
    error = _validate_dns_record_content(draft["type"], text)
    if error:
        context.user_data["pending_input"] = {"kind": "dns_add_content"}
        await _reply_prompt(
            update, context,
            f"❌ {error}\n请重新发送内容。",
            reply_markup=domain_add_cancel_keyboard(zone_id))
        return
    draft["content"] = text
    context.user_data["dns_add_draft"] = draft
    zone_name = _resolve_zone_name(
        context, zone_id, _domain_stored_records(context, zone_id))
    await _reply(update, context, fmt_dns_add_summary(zone_name, draft),
                 reply_markup=domain_add_confirm_keyboard(zone_id))


@require_allowed
async def domain_callback(update, context):
    """回调 "domain:..."：Zone 记录查看 / 添加 / 删除。

    zone：查该 Zone 的 DNS 记录列表；back：重新列 Zone；
    add：添加记录选类型；addtype：已选类型，开始输名称；
    add:confirm：确认添加；delpick：删除记录选一条；
    del：删除某条的确认页；del:yes：确认删除。
    """
    data = _callback_data(update)
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "back":
        await handle_domain(update, context)
        return
    if action == "zone" and len(parts) == 3:
        await _show_domain_records(update, context, parts[2])
        return
    if action == "add":
        if len(parts) == 4 and parts[2] == "confirm":
            await _dns_add_do(update, context, parts[3])
            return
        if len(parts) == 3:
            zone_id = parts[2]
            zone_name = _resolve_zone_name(
                context, zone_id,
                _domain_stored_records(context, zone_id))
            await _reply(update, context,
                         f"➕ 给 {zone_name} 添加 DNS 记录：\n先选记录类型。",
                         reply_markup=domain_add_type_keyboard(zone_id))
            return
    if action == "addtype" and len(parts) == 4:
        zone_id, rtype = parts[2], parts[3]
        if rtype not in DNS_ADD_TYPES:
            await _reply(update, context, "不支持的记录类型，请重新选择。")
            return
        context.user_data["dns_add_draft"] = {"zone_id": zone_id,
                                              "type": rtype}
        context.user_data["pending_input"] = {"kind": "dns_add_name"}
        zone_name = _resolve_zone_name(
            context, zone_id, _domain_stored_records(context, zone_id))
        await _reply_prompt(
            update, context,
            f"➕ {zone_name} 新建 {rtype} 记录：\n"
            "请发送记录名称（例如 www；根域名填 @）。",
            reply_markup=domain_add_cancel_keyboard(zone_id))
        return
    if action == "delpick" and len(parts) == 3:
        await _dns_delete_pick(update, context, parts[2])
        return
    if action == "del":
        if len(parts) == 5 and parts[2] == "yes":
            await _dns_delete_do(update, context, parts[3], parts[4])
            return
        if len(parts) == 4:
            await _dns_delete_confirm(update, context, parts[2], parts[3])
            return
    await _reply(update, context, "参数错误，请重新发送 /domain。")


async def _delete_secret_message(update, context):
    """尽力删除用户刚发的含凭据的消息（删不掉就算了，不报错）。

    凭据值只在用户那条消息里出现过一次；删掉后聊天记录里不再留明文。
    """
    msg = update.effective_message
    if msg is None:
        return
    try:
        await context.bot.delete_message(chat_id=msg.chat_id,
                                         message_id=msg.message_id)
    except Exception:  # noqa: BLE001 - 删不掉不影响流程
        pass


async def _cf_extract_pick(update, context):
    """从服务器提取：列出当前账号/区域下运行中、有公网 IP 的实例供选择。"""
    try:
        cli = get_cli(context)
        instances = await run_sync(list_instances, cli,
                                   get_compartment(context))
        instances = await run_sync(attach_public_ips, cli, instances)
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        await _reply(update, context, fmt_error(exc))
        return
    hosts = [inst for inst in instances
             if inst.get("lifecycle-state") == "RUNNING"
             and inst.get("public-ip")]
    if not hosts:
        await _reply(update, context,
                     "当前账号/区域下没有运行中且有公网 IP 的实例。\n"
                     "要换账号或区域请先用菜单 2 切换。")
        return
    context.user_data["cf_extract_hosts"] = hosts
    await _reply(
        update, context,
        "选一台服务器，我去读它上面的证书脚本（1.sh）提取 CF_Key / CF_Email：\n"
        "（只读这两行，提取到后先校验，通过才保存）",
        reply_markup=cf_extract_hosts_keyboard(hosts))


async def _cf_extract_run(update, context, idx_text):
    """从服务器提取：按下标取实例，SSH 读 1.sh，校验后保存。"""
    hosts = context.user_data.get("cf_extract_hosts") or []
    try:
        host_inst = hosts[int(idx_text)]
    except (TypeError, ValueError, IndexError):
        await _reply(update, context, "该服务器已不在列表里，请重新点“从服务器提取”。")
        return
    public_ip = host_inst.get("public-ip")
    name = host_inst.get("display-name") or public_ip or "?"
    host = _guard_host_for_instance(public_ip)
    if host is None:
        await _reply(update, context,
                     "❌ 这台服务器没有配置守护 SSH 密钥，机器人连不上它。\n"
                     "请先给它加上守护密钥，或换一台服务器。")
        return
    await _reply(update, context, f"正在从 {name}（{public_ip}）提取凭据…")
    api_key, email, error = await run_sync(extract_cf_from_host, host)
    if error:
        await _reply(update, context, f"❌ 提取失败：{error}")
        return
    ok, zones_total, _err = await run_sync(validate_and_save, "",
                                           api_key, email)
    if not ok:
        await _reply(update, context,
                     "❌ 提取到的凭据校验没通过（可能已失效），没有保存。\n"
                     "请手动添加，或去服务器上确认 1.sh 里的凭据是否有效。")
        return
    masked = mask_cf_email(email) if mask_cf_email is not None else "***"
    await _reply(
        update, context,
        f"✅ 已从 {name} 提取并保存 Cloudflare 凭据"
        f"（账号邮箱 {masked}，{zones_total} 个 Zone）。",
        reply_markup=cf_manage_keyboard())


@require_allowed
async def cf_callback(update, context):
    """回调 "cf:*"：Cloudflare 凭据添加 / 提取 / 管理（v1.26.0）。

    add：选凭据方式（token/key 两步输入走 text_input_callback）；
    extract：选服务器读 1.sh 提取；manage：看当前凭据状态；
    delete：删除已保存的凭据文件（不动环境变量）；
    cancel：取消当前输入。
    """
    data = _callback_data(update)
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "cancel":
        context.user_data.pop("pending_input", None)
        context.user_data.pop("cf_draft", None)
        await _reply(update, context, "已取消。")
        return
    if action == "manage":
        await _reply(update, context, fmt_cf_status(),
                     reply_markup=cf_manage_keyboard())
        return
    if action == "add":
        if len(parts) >= 3 and parts[2] == "token":
            context.user_data["pending_input"] = {"kind": "cf_token"}
            await _reply_prompt(
                update, context,
                "请发送 Cloudflare API Token（直接发文字即可）：\n"
                "Token 在 Cloudflare 后台 My Profile > API Tokens 里建，"
                "需要 Zone:Read 与 DNS:Edit 权限。\n"
                "收到后我会立即校验，校验通过才保存；"
                "你这条消息我会尽力删掉，不留明文。",
                reply_markup=cf_cancel_keyboard())
            return
        if len(parts) >= 3 and parts[2] == "key":
            context.user_data.pop("cf_draft", None)
            context.user_data["pending_input"] = {"kind": "cf_key"}
            await _reply_prompt(
                update, context,
                "请发送 Global API Key（直接发文字即可）：\n"
                "Key 在 Cloudflare 后台 My Profile > API Tokens 最下方查看。\n"
                "收到后我会再问账号邮箱，两步都对了才校验保存；"
                "你发的消息我会尽力删掉，不留明文。",
                reply_markup=cf_cancel_keyboard())
            return
        await _reply(update, context, "选一种凭据方式：",
                     reply_markup=cf_add_mode_keyboard())
        return
    if action == "delete":
        if len(parts) >= 3 and parts[2] == "yes":
            _t, _k, _e, source = resolve_cf_credentials()
            if source != "file":
                await _reply(update, context,
                             "没有已保存的凭据文件（当前用的是服务器环境变量里"
                             "的凭据，机器人删不了；要换请改服务器上的配置）。")
                return
            clear_cf_config()
            await _reply(update, context,
                         "已保存的凭据已删除。环境变量里如有凭据，/domain "
                         "会继续用它；都没有时请重新添加。")
            return
        await _reply(
            update, context,
            "确认删除机器人/面板里保存的 Cloudflare 凭据？\n"
            "（只删保存的文件，不动环境变量；删除后如环境变量里没有凭据，"
            "/domain 会报未配置。）",
            reply_markup=cf_delete_confirm_keyboard())
        return
    if action == "extract":
        if extract_cf_from_host is None or validate_and_save is None:
            await _reply(update, context, "凭据管理模块未就绪，请稍后再试。")
            return
        if len(parts) >= 3:
            await _cf_extract_run(update, context, parts[2])
            return
        await _cf_extract_pick(update, context)
        return
    await _reply(update, context, "参数错误，请重新发送 /domain。")


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
    # Traffic guard and stop-keepalive have their own switches, independent of the monitoring switch; an exception in one step must not affect the other. 流量守护与停机保活：各自的开关，不依赖监控开关；某一步异常不能影响另一步。
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

    已开启时查本月出站流量（vnstat，SSH 到 DEFAULT 账号的运行中
    实例上查并汇总），超阈值且距上次触发超过 24 小时才动作：
    notify 只发提醒；block 在全部运行中实例上阻断断网（保留 SSH）；
    limit 在全部运行中实例上限速 200K。instances 是 job 已查好的
    列表（可为 None，流量查询与动作需要时再查）。
    """
    cfg = load_traffic_guard()
    if not cfg.get("enabled"):
        return
    chat_id = cfg.get("chat_id") or (context.bot_data.get("monitor")
                                     or {}).get("chat_id")
    if not chat_id:
        logger.warning("流量守护已开启但未记录通知 chat_id，跳过本次检查")
        return
    used_gb, used_note = await _fetch_guard_monthly_gb(context, instances)
    if used_gb is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"流量守护检查失败：{used_note}，本次跳过。")
        return
    if not should_trigger_traffic_guard(cfg, used_gb):
        return
    record_traffic_guard_trigger(cfg)
    threshold = cfg.get("threshold_gb")
    action = cfg.get("action") or "notify"
    if action == "notify":
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"⚠️ 流量守护提醒：本月出站流量 {used_gb:.2f}GB，"
                  f"已超过阈值 {fmt_threshold_gb(threshold)}。\n"
                  "当前动作是“仅通知”，未做任何限制；"
                  "如需阻断或限速请到 /oracle 菜单 37. 流量守护里切换。"))
        return
    if instances is None:
        try:
            profile = bot_config.DEFAULT_PROFILE
            cli = bot_config.get_oci_client(profile=profile)
            compartment = bot_config.get_compartment_id(profile)
            instances = await run_sync(list_instances, cli, compartment)
            instances = await run_sync(attach_public_ips, cli, instances)
        except Exception as exc:  # noqa: BLE001
            await context.bot.send_message(
                chat_id=chat_id,
                text="流量守护：流量已超阈值，但查实例列表失败，"
                     f"未能执行动作：{fmt_error(exc)}")
            return
    if action == "block":
        command = guard_block_command()
        verb = "阻断断网（SSH 已保留）"
    else:
        command = guard_limit_command()
        verb = "限速 200K"
    ok_names, failed = await _run_guard_command_on_instances(
        command, instances)
    lines = [f"🛡️ 流量守护：本月出站流量 {used_gb:.2f}GB 已超过阈值 "
             f"{fmt_threshold_gb(threshold)}，已{verb}。"]
    if ok_names:
        lines.append(f"已执行：{', '.join(ok_names)}")
    for name, reason in failed:
        lines.append(f"{name}：未执行（{reason}）")
    if not ok_names and not failed:
        lines.append("当前没有运行中的实例，无需执行。")
    lines.append("恢复请到 /oracle 菜单 37. 流量守护里点“解除限制”。")
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
            continue
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
            + fmt_quota_summary(
                summary, get_cached_profile_display_name(profile))
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


async def billing_check_job(context):
    """每天跑一次：检查三账号本月费用与付费实例（账单预警）。

    逻辑搬自 check_billing.py：本月费用 vs SGD 1 预算、费用变化、
    付费 RUNNING 实例自动 STOP、免费额度超限。有告警时双通道通知
    （TG + Bark，Bark 未开启时只推 TG）；无变化时静默。
    通知 chat_id 优先用 Bark 配置里记的，其次监控/每日报告里记的，
    最后用白名单用户 ID（与机器人私聊的 chat_id 就是用户 ID）。
    """
    results = []
    for profile in bot_config.PROFILES:
        try:
            results.append(await run_sync(check_account_billing, profile))
        except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其他
            results.append({"profile": profile, "error": str(exc)})
    prev_state = load_billing_state()
    alerts = collect_billing_alerts(results, prev_state)
    new_state = {}
    for res in results:
        if res.get("error"):
            continue
        new_state[res["profile"]] = {
            "mtd_cost": res.get("mtd_cost", 0),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    save_billing_state(new_state)
    if not alerts:
        return
    chat_id = (load_bark_config().get("chat_id")
               or (context.bot_data.get("monitor") or {}).get("chat_id")
               or (context.bot_data.get("daily_report") or {}).get("chat_id"))
    if not chat_id and bot_config.ALLOWED_USER_IDS:
        chat_id = next(iter(bot_config.ALLOWED_USER_IDS))
    if not chat_id:
        logger.warning("账单预警有告警但未找到通知 chat_id，跳过通知")
        return
    # When the alert involves a paid instance (stopped / failed to stop / running while on the protect list), pin a reminder on top. 告警里有付费实例（停了 / 停失败 / 在保护名单里跑着）时置顶提醒。
    has_paid = any(res.get("stopped") or res.get("stop_errors")
                   or res.get("paid_running") for res in results)
    await run_sync(notify_dual, bot_config.TELEGRAM_BOT_TOKEN, chat_id,
                   "⚠️ Oracle 账单预警", fmt_billing_alerts(alerts),
                   group="oracle-billing", pin=has_paid)




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
        await _reply(update, context, fmt_error(exc))
        return
    context.user_data["os_buckets"] = list(buckets)
    await _reply(update, context, fmt_bucket_list(buckets),
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
        await _reply(update, context, "参数错误，请重新打开对象存储列表。")
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
            await _reply(update, context, "对象不存在或已过期，请重新打开对象列表。")
            return
        name = obj.get("name") or "?"
        await _reply(
            update, context,
            f"⚠️ 即将删除对象 {name}！\n删除后无法恢复。",
            reply_markup=confirm_keyboard("delobj", idx, "🗑️ 确认删除对象"))
        return
    if bucket is None:
        await _reply(update, context, "Bucket 不存在或已过期，请重新打开对象存储列表。")
        return
    bucket_name = bucket.get("name") or "?"
    if action == "objects":
        try:
            objects = await run_sync(list_objects, get_cli(context),
                                     bucket_name)
        except Exception as exc:  # noqa: BLE001
            await _reply(update, context, fmt_error(exc))
            return
        context.user_data["os_objects"] = list(objects)
        context.user_data["os_bucket_idx"] = idx
        await _reply(update, context, fmt_object_list(bucket_name, objects),
                     reply_markup=object_list_keyboard(objects))
        return
    if action == "delbucket":
        await _reply(
            update, context,
            f"⚠️ 即将删除 Bucket {bucket_name}！\n"
            "删除后无法恢复；Bucket 里还有对象时删除会失败，"
            "请先清空对象（或到 Web 面板勾选“同时删除全部对象”）。",
            reply_markup=confirm_keyboard("delbucket", idx,
                                          "🗑️ 确认删除 Bucket"))
        return
    await _reply(update, context, "未知操作，请重新打开对象存储列表。")


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
    await _reply_prompt(update, context,
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
    await _reply(update, context, fmt_plan_list(plans),
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
        await _reply(update, context, "参数错误，请重新打开抢机计划列表。")
        return
    _, action, plan_id = parts
    plan = PLANS.get(plan_id)
    if plan is None:
        await _reply(update, context, f"抢机计划 {plan_id} 不存在，可能已被删除。")
        return
    if action == "toggle":
        updated = PLANS.set_enabled(plan_id, not plan.get("enabled"))
        state = "已启用" if updated.get("enabled") else "已停用"
        await _reply(update, context, f"✅ 抢机计划 {plan_id} {state}。")
        await handle_plans(update, context)
        return
    if action == "delete":
        shape = (plan.get("shape") or "?").replace("VM.Standard.", "")
        await _reply(
            update, context,
            f"⚠️ 确认删除抢机计划 {plan_id}？\n"
            f"cron `{plan.get('cron')}`，{shape} "
            f"{plan.get('ocpus')} 核 / {plan.get('memory_gb')}GB。\n"
            "删除后不再按该计划定时抢机。",
            reply_markup=confirm_keyboard("delplan", plan_id,
                                          "🗑️ 确认删除计划"))
        return
    await _reply(update, context, "未知操作，请重新打开抢机计划列表。")


# ---- 11. 开放云面板端口 ----

@require_allowed
async def handle_openport(update, context):
    """菜单 11：开放云面板端口（二次确认）。

    未配置 PANEL_SECURITY_LIST_ID 时提示先配置，不执行。
    真正执行在 handle_confirm("openport")：OCI 安全组放行 + UFW 放行。
    """
    if "xxxxxxxx" in PANEL_SECURITY_LIST_ID:
        await _reply(update, context,
                     "⚠️ 未配置面板安全组：请先在 tgbot/handlers_extra.py 的 "
                     "PANEL_SECURITY_LIST_ID 里填入面板服务器所在子网的"
                     "安全组 OCID，再使用本功能。\n"
                     "查法：oci network security-list list "
                     "--compartment-id <你的 compartment OCID>",
                     reply_markup=back_to_oracle_keyboard())
        return
    await _reply(update, context,
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
    await _reply(update, context, fmt_client_status(statuses),
                 reply_markup=client_actions_keyboard())


@require_allowed
async def client_callback(update, context):
    """回调 "cl:restart:<idx>"：重启某服务的二次确认提示。

    真正执行在 handle_confirm("restartsvc")。
    """
    parts = _callback_data(update).split(":")
    if len(parts) != 3 or parts[1] != "restart":
        await _reply(update, context, "参数错误，请重新打开客户端管理。")
        return
    try:
        service = SERVICES[int(parts[2])]
    except (TypeError, ValueError, IndexError):
        await _reply(update, context, "服务不存在，请重新打开客户端管理。")
        return
    label = "面板" if service == WEB_SERVICE else "机器人"
    await _reply(update, context,
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
    await _reply(update, context,
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
    await _reply(update, context,
                 "⚠️ 确认升级客户端？\n"
                 "升级会拉取最新代码、安装依赖，然后重启面板与机器人，"
                 "约 1 分钟不可用。",
                 reply_markup=confirm_keyboard("upgrade", 0, "⬆️ 确认升级"))


@require_allowed
async def handle_oracleping(update, context):
    """打开浏览器端 OCI 区域延迟测试（43 个商业区域）。"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 开始 Oracle 全球测速",
                              url="https://regionping.cloud/oracle-ping-test")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])
    await _reply(update, context,
                 "🌍 Oracle Cloud 全球区域测速\n\n"
                 "在浏览器中测量你当前网络到 43 个 OCI 区域 Object Storage HTTPS 端点的延迟。"
                 "每个区域会预热连接并进行多次采样，适合比较从当前设备访问各区域的网络表现。\n\n"
                 "提示：这是 HTTPS 往返延迟，不是 ICMP Ping；请在实际使用代理/网络环境下打开。",
                 reply_markup=keyboard)


@require_allowed
async def handle_cfip(update, context):
    """Cloudflare 优选 IP 工具入口与安全使用说明。"""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ CloudflareSpeedTest 项目",
                              url="https://github.com/XIU2/CloudflareSpeedTest")],
        [InlineKeyboardButton("📡 Cloudflare 官方测速",
                              url="https://speed.cloudflare.com/")],
        [InlineKeyboardButton("◀ Oracle 菜单", callback_data="o:menu")],
    ])
    await _reply(update, context,
                 "⚡ Cloudflare 优选 IP\n\n"
                 "推荐使用 CloudflareSpeedTest 对 Cloudflare Anycast IPv4/IPv6 地址进行本地 TCP/HTTPS 延迟、下载速度与丢包测试。\n\n"
                 "建议流程：\n"
                 "1. 在运行代理客户端的同一网络/设备上测速；\n"
                 "2. 先测延迟与丢包，再用下载测速筛选；\n"
                 "3. 保留多个低延迟、低丢包 IP，分运营商/网络环境记录；\n"
                 "4. 将结果手动导入客户端的节点/地址配置，先备份原配置。\n\n"
                 "安全说明：本入口不会自动改 DNS、代理节点或服务器配置；优选结果因运营商、时段和线路而异。",
                 reply_markup=keyboard)


# ================= /oracle 菜单分发 =================

# Menu keys implemented by this module -> handlers ("changeip"/"rename"/"reinstall"/"console"/"bootswap" are not menu items but forwards for instance actions "i:<act>:<idx>"). 本模块实现的菜单项 key -> handler（"changeip"/"rename"/"reinstall"/"console"/"bootswap" 不是菜单项，是实例操作 "i:<act>:<idx>" 的转发项）。
# "console"/"bootswap" 不是菜单项，是实例操作 "i:<act>:<idx>" 的转发项）。
# Items not listed here make dispatch return False: 未列出的项 dispatch 返回 False：
#   - instances: core items, handled by handlers.py, not this module; - instances：核心项，由 handlers.py 处理，不在本模块；
#   - disable / delapi: not implemented (delapi is deliberately not done — it would delete the bot's own API key). - disable / delapi：不实现（其中 delapi 是故意不做——会删机器人自己的 API 密钥）。
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
    "autoip": handle_autoip,
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
    "bark": handle_bark,
    "rescue": handle_rescue,
    "oracleping": handle_oracleping,
    "cfip": handle_cfip,
    "rename": handle_rename_prompt,
    "reinstall": handle_reinstall_prompt,
    "console": handle_console,
    "bootswap": handle_bootswap,
    "bootbackup": instance_boot_backup_callback,
    "bootbackup-del": instance_boot_backup_delete_callback,
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
