#!/usr/bin/env python3
"""监控增强的纯逻辑与配置持久化（阶段 5 工作包 C）。

三个功能：
  1. 流量守护：本月出站流量超阈值时仅通知或自动关机，
     同一阈值触发后 24 小时内不再重复执行。
  2. 停机保活：STOPPED 实例按每实例开关自动开机，
     连续 3 次开机失败后停止尝试该实例。
  3. SSL 监控：域名证书到期前 30/14/7/1 天各提醒一次。

本模块是纯同步逻辑（JSON 文件读写 + 阈值判断），不调 OCI API、
不发 Telegram 消息，单元测试直接跑，不需要 mock 网络。
Telegram handler 与 JobQueue job 在 tgbot/handlers_extra.py 里，
调本模块的函数做判断与持久化。

配置文件（都在 tgbot.config.DATA_DIR 下，JSON）：
  - traffic_guard.json：{"enabled", "threshold_gb", "action",
    "last_triggered", "chat_id"}
  - keepalive.json：{"<实例 OCID>": true/false}（每实例独立开关）
  - keepalive_state.json：{"chat_id", "failures": {OCID: 次数},
    "gave_up": [OCID, ...]}（失败计数与停试名单，内部状态）
  - ssl_watch.json：{"domains": [...],
    "reminded": {域名: [已提醒过的阈值]}, "chat_id"}

安全说明：
  - 配置文件里只有开关、阈值与 OCID，不含任何密钥 / Token。
  - 自动关机 / 自动开机默认关闭，用户明确开启才生效（见默认值）。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

from tgbot import config as bot_config

# ================= 配置区（改这里） =================
# 流量守护默认阈值（GB）：OCI 免费层每月出站流量额度 10TB。
TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB = 10240
# 流量守护阈值预设（GB），Telegram 菜单里点选。
TRAFFIC_GUARD_THRESHOLD_PRESETS = [1024, 5120, 10240, 20480]
# 流量守护触发后冷却时间（小时）：同一阈值触发后这么久内不再重复执行
# （防反复开关机；仅通知动作同样冷却，防每 10 分钟刷屏）。
TRAFFIC_GUARD_COOLDOWN_HOURS = 24
# 停机保活：连续开机失败这么多次后停止尝试该实例并通知用户。
KEEPALIVE_MAX_FAILURES = 3
# SSL 提醒阈值（天）：到期前这些天数时各提醒一次。
SSL_REMIND_DAYS = (30, 14, 7, 1)
# 配置文件名（存在 DATA_DIR 下）。
TRAFFIC_GUARD_FILE = "traffic_guard.json"
KEEPALIVE_FILE = "keepalive.json"
KEEPALIVE_STATE_FILE = "keepalive_state.json"
SSL_WATCH_FILE = "ssl_watch.json"
# =====================================================

# 域名格式校验：小写字母 / 数字 / 连字符，至少一个点，单标签不接受。
# （只校验格式，不查 DNS；国际化域名请用 punycode 形式，如 xn-- 开头。）
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


# ---------------- JSON 文件读写 ----------------

def _config_path(filename):
    """配置文件的完整路径（每次都从 bot_config 读 DATA_DIR，方便测试替换）。"""
    return os.path.join(bot_config.DATA_DIR, filename)


def _load_json(path, default):
    """读 JSON 文件；不存在 / 损坏 / 不是 dict 时返回 default。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, dict) else default


def _save_json(path, data):
    """写 JSON 文件（不存在目录时自动创建）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _parse_time(value):
    """解析 ISO 时间字符串；解析失败返回 None（无时区时按 UTC 计）。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------- 流量守护 ----------------

def default_traffic_guard():
    """流量守护默认配置：关闭、10240GB、仅通知。"""
    return {
        "enabled": False,
        "threshold_gb": TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB,
        "action": "notify",
        "last_triggered": None,
        "chat_id": None,
    }


def load_traffic_guard(path=None):
    """读流量守护配置（文件缺失 / 字段非法时用默认值补齐）。"""
    cfg = default_traffic_guard()
    data = _load_json(path or _config_path(TRAFFIC_GUARD_FILE), {})
    cfg["enabled"] = bool(data.get("enabled", False))
    try:
        threshold = float(data.get("threshold_gb",
                                   TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB))
    except (TypeError, ValueError):
        threshold = TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB
    if threshold <= 0:
        threshold = TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB
    if float(threshold).is_integer():
        threshold = int(threshold)
    cfg["threshold_gb"] = threshold
    cfg["action"] = (data.get("action") if data.get("action")
                     in ("notify", "shutdown") else "notify")
    cfg["last_triggered"] = data.get("last_triggered")
    cfg["chat_id"] = data.get("chat_id")
    return cfg


def save_traffic_guard(cfg, path=None):
    """保存流量守护配置（归一化后写盘），返回写盘的字典。"""
    data = {
        "enabled": bool(cfg.get("enabled", False)),
        "threshold_gb": cfg.get("threshold_gb",
                                TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB),
        "action": (cfg.get("action") if cfg.get("action")
                   in ("notify", "shutdown") else "notify"),
        "last_triggered": cfg.get("last_triggered"),
        "chat_id": cfg.get("chat_id"),
    }
    _save_json(path or _config_path(TRAFFIC_GUARD_FILE), data)
    return data


def should_trigger_traffic_guard(cfg, used_gb, now=None):
    """是否触发流量守护：已开启 且 用量 >= 阈值 且 距上次触发已过 24 小时。"""
    if not cfg.get("enabled"):
        return False
    threshold = cfg.get("threshold_gb") or 0
    if used_gb < threshold:
        return False
    now = now or datetime.now(timezone.utc)
    last = _parse_time(cfg.get("last_triggered"))
    if last is not None and (now - last) < timedelta(
            hours=TRAFFIC_GUARD_COOLDOWN_HOURS):
        return False
    return True


def record_traffic_guard_trigger(cfg, now=None, path=None):
    """记录本次触发时间并保存配置（触发动作执行后调用）。"""
    now = now or datetime.now(timezone.utc)
    cfg["last_triggered"] = now.isoformat()
    save_traffic_guard(cfg, path=path)
    return cfg


def current_month_transfer_gb(items, now=None):
    """汇总用量 API 返回的 items 里本月的出站流量（GB）。

    items 是 handlers_extra.fetch_usage_summary 的返回值；只统计
    service 含 "Data Transfer" 且 time-usage-started 属于本月
    （YYYY-MM）的项，computed-quantity 按 API 返回值以 GB 计。
    """
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    total = 0.0
    for item in items or []:
        if "Data Transfer" not in (item.get("service") or ""):
            continue
        if not (item.get("time-usage-started") or "").startswith(month):
            continue
        try:
            total += float(item.get("computed-quantity") or 0)
        except (TypeError, ValueError):
            continue
    return total


# ---------------- 停机保活 ----------------

def load_keepalive(path=None):
    """读停机保活配置：{实例 OCID: true/false}，文件缺失时返回 {}。"""
    data = _load_json(path or _config_path(KEEPALIVE_FILE), {})
    return {str(k): bool(v) for k, v in data.items()}


def save_keepalive(cfg, path=None):
    """保存停机保活配置，返回写盘的字典。"""
    data = {str(k): bool(v) for k, v in (cfg or {}).items()}
    _save_json(path or _config_path(KEEPALIVE_FILE), data)
    return data


def load_keepalive_state(path=None):
    """读停机保活内部状态：{"chat_id", "failures", "gave_up"}。"""
    data = _load_json(path or _config_path(KEEPALIVE_STATE_FILE), {})
    failures = {}
    for key, value in (data.get("failures") or {}).items():
        try:
            failures[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return {
        "chat_id": data.get("chat_id"),
        "failures": failures,
        "gave_up": [str(x) for x in (data.get("gave_up") or [])],
    }


def save_keepalive_state(state, path=None):
    """保存停机保活内部状态，返回写盘的字典。"""
    data = {
        "chat_id": state.get("chat_id"),
        "failures": {str(k): int(v)
                     for k, v in (state.get("failures") or {}).items()},
        "gave_up": [str(x) for x in (state.get("gave_up") or [])],
    }
    _save_json(path or _config_path(KEEPALIVE_STATE_FILE), data)
    return data


def keepalive_enabled_instances(cfg):
    """返回开启了保活的实例 OCID 集合。"""
    return {ocid for ocid, on in (cfg or {}).items() if on}


def keepalive_record_failure(state, instance_id):
    """记录一次开机失败，返回 (失败次数, 是否达到停试上限)。

    达到上限时把实例加入 gave_up（调用方负责保存状态 + 通知用户）。
    """
    failures = state.setdefault("failures", {})
    try:
        count = int(failures.get(instance_id, 0)) + 1
    except (TypeError, ValueError):
        count = 1
    failures[instance_id] = count
    gave_up = count >= KEEPALIVE_MAX_FAILURES
    if gave_up and instance_id not in state.setdefault("gave_up", []):
        state["gave_up"].append(instance_id)
    return count, gave_up


def keepalive_record_success(state, instance_id):
    """开机成功（或实例已不在 STOPPED 状态）：清掉该实例的失败计数。

    注意 gave_up 不动——停试后需用户到菜单里手动重新开启才恢复尝试。
    """
    state.get("failures", {}).pop(instance_id, None)


def keepalive_reset_instance(state, instance_id):
    """用户手动开关某实例的保活时调用：清失败计数与停试标记，重新尝试。"""
    state.get("failures", {}).pop(instance_id, None)
    gave_up = state.get("gave_up") or []
    if instance_id in gave_up:
        state["gave_up"] = [x for x in gave_up if x != instance_id]


# ---------------- SSL 监控 ----------------

def load_ssl_watch(path=None):
    """读 SSL 监控配置：{"domains", "reminded", "chat_id"}。"""
    data = _load_json(path or _config_path(SSL_WATCH_FILE), {})
    domains = [str(d) for d in (data.get("domains") or []) if d]
    reminded = {}
    for domain, thresholds in (data.get("reminded") or {}).items():
        try:
            reminded[str(domain)] = sorted({int(t) for t in thresholds})
        except (TypeError, ValueError):
            continue
    return {
        "domains": domains,
        "reminded": reminded,
        "chat_id": data.get("chat_id"),
    }


def save_ssl_watch(cfg, path=None):
    """保存 SSL 监控配置，返回写盘的字典。"""
    data = {
        "domains": [str(d) for d in (cfg.get("domains") or [])],
        "reminded": {str(k): sorted(set(v))
                     for k, v in (cfg.get("reminded") or {}).items()},
        "chat_id": cfg.get("chat_id"),
    }
    _save_json(path or _config_path(SSL_WATCH_FILE), data)
    return data


def ssl_reminder_due(days_left, reminded):
    """判断本次检查应提醒哪些阈值，并返回更新后的已提醒列表。

    返回 (due, new_reminded)：
      - due：已跨过（days_left <= 阈值）且未提醒过的阈值；
      - new_reminded：先剪掉已不再跨过的阈值（证书续期后对应阈值
        可重新提醒），再把本次 due 的阈值记进去。
    """
    reminded = [t for t in (reminded or []) if days_left <= t]
    due = [t for t in SSL_REMIND_DAYS if days_left <= t and t not in reminded]
    new_reminded = sorted(set(reminded) | set(due))
    return due, new_reminded


def normalize_domain(text):
    """把用户输入整理成域名：去空白 / 小写 / 去掉 scheme、路径与端口。"""
    domain = (text or "").strip().lower()
    if "://" in domain:
        domain = domain.split("://", 1)[1]
    domain = domain.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" in domain:
        domain = domain.split(":", 1)[0]
    return domain.strip().rstrip(".")


def is_valid_domain(domain):
    """域名格式校验（只校验格式，不查 DNS）。"""
    if not domain or len(domain) > 253:
        return False
    return bool(_DOMAIN_RE.match(domain))
