#!/usr/bin/env python3
"""Telegram 救机入口（v2.01，菜单 44）：前置检查 / 安全恢复 /
无盘救机 / 孤儿盘清理 / Netboot 向导 / 控制台连接管理。

命令：/rescue；/oracle 菜单第 44 项；回调前缀 "rs:"。
核心逻辑在 oci/rescue.py（Web 与 Bot 共用），这里只做文案与接线。
实例/备份/孤儿盘以下标存进 context.user_data（回调 64 字节限制，
OCID 放不下），与实例列表的做法一致。
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from oci.boot_backup import list_boot_volume_backups
from oci.instance import list_instances
from oci.rescue import (
    create_console,
    delete_boot_volume_guarded,
    delete_console,
    list_consoles,
    list_rescue_orphans,
    netboot_guide,
    plan_diskless_rescue,
    preflight_check,
    rebuild_diskless_from_backup,
    safe_restore,
    total_rescue_orphan_gb,
)
from tgbot import menu_tracker
from tgbot.keyboards import back_to_oracle_keyboard
from tgbot.services import (
    fmt_error,
    get_cli,
    get_compartment,
    require_allowed,
    run_sync,
)

logger = logging.getLogger(__name__)

# ================= 文案（纯函数，单元测试直接覆盖） =================

RESCUE_MENU_TEXT = (
    "🛟 救机\n\n"
    "按 ARM 真机实测结论实现：无盘实例无法原地救回，只能从备份新建实例；"
    "恢复盘在成为某实例启动盘之前没有归属实例，attach 一律被拒。\n\n"
    "选一项："
)

PREFLIGHT_ACTIONS = [
    ("start", "开机"),
    ("restore", "安全恢复"),
    ("delete_volume", "删盘"),
    ("terminate", "终止实例"),
    ("rebuild_from_backup", "无盘救机"),
]


def rescue_menu_keyboard():
    """救机主菜单键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 前置检查", callback_data="rs:preflight"),
         InlineKeyboardButton("♻️ 安全恢复", callback_data="rs:safe")],
        [InlineKeyboardButton("🆘 无盘救机", callback_data="rs:diskless"),
         InlineKeyboardButton("🧹 孤儿盘清理", callback_data="rs:orphans")],
        [InlineKeyboardButton("🌐 Netboot 向导", callback_data="rs:netboot"),
         InlineKeyboardButton("🖥️ 控制台连接", callback_data="rs:console")],
        [InlineKeyboardButton("📋 控制台列表", callback_data="rs:conlist")],
        [InlineKeyboardButton("⬅️ 回 Oracle 菜单", callback_data="o:menu")],
    ])


def format_preflight_text(result):
    """前置检查结果全文。"""
    lines = ["🔍 前置检查：" + ("✅ 可以做" if result.get("can_do")
                              else "⛔ 不能做"), ""]
    for blocker in result.get("blockers") or []:
        lines.append(f"⛔ {blocker}")
    for warning in result.get("warnings") or []:
        lines.append(f"⚠️ {warning}")
    facts = result.get("facts") or {}
    lines.append("")
    lines.append(f"实例状态：{facts.get('instance_state') or '?'}")
    lines.append("启动盘：" + ("已挂载" if facts.get("has_boot_volume")
                              else "未挂载"))
    if facts.get("remaining_gb") is not None:
        lines.append(f"启动盘剩余额度：{facts['remaining_gb']} GB")
    if facts.get("is_backup_source"):
        lines.append(f"该盘是 {facts.get('backup_count')} 个备份的源盘")
    return "\n".join(lines)


def format_orphan_list_text(orphans, total_gb):
    """孤儿盘列表全文。"""
    if not orphans:
        return "🧹 没有孤儿盘（无挂载记录且源自备份恢复的启动盘）。"
    lines = [f"🧹 孤儿盘（{len(orphans)} 块，共占 {total_gb} GB 额度）：", ""]
    for idx, orphan in enumerate(orphans):
        name = orphan.get("display-name") or orphan.get("id") or "?"
        lines.append(f"{idx + 1}. {name}（{orphan.get('size-in-gbs')} GB）")
    lines.append("")
    lines.append("点一条删盘腾额度（删了不可恢复）。")
    return "\n".join(lines)


def format_diskless_plan_text(plan):
    """无盘救机方案全文。"""
    lines = [plan.get("message") or "", "", plan.get("cost_note") or ""]
    if plan.get("can_rebuild"):
        lines.append("")
        lines.append("可用备份：")
        for idx, backup in enumerate(plan.get("backups") or []):
            name = backup.get("display-name") or backup.get("id") or "?"
            lines.append(
                f"{idx + 1}. {name}（{backup.get('size-in-gbs')} GB）")
        lines.append("")
        lines.append("选一个备份，从它新建实例。")
    return "\n".join(lines)


def format_netboot_text(guide):
    """Netboot 向导全文。"""
    lines = ["🌐 Netboot 救机向导", "",
             f"适用场景：{guide.get('applies_when')}", "", "先决条件："]
    for item in guide.get("prerequisites") or []:
        lines.append(f"• {item}")
    lines.append("")
    lines.append("控制台步骤：")
    for idx, step in enumerate(guide.get("console_steps") or []):
        lines.append(f"{idx + 1}. {step.get('text')}"
                     + ("（手动）" if step.get("manual") else ""))
    lines.append("")
    lines.append("EFI Shell 命令：")
    for cmd in guide.get("efi_commands") or []:
        lines.append(f"  {cmd}")
    lines.append("")
    lines.append(f"TFTP 节点：{guide.get('tftp_server')}")
    lines.append("")
    lines.append("netboot.xyz 菜单指引：")
    for idx, step in enumerate(guide.get("menu_steps") or []):
        lines.append(f"{idx + 1}. {step.get('text')}（手动）")
    lines.append("")
    lines.append("DD 步骤：")
    for idx, step in enumerate(guide.get("dd_steps") or []):
        lines.append(f"{idx + 1}. {step}")
    return "\n".join(lines)


def format_console_text(name, console):
    """串行控制台连接信息全文（连接命令单独成行，方便复制）。"""
    lines = [f"🖥️ {name} 的串行控制台：", ""]
    lines.append("连接命令：")
    lines.append(console.get("connection-string") or "（未返回连接命令）")
    state = console.get("lifecycle-state")
    if state:
        lines.append(f"状态：{state}")
    fingerprint = console.get("fingerprint")
    if fingerprint:
        lines.append(f"指纹：{fingerprint}")
    lines.append("")
    lines.append("用实例的 SSH 密钥连接；首次连接需接受主机密钥。")
    return "\n".join(lines)


# ================= 内部工具 =================

async def _reply(update, context, text, reply_markup=None, parse_mode=None):
    return await menu_tracker.reply(
        update, context, text, reply_markup=reply_markup,
        parse_mode=parse_mode)


def _back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ 回救机菜单", callback_data="rs:menu")],
        [InlineKeyboardButton("⬅️ 回 Oracle 菜单", callback_data="o:menu")],
    ])


def _load_instances(context):
    """查当前账号实例列表并存进 user_data（下标供回调取回）。"""
    instances = list_instances(get_cli(context), get_compartment(context))
    context.user_data["rescue_instances"] = list(instances)
    return context.user_data["rescue_instances"]


def _resolve_rescue_instance(context, idx):
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    instances = context.user_data.get("rescue_instances") or []
    if 0 <= idx < len(instances):
        return instances[idx]
    return None


def _instance_picker_keyboard(prefix, instances):
    rows = []
    for idx, inst in enumerate(instances):
        name = inst.get("display-name") or inst.get("id") or "?"
        state = inst.get("lifecycle-state") or "?"
        rows.append([InlineKeyboardButton(
            f"{name}（{state}）",
            callback_data=f"{prefix}:{idx}")])
    rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                      callback_data="rs:menu")])
    return InlineKeyboardMarkup(rows)


def _resolve_rescue_backup(context, idx):
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    backups = context.user_data.get("rescue_backups") or []
    if 0 <= idx < len(backups):
        return backups[idx]
    return None


# ================= 入口 =================

async def _show_menu(update, context, edit=False):
    query = update.callback_query if update else None
    if edit and query is not None:
        await query.edit_message_text(RESCUE_MENU_TEXT,
                                      reply_markup=rescue_menu_keyboard())
        return
    await _reply(update, context, RESCUE_MENU_TEXT,
                 reply_markup=rescue_menu_keyboard())


@require_allowed
async def rescue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /rescue：救机主菜单。"""
    await _show_menu(update, context)


@require_allowed
async def handle_rescue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Oracle 菜单第 44 项“救机”的 dispatch 入口（与 /rescue 同菜单）。"""
    await _show_menu(update, context)


# ================= 回调 =================

@require_allowed
async def rescue_callback(update: Update,
                          context: ContextTypes.DEFAULT_TYPE):
    """处理 rs:* 回调。"""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""

    if action == "menu":
        await _show_menu(update, context, edit=True)
        return

    if action in ("preflight", "safe", "diskless", "netboot", "console"):
        try:
            instances = await run_sync(_load_instances, context)
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("救机查实例列表失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        if not instances:
            await query.edit_message_text(
                "当前账号下没有实例。", reply_markup=_back_keyboard())
            return
        titles = {
            "preflight": "🔍 前置检查：先选一台实例",
            "safe": "♻️ 安全恢复：先选一台实例",
            "diskless": "🆘 无盘救机：先选一台实例",
            "netboot": "🌐 Netboot 向导：先选一台实例",
            "console": "🖥️ 控制台连接：先选一台实例",
        }
        prefixes = {
            "preflight": "rs:pfpick",
            "safe": "rs:safei",
            "diskless": "rs:dli",
            "netboot": "rs:nbi",
            "console": "rs:coni",
        }
        await query.edit_message_text(
            titles[action],
            reply_markup=_instance_picker_keyboard(prefixes[action],
                                                   instances))
        return

    if action == "pfpick":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        idx = parts[2]
        name = inst.get("display-name") or "?"
        rows = [[InlineKeyboardButton(label, callback_data=f"rs:pf:{idx}:{key}")]
                for key, label in PREFLIGHT_ACTIONS]
        rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                          callback_data="rs:menu")])
        await query.edit_message_text(
            f"🔍 前置检查：实例「{name}」，要检查哪个操作？",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "pf":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        op = parts[3] if len(parts) > 3 else ""
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        try:
            result = await run_sync(
                preflight_check, get_cli(context),
                get_compartment(context), inst["id"], op)
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("前置检查失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text(format_preflight_text(result),
                                      reply_markup=_back_keyboard())
        return

    if action == "safei":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        try:
            backups = await run_sync(
                list_boot_volume_backups, get_cli(context),
                get_compartment(context))
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("救机查备份列表失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        backups = [b for b in backups
                   if b.get("lifecycle-state") == "AVAILABLE"]
        if not backups:
            await query.edit_message_text(
                "当前没有任何可用备份，无法安全恢复。请先给启动盘建备份。",
                reply_markup=_back_keyboard())
            return
        context.user_data["rescue_backups"] = backups
        idx = parts[2]
        rows = []
        for bidx, backup in enumerate(backups):
            name = backup.get("display-name") or backup.get("id") or "?"
            rows.append([InlineKeyboardButton(
                f"{name}（{backup.get('size-in-gbs')} GB）",
                callback_data=f"rs:safeb:{idx}:{bidx}")])
        rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                          callback_data="rs:menu")])
        await query.edit_message_text(
            f"♻️ 安全恢复：实例「{inst.get('display-name') or '?'}」，选一个备份：",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "safeb":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        backup = _resolve_rescue_backup(context, parts[3] if len(parts) > 3
                                        else "")
        if inst is None or backup is None:
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        rows = [[InlineKeyboardButton(
            "✅ 确认恢复（先建后删，实例会停机一次）",
            callback_data=f"rs:safeok:{parts[2]}:{parts[3]}")],
            [InlineKeyboardButton("❌ 取消", callback_data="rs:menu")]]
        await query.edit_message_text(
            "♻️ 确认安全恢复？\n\n"
            f"实例：{inst.get('display-name') or '?'}\n"
            f"备份：{backup.get('display-name') or '?'}\n\n"
            "流程：从备份建新盘 → 原生换盘替换旧盘 → 再删旧盘。"
            "额度不够会在开始前拦截；旧盘是备份源盘时不自动删旧盘。",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "safeok":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        backup = _resolve_rescue_backup(context, parts[3] if len(parts) > 3
                                        else "")
        if inst is None or backup is None:
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text("♻️ 安全恢复进行中（约 3 分钟），请稍等…")
        try:
            result = await run_sync(
                safe_restore, get_cli(context),
                get_compartment(context), inst["id"], backup["id"])
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("安全恢复失败：%s", exc)
            await query.edit_message_text(f"❌ 安全恢复失败：{fmt_error(exc)}",
                                          reply_markup=_back_keyboard())
            return
        lines = ["✅ 安全恢复完成。",
                 f"新盘：{result.get('new_boot_volume_id') or '?'}",
                 "旧盘已删除：" + ("是" if result.get("old_volume_deleted")
                                  else "否（见警告）")]
        for warning in result.get("warnings") or []:
            lines.append(f"⚠️ {warning}")
        await query.edit_message_text("\n".join(lines),
                                      reply_markup=_back_keyboard())
        return

    if action == "dli":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        try:
            plan = await run_sync(plan_diskless_rescue, get_cli(context),
                                  get_compartment(context), inst["id"])
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("无盘救机方案查询失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        context.user_data["rescue_backups"] = plan.get("backups") or []
        idx = parts[2]
        rows = []
        for bidx, backup in enumerate(plan.get("backups") or []):
            name = backup.get("display-name") or backup.get("id") or "?"
            rows.append([InlineKeyboardButton(
                f"从「{name}」新建实例",
                callback_data=f"rs:dlb:{idx}:{bidx}")])
        rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                          callback_data="rs:menu")])
        await query.edit_message_text(format_diskless_plan_text(plan),
                                      reply_markup=InlineKeyboardMarkup(rows)
                                      if rows else _back_keyboard())
        return

    if action == "dlb":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        backup = _resolve_rescue_backup(context, parts[3] if len(parts) > 3
                                        else "")
        if inst is None or backup is None:
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        rows = [[InlineKeyboardButton(
            "✅ 确认新建实例",
            callback_data=f"rs:dlok:{parts[2]}:{parts[3]}")],
            [InlineKeyboardButton("❌ 取消", callback_data="rs:menu")]]
        await query.edit_message_text(
            "🆘 确认从备份新建实例？\n\n"
            f"原实例：{inst.get('display-name') or '?'}（不动，事后手动终止）\n"
            f"备份：{backup.get('display-name') or '?'}\n\n"
            "代价：新实例有新的 OCID 与新的公网 IP（旧 IP 不继承），"
            "旧实例无法复活。",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "dlok":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        backup = _resolve_rescue_backup(context, parts[3] if len(parts) > 3
                                        else "")
        if inst is None or backup is None:
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text("🆘 正在从备份新建实例（约 2 分钟），请稍等…")
        try:
            from web.tasks import PLAN_DEFAULTS
            name = (inst.get("display-name") or "instance") + "-rescued"
            result = await run_sync(
                rebuild_diskless_from_backup, get_cli(context),
                get_compartment(context), inst["id"], backup["id"],
                availability_domain=PLAN_DEFAULTS["availability_domain"],
                subnet_id=PLAN_DEFAULTS["subnet_id"],
                display_name=name,
                ssh_keys_file=PLAN_DEFAULTS["ssh_keys_file"])
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("无盘救机新建实例失败：%s", exc)
            await query.edit_message_text(f"❌ 新建实例失败：{fmt_error(exc)}",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text(
            "✅ 新实例已创建：\n\n"
            f"实例 OCID：{result.get('instance_id')}\n"
            f"形状：{result.get('shape')}，"
            f"{result.get('ocpus')} 核 / {result.get('memory_gb')} GB\n\n"
            "⚠️ 新实例是新的 OCID 与新的公网 IP，旧实例无法复活；"
            "确认数据无误后再终止旧实例。",
            reply_markup=_back_keyboard())
        return

    if action == "orphans":
        try:
            found = await run_sync(list_rescue_orphans, get_cli(context),
                                   get_compartment(context))
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("孤儿盘查询失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        context.user_data["rescue_orphans"] = found
        total_gb = total_rescue_orphan_gb(found)
        rows = []
        for idx, orphan in enumerate(found):
            name = orphan.get("display-name") or orphan.get("id") or "?"
            rows.append([InlineKeyboardButton(
                f"删「{name}」（{orphan.get('size-in-gbs')} GB）",
                callback_data=f"rs:odel:{idx}")])
        rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                          callback_data="rs:menu")])
        await query.edit_message_text(
            format_orphan_list_text(found, total_gb),
            reply_markup=InlineKeyboardMarkup(rows)
            if found else _back_keyboard())
        return

    if action == "odel":
        orphans = context.user_data.get("rescue_orphans") or []
        try:
            idx = int(parts[2])
            orphan = orphans[idx]
        except (IndexError, TypeError, ValueError):
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        rows = [[InlineKeyboardButton(
            "✅ 确认删除",
            callback_data=f"rs:odelok:{parts[2]}")],
            [InlineKeyboardButton("❌ 取消", callback_data="rs:menu")]]
        await query.edit_message_text(
            "🧹 确认删除这块孤儿盘腾额度？\n\n"
            f"{orphan.get('display-name') or '?'}"
            f"（{orphan.get('size-in-gbs')} GB）\n\n"
            "删了不可恢复。",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "odelok":
        orphans = context.user_data.get("rescue_orphans") or []
        try:
            idx = int(parts[2])
            orphan = orphans[idx]
        except (IndexError, TypeError, ValueError):
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        try:
            await run_sync(delete_boot_volume_guarded, get_cli(context),
                           get_compartment(context), orphan["id"],
                           confirm=True)
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("删除孤儿盘失败：%s", exc)
            await query.edit_message_text(f"❌ 删除失败：{fmt_error(exc)}",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text(
            f"✅ 孤儿盘「{orphan.get('display-name') or '?'}」已删除，"
            f"腾出 {orphan.get('size-in-gbs')} GB 额度。",
            reply_markup=_back_keyboard())
        return

    if action == "nbi":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        guide = netboot_guide(inst["id"])
        rows = [[InlineKeyboardButton(
            "🖥️ 创建串行控制台连接",
            callback_data=f"rs:console:{parts[2]}")],
            [InlineKeyboardButton("⬅️ 回救机菜单",
                                  callback_data="rs:menu")]]
        await query.edit_message_text(format_netboot_text(guide),
                                      reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "coni":
        inst = _resolve_rescue_instance(context, parts[2] if len(parts) > 2
                                        else "")
        if inst is None:
            await query.edit_message_text("实例已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text("🖥️ 正在创建串行控制台连接（约 11 秒）…")
        try:
            console = await run_sync(create_console, get_cli(context),
                                     inst["id"])
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("创建控制台连接失败：%s", exc)
            await query.edit_message_text(f"❌ 创建失败：{fmt_error(exc)}",
                                          reply_markup=_back_keyboard())
            return
        name = inst.get("display-name") or "?"
        await query.edit_message_text(format_console_text(name, console),
                                      reply_markup=_back_keyboard())
        return

    if action == "console":
        # rs:console:<idx>: the "创建控制台连接" button in the Netboot guide. rs:console:<idx>：Netboot 向导里的“创建控制台连接”按钮。
        if len(parts) >= 3:
            query.data = f"rs:coni:{parts[2]}"
            await rescue_callback(update, context)
            return
        await query.edit_message_text(
            "请先在“控制台连接”里选一台实例。",
            reply_markup=_back_keyboard())
        return

    if action == "conlist":
        try:
            connections = await run_sync(list_consoles, get_cli(context),
                                         get_compartment(context))
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("控制台连接查询失败：%s", exc)
            await query.edit_message_text(fmt_error(exc),
                                          reply_markup=_back_keyboard())
            return
        context.user_data["rescue_consoles"] = connections
        if not connections:
            await query.edit_message_text(
                "当前没有串行控制台连接。", reply_markup=_back_keyboard())
            return
        lines = ["🖥️ 串行控制台连接：", ""]
        rows = []
        for idx, conn in enumerate(connections):
            lines.append(f"{idx + 1}. {conn.get('instance-id') or '?'}"
                         f"（{conn.get('lifecycle-state') or '?'}）")
            rows.append([InlineKeyboardButton(
                f"删连接 {idx + 1}",
                callback_data=f"rs:condel:{idx}")])
        rows.append([InlineKeyboardButton("⬅️ 回救机菜单",
                                          callback_data="rs:menu")])
        await query.edit_message_text("\n".join(lines),
                                      reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "condel":
        connections = context.user_data.get("rescue_consoles") or []
        try:
            idx = int(parts[2])
            conn = connections[idx]
        except (IndexError, TypeError, ValueError):
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        rows = [[InlineKeyboardButton(
            "✅ 确认删除",
            callback_data=f"rs:condelok:{parts[2]}")],
            [InlineKeyboardButton("❌ 取消", callback_data="rs:menu")]]
        await query.edit_message_text(
            "🖥️ 确认删除这个串行控制台连接？\n\n"
            f"连接：{conn.get('id') or '?'}",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if action == "condelok":
        connections = context.user_data.get("rescue_consoles") or []
        try:
            idx = int(parts[2])
            conn = connections[idx]
        except (IndexError, TypeError, ValueError):
            await query.edit_message_text("选择已失效，请回救机菜单重选。",
                                          reply_markup=_back_keyboard())
            return
        try:
            await run_sync(delete_console, get_cli(context), conn["id"])
        except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
            logger.warning("删除控制台连接失败：%s", exc)
            await query.edit_message_text(f"❌ 删除失败：{fmt_error(exc)}",
                                          reply_markup=_back_keyboard())
            return
        await query.edit_message_text("✅ 串行控制台连接已删除。",
                                      reply_markup=_back_keyboard())
        return

    await query.edit_message_text("参数错误，请回救机菜单重试。",
                                  reply_markup=_back_keyboard())
