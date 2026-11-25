#!/usr/bin/env python3
"""监控增强的纯逻辑与配置持久化（阶段 5 工作包 C）。

三个功能：
  1. 流量守护：本月出站流量（vnstat 自然月统计，SSH 到实例上查）
     超阈值时按动作执行：仅通知 / 阻断断网（iptables 熔断，SSH
     逃生门保留）/ 限速 200K（tc 限流到 1600kbit），同一阈值触发后
     24 小时内不再重复执行。动作与阈值照用户自己的 auto_limit.sh。
  2. 停机保活：STOPPED 实例按每实例开关自动开机，
     连续 3 次开机失败后停止尝试该实例。
  3. SSL 监控：域名证书到期前 30/14/7/1 天各提醒一次。

本模块是纯同步逻辑（JSON 文件读写 + 阈值判断 + 远程命令生成 +
vnstat 解析），不调 OCI API、不发 Telegram 消息、不连 SSH，
单元测试直接跑，不需要 mock 网络。Telegram handler 与 JobQueue
job 在 tgbot/handlers_extra.py 里，调本模块的函数做判断与持久化，
远程命令经 tgbot.handlers_client.run_remote_command 执行。

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
  - 阻断 / 限速默认关闭，用户明确开启才生效（见默认值）。
  - 阻断只切断外网出站：回环、已建立连接、SSH 端口、内网网段
    全部放行，SSH 逃生门保留；限速只降速不断网。
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

from tgbot import config as bot_config

# ================= 配置区（改这里） =================
# Traffic guard default threshold (GB): matches the user's auto_limit.sh LIMIT_GB (200). 流量守护默认阈值（GB）：照用户 auto_limit.sh 的 LIMIT_GB（200）。
TRAFFIC_GUARD_DEFAULT_THRESHOLD_GB = 200
# Traffic guard threshold presets (GB), picked in the Telegram menu: the script's 10/20/200 plus a 9 TB cap (9216 GB). 流量守护阈值预设（GB），Telegram 菜单里点选：脚本的 10/20/200 + 封顶 9TB（9216GB）。
TRAFFIC_GUARD_THRESHOLD_PRESETS = [10, 20, 200, 9216]
# Traffic guard action: notify (notify only) / block (cut the network, keep SSH) / limit (rate-limit to 200K). Mirrors auto_limit.sh's OVER_LIMIT_MODE (0=block, 1=limit), plus notify-only. 流量守护动作：notify 仅通知 / block 阻断断网（保留 SSH）/ limit 限速 200K。照 auto_limit.sh 的 OVER_LIMIT_MODE（0=阻断，1=限速），另加仅通知。
TRAFFIC_GUARD_ACTIONS = ("notify", "block", "limit")
TRAFFIC_GUARD_ACTION_LABELS = {
    "notify": "仅通知",
    "block": "阻断断网（保留SSH）",
    "limit": "限速200K",
}
# Rate-limit target (tc rate): 1600 kbit ≈ 200 KB/s, matching auto_limit.sh. 限速目标（tc 速率）：1600kbit ≈ 200 KB/s，照 auto_limit.sh。
TRAFFIC_GUARD_RATE_LIMIT = "1600kbit"
# Cooldown (hours) after a traffic guard trigger: no repeat action for the same threshold within this window (prevents repeated blocking/limiting; notify-only actions cool down too, so the user is not spammed every 10 minutes). 流量守护触发后冷却时间（小时）：同一阈值触发后这么久内不再重复执行（防反复阻断/限速；仅通知动作同样冷却，防每 10 分钟刷屏）。
TRAFFIC_GUARD_COOLDOWN_HOURS = 24
# Stop-keepalive: stop trying an instance and notify the user after this many consecutive boot failures. 停机保活：连续开机失败这么多次后停止尝试该实例并通知用户。
KEEPALIVE_MAX_FAILURES = 3
# SSL reminder thresholds (days): one reminder at each of these days before expiry. SSL 提醒阈值（天）：到期前这些天数时各提醒一次。
SSL_REMIND_DAYS = (30, 14, 7, 1)
# Config file names (under DATA_DIR). 配置文件名（存在 DATA_DIR 下）。
TRAFFIC_GUARD_FILE = "traffic_guard.json"
KEEPALIVE_FILE = "keepalive.json"
KEEPALIVE_STATE_FILE = "keepalive_state.json"
SSL_WATCH_FILE = "ssl_watch.json"
# =====================================================

# Domain format validation: lowercase letters / digits / hyphens, at least one dot; single labels are rejected. 域名格式校验：小写字母 / 数字 / 连字符，至少一个点，单标签不接受。
# (Format only, no DNS lookup; internationalized domains must use punycode, e.g. xn-- prefix.) （只校验格式，不查 DNS；国际化域名请用 punycode 形式，如 xn-- 开头。）
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

def fmt_threshold_gb(gb):
    """阈值显示：整 TB 时显示 TB（如 9216 → "9TB"），否则显示 GB。"""
    try:
        value = int(gb)
    except (TypeError, ValueError):
        return f"{gb}GB"
    if value >= 1024 and value % 1024 == 0:
        return f"{value // 1024}TB"
    return f"{value}GB"


def traffic_guard_action_label(action):
    """动作的中文名（未知动作回退到“仅通知”）。"""
    return TRAFFIC_GUARD_ACTION_LABELS.get(action,
                                           TRAFFIC_GUARD_ACTION_LABELS["notify"])


def next_traffic_guard_action(action):
    """动作切换顺序：仅通知 → 阻断断网 → 限速200K → 仅通知。"""
    try:
        idx = TRAFFIC_GUARD_ACTIONS.index(action)
    except ValueError:
        return "notify"
    return TRAFFIC_GUARD_ACTIONS[(idx + 1) % len(TRAFFIC_GUARD_ACTIONS)]


def default_traffic_guard():
    """流量守护默认配置：关闭、200GB、仅通知。"""
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
                     in TRAFFIC_GUARD_ACTIONS else "notify")
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
                   in TRAFFIC_GUARD_ACTIONS else "notify"),
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
    注意：OCI 用量接口不给真实出站数（一直 0.00GB），流量守护已改
    用 vnstat（见 parse_vnstat_monthly_gb），本函数只剩菜单 19 在用，
    v1.8.0 随菜单 19 一并换掉。
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


# ---------------- 流量守护：远程命令（经 SSH 执行） ----------------
# Commands mirror the user's auto_limit.sh logic (block / limit / vnstat traffic query), sent to the instance by handlers_extra via tgbot.handlers_client.run_remote_command. This module only builds the command strings and never connects via SSH. 命令照用户 auto_limit.sh 的逻辑（阻断 / 限速 / vnstat 查流量），由 handlers_extra 经 tgbot.handlers_client.run_remote_command 发到实例上执行。本模块只生成命令字符串，不连 SSH。

_GUARD_IFACE_DETECT = (
    "IF=$(ip route show default 2>/dev/null "
    "| awk '/default/ {print $5}' | head -n1); "
    "if [ -z \"$IF\" ]; then "
    "for _nic in eth0 ens3 enp0s6 enp0s3 ens5 ens4 enp1s0 eno1 ens6 venet0 eth1; do "
    "if [ -f \"/sys/class/net/$_nic/statistics/tx_bytes\" ]; then "
    "IF=\"$_nic\"; break; fi; done; fi; "
    "[ -z \"$IF\" ] && { echo GUARD-ERROR no-interface; exit 1; }"
)

_GUARD_SSH_PORT_DETECT = (
    "SSH_PORT=$(ss -tlnp 2>/dev/null | awk '/sshd/ {print $4}' "
    "| head -n1 | sed 's/.*://'); "
    "if [ -z \"$SSH_PORT\" ]; then "
    "SSH_PORT=$(grep -E '^Port' /etc/ssh/sshd_config 2>/dev/null "
    "| awk '{print $2}' | head -n1); fi; "
    "[ -z \"$SSH_PORT\" ] && SSH_PORT=22"
)


def guard_vnstat_command():
    """查本月流量：识别默认网卡后跑 vnstat --json m，输出 JSON。"""
    return _GUARD_IFACE_DETECT + "; vnstat -i \"$IF\" --json m"


# Max wait (seconds) for the auto-install of traffic deps: apt update + installing the vnstat / python3 / curl trio takes ~1-3 minutes on a fresh machine in practice, so 300s leaves headroom. 自动安装流量依赖的最长等待（秒）：apt update + 装 vnstat / python3 / curl 三件套，新机实测约 1-3 分钟，300 秒留足余量。
GUARD_ENSURE_INSTALL_TIMEOUT_SECONDS = 300


def guard_ensure_vnstat_command():
    """确保 vnstat / python3 / curl 已安装，并启用 vnstat 服务。

    三件套齐了直接返回 0，不动 apt；缺任一时跑
    apt-get update && apt-get install -y vnstat python3 curl
    && systemctl enable --now vnstat（DEBIAN_FRONTEND=noninteractive，
    避免交互提问挂住；apt-get 而不是 apt——apt 不是给脚本用的）。
    只支持 Debian/Ubuntu（apt-get 系）；其他发行版会直接失败，
    调用方按失败处理。用户要求：每个 VPS 缺了就默认安装。
    """
    check = ("for _c in vnstat python3 curl; do "
             "command -v \"$_c\" >/dev/null 2>&1 || _missing=1; done; "
             "[ -z \"$_missing\" ] && exit 0")
    install = ("DEBIAN_FRONTEND=noninteractive apt-get update "
               "&& DEBIAN_FRONTEND=noninteractive apt-get install -y "
               "vnstat python3 curl "
               "&& systemctl enable --now vnstat")
    return check + "; " + install


def parse_vnstat_monthly_gb(json_text, now=None):
    """解析 vnstat --json m 的输出，返回 (入站 GiB, 出站 GiB)。

    取 interfaces[0].traffic.month 里与当前年月匹配的一条；
    没有当月条目时取最后一条；解析失败 / 无数据时返回 None。
    vnstat 的 rx/tx 单位是字节，除以 1024^3 得 GiB。
    """
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(json_text)
        months = data["interfaces"][0]["traffic"]["month"]
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if not months:
        return None
    entry = None
    for item in months:
        date = item.get("date") or {}
        if (date.get("year") == now.year
                and date.get("month") == now.month):
            entry = item
            break
    if entry is None:
        entry = months[-1]
    try:
        rx_gb = float(entry["rx"]) / 1024 / 1024 / 1024
        tx_gb = float(entry["tx"]) / 1024 / 1024 / 1024
    except (TypeError, ValueError, KeyError):
        return None
    return (rx_gb, tx_gb)


def fetch_city_for_ip(ip, timeout=3):
    """查 IP 的中文城市名（ip-api.com，lang=zh-CN）。

    与用户 oracle_traffic_guard.py 的 get_ip_and_geo 同源。
    查不到/超时/返回异常时返回“未知地区”，不抛异常。
    """
    if not ip:
        return "未知地区"
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        req = urllib.request.Request(url, headers={"User-Agent": "M-Bot"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 查城市失败不影响流量显示
        return "未知地区"
    if isinstance(data, dict) and data.get("status") == "success":
        return data.get("city") or "未知地区"
    return "未知地区"


def guard_block_command():
    """阻断断网命令（照 auto_limit.sh 的 trip_circuit_breaker）。

    TRAFFIC_GUARD 链：放行回环、已建立连接、SSH 端口出站、内网
    网段，其余外网出站 DROP。已熔断时不再重复执行。执行结果打在
    stdout 最后一行：GUARD-BLOCKED / GUARD-ALREADY-BLOCKED。
    注意：当前 SSH 会话属于 ESTABLISHED，不会被切断。
    """
    return (
        _GUARD_IFACE_DETECT + "; " + _GUARD_SSH_PORT_DETECT + "; "
        "iptables -N TRAFFIC_GUARD 2>/dev/null || true; "
        "if ! iptables -C OUTPUT -j TRAFFIC_GUARD 2>/dev/null; then "
        "iptables -I OUTPUT 1 -j TRAFFIC_GUARD; fi; "
        "if iptables -L TRAFFIC_GUARD -n 2>/dev/null | grep -q DROP; then "
        "echo GUARD-ALREADY-BLOCKED; exit 0; fi; "
        "iptables -F TRAFFIC_GUARD; "
        "iptables -A TRAFFIC_GUARD -o lo -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -m conntrack "
        "--ctstate ESTABLISHED,RELATED -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -p tcp --sport \"$SSH_PORT\" -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -d 10.0.0.0/8 -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -d 172.16.0.0/12 -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -d 192.168.0.0/16 -j ACCEPT; "
        "iptables -A TRAFFIC_GUARD -o \"$IF\" -j DROP; "
        "echo GUARD-BLOCKED"
    )


def guard_limit_command(rate=None):
    """限速命令（照 auto_limit.sh 的限速分支）：tc htb 限流。

    rate 默认 TRAFFIC_GUARD_RATE_LIMIT（1600kbit ≈ 200KB/s）。
    已有限速规则时不再重复执行。结果打在 stdout 最后一行：
    GUARD-LIMITED / GUARD-ALREADY-LIMITED。
    """
    rate = rate or TRAFFIC_GUARD_RATE_LIMIT
    return (
        _GUARD_IFACE_DETECT + "; "
        "if tc qdisc show dev \"$IF\" 2>/dev/null | grep -q htb; then "
        "echo GUARD-ALREADY-LIMITED; exit 0; fi; "
        "tc qdisc del dev \"$IF\" root 2>/dev/null; "
        "tc qdisc add dev \"$IF\" root handle 1: htb default 10; "
        "tc class add dev \"$IF\" parent 1: classid 1:1 htb "
        f"rate {rate} ceil {rate}; "
        "tc class add dev \"$IF\" parent 1:1 classid 1:10 htb "
        f"rate {rate} ceil {rate}; "
        "tc filter add dev \"$IF\" protocol ip prio 1 u32 "
        "match u32 0 0 flowid 1:10; "
        "echo GUARD-LIMITED"
    )


def guard_unblock_command():
    """解除限制：删 TRAFFIC_GUARD 链的 OUTPUT 跳转并删链，删 tc 限速。

    两步都 best-effort（没设过限制时也正常返回）。结果打在
    stdout 最后一行：GUARD-UNBLOCKED。
    """
    return (
        _GUARD_IFACE_DETECT + "; "
        "iptables -D OUTPUT -j TRAFFIC_GUARD 2>/dev/null || true; "
        "iptables -F TRAFFIC_GUARD 2>/dev/null || true; "
        "iptables -X TRAFFIC_GUARD 2>/dev/null || true; "
        "tc qdisc del dev \"$IF\" root 2>/dev/null || true; "
        "echo GUARD-UNBLOCKED"
    )


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
