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
服务重启时，带抢机规格（grab_spec）的抢机任务按规格恢复重跑，
其余还在跑的任务标记为 cancelled（进程已死，线程不可能还在跑）。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime

from oci.boot_swap import BootSwapError, swap_boot_volume
from oci.client import OCIClient, QuotaExceededError
from oci.dd_init import DD_SSH_PORT, start_dd_init
from oci.grabber import GrabCancelledError, Grabber
from oci.network import get_public_ip, wait_for_public_ip
from tgbot import config as bot_config
from tgbot import task_store
from tgbot.billing import FREE_SHAPES
from tgbot.cpu_policy import (combined_boot_keys,
                              get_allowed_cpu_models, make_cpu_check_hook)
from tgbot.handlers_client import run_remote_command
from tgbot.instance_credentials import (resolve_root_password,
                                       save_credential,
                                       update_credential_ip)
from tgbot.notify import (
    fmt_notify_outcome,
    fmt_paid_instance_alert,
    notify_boot_success,
    notify_dual,
    start_ip_followup,
)
from web import config
from web.routes.cloudreport import clear_cloud_report_cache
from web.routes.quota import clear_quota_cache

try:
    # APScheduler: cron scheduling for grabber plans (an explicit dependency in requirements.txt; when not installed, plans can still be added/edited/removed, but the scheduler cannot start and validate_cron reports the error explicitly). APScheduler：抢机计划的 cron 调度（requirements.txt 里有显式依赖；未安装时计划仍可增删改，但调度器无法启动，validate_cron 会明确报错）。
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    BackgroundScheduler = None
    CronTrigger = None

# ================= 抢机计划配置区（改这里） =================
# Full parameters the Grabber needs when a grabber plan fires; the plan itself only stores cron / shape / ocpus / memory_gb / enabled (see GrabberPlanRegistry), and the remaining fields (availability domain / image / subnet / instance name / SSH public key file) prefer the plan's optional fields, falling back to the defaults below. 抢机计划触发时 Grabber 需要的完整参数；计划本身只存 cron / shape / ocpus / memory_gb / enabled（见 GrabberPlanRegistry），其余字段（可用域 / 镜像 / 子网 / 实例名 / SSH 公钥文件）优先取计划里的可选字段，缺了就用下面这些默认值。
# At deploy time fill the real values via env vars (MBOT_PLAN_AD / MBOT_PLAN_IMAGE_ID / MBOT_PLAN_SUBNET_ID / MBOT_PLAN_SSH_KEYS_FILE); do not edit the code. 部署时用环境变量填真实值（MBOT_PLAN_AD / MBOT_PLAN_IMAGE_ID / MBOT_PLAN_SUBNET_ID / MBOT_PLAN_SSH_KEYS_FILE），不改代码。
PLAN_DEFAULTS = {
    # Availability domain (e.g. "xxxx:AP-SINGAPORE-1-AD-1"; see the instance details in the OCI console). 可用域（如 "xxxx:AP-SINGAPORE-1-AD-1"，OCI 控制台实例详情里看）。
    "availability_domain": os.environ.get(
        "MBOT_PLAN_AD", "xxxx:AP-SINGAPORE-1-AD-1"),
    # System image OCID (lookup method in the IMAGE_IDS comment in web/config.py). 系统镜像 OCID（查法见 web/config.py 的 IMAGE_IDS 注释）。
    "image_id": os.environ.get(
        "MBOT_PLAN_IMAGE_ID", "ocid1.image.oc1..xxxxxxxx"),
    # Subnet OCID (see VCN in the OCI console). 子网 OCID（OCI 控制台 VCN 里看）。
    "subnet_id": os.environ.get(
        "MBOT_PLAN_SUBNET_ID", "ocid1.subnet.oc1..xxxxxxxx"),
    # Instance display name (used with the plan id suffix when the plan has no display_name). 实例显示名（计划里没填 display_name 时用它 + 计划 id 后缀）。
    "display_name": "plan-grab",
    # SSH public key file path (source of the instance's authorized_keys content). SSH 公钥文件路径（实例的 authorized_keys 内容来源）。
    "ssh_keys_file": os.environ.get(
        "MBOT_PLAN_SSH_KEYS_FILE", "/path/to/keys.pub"),
    # Target CPU (E2 grabbing filters for 7J13; None for A1 means keep whatever is grabbed). 目标 CPU（E2 抢机筛 7J13 用；A1 填 None 表示抢到就留）。
    "target_cpu": None,
}
# Allowed grab shapes (used by plan validation). 允许的抢机形状（计划校验用）。
PLAN_SHAPES = ("VM.Standard.E2.1.Micro", "VM.Standard.A1.Flex")
# A1.Flex free-tier cap (used by plan validation: ocpus 1-4, memory 1-24 GB). A1.Flex 免费额度上限（计划校验用：ocpus 1-4，内存 1-24GB）。
PLAN_MAX_OCPUS = 4
PLAN_MAX_MEMORY_GB = 24
# The scheduler re-syncs plans from the file every this many seconds (the Telegram bot and the web panel share grabber_plans.json; after the bot changes a plan, the panel scheduler picks it up within 1 minute via this sync, no panel restart needed). 调度器每隔这么多秒重新从文件同步一次计划（Telegram 机器人与 Web 面板共用 grabber_plans.json，机器人侧改了计划后，面板调度器靠这次同步在 1 分钟内跟上，不用重启面板）。
PLAN_SYNC_INTERVAL_SECONDS = 60
# =====================================================

logger = logging.getLogger(__name__)


class TaskRegistry:
    """后台长任务的注册表：线程安全，支持创建/查询/日志/取消。

    任务记录存在共享文件 tasks.json（tgbot/task_store.py，与机器人
    同一份），每次查询都重新读文件，所以机器人建的任务这里也看得见；
    stop_event 只存内存（本进程的任务才有），对方进程的任务取消时
    走跨进程请求（对方的监听线程收到后置 stop_event）。
    """

    def __init__(self, tasks_file=None):
        self._lock = threading.RLock()
        self._stop_events = {}
        # When tasks_file is empty, read from task_store.tasks_path() each time (follows web.config.DATA_DIR changes, so tests can swap it). tasks_file 为空时每次从 task_store.tasks_path() 读（跟随 web.config.DATA_DIR 的变化，方便测试替换）。
        self._tasks_file = tasks_file

    @property
    def tasks_file(self):
        """共享任务文件路径（每次解析，测试替换 DATA_DIR 后立即生效）。"""
        return self._tasks_file or task_store.tasks_path()

    def recover(self):
        """启动时调用（web/app.create_app）：把本进程上次未结束的任务
        标为已中断；对方进程的任务不动。"""
        task_store.recover_interrupted(self.tasks_file, "web")

    # ---------------- 注册 / 查询 ----------------

    def create(self, kind, detail="", label=None):
        """注册一个任务，返回 ID（如抢机任务 "web-g1"）。初始状态 pending。

        label 是显示用的任务说明（如 "E2.1.Micro（新加坡）·
        AMD_2026-01-01"），任务页用它当第一列，不再直接暴露
        grab 这类内部类型；旧任务没有 label 时前端退回 kind。
        """
        with self._lock:
            record = task_store.create_record(
                self.tasks_file, "web", kind, detail, label)
            self._stop_events[record["id"]] = threading.Event()
            return record["id"]

    def get(self, task_id):
        """取任务的公开视图；不存在返回 None（含机器人建的任务）。"""
        with self._lock:
            record = task_store.get_record(self.tasks_file, task_id)
            if record is None:
                return None
            return task_store.public_record(record)

    def list(self):
        """全部任务的公开视图列表（按注册顺序，含机器人建的任务）。"""
        with self._lock:
            return [task_store.public_record(record)
                    for record in task_store.load_records(self.tasks_file)]

    def attach(self, task_id):
        """把共享文件里已有的任务接进本进程（启动恢复抢机用）。

        只补 stop_event（网页版注册表不存内存任务副本）；
        记录不存在返回 False。
        """
        with self._lock:
            if task_id in self._stop_events:
                return True
            if task_store.get_record(self.tasks_file, task_id) is None:
                return False
            self._stop_events[task_id] = threading.Event()
            return True

    def stop_event(self, task_id):
        """取任务的 stop_event（任务循环里检查它）；任务不存在或
        是对方进程的任务时返回 None。"""
        with self._lock:
            return self._stop_events.get(task_id)

    # ---------------- 更新 ----------------

    def _update(self, task_id, **fields):
        """写一条任务更新；磁盘不可用时只记 warning，不抛异常。

        抢机等后台线程在任务进行中持续调 append_log / set_status；
        若共享文件所在的目录被删（如测试清理临时目录）或磁盘出
        问题，写盘失败不能杀死线程、也不能让任务卡在 running。
        返回更新后的记录；任务不存在或写盘失败时返回 None。
        """
        try:
            return task_store.update_record(
                self.tasks_file, task_id, **fields)
        except OSError as exc:
            logger.warning("任务 %s 更新写盘失败（%s），更新丢失：%s",
                           task_id, type(exc).__name__, exc)
            return None

    def append_log(self, task_id, message):
        """往任务日志里追加一条消息（带时间前缀），同时刷新详情为
        最新消息（机器人端任务列表只显示详情）。任务不存在返回 False。"""
        with self._lock:
            record = self._update(task_id, detail=message, log=message)
            return record is not None

    def set_status(self, task_id, status):
        """更新任务状态。任务不存在返回 False。"""
        with self._lock:
            record = self._update(task_id, status=status)
            return record is not None

    def set_result(self, task_id, result):
        """更新任务结果（JSON 可序列化字典）。任务不存在返回 False。"""
        with self._lock:
            record = self._update(task_id, result=result)
            return record is not None

    def cancel(self, task_id):
        """取消任务：本进程的任务置 stop_event 并把状态标为 cancelled；
        对方进程的任务发跨进程取消请求（对方的监听线程执行）。

        返回 True 表示任务存在且在运行中；False 表示任务不存在或已结束。
        """
        with self._lock:
            if task_id in self._stop_events:
                task = task_store.get_record(self.tasks_file, task_id)
                if task is None or task["status"] not in ("pending",
                                                          "running"):
                    return False
                self._stop_events[task_id].set()
                self._update(task_id, status="cancelled",
                             log="任务已取消。")
                return True
            return task_store.request_cancel(self.tasks_file, task_id)

    def delete(self, task_id):
        """删除一条任务记录（只对已结束的任务有效；运行中的任务须先取消）。

        返回 True 表示已删除；False 表示任务不存在或仍在运行中。
        """
        with self._lock:
            record = task_store.get_record(self.tasks_file, task_id)
            if record is None:
                return False
            if record.get("status") in ("pending", "running"):
                return False
            self._stop_events.pop(task_id, None)
            return task_store.delete_record(self.tasks_file, task_id)

    def reset(self):
        """清空全部任务（测试用）。"""
        with self._lock:
            self._stop_events.clear()
            task_store.clear_records(self.tasks_file)


# Global task registry (task records share tasks.json with the bot). 全局任务注册表（任务记录与机器人共用 tasks.json）。
TASKS = TaskRegistry()


# ---------------- 抢机任务 ----------------

def _notify_grab_success(registry, task_id, cli, params, result, notify):
    """抢机成功后的双通道推送（与机器人端 _notify_boot_success 同逻辑）。

    等公网 IP（最长 60 秒）；60 秒还没 IP 就先发通知写"获取中"，
    再起跟进线程后台继续等，IP 一出来补发一条 TG 消息。调
    notify_boot_success 发 TG + Bark；付费形状额外发一条置顶提醒。
    绝不抛异常，通知失败只记进任务日志，不影响任务本身。
    """
    try:
        public_ip = wait_for_public_ip(cli, result.instance_id)
        if not public_ip:
            start_ip_followup(
                lambda: get_public_ip(cli, result.instance_id),
                params.get("display_name") or "?",
                notify.get("token"), notify.get("chat_id"),
                on_ip=lambda ip: update_credential_ip(
                    result.instance_id, ip))
        # 开机成功先把 root 密码按实例存进凭据库（之后 SSH、DD 后登录、
        # DD 后追加 SSH 公钥都靠它；用通知里那份密码，不另生成）。
        # 存失败只记任务日志，不拖垮通知。
        if notify.get("root_password") and result.instance_id:
            try:
                save_credential(
                    result.instance_id, notify["root_password"],
                    ip=public_ip,
                    instance_name=params.get("display_name"))
            except Exception as exc:  # noqa: BLE001 - 存失败不拖垮通知
                registry.append_log(task_id, f"root 密码保存失败：{exc}")
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
            ssh_key_set=bool(params.get("ssh_keys_file")),
            cpu_unverified=notify.get("cpu_unverified", False),
            cpu_model=getattr(result, "cpu_model", None))
        if outcome.get("errors"):
            registry.append_log(
                task_id,
                "开机成功通知部分失败：" + "; ".join(outcome["errors"]))
        else:
            # A success also leaves a record line: if a push is lost, there is still evidence to check. 成功也要留一行记录：推送丢了才有据可查。
            registry.append_log(task_id, fmt_notify_outcome(outcome))
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
        # DD 重装开时：开机成功通知已发，接着起 DD 初始化后台任务
        # （等 DD 完成再经 SSH 跑初始化脚本；没脚本也跟踪 DD 完成并通知）。
        # 密码先用通知里那份，没有时按实例 OCID 读凭据库里开机时存的那份。
        _dd_password = notify.get("root_password") or \
            resolve_root_password(result.instance_id)
        if params.get("dd") and _dd_password and public_ip:
            name = params.get("display_name") or "?"
            token = notify.get("token")
            chat_id = notify.get("chat_id")

            def _on_event(event_type, message, _name=name, _ip=public_ip):
                if event_type == "dd_done":
                    # dd_done 的 message 已含“有/无初始化脚本”的说明，直接发。
                    notify_dual(token, chat_id, "🔄 DD 完成", message)
                elif event_type == "init_done":
                    # init_done 的 message 已含验证结果（密码登录可用与否），
                    # 直接发双通道通知（Telegram + Bark）。
                    notify_dual(token, chat_id, "✅ 初始化完成", message)
                elif event_type in ("dd_timeout", "init_timeout",
                                    "init_failed"):
                    notify_dual(token, chat_id, "❌ DD 初始化失败",
                                f"实例 {_name}（{_ip}）{message}")
                registry.append_log(task_id, message)

            start_dd_init(public_ip, _dd_password,
                          params.get("init_script"), instance_name=name,
                          port=DD_SSH_PORT, on_event=_on_event)
            registry.append_log(task_id, f"已起 DD 初始化任务：{name}（{public_ip}）")
        elif params.get("dd"):
            registry.append_log(task_id, "DD 已开但缺 root 密码或公网 IP，未起 DD 初始化。")
    except Exception as exc:  # noqa: BLE001 - 通知绝不拖垮任务
        registry.append_log(task_id, f"开机成功通知失败：{exc}")


def _run_grabber(registry, task_id, cli, compartment_id, params,
                 notify=None, resume_spec=None):
    """抢机线程体（daemon 线程里跑）。

    进度通过 on_event 回调写进任务日志；结束时更新任务状态与结果。
    线程里任何异常都要落到任务状态，不能静默消失。
    notify 不为空时，抢机成功后双通道推送开机成功通知（TG + Bark，
    含 root 密码；见 _notify_grab_success）。
    target_cpu 非空时按 tgbot/cpu_policy.py 的规则筛 CPU（只留
    配置里的型号，默认 7J13）：开机时把 guard 公钥拼进启动
    SSH keys，Grabber 注入 cpu_check_hook，抢到先 SSH 验 CPU，
    不符释放（永删引导卷）继续抢；guard 密钥未配置时筛不了，
    日志里写明并抢到即留。
    持久化：启动时把完整抢机规格写进共享任务记录（grab_spec），
    抢到后立即更新已抢列表；服务重启后 resume_interrupted_grabs
    按它恢复抢机（resume_spec 传进来时就是恢复启动）。规格写盘
    失败只记日志，不影响抢机本身。
    """
    registry.set_status(task_id, "running")

    # E2 grabs filter by CPU by default: when the caller leaves target_cpu
    # empty, fill it with the allowed models from cpu_policy.json (default
    # 7J13-only), so a panel grab never keeps a wrong-CPU machine.
    # E2 默认筛 CPU：调用方没填 target_cpu 时用 cpu_policy.json 的允许型号
    # （默认只留 7J13）补上，面板抢机不再留错 CPU 的机器。
    if (not params.get("target_cpu")
            and params.get("shape") == "VM.Standard.E2.1.Micro"):
        params = dict(params)
        params["target_cpu"] = get_allowed_cpu_models()

    # "any" is the caller's explicit no-filter choice (the panel CPU
    # dropdown's "什么 CPU 都可以", same as the bot's b:cpu:any).
    # Convert after the E2 default-fill above so the fill doesn't
    # re-apply to an explicit "any".
    # “any”是调用方明确不要筛 CPU（面板 CPU 下拉的“什么 CPU 都可以”，
    # 与机器人 b:cpu:any 一致）。放在 E2 默认补全之后转，避免补全
    # 把明确的 any 又盖回去。
    if params.get("target_cpu") == "any":
        params = dict(params)
        params["target_cpu"] = None

    # Grab spec written to disk (on recovery startup, reuse the spec passed in, do not rebuild it). 抢机规格写盘（恢复启动时沿用传进来的那份，不重建）。
    if resume_spec is not None:
        spec = resume_spec
    else:
        spec = None
        if registry.tasks_file:
            try:
                spec = task_store.build_grab_spec(
                    cli.profile, cli.region, compartment_id, params,
                    notify)
                task_store.update_record(
                    registry.tasks_file, task_id, fields={"grab_spec": spec})
            except Exception as exc:  # noqa: BLE001 - 规格写盘失败不拖垮抢机
                logger.warning("抢机规格写盘失败（任务 %s）：%s",
                               task_id, exc)
                spec = None

    cpu_unverified = False

    def on_event(_event_type, message):
        nonlocal cpu_unverified
        if _event_type in ("cpu_check_failed", "cpu_filter_unavailable"):
            cpu_unverified = True
        registry.append_log(task_id, message)

    # Cross-process cancel listener: when the bot side cancels, the other side writes cancel_requested into a shared file; polling here sets stop_event and the grab loop stops. 跨进程取消监听：机器人端点取消时，对方把 cancel_requested 写进共享文件，这里轮询到后置 stop_event，抢机循环随即停下。
    task_store.start_cancel_watcher(registry.tasks_file, task_id,
                                    registry.stop_event(task_id))

    filtering = bool(params.get("target_cpu"))
    try:
        with combined_boot_keys(params.get("ssh_keys_file"),
                                include_guard=filtering) as boot_keys:
            if filtering and not boot_keys.guard_included:
                on_event("cpu_filter_unavailable",
                         "未配置 guard 密钥，CPU 筛选未生效，抢到即留。")
                target_cpus = None
                hook = None
            elif filtering:
                target_cpus = get_allowed_cpu_models()
                hook = make_cpu_check_hook(cli, run_remote_command)
            else:
                target_cpus = None
                hook = None
            grabber = Grabber(
                cli, compartment_id,
                params["availability_domain"], params["shape"],
                params["image_id"], params["subnet_id"],
                params["display_name"], boot_keys.path,
                target_cpu=target_cpus,
                on_event=on_event,
                stop_event=registry.stop_event(task_id),
                # 0 means unset (the form default); converted to None, the Grabber uses its default behavior. 0 表示未填（表单默认值），转 None 后 Grabber 用默认行为。
                ocpus=params.get("ocpus") or None,
                memory_gb=params.get("memory_gb") or None,
                boot_volume_size_gbs=params.get("boot_volume_size_gbs"),
                assign_public_ip=params.get("assign_public_ip", True),
                assign_ipv6=params.get("assign_ipv6", False),
                user_data=params.get("user_data") or None,
                min_delay=params.get("min_delay") or None,
                max_delay=params.get("max_delay") or None)
            if hook is not None:
                grabber.cpu_check_hook = hook
            result = grabber.run()
            # Write the success into the spec immediately after grabbing: if the process dies before marking done, recovery treats it as already fully grabbed and does not grab another one. 抢到后立即把成功写进规格：标 done 之前进程死了，恢复时按已抢满处理，不会重复再抢一台。
            if spec is not None and registry.tasks_file:
                try:
                    spec["successes"] = [
                        task_store.grab_success_to_dict(result)]
                    task_store.update_record(
                        registry.tasks_file, task_id,
                        fields={"grab_spec": spec})
                except Exception as exc:  # noqa: BLE001 - 写盘失败不拖垮抢机
                    logger.warning("抢机进度写盘失败（任务 %s）：%s",
                                   task_id, exc)
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
    # Check the cancel flag once more after run() returns normally (a grab cancelled at the exact moment of success does not count as success). run() 正常返回后也检查一次取消标志（抢到瞬间被取消时不算成功）。
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
    clear_cloud_report_cache()
    clear_quota_cache()
    if notify:
        notify_with_flag = dict(notify)
        notify_with_flag["cpu_unverified"] = cpu_unverified
        _notify_grab_success(registry, task_id, cli, params, result,
                             notify_with_flag)


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
    shape_short = str(params.get("shape", "?")).replace("VM.Standard.", "")
    region_label = (notify or {}).get("region_label")
    if region_label:
        label = (f"{shape_short}（{region_label}）· "
                 f"{params.get('display_name', '?')}")
    else:
        label = f"{shape_short} · {params.get('display_name', '?')}"
    task_id = registry.create("grab", detail=detail, label=label)
    thread = threading.Thread(
        target=_run_grabber,
        args=(registry, task_id, cli, compartment_id, params, notify),
        daemon=True)
    thread.start()
    return task_id


# ---------------- 抢机任务启动恢复 ----------------

def _resume_notify(spec):
    """恢复用的 notify 字典：规格里存的字段 + 现读的 token。"""
    notify = dict(spec.get("notify") or {})
    notify["token"] = bot_config.TELEGRAM_BOT_TOKEN
    return notify


def _relaunch_grab(registry, record, spec):
    """按规格重新拉起一个抢机线程（启动恢复用）。"""
    task_id = record["id"]
    params = spec["params"]
    try:
        cli = OCIClient(profile=spec.get("profile") or "DEFAULT",
                        region=spec.get("region") or "ap-singapore-1")
    except Exception as exc:  # noqa: BLE001 - 账号配不上时任务标失败，不炸启动
        logger.warning("恢复抢机 %s：建 OCI 客户端失败：%s", task_id, exc)
        registry.attach(task_id)
        registry.set_status(task_id, "failed")
        registry.append_log(task_id,
                            f"重启后恢复抢机失败：建 OCI 客户端失败：{exc}")
        return
    registry.attach(task_id)
    registry.append_log(task_id, "🔄 重启后恢复抢机")
    logger.info("恢复抢机 %s：%s", task_id, params.get("display_name"))
    thread = threading.Thread(
        target=_run_grabber,
        args=(registry, task_id, cli, spec.get("compartment_id"),
              dict(params), _resume_notify(spec)),
        kwargs={"resume_spec": spec},
        daemon=True)
    thread.start()


def _finish_resumed_grab(registry, record, spec, successes):
    """已抢满的恢复任务：标记完成、写结果、后台补发成功通知。

    状态还是 running 说明成功通知还没发出去（线程里的顺序是
    先标 done 再通知），所以这里要补发；补发在后台线程里做
    （等公网 IP 最长 60 秒，不能堵面板启动）。
    """
    task_id = record["id"]
    params = spec["params"]
    result = successes[0]
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
        f"总耗时 {result.elapsed_seconds:.0f} 秒。"
        "（服务重启后恢复，抢机已完成）")
    registry.set_status(task_id, "done")

    def _notify_in_background():
        try:
            cli = OCIClient(profile=spec.get("profile") or "DEFAULT",
                            region=spec.get("region") or "ap-singapore-1")
        except Exception as exc:  # noqa: BLE001 - 客户端建不出就不补通知
            logger.warning("恢复抢机 %s：补发通知时建 OCI 客户端失败：%s",
                           task_id, exc)
            return
        _notify_grab_success(registry, task_id, cli, dict(params), result,
                             _resume_notify(spec))

    threading.Thread(target=_notify_in_background, daemon=True).start()


def resume_interrupted_grabs(registry=None):
    """启动恢复（web lifespan 调用，代替 TASKS.recover()）。

    面板进程重启时：本来源还在跑的抢机任务按任务记录里的
    grab_spec 恢复重跑，其余本来源未结束任务按老规矩标为
    已中断；机器人端的任务绝不碰。已抢满台数的任务标记完成
    并补发成功通知；无规格或非本来源的抢机任务不恢复。
    """
    registry = registry or TASKS
    path = registry.tasks_file
    if not path:
        registry.recover()
        return
    records = task_store.load_records(path)
    resumable = []
    for record in records:
        if record.get("origin", "web") != "web":
            continue
        if record.get("kind") != "grab":
            continue
        if record.get("status") not in ("pending", "running"):
            continue
        spec = record.get("grab_spec")
        if not isinstance(spec, dict) or not spec.get("params"):
            continue
        resumable.append((record, spec))
    relaunch = []
    for record, spec in resumable:
        count = (spec["params"].get("count", 1) or 1)
        successes = [task_store.grab_success_from_dict(s)
                     for s in spec.get("successes", [])]
        if len(successes) >= count:
            _finish_resumed_grab(registry, record, spec, successes)
        else:
            relaunch.append((record, spec))
    task_store.recover_interrupted(
        path, "web", keep_ids=[r["id"] for r, _ in relaunch])
    for record, spec in relaunch:
        _relaunch_grab(registry, record, spec)


# ---------------- 引导卷恢复（停机换盘，原地恢复） ----------------

# Set of instance OCIDs currently being disk-swapped (only one swap task per instance at a time; the lock is independent from the bot process's lock, so swapping the same machine from both processes at once is the user's responsibility to avoid). 正在换盘的实例 OCID 集合（同一实例同时只允许一个换盘任务；与机器人进程的锁各自独立，跨进程同时换同一台需用户自行避免）。
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
        "old_volume_deleted": result.get("old_volume_deleted"),
    })
    old_tail = (result.get("old_boot_volume_id") or "")[-4:]
    new_tail = (result.get("new_boot_volume_id") or "")[-4:]
    old_note = ("已删除，额度已释放" if result.get("old_volume_deleted")
                else "已拆下，自动删除失败，请手动删除")
    registry.append_log(
        task_id,
        f"换盘成功：新盘 …{new_tail}，旧盘 …{old_tail}（{old_note}）。")
    registry.set_status(task_id, "done")
    clear_cloud_report_cache()
    clear_quota_cache()
    if notify:
        body = (
            f"✅ 实例「{instance_name}」引导卷恢复成功。\n"
            f"备份：{backup_name}\n"
            f"新启动盘：…{new_tail}\n"
            f"旧启动盘：…{old_tail}（{old_note}）\n"
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
        # id = the largest numeric suffix among existing plans + 1 (a deleted plan's id is never reused). id 按已有计划的最大数字后缀 +1（删过计划也不会重用 id）。
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


# Global grabber-plan registry (shared within one process; multi-process sync goes through the file). 全局抢机计划注册表（单进程内共享；多进程靠文件同步）。
PLANS = GrabberPlanRegistry()


# ---------------- 抢机计划调度器（APScheduler） ----------------

_scheduler = None
_scheduler_lock = threading.Lock()
# Prefix for plan job ids (sync only touches jobs with this prefix, never plans-refresh). 计划 job 的 id 前缀（同步时只动带此前缀的 job，不动 plans-refresh）。
_PLAN_JOB_PREFIX = "plan-"
# Id of the periodic sync job (re-reads plans from the file every minute and syncs the job list). 定时同步 job 的 id（每分钟从文件重读计划，同步 job 列表）。
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
        # The detail prefix carries the plan id, so the dedup check above can recognize a running task of the same plan. detail 前缀带计划 id，供上面的去重判断识别同一计划的在跑任务。
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
            # When the plan file is hand-edited into a broken state, skip that plan instead of crashing the scheduler. 计划文件被手工改坏时跳过该计划，不让调度器崩掉。
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
