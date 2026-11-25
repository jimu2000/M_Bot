#!/usr/bin/env python3
"""Telegram Bot API 发送辅助（阶段 5 工作包 E）。

Web 面板侧发 Telegram 消息用：Telegram 验证码登录（发登录码）、
巡检计划（发巡检摘要）。直接调 Bot API（httpx），不依赖
python-telegram-bot 的 Application（面板进程里没有 bot 实例）。

  - Token 与白名单用户复用 tgbot/config.py（环境变量
    MBOT_TELEGRAM_TOKEN / MBOT_ALLOWED_USERS）。
  - send_telegram_message(token, chat_id, text)：发一条消息，
    成功返回 True，失败返回 False（不抛异常，调用方按失败处理）。
  - send_to_allowed_users(text)：发给全部白名单用户，返回成功数。
  - 验证码等敏感内容绝不写日志（本模块不记任何日志）。

测试用 mock.patch 盖掉 httpx.post，不真的发消息。
"""

import httpx

# ================= 配置区（改这里） =================
# Bot API base URL. Bot API 基础地址。
TELEGRAM_API_BASE = "https://api.telegram.org"
# Timeout (seconds) per send. 单次发送超时（秒）。
SEND_TIMEOUT_SECONDS = 10
# =====================================================


def send_telegram_message(token, chat_id, text,
                          timeout=SEND_TIMEOUT_SECONDS):
    """调 Bot API 发一条纯文本消息，成功返回 True，失败返回 False。

    token/chat_id/text 缺失、网络异常、API 返回非 ok 都返回 False，
    不抛异常。消息内容不记日志。
    """
    if not token or chat_id is None or not text:
        return False
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        resp = httpx.post(url, json={"chat_id": chat_id, "text": text},
                          timeout=timeout)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        data = resp.json()
    except ValueError:
        return False
    return bool(isinstance(data, dict) and data.get("ok"))


def send_to_allowed_users(text):
    """把消息发给全部白名单用户，返回成功发送的用户数。

    Token 未配置或白名单为空时返回 0（不报错）。
    """
    from tgbot import config as bot_config

    token = bot_config.TELEGRAM_BOT_TOKEN
    user_ids = bot_config.ALLOWED_USER_IDS
    if not token or not user_ids:
        return 0
    sent = 0
    for user_id in sorted(user_ids):
        if send_telegram_message(token, user_id, text):
            sent += 1
    return sent
