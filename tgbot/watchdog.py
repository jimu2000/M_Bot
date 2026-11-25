#!/usr/bin/env python3
"""流量看门狗（用户自己的 oracle_traffic_guard.py）的安装/关闭/状态。

与 v1.7.2 的机器人内置守护是两回事：内置那套用户明确说不用
（2026-11-24 定：守护用他自己的脚本，不要机器人内置的），这里装的是
他自己的脚本（scripts/oracle_traffic_guard.py，vnstat 双统计 + 阶梯预警
+ 熔断/限速二选一），只负责装上去、配好 Bark、挂 cron、开关状态。

本模块只生成命令与解析输出，不直接连 SSH：执行方由调用方注入
（runner 协议见 _Runner；生产用 tgbot.handlers_client 的
run_remote_command / put_remote_file，测试用假 runner）。

安装流程（enable）：
  1. probe：脚本已是新版 + cron 在跑 → 已开启，直接返回。
  2. 旧版脚本在（无自动识别端口）：从旧文件里提取 Bark Key 写到
     配置文件（保留主机原有的 Key，不用机器人那套覆盖），再换新脚本。
  3. 全新安装：用机器人 Bark 配置（bark.json）写配置文件；机器人没配
     Bark 时报错，让用户先去菜单 43 配好。
  4. 确保 vnstat/python3/curl（复用 v1.16.0 的 guard_ensure_vnstat_command）。
  5. SFTP 上传模板脚本，chmod +x；加 cron（去重）；跑 test 发测试推送。

关闭流程（disable）：摘 cron + 跑 reset（解除熔断与限速），脚本文件保留。

公开版与本地的区别：模板脚本本身不写死 SSH 端口（运行时自动识别）
也不含任何 Key（Bark 配置走目标机上的 /root/.traffic_guard_bark，
600 权限）；安装时用的 Key 来自机器人 Bark 配置或主机旧脚本，不进仓库。
"""

import os
import re
from urllib.parse import urlparse

WATCHDOG_SCRIPT_PATH = "/usr/local/bin/oracle_traffic_guard.py"
WATCHDOG_CONFIG_PATH = "/root/.traffic_guard_bark"
WATCHDOG_CRON_LINE = (
    "*/10 * * * * /usr/bin/python3 /usr/local/bin/oracle_traffic_guard.py check"
)
# Signature string of the new template: probe uses it to tell whether the installed script is the auto-detect-port version. 新版模板的特征串：probe 靠它判断已装脚本是不是自动识别端口版。
WATCHDOG_AUTO_MARKER = "detect_ssh_port"

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "oracle_traffic_guard.py")


class WatchdogError(Exception):
    """看门狗安装/操作失败（消息可直接展示给用户）。"""


def load_template(path=None):
    """读模板脚本内容；文件缺失时抛 WatchdogError。"""
    path = path or TEMPLATE_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise WatchdogError(f"看门狗模板脚本缺失：{path}") from exc


def parse_bark_url(url):
    """把 Bark 推送地址拆成 (server, device_key)。

    url 形如 https://api.day.app/<device-key>/（官方）或自建地址；
    解析不出时返回 (None, None)。
    """
    if not url:
        return None, None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None, None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, None
    server = f"{parsed.scheme}://{parsed.netloc}"
    device_key = parsed.path.strip("/").split("/")[0] if parsed.path.strip("/") else ""
    if not device_key:
        return None, None
    return server, device_key


def bark_settings_for_install(bark_cfg):
    """从机器人 Bark 配置取出安装用的 (server, device_key, enc_key)。

    bark_cfg 是 tgbot.bark.load_bark_config() 的返回；未配齐返回 None。
    """
    cfg = bark_cfg or {}
    server, device_key = parse_bark_url(cfg.get("url") or "")
    enc_key = cfg.get("key") or ""
    if not server or not device_key or len(enc_key) != 32:
        return None
    return server, device_key, enc_key


def render_bark_config(server, device_key, enc_key):
    """生成目标机上的 Bark 配置文件内容（KEY=value 行）。"""
    return (f"BARK_SERVER={server}\n"
            f"BARK_KEY={device_key}\n"
            f"BARK_ENC_KEY={enc_key}\n")


def probe_command():
    """一台机器上查门狗状态的 shell 命令（单条，输出 KEY=value 行）。

    SCRIPT：脚本文件在不在；AUTO：是不是自动识别端口的新版；
    CRON：crontab 里有没有 guard 的 check 任务；
    MODE：OVER_LIMIT_MODE（0 阻断 / 1 限速，读不到为空）；
    TRIPPED：iptables 熔断是不是生效中；LIMITED：tc 限速是不是生效中。
    """
    return (
        f"[ -f {WATCHDOG_SCRIPT_PATH} ] && echo SCRIPT=1 || echo SCRIPT=0; "
        f"grep -q {WATCHDOG_AUTO_MARKER} {WATCHDOG_SCRIPT_PATH} 2>/dev/null "
        f"&& echo AUTO=1 || echo AUTO=0; "
        f"crontab -l 2>/dev/null | grep -q oracle_traffic_guard "
        f"&& echo CRON=1 || echo CRON=0; "
        f"grep -E '^OVER_LIMIT_MODE' {WATCHDOG_SCRIPT_PATH} 2>/dev/null "
        f"| head -n1 | sed 's/[^0-9]//g' | sed 's/^/MODE=/'; "
        f"iptables -L TRAFFIC_GUARD -n 2>/dev/null | grep -q DROP "
        f"&& echo TRIPPED=1 || echo TRIPPED=0; "
        f"tc qdisc show 2>/dev/null | grep -q tbf "
        f"&& echo LIMITED=1 || echo LIMITED=0"
    )


def parse_probe_output(text):
    """解析 probe_command 的输出，返回状态 dict。"""
    status = {
        "installed": False, "auto": False, "enabled": False,
        "mode": None, "tripped": False, "limited": False,
    }
    if not text:
        return status
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    status["installed"] = values.get("SCRIPT") == "1"
    status["auto"] = values.get("AUTO") == "1"
    status["enabled"] = status["installed"] and values.get("CRON") == "1"
    mode = values.get("MODE")
    status["mode"] = int(mode) if mode and mode.isdigit() else None
    status["tripped"] = values.get("TRIPPED") == "1"
    status["limited"] = values.get("LIMITED") == "1"
    return status


def guard_status_label(status):
    """看门狗状态一行文案（菜单 19 与网页版共用）。"""
    if not status:
        return "看门狗：未知（SSH 连不上）"
    if not status.get("installed"):
        return "看门狗：未安装"
    if status.get("tripped"):
        return "看门狗：已熔断断网！"
    if status.get("limited"):
        return "看门狗：限速中"
    if not status.get("enabled"):
        return "看门狗：已安装，未开启"
    mode = status.get("mode")
    mode_text = "阻断模式" if mode == 0 else ("限速模式" if mode == 1 else "已开启")
    return f"看门狗：已开启（{mode_text}）"


def extract_keys_command():
    """从旧脚本文件里提取 Bark Key 的 shell 命令（升级迁移用）。

    输出 BARK_KEY=... / BARK_ENC_KEY=... 两行（没有就是空值）。
    旧脚本把 Key 硬编码在文件里，换新脚本前必须先取出来写到
    配置文件，否则换完脚本推送就断了。
    """
    return (
        f"grep -E '^BARK_KEY' {WATCHDOG_SCRIPT_PATH} 2>/dev/null | head -n1; "
        f"grep -E '^BARK_ENC_KEY' {WATCHDOG_SCRIPT_PATH} 2>/dev/null | head -n1"
    )


_KEY_LINE_RE = re.compile(r"^BARK_(?:ENC_)?KEY\s*=\s*[\"']([^\"']+)[\"']")


def parse_extracted_keys(text):
    """解析 extract_keys_command 的输出，返回 (device_key, enc_key)。"""
    device_key, enc_key = "", ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("BARK_ENC_KEY"):
            match = _KEY_LINE_RE.match(line)
            if match:
                enc_key = match.group(1)
        elif line.startswith("BARK_KEY"):
            match = _KEY_LINE_RE.match(line)
            if match:
                device_key = match.group(1)
    # Placeholders do not count as a Key. 占位符不算 Key。
    if device_key in ("xxxxxxxxx", "xxxxxxxx"):
        device_key = ""
    if enc_key == "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx":
        enc_key = ""
    return device_key, enc_key


def add_cron_command():
    """加 cron（先删旧的 guard 行再加，不重复）。"""
    return (
        "(crontab -l 2>/dev/null | grep -v oracle_traffic_guard; "
        f"echo '{WATCHDOG_CRON_LINE}') | crontab -"
    )


def remove_cron_command():
    """摘 cron（只删 guard 的行，其他任务不动）。"""
    return "(crontab -l 2>/dev/null | grep -v oracle_traffic_guard) | crontab -"


def install_watchdog(host, bark_cfg, run_command, put_file,
                     ensure_command, template_text=None):
    """给一台主机安装并开启看门狗。

    host：run_remote_command 用的主机记录 dict。
    bark_cfg：机器人 Bark 配置（load_bark_config 的返回）。
    run_command(host, command, timeout=None)：执行远程命令，
        返回 (exit_code, stdout, stderr)，失败抛异常。
    put_file(host, content, remote_path, mode)：上传文件内容。
    ensure_command：确保 vnstat/python3/curl 的命令字符串。
    template_text：模板脚本内容（不传则现读）。

    返回 {"action": "installed"|"upgraded"|"enabled"|"already",
          "test_ok": bool, "mode": int|None}。
    "installed"：全新安装；"upgraded"：旧版脚本在，升级到新版；
    "enabled"：新版已装、只是 cron 被摘了，重新开启（不是升级）。
    """
    _code, out, _err = run_command(host, probe_command())
    status = parse_probe_output(out)
    if status["installed"] and status["auto"] and status["enabled"]:
        return {"action": "already", "test_ok": True,
                "mode": status.get("mode")}

    # Bark config: when the old script has a Key, keep the host's one (do not overwrite it with the bot's — the host's Key may be paired with the one in the App); only a fresh install uses the bot's Key. Bark 配置：旧脚本有 Key 就沿用主机的（别用机器人的覆盖，主机那套可能和 App 里的 Key 是一对）；全新安装才用机器人的。
    bark_settings = None
    if status["installed"] and not status["auto"]:
        _c, keys_out, _e = run_command(host, extract_keys_command())
        device_key, enc_key = parse_extracted_keys(keys_out)
        if device_key and len(enc_key) == 32:
            bark_settings = ("https://api.day.app", device_key, enc_key)
    if bark_settings is None:
        bark_settings = bark_settings_for_install(bark_cfg)
    if bark_settings is None:
        raise WatchdogError(
            "机器人还没配 Bark（菜单 43 配好地址和 Key 再来），"
            "看门狗装上也推不了通知。")

    # Dependencies: vnstat/python3/curl (install when missing; reuses menu 19's install command). 依赖：vnstat/python3/curl（缺了就装，复用菜单 19 同款命令）。
    code, _o, err = run_command(host, ensure_command, timeout=300)
    if code != 0:
        raise WatchdogError(
            f"安装依赖失败（vnstat/python3/curl）：{(err or '').strip()[-200:]}")

    # Write the Bark config file (mode 600), then upload the template script. 写 Bark 配置文件（600 权限），再上传模板脚本。
    server, device_key, enc_key = bark_settings
    put_file(host, render_bark_config(server, device_key, enc_key),
             WATCHDOG_CONFIG_PATH, mode=0o600)
    text = template_text if template_text is not None else load_template()
    put_file(host, text, WATCHDOG_SCRIPT_PATH, mode=0o755)

    # Install the cron + run test (test sends one Bark; a failure is not an install failure, just recorded). 挂 cron + 跑 test（test 发一条 Bark，失败不算安装失败，只记下来）。
    code, _o, err = run_command(host, add_cron_command())
    if code != 0:
        raise WatchdogError(f"挂 cron 失败：{(err or '').strip()[-200:]}")
    test_ok = True
    try:
        code, _o, _e = run_command(
            host, f"/usr/bin/python3 {WATCHDOG_SCRIPT_PATH} test", timeout=60)
        test_ok = code == 0
    except Exception:  # noqa: BLE001 - 测试推送失败不算安装失败
        test_ok = False
    if status["installed"] and status["auto"]:
        # New version already installed and only the cron was removed: re-enable it, not an upgrade (a real-machine check found re-enabling once misreported "已升级"). 新版已装、只是 cron 被摘了：重新开启，不是升级（真机验证挖出：重开时曾误报“已升级”）。
        action = "enabled"
    elif status["installed"]:
        action = "upgraded"
    else:
        action = "installed"
    return {"action": action, "test_ok": test_ok,
            "mode": status.get("mode")}


def disable_watchdog(host, run_command):
    """关闭一台主机的看门狗：摘 cron + 解除熔断/限速，脚本文件保留。"""
    _code, out, _err = run_command(host, probe_command())
    status = parse_probe_output(out)
    if not status["installed"] and not status["enabled"]:
        return {"action": "already_off"}
    code, _o, err = run_command(host, remove_cron_command())
    if code != 0:
        raise WatchdogError(f"摘 cron 失败：{(err or '').strip()[-200:]}")
    code, _o, err = run_command(
        host, f"/usr/bin/python3 {WATCHDOG_SCRIPT_PATH} reset", timeout=60)
    if code != 0:
        raise WatchdogError(f"解除熔断/限速失败：{(err or '').strip()[-200:]}")
    return {"action": "disabled"}
