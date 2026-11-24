#!/usr/bin/env python3
"""Telegram 机器人配置。

配置区在文件顶端，方便修改。真实 Token / OCID 一律不写进代码：
  - 机器人 Token 从环境变量 MBOT_TELEGRAM_TOKEN 读。
  - 允许操作的用户 ID 从环境变量 MBOT_ALLOWED_USERS 读（逗号分隔的数字）。
  - OCI 的 profile / region / compartment 在下面 PROFILES 里配，
    真实 OCID 用占位符，部署时改成真实值。

安全说明：
  - 本模块不读取、不打印、不外传 OCI API 私钥。认证全部由 OCI CLI
    自己读配置文件（~/oci_config，旧部署回退 ~/.oci/config）与
    key_file 完成。
  - 白名单是硬规则：不在 ALLOWED_USER_IDS 里的用户发任何命令都直接拒绝。
"""

import json
import os
import sys
import tempfile

# ================= 配置区（改这里） =================
# Telegram 机器人 Token：从环境变量读，不要硬编码。
# 获取方式：Telegram 上找 @BotFather，/newbot 创建机器人后会给 Token。
TELEGRAM_BOT_TOKEN = os.environ.get("MBOT_TELEGRAM_TOKEN", "")

# 允许使用机器人的 Telegram 用户 ID（白名单），逗号分隔。
# 查自己的 ID：Telegram 上找 @userinfobot，发任意消息即返回你的 ID。
# 为空时不允许任何人操作（机器人启动时会拒绝启动并提示）。
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("MBOT_ALLOWED_USERS", "").split(",")
    if x.strip().isdigit()
}

# 默认 OCI profile（OCI 配置文件里的 profile 名）。
DEFAULT_PROFILE = "DEFAULT"
# 默认区域。
DEFAULT_REGION = "ap-singapore-1"
# 默认 compartment OCID（占位符；根 compartment 即 tenancy OCID）。
# 部署时用环境变量 MBOT_COMPARTMENT_DEFAULT 填真实值。
DEFAULT_COMPARTMENT_ID = os.environ.get(
    "MBOT_COMPARTMENT_DEFAULT", "ocid1.tenancy.oc1..xxxxxxxx")

# 多账号配置：profile 名 -> {region, compartment_id}。
# 每个账号在 OCI 配置文件（~/oci_config）里对应一个 profile。
# 注册方式：环境变量 MBOT_COMPARTMENT_<PROFILE>，值是该账号的
# compartment OCID（根 compartment 即 tenancy OCID）。机器人导入
# 新账号（/oci / /uploadapi）时会自动往 env 文件里加这一项，
# 重启机器人后新账号就出现在切换菜单里。
# region 从配置文件里该 profile 的 region 字段读；读不到时用下面的
# 回退表（旧账号）或 DEFAULT_REGION（新账号）。
# 未设 MBOT_COMPARTMENT_* 的 profile 不加入 PROFILES（例如只给
# R-Bot 用的受限 API profile 不会出现在菜单里）。
LEGACY_PROFILE_REGIONS = {
    "DEFAULT": "ap-singapore-1",
    "ACCOUNT2": "us-sanjose-1",
    "ACCOUNT3": "us-chicago-1",
}


def _build_profiles():
    from oci.client import read_oci_config_profiles
    file_profiles = read_oci_config_profiles()
    profiles = {
        "DEFAULT": {
            "region": file_profiles.get("DEFAULT", {}).get("region")
                      or LEGACY_PROFILE_REGIONS["DEFAULT"],
            "compartment_id": DEFAULT_COMPARTMENT_ID,
        },
    }
    for key, value in sorted(os.environ.items()):
        if not key.startswith("MBOT_COMPARTMENT_") or not value:
            continue
        name = key[len("MBOT_COMPARTMENT_"):]
        if name == "DEFAULT":
            continue
        profiles[name] = {
            "region": file_profiles.get(name, {}).get("region")
                      or LEGACY_PROFILE_REGIONS.get(name, DEFAULT_REGION),
            "compartment_id": value,
        }
    return profiles


PROFILES = _build_profiles()


def _quote_env_value(value):
    """env 文件里的值：含空格或 shell 特殊字符时用单引号包起来。"""
    text = str(value)
    if not text:
        return "''"
    import re
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def update_env_file_vars(updates, env_file=None):
    """把一批 KEY=VALUE 写进 env 文件（原子替换，权限 600）。

    已有的键原地替换，没有的键追加；其余行原样保留。
    值含空格或特殊字符时用单引号包起来（单引号本身转义为 '"'"'）。
    env_file 默认取 web/config.py 的 ENV_FILE（不要写死路径）。
    """
    if env_file is None:
        try:
            from web import config as web_config
            env_file = web_config.ENV_FILE
        except Exception:  # noqa: BLE001 - web.config 不可用时回退默认路径
            env_file = os.path.expanduser("~/.rbot-clone.env")
    path = env_file
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    remaining = dict(updates)
    output = []
    for line in lines:
        replaced = False
        for key, value in updates.items():
            if line.startswith(f"{key}="):
                output.append(f"{key}={_quote_env_value(value)}\n")
                remaining.pop(key, None)
                replaced = True
                break
        if not replaced:
            output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={_quote_env_value(value)}\n")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".", prefix=".mbot-env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(output)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)
    return path


# 区域中文名（与 web/config.py 的 REGION_LABELS 保持同步；未知区域显示原代码）。
# 账号切换（/oracle 菜单顶行“切换账号”按钮）按主区中文名显示账号，不再显示 DEFAULT/ACCOUNT2
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


def _needs_profile_suffix(name):
    """同主区的已注册账号里，自己是不是第一个（按 PROFILES 顺序）。

    不是第一个时显示名要加“·profile”区分，否则切换菜单里分不清
    （例如 DEFAULT/sg1/sg2 都在新加坡时，sg1 显示“新加坡·sg1”，
    DEFAULT 保持“新加坡”不变）。
    """
    region = get_profile_config(name)["region"]
    for other, cfg in PROFILES.items():
        if cfg.get("region") == region:
            return other != name
    return False


def profile_display_name(name, region_count=None):
    """账号（profile）的显示名：主区中文名；订阅了多个区域时后缀（N 个区）。

    例：DEFAULT（新加坡，订阅 3 个区）-> "新加坡（3 个区）"；
    ACCOUNT2（圣何塞，1 个区）-> "圣何塞"。region_count 为 None 或 <=1
    时不加后缀（查不到区数时只显示主区名，不阻塞切换菜单）。
    同主区有多个账号时，非第一个加“·profile”后缀区分（如“新加坡·sg1”）。
    """
    region = get_profile_config(name)["region"]
    label = get_region_label(region)
    if _needs_profile_suffix(name):
        label += f"·{name}"
    if region_count and region_count > 1:
        label += f"（{region_count} 个区）"
    return label


def profile_display_name_list(name, region_count=None):
    """账号切换列表里的显示名：主区中文名；订阅了多个区域时后缀
    “、N 个区”（如“新加坡、3 个区”）；只有一个主区时不加后缀。

    与 profile_display_name 的区别只是分隔符（列表里用顿号，菜单
    顶行用括号）；区数同样是实查的订阅区域数（iam
    region-subscription list），不是写死的。
    同主区有多个账号时，非第一个同样加“·profile”后缀区分。
    """
    region = get_profile_config(name)["region"]
    label = get_region_label(region)
    if _needs_profile_suffix(name):
        label += f"·{name}"
    if region_count and region_count > 1:
        label += f"、{region_count} 个区"
    return label

# 机器人日志文件（查询最新日志命令读这个文件）。
LOG_FILE = os.path.expanduser("~/workspace/rbot-clone/telegram-bot.log")
# 快捷开机配置保存文件（JSON）。
PRESETS_FILE = os.path.expanduser("~/workspace/rbot-clone/tgbot/presets.json")
# 数据目录：监控类配置（流量守护 / 停机保活 / SSL 监控）存这里
# （traffic_guard.json / keepalive.json / ssl_watch.json 等）。
# 部署时用 MBOT_TGBOT_DATA_DIR 指到部署路径（如 /opt/rbot-clone/tgbot/data）。
DATA_DIR = os.path.expanduser(os.environ.get(
    "MBOT_TGBOT_DATA_DIR", "~/workspace/rbot-clone/tgbot/data"))
# 区域开机配置（每个账号每个区域的可用域 / 子网 / 镜像 OCID）：JSON 文件，
# 部署时填真实值，不进仓库。格式：
#   {"DEFAULT": {"ap-seoul-1": {"availability_domain": "...",
#                               "subnet_id": "...",
#                               "image_aarch64": "...", "image_x86_64": "...",
#                               "ol_aarch64": "...", "ol_x86_64": "..."}}}
# 按 (profile, region) 双键查：子网/VCN 是按租户的，不同账号同区域的
# 子网 OCID 不同，不能只按区域查。某 (profile, region) 未配时开机回退
# 到环境变量的默认部署值（主区）。
REGION_PLANS_FILE = os.path.expanduser(os.environ.get(
    "MBOT_REGION_PLANS_FILE", os.path.join(DATA_DIR, "region_plans.json")))
# 流量守护的 SSH 私钥：阻断 / 限速 / vnstat 查流量要 SSH 到实例上执行，
# 用这把专用密钥（公钥需加到各实例的 authorized_keys）。未配置时
# 流量守护只能“仅通知”，阻断/限速/解除按钮会提示未配置。
# 部署示例：MBOT_GUARD_SSH_KEY=/root/.ssh/rbot_guard
GUARD_SSH_KEY = os.path.expanduser(os.environ.get(
    "MBOT_GUARD_SSH_KEY", "")) if os.environ.get("MBOT_GUARD_SSH_KEY") else ""
# 流量守护 SSH 端口与用户名（实例标准配置：root + 14422）。
GUARD_SSH_PORT = 14422
GUARD_SSH_USER = "root"
# =====================================================

# 把项目根目录加到 sys.path 开头，保证 `import oci` 解析到本项目
# 的 oci/ 包（而不是系统里可能装的 OCI Python SDK 同名包）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def get_region_plan(profile, region):
    """返回某账号某区域的开机配置 dict；文件不存在、格式不对或该
    (profile, region) 未配时返回 {}（调用方回退到环境变量的默认部署值）。"""
    try:
        with open(REGION_PLANS_FILE, encoding="utf-8") as handle:
            plans = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(plans, dict):
        return {}
    by_region = plans.get(profile)
    if not isinstance(by_region, dict):
        return {}
    plan = by_region.get(region)
    return dict(plan) if isinstance(plan, dict) else {}


def get_token():
    """返回机器人 Token；未配置时抛 RuntimeError（启动时检查）。"""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "未配置 Telegram 机器人 Token：请设置环境变量 MBOT_TELEGRAM_TOKEN "
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
