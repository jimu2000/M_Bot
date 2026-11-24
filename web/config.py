#!/usr/bin/env python3
"""Web 面板配置。

配置区在文件顶端，方便修改。真实 OCID / 密码一律不写进代码：
  - 管理员密码哈希从环境变量 MBOT_WEB_PASSWORD_HASH 读
    （用 `python3 -m web.auth` 生成；未设置时面板拒绝启动）。
  - 会话密钥从环境变量 MBOT_WEB_SECRET 读；未设置时启动时随机生成
    （重启后旧会话全部失效，生产部署请固定这个值）。
  - OCI 的 profile / region / compartment 在下面 PROFILES 里配，
    真实 OCID 用占位符，部署时改成真实值。

安全说明：
  - 本模块不读取、不打印、不外传 OCI API 私钥。认证全部由 OCI CLI
    自己读配置文件（~/oci_config，旧部署回退 ~/.oci/config）与 key_file 完成。
  - 面板默认只监听 127.0.0.1（本机访问）；要从外网访问请自行在前面
    加反向代理 + HTTPS，不要直接把 9527 暴露到公网。
"""

import os
import secrets
import sys
import warnings

# ================= 配置区（改这里） =================
# 面板监听地址与端口（默认 9527）。
HOST = "127.0.0.1"
PORT = 9527

# 会话密钥：从环境变量读；未设置时启动时随机生成并警告。
SESSION_SECRET = os.environ.get("MBOT_WEB_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    warnings.warn(
        "未设置环境变量 MBOT_WEB_SECRET，会话密钥为随机值，"
        "重启面板后所有已登录会话失效。生产部署请固定该值。")

# 管理员用户名：从环境变量读，默认 admin。
ADMIN_USERNAME = os.environ.get("MBOT_WEB_USER", "admin")
# 管理员密码哈希：从环境变量读，默认空 = 未设置。
# 生成方法：python3 -m web.auth（交互输入密码后打印哈希）。
# 未设置时 create_app() 会拒绝启动。
ADMIN_PASSWORD_HASH = os.environ.get("MBOT_WEB_PASSWORD_HASH", "")

# 登录失败锁定：连续失败 LOCKOUT_THRESHOLD 次后锁定账号。
LOCKOUT_THRESHOLD = 5
# 首次锁定时长（分钟），之后每次锁定翻倍。
LOCKOUT_BASE_MINUTES = 1
# 锁定时长上限（分钟，30 分钟封顶）。
LOCKOUT_MAX_MINUTES = 30

# 登录图形验证码开关（阶段 5 工作包 E）：默认关闭，登录流程不变。
# 开启：设环境变量 MBOT_CAPTCHA_ENABLED=1，登录页显示图形验证码，
# POST /api/auth/login 需带 captchaId + captchaAnswer。
CAPTCHA_ENABLED = os.environ.get("MBOT_CAPTCHA_ENABLED", "") == "1"

# Clicky 站点 ID：登录页统计用。空字符串 = 不加载 Clicky。
# 在 clicky.com 建站点后把 ID 填进环境变量 MBOT_CLICKY_ID；
# 公开仓库里不硬编码任何站点 ID。
CLICKY_ID = os.environ.get("MBOT_CLICKY_ID", "")

# 面板公网地址（如 https://panel.example.com）：Telegram 一键登录链接
# 用它拼地址。为空时用请求自身的 Host 拼（反代需正确透传 Host 与
# X-Forwarded-Proto，否则链接可能是 http 或内网地址）。
PANEL_URL = os.environ.get("MBOT_PANEL_URL", "").rstrip("/")

# 登录会话有效期（小时）。
SESSION_EXPIRE_HOURS = 12
# 勾选“记住我”时会话有效期（天）：服务端会话与 cookie 同时延长。
REMEMBER_ME_DAYS = 30
# 会话 cookie 名。
SESSION_COOKIE = "mbot_session"

# 数据目录：存 hosts.json（已保存 SSH 主机）与 tasks.json（任务记录）。
# 部署时用 MBOT_WEB_DATA_DIR 指到部署路径（如 /opt/rbot-clone/web/data）。
DATA_DIR = os.path.expanduser(os.environ.get(
    "MBOT_WEB_DATA_DIR", "~/workspace/rbot-clone/web/data"))

# 环境变量文件：改密码时把新哈希写到这里（systemd 经 EnvironmentFile
# 读入，重启服务后生效）。部署路径是 /root/.rbot-clone.env（root 运行
# 时 ~ 就是 /root）；开发时可用 MBOT_WEB_ENV_FILE 指到别处。
ENV_FILE = os.environ.get(
    "MBOT_WEB_ENV_FILE", os.path.expanduser("~/.rbot-clone.env"))

# 抢机计划调度器开关：设 MBOT_ENABLE_PLAN_SCHEDULER=1 才在面板启动时
# 启动 APScheduler（按 grabber_plans.json 里的计划定时抢机）。
# 默认关闭（测试与不需要定时抢机的部署不受影响）。
ENABLE_PLAN_SCHEDULER = os.environ.get(
    "MBOT_ENABLE_PLAN_SCHEDULER", "") == "1"

# 默认 OCI profile（OCI 配置文件里的 profile 名）。
DEFAULT_PROFILE = "DEFAULT"
# 默认区域。
DEFAULT_REGION = "ap-singapore-1"
# 默认 compartment OCID（占位符；根 compartment 即 tenancy OCID）。
DEFAULT_COMPARTMENT_ID = os.environ.get(
    "MBOT_COMPARTMENT_DEFAULT", "ocid1.tenancy.oc1..xxxxxxxx")

# 多账号配置：profile 名 -> {region, compartment_id}。
# 与 tgbot/config.py 的 _build_profiles 保持同步：注册方式是环境变量
# MBOT_COMPARTMENT_<PROFILE>（值是 compartment OCID，根 compartment
# 即 tenancy OCID）；region 从配置文件里该 profile 的 region 字段读，
# 读不到时用回退表（旧账号）或 DEFAULT_REGION（新账号）。
# 未设 MBOT_COMPARTMENT_* 的 profile 不加入 PROFILES。
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

# 区域中文名（前端 profile 下拉显示"名字 · 区域中文名"用；未列出的区域
# 显示原始区域代码）。模型：区域只是每个 profile 自带的一个显示字段，
# 账号/区域切换就是切换 profile，没有独立的区域选择器。
REGION_LABELS = {
    "ap-singapore-1": "新加坡",
    "ap-seoul-1": "首尔",
    "us-sanjose-1": "圣何塞",
    "us-chicago-1": "芝加哥",
}

# 创建实例用的系统镜像 OCID（按架构查表；创建表单下拉选架构后，
# 后端按这张表解析成真实镜像 OCID 再传给 OCI CLI）。
# 部署时用环境变量 MBOT_IMAGE_AARCH64 / MBOT_IMAGE_X86_64 填真实值。
# 查真实镜像 OCID 的方法（替换 <compartment> 为你的 compartment OCID）：
#   oci compute image list --compartment-id <compartment> \
#     --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
#     --shape "VM.Standard.A1.Flex" --query 'data[].{id:id, name:"display-name"}'
# A1.Flex（ARM）与 E2.1.Micro（x86）要分别查，各填一条。
IMAGE_IDS = {
    # ARM 架构（A1.Flex 用）：Canonical Ubuntu 24.04 aarch64 镜像 OCID。
    "aarch64": os.environ.get(
        "MBOT_IMAGE_AARCH64", "ocid1.image.oc1..xxxxxxxx"),
    # x86_64 架构（E2.1.Micro 用）：Canonical Ubuntu 24.04 x86_64 镜像 OCID。
    "x86_64": os.environ.get(
        "MBOT_IMAGE_X86_64", "ocid1.image.oc1..xxxxxxxx"),
}
# =====================================================

# 把项目根目录加到 sys.path 开头，保证 `import oci` 解析到本项目
# 的 oci/ 包（而不是系统里可能装的 OCI Python SDK 同名包）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


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


def get_region_label(region):
    """返回区域的中文名；未列出的区域返回原始区域代码。"""
    return REGION_LABELS.get(region, region)


def _needs_profile_suffix(name):
    """同主区的已注册账号里，自己是不是第一个（按 PROFILES 顺序）。

    不是第一个时显示名要加“·profile”区分（与 tgbot/config.py 的
    同名函数保持一致）。
    """
    region = get_profile_config(name)["region"]
    for other, cfg in PROFILES.items():
        if cfg.get("region") == region:
            return other != name
    return False


def profile_display_name(name, region_count=None):
    """账号（profile）的显示名：主区中文名；订阅了多个区域时后缀（N 个区）。

    与 tgbot/config.py 的同名函数保持一致（机器人切换菜单与面板
    下拉用同一套显示名；用户要求 2026-11-24：bot 改完的网页版也要
    相应调整）。region_count 为 None 或 <=1 时不加后缀。
    同主区有多个账号时，非第一个加“·profile”后缀区分（如“新加坡·sg1”）。
    """
    region = get_profile_config(name)["region"]
    label = get_region_label(region)
    if _needs_profile_suffix(name):
        label += f"·{name}"
    if region_count and region_count > 1:
        label += f"（{region_count} 个区）"
    return label
