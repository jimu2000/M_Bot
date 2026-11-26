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
# Panel listen address and port (default 9527). 面板监听地址与端口（默认 9527）。
HOST = "127.0.0.1"
PORT = 9527

# Session key: read from an env var; when unset, a random one is generated at startup with a warning. 会话密钥：从环境变量读；未设置时启动时随机生成并警告。
SESSION_SECRET = os.environ.get("MBOT_WEB_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    warnings.warn(
        "未设置环境变量 MBOT_WEB_SECRET，会话密钥为随机值，"
        "重启面板后所有已登录会话失效。生产部署请固定该值。")

# Admin username: read from an env var, default admin. 管理员用户名：从环境变量读，默认 admin。
ADMIN_USERNAME = os.environ.get("MBOT_WEB_USER", "admin")
# Admin password hash: read from an env var; empty by default = unset. 管理员密码哈希：从环境变量读，默认空 = 未设置。
# How to generate it: python3 -m web.auth (enter the password interactively; it prints the hash). 生成方法：python3 -m web.auth（交互输入密码后打印哈希）。
# When unset, create_app() refuses to start. 未设置时 create_app() 会拒绝启动。
ADMIN_PASSWORD_HASH = os.environ.get("MBOT_WEB_PASSWORD_HASH", "")

# Login lockout: the account locks after LOCKOUT_THRESHOLD consecutive failures. 登录失败锁定：连续失败 LOCKOUT_THRESHOLD 次后锁定账号。
LOCKOUT_THRESHOLD = 5
# First lockout duration (minutes); each later lockout doubles. 首次锁定时长（分钟），之后每次锁定翻倍。
LOCKOUT_BASE_MINUTES = 1
# Lockout duration cap (minutes, 30 minutes max). 锁定时长上限（分钟，30 分钟封顶）。
LOCKOUT_MAX_MINUTES = 30

# Login captcha switch: off by default, login flow unchanged. 登录图形验证码开关：默认关闭，登录流程不变。
# To enable: set env var MBOT_CAPTCHA_ENABLED=1; the login page shows a captcha, and POST /api/auth/login needs captchaId + captchaAnswer. 开启：设环境变量 MBOT_CAPTCHA_ENABLED=1，登录页显示图形验证码，POST /api/auth/login 需带 captchaId + captchaAnswer。
CAPTCHA_ENABLED = os.environ.get("MBOT_CAPTCHA_ENABLED", "") == "1"

# Demo mode: set env var MBOT_DEMO_MODE=1 to serve simulated data. 演示模式开关：设环境变量 MBOT_DEMO_MODE=1 后面板返回模拟数据。
# In demo mode: demo/demo logs in (the real admin login still works), all
# data endpoints return demo tenants/instances/volumes/quota/backups/tasks,
# and write operations return a fake success without calling OCI or changing
# any real state. Non-demo behavior is unchanged. 演示模式下：demo/demo 可登录
# （真实管理员登录仍可用），全部数据接口返回演示租户/实例/启动盘/配额/
# 备份/任务，写操作只返回假成功，不调 OCI、不改真实状态。非演示模式不受影响。
DEMO_MODE = os.environ.get("MBOT_DEMO_MODE", "") == "1"

# Microsoft Clarity project ID, for login-page analytics. Empty string = do not load Clarity. Microsoft Clarity 项目 ID：登录页统计用。空字符串 = 不加载 Clarity。
# Create a project at clarity.microsoft.com, then put its ID in env var MBOT_CLARITY_ID; never hardcode any project ID in the public repo. 在 clarity.microsoft.com 建项目后把 ID 填进环境变量 MBOT_CLARITY_ID；公开仓库里不硬编码任何项目 ID。
CLARITY_ID = os.environ.get("MBOT_CLARITY_ID", "")

# Matomo (self-hosted analytics) tracker base URL and site ID. Both must be
# set for the Matomo snippet to load; empty = do not load Matomo.
# Matomo（自建统计）追踪地址与站点 ID，两项都配了才加载统计代码；为空 = 不加载。
# Example: MBOT_MATOMO_URL=https://matomo.example.com/ MBOT_MATOMO_SITE_ID=1
# Never hardcode a tracker URL or site ID in the public repo. 公开仓库里不硬编码追踪地址与站点 ID。
_MATOMO_URL = os.environ.get("MBOT_MATOMO_URL", "").rstrip("/")
MATOMO_URL = (_MATOMO_URL + "/") if _MATOMO_URL else ""
MATOMO_SITE_ID = os.environ.get("MBOT_MATOMO_SITE_ID", "")

# Panel public URL (e.g. https://panel.example.com): used to build the Telegram one-click login link. 面板公网地址（如 https://panel.example.com）：Telegram 一键登录链接用它拼地址。
# When empty, build from the request's own Host (the reverse proxy must forward Host and X-Forwarded-Proto correctly, or the link may come out http or an internal address). 为空时用请求自身的 Host 拼（反代需正确透传 Host 与 X-Forwarded-Proto，否则链接可能是 http 或内网地址）。
PANEL_URL = os.environ.get("MBOT_PANEL_URL", "").rstrip("/")

# Login session lifetime (hours). 登录会话有效期（小时）。
SESSION_EXPIRE_HOURS = 12
# Session lifetime with "remember me" checked (days): server session and cookie are extended together. 勾选“记住我”时会话有效期（天）：服务端会话与 cookie 同时延长。
REMEMBER_ME_DAYS = 30
# Session cookie name. 会话 cookie 名。
SESSION_COOKIE = "mbot_session"

# Data directory: stores hosts.json (saved SSH hosts) and tasks.json (task records). 数据目录：存 hosts.json（已保存 SSH 主机）与 tasks.json（任务记录）。
# At deploy time point MBOT_WEB_DATA_DIR at the deploy path (e.g. /opt/rbot-clone/web/data). 部署时用 MBOT_WEB_DATA_DIR 指到部署路径（如 /opt/rbot-clone/web/data）。
DATA_DIR = os.path.expanduser(os.environ.get(
    "MBOT_WEB_DATA_DIR", "~/workspace/rbot-clone/web/data"))

# Env file: a password change writes the new hash here (systemd reads it via EnvironmentFile; takes effect after a service restart). The deploy path is /root/.rbot-clone.env (when running as root, ~ is /root); in development MBOT_WEB_ENV_FILE can point elsewhere. 环境变量文件：改密码时把新哈希写到这里（systemd 经 EnvironmentFile 读入，重启服务后生效）。部署路径是 /root/.rbot-clone.env（root 运行时 ~ 就是 /root）；开发时可用 MBOT_WEB_ENV_FILE 指到别处。
ENV_FILE = os.environ.get(
    "MBOT_WEB_ENV_FILE", os.path.expanduser("~/.rbot-clone.env"))

# Grabber-plan scheduler switch: set MBOT_ENABLE_PLAN_SCHEDULER=1 to start APScheduler when the panel starts (runs grabber plans from grabber_plans.json on schedule). 抢机计划调度器开关：设 MBOT_ENABLE_PLAN_SCHEDULER=1 才在面板启动时启动 APScheduler（按 grabber_plans.json 里的计划定时抢机）。
# Off by default (tests and deploys without scheduled grabbing are unaffected). 默认关闭（测试与不需要定时抢机的部署不受影响）。
ENABLE_PLAN_SCHEDULER = os.environ.get(
    "MBOT_ENABLE_PLAN_SCHEDULER", "") == "1"

# Default OCI profile (a profile name in the OCI config file). 默认 OCI profile（OCI 配置文件里的 profile 名）。
DEFAULT_PROFILE = "DEFAULT"
# Default region. 默认区域。
DEFAULT_REGION = "ap-singapore-1"
# Default compartment OCID (placeholder; the root compartment is the tenancy OCID). 默认 compartment OCID（占位符；根 compartment 即 tenancy OCID）。
DEFAULT_COMPARTMENT_ID = os.environ.get(
    "MBOT_COMPARTMENT_DEFAULT", "ocid1.tenancy.oc1..xxxxxxxx")

# Multi-account config: profile name -> {region, compartment_id}. 多账号配置：profile 名 -> {region, compartment_id}。
# Keep in sync with tgbot/config.py _build_profiles: register via env var MBOT_COMPARTMENT_<PROFILE> (value is the compartment OCID; the root compartment is the tenancy OCID); the region is read from that profile's region field in the config file, falling back to the legacy table (old accounts) or DEFAULT_REGION (new accounts). 与 tgbot/config.py 的 _build_profiles 保持同步：注册方式是环境变量 MBOT_COMPARTMENT_<PROFILE>（值是 compartment OCID，根 compartment 即 tenancy OCID）；region 从配置文件里该 profile 的 region 字段读，读不到时用回退表（旧账号）或 DEFAULT_REGION（新账号）。
# Profiles with no MBOT_COMPARTMENT_* in env are still added after deriving their compartment from the OCI config's tenancy (a clean install works by just copying oci_config); an env value that is a placeholder counts as unconfigured and is likewise derived from the tenancy. env 没配 MBOT_COMPARTMENT_* 的 profile 也会从 OCI 配置的 tenancy 推导 compartment 后加入（干净安装后复制 oci_config 即可用）；env 值是占位符时当成未配置，同样从 tenancy 推导。
LEGACY_PROFILE_REGIONS = {
    "DEFAULT": "ap-singapore-1",
    "ACCOUNT2": "us-sanjose-1",
    "ACCOUNT3": "us-chicago-1",
}


# Placeholder compartment (an invalid value written into the env file by an old install.sh; treated as unconfigured and derived from the OCI config's tenancy). 占位符 compartment（install.sh 旧版写进 env 文件的无效值；当成未配置，从 OCI 配置的 tenancy 推导）。
PLACEHOLDER_COMPARTMENT_ID = "ocid1.tenancy.oc1..xxxxxxxx"


def _resolve_compartment(profile_name, file_profiles, env_value=""):
    """解析某 profile 的 compartment OCID。

    优先级：env 里 MBOT_COMPARTMENT_* 的真实值 > 该 profile 在 OCI
    配置文件里的 tenancy（根 compartment 即 tenancy）> ""（解析不出）。
    env 值是占位符或空时当成未配置。
    """
    if env_value and env_value != PLACEHOLDER_COMPARTMENT_ID:
        return env_value
    return (file_profiles.get(profile_name, {}) or {}).get("tenancy", "") or ""


def _build_profiles():
    from oci.client import read_oci_config_profiles
    file_profiles = read_oci_config_profiles()
    profiles = {}
    # DEFAULT is always present: env first (a placeholder is invalid), otherwise derive from the config file's tenancy; when neither works, keep the placeholder (same as the old behavior; the caller reports a config error). DEFAULT 一定在：env 优先（占位符无效），否则从配置文件 tenancy 推导；都推不出来时留占位符（与旧行为一致，调用方会报配置错误）。
    default_comp = _resolve_compartment(
        "DEFAULT", file_profiles,
        os.environ.get("MBOT_COMPARTMENT_DEFAULT", ""))
    profiles["DEFAULT"] = {
        "region": file_profiles.get("DEFAULT", {}).get("region")
                  or LEGACY_PROFILE_REGIONS["DEFAULT"],
        "compartment_id": default_comp or PLACEHOLDER_COMPARTMENT_ID,
    }
    # Other accounts with an explicitly configured compartment in env. env 显式配了 compartment 的其他账号。
    for key, value in sorted(os.environ.items()):
        if not key.startswith("MBOT_COMPARTMENT_") or not value:
            continue
        name = key[len("MBOT_COMPARTMENT_"):]
        if name == "DEFAULT":
            continue
        comp = _resolve_compartment(name, file_profiles, value)
        if not comp:
            continue
        profiles[name] = {
            "region": file_profiles.get(name, {}).get("region")
                      or LEGACY_PROFILE_REGIONS.get(name, DEFAULT_REGION),
            "compartment_id": comp,
        }
    # Accounts present in the config file but without a compartment in env: derive from the tenancy and add (a clean install with oci_config copied just works for multi-account; sections with no tenancy, like [cloudflare], yield no compartment and are skipped). 配置文件里有、但 env 没配 compartment 的账号：从 tenancy 推导补上（干净安装后复制 oci_config，多账号场景直接可用；没 tenancy 的段（如 [cloudflare]）解析不出 compartment，跳过）。
    for name in sorted(file_profiles):
        if name in profiles:
            continue
        comp = _resolve_compartment(name, file_profiles, "")
        if not comp:
            continue
        profiles[name] = {
            "region": file_profiles.get(name, {}).get("region")
                      or LEGACY_PROFILE_REGIONS.get(name, DEFAULT_REGION),
            "compartment_id": comp,
        }
    return profiles


PROFILES = _build_profiles()


def reload_profiles():
    """重新读取账号表（删除账号后让面板列表立即更新，不用重启）。

    只重读当前进程的环境变量与配置文件；机器人是独立进程，
    仍需重启才生效。
    """
    PROFILES.clear()
    PROFILES.update(_build_profiles())

# Region display names (the frontend profile dropdown shows "name · region display name"; regions not listed here show the raw region code). Model: a region is just a display field each profile carries; switching account/region is switching profiles, there is no standalone region selector. 区域中文名（前端 profile 下拉显示“名字 · 区域中文名”用；未列出的区域显示原始区域代码）。模型：区域只是每个 profile 自带的一个显示字段，账号/区域切换就是切换 profile，没有独立的区域选择器。
REGION_LABELS = {
    "ap-singapore-1": "新加坡",
    "ap-seoul-1": "首尔",
    "us-sanjose-1": "圣何塞",
    "us-chicago-1": "芝加哥",
}

# System image OCID for instance creation (looked up by architecture; after the create form's architecture dropdown is chosen, the backend resolves the real image OCID from this table and passes it to the OCI CLI). 创建实例用的系统镜像 OCID（按架构查表；创建表单下拉选架构后，后端按这张表解析成真实镜像 OCID 再传给 OCI CLI）。
# At deploy time fill the real values via env vars MBOT_IMAGE_AARCH64 / MBOT_IMAGE_X86_64. 部署时用环境变量 MBOT_IMAGE_AARCH64 / MBOT_IMAGE_X86_64 填真实值。
# How to find the real image OCID (replace <compartment> with your compartment OCID): 查真实镜像 OCID 的方法（替换 <compartment> 为你的 compartment OCID）：
#   oci compute image list --compartment-id <compartment> \
#     --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
#     --shape "VM.Standard.A1.Flex" --query 'data[].{id:id, name:"display-name"}'
IMAGE_IDS = {
    # ARM architecture (for A1.Flex): Canonical Ubuntu 24.04 aarch64 image OCID. ARM 架构（A1.Flex 用）：Canonical Ubuntu 24.04 aarch64 镜像 OCID。
    "aarch64": os.environ.get(
        "MBOT_IMAGE_AARCH64", "ocid1.image.oc1..xxxxxxxx"),
    # x86_64 architecture (for E2.1.Micro): Canonical Ubuntu 24.04 x86_64 image OCID. x86_64 架构（E2.1.Micro 用）：Canonical Ubuntu 24.04 x86_64 镜像 OCID。
    "x86_64": os.environ.get(
        "MBOT_IMAGE_X86_64", "ocid1.image.oc1..xxxxxxxx"),
}
# =====================================================

# Prepend the project root to sys.path so `import oci` resolves to this project's oci/ package (not a same-named OCI Python SDK that may be installed system-wide). 把项目根目录加到 sys.path 开头，保证 `import oci` 解析到本项目的 oci/ 包（而不是系统里可能装的 OCI Python SDK 同名包）。
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
