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
from oci.client import (
    AuthError,
    NotFoundError,
    OCIError,
    OutOfCapacityError,
    QuotaExceededError,
    RateLimitError,
)

# ================= 配置区（改这里） =================
# 实例详情里 OCID 只显示后 4 位（防聊天记录泄露完整 OCID）。
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
            text = "⛔ 未授权：你的 Telegram 账号不在白名单里，无法使用本机器人。"
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
    bot_config.get_profile_config(profile)  # 未知 profile 抛 ValueError
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


# 实例列表分组：组顺序与组头（fmt_instance_list 与
# instance_list_keyboard 共用，保证文字与按钮顺序一致）。
# 已终止（TERMINATED）不在组里：sort_instances_by_group 直接
# 把它过滤掉，不显示（用户要求：列表里一堆已终止的看着乱）。
INSTANCE_GROUP_ORDER = ("RUNNING", "STOPPED")
INSTANCE_GROUP_TITLES = {
    "RUNNING": "🟢 运行中",
    "STOPPED": "🟡 已停止",
    "TERMINATED": "⚫ 已终止",
}
INSTANCE_GROUP_OTHER_TITLE = "❓ 其他"


def instance_group_title(state):
    """状态对应的分组标题（不含数量）；未知状态归入“其他”。"""
    state = (state or "").upper()
    return INSTANCE_GROUP_TITLES.get(state, INSTANCE_GROUP_OTHER_TITLE)


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


# 启动盘列表分组：组顺序与组头（fmt_volume_list 与
# volume_list_keyboard 共用，保证文字与按钮顺序一致）。
# TERMINATED 的盘是已删除的墓碑记录（不占额度、不能操作），
# 必须和可用盘分开，否则 11 块旧测试盘会把 3 块在用的盘淹没
# （2026-11-24 用户截图反馈）。
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


def fmt_instance_list(instances, profile):
    """实例列表的文字部分：只剩标题 + 各组数量 + 操作提示。

    每台实例的信息（名称 / A1·E2 / 公网 IP）都在按钮上
    （见 keyboards.instance_list_keyboard），文字不再逐台
    重复，否则文字列表和按钮列表会同时出现两份。
    空组不显示。已终止的实例已过滤，不计入总数。
    """
    if not instances:
        return f"[{profile}] 该账号下没有实例。"
    ordered = sort_instances_by_group(instances)
    if not ordered:
        return (f"[{profile}] 没有可显示的实例"
                "（已终止的实例已隐藏）。")
    counts = {}
    for _idx, inst in ordered:
        title = instance_group_title(inst.get("lifecycle-state"))
        counts[title] = counts.get(title, 0) + 1
    # 按组顺序排数量，如“🟢 运行中 3 台 · 🟡 已停止 1 台”。
    parts = []
    for state in INSTANCE_GROUP_ORDER:
        title = INSTANCE_GROUP_TITLES[state]
        if title in counts:
            parts.append(f"{title} {counts[title]} 台")
    other = counts.get(INSTANCE_GROUP_OTHER_TITLE)
    if other:
        parts.append(f"{INSTANCE_GROUP_OTHER_TITLE} {other} 台")
    lines = [
        f"[{profile}] 实例列表（{len(ordered)} 台）：",
        " · ".join(parts),
        "",
        "点下面按钮查看详情或操作。",
    ]
    return "\n".join(lines)


def fmt_instance_detail(inst, public_ip=None, cpu_model=None):
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
        f"状态：{state}",
        f"规格：{shape}（{ocpus} 核 / {memory}GB）",
        f"CPU 型号：{cpu_model or '未知'}",
        f"可用域：{ad}",
        f"创建时间：{created}",
    ]
    if public_ip:
        lines.append(f"公网 IP：{public_ip}")
    return "\n".join(lines)


def fmt_quota_summary(summary, profile):
    """配额汇总全文（quota.get_free_tier_summary 的返回值）。"""
    a1 = summary["a1"]
    e2 = summary["e2"]
    boot = summary["boot_volume"]
    ok = "✅ 未超限" if summary["overall_ok"] else "⚠️ 已超限"
    lines = [
        f"[{profile}] 免费额度（{ok}）：",
        "",
        f"A1.Flex：{a1['ocpu_used']}/{a1['ocpu_limit']} 核，"
        f"{a1['memory_used_gb']}/{a1['memory_limit_gb']}GB",
        f"E2.1.Micro：{e2['count']}/{e2['limit']} 台",
        f"启动盘：{boot['used_gb']}/{boot['limit_gb']}GB",
    ]
    return "\n".join(lines)


def fmt_error(exc):
    """把 OCI 异常转成用户能看懂的中文提示。"""
    if isinstance(exc, OutOfCapacityError):
        return "❌ 无容量（Out of host capacity）：机房暂时没货，请稍后再试。"
    if isinstance(exc, RateLimitError):
        return "❌ API 限流（429）：请求太频繁，机器人已自动退避，请稍后再试。"
    if isinstance(exc, QuotaExceededError):
        return "❌ 配额不足：免费额度已用完，继续重试也没用，请先释放资源或换账号。"
    if isinstance(exc, AuthError):
        return "❌ 认证失败：请检查 ~/.oci/config 与 API 密钥是否正确。"
    if isinstance(exc, NotFoundError):
        return "❌ 资源不存在：可能已被删除，请刷新列表后再试。"
    if isinstance(exc, OCIError):
        return f"❌ OCI 调用失败：{exc}"
    return f"❌ 出错了：{exc}"


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
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}
        self._counter = 0

    def register(self, kind, detail="", label=None):
        """注册一个任务，返回短 ID（如 "g1"）。

        label 是显示用的任务说明（如 "E2.1.Micro（新加坡）·
        AMD_2026-11-24"），菜单 21 与取消按钮用它，不再直接
        暴露 g1 / grab 这类内部编号与类型。
        """
        with self._lock:
            self._counter += 1
            task_id = f"{kind[0]}{self._counter}"
            self._tasks[task_id] = {
                "id": task_id,
                "kind": kind,
                "status": "running",
                "detail": detail,
                "label": label,
                "started_at": time.time(),
                "stop_event": threading.Event(),
            }
            return task_id

    def update(self, task_id, status=None, detail=None):
        """更新任务状态与详情。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if status is not None:
                task["status"] = status
            if detail is not None:
                task["detail"] = detail
            return True

    def get(self, task_id):
        """取任务字典的浅拷贝；不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_all(self):
        """全部任务列表（按注册顺序）。"""
        with self._lock:
            return [dict(t) for t in self._tasks.values()]

    def running(self):
        """运行中的任务列表。"""
        return [t for t in self.list_all() if t["status"] == "running"]

    def cancel(self, task_id):
        """取消任务：置 stop_event 并把状态标为 cancelled。

        返回 True 表示任务存在且在运行中；False 表示任务不存在或已结束。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] != "running":
                return False
            task["stop_event"].set()
            task["status"] = "cancelled"
            return True

    def stop_event(self, task_id):
        """取任务的 stop_event（任务循环里检查它）；任务不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task["stop_event"] if task else None


# 全局任务注册表（单机器人进程内共享）。
TASKS = TaskRegistry()


def fmt_task_list(tasks):
    """任务列表全文：每条显示在抢什么机器 + 最新进度。

    不再显示 g1（grab, running, …）这种内部编号与英文类型。
    label 由注册方传入；没有 label 的旧任务退回“任务 <id>”。
    """
    if not tasks:
        return "当前没有运行中的任务。"
    lines = ["📋 运行中的任务：", ""]
    for task in tasks:
        label = task.get("label") or f"任务 {task['id']}"
        lines.append(f"• {label}")
        if task.get("detail"):
            lines.append(f"  {task['detail']}")
    return "\n".join(lines)
