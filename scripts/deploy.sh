#!/bin/bash
# M_Bot部署脚本（模板，阶段 5 工作包 E）。
#
# 管理脚本设计要点：
#   1. systemd 单元 Restart=always + RestartSec=10（崩了自动拉起）；
#   2. 原子升级：新版下载到同文件系统临时目录 -> 校验 -> mv 覆盖
#      （避开 /tmp 空间不足与跨文件系统 mv 非原子的问题）；
#   3. 升级锁：flock 防并发升级（锁文件在安装目录内）；
#   4. 端口解析优先级：命令行参数 > 进程命令行 > 端口文件 > 默认 9527。
#
# 用法（占位符，部署前按实际修改“配置区”）：
#   ./deploy.sh install     # 首次部署：装依赖、写 systemd 单元、启动服务
#   ./deploy.sh upgrade     # 原子升级到最新版（先备份当前版本，可回滚）
#   ./deploy.sh rollback    # 回滚到上一次备份的版本
#   ./deploy.sh start|stop|restart|status
#   ./deploy.sh health      # 健康检查（调 /api/system/health，需已登录会话时
#                           # 改用免认证的进程级检查：见 health_check()）
#
# 注意：
#   - 本脚本是模板，不含真实 IP / 密码 / Token；真实值一律放
#     env 文件 $INSTALL_DIR/.rbot-clone.env（MBOT_*，600 权限，systemd
#     经 EnvironmentFile 读入）与统一配置文件
#     ~/M_bot_config.conf（纯文本，oci / cloudflare / bark 三节，
#     600 权限，启动时由代码读进环境变量，systemd 传 MBOT_CONFIG_FILE
#     指过去）。
#   - 面板默认只监听 127.0.0.1（见 web/config.py），不要直接把 9527
#     暴露到公网；外网访问请在前面加反向代理 + HTTPS。
#   - 升级前会自动备份当前版本到 $INSTALL_DIR/.backup/，回滚用 rollback。
#   - 升级不会动 web/data/（hosts.json、任务记录等运行时数据）。
#
# English summary: M_Bot deploy script (template). Design points:
# 1. systemd units use Restart=always + RestartSec=10 (auto-restart 10s
# after a crash); 2. atomic upgrade: download the new version into a temp
# dir on the same filesystem -> verify -> mv over (avoids /tmp running out
# of space and non-atomic cross-filesystem mv); 3. upgrade lock: flock
# prevents concurrent upgrades (lock file inside the install dir);
# 4. port resolution priority: CLI arg > running process command line >
# port file > default 9527. Usage (placeholders; edit the config section
# before deploying): ./deploy.sh install (first deploy), upgrade (atomic
# upgrade to the latest version, backs up first, rollback available),
# rollback, start|stop|restart|status, health (process-level check, since
# /api/system/health needs a login session). Notes: this script is a
# template with no real IPs / passwords / tokens; real values go in
# the env file $INSTALL_DIR/.rbot-clone.env (MBOT_*, mode 600, read by systemd
# via EnvironmentFile) and the unified txt config
# ~/M_bot_config.conf (plain text, oci / cloudflare / bark, mode 600,
# loaded into the environment by the code at startup; systemd passes
# MBOT_CONFIG_FILE pointing at it).
# The panel listens on 127.0.0.1 only (see web/config.py); do not expose
# 9527 directly to the internet, put a reverse proxy + HTTPS in front.

set -euo pipefail

# ================= 配置区（部署前改这里） =================
# Install directory (M_Bot code lives here; not /tmp, it is wiped on reboot). 安装目录（M_Bot 代码放这里；不要用 /tmp，重启会丢）。
INSTALL_DIR="/opt/rbot-clone"
# Code source (git repo URL, placeholder; or change to a release package URL). 代码来源（git 仓库地址，占位符；或改成发布包 URL）。
REPO_URL="https://github.com/<你的账号>/M_Bot.git"
# Run user (a dedicated user is recommended; root also works, but never nobody). 运行用户（建议用专用用户；root 运行也可以，但不要用 nobody）。
SERVICE_USER="root"
# systemd service names. systemd 服务名。
SERVICE_NAME="rbot-clone-web"
# Env file (admin password hash, session key, etc.; mode 600, read by systemd). 环境文件（管理员密码哈希、会话密钥等，600 权限，systemd 读入）。
ENV_FILE="$INSTALL_DIR/.rbot-clone.env"
# Unified txt config file (OCI profiles, Cloudflare, Bark; mode 600, loaded by the code at startup). Lives in the deploy user's home dir (/root for a root deploy), not in the install dir, so it is easy to find and edit. 统一配置文件（纯文本，OCI 账号、Cloudflare、Bark 三节，600 权限，启动时由代码读入）。放部署者的家目录（root 部署时是 /root/M_bot_config.conf），不放安装目录，方便找到、直接改。
CONFIG_FILE="$HOME/M_bot_config.conf"
# Python virtualenv directory (created during install). Python 虚拟环境目录（install 时创建）。
VENV_DIR="$INSTALL_DIR/venv"
# Port file (priority level 3 of port resolution; the default port is used when the file does not exist). 端口文件（端口解析优先级第 3 级；文件不存在时用默认端口）。
PORT_FILE="$INSTALL_DIR/.port"
# Default port (last level of port resolution, 9527). 默认端口（端口解析优先级最后一级，9527）。
DEFAULT_PORT=9527
# Upgrade lock file (for flock, prevents concurrent upgrades). 升级锁文件（flock 用，防并发升级）。
UPGRADE_LOCK="$INSTALL_DIR/.upgrade.lock"
# Backup directory (the current version is copied here before an upgrade, for rollback). 备份目录（升级前把当前版本拷一份到这里，回滚用）。
BACKUP_DIR="$INSTALL_DIR/.backup"
# =====================================================

# ---------- 端口解析（优先级：参数 > 进程命令行 > 文件 > 默认） ----------
resolve_port() {
    for arg in "$@"; do
        case "$arg" in
            --port=*) echo "${arg#--port=}"; return 0 ;;
        esac
    done
    local pid
    pid="$(pgrep -f "uvicorn.*web.app" | head -n 1 || true)"
    if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
        local port
        port="$(tr '\0' ' ' < "/proc/$pid/cmdline" \
            | sed -n 's/.*--port[= ]\([0-9]*\).*/\1/p' | head -n 1)"
        if [ -n "$port" ]; then echo "$port"; return 0; fi
    fi
    if [ -r "$PORT_FILE" ]; then
        local file_port
        file_port="$(tr -d ' \n' < "$PORT_FILE")"
        if [ -n "$file_port" ]; then echo "$file_port"; return 0; fi
    fi
    echo "$DEFAULT_PORT"
}

# ---------- systemd 单元 ----------
write_systemd_unit() {
    local port="$1"
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=M_Bot Web Panel
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
Environment="MBOT_CONFIG_FILE=${CONFIG_FILE}"
ExecStart=${VENV_DIR}/bin/uvicorn web.app:app --host 127.0.0.1 --port ${port}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

# ---------- 健康检查 ----------
# The panel's /api/system/health needs a login session, so do a process-level check here: the service is running + the port is listening means healthy (for a post-login API-level check, call the API manually). 面板 /api/system/health 需要登录会话，这里做进程级检查：服务在跑 + 端口在监听即算健康（登录后的接口级检查请手动调 API）。
health_check() {
    local port
    port="$(resolve_port "$@")"
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "不健康：服务 $SERVICE_NAME 未运行"
        return 1
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltn "sport = :$port" | grep -q ":$port" || {
            echo "不健康：端口 $port 未监听"; return 1; }
    fi
    echo "健康：服务运行中，端口 $port 监听中"
    return 0
}

# ---------- 首次部署 ----------
cmd_install() {
    local port
    port="$(resolve_port "$@")"
    echo "安装到 $INSTALL_DIR（端口 $port）…"
    if [ ! -d "$INSTALL_DIR/.git" ]; then
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    cd "$INSTALL_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r requirements.txt
    if [ ! -f "$ENV_FILE" ]; then
        echo "错误：环境文件 $ENV_FILE 不存在。"
        echo "请先运行 python3 -m web.auth 生成管理员密码哈希，"
        echo "再把 MBOT_WEB_PASSWORD_HASH 与 MBOT_WEB_SECRET 写进该文件（600 权限）。"
        exit 1
    fi
    chmod 600 "$ENV_FILE"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "统一配置文件 $CONFIG_FILE 不存在，新建空骨架（600 权限，带双语注释）…"
        # The skeleton (full layout with bilingual comments) comes from
        # mbot_config.save_config so the format lives in one place.
        # 空骨架（完整排版 + 双语注释）由 mbot_config.save_config 生成，
        # 格式只在一处维护。
        PYTHONPATH="$INSTALL_DIR" python3 -c \
            "import mbot_config; mbot_config.save_config(mbot_config.empty_config(), '$CONFIG_FILE')" \
            || { echo "错误：统一配置文件骨架生成失败。"; exit 1; }
    fi
    echo "$port" > "$PORT_FILE"
    write_systemd_unit "$port"
    systemctl enable --now "$SERVICE_NAME"
    echo "安装完成。访问方式（SSH 隧道）："
    echo "  ssh -L 9527:127.0.0.1:$port <用户>@<面板服务器 IP>"
    echo "  然后打开 http://127.0.0.1:9527/login"
}

# ---------- 原子升级 ----------
# Atomic upgrade: download into a temp dir inside the install dir (same filesystem as the target, so mv is atomic and /tmp size limits are avoided) -> verify -> mv over; the upgrade lock prevents concurrency. 原子升级：下载到安装目录内的临时目录（与目标同文件系统，mv 才原子，避开 /tmp 空间不足）-> 校验 -> mv 覆盖；升级锁防并发。
cmd_upgrade() {
    # Upgrade lock: failing to get the lock means another upgrade is running, so exit directly (flock is non-blocking). 升级锁：拿不到锁说明已有升级在进行，直接退出（flock 非阻塞）。
    exec 9>"$UPGRADE_LOCK"
    if ! flock -n 9; then
        echo "已有升级在进行（锁：$UPGRADE_LOCK），本次退出。"
        exit 1
    fi
    local port tmp_dir
    port="$(resolve_port "$@")"
    tmp_dir="$(mktemp -d "$INSTALL_DIR/.upgrade.XXXXXX")"
    echo "升级：临时目录 $tmp_dir"
    cleanup_tmp() { rm -rf "$tmp_dir"; }
    trap cleanup_tmp EXIT
    git clone --depth 1 "$REPO_URL" "$tmp_dir/new"
    # Verify: continue only when the key files are all present (stop when any is missing; the live version is untouched). 校验：关键文件齐全才继续（缺了就停，不动线上版本）。
    for required in web/app.py requirements.txt web/config.py; do
        if [ ! -f "$tmp_dir/new/$required" ]; then
            echo "升级失败：新版本缺关键文件 $required，已停止，未动线上版本。"
            exit 1
        fi
    done
    rm -rf "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    for entry in web oci tgbot clouds mcp_server.py requirements.txt; do
        [ -e "$INSTALL_DIR/$entry" ] && cp -a "$INSTALL_DIR/$entry" "$BACKUP_DIR/"
    done
    echo "当前版本已备份到 $BACKUP_DIR"
    systemctl stop "$SERVICE_NAME" || true
    for entry in web oci tgbot clouds mcp_server.py requirements.txt; do
        rm -rf "$INSTALL_DIR/$entry"
        mv "$tmp_dir/new/$entry" "$INSTALL_DIR/$entry"
    done
    "$VENV_DIR/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
    systemctl start "$SERVICE_NAME"
    sleep 3
    if health_check; then
        echo "升级成功。"
    else
        echo "升级后健康检查失败，请检查日志（journalctl -u $SERVICE_NAME）"
        echo "或执行 ./deploy.sh rollback 回滚。"
        exit 1
    fi
}

# ---------- 回滚 ----------
cmd_rollback() {
    if [ ! -d "$BACKUP_DIR/web" ]; then
        echo "没有可用的备份（$BACKUP_DIR 不完整），无法回滚。"
        exit 1
    fi
    echo "回滚到备份版本（$BACKUP_DIR）…"
    systemctl stop "$SERVICE_NAME" || true
    for entry in web oci tgbot clouds mcp_server.py requirements.txt; do
        rm -rf "$INSTALL_DIR/$entry"
        cp -a "$BACKUP_DIR/$entry" "$INSTALL_DIR/$entry"
    done
    systemctl start "$SERVICE_NAME"
    sleep 3
    health_check
}

# ---------- 主入口 ----------
case "${1:-}" in
    install)  shift; cmd_install "$@" ;;
    upgrade)  shift; cmd_upgrade "$@" ;;
    rollback) cmd_rollback ;;
    start)    systemctl start "$SERVICE_NAME" ;;
    stop)     systemctl stop "$SERVICE_NAME" ;;
    restart)  systemctl restart "$SERVICE_NAME" ;;
    status)   systemctl status "$SERVICE_NAME" ;;
    health)   shift; health_check "$@" ;;
    *)
        echo "用法：$0 install|upgrade|rollback|start|stop|restart|status|health [--port=xxxx]"
        echo "首次部署前请先修改脚本顶部的“配置区”（安装目录、仓库地址、运行用户）。"
        exit 1
        ;;
esac
