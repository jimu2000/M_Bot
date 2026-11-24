#!/usr/bin/env python3
"""Telegram 命令菜单与新增命令。

命令：
  - 其他云：/azure /aws /gcp /do /vf /solusvm（查配置，未配提示配置方法；
    已配且模块支持时列实例；各云模块目前都是 stub，列表接口待实现）
  - /oci：上传 OCI 配置（或 Azure 配置）文件
  - /clearlock：清除面板登录失败锁定（重启面板服务）
  - /raninfo：重置面板管理员密码（随机新密码只发一次）
  - /me：我的信息（TG 身份 / 面板版本 / OCI 账号与实例数）
  - /admin：管理员菜单（面板状态 / 重启服务 / 版本）

命令菜单（Telegram 客户端点"菜单"按钮看到的列表）由 BOT_COMMANDS 定义，
bot 启动时经 post_init 调 setMyCommands 注册（见 tgbot/bot.py）。
菜单里的每个命令都必须有对应的 CommandHandler（测试
tests/test_tgbot_menu.py 保证两者一致，不许出现点了没反应的命令）。

不做（设计上跳过）：/bindclient /qrcode /codeflash /flash /migrate /
getflash /qb /qs /lang（闪电付费、客服专线、多语言）。

安全说明：
  - 全部 handler 都加 @require_allowed，只允许白名单用户。
  - /oci 只接受 config 文本；私钥文件（key_file 指向的 .pem）绝不经过
    Telegram，用户自行 scp 到服务器对应路径。
  - /raninfo 生成的新密码只在回复里出现一次；哈希用 web.auth 的
    pbkdf2_sha256（与面板 verify_password 配套），明文只写进
    ADMIN_PASSWORD_FILE（600 权限）。
  - 本文件不含真实 Token / 密码 / OCID / IP。
"""

import logging
import os
import secrets
import string
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from clouds import (AWSProvider, AzureProvider, DOProvider, GCPProvider,
                    SolusVMProvider, VirtFusionProvider)
from oci.instance import list_instances
from tgbot import config as bot_config
from tgbot import menu_tracker
from tgbot.client_ops import (BOT_SERVICE, WEB_SERVICE, restart_service,
                              service_status)
from tgbot.config import update_env_file_vars
from tgbot.handlers_client import (
    OCI_CONFIG_PATH, add_profile_compartments,
    build_oci_profiles_confirm_text, build_oci_profiles_result_text,
    commit_cf_section, commit_oci_profiles, parse_oci_config_text,
    prepare_oci_profiles, _has_section_ci)
from tgbot.services import fmt_error, require_allowed, run_sync
from web import config as web_config

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# 项目根目录（VERSION 文件所在目录）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 面板版本文件。
VERSION_FILE = os.path.join(PROJECT_ROOT, "VERSION")
# 面板管理员明文密码文件（/raninfo 写这里，600 权限；部署时是
# /root/.rbot-clone-admin-password，机器人以 root 运行所以 ~ 就是 /root）。
ADMIN_PASSWORD_FILE = os.environ.get(
    "MBOT_WEB_ADMIN_PASSWORD_FILE",
    os.path.expanduser("~/.rbot-clone-admin-password"))
# /raninfo 生成的新密码长度（字母+数字，满足面板密码强度要求）。
RANINFO_PASSWORD_LENGTH = 20
# =====================================================

# ---------------- 命令菜单 ----------------
# (命令, 中文说明)：启动时注册到 Telegram 菜单（setMyCommands）。
# 文案照菜单来；/me 改成"查看我的信息"，不提闪电权益。
BOT_COMMANDS = [
    ("start", "欢迎与主菜单"),
    ("help", "获取帮助"),
    ("oracle", "Oracle 云管理"),
    ("instances", "查看实例列表"),
    ("bark", "Bark 通知设置"),
    ("domain", "域名管理"),
    ("azure", "Azure 云管理"),
    ("aws", "AWS 云管理"),
    ("gcp", "GCP 云管理"),
    ("do", "DigitalOcean 实例管理"),
    ("vf", "VirtFusion 实例管理"),
    ("solusvm", "SolusVM 面板管理"),
    ("oci", "上传甲骨文或 azure 配置"),
    ("command", "当前客户端执行 shell 命令"),
    ("pubkey", "添加 ssh 公钥"),
    ("uploadapi", "添加 api"),
    ("oproxy", "添加代理"),
    ("clearlock", "清除密码错误过多导致的锁定"),
    ("raninfo", "重置随机用户信息"),
    ("me", "查看我的信息"),
    ("admin", "管理员指令"),
]

# 故意不进菜单的命令（闪电付费 / 客服专线 / 多语言），测试保证它们
# 不出现在 BOT_COMMANDS 里。
SKIPPED_COMMANDS = ("bindclient", "qrcode", "codeflash", "flash",
                    "migrate", "getflash", "qb", "qs", "lang")


async def _reply(update, context, text, reply_markup=None,
               parse_mode=None):
    """统一回复：tgbot.menu_tracker.reply 的薄包装（带选择框清理，
    每聊天只留一个活动选择框；点完删掉多余的选择框，只留结果）。"""
    return await menu_tracker.reply(
        update, context, text, reply_markup=reply_markup,
        parse_mode=parse_mode)


# ================= 其他云：/azure /aws /gcp /do /vf /solusvm =================

# 各云配置说明：display 展示名、provider_cls 模块类、env_vars 必填环境变量。
PROVIDER_SPECS = {
    "azure": {
        "display": "Azure",
        "provider_cls": AzureProvider,
        "env_vars": ("AZURE_SUBSCRIPTION_ID", "AZURE_TENANT_ID",
                     "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"),
    },
    "aws": {
        "display": "AWS",
        "provider_cls": AWSProvider,
        "env_vars": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    },
    "gcp": {
        "display": "GCP",
        "provider_cls": GCPProvider,
        "env_vars": ("GOOGLE_APPLICATION_CREDENTIALS",),
    },
    "do": {
        "display": "DigitalOcean",
        "provider_cls": DOProvider,
        "env_vars": ("DIGITALOCEAN_TOKEN",),
    },
    "vf": {
        "display": "VirtFusion",
        "provider_cls": VirtFusionProvider,
        "env_vars": ("VIRT_FUSION_API_URL", "VIRT_FUSION_API_TOKEN"),
    },
    "solusvm": {
        "display": "SolusVM",
        "provider_cls": SolusVMProvider,
        "env_vars": ("SOLUSVM_API_URL", "SOLUSVM_API_KEY",
                     "SOLUSVM_API_HASH"),
    },
}


def _missing_env_vars(env_vars):
    """env_vars 里未设置（或为空）的变量名列表。"""
    return [var for var in env_vars if not os.environ.get(var)]


def _fmt_cloud_instances(display, instances):
    """实例列表文案（clouds/base.py 的统一 snake_case 字段）。"""
    lines = [f"☁️ {display} 实例（{len(instances)} 台）：", ""]
    for inst in instances:
        name = inst.get("name") or inst.get("id") or "?"
        shape = inst.get("shape") or "?"
        state = inst.get("state") or "?"
        ip = inst.get("public_ip") or "无公网 IP"
        lines.append(f"• {name}（{shape}）{state} {ip}")
    return "\n".join(lines)


async def _handle_cloud_command(update, context, provider_name):
    """其他云命令的通用流程：查配置 → 未配提示配置方法 → 已配列实例。

    绝不编造数据：未配置就直说未配置；模块是 stub（方法抛
    NotImplementedError）就直说功能尚未实现。
    """
    spec = PROVIDER_SPECS[provider_name]
    display = spec["display"]
    missing = _missing_env_vars(spec["env_vars"])
    if missing:
        lines = [f"⚠️ 未配置 {display} 账号。", "",
                 "配置方法：在机器人服务器的 env 文件里加以下环境变量，",
                 "然后重启机器人服务：", "",
                 f"env 文件：{web_config.ENV_FILE}"]
        for var in missing:
            lines.append(f"• {var}=...")
        if provider_name == "azure":
            # /oci 只支持 Oracle 与 Azure 两种配置，别家不要提示这一句。
            lines += ["", "已有账号时也可以用 /oci 上传 Azure 配置。"]
        await _reply(update, context, "\n".join(lines))
        return
    provider = spec["provider_cls"]()
    try:
        instances = await run_sync(provider.list_instances)
    except NotImplementedError:
        await _reply(
            update, context,
            f"ℹ️ {display} 账号已配置，但实例列表功能尚未实现：\n"
            f"{display} 模块目前还是 stub，列表 / 创建等接口待开发，"
            "有账号后按真实 API 接入。")
        return
    except Exception as exc:  # noqa: BLE001 - 云 API 异常统一转中文提示
        logger.warning("%s 列实例失败：%s", display, exc)
        await _reply(update, context, fmt_error(exc))
        return
    if not instances:
        await _reply(update, context, f"{display} 账号下没有实例。")
        return
    await _reply(update, context, _fmt_cloud_instances(display, instances))


@require_allowed
async def azure_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /azure：Azure 云管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "azure")


@require_allowed
async def aws_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /aws：AWS 云管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "aws")


@require_allowed
async def gcp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /gcp：GCP 云管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "gcp")


@require_allowed
async def do_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /do：DigitalOcean 实例管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "do")


@require_allowed
async def vf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /vf：VirtFusion 实例管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "vf")


@require_allowed
async def solusvm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /solusvm：SolusVM 面板管理（查配置 / 列实例）。"""
    await _handle_cloud_command(update, context, "solusvm")


# ================= /oci：上传 OCI / Azure 配置 =================

# Azure 配置段名与必填键（config 文件里写 [azure] 段）。
AZURE_SECTION_NAME = "azure"
AZURE_REQUIRED_KEYS = ("subscription_id", "tenant_id", "client_id",
                       "client_secret")
# Azure 键 -> env 文件里的环境变量名。
AZURE_ENV_VARS = {
    "subscription_id": "AZURE_SUBSCRIPTION_ID",
    "tenant_id": "AZURE_TENANT_ID",
    "client_id": "AZURE_CLIENT_ID",
    "client_secret": "AZURE_CLIENT_SECRET",
}


def _clear_oci_pending(context):
    """清 /oci 的等待状态（与 /uploadapi 的状态互斥，避免双重消费）。"""
    context.user_data.pop("oci_awaiting", None)
    context.user_data.pop("oci_pending", None)


@require_allowed
async def oci_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /oci：提示发 config 文件或粘贴文本，进入等待状态。"""
    # 与 /uploadapi 互斥：/oci 接管后 uploadapi 不再消费同一条消息。
    context.user_data.pop("ua_awaiting", None)
    context.user_data.pop("ua_pending", None)
    context.user_data.pop("pending_input", None)
    context.user_data["oci_awaiting"] = True
    await _reply(
        update, context,
        "📤 上传甲骨文或 Azure 配置：\n\n"
        "请发送 config 文件（.ini / .txt / .conf），或直接粘贴 config 文本。\n"
        "支持 R-Bot 格式：oci=begin … oci=end 块里可一次放多个 [PROFILE]，"
        "一次全部导入。\n\n"
        "Oracle 配置格式：每个 [PROFILE] 段含 user / fingerprint / "
        "tenancy / region / key_file 五个键，校验后追加到 "
        f"{OCI_CONFIG_PATH}（root 家目录）。\n\n"
        "Azure 配置格式：含 [azure] 段 + subscription_id / tenant_id / "
        "client_id / client_secret 四个键，校验后写进机器人 env 文件。\n\n"
        "配置里如有 [cloudflare] 段（cf_token，或 cf_key + cf_email），"
        "会一并保存，/domain 与证书 DNS 验证会用它。\n\n"
        "key_file 已在 /root 下的保留原路径（如 /root/1.pem），其余自动"
        "改成 /root/<PROFILE>.pem。\n\n"
        "⚠️ 只传 config 文本；私钥文件（.pem）不经过 Telegram，请自行 "
        "scp 到服务器对应路径（原 key_file 已在服务器上的，写入后自动"
        "复制）。\n\n"
        "只加 OCI API 密钥也可以用 /uploadapi（同样的流程）。")


async def _handle_oci_config_text(update, context, text):
    """待输入状态下的 config 文本：解析 -> 校验 -> 二次确认键盘。"""
    parsed = parse_oci_config_text(text)
    if parsed["error"]:
        await _reply(update, context, f"❌ 配置格式不对：{parsed['error']}\n请重新发送。")
        return
    profile = parsed["profile"]
    fields = parsed["fields"]
    if profile.lower() == AZURE_SECTION_NAME:
        await _handle_oci_azure(update, context, fields)
        return
    new_profiles, skipped, error_text = prepare_oci_profiles(parsed)
    if error_text:
        await _reply(update, context, f"❌ {error_text}")
        return
    cloudflare = parsed.get("cloudflare")
    cf_exists = bool(cloudflare) and _has_section_ci(OCI_CONFIG_PATH,
                                                     "cloudflare")
    if not new_profiles and not (cloudflare and not cf_exists):
        _clear_oci_pending(context)
        await _reply(update, context,
                     f"⚠️ profile「{'、'.join(skipped)}」已存在，不会覆盖。\n"
                     "请改名后重传（改 [PROFILE] 段名）。")
        return
    context.user_data["oci_pending"] = {"kind": "oci",
                                        "profiles": new_profiles,
                                        "cloudflare": cloudflare}
    await _reply(update, context, build_oci_profiles_confirm_text(new_profiles,
                                                         skipped,
                                                         cloudflare,
                                                         cf_exists),
                 reply_markup=InlineKeyboardMarkup([
                     [InlineKeyboardButton("✅ 确认添加",
                                           callback_data="oci:confirm")],
                     [InlineKeyboardButton("❌ 取消", callback_data="x")]]))


async def _handle_oci_azure(update, context, fields):
    """[azure] 段：校验四个键 -> 二次确认（值不回显）。"""
    missing = [key for key in AZURE_REQUIRED_KEYS if not fields.get(key)]
    if missing:
        await _reply(update, context,
                     f"❌ Azure 配置缺少必填键：{'、'.join(missing)}\n"
                     "请补全后重新发送。")
        return
    context.user_data["oci_pending"] = {"kind": "azure", "fields": fields}
    lines = ["📤 确认添加 Azure 配置？", "",
             "将写入机器人 env 文件（4 项）：",
             "• AZURE_SUBSCRIPTION_ID",
             "• AZURE_TENANT_ID",
             "• AZURE_CLIENT_ID",
             "• AZURE_CLIENT_SECRET",
             "",
             "⚠️ 写入后需重启机器人服务才生效。",
             "值不会在聊天里回显。"]
    await _reply(update, context, "\n".join(lines),
                 reply_markup=InlineKeyboardMarkup([
                     [InlineKeyboardButton("✅ 确认添加",
                                           callback_data="oci:confirm")],
                     [InlineKeyboardButton("❌ 取消", callback_data="x")]]))


@require_allowed
async def oci_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """回调 oci:confirm：二次确认后真正写入配置。"""
    query = update.callback_query
    if query is not None:
        await query.answer()
    if query is None or query.data != "oci:confirm":
        await _reply(update, context, "参数错误，请重新发送 /oci。")
        return
    pending = context.user_data.get("oci_pending")
    if not pending:
        await _reply(update, context, "待添加的配置已过期，请重新发送 /oci。")
        return
    if pending["kind"] == "azure":
        await _confirm_oci_azure(update, context, pending)
        return
    profiles = pending.get("profiles") or []
    cloudflare = pending.get("cloudflare")
    if not profiles and not cloudflare:
        await _reply(update, context, "待添加的配置已过期，请重新发送 /oci。")
        return
    results = []
    added_vars = {}
    if profiles:
        try:
            results = await run_sync(commit_oci_profiles, profiles)
        except (ValueError, OSError) as exc:
            await _reply(update, context, f"❌ 写入失败：{exc}")
            return
    cf_saved = None
    if cloudflare:
        try:
            cf_saved = await run_sync(commit_cf_section, cloudflare)
        except OSError as exc:
            await _reply(update, context, f"❌ Cloudflare 凭据写入失败：{exc}")
            return
    if profiles:
        try:
            added_vars = await run_sync(add_profile_compartments, profiles)
        except OSError as exc:
            await _reply(update, context,
                         f"✅ 配置已写入，但账号切换变量添加失败：{exc}\n"
                         "请手动在 env 文件里加 MBOT_COMPARTMENT_<PROFILE> "
                         "后重启机器人。")
            _clear_oci_pending(context)
            return
    _clear_oci_pending(context)
    text = build_oci_profiles_result_text(results, added_vars,
                                          OCI_CONFIG_PATH, cf_saved)
    await _reply(update, context, text + "\n\n生效方法：重启机器人服务"
                 "（/admin 里可重启），之后可在菜单里切换到新账号。")


async def _confirm_oci_azure(update, context, pending):
    """Azure 配置确认写入：4 个值写进 env 文件。"""
    updates = {AZURE_ENV_VARS[key]: pending["fields"][key]
               for key in AZURE_REQUIRED_KEYS}
    try:
        await run_sync(update_env_file_vars, updates)
    except OSError as exc:
        await _reply(update, context, f"❌ 写入失败：{exc}")
        return
    _clear_oci_pending(context)
    await _reply(
        update, context,
        "✅ Azure 配置已写进机器人 env 文件（4 项）。\n\n"
        "生效方法：重启机器人服务（/admin 里可重启），"
        "之后 /azure 会显示已配置；实例列表接口仍在开发中。")


@require_allowed
async def handle_oci_document(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """文件消息路由：只在 /oci 等待上传时处理。

    /uploadapi 等待时优先（ua_awaiting 存在时不消费，避免双重处理）。
    其余情况静默返回。
    """
    if not context.user_data.get("oci_awaiting"):
        return
    if context.user_data.get("ua_awaiting"):
        return
    message = update.effective_message
    document = getattr(message, "document", None) if message else None
    if document is None:
        return
    try:
        tg_file = await document.get_file()
        raw = await tg_file.download_as_bytearray()
        text = bytes(raw).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 下载/解码失败都要提示用户
        await _reply(update, context, f"❌ 文件读取失败：{exc}")
        return
    await _handle_oci_config_text(update, context, text)


@require_allowed
async def handle_oci_text_input(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """纯文本消息路由：只在 /oci 等待粘贴时处理。

    pending_input（改名/重装流程）或 ua_awaiting（/uploadapi）存在时
    跳过，那条文本归别的 handler 处理，避免双重消费。
    无待输入状态时静默返回。
    """
    if context.user_data.get("pending_input"):
        return
    if context.user_data.get("ua_awaiting"):
        return
    if not context.user_data.get("oci_awaiting"):
        return
    message = update.effective_message
    text = (message.text or "") if message is not None else ""
    await _handle_oci_config_text(update, context, text)


# ================= /clearlock：清除面板登录锁定 =================

def clear_login_lockout():
    """清除面板登录失败锁定，返回中文结果文案。

    锁定状态存在面板进程内存里（web/auth.py 的 LoginGuard._records，
    机器人是另一个进程摸不到），所以只能重启面板服务清掉。
    重启失败时抛 RuntimeError（restart_service 的异常原样上抛）。
    """
    restart_service(WEB_SERVICE)
    return ("✅ 已重启 Web 面板服务，登录失败锁定已清除，"
            "可以重新登录了。")


@require_allowed
async def clearlock_command(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """处理 /clearlock：重启面板服务清除登录失败锁定。"""
    await _reply(update, context, "正在重启面板服务以清除登录锁定，请稍等…")
    try:
        result = await run_sync(clear_login_lockout)
    except RuntimeError as exc:
        await _reply(update, context, f"❌ 清除锁定失败：{exc}")
        return
    await _reply(update, context, result)


# ================= /raninfo：重置面板管理员密码 =================

def generate_admin_password(length=RANINFO_PASSWORD_LENGTH):
    """生成随机管理员密码：字母+数字，满足面板密码强度要求。

    面板要求至少 8 位且同时含字母与数字（web/routes/auth.py 的
    validate_new_password）；这里固定 20 位，且保证至少一位字母、
    一位数字。
    """
    alphabet = string.ascii_letters + string.digits
    while True:
        password = "".join(secrets.choice(alphabet)
                           for _ in range(length))
        if (any(c.isalpha() for c in password)
                and any(c.isdigit() for c in password)):
            return password


def reset_admin_password(env_file=None, password_file=None):
    """重置面板管理员密码，返回新密码明文。

    步骤：
      1. 生成 20 位随机密码；
      2. 用 web.auth.hash_password 算哈希（pbkdf2_sha256，100000 次，
         与面板 verify_password 配套），写进 env 文件的
         MBOT_WEB_PASSWORD_HASH（复用 web/routes/auth.py 的原子写）；
      3. 明文写进密码文件（原子写，600 权限）；
      4. 重启面板服务生效。
    调用方只把返回的明文发给白名单用户一次，不要记日志。
    """
    # 延迟 import：web.routes.auth 依赖 fastapi 等 Web 依赖，机器人
    # 启动时不一定需要；真正重置时再 import。
    from web.auth import hash_password
    from web.routes.auth import update_password_hash_in_env_file

    new_password = generate_admin_password()
    new_hash = hash_password(new_password)
    update_password_hash_in_env_file(new_hash, env_file=env_file)
    path = password_file or ADMIN_PASSWORD_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent or ".", prefix=".mbot-pw-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_password + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.chmod(path, 0o600)
    restart_service(WEB_SERVICE)
    return new_password


@require_allowed
async def raninfo_command(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """处理 /raninfo：重置面板管理员密码，新密码只发一次。"""
    await _reply(update, context, "正在重置面板管理员密码…")
    try:
        new_password = await run_sync(reset_admin_password)
    except (OSError, RuntimeError) as exc:
        await _reply(update, context, f"❌ 重置失败：{exc}")
        return
    await _reply(
        update, context,
        "✅ 面板管理员密码已重置，面板服务已重启。\n\n"
        f"新密码：`{new_password}`\n\n"
        "⚠️ 请立即保存，新密码只显示这一次。",
        parse_mode="Markdown")


# ================= /me：我的信息 =================

def read_panel_version(path=None):
    """读 VERSION 文件返回版本号（如 "1.3.0"）；读不到返回 "未知"。"""
    try:
        with open(path or VERSION_FILE, encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        return "未知"
    return version or "未知"


def _count_running_instances(profile):
    """某 profile 下 RUNNING 实例数（只读查询；失败时抛异常）。"""
    cli = bot_config.get_oci_client(profile=profile)
    compartment = bot_config.get_compartment_id(profile)
    instances = list_instances(cli, compartment)
    return sum(1 for inst in instances
               if inst.get("lifecycle-state") == "RUNNING")


@require_allowed
async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /me：我的信息（TG 身份 / 面板版本 / OCI 账号与实例数）。"""
    user = update.effective_user
    version = read_panel_version()
    version_text = "未知" if version == "未知" else f"v{version}"
    # 账号名用主区中文名（v1.15.0 修：原来直接显示 DEFAULT 等配置键名）。
    account_labels = "、".join(
        bot_config.profile_display_name(p) for p in bot_config.PROFILES)
    lines = ["👤 我的信息", "",
             f"Telegram ID：{user.id if user else '?'}",
             f"用户名：@{user.username}" if user and user.username
             else "用户名：未设置",
             f"面板版本：{version_text}",
             "",
             f"已配置 OCI 账号：{len(bot_config.PROFILES)} 个"
             f"（{account_labels}）"]
    for profile in bot_config.PROFILES:
        label = bot_config.profile_display_name(profile)
        try:
            count = await run_sync(_count_running_instances, profile)
            lines.append(f"• {label}：RUNNING {count} 台")
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("%s 查实例数失败：%s", profile, exc)
            lines.append(f"• {label}：{fmt_error(exc)}")
    await _reply(update, context, "\n".join(lines))


# ================= /admin：管理员菜单 =================

def admin_keyboard():
    """管理员菜单内联键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 面板状态", callback_data="adm:status")],
        [InlineKeyboardButton("🔄 重启面板服务",
                              callback_data="adm:restart-web")],
        [InlineKeyboardButton("🤖 重启机器人服务",
                              callback_data="adm:restart-bot")],
        [InlineKeyboardButton("📌 查看版本", callback_data="adm:version")],
    ])


def _confirm_keyboard(confirm_data, confirm_text):
    """二次确认键盘（确认 / 取消）。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(confirm_text, callback_data=confirm_data)],
        [InlineKeyboardButton("❌ 取消", callback_data="x")],
    ])


@require_allowed
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /admin：管理员菜单（内联按钮）。"""
    await _reply(update, context, "🛠 管理员菜单", reply_markup=admin_keyboard())


@require_allowed
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 adm:* 回调：面板状态 / 重启确认 / 版本。"""
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "adm:status":
        version = read_panel_version()
        version_text = "未知" if version == "未知" else f"v{version}"
        web_state = service_status(WEB_SERVICE)
        bot_state = service_status(BOT_SERVICE)
        await query.edit_message_text(
            "📊 面板状态\n\n"
            f"面板版本：{version_text}\n"
            f"面板服务（{WEB_SERVICE}）：{web_state}\n"
            f"机器人服务（{BOT_SERVICE}）：{bot_state}",
            reply_markup=admin_keyboard())
        return

    if data == "adm:version":
        version = read_panel_version()
        version_text = "未知" if version == "未知" else f"v{version}"
        await query.edit_message_text(
            f"📌 当前版本：{version_text}",
            reply_markup=admin_keyboard())
        return

    if data == "adm:restart-web":
        await query.edit_message_text(
            "确定要重启面板服务吗？重启期间面板约几秒不可用，"
            "机器人不受影响。",
            reply_markup=_confirm_keyboard("adm:confirm-web",
                                           "✅ 确认重启面板"))
        return

    if data == "adm:confirm-web":
        await query.edit_message_text("正在重启面板服务…")
        try:
            await run_sync(restart_service, WEB_SERVICE)
        except RuntimeError as exc:
            await query.edit_message_text(
                f"❌ 重启面板服务失败：{exc}",
                reply_markup=admin_keyboard())
            return
        await query.edit_message_text("✅ 面板服务已重启。",
                                      reply_markup=admin_keyboard())
        return

    if data == "adm:restart-bot":
        await query.edit_message_text(
            "确定要重启机器人服务吗？重启后机器人会断开本次会话几秒，"
            "之后自动恢复。",
            reply_markup=_confirm_keyboard("adm:confirm-bot",
                                           "✅ 确认重启机器人"))
        return

    if data == "adm:confirm-bot":
        # 先发消息再重启：重启会杀掉当前进程，之后的回复发不出去。
        await query.edit_message_text("正在重启机器人服务，几秒后恢复…")
        try:
            await run_sync(restart_service, BOT_SERVICE)
        except RuntimeError as exc:
            await query.edit_message_text(
                f"❌ 重启机器人服务失败：{exc}",
                reply_markup=admin_keyboard())
            return
        # 正常情况进程已被杀掉，走不到这里；以防万一补一句。
        await query.edit_message_text("✅ 机器人服务重启命令已发出。",
                                      reply_markup=admin_keyboard())
        return

    await query.edit_message_text("参数错误，请重新发送 /admin。")
