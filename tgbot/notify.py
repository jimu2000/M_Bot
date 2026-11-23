"""双通道通知（Telegram + Bark），daemon 线程里也能用。

Telegram 不走 python-telegram-bot 的事件循环，直接 HTTP 调
Bot API（sendMessage），所以抢机线程里也能发；Bark 走
tgbot.bark.send_bark（AES-256-CBC 加密推送）。
通知函数绝不抛异常（失败只返回错误，调用方线程不能因此死掉），
Token / Key / 密码绝不记日志、不出现在返回值里。
"""

import json
import logging
import urllib.error
import urllib.request

from tgbot.bark import send_bark

logger = logging.getLogger(__name__)

DEFAULT_BOOT_DISK_GB = 47


def send_telegram_message(token, chat_id, text, parse_mode=None):
    """直接 HTTP 调 Telegram Bot API 发一条消息，返回 (ok, error)。

    供 daemon 线程使用（无事件循环）。token 缺失时直接返回失败。
    """
    if not token or chat_id is None:
        return False, "Telegram token 或 chat_id 未配置"
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
                     memory_gb, disk_gb, instances, root_password=None):
    """开机成功通知全文（照 R 探长成功消息格式）。

    instances：每台 {"name", "public_ip", "attempts", "elapsed_seconds"}。
    root_password 只有设置时才出现（不设置不显示该行）。
    """
    lines = ["🎉 开机成功", ""]
    lines.append(f"账号：{profile_label}")
    lines.append(f"地区：{region_label}")
    lines.append(f"CPU：{cpu_type_label(shape)} {ocpus} 核")
    lines.append(f"内存：{memory_gb} GB")
    lines.append(f"硬盘：{disk_gb} GB")
    if len(instances) == 1:
        inst = instances[0]
        lines.append(f"实例：{inst['name']}")
        lines.append(f"抢机次数：{inst['attempts']} 次")
        lines.append(f"花费时间：{inst['elapsed_seconds']:.1f} 秒")
        lines.append(f"公网 IP：{inst.get('public_ip') or '获取中，稍后在实例列表查看'}")
    else:
        lines.append(f"抢到 {len(instances)} 台：")
        for inst in instances:
            lines.append(
                f"• {inst['name']}：{inst.get('public_ip') or 'IP 获取中'}，"
                f"{inst['attempts']} 次，{inst['elapsed_seconds']:.1f} 秒")
    if root_password:
        lines.append(f"root 密码：{root_password}")
    return "\n".join(lines)


def notify_boot_success(token, chat_id, profile_label, region_label,
                        shape, ocpus, memory_gb, disk_gb, instances,
                        root_password=None):
    """开机成功双通道推送：Telegram + Bark（Bark 未开启时只推 TG）。

    返回 {"telegram": bool, "bark": bool, "errors": [str]}。
    绝不抛异常。
    """
    text = fmt_boot_success(profile_label, region_label, shape, ocpus,
                            memory_gb, disk_gb, instances,
                            root_password=root_password)
    result = {"telegram": False, "bark": False, "errors": []}
    try:
        ok, err = send_telegram_message(token, chat_id, text)
        result["telegram"] = ok
        if not ok:
            result["errors"].append(f"Telegram：{err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Telegram：{exc}")
    try:
        ok, err = send_bark("🎉 开机成功", text)
        result["bark"] = ok
        # Bark 未开启/未配置是正常状态，不记为错误。
        if not ok and "未开启" not in err and "未配置" not in err:
            result["errors"].append(f"Bark：{err}")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Bark：{exc}")
    return result


def notify_dual(token, chat_id, title, body, group="m-bot"):
    """通用双通道推送：Telegram + Bark，返回同 notify_boot_success。"""
    result = {"telegram": False, "bark": False, "errors": []}
    try:
        ok, err = send_telegram_message(token, chat_id, f"{title}\n\n{body}")
        result["telegram"] = ok
        if not ok:
            result["errors"].append(f"Telegram：{err}")
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
