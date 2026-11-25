#!/usr/bin/env python3
"""巡检计划（阶段 5 工作包 E）。

  - GET /api/inspect-schedules：全部巡检计划。
  - POST /api/inspect-schedules：新增计划 {cron, targets, enabled?}。
  - DELETE /api/inspect-schedules/{id}：删除计划（前端删除前二次确认）。
  - POST /api/inspect-schedules/{id}/run：立即执行一次巡检（异步）。
  - GET /api/inspect-reports：最近的巡检报告列表（最新 20 份）。

计划存在 DATA_DIR/inspect_schedules.json，每条含 id、cron、targets、
enabled、created_at；targets 是实例 OCID 列表，或 ["all"] 表示全部实例。
巡检 = 查实例状态 + 磁盘 + 配额，报告存
DATA_DIR/inspect_reports/{计划id}-{时间戳}.json，摘要经 Telegram
通知白名单用户（复用 web.telegram_notify）。

定时触发用 APScheduler（与 web/tasks.py 的抢机计划调度器同款）：
面板启动时默认不启动调度器，设环境变量 MBOT_ENABLE_INSPECT_SCHEDULER=1
才启动（见 web/app.py 的 lifespan）。
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from oci.client import OCIError
from oci.instance import list_instances
from oci.quota import get_free_tier_summary
from oci.volume import list_boot_volumes
from web import config
from web.deps import error_response, get_current_user, resolve_profile
from web.tasks import validate_cron
from web.telegram_notify import send_to_allowed_users

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    BackgroundScheduler = None
    CronTrigger = None

router = APIRouter()

# ================= 配置区（改这里） =================
# Inspection scheduler switch: set MBOT_ENABLE_INSPECT_SCHEDULER=1 to start APScheduler when the panel boots. 巡检调度器开关：设 MBOT_ENABLE_INSPECT_SCHEDULER=1 才在面板启动时启动 APScheduler。
# Off by default (tests and deployments that do not need scheduled inspections are unaffected). 默认关闭（测试与不需要定时巡检的部署不受影响）。
ENABLE_INSPECT_SCHEDULER = os.environ.get(
    "MBOT_ENABLE_INSPECT_SCHEDULER", "") == "1"
# The scheduler re-syncs plans from file every this many seconds. 调度器每隔这么多秒重新从文件同步一次计划。
INSPECT_SYNC_INTERVAL_SECONDS = 60
# Max reports returned in the list (newest first). 报告列表最多返回这么多份（按生成时间倒序）。
INSPECT_REPORTS_LIMIT = 20
# =====================================================

# Report filename format: {plan-id}-{timestamp}.json (timestamp is epoch seconds). 报告文件名格式：{计划id}-{时间戳}.json（时间戳为 epoch 秒）。
_REPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+-\d+\.json$")
# Inspection target: instance OCID prefix ("all" means all instances). 巡检目标：实例 OCID 前缀（"all" 表示全部实例）。
_INSTANCE_OCID_PREFIX = "ocid1.instance.oc1."


def _schedules_file():
    """inspect_schedules.json 路径（每次都从 config 读 DATA_DIR）。"""
    return os.path.join(config.DATA_DIR, "inspect_schedules.json")


def _reports_dir():
    """巡检报告目录。"""
    return os.path.join(config.DATA_DIR, "inspect_reports")


class InspectScheduleRegistry:
    """巡检计划注册表：JSON 文件持久化（与抢机计划 GrabberPlanRegistry
    同款，id 按 insp + 数字递增）。"""

    def _load(self):
        try:
            with open(_schedules_file(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, schedules):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_schedules_file(), "w", encoding="utf-8") as f:
            json.dump(schedules, f, ensure_ascii=False, indent=2)

    def list(self):
        """全部计划（按添加顺序）。"""
        return self._load()

    def get(self, schedule_id):
        """按 id 取计划；不存在返回 None。"""
        for schedule in self._load():
            if schedule.get("id") == schedule_id:
                return schedule
        return None

    def add(self, cron, targets, enabled=True):
        """新增一条计划，返回完整计划字典。

        cron 非法、targets 为空或含非法值时抛 ValueError。
        targets 只能是 ["all"] 或实例 OCID 列表。
        """
        cron = validate_cron(cron)
        if not targets or not isinstance(targets, list):
            raise ValueError("targets 不能为空（实例 OCID 列表，或 [\"all\"]）")
        for target in targets:
            if target == "all":
                continue
            if not isinstance(target, str) or not target.startswith(
                    _INSTANCE_OCID_PREFIX):
                raise ValueError(
                    f"targets 里的值必须是实例 OCID 或 \"all\"，收到 {target!r}")
        schedules = self._load()
        max_num = 0
        for schedule in schedules:
            digits = "".join(ch for ch in str(schedule.get("id", ""))
                             if ch.isdigit())
            if digits:
                max_num = max(max_num, int(digits))
        schedule = {
            "id": f"insp{max_num + 1}",
            "cron": cron,
            "targets": list(targets),
            "enabled": bool(enabled),
            "created_at": time.time(),
        }
        schedules.append(schedule)
        self._save(schedules)
        return schedule

    def delete(self, schedule_id):
        """删除计划；不存在返回 False。"""
        schedules = self._load()
        kept = [s for s in schedules if s.get("id") != schedule_id]
        if len(kept) == len(schedules):
            return False
        self._save(kept)
        return True

    def set_enabled(self, schedule_id, enabled):
        """开关计划；不存在返回 None，否则返回更新后的计划字典。"""
        schedules = self._load()
        for schedule in schedules:
            if schedule.get("id") == schedule_id:
                schedule["enabled"] = bool(enabled)
                self._save(schedules)
                return schedule
        return None

    def reset(self):
        """清空全部计划（测试用）。"""
        self._save([])


# Global inspection-plan registry (shared within a single process; multi-process syncs via files). 全局巡检计划注册表（单进程内共享；多进程靠文件同步）。
SCHEDULES = InspectScheduleRegistry()


# ---------------- 巡检执行 ----------------

def run_inspection(schedule, notify=True):
    """执行一次巡检，返回报告字典并写盘。

    巡检内容（默认 profile）：
      - 目标实例的状态（targets 为 ["all"] 时查全部实例）；
      - 启动盘总数与总 GB；
      - 免费额度汇总（A1/E2/启动盘是否超限）。
    单项查询失败时记进 errors，不让整份报告失败；ok 为 True 当且仅当
    无 errors 且额度未超限。notify=True 时经 Telegram 发摘要给白名单用户。
    """
    schedule_id = schedule.get("id")
    targets = schedule.get("targets") or ["all"]
    started = time.time()
    report = {
        "scheduleId": schedule_id,
        "cron": schedule.get("cron"),
        "startedAt": datetime.fromtimestamp(started, tz=timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedAtEpochSec": int(started),
        "targets": targets,
        "instances": [],
        "volumes": {"count": 0, "totalGB": 0},
        "quota": None,
        "errors": [],
    }
    try:
        cli, compartment_id = resolve_profile(None)
        instances = list_instances(cli, compartment_id)
        wanted = None if "all" in targets else set(targets)
        seen = set()
        for inst in instances:
            if wanted is not None and inst.get("id") not in wanted:
                continue
            seen.add(inst.get("id"))
            report["instances"].append({
                "id": inst.get("id"),
                "name": inst.get("display-name"),
                "state": inst.get("lifecycle-state"),
            })
        if wanted is not None:
            for missing in sorted(wanted - seen):
                report["errors"].append(f"实例不存在或无权查看：{missing}")
        volumes = list_boot_volumes(cli, compartment_id)
        live = [v for v in volumes
                if v.get("lifecycle-state") not in ("TERMINATING", "TERMINATED")]
        report["volumes"] = {
            "count": len(live),
            "totalGB": sum(v.get("size-in-gbs") or 0 for v in live),
        }
        summary = get_free_tier_summary(cli, compartment_id)
        report["quota"] = {
            "a1": {"usedOcpu": summary["a1"]["ocpu_used"],
                   "capOcpu": summary["a1"]["ocpu_limit"],
                   "usedMem": summary["a1"]["memory_used_gb"],
                   "capMem": summary["a1"]["memory_limit_gb"]},
            "e2": {"count": summary["e2"]["count"],
                   "limit": summary["e2"]["limit"]},
            "bootVolume": {"usedGB": summary["boot_volume"]["used_gb"],
                           "limitGB": summary["boot_volume"]["limit_gb"]},
            "overallOk": summary["overall_ok"],
        }
        if not summary["overall_ok"]:
            report["errors"].append("免费额度已超限，请检查配额页")
    except (OCIError, ValueError) as exc:
        report["errors"].append(f"巡检查询失败：{exc}")
    report["finishedAt"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    report["ok"] = not report["errors"]

    # Write to disk (silently skipped on failure; does not affect the return). 写盘（失败静默跳过，不影响返回）。
    try:
        os.makedirs(_reports_dir(), exist_ok=True)
        name = f"{schedule_id}-{int(started)}.json"
        with open(os.path.join(_reports_dir(), name), "w",
                  encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["reportFile"] = name
    except OSError:
        pass

    if notify:
        _notify_inspection(report)
    return report


def _notify_inspection(report):
    """经 Telegram 发巡检摘要（失败静默，不影响巡检本身）。"""
    instances = report.get("instances") or []
    running = sum(1 for i in instances if i.get("state") == "RUNNING")
    stopped = sum(1 for i in instances if i.get("state") == "STOPPED")
    volumes = report.get("volumes") or {}
    status = "✅ 正常" if report.get("ok") else "⚠️ 有问题"
    lines = [
        f"【巡检 {report.get('scheduleId')}】{status}",
        f"实例：{len(instances)} 台（RUNNING {running}，STOPPED {stopped}）",
        f"启动盘：{volumes.get('count', 0)} 块，"
        f"{volumes.get('totalGB', 0)}GB",
    ]
    for error in (report.get("errors") or [])[:5]:
        lines.append(f"• {error}")
    try:
        send_to_allowed_users("\n".join(lines))
    except Exception:  # noqa: BLE001 - 通知失败不影响巡检
        pass


def list_reports(limit=INSPECT_REPORTS_LIMIT):
    """最近的巡检报告（按生成时间倒序，最多 limit 份）。

    只读文件名符合 {计划id}-{时间戳}.json 的文件，内容损坏的跳过。
    """
    reports = []
    try:
        names = os.listdir(_reports_dir())
    except OSError:
        return []
    for name in sorted(names, reverse=True):
        if not _REPORT_NAME_RE.match(name):
            continue
        try:
            with open(os.path.join(_reports_dir(), name),
                      encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(report, dict):
            report.setdefault("reportFile", name)
            reports.append(report)
        if len(reports) >= limit:
            break
    return reports


# ---------------- 巡检调度器（APScheduler） ----------------

_scheduler = None
_scheduler_lock = threading.Lock()
_INSPECT_JOB_PREFIX = "inspect-"
_INSPECT_REFRESH_JOB_ID = "inspect-refresh"


def _inspect_job_id(schedule_id):
    """某计划在调度器里的 job id。"""
    return f"{_INSPECT_JOB_PREFIX}{schedule_id}"


def _run_inspect_job(schedule_id):
    """计划触发时的回调：查计划还在且启用才执行巡检。"""
    schedule = SCHEDULES.get(schedule_id)
    if schedule is None or not schedule.get("enabled"):
        return
    try:
        run_inspection(schedule)
    except Exception as exc:  # noqa: BLE001 - 调度器线程里异常不能外泄
        print(f"巡检计划 {schedule_id} 触发失败：{exc}")


def _sync_inspect_jobs_locked():
    """按当前计划文件同步调度器里的巡检 job（调用方需持有锁）。"""
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith(_INSPECT_JOB_PREFIX):
            _scheduler.remove_job(job.id)
    for schedule in SCHEDULES.list():
        if not schedule.get("enabled"):
            continue
        try:
            trigger = CronTrigger.from_crontab(schedule["cron"])
        except (ValueError, TypeError, KeyError):
            print(f"巡检计划 {schedule.get('id')} 的 cron 无效，已跳过："
                  f"{schedule.get('cron')!r}")
            continue
        _scheduler.add_job(_run_inspect_job, trigger=trigger,
                           args=[schedule["id"]],
                           id=_inspect_job_id(schedule["id"]),
                           replace_existing=True)


def refresh_inspect_scheduler():
    """按当前计划文件重新同步调度器里的 job（增/删计划后调用）。"""
    with _scheduler_lock:
        _sync_inspect_jobs_locked()


def start_inspect_scheduler():
    """启动巡检计划调度器（幂等，重复调用返回同一个调度器）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        if BackgroundScheduler is None or CronTrigger is None:
            print("警告：APScheduler 未安装，巡检调度器无法启动，"
                  "请先 pip install apscheduler。")
            return None
        _scheduler = BackgroundScheduler()
        _sync_inspect_jobs_locked()
        _scheduler.add_job(
            refresh_inspect_scheduler, "interval",
            seconds=INSPECT_SYNC_INTERVAL_SECONDS,
            id=_INSPECT_REFRESH_JOB_ID, replace_existing=True)
        _scheduler.start()
        return _scheduler


def stop_inspect_scheduler():
    """停止巡检计划调度器（面板关闭与测试用）。"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.shutdown(wait=False)
            _scheduler = None


# ---------------- 路由 ----------------

class InspectScheduleBody(BaseModel):
    """新增巡检计划请求体。"""

    cron: str
    targets: list = []
    enabled: bool = True


@router.get("/api/inspect-schedules")
def list_all(username: str = Depends(get_current_user)):
    """全部巡检计划。"""
    return {"ok": True, "schedules": SCHEDULES.list()}


@router.post("/api/inspect-schedules", status_code=201)
def create(body: InspectScheduleBody,
           username: str = Depends(get_current_user)):
    """新增巡检计划（cron 非法或 targets 非法时 400）。"""
    try:
        schedule = SCHEDULES.add(body.cron, body.targets, body.enabled)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    refresh_inspect_scheduler()
    return {"ok": True, "schedule": schedule}


@router.delete("/api/inspect-schedules/{schedule_id}")
def delete(schedule_id: str, username: str = Depends(get_current_user)):
    """删除巡检计划（前端删除前二次确认）。"""
    if not SCHEDULES.delete(schedule_id):
        return error_response(f"巡检计划不存在：{schedule_id}",
                              status_code=404)
    refresh_inspect_scheduler()
    return {"ok": True, "id": schedule_id}


@router.post("/api/inspect-schedules/{schedule_id}/run", status_code=202)
def run_now(schedule_id: str, username: str = Depends(get_current_user)):
    """立即执行一次巡检（异步跑，报告稍后在 /api/inspect-reports 里看）。"""
    schedule = SCHEDULES.get(schedule_id)
    if schedule is None:
        return error_response(f"巡检计划不存在：{schedule_id}",
                              status_code=404)
    thread = threading.Thread(target=run_inspection, args=(schedule,),
                              daemon=True)
    thread.start()
    return {"ok": True, "id": schedule_id, "status": "running"}


@router.get("/api/inspect-reports")
def reports(username: str = Depends(get_current_user)):
    """最近的巡检报告列表（最新 20 份，按生成时间倒序）。"""
    return {"ok": True, "reports": list_reports()}


