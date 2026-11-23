#!/usr/bin/env python3
"""Telegram 机器人核心命令（/start、/help、/oracle、实例管理）。

路由（见 tgbot/bot.py）：
  - 命令：/start /help /oracle /instances
  - 回调：o:*（Oracle 菜单）、i:*（实例列表/详情/操作）、
    c:*（危险操作二次确认）、x（取消）

callback_data 只有 64 字节，放不下 OCID，所以回调里只传实例列表下标，
OCID 从 context.user_data["instances"] 里按下标取回（见 services）。

扩展菜单项（配额、流量、换 IP、抢机等）不在本文件实现，统一转发给
tgbot.handlers_extra.dispatch_oracle_action / handle_confirm（另一位
同事并行开发）；该模块尚未就绪或返回 False 时，回复"该功能暂未实现"。

安全说明：
  - 所有 handler 都加 @require_allowed，白名单外用户直接拒绝。
  - 写操作（启停/终止）执行前必须二次确认；终止默认永久删除引导卷
    （oci.instance.terminate_instance 的默认值，不可改）。
  - 本文件不含真实 OCID / Token / IP。
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from oci.instance import (get_instance, list_instances, restart_instance,
                          start_instance, stop_instance, terminate_instance)
from oci.network import get_public_ip
from tgbot.keyboards import (ORACLE_MENU_ITEMS, back_to_oracle_keyboard,
                             confirm_keyboard,
                             instance_actions_keyboard,
                             instance_list_keyboard, main_menu_keyboard,
                             oracle_menu_keyboard)
from tgbot.services import (fmt_error, fmt_instance_detail,
                            fmt_instance_list, get_cli, get_compartment,
                            get_current_profile, require_allowed,
                            resolve_instance, run_sync, store_instances)

logger = logging.getLogger(__name__)

# 操作中文名（确认提示与结果通知共用）。
_ACTION_TEXT = {
    "start": "启动",
    "stop": "停止",
    "restart": "重启",
    "terminate": "终止",
}

# 欢迎词与帮助文本。
WELCOME_TEXT = (
    "👋 你好！我是 R-Bot 开源克隆机器人。\n"
    "\n"
    "我可以帮你管理 Oracle Cloud 免费层实例：查看实例、启停、"
    "换 IP、查配额、抢机等。\n"
    "\n"
    "常用命令：\n"
    f"/oracle — Oracle 菜单（{len(ORACLE_MENU_ITEMS)} 项功能）\n"
    "/instances — 实例列表\n"
    "/help — 命令清单"
)

HELP_TEXT = (
    "📖 命令清单：\n"
    "\n"
    "/start — 欢迎与主菜单\n"
    "/help — 显示本清单\n"
    f"/oracle — Oracle 菜单（开机、实例、配额、流量等 {len(ORACLE_MENU_ITEMS)} 项）\n"
    "/instances — 列出当前账号的实例\n"
    "\n"
    "客户端工具：\n"
    "/command — 在已保存主机上远程执行 Shell 命令\n"
    "/pubkey — 管理 SSH 公钥（列表/添加/删除）\n"
    "/oproxy — 查看/设置机器人出站代理\n"
    "/uploadapi — 添加 OCI API 配置（上传 config 文件或粘贴文本）\n"
    "\n"
    "实例操作都在消息下方的按钮里完成：点实例进详情，"
    "再点启动 / 停止 / 重启 / 换 IP / 升降配 / 终止。"
    "危险操作（停止、终止）执行前会二次确认。"
)

ORACLE_MENU_TEXT = f"☁️ Oracle 菜单（共 {len(ORACLE_MENU_ITEMS)} 项，点按钮使用）："
NOT_IMPLEMENTED_TEXT = "该功能暂未实现。"
INSTANCE_GONE_TEXT = "实例不存在或列表已过期，请重新打开实例列表。"


# ---------------- 扩展模块转发 ----------------

async def _dispatch_extra(item, update, context):
    """转发菜单项给 handlers_extra.dispatch_oracle_action。

    返回 True 表示已处理；handlers_extra 模块尚未就绪（ImportError）
    或它返回 False 时返回 False，调用方回复"该功能暂未实现"。
    """
    try:
        from tgbot import handlers_extra
    except ImportError:
        logger.warning("handlers_extra 模块尚未就绪，菜单项 %r 未处理", item)
        return False
    dispatch = getattr(handlers_extra, "dispatch_oracle_action", None)
    if dispatch is None:
        logger.warning("handlers_extra 缺少 dispatch_oracle_action，菜单项 %r 未处理",
                       item)
        return False
    handled = await dispatch(item, update, context)
    return handled is not False


async def _handle_confirm_extra(action, idx, update, context):
    """转发二次确认给 handlers_extra.handle_confirm，同上。"""
    try:
        from tgbot import handlers_extra
    except ImportError:
        logger.warning("handlers_extra 模块尚未就绪，确认 %r 未处理", action)
        return False
    confirm = getattr(handlers_extra, "handle_confirm", None)
    if confirm is None:
        logger.warning("handlers_extra 缺少 handle_confirm，确认 %r 未处理", action)
        return False
    handled = await confirm(action, idx, update, context)
    return handled is not False


# ---------------- 内部工具 ----------------

def _menu_states(context):
    """取 /oracle 菜单按钮的动态开关状态（监控/自启/流量守护/停机保活）。

    handlers_extra 未就绪或读取失败时返回 None（按钮不带状态后缀，
    菜单照常显示）。状态本身由 handlers_extra.get_menu_states 提供。
    """
    try:
        from tgbot import handlers_extra
    except ImportError:
        return None
    get_states = getattr(handlers_extra, "get_menu_states", None)
    if not callable(get_states):
        return None
    try:
        states = get_states(context)
    except Exception:  # noqa: BLE001 - 状态读失败不影响菜单显示
        return None
    return states if isinstance(states, dict) else None


def _unwrap(data):
    """OCI CLI 的 get 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data or {}


async def _load_instance_list(context):
    """拉实例列表并格式化，返回 (文本, 键盘)。

    成功时把精简列表存进 user_data 供回调按下标取回；
    失败时返回 fmt_error 文本 + 返回菜单键盘。
    """
    try:
        instances = await run_sync(
            list_instances, get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        logger.warning("列实例失败：%s", exc)
        return fmt_error(exc), back_to_oracle_keyboard()
    store_instances(context, instances)
    profile = get_current_profile(context)
    return (fmt_instance_list(instances, profile),
            instance_list_keyboard(instances))


# ---------------- 命令 ----------------

@require_allowed
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start：欢迎词 + 主菜单。"""
    await update.effective_message.reply_text(
        WELCOME_TEXT, reply_markup=main_menu_keyboard())


@require_allowed
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help：命令清单。"""
    await update.effective_message.reply_text(HELP_TEXT)


@require_allowed
async def oracle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /oracle：发送 Oracle 菜单（第 1 页，监控按钮带开关状态）。"""
    await update.effective_message.reply_text(
        ORACLE_MENU_TEXT,
        reply_markup=oracle_menu_keyboard(0, states=_menu_states(context)))


@require_allowed
async def instances_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /instances：实例列表（回复命令消息，不编辑回调消息）。"""
    text, keyboard = await _load_instance_list(context)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


# ---------------- Oracle 菜单回调 ----------------

@require_allowed
async def oracle_menu_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """处理 o:* 回调：菜单显示 / 翻页 / 实例列表 / 其余项转发。"""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")

    if data == "o:menu":
        await query.edit_message_text(
            ORACLE_MENU_TEXT,
            reply_markup=oracle_menu_keyboard(0, states=_menu_states(context)))
        return

    if len(parts) == 3 and parts[1] == "page":
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        await query.edit_message_text(
            ORACLE_MENU_TEXT,
            reply_markup=oracle_menu_keyboard(page, states=_menu_states(context)))
        return

    if data == "o:instances":
        text, keyboard = await _load_instance_list(context)
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    # 其余菜单项（配额、流量、任务、测活、开机……）转发给扩展模块。
    item = parts[1] if len(parts) >= 2 else ""
    if not await _dispatch_extra(item, update, context):
        await query.edit_message_text(
            NOT_IMPLEMENTED_TEXT, reply_markup=back_to_oracle_keyboard())


# ---------------- 实例列表 / 详情回调 ----------------

@require_allowed
async def instance_list_callback(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """处理 i:list 回调：刷新实例列表（编辑当前消息）。"""
    query = update.callback_query
    await query.answer()
    text, keyboard = await _load_instance_list(context)
    await query.edit_message_text(text, reply_markup=keyboard)


@require_allowed
async def instance_detail_callback(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    """处理 i:detail:<idx> 回调：实例详情 + 操作按钮。"""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await query.edit_message_text(
            INSTANCE_GONE_TEXT, reply_markup=back_to_oracle_keyboard())
        return

    cli = get_cli(context)
    instance_id = inst.get("id")
    try:
        detail = await run_sync(get_instance, cli, instance_id)
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        logger.warning("查实例详情失败：%s", exc)
        await query.edit_message_text(
            fmt_error(exc), reply_markup=back_to_oracle_keyboard())
        return
    try:
        public_ip = await run_sync(get_public_ip, cli, instance_id)
    except Exception as exc:  # noqa: BLE001 - 查 IP 失败不影响详情展示
        logger.warning("查公网 IP 失败：%s", exc)
        public_ip = None

    state = _unwrap(detail).get("lifecycle-state", "?")
    await query.edit_message_text(
        fmt_instance_detail(detail, public_ip),
        reply_markup=instance_actions_keyboard(int(idx), state))


# ---------------- 实例操作回调 ----------------

@require_allowed
async def instance_action_callback(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    """处理 i:<action>:<idx> 回调。

    start/stop/restart/terminate：先发二次确认（terminate 加粗警告
    将永久删除引导卷）；ip/resize/rename/reinstall/console：
    转发给扩展模块。
    """
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await query.edit_message_text(
            INSTANCE_GONE_TEXT, reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or inst.get("id", "?")[-4:]

    if action in ("ip", "resize", "rename", "reinstall", "console"):
        # 扩展模块处理时用 pending_idx 取回实例下标。
        # 注意：实例操作里的 "ip" 转发为 "changeip"（换 IP 确认流程），
        # 不要转发 "ip"——"ip" 是菜单 2 的 IP 总览，不含换 IP 确认按钮。
        # 其余 action 与 dispatch 项同名，直接转发。
        context.user_data["pending_idx"] = idx
        item = "changeip" if action == "ip" else action
        if not await _dispatch_extra(item, update, context):
            await query.edit_message_text(
                NOT_IMPLEMENTED_TEXT, reply_markup=back_to_oracle_keyboard())
        return

    if action not in _ACTION_TEXT:
        await query.edit_message_text(
            NOT_IMPLEMENTED_TEXT, reply_markup=back_to_oracle_keyboard())
        return

    verb = _ACTION_TEXT[action]
    if action == "terminate":
        text = (
            f"⚠️ 确认终止实例「{name}」？\n"
            "\n"
            "<b>终止后将永久删除引导卷，数据无法恢复！</b>"
        )
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=confirm_keyboard(action, idx))
        return

    await query.edit_message_text(
        f"确认{verb}实例「{name}」？",
        reply_markup=confirm_keyboard(action, idx))


# ---------------- 二次确认回调 ----------------

@require_allowed
async def confirm_callback(update: Update,
                           context: ContextTypes.DEFAULT_TYPE):
    """处理 c:<action>:<idx> 回调：执行启停 / 重启 / 终止。

    其余 action（如 changeip / resize）转发给扩展模块处理。
    """
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""

    if action not in _ACTION_TEXT:
        if not await _handle_confirm_extra(action, idx, update, context):
            await query.edit_message_text(
                NOT_IMPLEMENTED_TEXT, reply_markup=back_to_oracle_keyboard())
        return

    inst = resolve_instance(context, idx)
    if inst is None:
        await query.edit_message_text(
            INSTANCE_GONE_TEXT, reply_markup=back_to_oracle_keyboard())
        return
    name = inst.get("display-name") or inst.get("id", "?")[-4:]
    verb = _ACTION_TEXT[action]

    try:
        cli = get_cli(context)
        instance_id = inst.get("id")
        # 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
        if action == "start":
            await run_sync(start_instance, cli, instance_id)
        elif action == "stop":
            await run_sync(stop_instance, cli, instance_id)
        elif action == "restart":
            await run_sync(restart_instance, cli, instance_id)
        elif action == "terminate":
            # 默认 preserve_boot_volume=False：永久删除引导卷。
            await run_sync(terminate_instance, cli, instance_id)
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        logger.warning("%s实例失败：%s", verb, exc)
        await query.edit_message_text(
            fmt_error(exc), reply_markup=back_to_oracle_keyboard())
        return

    if action == "terminate":
        text = f"✅ 已终止实例「{name}」，引导卷已永久删除。"
    else:
        text = f"✅ 已{verb}实例「{name}」。"
    await query.edit_message_text(text, reply_markup=back_to_oracle_keyboard())


# ---------------- 取消 / 未知命令 / 错误 ----------------

@require_allowed
async def cancel_callback(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """处理 x 回调：取消当前操作。"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("已取消。")


@require_allowed
async def unknown_command(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """未知命令：提示用 /help 查看命令清单。"""
    await update.effective_message.reply_text(
        "未知命令，输入 /help 查看可用命令。")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理：记日志，并用中文提示通知用户。

    注意：不加 @require_allowed——错误可能发生在白名单检查之前，
    且 update 在某些错误场景下是 None。

    两种常规 Telegram API 情况只记日志、不打扰用户：
    - "Message is not modified"：重复点同一菜单项，编辑内容与当前一致；
    - "Query is too old"：点了过期的旧按钮（回调已失效）。
    """
    logger.exception("处理 Telegram 更新时出错：%s", context.error)
    err_text = str(context.error)
    if ("Message is not modified" in err_text
            or "Query is too old" in err_text):
        return
    message = getattr(update, "effective_message", None)
    if message is not None:
        try:
            await message.reply_text(fmt_error(context.error))
        except Exception:  # noqa: BLE001 - 通知失败也不要再抛
            logger.exception("向用户发送错误通知失败")
