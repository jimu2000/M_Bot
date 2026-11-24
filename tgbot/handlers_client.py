#!/usr/bin/env python3
"""Telegram 客户端工具（阶段 5 工作包 B）。

顶层命令（bot.py 注册）：
  - /command   远程执行 Shell：选已保存主机 -> 发命令文本 -> Paramiko 执行回显
  - /pubkey    SSH 公钥管理：列出 / 添加 / 删除（只存公钥，绝不存私钥）
  - /oproxy    机器人出站代理设置（设置后需重启机器人服务才生效）
  - /uploadapi 添加 OCI API：发 config 文件或粘贴文本，支持 R-Bot
    格式（oci=begin/end 块，多 profile 一次导入），校验后写入
    ~/oci_config（key_file 在 /root 下保留，否则归一到 /root/<PROFILE>.pem）

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
  - 写操作（删公钥、清代理、写 OCI 配置、启停占用进程）都有二次确认
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
import shutil
import socket
import time
from datetime import datetime, timezone

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import Update
from telegram.ext import ContextTypes

from oci.client import OCI_CONFIG_LEGACY_PATH, OCI_CONFIG_ROOT_PATH
from oci.instance import list_instances
from oci.network import get_public_ip
from tgbot import menu_tracker
from tgbot.config import update_env_file_vars
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
# OCI config 文件路径（/uploadapi 与 /oci 的写入目标）：M_Bot 管理的
# OCI 配置放 root 家目录（~/oci_config），与 R-Bot 的放置习惯一致。
# 环境变量 MBOT_OCI_CONFIG_FILE 可显式指定（测试/部署用）。
# 旧部署的 ~/.oci/config 在首次写入时自动迁移（内容原样复制过去，
# 旧文件保留作备份）；读的时候新旧两个文件都会查。
OCI_CONFIG_PATH = (os.environ.get("MBOT_OCI_CONFIG_FILE")
                   or OCI_CONFIG_ROOT_PATH)

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
MEMHOG_LOG_PATH = "/tmp/mbot_memhog.log"
# 实例 SSH 默认值（hosts.json 里按 IP 匹配不到已保存主机时用）：
# 用户名 / 端口 / 密钥（面板服务器上的默认密钥，需已加到实例上）。
# 端口与密钥可由环境变量覆盖：MBOT_MEMHOG_SSH_PORT（默认 22，
# 生产实例用 14422）、MBOT_MEMHOG_SSH_KEY（默认 ~/.ssh/id_ed25519；
# 面板服务器上没有该私钥时必须设，指向部署用的专用密钥）。
MEMHOG_SSH_USER = "root"
try:
    MEMHOG_SSH_PORT = int(os.environ.get("MBOT_MEMHOG_SSH_PORT", "22"))
except ValueError:
    MEMHOG_SSH_PORT = 22
MEMHOG_SSH_KEY = os.path.expanduser(
    os.environ.get("MBOT_MEMHOG_SSH_KEY", "~/.ssh/id_ed25519"))
# =====================================================


# ---------------- 通用小工具 ----------------

async def _reply(update, context, text, reply_markup=None,
               parse_mode=None):
    """统一回复：tgbot.menu_tracker.reply 的薄包装（带选择框清理，
    每聊天只留一个活动选择框；点完删掉多余的选择框，只留结果）。"""
    return await menu_tracker.reply(
        update, context, text, reply_markup=reply_markup,
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


def _connect_host(host):
    """按 host 记录连 SSH（密钥认证，22 与 14422 之间端口回退）。

    返回已连接的 paramiko.SSHClient（调用方负责 close）。
    连接/认证失败抛 RemoteCommandError。run_remote_command 与
    put_remote_file 共用这套逻辑。
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
        try:
            parent = os.path.dirname(KNOWN_HOSTS_FILE)
            if parent:
                os.makedirs(parent, exist_ok=True)
            attempt_client.save_host_keys(KNOWN_HOSTS_FILE)
        except Exception as exc:
            logger.warning("known_hosts 保存失败（不影响连接）：%s", exc)
        return attempt_client
    raise RemoteCommandError("SSH 连接失败：无可用端口。")


def put_remote_file(host, content, remote_path, mode=0o644):
    """经 SFTP 把文本内容写到远程主机（看门狗安装传脚本/配置文件用）。

    content 是 str（按 UTF-8 写）；mode 是目标文件权限（默认 644，
    脚本传 0o755、配置文件传 0o600）。连接失败抛 RemoteCommandError，
    传输失败抛 RemoteCommandError（附原因）。
    """
    client = _connect_host(host)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as remote_file:
                remote_file.write(content)
            sftp.chmod(remote_path, mode)
        finally:
            try:
                sftp.close()
            except Exception:
                pass
    except RemoteCommandError:
        raise
    except (paramiko.SSHException, OSError) as exc:
        raise RemoteCommandError(f"SFTP 传输失败：{exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass


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
    连接逻辑在 _connect_host 里（put_remote_file 共用）。
    """
    client = _connect_host(host)
    try:
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
        await _reply(update, context,
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
    await _reply(update, context, "\n".join(lines),
                 reply_markup=host_select_keyboard(hosts))


@require_allowed
async def command_host_callback(update, context):
    """回调 cmd:host:<idx>：选中主机，进入命令输入；cmd:done：结束会话。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "done":
        _clear_pending(context)
        await _reply(update, context, "已结束远程执行会话。")
        return
    if action != "host":
        await _reply(update, context, "参数错误，请重新发送 /command。")
        return
    idx = parts[2] if len(parts) >= 3 else ""
    hosts = context.user_data.get("cmd_hosts") or []
    try:
        host = hosts[int(idx)]
    except (ValueError, IndexError, TypeError):
        host = None
    if host is None:
        await _reply(update, context, "主机不存在或已过期，请重新发送 /command。")
        return
    if host.get("auth_type") == "password":
        await _reply(update, context,
                     f"⚠️ 主机「{host.get('name') or host.get('host')}」"
                     "是密码认证，机器人不保存密码，无法用 /command 连接。\n"
                     "请在 Web 面板里把该主机改成密钥认证，"
                     "或用 Web SSH（连接时现输密码）。")
        return
    context.user_data["cmd_host_idx"] = int(idx)
    context.user_data["cmd_awaiting"] = True
    await _reply(
        update, context,
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
        await _reply(update, context, "命令为空，请重新发送。")
        return
    hosts = context.user_data.get("cmd_hosts") or []
    idx = context.user_data.get("cmd_host_idx")
    host = None
    if isinstance(idx, int) and 0 <= idx < len(hosts):
        host = hosts[idx]
    if host is None:
        context.user_data.pop("cmd_awaiting", None)
        await _reply(update, context, "主机选择已过期，请重新发送 /command。")
        return
    blocked = find_dangerous_pattern(command)
    if blocked:
        await _reply(update, context,
                     f"⛔ 危险命令，已拒绝执行：{blocked}\n"
                     "请发送其他命令，或点“结束会话”。")
        return
    name = host.get("name") or host.get("host")
    await _reply(update, context, f"⏳ 正在 {name} 上执行：{command}")
    try:
        exit_code, stdout, stderr = await run_sync(
            run_remote_command, host, command)
    except CommandTimeoutError as exc:
        await _reply(update, context, f"⏰ {exc}")
        return
    except RemoteCommandError as exc:
        await _reply(update, context, f"❌ 执行失败：{exc}")
        return
    output = stdout or ""
    if (stderr or "").strip():
        output += ("\n" if output else "") + "[stderr]\n" + stderr
    if not output.strip():
        output = "（命令无输出）"
    await _reply(update, context, f"🖥️ {name} $ {command}\n退出码：{exit_code}")
    for chunk in chunk_output(output):
        await _reply(update, context, chunk)


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
    await _reply(update, context, fmt_ssh_key_list(keys),
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
        await _reply(update, context,
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
            await _reply(update, context, "公钥不存在或已过期，请重新发送 /pubkey。")
            return
        if action == "del":
            await _reply(
                update, context,
                f"⚠️ 确认删除这条公钥？\n\n{fmt_ssh_key_line(entry, idx_int)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "🗑️ 确认删除",
                        callback_data=f"pk:confirmdel:{idx}")],
                    [InlineKeyboardButton("❌ 取消", callback_data="x")]]))
            return
        _keys, removed = remove_ssh_key(idx)
        if removed is None:
            await _reply(update, context, "公钥不存在或已删除。")
        else:
            await _reply(update, context,
                         f"✅ 已删除公钥：\n{fmt_ssh_key_line(removed, idx_int)}")
        return
    await _reply(update, context, "参数错误，请重新发送 /pubkey。")


async def _handle_pubkey_text(update, context, text):
    """待输入状态下的公钥文本：校验格式 -> 保存（去重）。"""
    ok, error = validate_ssh_public_key(text)
    if not ok:
        await _reply(update, context, f"❌ 公钥格式不对：{error}\n请重新发送，或发 /pubkey 返回。")
        return
    _keys, added = add_ssh_key(text)
    context.user_data.pop("pk_awaiting", None)
    if not added:
        await _reply(update, context, "⚠️ 这条公钥已保存过，未重复添加。")
    else:
        await _reply(update, context, "✅ 公钥已保存。")


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
    await _reply(update, context, text, reply_markup=proxy_keyboard(current))


@require_allowed
async def oproxy_callback(update, context):
    """回调 px:set / px:clear / px:confirmclear。

    清除分两步：先弹二次确认，点确认后才真正清除。
    """
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if action == "set":
        context.user_data["px_awaiting"] = True
        await _reply(update, context,
                     "请直接发送代理地址（纯文本）：\n"
                     "例如：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080\n\n"
                     "必须以 http://、https:// 或 socks5:// 开头。")
        return
    if action == "clear":
        current = load_proxy()
        if not current:
            await _reply(update, context, "当前未设置代理，无需清除。")
            return
        await _reply(
            update, context,
            f"⚠️ 确认清除出站代理？\n\n当前：{current}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 确认清除",
                                      callback_data="px:confirmclear")],
                [InlineKeyboardButton("❌ 取消", callback_data="x")]]))
        return
    if action == "confirmclear":
        save_proxy(None)
        await _reply(update, context, "✅ 出站代理已清除（直连）。\n⚠️ 重启机器人服务后生效。")
        return
    await _reply(update, context, "参数错误，请重新发送 /oproxy。")


async def _handle_proxy_text(update, context, text):
    """待输入状态下的代理地址文本：校验协议 -> 保存。"""
    url = (text or "").strip()
    ok, error = validate_proxy_url(url)
    if not ok:
        await _reply(update, context, f"❌ {error}\n请重新发送，或发 /oproxy 返回。")
        return
    save_proxy(url)
    context.user_data.pop("px_awaiting", None)
    await _reply(update, context, f"✅ 出站代理已设置为：{url}\n⚠️ 重启机器人服务后生效。")


# ================= /uploadapi 添加 API =================

def _extract_oci_block(text):
    """从 R-Bot 风格的整份配置里取出 oci=begin...oci=end 之间的内容。

    没有标记时返回原文（普通单份 config 直接解析）。
    只有 begin 没有 end 时返回 None（调用方报错）。
    块外的内容（username/password/azure 块等）一律忽略，不解析。
    """
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped == "oci=begin":
            start = i
        elif stripped == "oci=end" and start is not None:
            end = i
            break
    if start is None:
        return text
    if end is None:
        return None
    return "\n".join(lines[start + 1:end])


def parse_oci_config_text(text):
    """解析用户上传的 OCI config 文本（支持 R-Bot 格式）。

    接受两种输入：
      1. 普通 OCI config 文本（一个或多个 [PROFILE] 段）；
      2. R-Bot 风格的整份配置：只取 oci=begin 与 oci=end 之间的内容。
    返回 {"profiles": [{"profile", "fields", "missing"}],
          "profile": 首个 profile 名, "fields": 首个字段 dict,
          "missing": 首个缺失键, "error": str|None,
          "extra_profiles": [], "cloudflare": dict|None}。
    profiles 按文件里的顺序排，全部导入（旧版只取第一个；
    extra_profiles 保留作兼容，恒为空列表）。
    [cloudflare] 段不是 OCI profile：跳过不进 profiles，
    凭据提出来放 cloudflare（{"api_token","api_key","email"}，
    有 Token 或 Key+邮箱齐全时才有值；键名 cf_token / cf_key /
    cf_account_key（R-Bot 叫法）/ cf_email）。
    任一 profile 名不合法时 error 说明是哪个名字。
    """
    result = {"profiles": [], "profile": None, "fields": {}, "missing": [],
              "error": None, "extra_profiles": [], "cloudflare": None}
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        result["error"] = "内容为空。"
        return result
    block = _extract_oci_block(text)
    if block is None:
        result["error"] = "只找到 oci=begin，没找到 oci=end。"
        return result
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        parser.read_string(block)
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
    for profile in sections:
        if profile.lower() == "cloudflare":
            # [cloudflare] 段不是 OCI profile：只提凭据，不进账号列表。
            fields = {key: value.strip()
                      for key, value in parser.items(profile)}
            cf = {"api_token": fields.get("cf_token", ""),
                  "api_key": (fields.get("cf_key")
                              or fields.get("cf_account_key", "")),
                  "email": fields.get("cf_email", "")}
            if cf["api_token"] or (cf["api_key"] and cf["email"]):
                result["cloudflare"] = cf
            continue
        if not PROFILE_NAME_RE.match(profile):
            result["error"] = (f"profile 名「{profile}」不合法，"
                               "只能含字母、数字、下划线、点、横线。")
            return result
        fields = {key: value.strip()
                  for key, value in parser.items(profile)}
        missing = [key for key in REQUIRED_OCI_KEYS if not fields.get(key)]
        result["profiles"].append({"profile": profile, "fields": fields,
                                   "missing": missing})
    if not result["profiles"]:
        result["error"] = "没有找到 [PROFILE] 段。"
        return result
    first = result["profiles"][0]
    result["profile"] = first["profile"]
    result["fields"] = first["fields"]
    result["missing"] = first["missing"]
    return result


def profile_exists_in_config(profile, path=None):
    """OCI 配置文件里是否已有同名 profile。

    path 未指定时新文件（~/oci_config）与旧文件（~/.oci/config）都查；
    指定 path 时只查该文件。注意 [DEFAULT] 段要用换名的
    default_section 才能被 has_section 认出来。
    """
    candidates = [path] if path else [OCI_CONFIG_PATH,
                                      OCI_CONFIG_LEGACY_PATH]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        parser = configparser.ConfigParser(
            default_section="__oci_no_defaults__", interpolation=None)
        try:
            parser.read(candidate, encoding="utf-8")
        except configparser.Error:
            continue
        if parser.has_section(profile):
            return True
    return False


def _has_section_ci(path, name):
    """配置文件里有没有某段（段名大小写不敏感）。

    给 [cloudflare] 段用：R-Bot 与手写配置的段名大小写不统一，
    [Cloudflare] 与 [cloudflare] 应视为同一段。
    """
    if not path or not os.path.exists(path):
        return False
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return False
    return any(s.lower() == name.lower() for s in parser.sections())


def _append_config_block(block, path=None):
    """把一段 config 文本追加到 OCI 配置文件（迁移 + 分隔 + 600 权限）。

    目标默认 ~/oci_config；目标不存在但旧文件 ~/.oci/config 存在时，
    先把旧文件内容原样迁移过去（旧文件保留作备份），再追加。
    返回写入的文件路径。
    """
    target = path or OCI_CONFIG_PATH
    try:
        same_as_legacy = (os.path.abspath(target)
                          == os.path.abspath(OCI_CONFIG_LEGACY_PATH))
    except (OSError, ValueError):
        same_as_legacy = False
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if (path is None and not same_as_legacy
            and not os.path.exists(target)
            and os.path.exists(OCI_CONFIG_LEGACY_PATH)):
        shutil.copyfile(OCI_CONFIG_LEGACY_PATH, target)
    existing = ""
    if os.path.exists(target):
        with open(target, encoding="utf-8") as f:
            existing = f.read()
    sep = "" if (not existing or existing.endswith("\n")) else "\n"
    with open(target, "a", encoding="utf-8") as f:
        f.write(sep + block)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def commit_cf_section(cloudflare, path=None):
    """把 [cloudflare] 段追加到 OCI 配置文件。

    cloudflare 是 parse 出的 {"api_token","api_key","email"}。
    已有该段（大小写不敏感）时不覆盖；没凭据可写时什么都不做。
    返回 "saved" / "exists" / "empty"。
    """
    if not cloudflare:
        return "empty"
    token = (cloudflare.get("api_token") or "").strip()
    key = (cloudflare.get("api_key") or "").strip()
    email = (cloudflare.get("email") or "").strip()
    if not (token or (key and email)):
        return "empty"
    target = path or OCI_CONFIG_PATH
    if _has_section_ci(target, "cloudflare"):
        return "exists"
    if (path is None and not os.path.exists(target)
            and _has_section_ci(OCI_CONFIG_LEGACY_PATH, "cloudflare")):
        # 目标不存在时写入会自动迁移旧文件（里面已有该段），
        # 再追加会造成重复段；且解析本就会回退读旧文件，凭据已生效。
        return "exists"
    lines = ["[cloudflare]"]
    if token:
        lines.append(f"cf_token = {token}")
    if key:
        lines.append(f"cf_key = {key}")
    if email:
        lines.append(f"cf_email = {email}")
    _append_config_block("\n".join(lines) + "\n", path)
    return "saved"


def append_oci_profile(profile, fields, path=None):
    """把新 profile 追加到 OCI 配置文件（已有内容原样不动）。

    目标默认是 ~/oci_config；目标不存在但旧文件 ~/.oci/config 存在时，
    先把旧文件内容原样迁移过去（旧文件保留作备份），再追加。
    同名 profile 已存在时抛 ValueError，不覆盖。
    写入后文件权限设 600。返回写入的文件路径。
    """
    target = path or OCI_CONFIG_PATH
    if profile_exists_in_config(profile, target):
        raise ValueError(f"profile「{profile}」已存在，请改名后重传。")
    if path is None and profile_exists_in_config(profile):
        raise ValueError(f"profile「{profile}」已存在，请改名后重传。")
    lines = [f"[{profile}]"]
    for key in REQUIRED_OCI_KEYS:
        if key in fields:
            lines.append(f"{key} = {fields[key]}")
    for key, value in fields.items():
        if key not in REQUIRED_OCI_KEYS:
            lines.append(f"{key} = {value}")
    return _append_config_block("\n".join(lines) + "\n", path)


def normalize_oci_key_file(profile, fields):
    """key_file 归一化：已在 root 家目录下的绝对路径保留原样
    （如 /root/1.pem，与 R-Bot 习惯一致）；其余一律改成
    ~/<profile>.pem。返回 (new_fields, original_key_file)。
    """
    original = (fields.get("key_file") or "").strip()
    home = os.path.expanduser("~")
    new_fields = dict(fields)
    if original:
        abs_path = os.path.abspath(os.path.expanduser(original))
        home_abs = os.path.abspath(home)
        if abs_path == home_abs or abs_path.startswith(home_abs + os.sep):
            return new_fields, original
    new_fields["key_file"] = os.path.join(home, f"{profile}.pem")
    return new_fields, original


def maybe_copy_key_file(original, new_path):
    """原 key_file 已在服务器上且新路径尚无文件时，复制过去（600 权限）。

    返回 True 表示新路径已有私钥文件（本来就有，或刚复制过去）；
    False 表示新路径尚无私钥，需用户自行 scp。
    """
    if os.path.exists(new_path):
        return True
    if not original or not os.path.exists(original):
        return False
    try:
        shutil.copyfile(original, new_path)
        os.chmod(new_path, 0o600)
        return True
    except OSError:
        return False


def prepare_oci_profiles(parsed):
    """把解析结果整理成待导入列表：过滤已存在、归一化 key_file。

    返回 (new_profiles, skipped_existing, error_text)：
    new_profiles 是 [{"profile", "fields", "original_key_file"}]；
    skipped_existing 是已存在而跳过的 profile 名列表；
    error_text 非空时表示整体失败（任一 profile 缺必填键）。
    """
    for entry in parsed["profiles"]:
        if entry["missing"]:
            missing = "、".join(entry["missing"])
            return [], [], (f"profile「{entry['profile']}」缺少必填键："
                            f"{missing}\n请补全后重新发送。")
    new_profiles = []
    skipped = []
    for entry in parsed["profiles"]:
        profile = entry["profile"]
        if profile_exists_in_config(profile):
            skipped.append(profile)
            continue
        fields, original = normalize_oci_key_file(profile, entry["fields"])
        new_profiles.append({"profile": profile, "fields": fields,
                             "original_key_file": original})
    return new_profiles, skipped, ""


def commit_oci_profiles(profiles):
    """逐个把待导入的 profile 写进配置文件，并处理私钥文件。

    profiles 是 prepare_oci_profiles 返回的 new_profiles。
    返回 [{"profile", "key_file", "key_ready"}]：key_ready 为 True
    表示该 profile 的私钥文件已在 key_file 路径就位（本来就在，
    或刚从原路径复制过去）；False 表示需用户自行 scp。
    任一写入失败时抛 ValueError / OSError（已写入的不回滚，
    调用方把已成功的个数告诉用户）。
    """
    results = []
    for entry in profiles:
        profile = entry["profile"]
        fields = entry["fields"]
        append_oci_profile(profile, fields)
        key_path = fields.get("key_file", "")
        key_ready = maybe_copy_key_file(entry["original_key_file"], key_path)
        results.append({"profile": profile, "key_file": key_path,
                        "key_ready": key_ready})
    return results


def add_profile_compartments(profiles):
    """给新导入的 profile 在 env 文件里加 MBOT_COMPARTMENT_<PROFILE>。

    值取该 profile 的 tenancy OCID（根 compartment 即 tenancy）。
    已在 env 文件里的键不覆盖（用户可能配了子 compartment）。
    返回实际写入的 {变量名: tenancy}（为空表示都没写）。
    """
    updates = {}
    for entry in profiles:
        profile = entry["profile"]
        tenancy = (entry["fields"].get("tenancy") or "").strip()
        if not tenancy:
            continue
        var = f"MBOT_COMPARTMENT_{profile}"
        if os.environ.get(var):
            continue
        updates[var] = tenancy
    if not updates:
        return {}
    try:
        env_path = None
        try:
            from web import config as web_config
            env_path = web_config.ENV_FILE
        except Exception:  # noqa: BLE001 - web.config 不可用时用默认路径
            env_path = os.path.expanduser("~/.rbot-clone.env")
        if env_path and os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                existing = f.read()
            updates = {k: v for k, v in updates.items()
                       if f"{k}=" not in existing}
    except OSError:
        pass
    if not updates:
        return {}
    update_env_file_vars(updates)
    return updates


@require_allowed
async def uploadapi_command(update, context):
    """处理 /uploadapi：提示发 config 文件或粘贴文本，进入等待状态。"""
    _clear_pending(context)
    context.user_data["ua_awaiting"] = True
    await _reply(update, context,
                 "📤 添加 OCI API 配置：\n\n"
                 "请发送 config 文件（.txt / .conf），或直接粘贴 config 文本。\n"
                 "支持 R-Bot 格式：oci=begin … oci=end 块里可一次放多个 "
                 "[PROFILE]，一次全部导入。\n\n"
                 "格式要求：每个 [PROFILE] 段含 user / fingerprint / "
                 "tenancy / region / key_file 五个键。\n\n"
                 "配置里如有 [cloudflare] 段（cf_token，或 cf_key + "
                 "cf_email），会一并保存，/domain 与证书 DNS 验证会用它。\n\n"
                 "配置写入 ~/oci_config（root 家目录）；key_file 已在 /root "
                 "下的保留原路径（如 /root/1.pem），其余自动改成 "
                 "/root/<PROFILE>.pem。\n\n"
                 "⚠️ 只传 config 文本；私钥文件（.pem）不经过 Telegram，"
                 "请自行 scp 到服务器对应路径（原 key_file 已在服务器上"
                 "的，写入后自动复制）。")


def build_oci_profiles_confirm_text(profiles, skipped=None, cloudflare=None,
                                    cf_exists=False):
    """多 profile 导入的确认文案：逐个列 profile、区域、key_file。

    cloudflare 非空时追加一行（凭据值绝不回显）；profiles 为空
    且只有 cloudflare 时，确认文案只问 CF 段保存。
    """
    lines = []
    if profiles:
        lines.append(f"📤 确认添加 OCI API 配置？（{len(profiles)} 个账号）")
        lines.append("")
        for i, entry in enumerate(profiles, 1):
            fields = entry["fields"]
            user_tail = (fields.get("user") or "")[-4:]
            lines.append(f"{i}. {entry['profile']}"
                         f"（{fields.get('region')}）")
            lines.append(f"   用户：…{user_tail}")
            lines.append(f"   key_file：{fields.get('key_file')}")
            original = entry.get("original_key_file") or ""
            if original and original != fields.get("key_file"):
                lines.append(f"   （原 key_file：{original}）")
    else:
        lines.append("📤 确认保存 Cloudflare 凭据？")
        lines.append("")
    if cloudflare:
        if profiles:
            lines.append("")
        if cf_exists:
            lines.append("配置里已有 [cloudflare] 段，本次不覆盖。")
        elif profiles:
            lines.append("同时保存 Cloudflare 凭据到 [cloudflare] 段"
                         "（/domain 与证书 DNS 验证会用它）。")
        else:
            lines.append("保存到 [cloudflare] 段"
                         "（/domain 与证书 DNS 验证会用它）。")
    if skipped:
        lines.append("")
        lines.append(f"注意：profile「{'、'.join(skipped)}」已存在，"
                     "本次跳过。")
    if profiles:
        lines.append("")
        lines.append("⚠️ 私钥文件请自行 scp 到服务器上对应的 key_file 路径；"
                     "原 key_file 已在服务器上的，写入后自动复制。")
    return "\n".join(lines)


def build_oci_profiles_result_text(results, added_vars, target_path,
                                   cf_saved=None):
    """多 profile 导入完成后的结果文案。

    cf_saved 是 commit_cf_section 的返回值（"saved"/"exists"/None）。
    """
    lines = []
    if results:
        names = "、".join(r["profile"] for r in results)
        lines.append(f"✅ 已添加 {len(results)} 个 profile 到 {target_path}：{names}。")
        key_lines = []
        for r in results:
            if r["key_ready"]:
                key_lines.append(f"{r['profile']} 私钥已就位：{r['key_file']}")
            else:
                key_lines.append(f"{r['profile']} 私钥需 scp 到：{r['key_file']}")
        if key_lines:
            lines.append("🔑 " + "；".join(key_lines) + "。")
    if cf_saved == "saved":
        lines.append("✅ Cloudflare 凭据已保存到 [cloudflare] 段。")
    elif cf_saved == "exists":
        lines.append("Cloudflare 凭据段已存在，未覆盖。")
    if added_vars:
        var_names = "、".join(sorted(added_vars))
        lines.append(f"✅ 已自动配置账号切换（{var_names}），"
                     "重启机器人后可在菜单里切换到新账号。")
    return "\n".join(lines)


async def _handle_uploadapi_text(update, context, text):
    """待输入状态下的 config 文本：解析 -> 校验 -> 二次确认键盘。"""
    parsed = parse_oci_config_text(text)
    if parsed["error"]:
        await _reply(update, context, f"❌ 配置格式不对：{parsed['error']}\n请重新发送。")
        return
    new_profiles, skipped, error_text = prepare_oci_profiles(parsed)
    if error_text:
        await _reply(update, context, f"❌ {error_text}")
        return
    cloudflare = parsed.get("cloudflare")
    cf_exists = bool(cloudflare) and _has_section_ci(OCI_CONFIG_PATH,
                                                     "cloudflare")
    if not new_profiles and not (cloudflare and not cf_exists):
        context.user_data.pop("ua_awaiting", None)
        await _reply(update, context,
                     f"⚠️ profile「{'、'.join(skipped)}」已存在，不会覆盖。\n"
                     "请改名后重传（改 [PROFILE] 段名）。")
        return
    context.user_data["ua_pending"] = {"profiles": new_profiles,
                                       "cloudflare": cloudflare}
    await _reply(update, context, build_oci_profiles_confirm_text(new_profiles,
                                                         skipped,
                                                         cloudflare,
                                                         cf_exists),
                 reply_markup=InlineKeyboardMarkup([
                     [InlineKeyboardButton("✅ 确认添加",
                                           callback_data="ua:confirm")],
                     [InlineKeyboardButton("❌ 取消", callback_data="x")]]))


@require_allowed
async def uploadapi_callback(update, context):
    """回调 ua:confirm：二次确认后真正写入 OCI 配置文件。"""
    if _callback_data(update) != "ua:confirm":
        await _reply(update, context, "参数错误，请重新发送 /uploadapi。")
        return
    pending = context.user_data.get("ua_pending")
    if not pending or (not pending.get("profiles")
                       and not pending.get("cloudflare")):
        await _reply(update, context, "待添加的配置已过期，请重新发送 /uploadapi。")
        return
    profiles = pending.get("profiles") or []
    cloudflare = pending.get("cloudflare")
    results = []
    added_vars = {}
    if profiles:
        try:
            results = await run_sync(commit_oci_profiles, profiles)
        except (ValueError, OSError) as exc:
            await _reply(update, context, f"❌ 写入失败：{exc}")
            return
    cf_saved = None
    if cloudflare:
        try:
            cf_saved = await run_sync(commit_cf_section, cloudflare)
        except OSError as exc:
            await _reply(update, context, f"❌ Cloudflare 凭据写入失败：{exc}")
            return
    if profiles:
        try:
            added_vars = await run_sync(add_profile_compartments, profiles)
        except OSError as exc:
            await _reply(update, context,
                         f"✅ 配置已写入，但账号切换变量添加失败：{exc}\n"
                         "请手动在 env 文件里加 MBOT_COMPARTMENT_<PROFILE> "
                         "后重启机器人。")
            context.user_data.pop("ua_pending", None)
            context.user_data.pop("ua_awaiting", None)
            return
    context.user_data.pop("ua_pending", None)
    context.user_data.pop("ua_awaiting", None)
    await _reply(update, context, build_oci_profiles_result_text(
        results, added_vars, OCI_CONFIG_PATH, cf_saved))


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
        await _reply(update, context, f"❌ 文件读取失败：{exc}")
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
        await _reply(update, context, error)
        return
    if not running:
        await _reply(update, context, "没有运行中的实例，无法占用内存。")
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
    await _reply(update, context, "\n".join(lines),
                 reply_markup=_mem_pick_keyboard(running, "mh", hogs))


@require_allowed
async def memballoon_callback(update, context):
    """回调 mh:pick:<idx> / mh:go:<idx>：选实例 -> 二次确认 -> 启动占用进程。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, "实例不存在或已过期，请重新打开菜单 23。")
        return
    name = inst.get("display-name") or "?"
    if action == "pick":
        record = load_mem_hogs().get(inst.get("id"))
        if record:
            await _reply(update, context,
                         "⚠️ 该实例已在占用内存中，拒绝重复启动。\n\n"
                         f"实例：{name}\nPID：{record.get('pid')}\n"
                         f"启动时间：{record.get('started')}\n"
                         "如需停止，请用菜单 24（清除缓存和占用）。")
            return
        await _reply(update, context,
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
    await _reply(update, context, "参数错误，请重新打开菜单 23。")


async def _memballoon_go(update, context, inst, name):
    """mh:go 执行体：重复检查 -> 解析 SSH 目标 -> 启动占用进程 -> 记 PID。"""
    instance_id = inst.get("id")
    hogs = load_mem_hogs()
    if instance_id in hogs:
        await _reply(update, context,
                     f"⚠️ 该实例已在占用内存中"
                     f"（PID {hogs[instance_id].get('pid')}），拒绝重复启动。")
        return
    try:
        target, error = await run_sync(
            resolve_instance_ssh_target, get_cli(context), inst)
    except Exception as exc:  # noqa: BLE001 - 查 IP 失败也要提示用户
        await _reply(update, context, fmt_error(exc))
        return
    if error:
        await _reply(update, context, f"❌ {error}")
        return
    await _reply(update, context, f"⏳ 正在 {name} 上启动内存占用进程…")
    try:
        pid = await run_sync(start_memhog_on_host, target)
    except RemoteCommandError as exc:
        await _reply(update, context, f"❌ 启动失败：{exc}")
        return
    record_memhog(instance_id, pid, target["host"])
    await _reply(update, context,
                 f"✅ 已在实例「{name}」上启动内存占用进程。\n"
                 f"PID：{pid}\n占用：约 25% 总内存\n"
                 "停止方法：菜单 24（清除缓存和占用）。")


@require_allowed
async def handle_memclear(update, context):
    """菜单 24：清除缓存和占用——列运行中实例，选一台。"""
    running, error = await _load_running_instances(context)
    if error is not None:
        await _reply(update, context, error)
        return
    if not running:
        await _reply(update, context, "没有运行中的实例，无法清除缓存和占用。")
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
    await _reply(update, context, "\n".join(lines),
                 reply_markup=_mem_pick_keyboard(running, "mc", hogs))


@require_allowed
async def memclear_callback(update, context):
    """回调 mc:pick:<idx> / mc:go:<idx>：选实例 -> 二次确认 -> 杀进程 + 清缓存。"""
    parts = _callback_data(update).split(":")
    action = parts[1] if len(parts) >= 2 else ""
    idx = parts[2] if len(parts) >= 3 else ""
    inst = resolve_instance(context, idx)
    if inst is None:
        await _reply(update, context, "实例不存在或已过期，请重新打开菜单 24。")
        return
    name = inst.get("display-name") or "?"
    if action == "pick":
        record = load_mem_hogs().get(inst.get("id"))
        if record:
            hog_line = (f"占用进程：PID {record.get('pid')}，"
                        f"启动于 {record.get('started')}，将被杀掉。")
        else:
            hog_line = "该实例无占用进程，只清缓存。"
        await _reply(update, context,
                     f"🧹 确认清除实例「{name}」的缓存和占用？\n\n"
                     f"{hog_line}\n"
                     "还将执行 sync + drop_caches 清理系统缓存。",
                     reply_markup=_mem_confirm_keyboard(
                         "mc", idx, "🧹 确认清除"))
        return
    if action == "go":
        await _memclear_go(update, context, inst, name)
        return
    await _reply(update, context, "参数错误，请重新打开菜单 24。")


async def _memclear_go(update, context, inst, name):
    """mc:go 执行体：解析 SSH 目标 -> 杀占用进程 -> 清缓存 -> 删记录 -> 汇报。"""
    instance_id = inst.get("id")
    record = load_mem_hogs().get(instance_id)
    pid = record.get("pid") if record else None
    try:
        target, error = await run_sync(
            resolve_instance_ssh_target, get_cli(context), inst)
    except Exception as exc:  # noqa: BLE001 - 查 IP 失败也要提示用户
        await _reply(update, context, fmt_error(exc))
        return
    if error:
        await _reply(update, context, f"❌ {error}")
        return
    use_sudo = (target.get("username") or "root") != "root"
    await _reply(update, context, f"⏳ 正在 {name} 上清除缓存和占用…")
    try:
        result = await run_sync(clear_memhog_on_host, target, pid, use_sudo)
    except RemoteCommandError as exc:
        await _reply(update, context, f"❌ 清除失败：{exc}")
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
    await _reply(update, context, "\n".join(lines))
