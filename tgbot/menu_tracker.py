"""每聊天只留一个活动选择框（用户要求 2026-11-24：点完删掉多余的
选择框，聊天里不要堆一堆旧菜单）。

规则（reply() 统一执行，三个 handler 模块的 _reply 都是它的薄包装）：
- 带 inline 键盘的消息 = 选择框（菜单），每聊天同时只留一个活的；
  发新选择框时先删掉旧的。
- 不带键盘的消息 = 结果/通知；发结果时选择框已完成使命，一并删掉，
  只留结果。
- 点按钮（回调）时，先发新消息，再删被点的那条（回复要先发，删了
  就没法回复它）；同一回调里多次回复时，只有第一次走回复，之后改
  走 bot.send_message（被点的消息已删，回复它会报 Bad Request）。
- 删除失败（消息已删 / 超 48 小时 / 无权限）只记日志，不影响主流程。

本模块不 import 任何 tgbot 模块（handlers.py 与 handlers_extra 互相
引用，只能由不依赖它们的模块提供共享逻辑）。
"""

import logging

from telegram.error import BadRequest

logger = logging.getLogger(__name__)


def _markup_equal(old, new):
    """比对两份 inline 键盘是否相同（None 与空键盘视为相同）。"""
    def _norm(markup):
        if markup is None:
            return None
        to_dict = getattr(markup, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            if not data.get("inline_keyboard"):
                return None
            return data
        return markup
    return _norm(old) == _norm(new)


async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    """编辑回调消息（编辑前比对，消息不存在时改发新消息）。

    新内容与 markup 和当前消息完全相同时跳过编辑，不调 API，
    不抛 "Message is not modified"；消息已删/不存在时降级为发
    一条新消息，不把 Telegram 原文报错抛给调用方；其他
    BadRequest 原样抛出。返回编辑后或新发的消息；跳过时返回 None。
    """
    message = getattr(query, "message", None)
    if message is not None:
        if (message.text == text
                and _markup_equal(message.reply_markup, reply_markup)):
            logger.debug("编辑内容与当前消息相同，跳过")
            return None
    try:
        return await query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as exc:
        err = str(exc)
        if "Message is not modified" in err:
            logger.debug("编辑内容未变化，跳过")
            return None
        if "Message to edit not found" in err:
            logger.warning("要编辑的消息已不在，改发新消息")
            chat = getattr(message, "chat", None)
            if chat is not None:
                return await chat.send_message(
                    text, reply_markup=reply_markup, parse_mode=parse_mode)
            return None
        raise


# user_data key: the message id of the currently active picker. user_data 里的键：当前活动的选择框消息 ID。
LIVE_MENU_KEY = "live_menu_id"
# user_data key: the callback query.id of the last cleaned-up tapped message (when one callback replies several times, only the first deletes the tapped message and replies; later ones use send_message). user_data 里的键：上一次已清理被点消息的回调 query.id（同一回调多次回复时，只有第一次删被点消息、走回复；之后走 send_message）。
_CLEANED_QUERY_KEY = "_menu_cleaned_query"


async def _delete_message_safe(bot, chat_id, message_id):
    """删一条消息；失败只记 debug 日志，不影响主流程。"""
    if bot is None or not chat_id or not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
        logger.debug("删除消息 %s 失败：%s", message_id, exc)


async def delete_live_menu(context, chat_id, except_id=None):
    """删除当前跟踪的活动选择框（except_id 的那条不删），并清空跟踪。"""
    user_data = context.user_data
    old_id = user_data.pop(LIVE_MENU_KEY, None)
    if old_id is not None and old_id != except_id:
        await _delete_message_safe(context.bot, chat_id, old_id)


def forget_live_menu(context):
    """清空活动选择框跟踪（不删消息——消息已被调用方编辑成结果）。"""
    context.user_data.pop(LIVE_MENU_KEY, None)


async def reply(update, context, text, reply_markup=None, parse_mode=None):
    """统一回复（带选择框清理），返回新发送的消息，没有可发时返回 None。

    三个 handler 模块的 _reply 都是它的薄包装（调用点保持 _reply
    的写法，只补一个 context 参数）。
    """
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
    message = update.effective_message
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    user_data = context.user_data
    query_id = getattr(query, "id", None) if query is not None else None
    tapped_gone = (
        query is not None
        and user_data.get(_CLEANED_QUERY_KEY) == query_id
    )
    sent = None
    if message is not None and not tapped_gone:
        sent = await message.reply_text(text, reply_markup=reply_markup,
                                        parse_mode=parse_mode)
    elif chat_id is not None:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup,
            parse_mode=parse_mode)
    else:
        return None
    sent_id = getattr(sent, "message_id", None)
    if chat_id is not None:
        # Delete the tapped message first (only once per callback), then the previously tracked picker. 先删被点的消息（回调时，只删一次），再删之前跟踪的选择框。
        if query is not None and not tapped_gone:
            user_data[_CLEANED_QUERY_KEY] = query_id
            if query.message is not None:
                await _delete_message_safe(
                    context.bot, chat_id, query.message.message_id)
        await delete_live_menu(context, chat_id, except_id=sent_id)
    # Tracker update: with a keyboard = a new picker; without = a result, and the picker's job is done. 跟踪更新：带键盘 = 新选择框；不带 = 结果，选择框使命完成。
    if reply_markup is not None:
        if chat_id is not None and sent_id is not None:
            user_data[LIVE_MENU_KEY] = sent_id
    else:
        user_data.pop(LIVE_MENU_KEY, None)
    return sent
