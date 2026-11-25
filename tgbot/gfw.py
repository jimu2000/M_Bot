#!/usr/bin/env python3
"""GFW 自动换 IP（菜单 16）：国内 ping 检测 + 被墙自动换 IP。

用户要求 2026-11-24：菜单 16 改成“GFW自动换IP”，走国内 ping 检测，
IP 被墙就换 IP，换好发双通道通知（TG + Bark；没设置 Bark 就只通知
TG，Bark 是可选项）；换完新 IP 还是被墙就继续换、继续通知。

检测走 RIPE Atlas（https://atlas.ripe.net）：从中国大陆探针
（电信/联通/移动）ping 实例公网 IP，多数探针不通即判定被墙。
选 Atlas 的原因（2026-11-24 实测调研）：
  - itdog.cn：2026-10 起任务端点强制点选人机验证，curl 直接 403，
    无人值守的定时监控没法用；
  - 17CE / BOCE：官方 API 要账号 + 付费积分；
  - Atlas：正规 REST API，免费账号可领一次 5 万积分，一次 ping
    检测（5 个探针）约几十积分；国内在线探针 100+ 个
    （2026-11-24 实测 104 个，含 AS4134 电信 / AS9808 移动 /
    AS17622 联通）。
需要用户在 Atlas 注册并提供一个 API Key（设置步骤见 README）；
Key 存在 tgbot/data/gfw.json（0600），绝不记日志、不回显。

判定规则（防误判，误判会频繁换 IP 导致服务中断）：
  - 每次检测请求 5 个中国探针，各发 3 个 ping 包；
  - 上报的探针少于 3 个时结论是“未知”，不换 IP、不通知
    （只记进 last_check）；
  - 上报探针中可达（至少 1 个包有回包）的不到一半 → 被墙；
  - 其余情况 → 正常。
换 IP 循环（run_instance_cycle）：
  - 被墙 → 换 IP（复用 oci/network.py 的 change_public_ip，
    先删旧临时 IP 再绑新保留 IP，已真实验证过）→ 重新检测新 IP；
  - 新 IP 正常 → 发一条通知（旧 IP → 新 IP + 检测结果），结束；
  - 新 IP 仍被墙 → 继续换、继续通知；
  - 单轮最多换 MAX_IP_CHANGES_PER_CYCLE 次（默认 5 次），超了就
    发一条“已暂停”通知停下，等下个检查周期再继续（防整个网段
    被墙时无限换 IP）。
通知走 tgbot.notify.notify_dual（TG + Bark，Bark 未配置时只推 TG，
不是错误——Bark 是可选项）。

定时任务：每 30 分钟跑一轮（bot.py 注册 gfw_check_job），
只检查已开启的实例；没配 Key 或没开启任何实例时静默跳过。
"""

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from tgbot import config as bot_config

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# RIPE Atlas API. RIPE Atlas API。
ATLAS_API_BASE = "https://atlas.ripe.net/api/v2"
# China probes per check (5, mixed China Telecom/Unicom/Mobile, randomly assigned by Atlas with country=CN); pings sent per probe. 每次检测请求的中国探针数（5 个，电信/联通/移动混合，Atlas 按 country=CN 随机分配）；每个探针发的 ping 包数。
ATLAS_PROBE_COUNT = 5
ATLAS_PING_PACKETS = 3
# Minimum reporting probes needed for a verdict; below this the verdict is "unknown" and nothing happens. 检测结论需要的最少上报探针数；少于这个数时结论是“未知”，不行动。
MIN_REPORTED_PROBES = 3
# Blocked threshold: blocked when the reachable-probe share is below it (majority unreachable). 判定被墙的阈值：可达探针占比低于它即被墙（多数不通）。
BLOCKED_RATIO_BELOW = 0.5
# Max wait (seconds) for Atlas measurement results: results arrive gradually after the measurement is created, so poll every ATLAS_POLL_INTERVAL seconds and stop once enough have arrived or the timeout hits. 等 Atlas 测量结果的最长等待（秒）：测量创建后结果陆续到达，每 ATLAS_POLL_INTERVAL 秒查一次，够数或超时即停止等待。
ATLAS_RESULT_TIMEOUT = 150
ATLAS_POLL_INTERVAL = 10
# Max IP changes per round (one check cycle); stop and wait for the next cycle when exceeded. 单轮（一次检查周期）最多换 IP 次数；超了停下等下个周期。
MAX_IP_CHANGES_PER_CYCLE = 5
# Scheduled check interval (minutes; used by the job in bot.py). 定时检查间隔（分钟，bot.py 的 job 用这个值）。
GFW_CHECK_INTERVAL_MINUTES = 30

# Config file name (under tgbot/data/, alongside bark.json / paid_profiles.json). 配置文件名（放 tgbot/data/，与 bark.json / paid_profiles.json 同目录）。
GFW_CONFIG_FILENAME = "gfw.json"


# ================= 配置存取 =================

def _config_path():
    """gfw.json 的完整路径（走 bot_config.DATA_DIR）。"""
    return os.path.join(bot_config.DATA_DIR, GFW_CONFIG_FILENAME)


def load_gfw_config():
    """读 GFW 监控配置，文件缺失/损坏时返回空配置。

    结构：{"api_key": str, "enabled": {instance_id: {"profile",
    "region", "name", "ip"}}, "last_check": {instance_id: {...}}}。
    """
    path = _config_path()
    empty = {"api_key": "", "enabled": {}, "last_check": {}}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        logger.warning("GFW 配置文件损坏，返回空配置：%s", path)
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "api_key": data.get("api_key") or "",
        "enabled": data.get("enabled") or {},
        "last_check": data.get("last_check") or {},
    }


def save_gfw_config(cfg):
    """保存 GFW 配置（原子写 tmp + replace，文件 0600）。"""
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=".gfw-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_gfw_configured(cfg=None):
    """是否已设置 Atlas API Key（没 Key 时检测跑不起来）。"""
    if cfg is None:
        cfg = load_gfw_config()
    return bool((cfg.get("api_key") or "").strip())


def mask_api_key(key):
    """Key 脱敏显示：只留首尾各 4 位（Atlas Key 是 UUID 格式）。"""
    key = (key or "").strip()
    if len(key) <= 10:
        return "****" if key else "未设置"
    return f"{key[:4]}…{key[-4:]}"


def set_api_key(key):
    """保存 Atlas API Key（去首尾空白），返回脱敏后的显示值。"""
    cfg = load_gfw_config()
    cfg["api_key"] = (key or "").strip()
    save_gfw_config(cfg)
    return mask_api_key(cfg["api_key"])


def set_instance_enabled(instance_id, enabled, profile=None, region=None,
                         name=None, ip=None):
    """开启/关闭某实例的 GFW 监控（开启时记下 profile/区域/名字/IP，
    定时任务按这些信息找实例，不用再翻菜单状态）。"""
    cfg = load_gfw_config()
    if enabled:
        cfg["enabled"][instance_id] = {
            "profile": profile or bot_config.DEFAULT_PROFILE,
            "region": region or bot_config.get_region(
                profile or bot_config.DEFAULT_PROFILE),
            "name": name or instance_id,
            "ip": ip or "",
        }
    else:
        cfg["enabled"].pop(instance_id, None)
    save_gfw_config(cfg)
    return cfg


def record_last_check(instance_id, record):
    """记一台实例最近一次检查结果（带北京时间戳，供菜单/网页显示）。"""
    cfg = load_gfw_config()
    entry = dict(record)
    entry["at"] = datetime.now(timezone.utc).isoformat()
    cfg["last_check"][instance_id] = entry
    save_gfw_config(cfg)
    return cfg


# ================= RIPE Atlas 检测 =================

def _atlas_request(api_key, method, path, payload=None, timeout=30):
    """调 Atlas API，返回解析后的 JSON；HTTP 错误抛 AtlasError。

    Key 只放 query 参数（Atlas 的约定），绝不记日志。
    """
    url = f"{ATLAS_API_BASE}{path}?key={api_key}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "M-Bot/gfw-check",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - 读错误体失败就不要了
            pass
        raise AtlasError(f"Atlas API {method} {path} 失败："
                         f"HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise AtlasError(f"Atlas API {method} {path} 网络错误：{exc}") from exc


class AtlasError(Exception):
    """RIPE Atlas 调用失败（网络 / HTTP 错误 / 响应异常）。"""


def create_ping_measurement(api_key, target_ip,
                            probe_count=ATLAS_PROBE_COUNT,
                            packets=ATLAS_PING_PACKETS):
    """创建一个 one-off ping 测量（中国探针 → 目标 IP），返回测量 ID。

    probes 用 country=CN 让 Atlas 分配中国大陆探针（电信/联通/移动
    混合）；description 带 M-Bot 前缀，方便在 Atlas 面板里认出来。
    """
    payload = {
        "definitions": [{
            "target": target_ip,
            "description": f"M-Bot GFW check {target_ip}",
            "type": "ping",
            "af": 4,
            "packets": packets,
            "size": 48,
            "is_oneoff": True,
        }],
        "probes": [{
            "requested": probe_count,
            "type": "country",
            "value": "CN",
        }],
        "is_oneoff": True,
    }
    result = _atlas_request(api_key, "POST", "/measurements/",
                            payload=payload)
    ids = (result or {}).get("measurements") or []
    if not ids:
        raise AtlasError(f"Atlas 创建测量未返回 ID：{str(result)[:200]}")
    return ids[0]


def fetch_measurement_results(api_key, measurement_id):
    """取一次测量结果（每探针一条），返回 list（可能为空）。"""
    result = _atlas_request(
        api_key, "GET", f"/measurements/{measurement_id}/results/")
    if not isinstance(result, list):
        raise AtlasError(f"Atlas 结果格式异常：{str(result)[:200]}")
    return result


def wait_measurement_results(api_key, measurement_id,
                             requested=ATLAS_PROBE_COUNT,
                             timeout=ATLAS_RESULT_TIMEOUT,
                             interval=ATLAS_POLL_INTERVAL):
    """等测量结果：够 requested 条或超时即返回已有的结果。

    Atlas 结果陆续到达，一般 30-90 秒齐；超时后用已有结果判定
    （少于 MIN_REPORTED_PROBES 时结论为未知，不行动）。
    """
    deadline = time.time() + timeout
    results = []
    while True:
        try:
            results = fetch_measurement_results(api_key, measurement_id)
        except AtlasError:
            logger.warning("取 Atlas 结果失败（测量 %s），稍后重试",
                           measurement_id)
        if len(results) >= requested:
            return results
        if time.time() >= deadline:
            return results
        time.sleep(interval)


def evaluate_results(results):
    """按探针结果判定：返回 (verdict, reachable, reported, detail)。

    verdict：\"blocked\"（被墙）/ \"healthy\"（正常）/ \"unknown\"
    （上报太少，无法判定）。每探针的 result 是每包一条的 list，
    有 rtt 的包算通，{\"x\": \"*\"} 的包算超时；探针至少 1 个包
    有 rtt 才算可达（单个包丢包不算墙）。
    """
    reported = 0
    reachable = 0
    for entry in results or []:
        packets = (entry or {}).get("result")
        if not isinstance(packets, list) or not packets:
            continue
        reported += 1
        if any(isinstance(p, dict) and p.get("rtt") is not None
               for p in packets):
            reachable += 1
    if reported < MIN_REPORTED_PROBES:
        return ("unknown", reachable, reported,
                f"上报探针只有 {reported} 个（需要至少 "
                f"{MIN_REPORTED_PROBES} 个），无法判定")
    if reachable / reported < BLOCKED_RATIO_BELOW:
        return ("blocked", reachable, reported,
                f"国内 {reported} 个探针只有 {reachable} 个能 ping 通")
    return ("healthy", reachable, reported,
            f"国内 {reported} 个探针 {reachable} 个能 ping 通")


def check_ip_blocked(api_key, ip):
    """检测一个 IP 是否被墙：返回 dict。

    {blocked: True/False/None（未知）, reachable, reported,
    detail, measurement_id, error?}。Atlas 调用失败时 blocked=None
    且带 error，不抛异常（调用方按“未知”处理，不换 IP）。
    """
    try:
        msm_id = create_ping_measurement(api_key, ip)
        results = wait_measurement_results(api_key, msm_id)
    except AtlasError as exc:
        logger.warning("Atlas 检测 %s 失败：%s", ip, exc)
        return {"blocked": None, "reachable": 0, "reported": 0,
                "detail": f"检测失败：{exc}", "measurement_id": None,
                "error": str(exc)}
    verdict, reachable, reported, detail = evaluate_results(results)
    return {"blocked": (True if verdict == "blocked"
                        else False if verdict == "healthy" else None),
            "reachable": reachable, "reported": reported,
            "detail": detail, "measurement_id": msm_id}


# ================= 换 IP 循环 =================

def run_instance_cycle(cli, compartment_id, instance, api_key, notify,
                       checker=None, changer=None,
                       max_changes=MAX_IP_CHANGES_PER_CYCLE):
    """对一台实例跑一轮“检测 → 被墙就换 IP → 再检测”循环。

    cli / compartment_id：该实例所属账号/区域的 OCI 访问；
    instance：实例 dict（至少有 id / display-name / 公网 IP，
    IP 取 \"public-ip\" 键，没有时调用方先用 attach_public_ips 补）；
    notify(title, body)：双通道通知回调（调用方绑定 notify_dual，
    Bark 未配置时只推 TG）；checker 默认 check_ip_blocked，
    changer 默认 change_public_ip（测试可注入假的）。
    返回摘要 dict：{status, changes, ip, detail}，status 为
    healthy（本来就正常）/ recovered（换 IP 后恢复）/
    exhausted（换满 max_changes 次仍被墙）/ unknown（检测无结论，
    没换 IP）/ error（换 IP 出错）/ no_ip（实例没有公网 IP）。
    每换一次 IP 发一条通知；换满仍被墙再发一条“已暂停”通知；
    本来就正常、检测无结论时不发通知（定时任务不打扰）。
    """
    from oci.network import change_public_ip

    if checker is None:
        checker = check_ip_blocked
    if changer is None:
        changer = change_public_ip
    name = (instance or {}).get("display-name") or "未知实例"
    current_ip = (instance or {}).get("public-ip") or ""
    if not current_ip:
        return {"status": "no_ip", "changes": 0, "ip": "",
                "detail": f"{name} 没有公网 IP，无法检测"}
    changes = 0
    pending = None  # The new IP was already measured as blocked after the change, so the next round reuses that result instead of re-measuring (saves one Atlas credit and one minute of waiting). 换 IP 后已测过新 IP 是被墙，下一轮直接沿用，不重测（省一次 Atlas 积分和一分钟等待）。
    while True:
        res = pending if pending is not None else checker(api_key,
                                                          current_ip)
        pending = None
        if res.get("blocked") is None:
            return {"status": "unknown", "changes": changes,
                    "ip": current_ip,
                    "detail": res.get("detail") or "检测无结论"}
        if not res.get("blocked"):
            if changes:
                return {"status": "recovered", "changes": changes,
                        "ip": current_ip, "detail": res.get("detail") or ""}
            return {"status": "healthy", "changes": 0, "ip": current_ip,
                    "detail": res.get("detail") or ""}
        if changes >= max_changes:
            notify("⚠️ GFW 换 IP 已暂停",
                   f"实例：{name}\n当前 IP：{current_ip}\n"
                   f"国内检测：{res.get('detail')}\n"
                   f"本轮已换 {changes} 次 IP，换完仍被墙，"
                   f"已暂停换 IP，等下个检查周期继续。")
            return {"status": "exhausted", "changes": changes,
                    "ip": current_ip, "detail": res.get("detail") or ""}
        old_ip = current_ip
        try:
            new_ip = changer(cli, compartment_id, instance["id"])
        except Exception as exc:  # noqa: BLE001 - 换 IP 失败要通知用户
            logger.warning("GFW 换 IP 失败（%s）：%s", name, exc)
            notify("⚠️ GFW 换 IP 失败",
                   f"实例：{name}\n当前 IP：{old_ip}\n"
                   f"国内检测：{res.get('detail')}\n换 IP 时出错：{exc}\n"
                   f"请手动检查。")
            return {"status": "error", "changes": changes, "ip": old_ip,
                    "detail": f"换 IP 失败：{exc}"}
        changes += 1
        current_ip = new_ip
        # Do not notify right after the change; wait for the new IP's check result and report together (new IP healthy -> one "changed + recovered" message; still blocked -> one "changed but still blocked, changing again" message; exactly one notice per change). 换完先不通知，等新 IP 的检测结果出来再一起说（新 IP 正常 → 一条“已换 + 已恢复”；仍被墙 → 一条“已换但仍被墙，继续换”，每换一次恰好一条通知）。
        res2 = checker(api_key, current_ip)
        if res2.get("blocked") is None:
            notify("🔁 GFW 自动换 IP",
                   f"实例：{name}\n旧 IP：{old_ip}（被墙，"
                   f"{res.get('detail')}）\n新 IP：{new_ip}\n"
                   f"新 IP 检测无结论（{res2.get('detail')}），"
                   f"本轮不再继续换，下个检查周期再看。")
            return {"status": "unknown", "changes": changes,
                    "ip": current_ip,
                    "detail": res2.get("detail") or "检测无结论"}
        if not res2.get("blocked"):
            notify("✅ GFW 自动换 IP",
                   f"实例：{name}\n旧 IP：{old_ip}（被墙，"
                   f"{res.get('detail')}）\n新 IP：{new_ip}\n"
                   f"新 IP 国内检测：{res2.get('detail')}，已恢复。")
            return {"status": "recovered", "changes": changes,
                    "ip": current_ip, "detail": res2.get("detail") or ""}
        notify("🔁 GFW 自动换 IP（仍被墙，继续换）",
               f"实例：{name}\n旧 IP：{old_ip}（被墙，"
               f"{res.get('detail')}）\n新 IP：{new_ip}\n"
               f"新 IP 国内检测：{res2.get('detail')}，仍被墙，"
               f"继续换（本轮第 {changes} 次）。")
        pending = res2  # The next round starts by reusing this result, without re-measuring. 下一轮开头沿用这次的结果，不重测。


def _resolve_enabled_instances(enabled):
    """把已开启的实例解析成可检测的条目（读真实 OCI）。

    按 (profile, region) 分组，一组只 list 一次实例；只留 RUNNING
    且有公网 IP 的；找不到 / 已停 / 没公网 IP 的记进 skipped
    （调用方写进 last_check，不换 IP）。
    返回 (items, skipped)：items 是 {instance_id, profile, region,
    instance, cli, compartment_id}，skipped 是 {instance_id: 原因}。
    """
    from oci.instance import list_instances
    from oci.network import attach_public_ips

    groups = {}
    for inst_id, meta in (enabled or {}).items():
        key = (meta.get("profile") or bot_config.DEFAULT_PROFILE,
               meta.get("region") or bot_config.get_region(
                   meta.get("profile") or bot_config.DEFAULT_PROFILE))
        groups.setdefault(key, []).append(inst_id)
    items = []
    skipped = {}
    for (profile, region), inst_ids in groups.items():
        try:
            cli = bot_config.get_oci_client(profile=profile, region=region)
            compartment_id = bot_config.get_compartment_id(profile)
            instances = attach_public_ips(
                cli, list_instances(cli, compartment_id) or [])
        except Exception as exc:  # noqa: BLE001 - 单组失败不影响其他组
            for inst_id in inst_ids:
                skipped[inst_id] = f"读取实例列表失败：{exc}"
            continue
        by_id = {i.get("id"): i for i in instances}
        for inst_id in inst_ids:
            inst = by_id.get(inst_id)
            if inst is None:
                skipped[inst_id] = "实例不存在（可能已释放）"
            elif inst.get("lifecycle-state") != "RUNNING":
                skipped[inst_id] = (f"实例未运行（"
                                    f"{inst.get('lifecycle-state')}）")
            elif not inst.get("public-ip"):
                skipped[inst_id] = "实例没有公网 IP"
            else:
                items.append({"instance_id": inst_id, "profile": profile,
                              "region": region, "instance": inst,
                              "cli": cli,
                              "compartment_id": compartment_id})
    return items, skipped


def run_monitor_cycle(notify, checker=None, changer=None,
                      instance_resolver=None):
    """跑一轮 GFW 监控：对所有已开启的实例逐个检测换 IP。

    notify(title, body)：双通道通知回调；checker/changer/
    instance_resolver 可注入（测试用），默认走真实 Atlas/OCI。
    没配 Key 或没开启任何实例时直接返回 []（静默，不打扰）。
    每台的结果写进 last_check；返回每台的摘要 list。
    """
    cfg = load_gfw_config()
    api_key = (cfg.get("api_key") or "").strip()
    enabled = cfg.get("enabled") or {}
    if not api_key or not enabled:
        return []
    if instance_resolver is None:
        instance_resolver = _resolve_enabled_instances
    items, skipped = instance_resolver(enabled)
    results = []
    for inst_id, reason in skipped.items():
        record = {"status": "skipped", "changes": 0, "ip": "",
                  "detail": reason}
        record_last_check(inst_id, record)
        results.append({"instance_id": inst_id, **record})
    for item in items:
        inst = item["instance"]
        summary = run_instance_cycle(
            item["cli"], item["compartment_id"], inst, api_key,
            notify, checker=checker, changer=changer)
        record_last_check(item["instance_id"], {
            "status": summary["status"], "changes": summary["changes"],
            "ip": summary["ip"], "detail": summary["detail"],
            "name": inst.get("display-name") or "",
        })
        results.append({"instance_id": item["instance_id"], **summary})
    return results
