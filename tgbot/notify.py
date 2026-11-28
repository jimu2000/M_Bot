"""双通道通知（Telegram + Bark），daemon 线程里也能用。

Telegram 不走 python-telegram-bot 的事件循环，直接 HTTP 调
Bot API（sendMessage），所以抢机线程里也能发；Bark 走
tgbot.bark.send_bark（AES-256-CBC 加密推送）。
通知函数绝不抛异常（失败只返回错误，调用方线程不能因此死掉），
Token / Key / 密码绝不记日志、不出现在返回值里。
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

from tgbot.bark import send_bark

logger = logging.getLogger(__name__)

DEFAULT_BOOT_DISK_GB = 47
# start_ip_followup() keeps waiting for the IP: when the boot notice is already sent but the IP is not assigned yet, poll in the background for up to 10 minutes and send a follow-up TG message as soon as the IP appears. start_ip_followup() 跟进等 IP：开机通知已发出但 IP 还没分配时，后台继续轮询，最长等 10 分钟，IP 一出来就补发一条 TG 消息。
# No IP after 10 minutes: give up silently (the instance is visible in the list; do not bother the user further). 10 分钟还没 IP 就静默放弃（实例列表里能看到，不再打扰用户）。
IP_FOLLOWUP_MAX_SECONDS = 600
IP_FOLLOWUP_INTERVAL_SECONDS = 30


def send_telegram_message_full(token, chat_id, text, parse_mode=None):
    """直接 HTTP 调 Telegram Bot API 发一条消息。

    供 daemon 线程使用（无事件循环）。token 缺失时直接返回失败。
    返回 (ok, error, message_id)：message_id 供置顶用，发送失败时为 None。
    """
    if not token or chat_id is None:
        return False, "Telegram token 或 chat_id 未配置", None
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"Telegram HTTP {exc.code}", None
    except Exception as exc:  # noqa: BLE001
        return False, str(exc), None
    if result.get("ok"):
        message_id = (result.get("result") or {}).get("message_id")
        return True, "", message_id
    return False, str(result.get("description") or "Telegram API 返回失败"), None


def send_telegram_message(token, chat_id, text, parse_mode=None):
    """直接 HTTP 调 Telegram Bot API 发一条消息，返回 (ok, error)。"""
    ok, err, _mid = send_telegram_message_full(token, chat_id, text,
                                               parse_mode)
    return ok, err


def start_ip_followup(get_ip, instance_name, token, chat_id,
                      timeout_seconds=None, interval_seconds=None,
                      on_ip=None):
    """IP 跟进线程：开机通知已发但 IP 还没分配时，后台继续等。

    get_ip 是无参回调（调用方传 lambda: get_public_ip(cli, id)）；
    每隔 interval 查一次，查到 IP 就补发一条 Telegram 消息即停；
    超时还没 IP 就静默放弃。daemon 线程，绝不抛异常。
    token / chat_id / get_ip 缺失时不启动，返回 None。
    on_ip 是可选回调：IP 查到时调用 on_ip(ip)，调用方用它把后到的
    IP 补记进实例凭据库；回调异常不影响跟进线程。
    返回启动的 Thread（测试用 join 等它结束），未启动返回 None。
    """
    if not callable(get_ip) or not token or chat_id is None:
        return None
    timeout = (IP_FOLLOWUP_MAX_SECONDS if timeout_seconds is None
               else timeout_seconds)
    interval = (IP_FOLLOWUP_INTERVAL_SECONDS if interval_seconds is None
                else interval_seconds)

    def _worker():
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    ip = get_ip()
                except Exception:  # noqa: BLE001 - 查询失败继续等
                    ip = None
                if ip:
                    send_telegram_message(
                        token, chat_id,
                        f"🌐 实例「{instance_name}」的公网 IP 已分配：{ip}")
                    if callable(on_ip):
                        try:
                            on_ip(ip)
                        except Exception:  # noqa: BLE001 - 回调异常不影响跟进
                            pass
                    return
                if time.monotonic() >= deadline:
                    return
                time.sleep(interval)
        except Exception:  # noqa: BLE001 - 跟进线程绝不能抛异常
            logger.warning("IP 跟进线程异常退出", exc_info=True)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def pin_telegram_message(token, chat_id, message_id):
    """置顶一条已发送的消息（1:1 私聊实测可用，2026-01-01）。

    返回 (ok, error)。置顶失败不影响消息本身（调用方只记日志）。
    """
    if not token or chat_id is None or message_id is None:
        return False, "Telegram token、chat_id 或 message_id 未配置"
    payload = {"chat_id": chat_id, "message_id": message_id,
               "disable_notification": True}
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/pinChatMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return False, f"Telegram HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if result.get("ok"):
        return True, ""
    return False, str(result.get("description") or "Telegram API 返回失败")


def cpu_type_label(shape):
    """形状 -> CPU 类型中文（A1 是 ARM，E2 是 AMD，其他显示形状名）。"""
    if shape == "VM.Standard.A1.Flex":
        return "ARM"
    if shape == "VM.Standard.E2.1.Micro":
        return "AMD"
    return shape or "未知"


def fmt_boot_success(profile_label, region_label, shape, ocpus,
                     memory_gb, disk_gb, instances, root_password=None,
                     region_code=None, ssh_key_set=False,
                     cpu_unverified=False, cpu_model=None):
    """开机成功通知全文（照 R 探长成功消息的字段结构）。

    instances：每台 {"name", "public_ip", "attempts", "elapsed_seconds"}。
    root_password 只有设置时才出现（密码行 + 末尾的注意行）。
    disk_gb 为 None 时硬盘行显示"默认"（镜像默认大小）。
    region_code 是区域代码（如 ap-seoul-1），有则与中文名一起显示。
    cpu_unverified 为 True 时追加一行 CPU 未验明提醒（抢机时 SSH
    查不到 CPU 型号，实例暂留未释放）。
    cpu_model: measured CPU model (e.g. "AMD EPYC 7J13"); when given, the
    cpu line shows it, otherwise it falls back to cpu_type_label(shape).
    cpu_model 有值时 cpu 类型行显示具体型号（如 "AMD EPYC 7J13"），
    否则回落到 cpu_type_label(shape) 的 AMD/ARM。
    """
    lines = ["🎉 恭喜，开机成功！"]
    lines.append(f"账号：{profile_label}")
    lines.append(f"cpu类型：{cpu_model or cpu_type_label(shape)}")
    lines.append(f"cpu数量：{ocpus} 核")
    lines.append(f"内存大小：{memory_gb} GB")
    disk_text = "默认" if disk_gb is None else f"{disk_gb} GB"
    lines.append(f"硬盘选择：{disk_text}")
    if root_password:
        lines.append(f"root密码：{root_password}")
    lines.append(f"公钥：{'已设置' if ssh_key_set else '未设置'}")
    if region_code and region_label and region_code != region_label:
        region_text = f"{region_code}（{region_label}）"
    else:
        region_text = region_code or region_label
    total = len(instances)
    if total == 1:
        inst = instances[0]
        lines.append(f"开机数量：第 1/1 个")
        lines.append(f"实例：{inst['name']}")
        lines.append(f"花费时间：{inst['elapsed_seconds']:.0f} 秒")
        lines.append(f"抢机次数：{inst['attempts']} 次")
        lines.append(f"开机地区：{region_text}")
        lines.append(
            f"ip：{inst.get('public_ip') or '获取中，稍后在实例列表查看'}")
    else:
        lines.append(f"开机地区：{region_text}")
        for i, inst in enumerate(instances, start=1):
            lines.append("")
            lines.append(f"第 {i}/{total} 个：{inst['name']}")
            lines.append(
                f"ip：{inst.get('public_ip') or '获取中，稍后在实例列表查看'}")
            lines.append(f"花费时间：{inst['elapsed_seconds']:.0f} 秒")
            lines.append(f"抢机次数：{inst['attempts']} 次")
    if cpu_unverified:
        lines.append("⚠️ CPU 未验明，请人工核对")
    if root_password:
        lines.append("注意📢：root密码机器人已保存，之后 SSH（开机后、DD 后）"
                     "会用它登录；请你也自己保存一份")
    return "\n".join(lines)


def fmt_notify_outcome(outcome):
    """通知结果的一行摘要（写进任务日志，方便追溯推送丢没丢）。

    outcome 是 notify_boot_success / notify_dual 的返回值
    {"telegram": bool, "bark": bool, "errors": [str]}。Bark 没发时
    分两种：errors 里有 Bark 错误是发送失败，没有就是未配置/未开
    启被跳过（notify_boot_success 不把未配置记进 errors）。
    """
    tg = "Telegram 已发送" if outcome.get("telegram") else "Telegram 发送失败"
    if outcome.get("bark"):
        bark = "Bark 已发送"
    elif any(str(e).startswith("Bark：")
             for e in outcome.get("errors", [])):
        bark = "Bark 发送失败"
    else:
        bark = "Bark 未配置，已跳过"
    return f"通知：{tg}，{bark}"


def notify_boot_success(token, chat_id, profile_label, region_label,
                        shape, ocpus, memory_gb, disk_gb, instances,
                        root_password=None, region_code=None,
                        ssh_key_set=False, cpu_unverified=False,
                        cpu_model=None):
    """开机成功双通道推送：Telegram + Bark（Bark 未开启时只推 TG）。

    返回 {"telegram": bool, "bark": bool, "errors": [str]}。
    绝不抛异常。
    """
    text = fmt_boot_success(profile_label, region_label, shape, ocpus,
                            memory_gb, disk_gb, instances,
                            root_password=root_password,
                            region_code=region_code,
                            ssh_key_set=ssh_key_set,
                            cpu_unverified=cpu_unverified,
                            cpu_model=cpu_model)
    result = {"telegram": False, "bark": False, "errors": []}
    try:
        ok, err = send_telegram_message(token, chat_id, text)
        result["telegram"] = ok
        if not ok:
            result["errors"].append(f"Telegram：{err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Telegram：{exc}")
    try:
        ok, err = send_bark("🎉 恭喜，开机成功！", text)
        result["bark"] = ok
        # Bark not enabled / not configured is a normal state, not an error. Bark 未开启/未配置是正常状态，不记为错误。
        if not ok and "未开启" not in err and "未配置" not in err:
            result["errors"].append(f"Bark：{err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Bark：{exc}")
    return result


def notify_dual(token, chat_id, title, body, group="m-bot", pin=False):
    """通用双通道推送：Telegram + Bark，返回同 notify_boot_success。

    pin=True 时把 Telegram 消息置顶（1:1 私聊实测可用）；置顶失败
    只记进 errors，不影响消息与 Bark 推送本身。
    """
    result = {"telegram": False, "bark": False, "errors": []}
    try:
        ok, err, message_id = send_telegram_message_full(
            token, chat_id, f"{title}\n\n{body}")
        result["telegram"] = ok
        if not ok:
            result["errors"].append(f"Telegram：{err}")
        elif pin:
            pinned, pin_err = pin_telegram_message(token, chat_id, message_id)
            if not pinned:
                result["errors"].append(f"置顶失败：{pin_err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Telegram：{exc}")
    try:
        ok, err = send_bark(title, body, group=group)
        result["bark"] = ok
        if not ok and "未开启" not in err and "未配置" not in err:
            result["errors"].append(f"Bark：{err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Bark：{exc}")
    return result


def fmt_paid_instance_alert(profile_label, region_label, shape, instances):
    """付费实例提醒正文（标题由 notify_dual 加）：实例名 + 形状 +
    公网 IP，提醒会产生费用。

    instances 是 [{name, public_ip}]（与 fmt_boot_success 的入参同形，
    只取名字与 IP 两个字段）。
    """
    lines = [
        f"账号：{profile_label}",
        f"地区：{region_label}",
        f"形状：{shape}（不在免费层内，会产生费用）",
        "",
    ]
    for inst in instances:
        ip = inst.get("public_ip") or "获取中"
        lines.append(f"• {inst.get('name') or '?'}：{ip}")
    lines.append("")
    lines.append("不需要请尽快停止或释放，避免持续扣费。")
    return "\n".join(lines)
