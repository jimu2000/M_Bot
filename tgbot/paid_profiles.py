#!/usr/bin/env python3
"""按账号的“付费”标记：付费账号的启动卷备份不限 5 个。

用户要求（2026-11-24）：每账号一个“付费”开关，默认免费；
付费账号备份不限 5 个，免费账号备份超 5 个自动删最旧的。

存储：tgbot/data/paid_profiles.json（0600 权限），内容是
{"paid": ["PROFILE1", ...]}。bot 与网页端共用同一份文件
（网页端 import 本模块，路径走 bot_config.DATA_DIR，即
MBOT_TGBOT_DATA_DIR，部署时两端同 env 文件）。
"""

import json
import logging
import os

from tgbot import config as bot_config

logger = logging.getLogger(__name__)

PAID_PROFILES_FILE = os.path.join(bot_config.DATA_DIR,
                                  "paid_profiles.json")


def load_paid_profiles():
    """返回标记为付费的 profile 名集合；文件不存在/损坏返回空集。"""
    try:
        with open(PAID_PROFILES_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        logger.warning("付费账号配置读取失败（%s），按全部免费处理",
                       PAID_PROFILES_FILE, exc_info=exc)
        return set()
    paid = data.get("paid") if isinstance(data, dict) else None
    if not isinstance(paid, list):
        return set()
    return {str(p) for p in paid if p}


def save_paid_profiles(paid):
    """保存付费 profile 名集合（0600 权限）。"""
    os.makedirs(os.path.dirname(PAID_PROFILES_FILE) or ".", exist_ok=True)
    tmp = PAID_PROFILES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"paid": sorted(paid)}, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PAID_PROFILES_FILE)


def is_paid_profile(profile):
    """该 profile 是否标记为付费账号（默认免费）。"""
    return profile in load_paid_profiles()


def set_paid_profile(profile, paid):
    """设置/取消某 profile 的付费标记，返回设置后的付费状态。"""
    current = load_paid_profiles()
    if paid:
        current.add(profile)
    else:
        current.discard(profile)
    save_paid_profiles(current)
    return paid
