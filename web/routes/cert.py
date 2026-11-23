#!/usr/bin/env python3
"""证书管理（阶段 5 工作包 E）：ACME 证书状态与续期。

  - GET /api/cert/status：读面板服务器的 ACME 证书，返回 R-Bot 形状
    {acmeEnabled, domain, issuer, notBefore, notAfter, remainingHours,
     status}；无证书时 status="none"，其余字段为 None。
  - POST /api/cert/renew：跑 acme.sh --renew 续期（body 须含 confirm=true），
    异步执行，返回 taskId；GET /api/cert/renew/{taskId} 查执行状态。

证书路径：优先读环境变量 RBOT_ACME_HOME（默认 ~/.acme.sh），域名
优先用查询参数 ?domain=，否则用环境变量 RBOT_CERT_DOMAIN，否则
自动取 acme.sh 目录下第一个有 .cer 的域名。
证书解析优先用 cryptography（已装时），否则调 openssl 命令行兜底。
"""

import glob
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from web.deps import error_response, get_current_user

router = APIRouter()

# ================= 配置区（改这里） =================
# acme.sh 家目录（部署时 root 运行，默认 /root/.acme.sh）。
ACME_HOME = os.environ.get("RBOT_ACME_HOME",
                           os.path.expanduser("~/.acme.sh"))
# acme.sh 可执行文件路径（默认 ACME_HOME 下的 acme.sh）。
ACME_SH = os.environ.get("RBOT_ACME_SH", os.path.join(ACME_HOME, "acme.sh"))
# 默认域名（不设时自动检测 acme.sh 目录下第一个有证书的域名）。
CERT_DOMAIN = os.environ.get("RBOT_CERT_DOMAIN", "")
# 续期命令超时（秒）：acme.sh 续期含 DNS/HTTP 验证，一般 1 分钟内完成。
RENEW_TIMEOUT_SECONDS = 300
# 剩余时间小于这么多小时即判为“即将过期”（7 天）。
EXPIRING_THRESHOLD_HOURS = 7 * 24
# 续期输出保留的最大字符数。
RENEW_MAX_OUTPUT_CHARS = 10000
# =====================================================

# 域名格式校验（只校验字符，不查 DNS；acme.sh 命令用列表参数传，
# 不经过 shell，校验只是第一道防线）。
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")

try:
    from cryptography import x509
    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - cryptography 未安装时的降级分支
    _CRYPTOGRAPHY_AVAILABLE = False


def _is_acme_enabled():
    """acme.sh 是否可用（可执行文件存在）。"""
    return os.path.isfile(ACME_SH)


def find_cert_file(domain=None):
    """找证书文件，返回 (域名, 证书路径)；找不到返回 (None, None)。

    domain 指定时只查该域名的 <domain>.cer；不指定时扫描 ACME_HOME
    下 */*.cer，取字典序第一个（结果稳定）。
    """
    if domain:
        path = os.path.join(ACME_HOME, domain, f"{domain}.cer")
        return (domain, path) if os.path.isfile(path) else (None, None)
    matches = sorted(glob.glob(os.path.join(ACME_HOME, "*", "*.cer")))
    if not matches:
        return None, None
    path = matches[0]
    return os.path.basename(os.path.dirname(path)), path


def _parse_with_cryptography(cert_path):
    """用 cryptography 解析证书，返回 {issuer, notBefore, notAfter}
    （时间为 UTC 的 datetime）；解析失败返回 None。"""
    with open(cert_path, "rb") as f:
        data = f.read()
    cert = x509.load_pem_x509_certificate(data)
    issuer = cert.issuer.rfc4514_string()
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    return {"issuer": issuer, "notBefore": not_before,
            "notAfter": not_after}


def _parse_with_openssl(cert_path):
    """用 openssl 命令行解析证书（cryptography 不可用时的兜底）。

    解析 `openssl x509 -noout -dates -issuer` 的输出：
      notBefore=Nov 23 00:00:00 2026 GMT
      notAfter=Feb 21 00:00:00 2027 GMT
      issuer=CN = ...
    失败返回 None。
    """
    result = subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-dates", "-issuer"],
        capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    try:
        not_before = datetime.strptime(values["notBefore"],
                                       "%b %d %H:%M:%S %Y %Z")
        not_after = datetime.strptime(values["notAfter"],
                                      "%b %d %H:%M:%S %Y %Z")
    except (KeyError, ValueError):
        return None
    not_before = not_before.replace(tzinfo=timezone.utc)
    not_after = not_after.replace(tzinfo=timezone.utc)
    return {"issuer": values.get("issuer", ""),
            "notBefore": not_before, "notAfter": not_after}


def parse_cert(cert_path):
    """解析证书文件，返回 {issuer, notBefore, notAfter}；
    失败返回 None。优先 cryptography，失败则 openssl 兜底。"""
    if _CRYPTOGRAPHY_AVAILABLE:
        try:
            return _parse_with_cryptography(cert_path)
        except Exception:  # noqa: BLE001 - 解析失败时走 openssl 兜底
            pass
    try:
        return _parse_with_openssl(cert_path)
    except (OSError, subprocess.SubprocessError):
        return None


def _iso(dt):
    """datetime 转 ISO 8601 UTC 字符串（如 2026-11-23T00:00:00Z）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_cert_status(domain=None):
    """返回证书状态（R-Bot 形状）。

    status：none（无证书）/ valid（有效）/ expiring（7 天内过期）/
    expired（已过期）。remainingHours 为负数表示已过期这么多小时。
    """
    acme_enabled = _is_acme_enabled()
    domain = domain or CERT_DOMAIN or None
    found_domain, cert_path = find_cert_file(domain)
    if cert_path is None:
        return {"acmeEnabled": acme_enabled, "domain": found_domain,
                "issuer": None, "notBefore": None, "notAfter": None,
                "remainingHours": None, "status": "none"}
    parsed = parse_cert(cert_path)
    if parsed is None:
        return {"acmeEnabled": acme_enabled, "domain": found_domain,
                "issuer": None, "notBefore": None, "notAfter": None,
                "remainingHours": None, "status": "none"}
    now = datetime.now(timezone.utc)
    remaining = parsed["notAfter"] - now
    remaining_hours = int(remaining.total_seconds() // 3600)
    if remaining_hours < 0:
        status = "expired"
    elif remaining_hours < EXPIRING_THRESHOLD_HOURS:
        status = "expiring"
    else:
        status = "valid"
    return {"acmeEnabled": acme_enabled, "domain": found_domain,
            "issuer": parsed["issuer"],
            "notBefore": _iso(parsed["notBefore"]),
            "notAfter": _iso(parsed["notAfter"]),
            "remainingHours": remaining_hours, "status": status}


# ---------------- 续期任务（异步） ----------------

class RenewRegistry:
    """续期任务注册表：内存字典 + 线程锁。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._tasks = {}

    def create(self, domain):
        """新建续期任务，返回任务字典。"""
        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "domain": domain,
            "status": "running",  # running / done / failed
            "output": "",
            "error": None,
            "startedAt": time.time(),
            "finishedAt": None,
        }
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, task_id):
        """按 id 取任务；不存在返回 None。"""
        with self._lock:
            return self._tasks.get(task_id)

    def reset(self):
        """清空全部任务（测试用）。"""
        with self._lock:
            self._tasks.clear()


# 全局单例（单进程内共享）。
RENEW_TASKS = RenewRegistry()


def _run_renew(task):
    """续期任务的后台执行（daemon 线程里跑）。

    跑 `acme.sh --renew -d <域名>`（列表参数，不经过 shell），
    超时或失败时记 error；输出截断后存 output。
    """
    try:
        result = subprocess.run(
            [ACME_SH, "--renew", "-d", task["domain"]],
            capture_output=True, text=True,
            timeout=RENEW_TIMEOUT_SECONDS)
        output = (result.stdout or "") + (result.stderr or "")
        with RENEW_TASKS._lock:
            task["output"] = output[:RENEW_MAX_OUTPUT_CHARS]
            task["finishedAt"] = time.time()
            if result.returncode == 0:
                task["status"] = "done"
            else:
                task["status"] = "failed"
                task["error"] = f"acme.sh 续期失败（退出码 {result.returncode}）"
    except subprocess.TimeoutExpired:
        with RENEW_TASKS._lock:
            task["status"] = "failed"
            task["error"] = f"续期超时（超过 {RENEW_TIMEOUT_SECONDS} 秒）"
            task["finishedAt"] = time.time()
    except OSError as exc:
        with RENEW_TASKS._lock:
            task["status"] = "failed"
            task["error"] = f"无法执行 acme.sh：{exc}"
            task["finishedAt"] = time.time()


class RenewBody(BaseModel):
    """续期请求体：domain 不传时用当前证书域名；必须 confirm=true。"""

    domain: str = None
    confirm: bool = False


@router.get("/api/cert/status")
def cert_status(domain: str = Query(default=None),
                username: str = Depends(get_current_user)):
    """返回 ACME 证书状态（R-Bot 形状；无证书时 status="none"）。"""
    if domain and not _DOMAIN_RE.match(domain):
        return error_response(f"域名格式非法：{domain}", status_code=400)
    return {"ok": True, **get_cert_status(domain)}


@router.post("/api/cert/renew", status_code=202)
def cert_renew(body: RenewBody, username: str = Depends(get_current_user)):
    """续期证书：异步跑 acme.sh --renew，返回 taskId 供查询状态。"""
    if not body.confirm:
        return error_response("续期证书需要确认：请求体须含 confirm=true",
                              status_code=400)
    domain = body.domain or CERT_DOMAIN or None
    found_domain, _cert_path = find_cert_file(domain)
    if found_domain is None:
        return error_response("没有可续期的证书（请先确认域名与证书路径）",
                              status_code=404)
    if not _DOMAIN_RE.match(found_domain):
        return error_response(f"域名格式非法：{found_domain}", status_code=400)
    if not _is_acme_enabled():
        return error_response("acme.sh 不可用（未找到可执行文件）",
                              status_code=503)
    task = RENEW_TASKS.create(found_domain)
    thread = threading.Thread(target=_run_renew, args=(task,), daemon=True)
    thread.start()
    return {"ok": True, "taskId": task["id"], "domain": found_domain,
            "status": "running"}


@router.get("/api/cert/renew/{task_id}")
def cert_renew_status(task_id: str,
                      username: str = Depends(get_current_user)):
    """查续期任务状态（running / done / failed，含输出与错误）。"""
    task = RENEW_TASKS.get(task_id)
    if task is None:
        return error_response(f"续期任务不存在：{task_id}", status_code=404)
    return {"ok": True, "task": task}
