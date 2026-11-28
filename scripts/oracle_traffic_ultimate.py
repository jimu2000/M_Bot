#!/usr/bin/env python3
"""甲骨文云流量终极守卫（IP与地理位置整合版）。

vnstat / ip -s link 双保险统计 + 公网IP与全球地理位置解析 +
阶梯预警 + 安全熔断 + 自定义端口逃生。

来源：用户提供的 oracle_traffic_ultimate.py，改动两处（其余原样）：
  - Bark Key 不再写死在代码里：从 /root/.traffic_guard_bark
    （JSON，含 url/key，DD 初始化时由 M_Bot 从 M_bot_config.conf
    的 [bark] 节写入）读，读不到就不推送。
  - SSH 端口不再写死 14422：先查 sshd 实际监听端口，再读
    sshd_config，最后默认 22（与 M_Bot 的 SSH 端口自动识别一致）。
"""

import sys
import os
import json
import time
import urllib.parse
import subprocess
from datetime import datetime

STATE_DIR = "/var/lib/oracle_traffic_guard"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
LOG_FILE = "/var/log/oracle_traffic_guard.log"

# ==================== 配置阈值与参数区域 ====================
MONTHLY_LIMIT_GB = 200.0      # 绝对熔断线 (200 GB)

# 阶梯预警配置
STAGES = [
    (100.0, "100GB 阶段警报", "minuet"),
    (50.0,  "50GB 阶段提醒",  "telegraph"),
    (20.0,  "20GB 阶段提醒",  "chime"),
    (5.0,   "5GB 阶段提醒",   "glass")
]
# ============================================================

# Bark 配置：/root/.traffic_guard_bark（DD 初始化写入，JSON：{"url","key"}）；
# 环境变量 MBOT_BARK_KEY 兜底。读不到 key 就不推送（只记日志）。
_BARK_CONF_FILE = "/root/.traffic_guard_bark"


def _load_bark_key():
    key = os.environ.get("MBOT_BARK_KEY", "").strip()
    if key:
        return key
    try:
        with open(_BARK_CONF_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("key"):
            return str(data["key"]).strip()
    except (OSError, ValueError):
        pass
    return ""


BARK_KEY = _load_bark_key()


def _detect_ssh_port():
    """SSH 端口自动识别：先查 sshd 实际监听端口，再读 sshd_config，最后默认 22。"""
    # 1. Actual listening port (ss is authoritative). 实际监听端口（ss 最准）。
    try:
        p = subprocess.run(
            "ss -tlnp 2>/dev/null | grep -oP '(?<=:)\\d+(?=.*sshd)' | head -n1",
            shell=True, capture_output=True, text=True, timeout=10)
        port = (p.stdout or "").strip()
        if port.isdigit() and 1 <= int(port) <= 65535:
            return int(port)
    except Exception:
        pass
    # 2. sshd_config (sshd -T is authoritative over the raw file).
    # sshd_config（sshd -T 比直接读文件准）。
    try:
        p = subprocess.run(
            "sshd -T 2>/dev/null | grep -i '^port ' | head -n1",
            shell=True, capture_output=True, text=True, timeout=10)
        parts = (p.stdout or "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    except Exception:
        pass
    try:
        with open("/etc/ssh/sshd_config", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith("port "):
                    port = line.split()[1]
                    if port.isdigit():
                        return int(port)
    except OSError:
        pass
    return 22


SSH_PORT = _detect_ssh_port()


def log_msg(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{ts}] {msg}"
    print(formatted)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(formatted + "\n")
    except Exception:
        pass

def run_cmd(cmd: str):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def get_active_iface() -> str:
    candidates = ["ens3", "enp0s6"]
    for cand in candidates:
        if os.path.exists(f"/sys/class/net/{cand}"):
            return cand
    rc, out, _ = run_cmd("ip route show default | awk '/default/ {print $5}' | head -n1")
    if rc == 0 and out:
        return out.strip()
    return "ens3"

IFACE = get_active_iface()

def get_ip_and_geo() -> tuple:
    """获取公网 IP 与全球地理位置"""
    ip_info = "获取失败"
    geo_result = "未知国家 (未知地区)"

    # 1. 获取公网 IP
    rc, out, _ = run_cmd("curl -s --max-time 3 https://ip.sb")
    if rc == 0 and out:
        ip_info = out.strip()

    # 2. 获取地理位置
    rc, out, _ = run_cmd("curl -s --max-time 3 http://ip-api.com/json/?lang=zh-CN")
    if rc == 0 and out:
        try:
            data = json.loads(out)
            if data.get("status") == "success":
                country = data.get("country", "未知国家")
                city = data.get("city", "未知地区")
                geo_result = f"{country} ({city})"
        except Exception:
            pass

    return ip_info, geo_result

def send_bark_notification(title: str, body: str, sound: str = "alarm"):
    if not BARK_KEY:
        return
    try:
        et = urllib.parse.quote(title)
        eb = urllib.parse.quote(body)
        url = f"https://api.day.app/{BARK_KEY}/{et}/{eb}?sound={sound}"
        subprocess.run(f"curl -s --max-time 5 '{url}' > /dev/null 2>&1", shell=True)
    except Exception as e:
        log_msg(f"发送 Bark 通知失败: {e}")

def get_ip_tx_bytes() -> int:
    rc, out, _ = run_cmd(f"ip -s link show {IFACE}")
    if rc == 0 and out:
        try:
            lines = out.splitlines()
            for i, line in enumerate(lines):
                if "TX:" in line and i + 1 < len(lines):
                    parts = lines[i + 1].strip().split()
                    if parts:
                        return int(parts[0])
        except Exception as e:
            log_msg(f"解析 ip -s link 出错: {e}")
    return 0

def get_monthly_tx_bytes() -> int:
    rc, out, _ = run_cmd(f"vnstat -i {IFACE} --json m")
    if rc == 0 and out:
        try:
            data = json.loads(out)
            for iface in data.get("interfaces", []):
                if iface.get("name") == IFACE:
                    now = datetime.now()
                    for m in iface.get("traffic", {}).get("month", []):
                        d = m.get("date", {})
                        if d.get("year") == now.year and d.get("month") == now.month:
                            val = m.get("tx", 0)
                            if val > 0:
                                return val
        except Exception as e:
            log_msg(f"解析 vnstat 出错: {e}")
    return get_ip_tx_bytes()

def ensure_chain():
    run_cmd("iptables -N TRAFFIC_GUARD 2>/dev/null || true")
    rc, _, _ = run_cmd("iptables -C OUTPUT -j TRAFFIC_GUARD 2>/dev/null")
    if rc != 0:
        run_cmd("iptables -I OUTPUT 1 -j TRAFFIC_GUARD")

def is_circuit_broken() -> bool:
    rc, out, _ = run_cmd("iptables -L TRAFFIC_GUARD -n -v")
    return rc == 0 and "DROP" in out

def trip_circuit_breaker(reason: str):
    ensure_chain()
    ip_info, geo_result = get_ip_and_geo()
    log_msg(f"🚨 触发流量看门狗紧急熔断！网卡: {IFACE}，IP: {ip_info}，原因: {reason}")
    cmds = [
        "iptables -F TRAFFIC_GUARD",
        "iptables -A TRAFFIC_GUARD -o lo -j ACCEPT",
        "iptables -A TRAFFIC_GUARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        f"iptables -A TRAFFIC_GUARD -p tcp --sport {SSH_PORT} -j ACCEPT",
        "iptables -A TRAFFIC_GUARD -d 10.0.0.0/8 -j ACCEPT",
        "iptables -A TRAFFIC_GUARD -d 172.16.0.0/12 -j ACCEPT",
        "iptables -A TRAFFIC_GUARD -d 192.168.0.0/16 -j ACCEPT",
        f"iptables -A TRAFFIC_GUARD -o {IFACE} -j DROP",
    ]
    for c in cmds:
        run_cmd(c)

    body = f"公网 IP: {ip_info} ({geo_result})\n监听网卡: {IFACE}\n当月出站已达硬顶阈值\n状态: 外网已切断，SSH ({SSH_PORT}) 逃生门已保留"
    send_bark_notification("🚨 VPS 流量超限已熔断断网", body, sound="alarm")
    log_msg("✅ 熔断生效：外网出站已切断，自定义 SSH 端口保持正常。")

def check_traffic():
    ensure_chain()
    monthly_tx_bytes = get_monthly_tx_bytes()
    monthly_tx_gb = monthly_tx_bytes / (1024 ** 3)
    already_broken = is_circuit_broken()

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    current_month_str = datetime.now().strftime("%Y-%m")
    if state.get("last_month") != current_month_str:
        state = {"last_month": current_month_str, "notified_stages": []}

    notified_stages = state.get("notified_stages", [])

    if monthly_tx_gb >= MONTHLY_LIMIT_GB:
        if not already_broken:
            trip_circuit_breaker(f"当月累计出站已达 {monthly_tx_gb:.2f} GB（硬顶阈值 {MONTHLY_LIMIT_GB} GB）")

    for limit_val, title_text, sound_type in STAGES:
        if monthly_tx_gb >= limit_val and limit_val not in notified_stages:
            ip_info, geo_result = get_ip_and_geo()
            log_msg(f"⚠️ 流量预警：IP {ip_info} 月出站已达 {monthly_tx_gb:.2f} GB (触发 {limit_val}GB 阶梯)")
            body_msg = f"公网 IP: {ip_info} ({geo_result})\n网卡: {IFACE}\n当前月出站: {monthly_tx_gb:.2f} GB"
            send_bark_notification(f"⚠️ VPS {title_text}", body_msg, sound=sound_type)
            notified_stages.append(limit_val)
            break

    state["notified_stages"] = notified_stages
    state["monthly_tx_gb"] = round(monthly_tx_gb, 3)

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "status":
        ip_info, geo_result = get_ip_and_geo()
        print(f"=== 流量看门狗状态（IP与地理位置版） ===")
        print(f"公网 IP 地址: {ip_info} ({geo_result})")
        print(f"当前识别网卡: {IFACE}")
        print(f"当前统计出站流量: {get_monthly_tx_bytes() / (1024**3):.3f} GB")
        print(f"熔断器状态: {'🚨 已熔断断网' if is_circuit_broken() else '✅ 正常畅通'}")
    elif action == "test":
        print("正在获取 IP 与地理位置并发送测试推送...")
        ip_info, geo_result = get_ip_and_geo()
        current_flow = get_monthly_tx_bytes() / (1024**3)
        status_str = "🚨 已熔断断网" if is_circuit_broken() else "✅ 正常畅通"
        body_msg = f"公网 IP: {ip_info} ({geo_result})\n网卡: {IFACE}\n当前出站流量: {current_flow:.3f} GB\n熔断状态: {status_str}\n提示: IP与地理位置连通性测试成功！"
        send_bark_notification("🔔 VPS 流量卫士运行状态", body_msg, sound="glass")
        print("测试通知已成功发送至手机！")
    elif action == "trip":
        trip_circuit_breaker("管理员命令行手动触发测试")
        print("已成功模拟触发熔断！")
    elif action == "reset":
        run_cmd("iptables -F TRAFFIC_GUARD")
        run_cmd("iptables -A TRAFFIC_GUARD -j RETURN")
        print("已解除熔断状态。")
    else:
        check_traffic()
