#!/usr/bin/env python3
"""Telegram 客户端工具（阶段 5 工作包 B）。

顶层命令（bot.py 注册）：
  - /command   远程执行 Shell：选已保存主机 -> 发命令文本 -> Paramiko 执行回显
  - /pubkey    SSH 公钥管理：列出 / 添加 / 删除（只存公钥，绝不存私钥）
  - /oproxy    机器人出站代理设置（设置后需重启机器人服务才生效）
  - /uploadapi 添加 OCI API：发 config 文件或粘贴文本，校验后写入 ~/.oci/config

/oracle 菜单项（handlers_extra._DISPATCH 转发到本模块）：
  - 23. 占用 25% 内存（memballoon）：选实例 -> 二次确认 -> SSH 启动内存占用进程
  - 24. 清除缓存和占用（memclear）：选实例 -> 二次确认 -> 杀占用进程 + 清缓存

路由（callback_data 前缀，见 bot.py 注册）：
  - "cmd:host:<idx>" / "cmd:done"  /command 选主机 / 结束会话
  - "pk:add" / "pk:del:<idx>" / "pk:confirmdel:<idx>"  /pubkey
  - "px:set" / "px:clear" / "px:confirmclear"  /oproxy
  - "ua:confirm"  /uploadapi 二次确认
  - "mh:pick:<idx>" / "mh:go:<idx>"  菜单 23 选实例 / 确认执行
  - "mc:pick:<idx>" / "mc:go:<idx>"  菜单 24 选实例 / 确认执行
  - "x" 取消：复用 handlers.cancel_callback（bot.py 已注册）

待输入状态存在 context.user_data 里（cmd_awaiting / pk_awaiting /
px_awaiting / ua_awaiting），纯文本消息由 bot.py 注册的
handle_client_text_input 按状态分发；文件消息由 handle_client_document
处理（只在 ua_awaiting 时接）。

安全说明：
  - 所有 handler 都加 @require_allowed，白名单外用户直接拒绝。
  - /command 有危险命令黑名单（见配置区 DANGEROUS_COMMAND_PATTERNS），
    命中直接拒绝执行；输出超长截断 + 分段（Telegram 单条 4096 字符上限）。
  - 写操作（删公钥、清代理、写 ~/.oci/config、启停占用进程）都有二次确认
    或明确的确认步骤。
  - 本文件不含真实 OCID / Token / IP / 私钥；测试用 203.0.113.x 与假公钥。
  - 主机列表复用 Web 面板的主机存储（DATA_DIR/hosts.json，
    格式见 web/routes/hosts.py）；密码认证的主机无法用 /command
    （密码不存文件），需改成密钥认证或用 Web SSH。
"""

import base64
import configparser
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from oci.instance import list_instances
from oci.network import get_public_ip
from tgbot.services import (
    fmt_error,
    get_cli,
    get_compartment,
    require_allowed,
    resolve_instance,
    run_sync,
    store_instances,
)

logger = logging.getLogger(__name__)

# ================= 配置区（改这里） =================
# 数据目录：与 Web 面板共用（hosts.json / ssh_keys.json / proxy.json /
# mem_hog.json 都放这里）。web.config 不可用时用默认路径。
try:
    from web import config as _web_config

    DATA_DIR = _web_config.DATA_DIR
except Exception:  # noqa: BLE001 - web.config 不可用时回退默认路径
    DATA_DIR = os.path.expanduser("~/workspace/rbot-clone/web/data")

# 各数据文件路径（测试可直接 patch 这些模块级变量指向临时目录）。
HOSTS_FILE = os.path.join(DATA_DIR, "hosts.json")
SSH_KEYS_FILE = os.path.join(DATA_DIR, "ssh_keys.json")
PROXY_FILE = os.path.join(DATA_DIR, "proxy.json")
MEM_HOG_FILE = os.path.join(DATA_DIR, "mem_hog.json")
KNOWN_HOSTS_FILE = os.path.join(DATA_DIR, "known_hosts")
# OCI config 文件路径（/uploadapi 写入目标）。
OCI_CONFIG_PATH = os.path.expanduser("~/.oci/config")

# ---- /command ----
# 远程命令执行超时（秒），超时后终止并提示用户。
COMMAND_TIMEOUT_SECONDS = 30
# SSH 连接超时（秒）。
SSH_CONNECT_TIMEOUT = 10
# Telegram 单条消息上限 4096 字符；命令输出按此长度分段（留余量）。
COMMAND_OUTPUT_CHUNK_CHARS = 4000
# 命令输出总上限（字符）：超过则截断并提示，避免一次刷屏几十条。
MAX_OUTPUT_TOTAL_CHARS = 20000
# 危险命令黑名单：(提示名, 正则)，大小写不敏感，命中任一直接拒绝执行。
# 增删规则时直接改这个列表（正则用 re.search 匹配）。
DANGEROUS_COMMAND_PATTERNS = [
    ("删除根目录（rm -rf /）",
     r"\brm\s+(-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+/(?:\*|[\s;|&]|$)"),
    ("格式化磁盘（mkfs）", r"\bmkfs\b"),
    ("dd 写盘（dd if=）", r"\bdd\s+[^\n;|&]*\bif="),
    ("Fork 炸弹（:(){:|:&};:）", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}"),
    ("关机（shutdown）", r"\bshutdown\b"),
    ("重启（reboot）", r"\breboot\b"),
    ("关机（init 0）", r"\binit\s+0\b"),
    ("关机（halt）", r"\bhalt\b"),
    ("关机（poweroff）", r"\bpoweroff\b"),
    ("写块设备（> /dev/sd）", r">\s*/dev/sd[a-z0-9]*"),
]

# ---- /pubkey ----
# 允许的 SSH 公钥类型前缀（OpenSSH 公钥第一列）。
SSH_KEY_TYPES = ("ssh-rsa", "ssh-ed25519",
                 "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                 "ecdsa-sha2-nistp521", "ssh-dss")

# ---- /oproxy ----
# 允许的代理协议前缀。
PROXY_SCHEMES = ("http://", "https://", "socks5://")

# ---- /uploadapi ----
# OCI config 必填键（缺任一拒绝写入并说明缺什么）。
REQUIRED_OCI_KEYS = ("user", "fingerprint", "tenancy", "region", "key_file")
# profile 名合法字符（字母/数字/下划线/点/横线）。
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

# ---- 菜单 23/24：内存占用 ----
# 占用比例：总内存的 25%。
MEMHOG_FRACTION = 0.25
# 占用进程持有时长（秒，10**9 秒约 31 年，相当于永久持有）。
MEMHOG_SLEEP_SECONDS = 10 ** 9
# 占用进程的日志文件（远程主机上）。
MEMHOG_LOG_PATH = "/tmp/rbot_memhog.log"
# 实例 SSH 默认值（hosts.json 里按 IP 匹配不到已保存主机时用）：
# 用户名 / 端口 / 密钥（面板服务器上的默认密钥，需已加到实例上）。
# 端口与密钥可由环境变量覆盖：RBOT_MEMHOG_SSH_PORT（默认 22，
# 生产实例用 14422）、RBOT_MEMHOG_SSH_KEY（默认 ~/.ssh/id_ed25519；
# 面板服务器上没有该私钥时必须设，指向部署用的专用密钥）。
MEMHOG_SSH_USER = "root"
try:
    MEMHOG_SSH_PORT = int(os.environ.get("RBOT_MEMHOG_SSH_PORT", "22"))
except ValueError:
    MEMHOG_SSH_PORT = 22
MEMHOG_SSH_KEY = os.path.expanduser(
    os.environ.get("RBOT_MEMHOG_SSH_KEY", "~/.ssh/id_ed25519"))
# =====================================================


# ---------------- 通用小工具 ----------------

async def _reply(update, text, reply_markup=None, parse_mode=None):
    """统一回复：回调时先 answer（防按钮转圈），再用消息回复。"""
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, reply_markup=reply_markup,
                                 parse_mode=parse_mode)


def _callback_data(update):
    """取回调的 callback_data；不是回调时返回空字符串。"""
    query = update.callback_query
    return query.data if query is not None else ""


def _clear_pending(context):
    """清掉全部待输入状态（进新流程时调用，避免旧状态误触发）。

    也清 handlers_extra.text_input_callback 的 pending_input（改名/重装）：
    用户显式开始一个新流程时，旧的待输入流程视为取消，避免同一条
    文本被两个流程同时消费。
    """
    for key in ("cmd_awaiting", "cmd_host_idx", "cmd_hosts",
                "pk_awaiting", "px_awaiting",
                "ua_awaiting", "ua_pending",
                "pending_input"):
        context.user_data.pop(key, None)


# ---------------- 已保存主机（复用 Web 面板 hosts.json） ----------------

def load_hosts(path=None):
    """读 DATA_DIR/hosts.json，返回主机记录列表；文件不存在返回空列表。

    格式与 web/routes/hosts.py 一致：list，每条记录含 id / name / host /
    port / username / auth_type / key_path 等字段。
    """
    path = path or HOSTS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("hosts", [])
    return data if isinstance(data, list) else []


# ================= /command 远程执行 Shell =================

def find_dangerous_pattern(command):
    """检查命令是否命中危险黑名单；命中返回提示名，未命中返回 None。"""
    for name, pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, command or "", re.IGNORECASE):
            return name
    return None


def chunk_output(text, chunk_chars=COMMAND_OUTPUT_CHUNK_CHARS,
                 max_total=MAX_OUTPUT_TOTAL_CHARS):
    """命令输出截断 + 分段：超 max_total 截断并加提示，再按 chunk_chars 切分。"""
    if len(text) > max_total:
        text = text[:max_total] + "\n…（输出过长，已截断）"
    if not text:
        return [""]
    return [text[i:i + chunk_chars]
            for i in range(0, len(text), chunk_chars)]


class RemoteCommandError(Exception):
    """远程命令执行失败（连接 / 认证 / 超时以外的错误）。"""


class CommandTimeoutError(RemoteCommandError):
    """远程命令执行超时。"""


def _new_ssh_client():
    """新建一个配好 host key 策略的 SSHClient。

    每次连接尝试都用新的 client：失败的连接可能留下半开的
    Transport，复用同一个 client 重试可能直接报错。
    """
    fresh = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_FILE):
        try:
            fresh.load_host_keys(KNOWN_HOSTS_FILE)
        except Exception as exc:
            logger.warning("known_hosts 读取失败：%s", exc)
    fresh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return fresh


def run_remote_command(host, command, timeout=COMMAND_TIMEOUT_SECONDS):
    """在远程主机上执行一条命令，返回 (exit_code, stdout, stderr)。

    host：主机记录 dict，含 host / port / username / key_path（可选）。
    连接模式与 web/ssh.py 一致：known_hosts 存在则加载，
    AutoAddPolicy 记住新 host key；key_path 存在时用密钥认证，
    否则走 agent / 默认密钥查找（密码不存文件，故不支持密码认证）。
    端口回退：主端口连接失败（超时 / 拒绝 / 无可用连接）时，
    在 22 与 14422 之间重试一次（新实例默认 22，生产实例用
    14422；其他端口不回退）。认证失败不重试，直接报错。
    连接/认证失败抛 RemoteCommandError；命令执行超时抛 CommandTimeoutError。
    """
    hostname = host.get("host")
    port = host.get("port") or 22
    username = host.get("username") or "root"
    key_path = host.get("key_path")
    if key_path:
        key_path = os.path.expanduser(key_path)
        if not os.path.exists(key_path):
            raise RemoteCommandError(f"密钥文件不存在：{key_path}")
    kwargs = {
        "hostname": hostname,
        "username": username,
        "timeout": SSH_CONNECT_TIMEOUT,
        "banner_timeout": SSH_CONNECT_TIMEOUT,
    }
    if key_path:
        kwargs["key_filename"] = key_path
        kwargs["look_for_keys"] = False
    ports = [port]
    if port == 22:
        ports.append(14422)
    elif port == 14422:
        ports.append(22)
    client = None
    for attempt, try_port in enumerate(ports):
        attempt_client = _new_ssh_client()
        try:
            attempt_client.connect(**kwargs, port=try_port)
        except paramiko.AuthenticationException:
            attempt_client.close()
            raise RemoteCommandError("SSH 认证失败：用户名或密钥不正确。")
        except (socket.timeout, OSError, NoValidConnectionsError) as exc:
            attempt_client.close()
            if attempt + 1 < len(ports):
                logger.warning(
                    "SSH 连接 %s:%s 失败（%s），尝试备用端口 %s",
                    hostname, try_port, exc, ports[attempt + 1])
                continue
            raise RemoteCommandError(f"SSH 连接失败：{exc}")
        except paramiko.SSHException as exc:
            attempt_client.close()
            raise RemoteCommandError(f"SSH 连接失败：{exc}")
        client = attempt_client
        break
    try:
        try:
            parent = os.path.dirname(KNOWN_HOSTS_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            client.save_host_keys(KNOWN_HOSTS_FILE)
        except Exception as exc:
            logger.warning("known_hosts 保存失败（不影响连接）：%s", exc)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        try:
            channel = stdout.channel
            deadline = time.monotonic() + timeout
            while not channel.exit_status_ready():
                if time.monotonic() > deadline:
                    try:
                        channel.close()
                    except Exception:
                        pass
                    raise CommandTimeoutError(
                        f"命令执行超时（{timeout} 秒），已终止。")
                time.sleep(0.05)
            exit_code = channel.recv_exit_status()
            out = stdout.read()
            err = stderr.read()
        finally:
            for stream in (stdin, stdout, stderr):
                try:
                    stream.close()
                except Exception:
                    pass
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        return exit_code, out or "", err or ""
    except CommandTimeoutError:
        raise
    except paramiko.AuthenticationException:
        raise RemoteCommandError("SSH 认证失败：用户名或密钥不正确。")
    except (paramiko.SSHException, OSError, socket.timeout) as exc:
        raise RemoteCommandError(f"SSH 连接失败：{exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass


def host_select_keyboard(hosts):
    """主机选择键盘：每台一个按钮，callback 带下标。"""
    rows = []
    for idx, record in enumerate(hosts):
        label = record.get("name") or record.get("host") or f"主机 {idx + 1}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"cmd:host:{idx}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


@require_allowed
async def command_command(update, context):
    """处理 /command：列出已保存主机，选一台后进入命令输入。"""
    _clear_pending(context)
    hosts = load_hosts()
    if not hosts:
        await _reply(update,
                     "还没有已保存的主机。\n"
                     "请先在 Web 面板（主机管理）里添加主机，"
                     "再用 /command 远程执行命令。")
        return
    context.user_data["cmd_hosts"] = hosts
    lines = ["🖥️ 远程执行命令：选一台主机：", ""]
    for idx, record in enumerate(hosts):
        name = record.get("name") or record.get("host")
        lines.append(f"{idx + 1}. {name}（{record.get('username', 'root')}"
                     f"@{record.get('host')}:{record.get('port', 22)}）")
    await _reply(update, "\n".join(lines),
                 reply_markup=host_select_keyboard(hosts))


@require_allowed
async def command_host_callback(update, context):
    """回调 cmd:host:<idx>：选中主机，进入命令输入；cmd:done：结束会话。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "done":
        _clear_pending(context)
        await _reply(update, "已结束远程执行会话。")
        return
    if action != "host":
        await _reply(update, "参数错误，请重新发送 /command。")
        return
    idx = parts[2] if len(parts) >= 3 else ""
    hosts = context.user_data.get("cmd_hosts") or []
    try:
        host = hosts[int(idx)]
    except (ValueError, IndexError, TypeError):
        host = None
    if host is None:
        await _reply(update, "主机不存在或已过期，请重新发送 /command。")
        return
    if host.get("auth_type") == "password":
        await _reply(update,
                     f"⚠️ 主机「{host.get('name') or host.get('host')}」"
                     "是密码认证，机器人不保存密码，无法用 /command 连接。\n"
                     "请在 Web 面板里把该主机改成密钥认证，"
                     "或用 Web SSH（连接时现输密码）。")
        return
    context.user_data["cmd_host_idx"] = int(idx)
    context.user_data["cmd_awaiting"] = True
    await _reply(
        update,
        f"🖥️ 已选主机「{host.get('name') or host.get('host')}」\n"
        "请直接发送要执行的命令（纯文本）。\n"
        "危险命令（删根目录、格式化、关机等）会被直接拒绝。\n"
        "执行完可继续发送下一条命令，点下面按钮结束会话。",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🛑 结束会话", callback_data="cmd:done")]]))


async def _handle_command_text(update, context, text):
    """待输入状态下的命令文本：黑名单检查 -> Paramiko 执行 -> 分段回显。"""
    command = (text or "").strip()
    if not command:
        await _reply(update, "命令为空，请重新发送。")
        return
    hosts = context.user_data.get("cmd_hosts") or []
    idx = context.user_data.get("cmd_host_idx")
    host = None
    if isinstance(idx, int) and 0 <= idx < len(hosts):
        host = hosts[idx]
    if host is None:
        context.user_data.pop("cmd_awaiting", None)
        await _reply(update, "主机选择已过期，请重新发送 /command。")
        return
    blocked = find_dangerous_pattern(command)
    if blocked:
        await _reply(update,
                     f"⛔ 危险命令，已拒绝执行：{blocked}\n"
                     "请发送其他命令，或点“结束会话”。")
        return
    name = host.get("name") or host.get("host")
    await _reply(update, f"⏳ 正在 {name} 上执行：{command}")
    try:
        exit_code, stdout, stderr = await run_sync(
            run_remote_command, host, command)
    except CommandTimeoutError as exc:
        await _reply(update, f"⏰ {exc}")
        return
    except RemoteCommandError as exc:
        await _reply(update, f"❌ 执行失败：{exc}")
        return
    output = stdout or ""
    if (stderr or "").strip():
        output += ("\n" if output else "") + "[stderr]\n" + stderr
    if not output.strip():
        output = "（命令无输出）"
    await _reply(update, f"🖥️ {name} $ {command}\n退出码：{exit_code}")
    for chunk in chunk_output(output):
        await _reply(update, chunk)


# ================= /pubkey SSH 公钥管理 =================

def validate_ssh_public_key(text):
    """校验 OpenSSH 公钥格式，返回 (ok, error)。

    要求：类型在 SSH_KEY_TYPES 里 + base64 部分可解码 + 解码后的
    blob 内嵌类型与声明一致 + 可选注释。私钥内容直接拒绝并明确提示。
    """
    text = (text or "").strip()
    if not text:
        return False, "公钥内容为空。"
    if "PRIVATE KEY" in text:
        return False, "这是私钥！绝不能上传私钥，只传 .pub 公钥内容。"
    parts = text.split()
    if len(parts) < 2:
        return False, ("格式不对：应为“类型 base64 [注释]”，"
                       "例如 ssh-ed25519 AAAA... user@host。")
    key_type, blob = parts[0], parts[1]
    if key_type not in SSH_KEY_TYPES:
        return False, (f"不支持的密钥类型：{key_type}"
                       f"（支持：{', '.join(SSH_KEY_TYPES)}）。")
    try:
        decoded = base64.b64decode(blob, validate=True)
    except Exception:
        return False, "base64 部分无效，不是合法的公钥。"
    if len(decoded) < 4:
        return False, "公钥太短，看起来不完整。"
    inner_len = int.from_bytes(decoded[:4], "big")
    inner_type = decoded[4:4 + inner_len].decode("ascii", errors="replace")
    if inner_type != key_type:
        return False, "公钥内容与声明的类型不符。"
    return True, None


def load_ssh_keys(path=None):
    """读 ssh_keys.json，返回公钥记录列表；文件不存在或损坏返回 []。

    每条记录：{"key": "ssh-ed25519 AAAA... 注释", "added_at": "ISO 时间"}。
    只存公钥，绝不存私钥。
    """
    path = path or SSH_KEYS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_ssh_keys(keys, path=None):
    """把公钥列表写回 ssh_keys.json，返回 keys。"""
    path = path or SSH_KEYS_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)
    return keys


def add_ssh_key(key_text, path=None):
    """追加一条公钥（去重），返回 (keys, added_bool)。"""
    keys = load_ssh_keys(path)
    normalized = " ".join((key_text or "").split())
    for entry in keys:
        if entry.get("key") == normalized:
            return keys, False
    keys.append({
        "key": normalized,
        "added_at": datetime.now(timezone.utc).isoformat(),
    })
    save_ssh_keys(keys, path)
    return keys, True


def remove_ssh_key(idx, path=None):
    """按下标删除一条公钥，返回 (keys, removed_entry|None)。"""
    keys = load_ssh_keys(path)
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return keys, None
    if 0 <= idx < len(keys):
        removed = keys.pop(idx)
        save_ssh_keys(keys, path)
        return keys, removed
    return keys, None


def fmt_ssh_key_line(entry, idx):
    """公钥列表里的一行：序号 + 类型 + base64 尾 8 位 + 注释（不贴完整公钥）。"""
    parts = (entry.get("key") or "").split()
    key_type = parts[0] if parts else "?"
    blob = parts[1] if len(parts) > 1 else ""
    comment = " ".join(parts[2:]) if len(parts) > 2 else ""
    label = f"{key_type} …{blob[-8:]}" if blob else key_type
    if comment:
        label += f"（{comment}）"
    return f"{idx + 1}. {label}"


def fmt_ssh_key_list(keys):
    """公钥列表全文。"""
    if not keys:
        return ("🔑 SSH 公钥管理：\n\n还没有保存的公钥。\n"
                "点“➕ 添加公钥”粘贴公钥内容（只存公钥，绝不存私钥）。")
    lines = [f"🔑 SSH 公钥管理（{len(keys)} 个）：", ""]
    lines.extend(fmt_ssh_key_line(entry, idx)
                 for idx, entry in enumerate(keys))
    lines.append("")
    lines.append("点“➕ 添加公钥”新增，点某条后面的删除按钮删除（需二次确认）。")
    return "\n".join(lines)


def pubkey_list_keyboard(keys):
    """公钥列表键盘：添加按钮 + 每条一个删除按钮。"""
    rows = [[InlineKeyboardButton("➕ 添加公钥", callback_data="pk:add")]]
    for idx, entry in enumerate(keys):
        parts = (entry.get("key") or "").split()
        label = parts[0] if parts else "?"
        if len(parts) > 2:
            label += f" {' '.join(parts[2:])}"
        elif len(parts) > 1:
            label += f" …{parts[1][-8:]}"
        rows.append([InlineKeyboardButton(
            f"🗑️ 删除 {label}", callback_data=f"pk:del:{idx}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


@require_allowed
async def pubkey_command(update, context):
    """处理 /pubkey：列出已保存公钥 + 添加/删除按钮。"""
    _clear_pending(context)
    keys = load_ssh_keys()
    await _reply(update, fmt_ssh_key_list(keys),
                 reply_markup=pubkey_list_keyboard(keys))


@require_allowed
async def pubkey_callback(update, context):
    """回调 pk:add / pk:del:<idx> / pk:confirmdel:<idx>。

    删除分两步：先弹二次确认，点确认后才真正删除。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "add":
        context.user_data["pk_awaiting"] = True
        await _reply(update,
                     "请直接发送 SSH 公钥内容（纯文本，一行）：\n"
                     "例如：ssh-ed25519 AAAA... user@host\n\n"
                     "⚠️ 只传公钥（.pub 文件内容），绝不要发私钥！")
        return
    if action in ("del", "confirmdel"):
        idx = parts[2] if len(parts) >= 3 else ""
        keys = load_ssh_keys()
        try:
            entry = keys[int(idx)]
            idx_int = int(idx)
        except (ValueError, IndexError, TypeError):
            entry, idx_int = None, 0
        if entry is None:
            await _reply(update, "公钥不存在或已过期，请重新发送 /pubkey。")
            return
        if action == "del":
            await _reply(
                update,
                f"⚠️ 确认删除这条公钥？\n\n{fmt_ssh_key_line(entry, idx_int)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🗑️ 确认删除",
                        callback_data=f"pk:confirmdel:{idx}")],
                    [InlineKeyboardButton("❌ 取消", callback_data="x")]]))
            return
        _keys, removed = remove_ssh_key(idx)
        if removed is None:
            await _reply(update, "公钥不存在或已删除。")
        else:
            await _reply(update,
                         f"✅ 已删除公钥：\n{fmt_ssh_key_line(removed, idx_int)}")
        return
    await _reply(update, "参数错误，请重新发送 /pubkey。")


async def _handle_pubkey_text(update, context, text):
    """待输入状态下的公钥文本：校验格式 -> 保存（去重）。"""
    ok, error = validate_ssh_public_key(text)
    if not ok:
        await _reply(update, f"❌ 公钥格式不对：{error}\n请重新发送，或发 /pubkey 返回。")
        return
    _keys, added = add_ssh_key(text)
    context.user_data.pop("pk_awaiting", None)
    if not added:
        await _reply(update, "⚠️ 这条公钥已保存过，未重复添加。")
    else:
        await _reply(update, "✅ 公钥已保存。")


# ================= /oproxy 代理设置 =================

def validate_proxy_url(url):
    """校验代理地址，返回 (ok, error)。必须以 http/https/socks5 开头。"""
    url = (url or "").strip()
    if not url:
        return False, "代理地址为空。"
    if not url.lower().startswith(PROXY_SCHEMES):
        return False, "代理地址必须以 http://、https:// 或 socks5:// 开头。"
    return True, None


def load_proxy(path=None):
    """读 proxy.json，返回当前代理地址；未设置返回 None。"""
    path = path or PROXY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        url = data.get("proxy_url")
        return url if isinstance(url, str) and url.strip() else None
    return None


def save_proxy(proxy_url, path=None):
    """写 proxy.json：有地址写 {"proxy_url": ...}，None/空表示清除（写 {}）。"""
    path = path or PROXY_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = {"proxy_url": proxy_url} if proxy_url else {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def proxy_keyboard(current):
    """代理设置键盘：设置按钮；已设置时多一个清除按钮。"""
    rows = [[InlineKeyboardButton("✏️ 设置代理", callback_data="px:set")]]
    if current:
        rows.append([InlineKeyboardButton("🚫 清除代理",
                                          callback_data="px:clear")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


@require_allowed
async def oproxy_command(update, context):
    """处理 /oproxy：查看当前代理 + 设置/清除按钮。"""
    _clear_pending(context)
    current = load_proxy()
    if current:
        text = (f"🌐 机器人出站代理：\n\n当前：{current}\n\n"
                "设置后需重启机器人服务才生效。")
    else:
        text = ("🌐 机器人出站代理：\n\n当前：未设置（直连）\n\n"
                "设置后需重启机器人服务才生效。")
    await _reply(update, text, reply_markup=proxy_keyboard(current))


@require_allowed
async def oproxy_callback(update, context):
    """回调 px:set / px:clear / px:confirmclear。

    清除分两步：先弹二次确认，点确认后才真正清除。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "set":
        context.user_data["px_awaiting"] = True
        await _reply(update,
                     "请直接发送代理地址（纯文本）：\n"
                     "例如：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080\n\n"
                     "必须以 http://、https:// 或 socks5:// 开头。")
        return
    if action == "clear":
        current = load_proxy()
        if not current:
            await _reply(update, "当前未设置代理，无需清除。")
            return
        await _reply(
            update,
            f"⚠️ 确认清除出站代理？\n\n当前：{current}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 确认清除",
                                      callback_data="px:confirmclear")],
                [InlineKeyboardButton("❌ 取消", callback_data="x")]]))
        return
    if action == "confirmclear":
        save_proxy(None)
        await _reply(update, "✅ 出站代理已清除（直连）。\n⚠️ 重启机器人服务后生效。")
        return
    await _reply(update, "参数错误，请重新发送 /oproxy。")


async def _handle_proxy_text(update, context, text):
    """待输入状态下的代理地址文本：校验协议 -> 保存。"""
    url = (text or "").strip()
    ok, error = validate_proxy_url(url)
    if not ok:
        await _reply(update, f"❌ {error}\n请重新发送，或发 /oproxy 返回。")
        return
    save_proxy(url)
    context.user_data.pop("px_awaiting", None)
    await _reply(update, f"✅ 出站代理已设置为：{url}\n⚠️ 重启机器人服务后生效。")


# ================= /uploadapi 添加 API =================

def parse_oci_config_text(text):
    """解析用户上传的 OCI config 文本。

    返回 {"profile": str|None, "fields": dict, "missing": [str],
          "error": str|None, "extra_profiles": [str]}。
    成功时 error 为 None；missing 列出缺的必填键（REQUIRED_OCI_KEYS）；
    多个 section 时只取第一个，其余名字放 extra_profiles 提示用户。
    """
    result = {"profile": None, "fields": {}, "missing": [],
              "error": None, "extra_profiles": []}
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        result["error"] = "内容为空。"
        return result
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        result["error"] = "没有找到 [PROFILE] 段。"
        return result
    except configparser.Error as exc:
        result["error"] = f"无法解析为 config 格式：{exc}"
        return result
    sections = parser.sections()
    if not sections:
        result["error"] = "没有找到 [PROFILE] 段。"
        return result
    profile = sections[0]
    if not PROFILE_NAME_RE.match(profile):
        result["error"] = (f"profile 名「{profile}」不合法，"
                           "只能含字母、数字、下划线、点、横线。")
        return result
    result["profile"] = profile
    result["extra_profiles"] = sections[1:]
    fields = {key: value.strip()
              for key, value in parser.items(profile)}
    result["fields"] = fields
    result["missing"] = [key for key in REQUIRED_OCI_KEYS
                         if not fields.get(key)]
    return result


def profile_exists_in_config(profile, path=None):
    """~/.oci/config 里是否已有同名 profile。"""
    path = path or OCI_CONFIG_PATH
    if not os.path.exists(path):
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return False
    return parser.has_section(profile)


def append_oci_profile(profile, fields, path=None):
    """把新 profile 追加到 ~/.oci/config（已有内容原样不动）。

    同名 profile 已存在时抛 ValueError，不覆盖。
    返回写入的文件路径。
    """
    path = path or OCI_CONFIG_PATH
    if profile_exists_in_config(profile, path):
        raise ValueError(f"profile「{profile}」已存在，请改名后重传。")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    lines = [f"[{profile}]"]
    for key in REQUIRED_OCI_KEYS:
        if key in fields:
            lines.append(f"{key} = {fields[key]}")
    for key, value in fields.items():
        if key not in REQUIRED_OCI_KEYS:
            lines.append(f"{key} = {value}")
    block = "\n".join(lines) + "\n"
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(sep + block)
    return path


@require_allowed
async def uploadapi_command(update, context):
    """处理 /uploadapi：提示发 config 文件或粘贴文本，进入等待状态。"""
    _clear_pending(context)
    context.user_data["ua_awaiting"] = True
    await _reply(update,
                 "📤 添加 OCI API 配置：\n\n"
                 "请发送 config 文件（.txt / .conf），或直接粘贴 config 文本。\n\n"
                 "格式要求：含 [PROFILE] 段 + user / fingerprint / tenancy / "
                 "region / key_file 五个键。\n\n"
                 "⚠️ 只传 config 文本；私钥文件（key_file 指向的 .pem）"
                 "不经过 Telegram，请自行 scp 到服务器对应路径。")


async def _handle_uploadapi_text(update, context, text):
    """待输入状态下的 config 文本：解析 -> 校验 -> 二次确认键盘。"""
    parsed = parse_oci_config_text(text)
    if parsed["error"]:
        await _reply(update, f"❌ 配置格式不对：{parsed['error']}\n请重新发送。")
        return
    if parsed["missing"]:
        missing = "、".join(parsed["missing"])
        await _reply(update, f"❌ 配置缺少必填键：{missing}\n请补全后重新发送。")
        return
    profile = parsed["profile"]
    if profile_exists_in_config(profile):
        context.user_data.pop("ua_awaiting", None)
        await _reply(update,
                     f"⚠️ profile「{profile}」已存在，不会覆盖。\n"
                     "请改名后重传（改 [PROFILE] 段名）。")
        return
    context.user_data["ua_pending"] = {"profile": profile,
                                       "fields": parsed["fields"]}
    fields = parsed["fields"]
    user_tail = (fields.get("user") or "")[-4:]
    lines = ["📤 确认添加 OCI API 配置？", "",
             f"profile：{profile}",
             f"区域：{fields.get('region')}",
             f"用户：…{user_tail}",
             f"key_file：{fields.get('key_file')}"]
    if parsed["extra_profiles"]:
        lines.append("")
        lines.append(f"注意：文件里还有其他 profile"
                     f"（{', '.join(parsed['extra_profiles'])}），"
                     "本次只添加第一个。")
    lines.append("")
    lines.append("⚠️ 私钥文件请自行 scp 到服务器 key_file 指向的路径。")
    await _reply(update, "\n".join(lines),
                 reply_markup=InlineKeyboardMarkup([
                     [InlineKeyboardButton("✅ 确认添加",
                                           callback_data="ua:confirm")],
                     [InlineKeyboardButton("❌ 取消", callback_data="x")]]))


@require_allowed
async def uploadapi_callback(update, context):
    """回调 ua:confirm：二次确认后真正写入 ~/.oci/config。"""
    if _callback_data(update) != "ua:confirm":
        await _reply(update, "参数错误，请重新发送 /uploadapi。")
        return
    pending = context.user_data.get("ua_pending")
    if not pending:
        await _reply(update, "待添加的配置已过期，请重新发送 /uploadapi。")
        return
    profile = pending["profile"]
    try:
        await run_sync(append_oci_profile, profile, pending["fields"])
    except ValueError as exc:
        await _reply(update, f"❌ {exc}")
        return
    except OSError as exc:
        await _reply(update, f"❌ 写入失败：{exc}")
        return
    context.user_data.pop("ua_pending", None)
    context.user_data.pop("ua_awaiting", None)
    await _reply(update,
                 f"✅ 已添加 profile「{profile}」到 ~/.oci/config。\n"
                 "⚠️ 别忘了把私钥文件 scp 到服务器："
                 f"{pending['fields'].get('key_file')}")


@require_allowed
async def handle_client_document(update, context):
    """文件消息路由：只在等待上传 config 时处理（/uploadapi 流程）。

    其余情况静默返回（不回复，避免干扰正常聊天）。
    """
    if not context.user_data.get("ua_awaiting"):
        return
    message = update.effective_message
    document = getattr(message, "document", None) if message else None
    if document is None:
        return
    try:
        tg_file = await document.get_file()
        raw = await tg_file.download_as_bytearray()
        text = bytes(raw).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 下载/解码失败都要提示用户
        await _reply(update, f"❌ 文件读取失败：{exc}")
        return
    await _handle_uploadapi_text(update, context, text)


@require_allowed
async def handle_client_text_input(update, context):
    """纯文本消息路由：按 user_data 里的待输入状态分发。

    无待输入状态时静默返回（不回复，避免干扰正常聊天）。
    pending_input（改名/重装流程）存在时也跳过，那条文本归
    handlers_extra.text_input_callback 处理，避免双重消费。
    """
    if context.user_data.get("pending_input"):
        return
    message = update.effective_message
    text = (message.text or "") if message is not None else ""
    if context.user_data.get("cmd_awaiting"):
        await _handle_command_text(update, context, text)
    elif context.user_data.get("pk_awaiting"):
        await _handle_pubkey_text(update, context, text)
    elif context.user_data.get("px_awaiting"):
        await _handle_proxy_text(update, context, text)
    elif context.user_data.get("ua_awaiting"):
        await _handle_uploadapi_text(update, context, text)
    # 无待输入状态：静默忽略


# ================= 菜单 23/24：内存占用 / 清除缓存和占用 =================

def build_memhog_command(fraction=MEMHOG_FRACTION):
    """构造远程启动内存占用进程的 shell 命令。

    Python 单行：读 /proc/meminfo 第一行（MemTotal，单位 kB），
    分配 fraction 比例的内存并逐页触碰（让 RSS 真正上去，不只是虚拟内存），
    然后 sleep 近 32 年持有；nohup + 后台运行，echo $! 输出 PID。
    """
    python_code = (
        "import time; "
        "total=int(open('/proc/meminfo').readline().split()[1]); "
        f"size=int(total*1024*{fraction}); "
        "buf=bytearray(size); "
        "buf[::4096]=bytes((size+4095)//4096); "
        f"time.sleep({MEMHOG_SLEEP_SECONDS})"
    )
    return (f"nohup python3 -c \"{python_code}\" "
            f">{MEMHOG_LOG_PATH} 2>&1 </dev/null & echo $!")


def parse_memhog_pid(output):
    """从 `echo $!` 的输出里解析 PID；解析不到返回 None。"""
    text = (output or "").strip()
    if text.isdigit():
        return int(text)
    for line in reversed(text.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    return None


def build_kill_command(pid):
    """杀占用进程的命令：进程不存在时输出 GONE（不报错，调用方可区分）。"""
    return f"kill {int(pid)} 2>/dev/null && echo KILLED || echo GONE"


def parse_kill_output(output):
    """解析杀进程命令的输出：True 表示进程存在且已杀，False 表示已不存在。"""
    return "KILLED" in (output or "")


def build_drop_caches_command(use_sudo=False):
    """清缓存命令：sync 后把 3 写入 drop_caches。

    注意：`sudo echo 3 > /proc/...` 不生效——重定向由当前 shell
    （非 root）执行，所以非 root 时必须 sudo sh -c 包住整个重定向。
    """
    if use_sudo:
        return "sync; sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'"
    return "sync; echo 3 > /proc/sys/vm/drop_caches"


def load_mem_hogs(path=None):
    """读 mem_hog.json，返回 {实例 OCID: {pid, started, host}}；不存在返回 {}。"""
    path = path or MEM_HOG_FILE
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_mem_hogs(records, path=None):
    """把占用记录写回 mem_hog.json，返回 records。"""
    path = path or MEM_HOG_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    return records


def record_memhog(instance_id, pid, host_ip, path=None):
    """记录一台实例的占用进程（PID + 启动时间 + 主机 IP），返回完整记录。"""
    records = load_mem_hogs(path)
    records[instance_id] = {
        "pid": int(pid),
        "started": datetime.now(timezone.utc).isoformat(),
        "host": host_ip,
    }
    save_mem_hogs(records, path)
    return records


def remove_memhog(instance_id, path=None):
    """删除一台实例的占用记录；存在返回 True，不存在返回 False。"""
    records = load_mem_hogs(path)
    if instance_id not in records:
        return False
    del records[instance_id]
    save_mem_hogs(records, path)
    return True


def resolve_instance_ssh_target(cli, instance, hosts=None):
    """解析某 OCI 实例的 SSH 连接目标。

    返回 (target, error)：成功时 target 是 run_remote_command 接受的
    主机 dict {host, port, username, key_path}，error 为 None；
    失败时 target 为 None，error 是中文原因。
    匹配顺序：先按公网 IP 查 hosts.json 里带 key_path 的已保存主机，
    没有则用默认用户/端口/密钥（MEMHOG_SSH_*）。
    """
    public_ip = get_public_ip(cli, instance.get("id"))
    if not public_ip:
        return None, "该实例没有公网 IP，无法 SSH 连接。"
    if hosts is None:
        hosts = load_hosts()
    for record in hosts:
        if record.get("host") == public_ip and record.get("key_path"):
            return {
                "host": public_ip,
                "port": record.get("port") or 22,
                "username": record.get("username") or "root",
                "key_path": record.get("key_path"),
            }, None
    return {
        "host": public_ip,
        "port": MEMHOG_SSH_PORT,
        "username": MEMHOG_SSH_USER,
        "key_path": MEMHOG_SSH_KEY,
    }, None


def start_memhog_on_host(host, timeout=COMMAND_TIMEOUT_SECONDS):
    """在远程主机启动内存占用进程，返回 PID（int）。

    复用 run_remote_command（与 /command 同一 Paramiko 执行方式）。
    启动失败或 PID 解析失败抛 RemoteCommandError。
    """
    exit_code, stdout, stderr = run_remote_command(
        host, build_memhog_command(), timeout=timeout)
    if exit_code != 0:
        detail = (stdout + stderr).strip()[:200]
        raise RemoteCommandError(f"启动占用进程失败（退出码 {exit_code}）：{detail}")
    pid = parse_memhog_pid(stdout)
    if pid is None:
        raise RemoteCommandError(
            f"无法解析占用进程 PID，命令输出：{stdout.strip()[:200]}")
    return pid


def clear_memhog_on_host(host, pid=None, use_sudo=False,
                         timeout=COMMAND_TIMEOUT_SECONDS):
    """在远程主机清除占用与缓存。

    返回 {"killed": True/False/None, "drop_caches_ok": bool,
          "drop_caches_output": str}。
    killed：None 表示无 PID 可杀；False 表示进程已不存在。
    SSH 本身失败时抛 RemoteCommandError。
    """
    result = {"killed": None, "drop_caches_ok": False,
              "drop_caches_output": ""}
    if pid is not None:
        _code, out, _err = run_remote_command(
            host, build_kill_command(pid), timeout=timeout)
        result["killed"] = parse_kill_output(out)
    code, out, err = run_remote_command(
        host, build_drop_caches_command(use_sudo), timeout=timeout)
    result["drop_caches_ok"] = (code == 0)
    result["drop_caches_output"] = (out + err).strip()[:300]
    return result


def _mem_pick_keyboard(instances, prefix, hogs=None):
    """内存菜单的实例选择键盘：每台一个按钮，已占用的标 🧠。"""
    hogs = hogs or {}
    rows = []
    for idx, inst in enumerate(instances):
        name = inst.get("display-name") or (inst.get("id") or "")[-4:]
        mark = " 🧠" if inst.get("id") in hogs else ""
        rows.append([InlineKeyboardButton(
            f"{name}{mark}", callback_data=f"{prefix}:pick:{idx}")])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="x")])
    return InlineKeyboardMarkup(rows)


def _mem_confirm_keyboard(prefix, idx, confirm_text):
    """内存菜单的二次确认键盘。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(confirm_text,
                              callback_data=f"{prefix}:go:{idx}")],
        [InlineKeyboardButton("❌ 取消", callback_data="x")],
    ])


async def _load_running_instances(context):
    """拉实例列表并只留 RUNNING 的；失败返回 (None, 错误文本)。"""
    try:
        instances = await run_sync(list_instances,
                                   get_cli(context), get_compartment(context))
    except Exception as exc:  # noqa: BLE001 - OCI 异常统一转中文提示
        return None, fmt_error(exc)
    running = [i for i in instances if i.get("lifecycle-state") == "RUNNING"]
    return running, None


@require_allowed
async def handle_memballoon(update, context):
    """菜单 23：占用 25% 内存——列运行中实例，选一台。"""
    running, error = await _load_running_instances(context)
    if error is not None:
        await _reply(update, error)
        return
    if not running:
        await _reply(update, "没有运行中的实例，无法占用内存。")
        return
    store_instances(context, running)
    hogs = load_mem_hogs()
    lines = ["🧠 占用 25% 内存：选一台实例：", ""]
    for idx, inst in enumerate(running):
        mark = " 🧠已占用" if inst.get("id") in hogs else ""
        shape = (inst.get("shape") or "").replace("VM.Standard.", "")
        lines.append(f"{idx + 1}. {inst.get('display-name') or '?'}"
                     f"（{shape}）{mark}")
    lines.append("")
    lines.append("占用进程约占总内存的 25%，用于防止闲置回收；"
                 "已占用的实例会拒绝重复启动。")
    await _reply(update, "\n".join(lines),
                 reply_markup=_mem_pick_keyboard(running, "mh", hogs))


@require_allowed
async def memballoon_callback(update, context):
    """回调 mh:pick:<idx> / mh:go:<idx>：选实例 -> 二次确认 -> 启动占用进程。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "实例不存在或已过期，请重新打开菜单 23。")
        return
    name = inst.get("display-name") or "?"
    if action == "pick":
        record = load_mem_hogs().get(inst.get("id"))
        if record:
            await _reply(update,
                         "⚠️ 该实例已在占用内存中，拒绝重复启动。\n\n"
                         f"实例：{name}\nPID：{record.get('pid')}\n"
                         f"启动时间：{record.get('started')}\n"
                         "如需停止，请用菜单 24（清除缓存和占用）。")
            return
        await _reply(update,
                     f"🧠 确认在实例「{name}」上占用 25% 内存？\n\n"
                     "将通过 SSH 启动一个后台进程，分配约 25% 总内存并长期持有，"
                     "用于防止 OCI 回收闲置实例。\n"
                     "停止方法：菜单 24（清除缓存和占用）。",
                     reply_markup=_mem_confirm_keyboard(
                         "mh", idx, "🧠 确认占用"))
        return
    if action == "go":
        await _memballoon_go(update, context, inst, name)
        return
    await _reply(update, "参数错误，请重新打开菜单 23。")


async def _memballoon_go(update, context, inst, name):
    """mh:go 执行体：重复检查 -> 解析 SSH 目标 -> 启动占用进程 -> 记 PID。"""
    instance_id = inst.get("id")
    hogs = load_mem_hogs()
    if instance_id in hogs:
        await _reply(update,
                     f"⚠️ 该实例已在占用内存中"
                     f"（PID {hogs[instance_id].get('pid')}），拒绝重复启动。")
        return
    try:
        target, error = await run_sync(
            resolve_instance_ssh_target, get_cli(context), inst)
    except Exception as exc:  # noqa: BLE001 - 查 IP 失败也要提示用户
        await _reply(update, fmt_error(exc))
        return
    if error:
        await _reply(update, f"❌ {error}")
        return
    await _reply(update, f"⏳ 正在 {name} 上启动内存占用进程…")
    try:
        pid = await run_sync(start_memhog_on_host, target)
    except RemoteCommandError as exc:
        await _reply(update, f"❌ 启动失败：{exc}")
        return
    record_memhog(instance_id, pid, target["host"])
    await _reply(update,
                 f"✅ 已在实例「{name}」上启动内存占用进程。\n"
                 f"PID：{pid}\n占用：约 25% 总内存\n"
                 "停止方法：菜单 24（清除缓存和占用）。")


@require_allowed
async def handle_memclear(update, context):
    """菜单 24：清除缓存和占用——列运行中实例，选一台。"""
    running, error = await _load_running_instances(context)
    if error is not None:
        await _reply(update, error)
        return
    if not running:
        await _reply(update, "没有运行中的实例，无法清除缓存和占用。")
        return
    store_instances(context, running)
    hogs = load_mem_hogs()
    lines = ["🧹 清除缓存和占用：选一台实例：", ""]
    for idx, inst in enumerate(running):
        mark = " 🧠有占用进程" if inst.get("id") in hogs else ""
        shape = (inst.get("shape") or "").replace("VM.Standard.", "")
        lines.append(f"{idx + 1}. {inst.get('display-name') or '?'}"
                     f"（{shape}）{mark}")
    lines.append("")
    lines.append("执行内容：杀掉该实例的占用进程（如有），"
                 "再执行 sync + drop_caches 清缓存。")
    await _reply(update, "\n".join(lines),
                 reply_markup=_mem_pick_keyboard(running, "mc", hogs))


@require_allowed
async def memclear_callback(update, context):
    """回调 mc:pick:<idx> / mc:go:<idx>：选实例 -> 二次确认 -> 杀进程 + 清缓存。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, "实例不存在或已过期，请重新打开菜单 24。")
        return
    name = inst.get("display-name") or "?"
    if action == "pick":
        record = load_mem_hogs().get(inst.get("id"))
        if record:
            hog_line = (f"占用进程：PID {record.get('pid')}，"
                        f"启动于 {record.get('started')}，将被杀掉。")
        else:
            hog_line = "该实例无占用进程，只清缓存。"
        await _reply(update,
                     f"🧹 确认清除实例「{name}」的缓存和占用？\n\n"
                     f"{hog_line}\n"
                     "还将执行 sync + drop_caches 清理系统缓存。",
                     reply_markup=_mem_confirm_keyboard(
                         "mc", idx, "🧹 确认清除"))
        return
    if action == "go":
        await _memclear_go(update, context, inst, name)
        return
    await _reply(update, "参数错误，请重新打开菜单 24。")


async def _memclear_go(update, context, inst, name):
    """mc:go 执行体：解析 SSH 目标 -> 杀占用进程 -> 清缓存 -> 删记录 -> 汇报。"""
    instance_id = inst.get("id")
    record = load_mem_hogs().get(instance_id)
    pid = record.get("pid") if record else None
    try:
        target, error = await run_sync(
            resolve_instance_ssh_target, get_cli(context), inst)
    except Exception as exc:  # noqa: BLE001 - 查 IP 失败也要提示用户
        await _reply(update, fmt_error(exc))
        return
    if error:
        await _reply(update, f"❌ {error}")
        return
    use_sudo = (target.get("username") or "root") != "root"
    await _reply(update, f"⏳ 正在 {name} 上清除缓存和占用…")
    try:
        result = await run_sync(clear_memhog_on_host, target, pid, use_sudo)
    except RemoteCommandError as exc:
        await _reply(update, f"❌ 清除失败：{exc}")
        return
    removed = remove_memhog(instance_id)
    lines = [f"🧹 「{name}」清除完成：", ""]
    if pid is None:
        lines.append("• 无占用进程（无需杀进程）。")
    elif result["killed"]:
        lines.append(f"• 已杀掉占用进程 PID {pid}。")
    else:
        lines.append(f"• 占用进程 PID {pid} 已不存在（可能已退出）。")
    if result["drop_caches_ok"]:
        lines.append("• 清缓存成功（sync + drop_caches）。")
    else:
        detail = result["drop_caches_output"] or "未知错误"
        lines.append(f"• 清缓存失败：{detail}")
    if removed:
        lines.append("• 已删除占用记录。")
    await _reply(update, "\n".join(lines))
