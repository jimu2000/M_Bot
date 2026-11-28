#!/usr/bin/env python3
"""Telegram 机器人核心命令（/start、/help、/oracle、/instances、/lang、实例管理）。

路由（见 tgbot/bot.py）：
  - 命令：/start /help /oracle /instances /lang
  - 回调：o:*（Oracle 菜单，含 o:lang 语言切换）、i:*（实例列表/详情/操作）、
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

import asyncio
import html
import logging
import threading
import time

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from oci.instance import (get_instance, list_instances, restart_instance,
                          start_instance, stop_instance, terminate_instance)
from oci.network import attach_public_ips_bulk, get_public_ip
from tgbot import i18n as bot_i18n
from tgbot import menu_tracker
from tgbot.cpu_model import (cached_cpu_model, delete_cpu_model,
                             get_cpu_model, host_for_ip)
from tgbot.os_version import cached_os_version, get_os_version
from tgbot.instance_credentials import delete_credential, get_root_password
from tgbot.handlers_client import run_remote_command
from tgbot.i18n import lang_of, set_lang_context, toggle_user_lang
from tgbot.keyboards import (ORACLE_MENU_ITEMS, back_to_oracle_keyboard,
                             confirm_keyboard,
                             instance_actions_keyboard,
                             instance_list_keyboard, main_menu_keyboard,
                             oracle_menu_keyboard)
from tgbot.services import (fmt_error, fmt_instance_detail,
                            fmt_instance_list, get_cli, get_compartment,
                            get_current_profile, get_current_region,
                            require_allowed, resolve_instance, run_sync,
                            store_instances)

logger = logging.getLogger(__name__)


def _action_verb(action, lang="zh"):
    """实例操作动词（确认提示与结果通知共用）。"""
    return bot_i18n.t(lang, f"instances.verb_{action}")


def _welcome_text(lang="zh"):
    return bot_i18n.t(lang, "welcome", count=len(ORACLE_MENU_ITEMS))


def _help_text(lang="zh"):
    return bot_i18n.t(lang, "help", count=len(ORACLE_MENU_ITEMS))


def _oracle_menu_text(lang="zh"):
    return bot_i18n.t(lang, "oracle.menu_title",
                      count=len(ORACLE_MENU_ITEMS))


# Welcome and help text (Chinese default; the multilingual versions are _welcome_text / _help_text). 欢迎词与帮助文本（中文默认版；多语言版见 _welcome_text/_help_text）。
WELCOME_TEXT = _welcome_text("zh")
HELP_TEXT = _help_text("zh")
ORACLE_MENU_TEXT = _oracle_menu_text("zh")
NOT_IMPLEMENTED_TEXT = bot_i18n.t("zh", "common.not_implemented")
INSTANCE_GONE_TEXT = bot_i18n.t("zh", "common.instance_gone")


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


def _current_account_label(context):
    """当前账号显示名（/oracle 菜单顶行“切换账号”按钮用）。

    只读缓存，不调 OCI；handlers_extra 未就绪时回退到 profile 名。
    """
    try:
        from tgbot import handlers_extra
    except ImportError:
        return get_current_profile(context)
    get_label = getattr(handlers_extra, "get_cached_profile_display_name",
                        None)
    if not callable(get_label):
        return get_current_profile(context)
    try:
        label = get_label(get_current_profile(context))
    except Exception:  # noqa: BLE001 - 读失败不影响菜单显示
        return get_current_profile(context)
    # When the working region is not the home region, suffix the region name (e.g. "新加坡 · 首尔"). 工作区域不是主区时后缀区域名（如“新加坡 · 首尔”）。
    try:
        from tgbot.services import get_current_region
        from tgbot.config import get_region, get_region_label
        profile = get_current_profile(context)
        region = get_current_region(context)
        if region != get_region(profile):
            label += f" · {get_region_label(region)}"
    except Exception:  # noqa: BLE001 - 读失败不影响菜单显示
        pass
    return label


def _unwrap(data):
    """OCI CLI 的 get 响应通常包一层 {"data": ...}，这里解包。"""
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data or {}


# Instance list cache (bot): (profile, region, lang) -> {"expires",
# "instances", "text", "keyboard"}. 实例列表缓存（机器人端）：
# 网页版有 30 秒响应缓存，机器人原来每点一次就全量重查 OCI
# （list + 每台 3 次 IP 查询），点一次 2.8–3.2 秒；照抄网页版
# 的做法，30 秒内重复点直接返回缓存。刷新按钮（i:list）传
# refresh=True 绕过。缓存的是格式化好的文本/键盘与原始精简
# 列表；命中时仍要把列表存进 user_data（回调按下标取回），因为
# user_data 是按用户的，缓存是进程全局的。
INSTANCE_LIST_CACHE_TTL = 30
_INSTANCE_LIST_CACHE = {}
_INSTANCE_LIST_CACHE_LOCK = threading.Lock()
_INSTANCE_LIST_INFLIGHT = {}
_INSTANCE_LIST_CACHE_GENERATION = 0


def clear_instance_list_cache():
    """清实例列表缓存并使旧的在途加载结果失效。"""
    global _INSTANCE_LIST_CACHE_GENERATION
    with _INSTANCE_LIST_CACHE_LOCK:
        _INSTANCE_LIST_CACHE.clear()
        _INSTANCE_LIST_INFLIGHT.clear()
        _INSTANCE_LIST_CACHE_GENERATION += 1


async def _fetch_instance_list(context, cache_key, lang, generation):
    """实际执行一次 OCI 列表加载；同一 key 的并发请求共享此任务。"""
    try:
        cli = get_cli(context)
        compartment = get_compartment(context)
        instances = await run_sync(list_instances, cli, compartment)
        # 1 核/低内存 VPS 上把 VNIC 查询并发压到 2，避免一次列表刷新
        # 同时启动 5 个 SDK 请求造成 CPU/TLS 突刺；同 key 冷请求已单飞。
        ips = await run_sync(attach_public_ips_bulk, cli, instances,
                             compartment, max_workers=2)
        for inst, ip in zip(instances, ips):
            inst["public-ip"] = ip
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转提示
        logger.warning("列实例失败：%s", exc)
        return None, fmt_error(exc, lang=lang), back_to_oracle_keyboard(lang=lang)

    text = fmt_instance_list(instances, _current_account_label(context),
                             lang=lang)
    keyboard = instance_list_keyboard(instances, lang=lang)
    with _INSTANCE_LIST_CACHE_LOCK:
        # 写操作清缓存后，较早启动的请求不能把旧状态重新写回来。
        if generation == _INSTANCE_LIST_CACHE_GENERATION:
            _INSTANCE_LIST_CACHE[cache_key] = {
                "expires": time.monotonic() + INSTANCE_LIST_CACHE_TTL,
                "instances": instances,
                "text": text,
                "keyboard": keyboard,
            }
    return instances, text, keyboard


async def _load_instance_list(context, lang="zh", refresh=False):
    """拉实例列表并格式化；缓存未命中时同一账号/区域/语言只加载一次。

    refresh=True 会绕过已缓存结果，但仍与同 key 的并发刷新合并；
    成功刷新后更新缓存，避免刷新完成后下一次点击又立即冷加载。
    """
    cache_key = (get_current_profile(context),
                 get_current_region(context), lang)
    with _INSTANCE_LIST_CACHE_LOCK:
        entry = _INSTANCE_LIST_CACHE.get(cache_key)
        if (not refresh and entry
                and time.monotonic() < entry["expires"]):
            store_instances(context, entry["instances"])
            return entry["text"], entry["keyboard"]

        task = _INSTANCE_LIST_INFLIGHT.get(cache_key)
        if task is None:
            generation = _INSTANCE_LIST_CACHE_GENERATION
            task = asyncio.create_task(
                _fetch_instance_list(context, cache_key, lang, generation))
            _INSTANCE_LIST_INFLIGHT[cache_key] = task

            def _cleanup(done_task, key=cache_key):
                with _INSTANCE_LIST_CACHE_LOCK:
                    if _INSTANCE_LIST_INFLIGHT.get(key) is done_task:
                        _INSTANCE_LIST_INFLIGHT.pop(key, None)
            task.add_done_callback(_cleanup)

    # shield 防止某个用户取消/离开请求时，把共享加载任务一并取消。
    result = await asyncio.shield(task)
    instances, text, keyboard = result
    if instances is not None:
        store_instances(context, instances)
    return text, keyboard


# Instance detail cache: (profile, region, instance OCID) -> (expiry, detail, IP).
INSTANCE_DETAIL_CACHE_TTL = 12
_INSTANCE_DETAIL_CACHE = {}
_INSTANCE_DETAIL_INFLIGHT = {}
_INSTANCE_DETAIL_CACHE_GENERATION = 0


def clear_instance_detail_cache(instance_id=None):
    """清详情缓存；instance_id 暂保留接口，当前按账号切换/操作统一清空。"""
    global _INSTANCE_DETAIL_CACHE_GENERATION
    with _INSTANCE_LIST_CACHE_LOCK:
        _INSTANCE_DETAIL_CACHE.clear()
        _INSTANCE_DETAIL_INFLIGHT.clear()
        _INSTANCE_DETAIL_CACHE_GENERATION += 1


async def _fetch_instance_detail_pair(cli, instance_id, compartment,
                                      cache_key, generation):
    detail_result, ip_result = await asyncio.gather(
        run_sync(get_instance, cli, instance_id),
        run_sync(get_public_ip, cli, instance_id, compartment),
        return_exceptions=True)
    if not isinstance(detail_result, Exception):
        with _INSTANCE_LIST_CACHE_LOCK:
            if generation == _INSTANCE_DETAIL_CACHE_GENERATION:
                _INSTANCE_DETAIL_CACHE[cache_key] = (
                    time.monotonic() + INSTANCE_DETAIL_CACHE_TTL,
                    detail_result, ip_result)
    return detail_result, ip_result


async def _get_instance_detail_pair(context, cli, instance_id, compartment):
    key = (get_current_profile(context), get_current_region(context),
           instance_id)
    with _INSTANCE_LIST_CACHE_LOCK:
        entry = _INSTANCE_DETAIL_CACHE.get(key)
        if entry and time.monotonic() < entry[0]:
            return entry[1], entry[2]
        task = _INSTANCE_DETAIL_INFLIGHT.get(key)
        if task is None:
            generation = _INSTANCE_DETAIL_CACHE_GENERATION
            task = asyncio.create_task(_fetch_instance_detail_pair(
                cli, instance_id, compartment, key, generation))
            _INSTANCE_DETAIL_INFLIGHT[key] = task

            def _cleanup(done_task, cache_key=key):
                with _INSTANCE_LIST_CACHE_LOCK:
                    if _INSTANCE_DETAIL_INFLIGHT.get(cache_key) is done_task:
                        _INSTANCE_DETAIL_INFLIGHT.pop(cache_key, None)
            task.add_done_callback(_cleanup)
    return await asyncio.shield(task)


# ---------------- 命令 ----------------

@require_allowed
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start：欢迎词 + 主菜单。"""
    lang = lang_of(update, context)
    await menu_tracker.reply(update, context,
                             _welcome_text(lang),
                             reply_markup=main_menu_keyboard(lang=lang))


@require_allowed
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help：命令清单。"""
    await update.effective_message.reply_text(_help_text(
        lang_of(update, context)))


@require_allowed
async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /lang：切换语言（中文 <-> English），立即生效并持久化。"""
    user = update.effective_user
    user_id = user.id if user else None
    new_lang = toggle_user_lang(user_id)
    set_lang_context(context, new_lang)
    await menu_tracker.reply(
        update, context,
        bot_i18n.t(new_lang,
                   "lang.switched_en" if new_lang == "en"
                   else "lang.switched_zh"),
        reply_markup=main_menu_keyboard(lang=new_lang))


@require_allowed
async def oracle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /oracle：发送 Oracle 菜单（第 1 页，监控按钮带开关状态）。"""
    lang = lang_of(update, context)
    await menu_tracker.reply(
        update, context, _oracle_menu_text(lang),
        reply_markup=oracle_menu_keyboard(
            0, states=_menu_states(context),
            current_account=_current_account_label(context), lang=lang))


@require_allowed
async def instances_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /instances：实例列表（回复命令消息，不编辑回调消息）。"""
    lang = lang_of(update, context)
    text, keyboard = await _load_instance_list(context, lang=lang)
    await menu_tracker.reply(update, context, text, reply_markup=keyboard)


# ---------------- Oracle 菜单回调 ----------------

async def _cleanup_switch_messages(update, context):
    """回主菜单时删掉切换流程的中间消息（账号/区域列表），只留菜单。

    handlers_extra 里的 _delete_tracked_switch_messages 是实现；
    这里延迟导入（handlers.py 与 handlers_extra 互相引用，只能
    一边延迟）。当前消息（即将被编辑成菜单的那条）不删。
    """
    try:
        from tgbot import handlers_extra
    except ImportError:
        return
    delete = getattr(handlers_extra, "_delete_tracked_switch_messages",
                     None)
    if not callable(delete):
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    query = update.callback_query
    current_id = None
    if query is not None and query.message is not None:
        current_id = query.message.message_id
    if chat_id is not None:
        await delete(context, chat_id, except_id=current_id)


@require_allowed
async def oracle_menu_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """处理 o:* 回调：菜单显示 / 翻页 / 实例列表 / 其余项转发。"""
    query = update.callback_query
    await query.answer()
    _clear_cpu_pending(context)
    data = query.data or ""
    parts = data.split(":")
    lang = lang_of(update, context)

    if data == "o:lang":
        user = update.effective_user
        new_lang = toggle_user_lang(user.id if user else None)
        set_lang_context(context, new_lang)
        await _cleanup_switch_messages(update, context)
        await menu_tracker.safe_edit(query, 
            _oracle_menu_text(new_lang),
            reply_markup=oracle_menu_keyboard(
                0, states=_menu_states(context),
                current_account=_current_account_label(context),
                lang=new_lang))
        return

    if data == "o:menu":
        await _cleanup_switch_messages(update, context)
        await menu_tracker.safe_edit(query, 
            _oracle_menu_text(lang),
            reply_markup=oracle_menu_keyboard(
                0, states=_menu_states(context),
                current_account=_current_account_label(context),
                lang=lang))
        return

    if len(parts) == 3 and parts[1] == "page":
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
        await menu_tracker.safe_edit(query, 
            _oracle_menu_text(lang),
            reply_markup=oracle_menu_keyboard(
                page, states=_menu_states(context),
                current_account=_current_account_label(context),
                lang=lang))
        return

    if data == "o:instances":
        text, keyboard = await _load_instance_list(context, lang=lang)
        await menu_tracker.safe_edit(query, text, reply_markup=keyboard)
        return

    item = parts[1] if len(parts) >= 2 else ""
    if not await _dispatch_extra(item, update, context):
        await menu_tracker.safe_edit(query, 
            bot_i18n.t(lang, "common.not_implemented"),
            reply_markup=back_to_oracle_keyboard(lang=lang))


# ---------------- 实例列表 / 详情回调 ----------------

@require_allowed
async def instance_list_callback(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """处理 i:list 回调：刷新实例列表（编辑当前消息）。

    刷新按钮绕过列表缓存（refresh=True），强制重新查 OCI。
    """
    query = update.callback_query
    await query.answer()
    _clear_cpu_pending(context)
    text, keyboard = await _load_instance_list(
        context, lang=lang_of(update, context), refresh=True)
    await menu_tracker.safe_edit(query, text, reply_markup=keyboard)


@require_allowed
async def noop_callback(update: Update,
                        context: ContextTypes.DEFAULT_TYPE):
    """处理 i:noop 回调：实例列表的分组标题按钮。

    只应答（消除按钮上的加载转圈），不发新消息、不改消息内容。
    """
    await update.callback_query.answer()


# user_data key: a pending background CPU-model refresh for the detail
# message currently on screen: {"message_id", "instance_id"}.
# 后台 CPU 型号刷新的待处理标记：当前详情消息的 {message_id, instance_id}。
# The detail view is shown first with "检测中"; the background task
# only edits the message back if this marker still points at it —
# if the user tapped away (action / list / menu), the exit handler
# clears the marker so a late SSH result never clobbers the new
# content (e.g. a start/stop confirm dialog).
_CPU_PENDING_KEY = "_cpu_pending_msg"
_CPU_MODEL_TASKS = {}
_CPU_MODEL_COOLDOWN = {}
_CPU_MODEL_SEMAPHORE = asyncio.Semaphore(2)
CPU_MODEL_RETRY_COOLDOWN = 60


async def _get_cpu_model_deduped(instance_id, host):
    """同一实例 CPU 型号 SSH 检测单飞，最多同时检测两台实例。"""
    if not instance_id:
        return None
    task = _CPU_MODEL_TASKS.get(instance_id)
    if task is None:
        if time.monotonic() < _CPU_MODEL_COOLDOWN.get(instance_id, 0):
            return cached_cpu_model(instance_id)

        async def _detect():
            try:
                async with _CPU_MODEL_SEMAPHORE:
                    model = await run_sync(get_cpu_model, instance_id, host,
                                           run_remote_command)
            except Exception as exc:  # noqa: BLE001 - SSH 失败不影响详情
                logger.warning("后台查 CPU 型号失败：%s", exc)
                model = None
            if not model:
                _CPU_MODEL_COOLDOWN[instance_id] = (
                    time.monotonic() + CPU_MODEL_RETRY_COOLDOWN)
            else:
                _CPU_MODEL_COOLDOWN.pop(instance_id, None)
            return model

        task = asyncio.create_task(_detect())
        _CPU_MODEL_TASKS[instance_id] = task

        def _cleanup(done_task, iid=instance_id):
            if _CPU_MODEL_TASKS.get(iid) is done_task:
                _CPU_MODEL_TASKS.pop(iid, None)
        task.add_done_callback(_cleanup)
    return await asyncio.shield(task)


def _clear_cpu_pending(context):
    """清掉后台 CPU 型号刷新的待处理标记（用户已离开详情页）。"""
    context.user_data.pop(_CPU_PENDING_KEY, None)


async def _refresh_cpu_model_later(context, query, instance_id, host,
                                   detail, public_ip, state, idx, lang):
    """后台 SSH 测 CPU 型号，完成后把详情消息里的“检测中”刷成型号。

    只在用户还停留在该详情页时才编辑（_CPU_PENDING_KEY 仍指向
    这条消息）；用户已离开时直接返回，不发新消息、不盖回旧内容。
    检测失败时该行刷成“未知”（与原来同步检测失败的显示一致）。
    """
    try:
        model = await _get_cpu_model_deduped(instance_id, host)
    except Exception as exc:  # noqa: BLE001 - 检测失败只记日志
        logger.warning("后台查 CPU 型号失败：%s", exc)
        model = None
    # 系统版本：同一轮后台刷新里一起取（读缓存，未命中再 SSH 检测一次）。
    try:
        os_version = await run_sync(get_os_version, instance_id, host,
                                    run_remote_command)
    except Exception as exc:  # noqa: BLE001 - 检测失败只记日志
        logger.warning("后台查系统版本失败：%s", exc)
        os_version = cached_os_version(instance_id)
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    pending = context.user_data.get(_CPU_PENDING_KEY) or {}
    if pending.get("message_id") != message_id:
        return
    context.user_data.pop(_CPU_PENDING_KEY, None)
    try:
        await query.edit_message_text(
            fmt_instance_detail(detail, public_ip, model, lang=lang,
                                root_password=get_root_password(instance_id),
                                os_version=os_version),
            reply_markup=instance_actions_keyboard(int(idx), state,
                                                   lang=lang),
            parse_mode="HTML")
    except BadRequest as exc:
        err = str(exc)
        if ("Message is not modified" in err
                or "Message to edit not found" in err):
            # User tapped away / the message was deleted meanwhile;
            # the refresh is simply dropped, never resent as a new
            # message (unlike menu_tracker.safe_edit's fallback).
            logger.debug("后台刷 CPU 型号时消息已不在/未变化：%s", exc)
            return
        raise


@require_allowed
async def instance_detail_callback(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    """处理 i:detail:<idx> 回调：实例详情 + 操作按钮。

    查实例与查公网 IP 并行做（原来串行 0.57 + 1.72 秒）；查 IP
    时传 compartment，省一次查实例详情的调用。
    CPU 型号：先读缓存；未命中且需要 SSH 时不堵详情页——先显示
    “检测中”，SSH 在后台跑完再刷新这条消息。
    """
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    lang = lang_of(update, context)
    if inst is None:
        await menu_tracker.safe_edit(query,
            bot_i18n.t(lang, "common.instance_gone"),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return

    cli = get_cli(context)
    instance_id = inst.get("id")
    compartment = get_compartment(context)
    detail_result, ip_result = await _get_instance_detail_pair(
        context, cli, instance_id, compartment)
    if isinstance(detail_result, Exception):
        logger.warning("查实例详情失败：%s", detail_result)
        await menu_tracker.safe_edit(query,
            fmt_error(detail_result, lang=lang),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    detail = detail_result
    public_ip = None
    if isinstance(ip_result, Exception):
        # 查 IP 失败不影响详情展示（只省略 IP 行）。
        logger.warning("查公网 IP 失败：%s", ip_result)
    else:
        public_ip = ip_result

    state = _unwrap(detail).get("lifecycle-state", "?")
    # CPU model: cache first; on a miss, show "检测中" now and detect
    # via SSH in the background instead of blocking the detail view
    # for up to 20s. CPU 型号：先读缓存；未命中且要 SSH 时先显示
    # “检测中”，后台测完再刷新；检测失败该行显示“未知”。
    cpu_model = None
    pending_cpu = False
    if state == "RUNNING" and public_ip:
        cpu_model = cached_cpu_model(instance_id)
        if not cpu_model:
            host = host_for_ip(public_ip)
            if host is not None:
                pending_cpu = True
    display_model = (bot_i18n.t(lang, "common.detecting")
                     if pending_cpu else cpu_model)
    # 系统版本：先读缓存；未命中时后台 CPU 刷新那一轮会一起取到。
    os_version = cached_os_version(instance_id)
    await menu_tracker.safe_edit(query,
        fmt_instance_detail(detail, public_ip, display_model, lang=lang,
                            root_password=get_root_password(instance_id),
                            os_version=os_version),
        reply_markup=instance_actions_keyboard(int(idx), state,
                                               lang=lang),
        parse_mode="HTML")
    if pending_cpu:
        message = getattr(query, "message", None)
        message_id = getattr(message, "message_id", None)
        if message_id is not None:
            context.user_data[_CPU_PENDING_KEY] = {
                "message_id": message_id,
                "instance_id": instance_id,
            }
            context.application.create_task(_refresh_cpu_model_later(
                context, query, instance_id, host,
                detail, public_ip, state, idx, lang))


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
    _clear_cpu_pending(context)
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    lang = lang_of(update, context)
    if inst is None:
        await menu_tracker.safe_edit(query, 
            bot_i18n.t(lang, "common.instance_gone"),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    name = inst.get("display-name") or inst.get("id", "?")[-4:]

    if action in ("ip", "resize", "rename", "reinstall", "console",
                  "bootswap", "bootbackup", "bootbackup-del"):
        # The extension module uses pending_idx to get the instance index back. 扩展模块处理时用 pending_idx 取回实例下标。
        # Note: the "ip" instance action forwards as "changeip" (the change-IP confirm flow); do not forward "ip" — "ip" is the menu-2 IP overview, which has no change-IP confirm button. 注意：实例操作里的 "ip" 转发为 "changeip"（换 IP 确认流程），不要转发 "ip"——"ip" 是菜单 2 的 IP 总览，不含换 IP 确认按钮。
        # Other actions share names with dispatch items and forward directly. 其余 action 与 dispatch 项同名，直接转发。
        context.user_data["pending_idx"] = idx
        item = "changeip" if action == "ip" else action
        if not await _dispatch_extra(item, update, context):
            await menu_tracker.safe_edit(query, 
                bot_i18n.t(lang, "common.not_implemented"),
                reply_markup=back_to_oracle_keyboard(lang=lang))
        return

    if action not in ("start", "stop", "restart", "terminate"):
        await menu_tracker.safe_edit(query, 
            bot_i18n.t(lang, "common.not_implemented"),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return

    verb = _action_verb(action, lang)
    if action == "terminate":
        # Instance names are user-chosen and may contain <>&: with parse_mode="HTML" they must be escaped, or Telegram fails to parse or gets injected tags (quote=False escapes only &<>; Telegram HTML recognizes just these three entities). 实例名是用户起的，可能含 <>&：parse_mode="HTML" 时必须转义，否则 Telegram 解析失败或被注入标签（quote=False 只转义 &<>，Telegram HTML 只认这三个实体）。
        text = bot_i18n.t(
            lang, "instances.confirm_terminate",
            name=html.escape(name, quote=False))
        await menu_tracker.safe_edit(query, 
            text, parse_mode="HTML",
            reply_markup=confirm_keyboard(action, idx, lang=lang))
        return

    await menu_tracker.safe_edit(query, 
        bot_i18n.t(lang, f"instances.confirm_{action}", name=name),
        reply_markup=confirm_keyboard(action, idx, lang=lang))


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
    lang = lang_of(update, context)

    if action not in ("start", "stop", "restart", "terminate"):
        if not await _handle_confirm_extra(action, idx, update, context):
            await menu_tracker.safe_edit(query, 
                bot_i18n.t(lang, "common.not_implemented"),
                reply_markup=back_to_oracle_keyboard(lang=lang))
        return

    inst = resolve_instance(context, idx)
    if inst is None:
        await menu_tracker.safe_edit(query, 
            bot_i18n.t(lang, "common.instance_gone"),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    name = inst.get("display-name") or inst.get("id", "?")[-4:]
    verb = _action_verb(action, lang)

    try:
        cli = get_cli(context)
        instance_id = inst.get("id")
        # Call the module-level imported function directly (do not pre-build a dict), so tests can patch it. 直接调模块级导入的函数（不要预建成 dict），测试才能 patch。
        if action == "start":
            await run_sync(start_instance, cli, instance_id)
        elif action == "stop":
            await run_sync(stop_instance, cli, instance_id)
        elif action == "restart":
            await run_sync(restart_instance, cli, instance_id)
        elif action == "terminate":
            # Default preserve_boot_volume=False: the boot volume is permanently deleted. 默认 preserve_boot_volume=False：永久删除引导卷。
            await run_sync(terminate_instance, cli, instance_id)
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转提示
        logger.warning("%s实例失败：%s", verb, exc)
        await menu_tracker.safe_edit(query, 
            fmt_error(exc, lang=lang),
            reply_markup=back_to_oracle_keyboard(lang=lang))
        return

    # Write succeeded: the cached instance list may now show a stale
    # state (e.g. an instance still RUNNING after it was stopped).
    # 写操作成功：缓存的实例列表可能已过期（如已停的实例还显示
    # 运行中），清掉，下次点列表重新查。
    clear_instance_list_cache()
    clear_instance_detail_cache(instance_id)
    if action == "terminate":
        delete_cpu_model(instance_id)
        delete_credential(instance_id)
        text = bot_i18n.t(lang, "instances.done_terminate", name=name)
    else:
        text = bot_i18n.t(lang, f"instances.done_{action}", name=name)
    await menu_tracker.safe_edit(query, text,
                                  reply_markup=back_to_oracle_keyboard(
                                      lang=lang))


# ---------------- 取消 / 未知命令 / 错误 ----------------

@require_allowed
async def cancel_callback(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """处理 x 回调：取消当前操作。"""
    query = update.callback_query
    await query.answer()
    await menu_tracker.safe_edit(query, bot_i18n.t(lang_of(update, context),
                                            "common.cancel"))
    # The message is now a plain-text result, no longer a selection box; clear the tracking. 消息已变成纯文字结果，不再是选择框，清空跟踪。
    menu_tracker.forget_live_menu(context)


@require_allowed
async def unknown_command(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """未知命令：提示用 /help 查看命令清单。"""
    await update.effective_message.reply_text(
        bot_i18n.t(lang_of(update, context), "common.unknown_command"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理：记日志，并用中文提示通知用户。

    注意：不加 @require_allowed——错误可能发生在白名单检查之前，
    且 update 在某些错误场景下是 None。

    两种常规 Telegram API 情况只记日志、不打扰用户：
    - "Message is not modified"：重复点同一菜单项，编辑内容与当前一致；
    - "Query is too old"：点了过期的旧按钮（回调已失效）。
    "Message to edit not found"：要编辑的消息已删，只发一句人话提示，
    不把 Telegram 原文报错发给用户。
    """
    err_text = str(context.error)
    if ("Message is not modified" in err_text
            or "Query is too old" in err_text):
        logger.debug("常规 Telegram 情况，不打扰用户：%s", err_text)
        return
    if "Message to edit not found" in err_text:
        logger.warning("要编辑的消息已不在：%s", err_text)
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            try:
                lang = lang_of(update) if update is not None else "zh"
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=bot_i18n.t(lang, "error.message_gone"))
            except Exception:  # noqa: BLE001 - 通知失败也不要再抛
                logger.exception("向用户发送错误通知失败")
        return
    logger.exception("处理 Telegram 更新时出错：%s", context.error)
    message = getattr(update, "effective_message", None)
    if message is not None:
        try:
            lang = lang_of(update) if update is not None else "zh"
            await message.reply_text(fmt_error(context.error, lang=lang))
        except Exception:  # noqa: BLE001 - 通知失败也不要再抛
            logger.exception("向用户发送错误通知失败")
