#!/usr/bin/env python3
"""Telegram 机器人配置。

配置区在文件顶端，方便修改。真实 Token / OCID 一律不写进代码：
  - 机器人 Token 从环境变量 RBOT_TELEGRAM_TOKEN 读。
  - 允许操作的用户 ID 从环境变量 RBOT_ALLOWED_USERS 读（逗号分隔的数字）。
  - OCI 的 profile / region / compartment 在下面 PROFILES 里配，
    真实 OCID 用占位符，部署时改成真实值。

安全说明：
  - 本模块不读取、不打印、不外传 OCI API 私钥。认证全部由 OCI CLI
    自己读 ~/.oci/config 与 key_file 完成。
  - 白名单是硬规则：不在 ALLOWED_USER_IDS 里的用户发任何命令都直接拒绝。
"""

import os
import sys

# ================= 配置区（改这里） =================
# Telegram 机器人 Token：从环境变量读，不要硬编码。
# 获取方式：Telegram 上找 @BotFather，/newbot 创建机器人后会给 Token。
TELEGRAM_BOT_TOKEN = os.environ.get("RBOT_TELEGRAM_TOKEN", "")

# 允许使用机器人的 Telegram 用户 ID（白名单），逗号分隔。
# 查自己的 ID：Telegram 上找 @userinfobot，发任意消息即返回你的 ID。
# 为空时不允许任何人操作（机器人启动时会拒绝启动并提示）。
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("RBOT_ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

# 默认 OCI profile（~/.oci/config 里的 profile 名）。
DEFAULT_PROFILE = "DEFAULT"
# 默认区域。
DEFAULT_REGION = "ap-singapore-1"
# 默认 compartment OCID（占位符；根 compartment 即 tenancy OCID）。
# 部署时用环境变量 RBOT_COMPARTMENT_DEFAULT 填真实值。
DEFAULT_COMPARTMENT_ID = os.environ.get(
    "RBOT_COMPARTMENT_DEFAULT", "ocid1.tenancy.oc1..xxxxxxxx")

# 多账号配置：profile 名 -> {region, compartment_id}。
# 每个账号在 ~/.oci/config 里对应一个 profile。
# 部署时用环境变量填真实 compartment OCID：
#   RBOT_COMPARTMENT_DEFAULT / RBOT_COMPARTMENT_ACCOUNT2 /
#   RBOT_COMPARTMENT_ACCOUNT3（根 compartment 即 tenancy OCID）。
# 未设环境变量的账号不加入 PROFILES。
def _build_profiles():
    profiles = {
        "DEFAULT": {
            "region": "ap-singapore-1",
            "compartment_id": DEFAULT_COMPARTMENT_ID,
        },
    }
    account2 = os.environ.get("RBOT_COMPARTMENT_ACCOUNT2")
    if account2:
        profiles["ACCOUNT2"] = {
            "region": "us-sanjose-1",
            "compartment_id": account2,
        }
    account3 = os.environ.get("RBOT_COMPARTMENT_ACCOUNT3")
    if account3:
        profiles["ACCOUNT3"] = {
            "region": "us-chicago-1",
            "compartment_id": account3,
        }
    return profiles


PROFILES = _build_profiles()

# 区域中文名（与 web/config.py 的 REGION_LABELS 保持同步；未知区域显示原代码）。
# 账号切换菜单（菜单 13）按主区中文名显示账号，不再显示 DEFAULT/ACCOUNT2
# 这类 profile 名（用户反馈 2026-11-24：看着乱，分不清是哪个账号）。
REGION_LABELS = {
    "ap-singapore-1": "新加坡",
    "ap-seoul-1": "首尔",
    "us-sanjose-1": "圣何塞",
    "us-chicago-1": "芝加哥",
}


def get_region_label(region):
    """区域代码 -> 中文名；未知区域返回原代码。"""
    return REGION_LABELS.get(region, region)


def profile_display_name(name, region_count=None):
    """账号（profile）的显示名：主区中文名；订阅了多个区域时后缀（N 个区）。

    例：DEFAULT（新加坡，订阅 3 个区）-> "新加坡（3 个区）"；
    ACCOUNT2（圣何塞，1 个区）-> "圣何塞"。region_count 为 None 或 <=1
    时不加后缀（查不到区数时只显示主区名，不阻塞切换菜单）。
    """
    region = get_profile_config(name)["region"]
    label = get_region_label(region)
    if region_count and region_count > 1:
        label += f"（{region_count} 个区）"
    return label

# 机器人日志文件（查询最新日志命令读这个文件）。
LOG_FILE = os.path.expanduser("~/workspace/rbot-clone/telegram-bot.log")
# 快捷开机配置保存文件（JSON）。
PRESETS_FILE = os.path.expanduser("~/workspace/rbot-clone/tgbot/presets.json")
# 数据目录：监控类配置（流量守护 / 停机保活 / SSL 监控）存这里
# （traffic_guard.json / keepalive.json / ssl_watch.json 等）。
# 部署时用 RBOT_TGBOT_DATA_DIR 指到部署路径（如 /opt/rbot-clone/tgbot/data）。
DATA_DIR = os.path.expanduser(os.environ.get(
    "RBOT_TGBOT_DATA_DIR", "~/workspace/rbot-clone/tgbot/data"))
# 流量守护的 SSH 私钥：阻断 / 限速 / vnstat 查流量要 SSH 到实例上执行，
# 用这把专用密钥（公钥需加到各实例的 authorized_keys）。未配置时
# 流量守护只能“仅通知”，阻断/限速/解除按钮会提示未配置。
# 部署示例：RBOT_GUARD_SSH_KEY=/root/.ssh/rbot_guard
GUARD_SSH_KEY = os.path.expanduser(os.environ.get(
    "RBOT_GUARD_SSH_KEY", "")) if os.environ.get("RBOT_GUARD_SSH_KEY") else ""
# 流量守护 SSH 端口与用户名（实例标准配置：root + 14422）。
GUARD_SSH_PORT = 14422
GUARD_SSH_USER = "root"
# =====================================================

# 把项目根目录加到 sys.path 开头，保证 `import oci` 解析到本项目
# 的 oci/ 包（而不是系统里可能装的 OCI Python SDK 同名包）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def get_token():
    """返回机器人 Token；未配置时抛 RuntimeError（启动时检查）。"""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "未配置 Telegram 机器人 Token：请设置环境变量 RBOT_TELEGRAM_TOKEN "
            "（找 @BotFather 创建机器人后获取）")
    return TELEGRAM_BOT_TOKEN


def is_allowed(user_id):
    """用户 ID 是否在白名单里。白名单为空时谁都不允许。"""
    if not ALLOWED_USER_IDS:
        return False
    return user_id in ALLOWED_USER_IDS


def get_profile_config(profile=None):
    """返回指定 profile 的 {region, compartment_id}；未知 profile 抛 ValueError。"""
    name = profile or DEFAULT_PROFILE
    cfg = PROFILES.get(name)
    if cfg is None:
        raise ValueError(f"未知 profile：{name}（已配置：{', '.join(PROFILES)}）")
    return cfg


def get_compartment_id(profile=None):
    """返回指定 profile 的 compartment OCID。"""
    return get_profile_config(profile)["compartment_id"]


def get_region(profile=None):
    """返回指定 profile 的区域。"""
    return get_profile_config(profile)["region"]


def get_oci_client(profile=None, region=None):
    """返回指定 profile 的 OCIClient（认证由 OCI CLI 自己处理）。

    注意：这里延迟 import，避免 config 被无 oci 环境的测试 import 时失败。
    """
    from oci.client import OCIClient

    cfg = get_profile_config(profile)
    return OCIClient(profile=profile or DEFAULT_PROFILE,
                      region=region or cfg["region"])
