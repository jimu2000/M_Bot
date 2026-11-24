"""备份创建完成通知：后台轮询备份状态，到 AVAILABLE 后双通道通知。

备份创建是异步操作（CREATING 几分钟才到 AVAILABLE），创建点只回
“创建中”是不够的——用户离开 Telegram 后不知道备份好了没
（2026-11-24 用户要求：创建和恢复都要 Bark 通知）。
本模块起一个 daemon 线程轮询，等备份可用（或失败/超时）后用
notify_dual 发 Telegram + Bark 双通道通知（与引导卷恢复同款）。

机器人端三处创建点（菜单 7、实例详情、满额删最旧再建）与网页端
创建接口共用本模块。
"""

import logging
import threading

from oci.boot_backup import (
    BACKUP_WATCH_INTERVAL_SECONDS,
    BACKUP_WATCH_MAX_SECONDS,
    wait_for_backup_available,
)
from tgbot.notify import notify_dual

logger = logging.getLogger(__name__)

BACKUP_WATCH_GROUP = "bootbackup"


def _backup_watch_thread(cli, backup_id, instance_name, backup_name,
                         token, chat_id):
    """轮询等备份可用，结果双通道通知；绝不抛异常。"""
    try:
        state = wait_for_backup_available(cli, backup_id)
    except TimeoutError:
        notify_dual(token, chat_id, "💾 备份超时",
                    f"实例「{instance_name}」的备份「{backup_name}」"
                    f"创建超过 {BACKUP_WATCH_MAX_SECONDS // 60} 分钟仍未完成，"
                    "请在备份列表查看状态。",
                    group=BACKUP_WATCH_GROUP)
        return
    except Exception as exc:  # noqa: BLE001 - 轮询异常也通知用户
        logger.warning("备份完成轮询异常：%s", exc)
        notify_dual(token, chat_id, "💾 备份状态未知",
                    f"实例「{instance_name}」的备份「{backup_name}」"
                    "创建后查不到状态，请在备份列表查看。",
                    group=BACKUP_WATCH_GROUP)
        return
    if state == "AVAILABLE":
        notify_dual(token, chat_id, "💾 备份已完成",
                    f"实例「{instance_name}」的备份「{backup_name}」已可用。",
                    group=BACKUP_WATCH_GROUP)
    else:
        notify_dual(token, chat_id, "💾 备份失败",
                    f"实例「{instance_name}」的备份「{backup_name}」"
                    f"创建失败（状态：{state}）。",
                    group=BACKUP_WATCH_GROUP)


def start_backup_watch(cli, backup_id, instance_name, backup_name,
                       token, chat_id):
    """起 daemon 线程等备份可用后通知；token/chat_id/备份 id 缺失时不启动。"""
    if not token or chat_id is None or not backup_id:
        return None
    thread = threading.Thread(
        target=_backup_watch_thread,
        args=(cli, backup_id, instance_name, backup_name, token, chat_id),
        daemon=True)
    thread.start()
    return thread
