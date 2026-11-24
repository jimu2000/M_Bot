#!/bin/bash
# ============================================================================
# M_Bot 一键安装脚本（Debian / Ubuntu，root 运行）
#
# 功能：首次安装、更新重启、启动、停止、重启、状态、日志、卸载。
# 同一条命令首次运行是安装，之后运行是更新并重启（配置不丢）。
#
# 一键安装：
#   wget -O install.sh https://raw.githubusercontent.com/jimu2000/M_Bot/main/install.sh \
#     && chmod +x install.sh && bash install.sh
#
# 管理命令（安装后，在任意目录运行）：
#   bash install.sh start|stop|restart|status|log|upgrade|uninstall
#
# 安装前准备（缺一不可，装完机器人才能工作）：
#   1. Telegram 机器人 Token：找 @BotFather 新建机器人拿到
#      （格式如 123456789:AAE...，装到一半会问你）。
#   2. 你的 Telegram 用户 ID：找 @userinfobot 发 /start 拿到
#      （纯数字，装到一半会问你；白名单只放这个 ID）。
#   3. OCI API 密钥：安装后在机器人里用 /uploadapi 上传你的
#      OCI 配置（~/.oci/config + API 私钥），或手动放到 ~/.oci/。
#
# 非交互安装（跳过提问，变量缺了就用默认值/自动生成）：
#   MBOT_INSTALL_TOKEN=xxx MBOT_INSTALL_USER_ID=123 \
#     MBOT_INSTALL_PASSWORD=xxx MBOT_INSTALL_DOMAIN=panel.example.com \
#     bash install.sh
#
# 注意：
#   - 面板只绑 127.0.0.1:9527，不直接对外；外网访问走 nginx 反代 + HTTPS。
#   - 有域名：certbot 自动申请 Let's Encrypt 证书，访问 https://你的域名。
#   - 没域名：openssl 生成自签证书，访问 https://服务器IP:9527
#     （浏览器会提示证书不受信任，点“继续访问”即可）。
#   - 管理员密码不给就自动生成，只显示一次，并存到
#     /root/.mbot-admin-password（600 权限）。
#   - UFW 开启前会先放行当前 SSH 端口，不会把自己锁在外面。
# ============================================================================

set -euo pipefail

# ================= 配置区（改这里） =================
# 代码仓库（git clone 用）。
REPO_URL="https://github.com/jimu2000/M_Bot.git"
# 安装目录（代码放这里；不要用 /tmp，重启会丢）。
INSTALL_DIR="/opt/mbot"
# 环境文件（Token、密码哈希、会话密钥等，600 权限，systemd 读入）。
ENV_FILE="/root/.mbot.env"
# 管理员密码明文（自动生成时才写，600 权限，只显示一次后请妥善保存）。
ADMIN_PASSWORD_FILE="/root/.mbot-admin-password"
# Python 虚拟环境目录（install 时创建）。
VENV_DIR="$INSTALL_DIR/venv"
# systemd 服务名。
WEB_SERVICE="mbot-web"
BOT_SERVICE="mbot-bot"
# 面板端口（nginx 对外监听的端口，用户访问用；有域名时是 443，
# 没域名时是这个端口）。
PANEL_PORT=9527
# 面板内部端口（uvicorn 只绑 127.0.0.1:INTERNAL_PORT，nginx 反代到它）。
# 必须和 PANEL_PORT 不同：nginx 绑 0.0.0.0:9527 后，uvicorn 再绑
# 127.0.0.1:9527 会报 address already in use（Linux 上通配绑定会占
# 住该端口的所有地址，包括回环；2026-11-24 首尔测试机实测）。
INTERNAL_PORT=18001
# nginx 反代配置路径（conf.d 里的独立文件，不动用户已有配置）。
NGINX_CONF="/etc/nginx/conf.d/mbot-panel.conf"
# 自签证书目录（没域名时用）。
SELFSIGN_DIR="/etc/nginx/ssl/mbot-selfsigned"
# =====================================================

# ---------- 输出 ----------
info()  { echo "[*] $*"; }
ok()    { echo "[+] $*"; }
warn()  { echo "[!] $*" >&2; }
die()   { echo "[x] $*" >&2; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

require_root() {
    [ "$(id -u)" = "0" ] || die "请用 root 运行（sudo -i 后再执行）。"
}

detect_os() {
    # 只支持 Debian / Ubuntu（apt 系）；其他系统直接退出，不瞎装。
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "${ID:-}" in
            debian|ubuntu) return 0 ;;
        esac
    fi
    die "只支持 Debian / Ubuntu（当前系统：${ID:-未知}），请手动按 README 部署。"
}

# ---------- 依赖 ----------
# 新装系统首次启动时 unattended-upgrades / apt-daily 会占着 apt 锁，
# 直接 apt-get 会报 Could not get lock，set -e 下脚本当场退出
# （2026-11-24 首尔测试机实测：刚开机的 Ubuntu 24.04 必然撞上）。
apt_locked() {
    # 优先 fuser（psmisc，未装时回落 pgrep；都没有就当没锁）。
    # 注意：不要匹配 unattended-upgrade 进程本身——
    # unattended-upgrade-shutdown --wait-for-signal 是常驻守护，
    # 不占 apt 锁，匹配它会永远等下去。
    if command_exists fuser; then
        fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
            || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1
        return $?
    fi
    if command_exists pgrep; then
        pgrep -x apt >/dev/null 2>&1 \
            || pgrep -x apt-get >/dev/null 2>&1 \
            || pgrep -x dpkg >/dev/null 2>&1
        return $?
    fi
    return 1
}

wait_for_apt() {
    local waited=0
    while apt_locked; do
        if [ "$waited" -ge 600 ]; then
            die "等 apt 锁超过 10 分钟（可能系统更新卡住了），请稍后再试。"
        fi
        [ "$waited" = "0" ] && info "apt 正被系统更新占用，等锁释放…"
        sleep 10
        waited=$((waited + 10))
    done
}

install_deps() {
    info "安装系统依赖（python3 / git / nginx / ufw / certbot）…"
    export DEBIAN_FRONTEND=noninteractive
    wait_for_apt
    # 等锁与 apt-get 之间仍可能有竞态（锁刚被别的进程抢走），
    # update / install 各重试 3 次，失败才退出。
    local attempt=0
    until apt-get update -qq; do
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && die "apt-get update 多次失败，请检查网络与 apt 源。"
        warn "apt-get update 失败（可能锁被占），15 秒后重试…"
        sleep 15
        wait_for_apt
    done
    attempt=0
    until apt-get install -y -qq python3 python3-venv python3-pip git nginx \
        ufw curl wget openssl iproute2 procps psmisc >/dev/null; do
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && die "apt 安装依赖失败，请检查网络与 apt 源。"
        warn "apt 安装失败（可能锁被占），15 秒后重试…"
        sleep 15
        wait_for_apt
    done
    ok "系统依赖已就绪。"
}

# ---------- 交互提问 ----------
# 已有环境文件时不重复问（更新重启不丢配置）；非交互变量优先。
prompt_config() {
    if [ -f "$ENV_FILE" ]; then
        info "检测到已有配置 $ENV_FILE，跳过提问（更新模式）。"
        FRESH_INSTALL=0
        return 0
    fi
    FRESH_INSTALL=1
    echo
    echo "=== M_Bot 安装配置（Token 与用户 ID 必填，密码/域名可留空） ==="
    # Token：环境变量 > 交互输入。
    # 注意：read 后面都要加 || true——nohup / 非交互运行时 stdin
    # 可能是 EOF，read 返回非零，set -e 会直接杀掉脚本。
    # EOF 时变量保持为空，后面按"未提供"处理（Token 缺了会报错，
    # 密码/域名缺了走默认值）。
    TOKEN="${MBOT_INSTALL_TOKEN:-}"
    if [ -z "$TOKEN" ]; then
        printf "Telegram 机器人 Token（@BotFather 处获得）："
        read -r TOKEN || true
    fi
    [ -n "$TOKEN" ] || die "Token 不能为空（去 @BotFather 建机器人再来）。"
    case "$TOKEN" in
        *:*[A-Za-z0-9_-]*) : ;;
        *) die "Token 格式不对，应为 数字:字母数字 的形式。" ;;
    esac
    # 用户 ID：环境变量 > 交互输入。
    USER_ID="${MBOT_INSTALL_USER_ID:-}"
    if [ -z "$USER_ID" ]; then
        printf "你的 Telegram 用户 ID（纯数字，@userinfobot 发 /start 查看）："
        read -r USER_ID || true
    fi
    case "$USER_ID" in
        *[!0-9]*|"") die "用户 ID 必须是纯数字。" ;;
    esac
    # 管理员密码：环境变量 > 交互输入 > 自动生成。
    ADMIN_PASSWORD="${MBOT_INSTALL_PASSWORD:-}"
    if [ -z "$ADMIN_PASSWORD" ]; then
        printf "面板管理员密码（留空自动生成 16 位随机密码）："
        read -r -s ADMIN_PASSWORD || true
        echo
    fi
    if [ -z "$ADMIN_PASSWORD" ]; then
        ADMIN_PASSWORD="$(openssl rand -base64 12 | tr -d '+/=' | head -c 16)"
        GENERATED_PASSWORD=1
    else
        GENERATED_PASSWORD=0
    fi
    # 域名：环境变量 > 交互输入 > 留空（自签证书 + IP 访问）。
    DOMAIN="${MBOT_INSTALL_DOMAIN:-}"
    if [ -z "${MBOT_INSTALL_DOMAIN+x}" ]; then
        printf "面板域名（留空则用自签证书 + IP 访问，如 panel.example.com）："
        read -r DOMAIN || true
    fi
    if [ -n "$DOMAIN" ]; then
        case "$DOMAIN" in
            *.*) : ;;
            *) die "域名格式不对，应含至少一个点（如 panel.example.com）。" ;;
        esac
    fi
}

# ---------- 代码 ----------
install_code() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "更新代码（git pull）…"
        (cd "$INSTALL_DIR" && git pull --ff-only 2>/dev/null) \
            || warn "git pull 失败（可能有本地改动），用现有代码继续。"
    else
        info "克隆代码到 $INSTALL_DIR …"
        [ -e "$INSTALL_DIR" ] && die "$INSTALL_DIR 已存在但不是 git 仓库，请先移走它。"
        git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" \
            || die "git clone 失败，请检查网络与仓库地址。"
    fi
    [ -f "$INSTALL_DIR/requirements.txt" ] \
        || die "代码缺 requirements.txt，克隆不完整，请删除 $INSTALL_DIR 重试。"
    [ -f "$INSTALL_DIR/web/app.py" ] \
        || die "代码缺 web/app.py，克隆不完整，请删除 $INSTALL_DIR 重试。"
    ok "代码已就绪。"
}

setup_venv() {
    info "创建虚拟环境并安装 Python 依赖（含 oci-cli）…"
    [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip 安装依赖失败，请检查网络。"
    # OCI 调用走官方 OCI CLI（subprocess），requirements.txt 里没有，
    # 这里单独装进 venv，MBOT_OCI_CLI 指到它。
    "$VENV_DIR/bin/pip" install --quiet oci-cli \
        || die "pip 安装 oci-cli 失败，请检查网络。"
    ok "Python 依赖已就绪。"
}

# ---------- 环境文件 ----------
write_env_file() {
    [ -f "$ENV_FILE" ] && { info "环境文件已存在，不覆盖。"; return 0; }
    info "写入环境文件 $ENV_FILE（600 权限）…"
    local secret hash
    secret="$(openssl rand -hex 32)"
    # 密码经环境变量传给 Python，避免 shell 引用转义问题。
    # PYTHONPATH 必须指到安装目录：安装脚本的当前目录不一定是
    # /opt/mbot，python -c 不加 PYTHONPATH 就 import 不到 web.auth
    # （2026-11-24 首尔测试机实测：hash 生成失败就是这个原因）。
    # 出错时把 stderr 打出来，别只给一句"失败"，否则没法定位。
    hash="$(MBOT_PW="$ADMIN_PASSWORD" PYTHONPATH="$INSTALL_DIR" \
        "$VENV_DIR/bin/python" -c \
        "import os; from web.auth import hash_password; print(hash_password(os.environ['MBOT_PW']))" \
        2>/tmp/mbot_hash_err)" \
        || { cat /tmp/mbot_hash_err >&2; die "生成密码哈希失败。"; }
    [ -n "$hash" ] || die "生成密码哈希失败（空哈希）。"
    cat > "$ENV_FILE" <<EOF
# M_Bot 运行配置（install.sh 生成，600 权限，不要分享此文件）
MBOT_TELEGRAM_TOKEN=$TOKEN
MBOT_ALLOWED_USERS=$USER_ID
MBOT_WEB_PASSWORD_HASH=$hash
MBOT_WEB_SECRET=$secret
MBOT_COMPARTMENT_DEFAULT=ocid1.tenancy.oc1..xxxxxxxx
MBOT_OCI_CLI=$VENV_DIR/bin/oci
MBOT_TGBOT_DATA_DIR=$INSTALL_DIR/tgbot/data
MBOT_WEB_DATA_DIR=$INSTALL_DIR/web/data
EOF
    chmod 600 "$ENV_FILE"
    if [ "${GENERATED_PASSWORD:-0}" = "1" ]; then
        printf '%s\n' "$ADMIN_PASSWORD" > "$ADMIN_PASSWORD_FILE"
        chmod 600 "$ADMIN_PASSWORD_FILE"
    fi
    # 运行时数据目录（机器人与面板共用 tgbot/data，如 bark.json、
    # region_plans.json；面板数据在 web/data）。
    mkdir -p "$INSTALL_DIR/tgbot/data" "$INSTALL_DIR/web/data"
    ok "环境文件已写入。"
}

# ---------- systemd 单元 ----------
write_systemd_units() {
    info "写入 systemd 单元（$WEB_SERVICE / $BOT_SERVICE）…"
    cat > "/etc/systemd/system/${WEB_SERVICE}.service" <<EOF
[Unit]
Description=M_Bot web panel (FastAPI + uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"
Environment="SUPPRESS_LABEL_WARNING=True"
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn web.app:app --host 127.0.0.1 --port ${INTERNAL_PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    cat > "/etc/systemd/system/${BOT_SERVICE}.service" <<EOF
[Unit]
Description=M_Bot Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin"
Environment="SUPPRESS_LABEL_WARNING=True"
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python -m tgbot.bot
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now "$WEB_SERVICE" "$BOT_SERVICE" \
        || die "启动服务失败，请看 journalctl -u $WEB_SERVICE / -u $BOT_SERVICE。"
    ok "服务已启动（开机自启）。"
}

# ---------- nginx 反代 ----------
# 面板只绑 127.0.0.1:9527；nginx 对外反代并加 HTTPS + WebSocket 支持。
write_nginx_selfsigned() {
    local cert_dir="$SELFSIGN_DIR"
    mkdir -p "$cert_dir"
    if [ ! -f "$cert_dir/fullchain.pem" ] || [ ! -f "$cert_dir/privkey.pem" ]; then
        info "生成自签证书（有效期 10 年，仅用于加密传输）…"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -keyout "$cert_dir/privkey.pem" \
            -out "$cert_dir/fullchain.pem" \
            -subj "/CN=$(hostname)" >/dev/null 2>&1 \
            || die "自签证书生成失败。"
    fi
    cat > "$NGINX_CONF" <<EOF
# M_Bot 面板反代（install.sh 生成，自签证书，监听 ${PANEL_PORT}）
server {
    listen ${PANEL_PORT} ssl;
    listen [::]:${PANEL_PORT} ssl;
    server_name _;

    ssl_certificate ${cert_dir}/fullchain.pem;
    ssl_certificate_key ${cert_dir}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 60s;
    }
}
EOF
    nginx -t >/dev/null 2>&1 || die "nginx 配置校验失败，请检查 $NGINX_CONF。"
    systemctl reload nginx 2>/dev/null || systemctl restart nginx \
        || die "nginx 重载失败，请看 journalctl -u nginx。"
    ok "nginx 反代已生效（自签证书，端口 ${PANEL_PORT}）。"
}

write_nginx_domain() {
    local domain="$1"
    # 先写纯 HTTP 的 vhost（certbot 需要它做 http-01 验证），
    # 再让 certbot --nginx 自动加上 HTTPS。
    cat > "$NGINX_CONF" <<EOF
# M_Bot 面板反代（install.sh 生成，Let's Encrypt，域名 ${domain}）
server {
    listen 80;
    listen [::]:80;
    server_name ${domain};

    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 60s;
    }
}
EOF
    nginx -t >/dev/null 2>&1 || die "nginx 配置校验失败，请检查 $NGINX_CONF。"
    systemctl reload nginx 2>/dev/null || systemctl restart nginx \
        || die "nginx 重载失败，请看 journalctl -u nginx。"
    # certbot 只在有域名时才需要，这里按需安装，装不上也不致命。
    if ! command_exists certbot; then
        info "安装 certbot（域名证书用）…"
        wait_for_apt
        apt-get install -y -qq certbot python3-certbot-nginx >/dev/null 2>&1 \
            || warn "certbot 安装失败，只能用自签证书。"
    fi
    if command_exists certbot; then
        info "申请 Let's Encrypt 证书（域名 $domain，需该域名已解析到本机）…"
        if certbot --nginx -d "$domain" --non-interactive --agree-tos \
                --register-unsafely-without-email --redirect >/dev/null 2>&1; then
            ok "Let's Encrypt 证书已申请并生效。"
            CERT_OK=1
            return 0
        fi
        warn "certbot 申请证书失败（域名可能未解析到本机），回落自签证书。"
    else
        warn "未装 certbot，回落自签证书。"
    fi
    CERT_OK=0
    write_nginx_selfsigned
}

setup_nginx() {
    # 9527 已被非 nginx 进程占用时先停下来（常见于装过其他面板），
    # 否则 nginx 绑不上端口，用户会看到一片空白。
    if command_exists ss; then
        local holder
        holder="$(ss -ltnp 2>/dev/null | grep ":${PANEL_PORT} " | head -n 1 || true)"
        if [ -n "$holder" ]; then
            case "$holder" in
                *nginx*) : ;;  # 已是我们自己的 nginx，重装时正常
                *) die "端口 ${PANEL_PORT} 已被占用：${holder}。请先停掉它再安装。" ;;
            esac
        fi
    fi
    if [ -n "${DOMAIN:-}" ]; then
        write_nginx_domain "$DOMAIN"
    else
        write_nginx_selfsigned
    fi
}

# ---------- UFW ----------
# 开启前先放行当前 SSH 端口（sshd 实际监听端口 > sshd_config > 22），
# 否则 ufw enable 会把自己锁在外面。
detect_ssh_port() {
    local port=""
    if command_exists ss; then
        port="$(ss -tlnp 2>/dev/null | grep sshd | grep -oE ':[0-9]+' \
            | head -n 1 | tr -d ':' || true)"
    fi
    if [ -z "$port" ] && [ -r /etc/ssh/sshd_config ]; then
        port="$(grep -E '^Port ' /etc/ssh/sshd_config | awk '{print $2}' | head -n 1)"
    fi
    echo "${port:-22}"
}

setup_ufw() {
    command_exists ufw || { warn "未装 ufw，跳过防火墙设置。"; return 0; }
    local ssh_port
    ssh_port="$(detect_ssh_port)"
    info "放行 SSH 端口 $ssh_port 与面板端口…"
    ufw allow "$ssh_port/tcp" >/dev/null 2>&1 || true
    # 9527 始终放行：域名证书申请失败回落自签时，面板走的就是这个端口。
    # 只在有域名时才放行 80/443 的话，回落路径会被 UFW 挡住
    # （2026-11-24 首尔测试机实测：回落时 UFW 里只有 80/443）。
    ufw allow "${PANEL_PORT}/tcp" >/dev/null 2>&1 || true
    if [ -n "${DOMAIN:-}" ]; then
        ufw allow 80/tcp >/dev/null 2>&1 || true
        ufw allow 443/tcp >/dev/null 2>&1 || true
    fi
    if ufw status 2>/dev/null | grep -q "Status: inactive"; then
        ufw --force enable >/dev/null 2>&1 || warn "ufw 开启失败，请手动检查。"
        ok "UFW 已开启（已放行 SSH $ssh_port + 面板端口）。"
    else
        ok "UFW 规则已添加（防火墙本来就是开着的）。"
    fi
    # Oracle 云镜像（Ubuntu）自带 /etc/iptables/rules.v4，INPUT 链里有
    # 一条 -j REJECT（只放行 22），位置在 UFW 的链前面，会把 UFW 的
    # 放行规则全部挡住——面板端口从外网看就是 No route to host，
    # 本机 curl 却正常（2026-11-24 首尔测试机实测）。UFW 开启后这条
    # REJECT 是多余的（UFW 默认入站就是拒绝），删掉它。
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        remove_shadow_reject 4
        remove_shadow_reject 6
    fi
}

# 删掉镜像自带的、挡在 UFW 链前面的 INPUT REJECT 规则。
# 只删 INPUT 那条；FORWARD 与 InstanceServices（OCI 元数据服务）不动。
# $1: 4 或 6。
remove_shadow_reject() {
    local ver="$1" ipt rules
    if [ "$ver" = "6" ]; then
        ipt="ip6tables"; rules="/etc/iptables/rules.v6"
    else
        ipt="iptables"; rules="/etc/iptables/rules.v4"
    fi
    command_exists "$ipt" || return 0
    # 找第一条 REJECT 的行号；先遇到 ufw-* 跳转说明 REJECT 在 UFW 后面，
    # 挡不住 UFW 的规则，不用管。
    local n
    n="$("$ipt" -L INPUT --line-numbers -n 2>/dev/null \
        | awk '$1 ~ /^[0-9]+$/ { if ($2 == "REJECT") { print $1; exit } if ($2 ~ /^ufw-/) exit }')"
    [ -n "$n" ] || return 0
    info "发现镜像自带的 iptables REJECT 规则（第 $n 行）挡住了 UFW，正在清理…"
    "$ipt" -D INPUT "$n" 2>/dev/null || warn "删除实时 REJECT 规则失败，请手动检查 iptables。"
    if [ -f "$rules" ] && grep -q "^-A INPUT -j REJECT" "$rules"; then
        cp -a "$rules" "$rules.bak.mbot" 2>/dev/null || true
        sed -i '/^-A INPUT -j REJECT/d' "$rules"
        ok "已清理 $rules 里的 INPUT REJECT（原文件备为 $rules.bak.mbot）。"
    fi
}

# ---------- 健康检查 ----------
health_check() {
    local fail=0
    sleep 3
    if systemctl is-active --quiet "$WEB_SERVICE"; then
        ok "面板服务运行中。"
    else
        warn "面板服务未运行！看日志：journalctl -u $WEB_SERVICE -n 50"
        fail=1
    fi
    if systemctl is-active --quiet "$BOT_SERVICE"; then
        ok "机器人服务运行中。"
    else
        warn "机器人服务未运行（常见原因是 Token 不对）！看日志：journalctl -u $BOT_SERVICE -n 50"
        fail=1
    fi
    # 内部端口是 uvicorn（面板本身），外部端口是 nginx（反代）。
    # 两个都要在：只查外部端口会在 uvicorn 挂了时误报正常
    # （nginx 自己还在监听；2026-11-24 首尔测试机实测）。
    if command_exists ss && ss -ltn 2>/dev/null | grep -q ":${INTERNAL_PORT} "; then
        ok "面板内部端口 ${INTERNAL_PORT} 监听中。"
    else
        warn "面板内部端口 ${INTERNAL_PORT} 未监听，uvicorn 可能未起来。"
        fail=1
    fi
    if command_exists ss && ss -ltn 2>/dev/null | grep -q ":${PANEL_PORT} "; then
        ok "面板外部端口 ${PANEL_PORT} 监听中。"
    else
        warn "面板外部端口 ${PANEL_PORT} 未监听，nginx 反代可能未起来。"
        fail=1
    fi
    return "$fail"
}

print_access_info() {
    local ip
    ip="$(curl -s -m 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
    echo
    echo "==================== 安装完成 ===================="
    # 域名地址只在证书真的申请成功时才给：certbot 失败回落自签后，
    # 面板走的是 IP:9527，给域名地址会误导用户（域名根本不解析；
    # 2026-11-24 首尔测试机实测）。
    if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
        echo "面板地址：https://${DOMAIN}/login"
    else
        if [ -n "${DOMAIN:-}" ]; then
            echo "域名证书申请失败，已回落自签证书（域名解析到本机后可重跑安装）。"
        fi
        echo "面板地址：https://${ip}:${PANEL_PORT}/login"
        echo "（自签证书，浏览器提示不受信任时点“继续访问”即可）"
    fi
    echo "用户名：admin"
    if [ "${GENERATED_PASSWORD:-0}" = "1" ]; then
        echo "密码：${ADMIN_PASSWORD}（只显示这一次，也存在 $ADMIN_PASSWORD_FILE）"
    else
        echo "密码：你刚才输入的密码"
    fi
    echo
    echo "下一步："
    echo "  1. 在 Telegram 里找你的机器人，发 /uploadapi 上传 OCI 配置"
    echo "     （~/.oci/config + API 私钥），或手动放到服务器的 ~/.oci/。"
    echo "  2. 发 /oracle 打开菜单，点“切换账号/区域”确认账号已接通。"
    echo "如果浏览器打不开面板：检查云厂商的安全组/安全列表是否放行了"
    if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
        echo "  443 端口（本机 UFW 已放行，云上那层也要放行）。"
    else
        echo "  ${PANEL_PORT} 端口（本机 UFW 已放行，云上那层也要放行）。"
    fi
    echo "管理命令：bash install.sh start|stop|restart|status|log|upgrade|uninstall"
    echo "=================================================="
}

# ---------- 主流程 ----------
cmd_install() {
    require_root
    detect_os
    # 先装依赖再提问：提问里的随机密码生成要用到 openssl，
    # 精简镜像上 openssl 未必预装。
    install_deps
    prompt_config
    install_code
    setup_venv
    write_env_file
    write_systemd_units
    if [ "$FRESH_INSTALL" = "1" ]; then
        setup_nginx
        setup_ufw
    elif [ ! -f "$NGINX_CONF" ]; then
        # 更新模式但 nginx 配置丢了：按环境变量的域名重建（没给就自签）。
        DOMAIN="${MBOT_INSTALL_DOMAIN:-}"
        setup_nginx
    else
        info "nginx 配置已存在，跳过（想换域名请删 $NGINX_CONF 后重跑）。"
    fi
    health_check || warn "健康检查有告警，见上面日志。"
    if [ "$FRESH_INSTALL" = "1" ]; then
        print_access_info
    else
        echo
        echo "更新完成：代码已更新，两个服务已重启。"
        echo "查看状态：bash install.sh status"
    fi
}

cmd_upgrade() {
    require_root
    [ -d "$INSTALL_DIR/.git" ] || die "未安装（$INSTALL_DIR 不存在），先运行不带参数的安装。"
    info "升级：拉最新代码 → 装依赖 → 重启服务…"
    (cd "$INSTALL_DIR" && git pull --ff-only) \
        || die "git pull 失败（可能有本地改动），请手动处理。"
    "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    "$VENV_DIR/bin/pip" install --quiet oci-cli
    systemctl restart "$WEB_SERVICE" "$BOT_SERVICE"
    health_check || warn "健康检查有告警，见上面日志。"
    ok "升级完成。"
}

cmd_uninstall() {
    require_root
    echo "即将卸载 M_Bot：停服务、删 systemd 单元、删 nginx 反代、删 $INSTALL_DIR。"
    echo "环境文件 $ENV_FILE 与密码文件 $ADMIN_PASSWORD_FILE 会保留（凭据不删）。"
    printf "确认卸载？输入 yes 继续："
    read -r ans || true
    [ "$ans" = "yes" ] || { echo "已取消。"; exit 0; }
    systemctl stop "$WEB_SERVICE" "$BOT_SERVICE" 2>/dev/null || true
    systemctl disable "$WEB_SERVICE" "$BOT_SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${WEB_SERVICE}.service" \
        "/etc/systemd/system/${BOT_SERVICE}.service"
    systemctl daemon-reload
    rm -f "$NGINX_CONF"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
    rm -rf "$INSTALL_DIR"
    ok "已卸载。凭据仍在 $ENV_FILE（想彻底删掉请手动 rm 它）。"
}

# ---------- 入口 ----------
# 不带参数：首次=安装，再次=更新并重启（R-Bot 模板同款用法）。
case "${1:-}" in
    "")         cmd_install ;;
    start)      systemctl start "$WEB_SERVICE" "$BOT_SERVICE" && echo "已启动。" ;;
    stop)       systemctl stop "$WEB_SERVICE" "$BOT_SERVICE" && echo "已停止。" ;;
    restart)    systemctl restart "$WEB_SERVICE" "$BOT_SERVICE" && echo "已重启。" ;;
    status)     systemctl status "$WEB_SERVICE" "$BOT_SERVICE" --no-pager ;;
    log)        journalctl -u "$WEB_SERVICE" -u "$BOT_SERVICE" -n 100 --no-pager ;;
    upgrade)    cmd_upgrade ;;
    uninstall)  cmd_uninstall ;;
    *)
        echo "用法：$0 [start|stop|restart|status|log|upgrade|uninstall]"
        echo "不带参数：首次运行安装，再次运行更新并重启。"
        exit 1
        ;;
esac
