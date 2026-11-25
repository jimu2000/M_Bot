#!/usr/bin/env python3
"""实例详情“绑定域名”（Bot 端，v2.01）。

流程：实例详情点“🌐 绑定域名”→ 选主域名（Cloudflare Zone）
→ 输二级域名（主机名，@ 表示根域名）→ 建 A 记录指向实例公网 IP。

复用现有逻辑，不另起一套：
  - Cloudflare 凭据与客户端：tgbot.cf_config.get_cf_client
    （文件 > oci_config [cloudflare] 段 > 环境变量），与 /domain 一致；
  - DNS 调用：clouds.cloudflare.CloudflareClient 的
    list_zones / list_dns_records / create_dns_record /
    update_dns_record，与 /domain 与 Web DNS 路由一致。

状态存在 context.user_data["bind_domain_pending"]（不用
pending_input，避免与 handlers_extra.text_input_callback 的
分发冲突——它只认 pending_input，没有时直接忽略，本模块的
文字输入由 bot.py 里单独注册的 MessageHandler 处理）。
"""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from tgbot import i18n as bot_i18n
from tgbot import menu_tracker
from tgbot.i18n import lang_of
from tgbot.keyboards import back_to_oracle_keyboard, cf_config_keyboard
from tgbot.services import require_allowed, resolve_instance, run_sync

logger = logging.getLogger(__name__)

try:
    from tgbot.cf_config import get_cf_client as get_configured_cf_client
except ImportError:  # pragma: no cover - cf_config 未落地时回落
    get_configured_cf_client = None

try:
    from clouds.cloudflare import CloudflareError, get_client
except ImportError:  # pragma: no cover - clouds 模块缺失时降级
    get_client = None

    class CloudflareError(Exception):
        """clouds.cloudflare 未就绪时的替身（有 .kind 字段）。"""

        def __init__(self, message="", kind="api"):
            super().__init__(message)
            self.kind = kind


# A subdomain only allows letters, digits, -, _, ., or a lone @ (the root domain). 二级域名只允许字母数字、-、_、.，或单独一个 @（根域名）。
_SUBDOMAIN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def validate_subdomain(text):
    """校验二级域名输入。

    返回 (ok, normalized)：ok 时 normalized 是小写去尾点后的主机名
    （"@" 表示根域名）；格式不对时返回 (False, None)。
    """
    sub = (text or "").strip().lower().rstrip(".")
    if sub == "@":
        return True, "@"
    if not sub or len(sub) > 253:
        return False, None
    if not _SUBDOMAIN_RE.match(sub):
        return False, None
    if sub.startswith(("-", ".")) or sub.endswith(("-", ".")):
        return False, None
    if ".." in sub:
        return False, None
    if any(not label or len(label) > 63 for label in sub.split(".")):
        return False, None
    return True, sub


def build_fqdn(subdomain, zone_name):
    """拼完整域名：@ 用 Zone 名本身，其余拼到 Zone 名前面。"""
    if subdomain == "@":
        return zone_name
    return f"{subdomain}.{zone_name}"


def find_existing_a_record(client, zone_id, fqdn):
    """查同名 A 记录（按完整域名过滤，返回第一条，命中不到返回 None）。"""
    records = client.list_dns_records(zone_id, type="A", name=fqdn) or []
    for record in records:
        if (record.get("name") or "").lower() == fqdn.lower():
            return record
    return None


def bind_a_record(client, zone_id, fqdn, ip, overwrite=False):
    """建 A 记录（fqdn → ip），返回 (action, record)。

    action：created（新建）/ updated（覆盖旧记录）/
    exists_same（已是同一指向，不用动）/ exists_conflict
    （已有不同指向的记录，overwrite=False 时不动，等用户确认）。
    """
    existing = find_existing_a_record(client, zone_id, fqdn)
    if existing is not None:
        if existing.get("content") == ip:
            return "exists_same", existing
        if not overwrite:
            return "exists_conflict", existing
        updated = client.update_dns_record(
            zone_id, existing.get("id"), "A", fqdn, ip)
        return "updated", updated
    created = client.create_dns_record(zone_id, "A", fqdn, ip)
    return "created", created


def _get_cf_client():
    """取 Cloudflare 客户端（与 handlers_extra._get_cf_client 同源）。"""
    if get_configured_cf_client is not None:
        return get_configured_cf_client()
    if get_client is None:
        raise CloudflareError("Cloudflare 客户端未就绪", kind="config")
    return get_client()


def _cf_error_text(exc, lang="zh"):
    """CloudflareError 转提示文案（不泄露 API 原始错误细节）。"""
    kind = getattr(exc, "kind", "api")
    if kind in ("config", "auth"):
        return bot_i18n.t(lang, "bind_domain.cf_auth")
    if kind == "not_found":
        return bot_i18n.t(lang, "bind_domain.cf_not_found")
    if kind == "rate_limit":
        return bot_i18n.t(lang, "bind_domain.cf_rate_limit")
    return bot_i18n.t(lang, "bind_domain.cf_failed")


async def _reply(update, context, text, reply_markup=None):
    return await menu_tracker.reply(
        update, context, text, reply_markup=reply_markup)


def bind_domain_zones_keyboard(zones, idx, lang="zh"):
    """绑定域名：Zone 选择键盘（每 Zone 一个按钮 + 取消）。"""
    rows = []
    for zidx, zone in enumerate(zones):
        name = zone.get("name") or zone.get("id", "?")
        status = zone.get("status") or ""
        label = f"🌐 {name}" + (f"（{status}）" if status != "active" else "")
        rows.append([InlineKeyboardButton(
            label, callback_data=f"bd:zone:{idx}:{zidx}")])
    rows.append([InlineKeyboardButton(
        bot_i18n.t(lang, "common.cancel_button"),
        callback_data="bd:cancel")])
    return InlineKeyboardMarkup(rows)


def bind_domain_cancel_keyboard(lang="zh"):
    """绑定域名：等用户输二级域名时的取消键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_i18n.t(lang, "common.cancel_button"),
                              callback_data="bd:cancel")],
    ])


def bind_domain_overwrite_keyboard(lang="zh"):
    """绑定域名：同名 A 记录已存在时的确认覆盖键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(bot_i18n.t(lang, "bind_domain.overwrite"),
                              callback_data="bd:overwrite")],
        [InlineKeyboardButton(bot_i18n.t(lang, "common.cancel_button"),
                              callback_data="bd:cancel")],
    ])


def _clear_pending(context):
    context.user_data.pop("bind_domain_pending", None)
    context.user_data.pop("bind_domain_zones", None)


async def _start(update, context, idx, lang):
    """bd:start：入口。无公网 IP 或未配凭据时只提示，不进 Zone 选择。"""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context,
                     bot_i18n.t(lang, "common.instance_gone"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    name = inst.get("display-name") or inst.get("id", "?")[-4:]
    ip = inst.get("public-ip")
    if not ip:
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.no_ip", name=name),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    try:
        client = _get_cf_client()
        zones = await run_sync(client.list_zones)
    except CloudflareError as exc:
        if getattr(exc, "kind", "api") == "config":
            await _reply(update, context,
                         bot_i18n.t(lang, "bind_domain.no_credentials"),
                         reply_markup=cf_config_keyboard())
        else:
            await _reply(update, context, _cf_error_text(exc, lang),
                         reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    except Exception as exc:  # noqa: BLE001 - 其他异常统一转提示
        logger.warning("绑定域名列 Zone 失败：%s", exc)
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.cf_failed"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    if not zones:
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.no_zones"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    context.user_data["bind_domain_zones"] = list(zones)
    context.user_data["bind_domain_pending"] = {
        "idx": str(idx), "ip": ip, "name": name}
    await _reply(update, context,
                 bot_i18n.t(lang, "bind_domain.pick_zone",
                            name=name, ip=ip),
                 reply_markup=bind_domain_zones_keyboard(zones, idx,
                                                         lang=lang))


async def _pick_zone(update, context, idx, zidx, lang):
    """bd:zone：选好主域名，等用户输二级域名。"""
    pending = context.user_data.get("bind_domain_pending") or {}
    zones = context.user_data.get("bind_domain_zones") or []
    if pending.get("idx") != str(idx) or not (0 <= zidx < len(zones)):
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.expired"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    zone = zones[zidx]
    pending["zone_id"] = zone.get("id")
    pending["zone_name"] = zone.get("name")
    context.user_data["bind_domain_pending"] = pending
    await _reply(update, context,
                 bot_i18n.t(lang, "bind_domain.input_subdomain",
                            zone=zone.get("name")),
                 reply_markup=bind_domain_cancel_keyboard(lang=lang))


async def _overwrite(update, context, lang):
    """bd:overwrite：确认覆盖已存在的同名 A 记录。"""
    pending = context.user_data.get("bind_domain_pending") or {}
    if not pending.get("existing_id") or not pending.get("fqdn"):
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.expired"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    try:
        client = _get_cf_client()
        await run_sync(client.update_dns_record,
                       pending["zone_id"], pending["existing_id"],
                       "A", pending["fqdn"], pending["ip"])
    except CloudflareError as exc:
        await _reply(update, context, _cf_error_text(exc, lang),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return
    fqdn, ip = pending["fqdn"], pending["ip"]
    old_ip = pending.get("existing_ip") or "?"
    _clear_pending(context)
    await _reply(update, context,
                 bot_i18n.t(lang, "bind_domain.done_updated",
                            fqdn=fqdn, ip=ip, old_ip=old_ip),
                 reply_markup=back_to_oracle_keyboard(lang=lang))


@require_allowed
async def bind_domain_callback(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """处理 bd:* 回调：开始 / 选 Zone / 确认覆盖 / 取消。"""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) >= 2 else ""
    lang = lang_of(update, context)
    if action == "start":
        await _start(update, context, parts[2] if len(parts) >= 3 else "",
                     lang)
    elif action == "zone":
        try:
            zidx = int(parts[3]) if len(parts) >= 4 else -1
        except ValueError:
            zidx = -1
        await _pick_zone(update, context,
                         parts[2] if len(parts) >= 3 else "", zidx, lang)
    elif action == "overwrite":
        await _overwrite(update, context, lang)
    elif action == "cancel":
        _clear_pending(context)
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.cancelled"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
    else:
        await _reply(update, context,
                     bot_i18n.t(lang, "common.param_error"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))


@require_allowed
async def bind_domain_text_input(update, context):
    """处理绑定域名流程里的二级域名文字输入。

    只在 user_data 有 bind_domain_pending 且已选 Zone 时处理，
    其余情况返回 False（交给其他文字输入 handler）。
    格式不对时保留流程、让用户重输；其余分支走完即清流程状态。
    """
    pending = context.user_data.get("bind_domain_pending")
    if not pending or not pending.get("zone_id"):
        return False
    lang = lang_of(update, context)
    text = (update.effective_message.text or "").strip()
    ok, sub = validate_subdomain(text)
    if not ok:
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.invalid_subdomain"),
                     reply_markup=bind_domain_cancel_keyboard(lang=lang))
        return True
    fqdn = build_fqdn(sub, pending["zone_name"])
    ip = pending["ip"]
    try:
        client = _get_cf_client()
        action, record = await run_sync(
            bind_a_record, client, pending["zone_id"], fqdn, ip)
    except CloudflareError as exc:
        await _reply(update, context, _cf_error_text(exc, lang),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return True
    except Exception as exc:  # noqa: BLE001 - 其他异常统一转提示
        logger.warning("绑定域名建记录失败：%s", exc)
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.cf_failed"),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return True
    if action == "exists_same":
        _clear_pending(context)
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.exists_same",
                                fqdn=fqdn, ip=ip),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return True
    if action == "created":
        _clear_pending(context)
        await _reply(update, context,
                     bot_i18n.t(lang, "bind_domain.done_created",
                                fqdn=fqdn, ip=ip),
                     reply_markup=back_to_oracle_keyboard(lang=lang))
        return True
    pending["fqdn"] = fqdn
    pending["existing_id"] = record.get("id")
    pending["existing_ip"] = record.get("content")
    context.user_data["bind_domain_pending"] = pending
    await _reply(update, context,
                 bot_i18n.t(lang, "bind_domain.exists_conflict",
                            fqdn=fqdn, old_ip=record.get("content"),
                            ip=ip),
                 reply_markup=bind_domain_overwrite_keyboard(lang=lang))
    return True
