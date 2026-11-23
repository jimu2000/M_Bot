#!/bin/bash
# R-Bot 开源克隆版部署脚本（模板，阶段 5 工作包 E）。
#
# 借鉴自 R-Bot 管理脚本 sh_client_bot.sh（见 docs/rbot-sh_client_bot.sh）：
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
#     /root/.rbot-clone.env（600 权限），由 systemd 经 EnvironmentFile 读入。
#   - 面板默认只监听 127.0.0.1（见 web/config.py），不要直接把 9527
#     暴露到公网；外网访问请在前面加反向代理 + HTTPS。
#   - 升级前会自动备份当前版本到 $INSTALL_DIR/.backup/，回滚用 rollback。
#   - 升级不会动 web/data/（hosts.json、任务记录等运行时数据）。

set -euo pipefail

# ================= 配置区（部署前改这里） =================
# 安装目录（克隆版代码放这里；不要用 /tmp，重启会丢）。
INSTALL_DIR="/opt/rbot-clone"
# 代码来源（git 仓库地址，占位符；或改成发布包 URL）。
REPO_URL="https://github.com/<你的账号>/rbot-clone.git"
# 运行用户（建议用专用用户；root 运行也可以，但不要用 nobody）。
SERVICE_USER="root"
# systemd 服务名。
SERVICE_NAME="rbot-clone-web"
# 环境文件（管理员密码哈希、会话密钥等，600 权限，systemd 读入）。
ENV_FILE="/root/.rbot-clone.env"
# Python 虚拟环境目录（install 时创建）。
VENV_DIR="$INSTALL_DIR/venv"
# 端口文件（端口解析优先级第 3 级；文件不存在时用默认端口）。
PORT_FILE="$INSTALL_DIR/.port"
# 默认端口（端口解析优先级最后一级，与 R-Bot 一致用 9527）。
DEFAULT_PORT=9527
# 升级锁文件（flock 用，防并发升级）。
UPGRADE_LOCK="$INSTALL_DIR/.upgrade.lock"
# 备份目录（升级前把当前版本拷一份到这里，回滚用）。
BACKUP_DIR="$INSTALL_DIR/.backup"
# =====================================================

# ---------- 端口解析（优先级：参数 > 进程命令行 > 文件 > 默认） ----------
# 与 R-Bot 的端口解析优先级一致（Java 传入 > 进程命令行 --server.port >
# 监听探测 > .r_client.port 文件 > 默认 9527），克隆版简化为四级。
resolve_port() {
    # 第 1 级：命令行参数 --port=xxxx（deploy.sh 的第一个参数透传时用）。
    for arg in "$@"; do
        case "$arg" in
            --port=*) echo "${arg#--port=}"; return 0 ;;
        esac
    done
    # 第 2 级：运行中进程的命令行（uvicorn --port xxxx）。
    local pid
    pid="$(pgrep -f "uvicorn.*web.app" | head -n 1 || true)"
    if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
        local port
        port="$(tr '\0' ' ' < "/proc/$pid/cmdline" \
            | sed -n 's/.*--port[= ]\([0-9]*\).*/\1/p' | head -n 1)"
        if [ -n "$port" ]; then echo "$port"; return 0; fi
    fi
    # 第 3 级：端口文件。
    if [ -r "$PORT_FILE" ]; then
        local file_port
        file_port="$(tr -d ' \n' < "$PORT_FILE")"
        if [ -n "$file_port" ]; then echo "$file_port"; return 0; fi
    fi
    # 第 4 级：默认端口。
    echo "$DEFAULT_PORT"
}

# ---------- systemd 单元 ----------
# Restart=always + RestartSec=10：进程崩了 10 秒后自动拉起
# （借鉴 R-Bot 的 rbot.service：Type=simple，Restart=always，RestartSec=10）。
write_systemd_unit() {
    local port="$1"
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=R-Bot Open Source Clone Web Panel
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn web.app:app --host 127.0.0.1 --port ${port}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

# ---------- 健康检查 ----------
# 面板 /api/system/health 需要登录会话，这里做进程级检查：
# 服务在跑 + 端口在监听即算健康（登录后的接口级检查请手动调 API）。
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
        echo "再把 RBOT_WEB_PASSWORD_HASH 与 RBOT_WEB_SECRET 写进该文件（600 权限）。"
        exit 1
    fi
    chmod 600 "$ENV_FILE"
    echo "$port" > "$PORT_FILE"
    write_systemd_unit "$port"
    systemctl enable --now "$SERVICE_NAME"
    echo "安装完成。访问方式（SSH 隧道）："
    echo "  ssh -L 9527:127.0.0.1:$port <用户>@<面板服务器 IP>"
    echo "  然后打开 http://127.0.0.1:9527/login"
}

# ---------- 原子升级 ----------
# 借鉴 R-Bot 的原子升级：下载到安装目录内的临时目录（与目标同文件系统，
# mv 才原子，避开 /tmp 空间不足）-> 校验 -> mv 覆盖；升级锁防并发。
cmd_upgrade() {
    # 升级锁：拿不到锁说明已有升级在进行，直接退出（flock 非阻塞）。
    exec 9>"$UPGRADE_LOCK"
    if ! flock -n 9; then
        echo "已有升级在进行（锁：$UPGRADE_LOCK），本次退出。"
        exit 1
    fi
    local port tmp_dir
    port="$(resolve_port "$@")"
    tmp_dir="$(mktemp -d "$INSTALL_DIR/.upgrade.XXXXXX")"
    echo "升级：临时目录 $tmp_dir"
    # 清理函数：成功或失败都删临时目录（trap 保证执行）。
    cleanup_tmp() { rm -rf "$tmp_dir"; }
    trap cleanup_tmp EXIT
    git clone --depth 1 "$REPO_URL" "$tmp_dir/new"
    # 校验：关键文件齐全才继续（缺了就停，不动线上版本）。
    for required in web/app.py requirements.txt web/config.py; do
        if [ ! -f "$tmp_dir/new/$required" ]; then
            echo "升级失败：新版本缺关键文件 $required，已停止，未动线上版本。"
            exit 1
        fi
    done
    # 备份当前版本（回滚用；只备代码，不备 web/data 与 venv）。
    rm -rf "$BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
    for entry in web oci tgbot clouds mcp_server.py requirements.txt; do
        [ -e "$INSTALL_DIR/$entry" ] && cp -a "$INSTALL_DIR/$entry" "$BACKUP_DIR/"
    done
    echo "当前版本已备份到 $BACKUP_DIR"
    # 停服务 -> mv 覆盖（同文件系统，原子）-> 装依赖 -> 起服务。
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
