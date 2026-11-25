#!/usr/bin/env python3
"""客户端本机操作：systemd 服务状态/重启、客户端升级、UFW 放行端口。

供 Telegram 机器人菜单项调用（客户端管理 / 重启机器人 / 升级客户端 /
开放云面板端口）。全部操作都在本机（机器人所在服务器）执行；UFW
放行支持 ssh 到另一台面板服务器执行。

安全说明（重要）：
  - 本模块只跑 systemctl / git / pip / ufw / ssh 这几个白名单命令，
    不拼接 shell 字符串（subprocess 列表参数，无 shell=True），
    服务名与端口都先校验再执行。
  - 重启机器人服务会杀掉当前进程：调用方必须先把“正在重启”消息
    发给用户，再调 restart_service。
  - 升级会重启面板与机器人：调用方必须先二次确认并提示约 1 分钟不可用。
  - 单元测试一律 mock subprocess.run，绝不真的执行 systemctl/git/pip。

用法示例：
    from tgbot.client_ops import service_status, restart_service
    service_status("rbot-clone-web")   # "active" / "inactive" / ...
    restart_service("rbot-clone-bot")  # 失败时抛 RuntimeError
"""

import os
import subprocess
import sys

# ================= 配置区（改这里） =================
# The two systemd service names (the deployed systemd units carry exactly these names). 两个 systemd 服务名（部署时 systemd 单元就是这两个名字）。
WEB_SERVICE = "rbot-clone-web"
BOT_SERVICE = "rbot-clone-bot"
# Service list (order matches the Telegram client-management menu button indexes: 0 = panel, 1 = bot). 服务列表（顺序与 Telegram 客户端管理菜单的按钮下标一致：0=面板，1=机器人）。
SERVICES = (WEB_SERVICE, BOT_SERVICE)
# Cloud panel port (used for the UFW allow rule). 云面板端口（UFW 放行用）。
PANEL_PORT = 9527
# Project root directory (defaults to the repo root of this file; /opt/rbot-clone when deployed there; tests can override it with the path argument). 项目根目录（默认取本文件所在仓库的根目录；部署在 /opt/rbot-clone 时就是 /opt/rbot-clone；测试时可传 path 参数覆盖）。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Timeout (seconds) per command. 各命令的超时（秒）。
SYSTEMCTL_TIMEOUT = 60
GIT_TIMEOUT = 120
PIP_TIMEOUT = 300
UFW_TIMEOUT = 30
SSH_TIMEOUT = 30
# SSH port and user for the UFW allow rule to the remote panel server. UFW 放行到远程面板服务器时的 SSH 端口与用户。
SSH_PORT = 14422
SSH_USER = "root"
# =====================================================

# Possible systemctl is-active outputs (anything outside the allowlist counts as unknown). systemctl is-active 可能返回的状态（白名单之外的输出一律当 unknown）。
_KNOWN_STATES = ("active", "inactive", "failed", "activating",
                 "deactivating", "reloading")


def _run(cmd, timeout):
    """跑一条命令，返回 CompletedProcess。

    找不到可执行文件时抛 FileNotFoundError（调用方按需处理）；
    超时抛 subprocess.TimeoutExpired。
    """
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)


def service_status(service):
    """查一个 systemd 服务的状态。

    跑 `systemctl is-active <服务>`：返回 "active" / "inactive" /
    "failed" 等；systemctl 不存在、没 systemd 或输出无法识别时
    返回 "unknown"（不抛异常，调用方可以直接展示）。

    注意：is-active 对非 active 的服务返回非 0 退出码，这是正常情况，
    不能按“退出码非 0 = 出错”处理。
    """
    if service not in SERVICES:
        raise ValueError(f"未知的服务名：{service}（只允许 {SERVICES}）")
    try:
        result = _run(["systemctl", "is-active", service],
                      timeout=SYSTEMCTL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    state = (result.stdout or "").strip()
    return state if state in _KNOWN_STATES else "unknown"


def restart_service(service):
    """重启一个 systemd 服务（`systemctl restart <服务>`）。

    成功返回 None；失败抛 RuntimeError（带 stderr 摘要）。
    注意：重启 rbot-clone-bot 会杀掉当前机器人进程，调用方必须先
    把“正在重启”消息发给用户。
    """
    if service not in SERVICES:
        raise ValueError(f"未知的服务名：{service}（只允许 {SERVICES}）")
    try:
        result = _run(["systemctl", "restart", service],
                      timeout=SYSTEMCTL_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"重启 {service} 超时（{SYSTEMCTL_TIMEOUT} 秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"重启 {service} 失败：无法执行 systemctl（{exc}）") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"重启 {service} 失败：{detail[:300]}")
    return None


def is_git_repo(path=None):
    """指定目录（默认项目根目录）是否在 git 仓库里。

    跑 `git -C <目录> rev-parse --is-inside-work-tree`，输出 "true"
    且退出码 0 时返回 True；其余情况一律返回 False（不抛异常）。
    """
    path = path or PROJECT_ROOT
    try:
        result = _run(["git", "-C", path, "rev-parse",
                       "--is-inside-work-tree"], timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def upgrade_client(path=None):
    """升级客户端：git pull + pip install -r requirements.txt + 重启两个服务。

    步骤：
      1. 不是 git 仓库时抛 ValueError（调用方提示用户手动升级）；
      2. `git pull --ff-only`（有冲突时失败，不强行合并）；
      3. 用当前 Python 解释器 `pip install -r requirements.txt`
         （保证装进机器人所在的 venv）；
      4. 先重启面板服务，再重启机器人服务（重启机器人会杀掉当前进程，
         调用方必须在调用前把“开始升级”消息发给用户）。

    成功返回 git pull 输出的最后一行（摘要，如 "Already up to date."）；
    任一步失败抛 RuntimeError（已执行的步骤不回滚）。
    """
    path = path or PROJECT_ROOT
    if not is_git_repo(path):
        raise ValueError("当前目录不是 git 仓库，无法自动升级，请手动升级")
    try:
        pull = _run(["git", "-C", path, "pull", "--ff-only"],
                    timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git pull 超时（{GIT_TIMEOUT} 秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"git pull 失败：无法执行 git（{exc}）") from exc
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "").strip()
        raise RuntimeError(f"git pull 失败：{detail[:300]}")
    requirements = os.path.join(path, "requirements.txt")
    try:
        pip = _run([sys.executable, "-m", "pip", "install", "-r",
                    requirements], timeout=PIP_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"pip install 超时（{PIP_TIMEOUT} 秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"pip install 失败：无法执行 pip（{exc}）") from exc
    if pip.returncode != 0:
        detail = (pip.stderr or pip.stdout or "").strip()
        raise RuntimeError(f"pip install 失败：{detail[:300]}")
    # Restart the panel first, then the bot (restarting the bot kills the current process). 先重启面板，再重启机器人（重启机器人会杀掉当前进程）。
    restart_service(WEB_SERVICE)
    restart_service(BOT_SERVICE)
    lines = [line for line in (pull.stdout or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else "Already up to date."


def ufw_allow_port(port, host=None, ssh_port=SSH_PORT, ssh_user=SSH_USER):
    """UFW 放行一个 TCP 端口。

    host 为空时在本机执行 `ufw allow <端口>/tcp`（面板服务器就是本机
    的情况）；host 不为空时 ssh 到该主机执行（密钥登录，BatchMode，
    不交互问密码）。

    成功返回 None；失败抛 RuntimeError。端口范围 1-65535，否则 ValueError。
    """
    try:
        port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"端口必须是 1-65535 的整数，收到 {port!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"端口必须在 1-65535 之间，收到 {port}")
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               "-p", str(ssh_port), f"{ssh_user}@{host}",
               "ufw", "allow", f"{port}/tcp"]
        timeout = SSH_TIMEOUT
    else:
        cmd = ["ufw", "allow", f"{port}/tcp"]
        timeout = UFW_TIMEOUT
    try:
        result = _run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"UFW 放行 {port}/tcp 超时（{timeout} 秒）") from exc
    except OSError as exc:
        raise RuntimeError(f"UFW 放行 {port}/tcp 失败：无法执行命令（{exc}）") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"UFW 放行 {port}/tcp 失败：{detail[:300]}")
    return None
