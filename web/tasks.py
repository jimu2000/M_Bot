#!/usr/bin/env python3
"""后台任务注册表（Web 面板用，线程安全）。

思路与 tgbot/services.py 的 TaskRegistry 一致：抢机等长任务在 daemon
线程里跑，进度通过日志列表跟踪，取消是协作式的（任务循环定期检查
stop_event，Grabber 已支持）。

任务字典字段：id / kind / status / detail / logs / result /
created_at / stop_event。
status：pending / running / done / failed / cancelled。
对外返回的任务视图不含 stop_event（不可 JSON 序列化）。

持久化：任务记录（不含 stop_event）保存到 DATA_DIR/tasks.json；
服务重启时加载，之前还在跑的任务标记为 cancelled（进程已死，
线程不可能还在跑）。
"""

import json
import os
import threading
import time
from datetime import datetime

from oci.boot_swap import BootSwapError, swap_boot_volume
from oci.client import OCIClient, QuotaExceededError
from oci.grabber import GrabCancelledError, Grabber
from oci.network import get_public_ip
from tgbot.billing import FREE_SHAPES
from tgbot.notify import (
    fmt_paid_instance_alert,
    notify_boot_success,
    notify_dual,
)
from web import config

try:
    # APScheduler：抢机计划的 cron 调度（requirements.txt 里有显式依赖；
    # 未安装时计划仍可增删改，但调度器无法启动，validate_cron 会明确报错）。
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    BackgroundScheduler = None
    CronTrigger = None

# ================= 抢机计划配置区（改这里） =================
# 抢机计划触发时 Grabber 需要的完整参数；计划本身只存 cron / shape /
# ocpus / memory_gb / enabled（见 GrabberPlanRegistry），其余字段
# （可用域 / 镜像 / 子网 / 实例名 / SSH 公钥文件）优先取计划里的
# 可选字段，缺了就用下面这些默认值。
# 部署时用环境变量填真实值（MBOT_PLAN_AD / MBOT_PLAN_IMAGE_ID /
# MBOT_PLAN_SUBNET_ID / MBOT_PLAN_SSH_KEYS_FILE），不改代码。
PLAN_DEFAULTS = {
    # 可用域（如 "xxxx:AP-SINGAPORE-1-AD-1"，OCI 控制台实例详情里看）。
    "availability_domain": os.environ.get(
        "MBOT_PLAN_AD", "xxxx:AP-SINGAPORE-1-AD-1"),
    # 系统镜像 OCID（查法见 web/config.py 的 IMAGE_IDS 注释）。
    "image_id": os.environ.get(
        "MBOT_PLAN_IMAGE_ID", "ocid1.image.oc1..xxxxxxxx"),
    # 子网 OCID（OCI 控制台 VCN 里看）。
    "subnet_id": os.environ.get(
        "MBOT_PLAN_SUBNET_ID", "ocid1.subnet.oc1..xxxxxxxx"),
    # 实例显示名（计划里没填 display_name 时用它 + 计划 id 后缀）。
    "display_name": "plan-grab",
    # SSH 公钥文件路径（实例的 authorized_keys 内容来源）。
    "ssh_keys_file": os.environ.get(
        "MBOT_PLAN_SSH_KEYS_FILE", "/path/to/keys.pub"),
    # 目标 CPU（E2 抢机筛 7J13 用；A1 填 None 表示抢到就留）。
    "target_cpu": None,
}
# 允许的抢机形状（计划校验用）。
PLAN_SHAPES = ("VM.Standard.E2.1.Micro", "VM.Standard.A1.Flex")
# A1.Flex 免费额度上限（计划校验用：ocpus 1-4，内存 1-24GB）。
PLAN_MAX_OCPUS = 4
PLAN_MAX_MEMORY_GB = 24
# 调度器每隔这么多秒重新从文件同步一次计划（Telegram 机器人与 Web
# 面板共用 grabber_plans.json，机器人侧改了计划后，面板调度器靠
# 这次同步在 1 分钟内跟上，不用重启面板）。
PLAN_SYNC_INTERVAL_SECONDS = 60
# =====================================================


class TaskRegistry:
    """后台长任务的注册表：线程安全，支持创建/查询/日志/取消。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}
        self._counter = 0
        self._load()

    # ---------------- 持久化 ----------------

    def _persist_path(self):
        """任务记录文件路径（每次都从 config 读 DATA_DIR，方便测试替换）。"""
        return os.path.join(config.DATA_DIR, "tasks.json")

    def _save(self):
        """把任务公开视图写盘（失败时静默跳过，不影响任务本身）。"""
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            with open(self._persist_path(), "w", encoding="utf-8") as f:
                json.dump(self._public_list_locked(), f, ensure_ascii=False)
        except OSError:
            pass

    def _load(self):
        """启动时从 tasks.json 恢复任务记录。

        之前还在跑的任务（pending/running）标记为 cancelled 并追加一条
        日志——进程重启后原线程已不存在，不可能还在跑。
        """
        try:
            with open(self._persist_path(), encoding="utf-8") as f:
                records = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(records, list):
            return
        with self._lock:
            for record in records:
                task_id = record.get("id")
                if not task_id:
                    continue
                task = {
                    "id": task_id,
                    "kind": record.get("kind", "?"),
                    "status": record.get("status", "cancelled"),
                    "detail": record.get("detail", ""),
                    "logs": list(record.get("logs") or []),
                    "result": record.get("result"),
                    "created_at": record.get("created_at", time.time()),
                    "stop_event": threading.Event(),
                }
                if task["status"] in ("pending", "running"):
                    task["status"] = "cancelled"
                    task["logs"].append(self._stamp("服务重启，任务已中断。"))
                self._tasks[task_id] = task
                # 恢复计数器，避免新任务 ID 与旧任务冲突。
                digits = "".join(ch for ch in task_id if ch.isdigit())
                if digits:
                    self._counter = max(self._counter, int(digits))

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _stamp(message):
        """给日志消息加时间前缀。"""
        return f"[{datetime.now().strftime('%H:%M:%S')}] {message}"

    def _public(self, task):
        """任务的公开视图（去掉 stop_event，logs 复制一份）。"""
        return {
            "id": task["id"],
            "kind": task["kind"],
            "status": task["status"],
            "detail": task["detail"],
            "logs": list(task["logs"]),
            "result": task["result"],
            "created_at": task["created_at"],
        }

    def _public_list_locked(self):
        """全部任务的公开视图列表（调用方需已持有锁）。"""
        return [self._public(t) for t in self._tasks.values()]

    # ---------------- 注册 / 查询 ----------------

    def create(self, kind, detail=""):
        """注册一个任务，返回短 ID（如抢机任务 "g1"）。初始状态 pending。"""
        with self._lock:
            self._counter += 1
            task_id = f"{kind[0]}{self._counter}"
            self._tasks[task_id] = {
                "id": task_id,
                "kind": kind,
                "status": "pending",
                "detail": detail,
                "logs": [],
                "result": None,
                "created_at": time.time(),
                "stop_event": threading.Event(),
            }
            self._save()
            return task_id

    def get(self, task_id):
        """取任务的公开视图；不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return self._public(task) if task else None

    def list(self):
        """全部任务的公开视图列表（按注册顺序）。"""
        with self._lock:
            return self._public_list_locked()

    def stop_event(self, task_id):
        """取任务的 stop_event（任务循环里检查它）；任务不存在返回 None。"""
        with self._lock:
            task = self._tasks.get(task_id)
            return task["stop_event"] if task else None

    # ---------------- 更新 ----------------

    def append_log(self, task_id, message):
        """往任务日志里追加一条消息（带时间前缀）。任务不存在返回 False。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task["logs"].append(self._stamp(message))
            self._save()
            return True

    def set_status(self, task_id, status):
        """更新任务状态。任务不存在返回 False。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task["status"] = status
            self._save()
            return True

    def set_result(self, task_id, result):
        """更新任务结果（JSON 可序列化字典）。任务不存在返回 False。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task["result"] = result
            self._save()
            return True

    def cancel(self, task_id):
        """取消任务：置 stop_event 并把状态标为 cancelled。

        返回 True 表示任务存在且在运行中；False 表示任务不存在或已结束。
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["status"] not in ("pending", "running"):
                return False
            task["stop_event"].set()
            task["status"] = "cancelled"
            task["logs"].append(self._stamp("任务已取消。"))
            self._save()
            return True

    def reset(self):
        """清空全部任务（测试用）。"""
        with self._lock:
            self._tasks.clear()
            self._counter = 0
            self._save()


# 全局任务注册表（单进程内共享）。
TASKS = TaskRegistry()


# ---------------- 抢机任务 ----------------

def _notify_grab_success(registry, task_id, cli, params, result, notify):
    """抢机成功后的双通道推送（与机器人端 _notify_boot_success 同逻辑）。

    查公网 IP（最多重试 3 次，间隔 3 秒；查不到写"获取中"），调
    notify_boot_success 发 TG + Bark；付费形状额外发一条置顶提醒。
    绝不抛异常，通知失败只记进任务日志，不影响任务本身。
    """
    try:
        public_ip = None
        for attempt in range(3):
            try:
                public_ip = get_public_ip(cli, result.instance_id)
            except Exception:  # noqa: BLE001 - 查 IP 失败就重试/放弃
                public_ip = None
            if public_ip or attempt == 2:
                break
            time.sleep(3)
        shape = params.get("shape") or ""
        ocpus = params.get("ocpus") or 1
        memory_gb = params.get("memory_gb")
        if not memory_gb:
            memory_gb = 1 if shape == "VM.Standard.E2.1.Micro" else 6
        instances = [{
            "name": params.get("display_name") or "?",
            "public_ip": public_ip,
            "attempts": result.attempts,
            "elapsed_seconds": result.elapsed_seconds,
        }]
        outcome = notify_boot_success(
            notify.get("token"), notify.get("chat_id"),
            notify.get("profile_label") or "?",
            notify.get("region_label") or "?",
            shape, ocpus, memory_gb,
            params.get("boot_volume_size_gbs"),
            instances,
            root_password=notify.get("root_password"),
            region_code=notify.get("region_code"),
            ssh_key_set=bool(params.get("ssh_keys_file")))
        if outcome.get("errors"):
            registry.append_log(
                task_id,
                "开机成功通知部分失败：" + "; ".join(outcome["errors"]))
        if shape not in FREE_SHAPES:
            paid = notify_dual(
                notify.get("token"), notify.get("chat_id"),
                "⚠️ 付费实例提醒",
                fmt_paid_instance_alert(
                    notify.get("profile_label") or "?",
                    notify.get("region_label") or "?",
                    shape, instances),
                group="paid-instance", pin=True)
            if paid.get("errors"):
                registry.append_log(
                    task_id,
                    "付费实例提醒部分失败：" + "; ".join(paid["errors"]))
    except Exception as exc:  # noqa: BLE001 - 通知绝不拖垮任务
        registry.append_log(task_id, f"开机成功通知失败：{exc}")


def _run_grabber(registry, task_id, cli, compartment_id, params,
                 notify=None):
    """抢机线程体（daemon 线程里跑）。

    进度通过 on_event 回调写进任务日志；结束时更新任务状态与结果。
    线程里任何异常都要落到任务状态，不能静默消失。
    notify 不为空时，抢机成功后双通道推送开机成功通知（TG + Bark，
    含 root 密码；见 _notify_grab_success）。
    """
    registry.set_status(task_id, "running")

    def on_event(_event_type, message):
        registry.append_log(task_id, message)

    grabber = Grabber(
        cli, compartment_id,
        params["availability_domain"], params["shape"],
        params["image_id"], params["subnet_id"],
        params["display_name"], params["ssh_keys_file"],
        target_cpu=params.get("target_cpu"),
        on_event=on_event,
        stop_event=registry.stop_event(task_id),
        # 0 表示未填（表单默认值），转 None 后 Grabber 用默认行为。
        ocpus=params.get("ocpus") or None,
        memory_gb=params.get("memory_gb") or None,
        boot_volume_size_gbs=params.get("boot_volume_size_gbs"),
        assign_public_ip=params.get("assign_public_ip", True),
        assign_ipv6=params.get("assign_ipv6", False),
        user_data=params.get("user_data") or None,
        min_delay=params.get("min_delay") or None,
        max_delay=params.get("max_delay") or None)
    try:
        result = grabber.run()
    except GrabCancelledError:
        registry.set_status(task_id, "cancelled")
        registry.append_log(task_id, "抢机已取消。")
        return
    except QuotaExceededError as exc:
        registry.set_status(task_id, "failed")
        registry.append_log(task_id, f"配额不足，停止抢机：{exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要落到任务状态
        registry.set_status(task_id, "failed")
        registry.append_log(task_id, f"抢机失败：{exc}")
        return
    # run() 正常返回后也检查一次取消标志（抢到瞬间被取消时不算成功）。
    stop_event = registry.stop_event(task_id)
    if stop_event is not None and stop_event.is_set():
        registry.set_status(task_id, "cancelled")
        registry.append_log(task_id, "抢机已取消。")
        return
    registry.set_result(task_id, {
        "instance_id": result.instance_id,
        "attempts": result.attempts,
        "releases": result.releases,
        "elapsed_seconds": round(result.elapsed_seconds, 1),
    })
    registry.append_log(
        task_id,
        f"抢机完成：实例 …{result.instance_id[-4:]}，"
        f"共尝试 {result.attempts} 次，"
        f"释放 {result.releases} 台 CPU 不符，"
        f"总耗时 {result.elapsed_seconds:.0f} 秒。")
    registry.set_status(task_id, "done")
    if notify:
        _notify_grab_success(registry, task_id, cli, params, result,
                             notify)


def start_grab_task(registry, cli, compartment_id, params,
                    detail_prefix="", notify=None):
    """注册抢机任务并开 daemon 线程跑 Grabber，返回任务 ID。

    params 需含：availability_domain / shape / image_id / subnet_id /
    display_name / ssh_keys_file；可选：target_cpu / ocpus / memory_gb /
    boot_volume_size_gbs / assign_public_ip / assign_ipv6 / min_delay /
    max_delay / user_data（缺了就用 Grabber 的默认值；ocpus / memory_gb /
    min_delay / max_delay 为 0 时按未填处理）。
    detail_prefix：任务 detail 前缀（如抢机计划触发时传 "抢机计划 p1"，
    供触发去重时识别同一计划的在跑任务；为空时不加前缀）。
    notify：双通道通知信息（见 _notify_grab_success），抢机成功后发
    开机成功通知；为 None 时不通知（抢机计划等内部触发不传）。
    """
    detail = (f"抢机：{params.get('display_name', '?')}"
              f"（{params.get('shape', '?')}）")
    if detail_prefix:
        detail = f"{detail_prefix}：{detail}"
    task_id = registry.create("grab", detail=detail)
    thread = threading.Thread(
        target=_run_grabber,
        args=(registry, task_id, cli, compartment_id, params, notify),
        daemon=True)
    thread.start()
    return task_id


# ---------------- 引导卷恢复（停机换盘，原地恢复） ----------------

# 正在换盘的实例 OCID 集合（同一实例同时只允许一个换盘任务；
# 与机器人进程的锁各自独立，跨进程同时换同一台需用户自行避免）。
_BOOTSWAP_RUNNING = set()
_BOOTSWAP_LOCK = threading.Lock()


def _run_bootswap(registry, task_id, cli, compartment_id, instance_id,
                  instance_name, backup_id, backup_name, notify):
    """换盘任务的线程体：跑 swap，进度进任务日志，结果双通道通知。"""
    registry.set_status(task_id, "running")

    def on_step(text):
        registry.append_log(task_id, text)

    try:
        result = swap_boot_volume(cli, compartment_id, instance_id,
                                  backup_id, on_step=on_step)
    except BootSwapError as exc:
        registry.set_status(task_id, "failed")
        registry.append_log(task_id, f"换盘失败：{exc}")
        if notify:
            body = (f"❌ 实例「{instance_name}」引导卷恢复失败：{exc}\n"
                    + ("已换回旧盘。" if exc.rolled_back
                       else "回滚未完成，请在控制台手动处理。"))
            try:
                notify_dual(notify.get("token"), notify.get("chat_id"),
                            "💾 引导卷恢复失败", body, group="bootswap")
            except Exception as notify_exc:  # noqa: BLE001 - 通知失败只记日志
                registry.append_log(task_id, f"通知发送失败：{notify_exc}")
        return
    except Exception as exc:  # noqa: BLE001 - 线程里任何异常都要落到任务状态
        registry.set_status(task_id, "failed")
        registry.append_log(task_id, f"换盘失败：{exc}")
        if notify:
            try:
                notify_dual(notify.get("token"), notify.get("chat_id"),
                            "💾 引导卷恢复失败",
                            f"❌ 实例「{instance_name}」引导卷恢复失败：{exc}",
                            group="bootswap")
            except Exception as notify_exc:  # noqa: BLE001 - 通知失败只记日志
                registry.append_log(task_id, f"通知发送失败：{notify_exc}")
        return
    finally:
        with _BOOTSWAP_LOCK:
            _BOOTSWAP_RUNNING.discard(instance_id)
    registry.set_result(task_id, {
        "instance_id": instance_id,
        "old_boot_volume_id": result.get("old_boot_volume_id"),
        "new_boot_volume_id": result.get("new_boot_volume_id"),
        "was_running": result.get("was_running"),
    })
    old_tail = (result.get("old_boot_volume_id") or "")[-4:]
    new_tail = (result.get("new_boot_volume_id") or "")[-4:]
    registry.append_log(
        task_id,
        f"换盘成功：新盘 …{new_tail}，旧盘 …{old_tail}（已拆下，未删除）。")
    registry.set_status(task_id, "done")
    if notify:
        body = (
            f"✅ 实例「{instance_name}」引导卷恢复成功。\n"
            f"备份：{backup_name}\n"
            f"新启动盘：…{new_tail}\n"
            f"旧启动盘：…{old_tail}（已拆下，未删除，仍占额度）\n"
            + ("实例已重新开机。"
               if result.get("was_running") else "实例保持停止状态。"))
        try:
            notify_dual(notify.get("token"), notify.get("chat_id"),
                        "💾 引导卷恢复成功", body, group="bootswap")
        except Exception as notify_exc:  # noqa: BLE001 - 通知失败只记日志
            registry.append_log(task_id, f"通知发送失败：{notify_exc}")


def start_bootswap_task(registry, cli, compartment_id, instance_id,
                        instance_name, backup_id, backup_name,
                        notify=None):
    """注册换盘任务并开 daemon 线程跑，返回任务 ID。

    同一实例已有换盘任务在跑时抛 ValueError（调用方应转 409）。
    notify：双通道通知信息 {"token", "chat_id"}，为 None 时不通知。
    """
    with _BOOTSWAP_LOCK:
        if instance_id in _BOOTSWAP_RUNNING:
            raise ValueError("这台实例正在换盘，请等它完成后再试。")
        _BOOTSWAP_RUNNING.add(instance_id)
    task_id = registry.create(
        "bootswap", detail=f"引导卷恢复：{instance_name}")
    thread = threading.Thread(
        target=_run_bootswap,
        args=(registry, task_id, cli, compartment_id, instance_id,
              instance_name, backup_id, backup_name, notify),
        daemon=True)
    thread.start()
    return task_id


# ---------------- 抢机计划（定时抢机） ----------------

def validate_cron(expr):
    """校验 cron 表达式是否合法（标准 5 段 crontab，如 "0 2 * * *"）。

    用 APScheduler 自身的 CronTrigger.from_crontab 校验（croniter 未装，
    不引入新依赖）。合法返回去掉首尾空白的表达式；非法抛 ValueError
    （带 APScheduler 的原始报错原因）。
    APScheduler 未安装时直接抛 ValueError 提示安装依赖。
    """
    if CronTrigger is None:
        raise ValueError(
            "APScheduler 未安装，无法校验 cron 表达式："
            "请先 pip install apscheduler")
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("cron 表达式不能为空（标准 5 段，如 \"0 2 * * *\"）")
    expr = expr.strip()
    try:
        CronTrigger.from_crontab(expr)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"cron 表达式无效：{expr!r}（{exc}）")
    return expr


class GrabberPlanRegistry:
    """抢机计划注册表：存 DATA_DIR/grabber_plans.json。

    计划字段：id / cron / shape / ocpus / memory_gb / enabled，
    外加可选字段 profile / availability_domain / image_id / subnet_id /
    display_name / ssh_keys_file / target_cpu（缺了就用 PLAN_DEFAULTS）。

    注意：本注册表不做内存缓存，每次操作都重新读文件——Telegram
    机器人与 Web 面板是两个进程，共用同一个计划文件，读文件才能
    互相看到对方的修改（hosts.py 的 hosts.json 也是这个模式）。
    """

    def _persist_path(self):
        """计划文件路径（每次都从 config 读 DATA_DIR，方便测试替换）。"""
        return os.path.join(config.DATA_DIR, "grabber_plans.json")

    def _load(self):
        """读计划文件，返回计划列表；文件不存在或格式不对返回 []。"""
        try:
            with open(self._persist_path(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, plans):
        """把计划列表写回文件。"""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(self._persist_path(), "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=2)

    def list(self):
        """全部计划（按添加顺序）。"""
        return self._load()

    def get(self, plan_id):
        """按 id 取计划；不存在返回 None。"""
        for plan in self._load():
            if plan.get("id") == plan_id:
                return plan
        return None

    def add(self, cron, shape, ocpus, memory_gb, enabled=True, **extra):
        """新增一条计划，返回完整计划字典。

        cron 非法、shape 不在 PLAN_SHAPES 里、ocpus/memory_gb 超出
        免费额度范围时抛 ValueError。extra 里的可选字段原样存入
        （只保留白名单字段，其余丢弃）。
        """
        cron = validate_cron(cron)
        if shape not in PLAN_SHAPES:
            raise ValueError(
                f"shape 只能是 {', '.join(PLAN_SHAPES)}，收到 {shape!r}")
        try:
            ocpus = int(ocpus)
            memory_gb = int(memory_gb)
        except (TypeError, ValueError) as exc:
            raise ValueError("ocpus / memory_gb 必须是整数") from exc
        if not 1 <= ocpus <= PLAN_MAX_OCPUS:
            raise ValueError(
                f"ocpus 必须在 1-{PLAN_MAX_OCPUS} 之间，收到 {ocpus}")
        if not 1 <= memory_gb <= PLAN_MAX_MEMORY_GB:
            raise ValueError(
                f"memory_gb 必须在 1-{PLAN_MAX_MEMORY_GB} 之间，"
                f"收到 {memory_gb}")
        plans = self._load()
        # id 按已有计划的最大数字后缀 +1（删过计划也不会重用 id）。
        max_num = 0
        for plan in plans:
            digits = "".join(ch for ch in str(plan.get("id", ""))
                             if ch.isdigit())
            if digits:
                max_num = max(max_num, int(digits))
        plan = {
            "id": f"p{max_num + 1}",
            "cron": cron,
            "shape": shape,
            "ocpus": ocpus,
            "memory_gb": memory_gb,
            "enabled": bool(enabled),
            "created_at": time.time(),
        }
        for key in ("profile", "availability_domain", "image_id",
                    "subnet_id", "display_name", "ssh_keys_file",
                    "target_cpu"):
            if extra.get(key) is not None:
                plan[key] = extra[key]
        plans.append(plan)
        self._save(plans)
        return plan

    def delete(self, plan_id):
        """删除计划；不存在返回 False。"""
        plans = self._load()
        kept = [p for p in plans if p.get("id") != plan_id]
        if len(kept) == len(plans):
            return False
        self._save(kept)
        return True

    def set_enabled(self, plan_id, enabled):
        """开关计划；不存在返回 None，否则返回更新后的计划字典。"""
        plans = self._load()
        for plan in plans:
            if plan.get("id") == plan_id:
                plan["enabled"] = bool(enabled)
                self._save(plans)
                return plan
        return None

    def reset(self):
        """清空全部计划（测试用）。"""
        self._save([])


# 全局抢机计划注册表（单进程内共享；多进程靠文件同步）。
PLANS = GrabberPlanRegistry()


# ---------------- 抢机计划调度器（APScheduler） ----------------

_scheduler = None
_scheduler_lock = threading.Lock()
# 计划 job 的 id 前缀（同步时只动带此前缀的 job，不动 plans-refresh）。
_PLAN_JOB_PREFIX = "plan-"
# 定时同步 job 的 id（每分钟从文件重读计划，同步 job 列表）。
_PLANS_REFRESH_JOB_ID = "plans-refresh"


def _plan_job_id(plan_id):
    """某计划在调度器里的 job id。"""
    return f"{_PLAN_JOB_PREFIX}{plan_id}"


def _plan_grab_params(plan):
    """把计划转成 start_grab_task 需要的 params（缺的字段补默认值）。

    display_name 没填时用 PLAN_DEFAULTS 的 display_name + 计划 id，
    避免多条计划抢出的实例重名。
    """
    display_name = plan.get("display_name") or (
        f"{PLAN_DEFAULTS['display_name']}-{plan['id']}")
    params = {
        "availability_domain": plan.get("availability_domain")
                               or PLAN_DEFAULTS["availability_domain"],
        "shape": plan["shape"],
        "image_id": plan.get("image_id") or PLAN_DEFAULTS["image_id"],
        "subnet_id": plan.get("subnet_id") or PLAN_DEFAULTS["subnet_id"],
        "display_name": display_name,
        "ssh_keys_file": plan.get("ssh_keys_file")
                         or PLAN_DEFAULTS["ssh_keys_file"],
        "target_cpu": plan.get("target_cpu", PLAN_DEFAULTS["target_cpu"]),
        "ocpus": plan.get("ocpus"),
        "memory_gb": plan.get("memory_gb"),
    }
    return params


def _run_plan_job(plan_id):
    """计划触发时的回调：复用 start_grab_task 启动抢机任务。

    由 APScheduler 在调度器线程里调用。计划被删/被停用、或同一计划
    已有抢机任务在跑时直接跳过（不重复抢机）。异常只记到任务日志
    体系外：这里没有任务可记，打印到标准输出供 systemd 日志查看。
    """
    plan = PLANS.get(plan_id)
    if plan is None or not plan.get("enabled"):
        return
    marker = f"抢机计划 {plan_id}"
    for task in TASKS.list():
        if (task.get("kind") == "grab"
                and task.get("status") in ("pending", "running")
                and marker in (task.get("detail") or "")):
            print(f"抢机计划 {plan_id} 已有抢机任务在跑，本次触发跳过。")
            return
    try:
        profile = plan.get("profile") or config.DEFAULT_PROFILE
        cfg = config.get_profile_config(profile)
        cli = OCIClient(profile=profile, region=cfg["region"])
        params = _plan_grab_params(plan)
        # detail 前缀带计划 id，供上面的去重判断识别同一计划的在跑任务。
        start_grab_task(TASKS, cli, cfg["compartment_id"], params,
                        detail_prefix=marker)
    except Exception as exc:  # noqa: BLE001 - 调度器线程里异常不能外泄
        print(f"抢机计划 {plan_id} 触发失败：{exc}")


def _sync_plan_jobs_locked():
    """按当前计划文件同步调度器里的计划 job（调用方需持有锁）。

    只动 id 带 plan- 前缀的 job（plans-refresh 同步 job 本身不动）。
    启用的计划各注册一个 cron job；停用/删除的计划 job 被移除。
    """
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith(_PLAN_JOB_PREFIX):
            _scheduler.remove_job(job.id)
    for plan in PLANS.list():
        if not plan.get("enabled"):
            continue
        try:
            trigger = CronTrigger.from_crontab(plan["cron"])
        except (ValueError, TypeError, KeyError):
            # 计划文件被手工改坏时跳过该计划，不让调度器崩掉。
            print(f"抢机计划 {plan.get('id')} 的 cron 无效，已跳过："
                  f"{plan.get('cron')!r}")
            continue
        _scheduler.add_job(_run_plan_job, trigger=trigger,
                           args=[plan["id"]],
                           id=_plan_job_id(plan["id"]),
                           replace_existing=True)


def refresh_plan_scheduler():
    """按当前计划文件重新同步调度器里的 job（增/删/开关计划后调用）。

    调度器未启动时什么都不做。
    """
    with _scheduler_lock:
        _sync_plan_jobs_locked()


def start_plan_scheduler():
    """启动抢机计划调度器（幂等，重复调用返回同一个调度器）。

    按当前启用的计划注册 cron job，另注册一个每分钟的同步 job
    （Telegram 机器人改了计划文件后，面板调度器 1 分钟内跟上）。
    APScheduler 未安装时打印警告并返回 None。
    """
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        if BackgroundScheduler is None or CronTrigger is None:
            print("警告：APScheduler 未安装，抢机计划调度器无法启动，"
                  "请先 pip install apscheduler。")
            return None
        _scheduler = BackgroundScheduler()
        _sync_plan_jobs_locked()
        _scheduler.add_job(
            refresh_plan_scheduler, "interval",
            seconds=PLAN_SYNC_INTERVAL_SECONDS,
            id=_PLANS_REFRESH_JOB_ID, replace_existing=True)
        _scheduler.start()
        return _scheduler


def stop_plan_scheduler():
    """停止抢机计划调度器（面板关闭与测试用）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None
