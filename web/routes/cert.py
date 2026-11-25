#!/usr/bin/env python3
"""证书管理（阶段 5 工作包 E）：ACME 证书状态与续期。

  - GET /api/cert/status：读面板服务器的证书，返回面板形状
    {acmeEnabled, domain, issuer, notBefore, notAfter, remainingHours,
     status, source}；无证书时 status="none"，其余字段为 None。
    证书按 acme.sh → certbot → lego → nginx（ssl_certificate 指向的
    文件）的顺序找，source 标明找到的来源；续期只走 acme.sh。
  - POST /api/cert/renew：跑 acme.sh --renew 续期（body 须含 confirm=true），
    异步执行，返回 taskId；GET /api/cert/renew/{taskId} 查执行状态。

证书路径：优先读环境变量 MBOT_ACME_HOME（默认 ~/.acme.sh），域名
优先用查询参数 ?domain=，否则用环境变量 MBOT_CERT_DOMAIN，否则
自动取第一个找到的证书（各来源内按字典序）。
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
# acme.sh home directory (deployed as root; default /root/.acme.sh). acme.sh 家目录（部署时 root 运行，默认 /root/.acme.sh）。
ACME_HOME = os.environ.get("MBOT_ACME_HOME",
                           os.path.expanduser("~/.acme.sh"))
# Path to the acme.sh executable (default: acme.sh under ACME_HOME). acme.sh 可执行文件路径（默认 ACME_HOME 下的 acme.sh）。
ACME_SH = os.environ.get("MBOT_ACME_SH", os.path.join(ACME_HOME, "acme.sh"))
# Default domain (when unset, auto-detect the domain of the first certificate found). 默认域名（不设时自动检测第一个找到的证书的域名）。
CERT_DOMAIN = os.environ.get("MBOT_CERT_DOMAIN", "")
# certbot live directory (default /etc/letsencrypt/live). certbot live 目录（默认 /etc/letsencrypt/live）。
CERTBOT_LIVE_DIR = os.environ.get("MBOT_CERTBOT_LIVE_DIR",
                                  "/etc/letsencrypt/live")
# lego certificate directories (colon-separated when multiple; default: user dir first, then /etc/lego). lego 证书目录（多个时冒号分隔；默认先用户目录后 /etc/lego）。
LEGO_CERT_DIRS = [d for d in os.environ.get(
    "MBOT_LEGO_CERT_DIRS",
    os.path.join(os.path.expanduser("~"), ".lego", "certificates")
    + ":/etc/lego/certificates").split(":") if d]
# nginx config (used to find ssl_certificate; the panel's own vhost comes first). nginx 配置（找 ssl_certificate 用；面板自己的 vhost 放第一位）。
NGINX_CONF_GLOBS = [
    "/etc/nginx/conf.d/mbot-panel.conf",
    "/etc/nginx/conf.d/*.conf",
    "/etc/nginx/sites-enabled/*",
]
# Renewal command timeout (seconds): an acme.sh renewal includes DNS/HTTP validation and usually finishes within a minute. 续期命令超时（秒）：acme.sh 续期含 DNS/HTTP 验证，一般 1 分钟内完成。
RENEW_TIMEOUT_SECONDS = 300
# A certificate is "expiring soon" when less than this many hours remain (7 days). 剩余时间小于这么多小时即判为“即将过期”（7 天）。
EXPIRING_THRESHOLD_HOURS = 7 * 24
# Maximum characters of renewal output to keep. 续期输出保留的最大字符数。
RENEW_MAX_OUTPUT_CHARS = 10000
# =====================================================

# Domain format check (characters only, no DNS lookup; the acme.sh command is passed as a list, not through a shell, so the check is only a first line of defense). 域名格式校验（只校验字符，不查 DNS；acme.sh 命令用列表参数传，不经过 shell，校验只是第一道防线）。
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

    domain 指定时查 <domain>/<domain>.cer，ECC 证书再查
    <domain>_ecc/<domain>.cer；不指定时扫描 ACME_HOME 下 */*.cer，
    取字典序第一个（跳过 ca.cer——那是中间证书，不是叶子证书），
    域名去掉目录名末尾的 _ecc 后缀。
    """
    if domain:
        for sub in (domain, f"{domain}_ecc"):
            path = os.path.join(ACME_HOME, sub, f"{domain}.cer")
            if os.path.isfile(path):
                return domain, path
        return None, None
    matches = sorted(p for p in glob.glob(
        os.path.join(ACME_HOME, "*", "*.cer"))
        if os.path.basename(p) != "ca.cer")
    if not matches:
        return None, None
    path = matches[0]
    dirname = os.path.basename(os.path.dirname(path))
    if dirname.endswith("_ecc"):
        dirname = dirname[:-4]
    return dirname, path


def _find_certbot(domain=None):
    """certbot 证书：<live>/<domain>/fullchain.pem。

    domain 指定时只查该域名；不指定时扫描 live 下的 fullchain.pem，
    取字典序第一个（结果稳定）。
    """
    if domain:
        path = os.path.join(CERTBOT_LIVE_DIR, domain, "fullchain.pem")
        return (domain, path) if os.path.isfile(path) else (None, None)
    matches = sorted(glob.glob(os.path.join(CERTBOT_LIVE_DIR, "*",
                                            "fullchain.pem")))
    if not matches:
        return None, None
    path = matches[0]
    return os.path.basename(os.path.dirname(path)), path


def _find_lego(domain=None):
    """lego 证书：<lego>/certificates/<domain>.crt（多目录按序找）。

    domain 指定时只查该域名；不指定时各目录内取字典序第一个 .crt。
    """
    for cert_dir in LEGO_CERT_DIRS:
        if domain:
            path = os.path.join(cert_dir, f"{domain}.crt")
            if os.path.isfile(path):
                return domain, path
        else:
            matches = sorted(glob.glob(os.path.join(cert_dir, "*.crt")))
            if matches:
                path = matches[0]
                return os.path.basename(path)[:-4], path
    return None, None


_SSL_CERT_RE = re.compile(r"^\s*ssl_certificate\s+([^;\s]+)\s*;",
                          re.MULTILINE)


def _nginx_cert_paths():
    """nginx 配置里的 ssl_certificate 路径（按配置文件顺序，去重，
    只留真实存在的文件）。"""
    paths = []
    seen = set()
    for pattern in NGINX_CONF_GLOBS:
        for conf in sorted(glob.glob(pattern)):
            try:
                with open(conf, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError:
                continue
            for match in _SSL_CERT_RE.finditer(content):
                path = match.group(1)
                if path not in seen and os.path.isfile(path):
                    seen.add(path)
                    paths.append(path)
    return paths


def _cert_common_name(cert_path):
    """证书的 CN（nginx 来源推导域名用）；取不到返回 None。"""
    if _CRYPTOGRAPHY_AVAILABLE:
        try:
            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read())
            attrs = cert.subject.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME)
            if attrs:
                return attrs[0].value
        except Exception:  # noqa: BLE001 - 解析失败时走 openssl 兜底
            pass
    try:
        result = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-noout", "-subject",
             "-nameopt", "RFC2253"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"(?:^|,)CN=([^,]+)", result.stdout)
    return match.group(1) if match else None


def _domain_from_cert_path(path):
    """从证书路径推导域名：certbot live 目录名 / lego 文件名；
    推导不出时读证书 CN；都取不到返回 None。"""
    parts = path.split(os.sep)
    if "live" in parts:
        idx = parts.index("live")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    base = os.path.basename(path)
    if base.endswith(".crt"):
        return base[:-4]
    return _cert_common_name(path)


def _find_nginx_cert(domain=None):
    """nginx 实际在用的证书（ssl_certificate 指向的文件）。

    domain 指定时只接受推导域名一致的；不指定时取第一个。
    """
    for path in _nginx_cert_paths():
        found = _domain_from_cert_path(path)
        if domain and found != domain:
            continue
        return found, path
    return None, None


def find_cert_file_any(domain=None):
    """找证书文件（多来源），返回 (域名, 证书路径, 来源)。

    来源按序：acme.sh → certbot → lego → nginx（实际在用的证书）。
    找不到返回 (None, None, None)。续期只走 acme.sh，不用这个函数。
    """
    found_domain, path = find_cert_file(domain)
    if path is not None:
        return found_domain, path, "acme.sh"
    found_domain, path = _find_certbot(domain)
    if path is not None:
        return found_domain, path, "certbot"
    found_domain, path = _find_lego(domain)
    if path is not None:
        return found_domain, path, "lego"
    found_domain, path = _find_nginx_cert(domain)
    if path is not None:
        return found_domain, path, "nginx"
    return None, None, None


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
    """返回证书状态（面板形状）。

    status：none（无证书）/ valid（有效）/ expiring（7 天内过期）/
    expired（已过期）。remainingHours 为负数表示已过期这么多小时。
    source：证书来源（acme.sh / certbot / lego / nginx，无证书时 None）。
    """
    acme_enabled = _is_acme_enabled()
    domain = domain or CERT_DOMAIN or None
    found_domain, cert_path, source = find_cert_file_any(domain)
    if cert_path is None:
        return {"acmeEnabled": acme_enabled, "domain": found_domain,
                "issuer": None, "notBefore": None, "notAfter": None,
                "remainingHours": None, "status": "none",
                "source": None}
    parsed = parse_cert(cert_path)
    if parsed is None:
        return {"acmeEnabled": acme_enabled, "domain": found_domain,
                "issuer": None, "notBefore": None, "notAfter": None,
                "remainingHours": None, "status": "none",
                "source": source}
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
            "remainingHours": remaining_hours, "status": status,
            "source": source}


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
            "status": "running",
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


# Global singleton (shared within a single process). 全局单例（单进程内共享）。
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
    """返回 ACME 证书状态（面板形状；无证书时 status="none"）。"""
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
