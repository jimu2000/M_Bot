#!/usr/bin/env python3
"""Web 面板配置。

配置区在文件顶端，方便修改。真实 OCID / 密码一律不写进代码：
  - 管理员密码哈希从环境变量 RBOT_WEB_PASSWORD_HASH 读
    （用 `python3 -m web.auth` 生成；未设置时面板拒绝启动）。
  - 会话密钥从环境变量 RBOT_WEB_SECRET 读；未设置时启动时随机生成
    （重启后旧会话全部失效，生产部署请固定这个值）。
  - OCI 的 profile / region / compartment 在下面 PROFILES 里配，
    真实 OCID 用占位符，部署时改成真实值。

安全说明：
  - 本模块不读取、不打印、不外传 OCI API 私钥。认证全部由 OCI CLI
    自己读 ~/.oci/config 与 key_file 完成。
  - 面板默认只监听 127.0.0.1（本机访问）；要从外网访问请自行在前面
    加反向代理 + HTTPS，不要直接把 9527 暴露到公网。
"""

import os
import secrets
import sys
import warnings

# ================= 配置区（改这里） =================
# 面板监听地址与端口（R-Bot 默认也是 9527）。
HOST = "127.0.0.1"
PORT = 9527

# 会话密钥：从环境变量读；未设置时启动时随机生成并警告。
SESSION_SECRET = os.environ.get("RBOT_WEB_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    warnings.warn(
        "未设置环境变量 RBOT_WEB_SECRET，会话密钥为随机值，"
        "重启面板后所有已登录会话失效。生产部署请固定该值。")

# 管理员用户名：从环境变量读，默认 admin。
ADMIN_USERNAME = os.environ.get("RBOT_WEB_USER", "admin")
# 管理员密码哈希：从环境变量读，默认空 = 未设置。
# 生成方法：python3 -m web.auth（交互输入密码后打印哈希）。
# 未设置时 create_app() 会拒绝启动。
ADMIN_PASSWORD_HASH = os.environ.get("RBOT_WEB_PASSWORD_HASH", "")

# 登录失败锁定：连续失败 LOCKOUT_THRESHOLD 次后锁定账号。
LOCKOUT_THRESHOLD = 5
# 首次锁定时长（分钟），之后每次锁定翻倍。
LOCKOUT_BASE_MINUTES = 1
# 锁定时长上限（分钟，与 R-Bot 的 30 分钟封顶一致）。
LOCKOUT_MAX_MINUTES = 30

# 登录图形验证码开关（阶段 5 工作包 E）：默认关闭，登录流程不变。
# 开启：设环境变量 RBOT_CAPTCHA_ENABLED=1，登录页显示图形验证码，
# POST /api/auth/login 需带 captchaId + captchaAnswer。
CAPTCHA_ENABLED = os.environ.get("RBOT_CAPTCHA_ENABLED", "") == "1"

# 登录会话有效期（小时）。
SESSION_EXPIRE_HOURS = 12
# 会话 cookie 名。
SESSION_COOKIE = "rbot_session"

# 数据目录：存 hosts.json（已保存 SSH 主机）与 tasks.json（任务记录）。
# 部署时用 RBOT_WEB_DATA_DIR 指到部署路径（如 /opt/rbot-clone/web/data）。
DATA_DIR = os.path.expanduser(os.environ.get(
    "RBOT_WEB_DATA_DIR", "~/workspace/rbot-clone/web/data"))

# 环境变量文件：改密码时把新哈希写到这里（systemd 经 EnvironmentFile
# 读入，重启服务后生效）。部署路径是 /root/.rbot-clone.env（root 运行
# 时 ~ 就是 /root）；开发时可用 RBOT_WEB_ENV_FILE 指到别处。
ENV_FILE = os.environ.get(
    "RBOT_WEB_ENV_FILE", os.path.expanduser("~/.rbot-clone.env"))

# 抢机计划调度器开关：设 RBOT_ENABLE_PLAN_SCHEDULER=1 才在面板启动时
# 启动 APScheduler（按 grabber_plans.json 里的计划定时抢机）。
# 默认关闭（测试与不需要定时抢机的部署不受影响）。
ENABLE_PLAN_SCHEDULER = os.environ.get(
    "RBOT_ENABLE_PLAN_SCHEDULER", "") == "1"

# 默认 OCI profile（~/.oci/config 里的 profile 名）。
DEFAULT_PROFILE = "DEFAULT"
# 默认区域。
DEFAULT_REGION = "ap-singapore-1"
# 默认 compartment OCID（占位符；根 compartment 即 tenancy OCID）。
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

# 区域中文名（前端 profile 下拉显示"名字 · 区域中文名"用；未列出的区域
# 显示原始区域代码）。R-Bot 模型：区域只是每个 profile 自带的一个显示字段，
# 账号/区域切换就是切换 profile，没有独立的区域选择器。
REGION_LABELS = {
    "ap-singapore-1": "新加坡",
    "us-sanjose-1": "圣何塞",
    "us-chicago-1": "芝加哥",
}

# 创建实例用的系统镜像 OCID（按架构查表；创建表单下拉选架构后，
# 后端按这张表解析成真实镜像 OCID 再传给 OCI CLI）。
# 部署时用环境变量 RBOT_IMAGE_AARCH64 / RBOT_IMAGE_X86_64 填真实值。
# 查真实镜像 OCID 的方法（替换 <compartment> 为你的 compartment OCID）：
#   oci compute image list --compartment-id <compartment> \
#     --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
#     --shape "VM.Standard.A1.Flex" --query 'data[].{id:id, name:"display-name"}'
# A1.Flex（ARM）与 E2.1.Micro（x86）要分别查，各填一条。
IMAGE_IDS = {
    # ARM 架构（A1.Flex 用）：Canonical Ubuntu 24.04 aarch64 镜像 OCID。
    "aarch64": os.environ.get(
        "RBOT_IMAGE_AARCH64", "ocid1.image.oc1..xxxxxxxx"),
    # x86_64 架构（E2.1.Micro 用）：Canonical Ubuntu 24.04 x86_64 镜像 OCID。
    "x86_64": os.environ.get(
        "RBOT_IMAGE_X86_64", "ocid1.image.oc1..xxxxxxxx"),
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
