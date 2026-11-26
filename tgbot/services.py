#!/usr/bin/env python3
"""Telegram 处理函数的共享工具。

  - 白名单检查装饰器（require_allowed）
  - 从 CallbackContext 取当前 profile / OCIClient / compartment
  - 实例列表存取（callback_data 只有 64 字节，放不下 OCID，
    所以把实例列表存进 context.user_data，回调里用下标取回）
  - 消息格式化（实例列表/详情、配额汇总、错误文本）
  - 后台任务注册表（TaskRegistry：抢机等长任务的状态跟踪）

本模块是纯同步逻辑（除 TaskRegistry 的线程安全锁外），不依赖
Telegram 网络，单元测试直接用 mock 的 update/context 就能跑。
"""

import asyncio
import functools
import threading
import time
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from tgbot import config as bot_config
from tgbot import i18n as bot_i18n
from tgbot import task_store
from oci.client import (
    AuthError,
    NotFoundError,
    OCIError,
    OutOfCapacityError,
    QuotaExceededError,
    RateLimitError,
)

# ================= 配置区（改这里） =================
# In instance details, only the last 4 chars of the OCID are shown (so a full OCID never leaks into chat history). 实例详情里 OCID 只显示后 4 位（防聊天记录泄露完整 OCID）。
OCID_TAIL_LEN = 4
# =====================================================


# ---------------- 白名单 ----------------

def require_allowed(handler):
    """装饰器：只允许白名单用户调用；其余用户直接拒绝并返回。"""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else None
        if not bot_config.is_allowed(user_id):
            text = bot_i18n.t(bot_i18n.get_user_lang(user_id),
                              "common.denied")
            if update.effective_message:
                await update.effective_message.reply_text(text)
            elif update.callback_query:
                await update.callback_query.answer(text, show_alert=True)
            return None
        return await handler(update, context, *args, **kwargs)
    return wrapper


# ---------------- 当前账号（profile） ----------------

def get_current_profile(context):
    """当前用户选中的 profile（存在 user_data 里，默认 DEFAULT）。"""
    return context.user_data.get("profile", bot_config.DEFAULT_PROFILE)


def set_current_profile(context, profile):
    """切换当前 profile（切换前先校验 profile 存在）。

    切换账号时工作区域重置为新账号的主区（旧区域不带过去，避免
    新加坡的首尔区域泄漏到芝加哥账号上）。
    """
    bot_config.get_profile_config(profile)
    context.user_data["profile"] = profile
    context.user_data["region"] = bot_config.get_region(profile)


def get_current_region(context):
    """当前工作区域（存在 user_data 里，默认当前账号的主区）。"""
    region = context.user_data.get("region")
    if region:
        return region
    return bot_config.get_region(get_current_profile(context))


def set_current_region(context, region):
    """切换当前工作区域（不校验订阅关系；回调层在切换前校验）。"""
    context.user_data["region"] = region


def get_cli(context):
    """当前 profile + 当前工作区域的 OCIClient。"""
    profile = get_current_profile(context)
    return bot_config.get_oci_client(profile=profile,
                                     region=get_current_region(context))


def get_compartment(context):
    """当前 profile 的 compartment OCID。"""
    return bot_config.get_compartment_id(get_current_profile(context))


# ---------------- 实例列表存取（下标 <-> OCID） ----------------

def store_instances(context, instances):
    """把实例精简列表存进 user_data，供回调按下标取回。"""
    context.user_data["instances"] = list(instances)


def resolve_instance(context, idx):
    """按下标取实例精简字典；下标无效返回 None。idx 可能是字符串。"""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    instances = context.user_data.get("instances") or []
    if 0 <= idx < len(instances):
        return instances[idx]
    return None


def store_volumes(context, volumes):
    """把启动盘列表存进 user_data，供磁盘回调按下标取回。"""
    context.user_data["volumes"] = list(volumes)


def resolve_volume(context, idx):
    """按下标取启动盘字典；下标无效返回 None。"""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    volumes = context.user_data.get("volumes") or []
    if 0 <= idx < len(volumes):
        return volumes[idx]
    return None


def store_boot_backups(context, backups):
    """把启动卷备份列表存进 user_data，供备份回调按下标取回。"""
    context.user_data["boot_backups"] = list(backups)


def resolve_boot_backup(context, idx):
    """按下标取启动卷备份字典；下标无效返回 None。"""
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    backups = context.user_data.get("boot_backups") or []
    if 0 <= idx < len(backups):
        return backups[idx]
    return None


# ---------------- 同步调用包装 ----------------

async def run_sync(func, *args, **kwargs):
    """在线程里跑同步函数（OCI CLI 调用是阻塞的，不能卡事件循环）。"""
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------------- 格式化 ----------------

def _tail(ocid):
    """OCID 只留后 4 位。"""
    if not ocid:
        return "?"
    return f"…{ocid[-OCID_TAIL_LEN:]}"


def short_shape(shape):
    """规格简称，放按钮上：A1.Flex → A1，E2.1.Micro → E2，E5.Flex → E5。

    只留第一段（按钮放不下全名）；空规格返回 "?"。
    """
    shape = (shape or "").replace("VM.Standard.", "")
    if not shape:
        return "?"
    return shape.split(".")[0]


def fmt_shape_spec(shape, ocpus, memory, lang="zh"):
    """规格行：E2.1.Micro 后面加“永久免费”/ "Free forever"。

    如 E2.1.Micro（1 核 / 1GB）永久免费；其他规格不加。
    shape 可带 VM.Standard. 前缀，也可已是简称。
    """
    short = (shape or "?").replace("VM.Standard.", "")
    unit = bot_i18n.t(lang, "boot.spec_a1", ocpus=ocpus, memory=memory)
    text = f"{short}（{unit}）"
    if short == "E2.1.Micro":
        text += bot_i18n.t(lang, "instances.detail_free_forever")
    return text


# Instance list grouping: group order and headers are shared by fmt_instance_list and instance_list_keyboard, so the text and button order always agree. 实例列表分组：组顺序与组头（fmt_instance_list 与 instance_list_keyboard 共用，保证文字与按钮顺序一致）。
# TERMINATED instances are not in any group: sort_instances_by_group filters them out and does not show them (a list full of terminated instances is unreadable). 已终止（TERMINATED）不在组里：sort_instances_by_group 直接把它过滤掉，不显示（列表里一堆已终止的看着乱）。
INSTANCE_GROUP_ORDER = ("RUNNING", "STOPPED")
INSTANCE_GROUP_TITLES = {
    "RUNNING": "instances.group_running",
    "STOPPED": "instances.group_stopped",
    "TERMINATED": "instances.group_terminated",
}
INSTANCE_GROUP_OTHER_TITLE_KEY = "instances.group_other"


def instance_group_title(state, lang="zh"):
    """状态对应的分组标题（不含数量）；未知状态归入“其他”。"""
    state = (state or "").upper()
    key = INSTANCE_GROUP_TITLES.get(state, INSTANCE_GROUP_OTHER_TITLE_KEY)
    return bot_i18n.t(lang, key)


def sort_instances_by_group(instances):
    """按状态分组排序：返回 [(原下标, 实例)]。

    已终止（TERMINATED）的实例直接过滤掉，不显示。组顺序：
    RUNNING → STOPPED → 其他状态；组内保持原列表顺序。下标是
    实例在原列表里的下标（回调按它取回，见 store_instances，
    绝对不能用分组后的下标）。
    """
    order = {state: i for i, state in enumerate(INSTANCE_GROUP_ORDER)}
    fallback = len(INSTANCE_GROUP_ORDER)
    indexed = [(i, inst) for i, inst in enumerate(instances)
               if (inst.get("lifecycle-state") or "").upper()
               != "TERMINATED"]
    indexed.sort(key=lambda pair: (
        order.get((pair[1].get("lifecycle-state") or "").upper(),
                  fallback),
        pair[0]))
    return indexed


# Boot volume list grouping: group order and headers are shared by fmt_volume_list and volume_list_keyboard, so the text and button order always agree. 启动盘列表分组：组顺序与组头（fmt_volume_list 与 volume_list_keyboard 共用，保证文字与按钮顺序一致）。
# TERMINATED volumes are deleted tombstone records (no quota, no operations) and must be kept apart from usable volumes, or the old test volumes would drown out the ones in use. TERMINATED 的盘是已删除的墓碑记录（不占额度、不能操作），必须和可用盘分开，否则旧测试盘会把在用的盘淹没。
VOLUME_GROUP_ORDER = ("AVAILABLE", "TERMINATING", "TERMINATED")
VOLUME_GROUP_TITLES = {
    "AVAILABLE": "🟢 可用",
    "TERMINATING": "🟡 删除中",
    "TERMINATED": "⚫ 已删除",
}
VOLUME_GROUP_OTHER_TITLE = "❓ 其他"


def volume_group_title(state):
    """状态对应的分组标题（不含数量）；未知状态归入“其他”。"""
    state = (state or "").upper()
    return VOLUME_GROUP_TITLES.get(state, VOLUME_GROUP_OTHER_TITLE)


def sort_volumes_by_group(volumes):
    """按状态分组排序：返回 [(原下标, 启动盘)]。

    组顺序：AVAILABLE → TERMINATING → TERMINATED → 其他状态；
    组内保持原列表顺序。下标是启动盘在原列表里的下标（回调按
    它取回，见 store_volumes，绝对不能用分组后的下标）。
    """
    order = {state: i for i, state in enumerate(VOLUME_GROUP_ORDER)}
    fallback = len(VOLUME_GROUP_ORDER)
    indexed = list(enumerate(volumes))
    indexed.sort(key=lambda pair: (
        order.get((pair[1].get("lifecycle-state") or "").upper(),
                  fallback),
        pair[0]))
    return indexed


def fmt_instance_list(instances, profile, lang="zh"):
    """实例列表的文字部分：只剩标题 + 各组数量 + 操作提示。

    每台实例的信息（名称 / A1·E2 / 公网 IP）都在按钮上
    （见 keyboards.instance_list_keyboard），文字不再逐台
    重复，否则文字列表和按钮列表会同时出现两份。
    空组不显示。已终止的实例已过滤，不计入总数。
    """
    if not instances:
        return bot_i18n.t(lang, "instances.list_empty", profile=profile)
    ordered = sort_instances_by_group(instances)
    if not ordered:
        return bot_i18n.t(lang, "instances.list_none_visible",
                          profile=profile)
    counts = {}
    for _idx, inst in ordered:
        title = instance_group_title(inst.get("lifecycle-state"), lang=lang)
        counts[title] = counts.get(title, 0) + 1
    parts = []
    for state in INSTANCE_GROUP_ORDER:
        title = instance_group_title(state, lang=lang)
        if title in counts:
            if lang == "zh":
                parts.append(f"{title} {counts[title]} 台")
            else:
                parts.append(f"{title} {counts[title]}")
    other_title = instance_group_title("OTHER", lang=lang)
    other = counts.get(other_title)
    if other:
        if lang == "zh":
            parts.append(f"{other_title} {other} 台")
        else:
            parts.append(f"{other_title} {other}")
    lines = [
        bot_i18n.t(lang, "instances.list_title", profile=profile,
                   count=len(ordered)),
        " · ".join(parts),
        "",
        bot_i18n.t(lang, "instances.list_hint"),
    ]
    return "\n".join(lines)


def fmt_instance_detail(inst, public_ip=None, cpu_model=None, lang="zh"):
    """实例详情全文。inst 是 get_instance 返回的完整 JSON（已解包 data）。

    cpu_model 是缓存/检测到的 CPU 型号（如 "AMD EPYC 7J13"），
    未检测到时该行显示“未知”。
    """
    data = inst.get("data") if isinstance(inst, dict) else None
    if data is None:
        data = inst or {}
    name = data.get("display-name", "?")
    shape = (data.get("shape") or "?").replace("VM.Standard.", "")
    state = data.get("lifecycle-state", "?")
    ad = data.get("availability-domain", "?")
    created = (data.get("time-created") or "?")[:19].replace("T", " ")
    cfg = data.get("shape-config") or {}
    ocpus = cfg.get("ocpus", "?")
    memory = cfg.get("memory-in-gbs", "?")
    lines = [
        f"🖥️ {name}",
        f"ID：{_tail(data.get('id'))}",
        f"{bot_i18n.t(lang, 'instances.detail_state')}：{state}",
        f"{bot_i18n.t(lang, 'instances.detail_shape')}："
        f"{fmt_shape_spec(shape, ocpus, memory, lang=lang)}",
        f"{bot_i18n.t(lang, 'instances.detail_cpu')}："
        f"{cpu_model or bot_i18n.t(lang, 'common.unknown')}",
        f"{bot_i18n.t(lang, 'instances.detail_ad')}：{ad}",
        f"{bot_i18n.t(lang, 'instances.detail_created')}：{created}",
    ]
    if public_ip:
        lines.append(f"{bot_i18n.t(lang, 'instances.detail_public_ip')}："
                     f"{public_ip}")
    return "\n".join(lines)


def fmt_quota_summary(summary, profile, lang="zh"):
    """配额汇总全文（quota.get_free_tier_summary 的返回值）。"""
    a1 = summary["a1"]
    e2 = summary["e2"]
    boot = summary["boot_volume"]
    status = bot_i18n.t(lang, "quota.ok" if summary["overall_ok"]
                        else "quota.over")
    lines = [
        bot_i18n.t(lang, "quota.title", profile=profile, status=status),
        "",
        bot_i18n.t(lang, "quota.a1", used=a1['ocpu_used'],
                   limit=a1['ocpu_limit'],
                   mem_used=a1['memory_used_gb'],
                   mem_limit=a1['memory_limit_gb']),
        bot_i18n.t(lang, "quota.e2", count=e2['count'], limit=e2['limit']),
        bot_i18n.t(lang, "quota.boot_volume", used=boot['used_gb'],
                   limit=boot['limit_gb']),
    ]
    return "\n".join(lines)


def fmt_error(exc, lang="zh"):
    """把 OCI 异常转成用户能看懂的提示（按语言）。"""
    if isinstance(exc, OutOfCapacityError):
        return bot_i18n.t(lang, "error.capacity")
    if isinstance(exc, RateLimitError):
        return bot_i18n.t(lang, "error.rate_limit")
    if isinstance(exc, QuotaExceededError):
        return bot_i18n.t(lang, "error.quota")
    if isinstance(exc, AuthError):
        return bot_i18n.t(lang, "error.auth")
    if isinstance(exc, NotFoundError):
        return bot_i18n.t(lang, "error.not_found")
    if isinstance(exc, OCIError):
        return bot_i18n.t(lang, "error.oci_failed", detail=exc)
    return bot_i18n.t(lang, "error.generic", detail=exc)


def fmt_duration(seconds):
    """秒数转中文时长（如 3661 -> "1 小时 1 分钟"）。"""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟 {secs} 秒"
    return f"{secs} 秒"


# ---------------- 后台任务注册表 ----------------

class TaskRegistry:
    """后台长任务（抢机等）的注册表：线程安全，支持注册/更新/查询/取消。

    任务字典字段：id / kind / status / detail / label / started_at /
    stop_event。label 是给人看的任务说明（如抢什么机器），
    注册时传入；没有 label 的旧任务在显示时退回“任务 <id>”。
    status：running / done / failed / cancelled。
    取消是协作式的：任务循环里要定期检查 stop_event（grabber.run 已支持）。

    tasks_file 非空时任务记录同步写进共享文件（tgbot/task_store.py，
    与网页版同一份 tasks.json），查询时合并对方进程的任务；对方
    进程的任务没有本地 stop_event（取到 None），取消时走跨进程
    请求（对方的监听线程收到后置 stop_event）。tasks_file 为空时
    纯内存（单元测试用，不碰文件）。
    """

    def __init__(self, tasks_file=None, origin="bot"):
        self._lock = threading.Lock()
        self._tasks = {}
        self._counter = 0
        self.tasks_file = tasks_file
        self.origin = origin

    def recover(self):
        """启动时调用：把本进程上次未结束的任务标为已中断。"""
        if self.tasks_file:
            task_store.recover_interrupted(self.tasks_file, self.origin)
            # Sync the in-memory copy too: every task in memory belongs to this process, so mark them interrupted as well. 同步内存副本：内存里的任务全是本进程的，同样标为已中断。
            with self._lock:
                for task in self._tasks.values():
                    if task.get("status") in ("pending", "running"):
                        task["status"] = "cancelled"
                        task["detail"] = "服务重启，任务已中断。"

    def _sync_cancel_flag(self, task):
        """共享模式下，把文件里对方写来的取消请求同步进内存副本。

        对方进程只会改本任务的 cancel_requested（状态只由任务
        所在进程改），所以只同步这一个字段。调用时已持有 _lock。
        """
        if not self.tasks_file:
            return
        record = task_store.get_record(self.tasks_file, task["id"])
        if record is not None:
            task["cancel_requested"] = record.get("cancel_requested",
                                                  False)

    @staticmethod
    def _record_to_task(record):
        """共享文件记录转机器人任务字典（补 started_at，stop_event 为 None）。"""
        task = dict(record)
        task["started_at"] = record.get("created_at", time.time())
        task["stop_event"] = None
        return task

    def register(self, kind, detail="", label=None):
        """注册一个任务，返回短 ID（如共享模式 "bot-g1"，内存模式 "g1"）。

        label 是显示用的任务说明（如 "E2.1.Micro（新加坡）·
        AMD_2026-01-01"），菜单 21 与取消按钮用它，不再直接
        暴露 g1 / grab 这类内部编号与类型。
        """
        with self._lock:
            if self.tasks_file:
                record = task_store.create_record(
                    self.tasks_file, self.origin, kind, detail, label,
                    status="running")
                task_id = record["id"]
                task = dict(record)
                task["started_at"] = record["created_at"]
            else:
                self._counter += 1
                task_id = f"{kind[0]}{self._counter}"
                task = {
                    "id": task_id,
                    "kind": kind,
                    "status": "running",
                    "detail": detail,
                    "label": label,
                    "started_at": time.time(),
                }
            task["stop_event"] = threading.Event()
            self._tasks[task_id] = task
            return task_id

    def update(self, task_id, status=None, detail=None):
        """更新任务状态与详情（共享模式下详情同时追加进任务日志）。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if status is not None:
                task["status"] = status
            if detail is not None:
                task["detail"] = detail
            if self.tasks_file:
                task_store.update_record(
                    self.tasks_file, task_id, status=status,
                    detail=detail, log=detail)
            return True

    def append_log(self, task_id, message):
        """往任务日志追加一条消息（不改详情与状态）。

        共享模式下写进共享文件；纯内存模式下记进内存任务的
        logs 列表。任务不存在返回 False。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if self.tasks_file:
                task_store.update_record(
                    self.tasks_file, task_id, log=message)
            else:
                task.setdefault("logs", []).append(message)
            return True

    def attach(self, task_id):
        """把共享文件里已有的任务接进内存（启动恢复抢机用）。

        不新建记录，只给本进程补一个带 stop_event 的内存副本；
        记录不存在或纯内存模式时返回 False。
        """
        with self._lock:
            if task_id in self._tasks:
                return True
            if not self.tasks_file:
                return False
            record = task_store.get_record(self.tasks_file, task_id)
            if record is None:
                return False
            task = self._record_to_task(record)
            task["stop_event"] = threading.Event()
            self._tasks[task_id] = task
            return True

    def get(self, task_id):
        """取任务字典的浅拷贝；不存在返回 None。

        共享模式下本进程没有的任务去共享文件里找（对方进程的任务）。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                self._sync_cancel_flag(task)
                return dict(task)
            if not self.tasks_file:
                return None
            record = task_store.get_record(self.tasks_file, task_id)
            if record is None:
                return None
            return self._record_to_task(record)

    def list_all(self):
        """全部任务列表（按注册顺序；共享模式下含对方进程的任务）。"""
        with self._lock:
            tasks = []
            for task in self._tasks.values():
                self._sync_cancel_flag(task)
                tasks.append(dict(task))
            if not self.tasks_file:
                return tasks
            seen = {t["id"] for t in tasks}
            for record in task_store.load_records(self.tasks_file):
                if record["id"] not in seen:
                    tasks.append(self._record_to_task(record))
            return tasks

    def running(self):
        """运行中的任务列表。"""
        return [t for t in self.list_all() if t["status"] == "running"]

    def cancel(self, task_id):
        """取消任务：本进程的任务置 stop_event 并把状态标为 cancelled；
        对方进程的任务发跨进程取消请求（对方的监听线程执行）。

        返回 True 表示任务存在且在运行中；False 表示任务不存在或已结束。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                if task["status"] != "running":
                    return False
                task["stop_event"].set()
                task["status"] = "cancelled"
                if self.tasks_file:
                    task_store.update_record(
                        self.tasks_file, task_id, status="cancelled",
                        log="任务已取消。")
                return True
            if not self.tasks_file:
                return False
            return task_store.request_cancel(self.tasks_file, task_id)

    def stop_event(self, task_id):
        """取任务的 stop_event（任务循环里检查它）；任务不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task["stop_event"] if task else None


# Global task registry (shared inside the bot process; task records are shared with the web panel via tasks.json). 全局任务注册表（机器人进程内共享；任务记录与网页版共用 tasks.json）。
TASKS = TaskRegistry(tasks_file=task_store.tasks_path())


def fmt_task_list(tasks, lang="zh"):
    """任务列表全文：每条显示在抢什么机器 + 最新进度。

    不再显示 g1（grab, running, …）这种内部编号与英文类型。
    label 由注册方传入；没有 label 的旧任务退回“任务 <id>”。
    """
    if not tasks:
        return bot_i18n.t(lang, "tasks.none")
    lines = [bot_i18n.t(lang, "tasks.title") + "：", ""]
    for task in tasks:
        label = task.get("label") or bot_i18n.t(
            lang, "tasks.old_label", task_id=task['id'])
        lines.append(f"• {label}")
        if task.get("detail"):
            lines.append(f"  {task['detail']}")
    return "\n".join(lines)
