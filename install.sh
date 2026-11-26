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
#   bash install.sh start|stop|restart|status|log|upgrade|auto-upgrade|setup-auto-upgrade|uninstall
#
# 安装前准备（缺一不可，装完机器人才能工作）：
#   1. Telegram 机器人 Token：@BotFather 处获得——新建机器人发 /newbot，
#      已有机器人发 /token 找回（格式如 123456789:AAE...，装到一半会问你）。
#   2. 你的 Telegram 用户 ID：找 @userinfobot 发 /start 拿到
#      （纯数字，装到一半会问你；白名单只放这个 ID）。
#   3. OCI API 密钥：config 文本发给自己的机器人（/uploadapi），
#      私钥 .pem 用 scp 传到服务器 /root/ 下（不走 Telegram）。
#      详细步骤见 README「拿到 OCI API 密钥（详细步骤）」。
#
# 非交互安装（跳过提问，变量缺了就用默认值/自动生成）：
#   MBOT_INSTALL_TOKEN=xxx MBOT_INSTALL_USER_ID=123 \
#     MBOT_INSTALL_PASSWORD=xxx MBOT_INSTALL_DOMAIN=panel.example.com \
#     MBOT_INSTALL_CERT_METHOD=cfdns MBOT_INSTALL_CF_API_TOKEN=xxx \
#     bash install.sh
#   cfdns 的凭据也可以不传：安装时会先看 OCI 配置文件
#   $INSTALL_DIR/oci_config（安装目录下）里有没有 [cloudflare] 段，
#   有就直接用（cf_token，或 cf_key + cf_email），不用再问。
#
# 注意：
#   - 面板（uvicorn）只绑 127.0.0.1，不直接对外；外网访问走 nginx 反代 + HTTPS，优先 443 端口，被占时回落 9527。
#   - nginx 从 nginx.org 官方仓库装最新版（mainline 分支），不是发行版
#     自带的旧版；仓库加不上时回落发行版版本，安装不中断。
#   - 防火墙自动识别：ufw / firewalld / nftables / iptables，哪个在跑
#     就用哪个放行 SSH + 80 + 443 + 9527；都没在跑时优先启用 ufw。
#   - OCI 实例还会经 OCI API 自动放行云端安全列表（80/443/9527）；
#     没配 API 认证时只警告，安装不中断。
#   - Telegram Token 可留空：留空则只装面板，不装机器人。
#   - 域名自动绑定：有域名时发证前先校验解析，没解析到本机且有
#     Cloudflare 凭据（环境变量或 $INSTALL_DIR/oci_config 的 [cloudflare] 段）
#     就自动建/改 A 记录指到本机，等解析生效再发证。
#   - 装完自动自检：证书（颁发者/有效期/域名匹配）、登录页 200、
#     真实登录（admin 密码 + 会话 cookie）、http→https 跳转。
#   - 有域名：自动申请 Let's Encrypt 证书，访问 https://你的域名。
#     验证方式自动检测，不用选：已设置 Cloudflare 凭据
#     （CF_API_TOKEN，或 CF_API_KEY + CF_EMAIL，或 CF_Key + CF_Email）
#     就走 cfdns（Cloudflare DNS 验证，lego；验证不占端口），没凭据就走 http
#     （80 端口验证，certbot）。MBOT_INSTALL_CERT_METHOD 仍可
#     显式指定 http/cfdns。
#   - 没域名：自动用服务器公网 IP 申请 Let's Encrypt IP 证书
#     （lego，shortlived，有效期约 6 天，cron 每 2 天自动续期），
#     访问 https://服务器IP（443 空闲时）或 https://服务器IP:9527（443 被占时）；
#     申请失败时回落自签证书
#     （浏览器会提示证书不受信任，点“继续访问”即可）。
#   - The admin password is auto-generated when not given, shown only
#     once, and stored in $INSTALL_DIR/.mbot-admin-password (mode 600).
#   - UFW opens the current SSH port first, so it never locks you out.
#
# English summary: one-click installer for M_Bot (Debian / Ubuntu, run as
# root). First run installs, later runs update and restart (config kept).
# Commands after install: bash install.sh
# start|stop|restart|status|log|upgrade|auto-upgrade|setup-auto-upgrade|uninstall.
# Before installing you need: 1. a Telegram bot token from @BotFather
# (/newbot for a new bot, /token to recover one); 2. your Telegram user ID
# from @userinfobot (digits only; the allowlist holds just this ID);
# 3. OCI API keys: send the config text to your own bot (/uploadapi) and
# scp the .pem private key to /root/ (never via Telegram). Details in the
# README section on getting OCI API keys. Non-interactive install: pass
# MBOT_INSTALL_TOKEN / MBOT_INSTALL_USER_ID / MBOT_INSTALL_PASSWORD /
# MBOT_INSTALL_DOMAIN / MBOT_INSTALL_CERT_METHOD / MBOT_INSTALL_CF_API_TOKEN
# as env vars. Notes: the panel binds 127.0.0.1 only and is exposed via
# nginx + HTTPS; nginx comes from the official nginx.org repo (mainline),
# falling back to the distro build; the firewall (ufw / firewalld / nftables
# / iptables) opens SSH + 80 + 443 + 9527, and OCI security lists are opened
# via the OCI API too; the Telegram token may be left empty (panel only);
# with a domain, the A record is auto-created via Cloudflare when credentials
# are available; install ends with a self-check (cert, login page, real
# login, http-to-https redirect); a domain gets a Let's Encrypt cert
# (validation auto-detected: Cloudflare credentials present means
# Cloudflare DNS-01 via lego, otherwise http), no domain gets a Let's
# Encrypt IP cert (shortlived, ~6 days, auto-renewed every 2 days) or
# falls back to a self-signed cert.
# ============================================================================

set -euo pipefail

# ================= 配置区（改这里） =================
# Repo URL (used by git clone). 代码仓库（git clone 用）。
REPO_URL="https://github.com/jimu2000/M_Bot.git"
# Install directory (code lives here; not /tmp, it is wiped on reboot). 安装目录（代码放这里；不要用 /tmp，重启会丢）。
INSTALL_DIR="/opt/mbot"
# Env file (tokens, password hash, session key; mode 600, read by systemd). 环境文件（Token、密码哈希、会话密钥等，600 权限，systemd 读入）。
ENV_FILE="$INSTALL_DIR/.mbot.env"
# Unified txt config (OCI profiles, Cloudflare, Bark; mode 600, loaded by the code at startup). 统一配置文件（纯文本，OCI 账号、Cloudflare、Bark 三节，600 权限，启动时由代码读入）。
CONFIG_FILE="$INSTALL_DIR/M_bot_config.conf"
# Admin password in plain text (only written when auto-generated; mode 600, shown once, keep it safe). 管理员密码明文（自动生成时才写，600 权限，只显示一次后请妥善保存）。
ADMIN_PASSWORD_FILE="$INSTALL_DIR/.mbot-admin-password"
# Python virtualenv directory (created during install). Python 虚拟环境目录（install 时创建）。
VENV_DIR="$INSTALL_DIR/venv"
# systemd service names. systemd 服务名。
WEB_SERVICE="mbot-web"
BOT_SERVICE="mbot-bot"
# Panel fallback port (nginx prefers 443 when free, and listens on this port when 443 is occupied). 面板备用端口（443 空闲时 nginx 走 443，443 被占时走这个端口）。
PANEL_PORT=9527
# Internal port (uvicorn binds 127.0.0.1:INTERNAL_PORT only; nginx proxies to it). 面板内部端口（uvicorn 只绑 127.0.0.1:INTERNAL_PORT，nginx 反代到它）。
# Must differ from PANEL_PORT: after nginx binds 0.0.0.0:9527, uvicorn binding 127.0.0.1:9527 fails with address already in use (a wildcard bind also occupies loopback). 必须和 PANEL_PORT 不同：nginx 绑 0.0.0.0:9527 后，uvicorn 再绑 127.0.0.1:9527 会报 address already in use（Linux 上通配绑定会占住该端口的所有地址，包括回环）。
INTERNAL_PORT=18001
# nginx reverse-proxy config path (a standalone file under conf.d; user configs untouched). nginx 反代配置路径（conf.d 里的独立文件，不动用户已有配置）。
NGINX_CONF="/etc/nginx/conf.d/mbot-panel.conf"
# Self-signed cert directory (fallback when no domain and the IP cert fails). 自签证书目录（没域名、IP 证书又申请失败时回落用）。
SELFSIGN_DIR="/etc/nginx/ssl/mbot-selfsigned"
# lego (ACME client for IP certs and Cloudflare DNS-01; single-file Go binary from GitHub releases, supports --profile shortlived and IP certs). lego（ACME 客户端，IP 证书与 Cloudflare DNS-01 用；单文件 Go 二进制，从 GitHub releases 下，支持 --profile shortlived 与 IP 证书）。
# Version pinned: verify a new version's CLI has not changed on a test machine before upgrading. 版本钉死：升级前先在测试机上验过新版的命令行没变。
LEGO_VERSION="v5.7.0"
LEGO_BIN="/usr/local/bin/lego"
LEGO_DIR="/etc/lego"
# lego cert storage (lego default layout: certificates/<name>.crt/.key). lego 证书存放目录（lego 默认结构：certificates/<名字>.crt/.key）。
LEGO_CERT_DIR="$LEGO_DIR/certificates"
# ACME webroot (served by nginx on port 80 for IP-cert http-01). ACME webroot（IP 证书 http-01 验证用，nginx 80 端口 serve 它）。
ACME_WEBROOT="/var/www/mbot-acme"
# Renewal script + cron (IP certs last only 160 hours, run every 2 days; lego run also renews and skips when outside the renewal window). 证书续期脚本与 cron（IP 证书只有 160 小时有效期，每 2 天跑一次；lego run 兼续期：证书没到续期窗口时直接跳过）。
RENEW_SCRIPT="/usr/local/bin/mbot-renew-cert.sh"
RENEW_CRON="/etc/cron.d/mbot-cert-renew"
# Scheduled auto-upgrade (runs install.sh auto-upgrade daily at 04:00; checks mbot and nginx for new versions, skips when both are already latest; only logs when already latest, upgrades only on a new version; Bark notification on failure). 定时自动升级（每天凌晨 4 点跑 install.sh auto-upgrade；同时检查 mbot 与 nginx 有无新版，两者都已是最新版时跳过；已是最新版时只记日志不重启服务，有新版时才升级；失败时 Bark 通知）。
UPGRADE_CRON="/etc/cron.d/mbot-auto-upgrade"
UPGRADE_LOG="/var/log/mbot-upgrade.log"
AUTO_UPGRADE_SCHEDULE="0 4 * * *"
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
# A fresh system may still hold the apt lock (unattended-upgrades / apt-daily on first boot), so a direct apt-get fails with Could not get lock and set -e kills the script. 新装系统首次启动时 unattended-upgrades / apt-daily 会占着 apt 锁，直接 apt-get 会报 Could not get lock，set -e 下脚本当场退出。
apt_locked() {
    # Do not match the unattended-upgrade process itself: unattended-upgrade-shutdown --wait-for-signal is a resident daemon that holds no apt lock; waiting on it would hang forever. 注意：不要匹配 unattended-upgrade 进程本身——unattended-upgrade-shutdown --wait-for-signal 是常驻守护，不占 apt 锁，匹配它会永远等下去。
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

# nginx.org official repo (mainline): the distro nginx is too old, so add the official repo for the latest version. Failure returns 1 and the caller falls back to the distro version; install continues. The repo URL swaps the distro name automatically (debian/ubuntu, never hardcode debian or Ubuntu gets 404). nginx.org 官方仓库（mainline 分支）：发行版自带的 nginx 太旧，这里加官方仓库装最新版。失败返回 1，调用方回落发行版版本，安装不中断。仓库 URL 里发行版名按系统自动换（debian/ubuntu，不能写死 debian，否则 Ubuntu 上 404）。
# Key goes to a dedicated keyring with signed-by (only nginx.org packages trusted), not the global trusted.gpg.d. 密钥放专用 keyring + signed-by（只信任 nginx.org 的包），不放全局 trusted.gpg.d。
setup_nginx_repo() {
    local id codename
    id="$(. /etc/os-release; echo "${ID:-}")"
    case "$id" in
        debian|ubuntu) : ;;
        *) warn "未知发行版 $id，nginx 用发行版自带版本。"; return 1 ;;
    esac
    # Fetching the repo key needs curl + gpg + lsb-release, which minimal images may not preinstall. 取仓库密钥要 curl + gpg + lsb-release，精简镜像上未必预装。
    if ! command_exists curl || ! command_exists gpg \
            || ! command_exists lsb_release; then
        wait_for_apt
        apt-get install -y -qq apt-transport-https ca-certificates curl \
            gnupg lsb-release >/dev/null 2>&1 \
            || { warn "仓库工具安装失败，nginx 用发行版自带版本。"; return 1; }
    fi
    codename="$(lsb_release -cs 2>/dev/null || true)"
    [ -n "$codename" ] \
        || codename="$(. /etc/os-release; echo "${VERSION_CODENAME:-}")"
    [ -n "$codename" ] \
        || { warn "读不到系统代号，nginx 用发行版自带版本。"; return 1; }
    if ! curl -fsSL https://nginx.org/keys/nginx_signing.key \
            | gpg --batch --yes --dearmor -o /usr/share/keyrings/nginx-archive-keyring.gpg \
            2>/dev/null; then
        warn "nginx 签名密钥下载失败，nginx 用发行版自带版本。"
        return 1
    fi
    printf 'deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] https://nginx.org/packages/mainline/%s %s nginx\n' \
        "$id" "$codename" > /etc/apt/sources.list.d/nginx.list
    printf 'Package: *\nPin: origin nginx.org\nPin-Priority: 900\n' \
        > /etc/apt/preferences.d/99nginx
    wait_for_apt
    apt-get update -qq >/dev/null 2>&1 \
        || { warn "nginx 仓库更新失败，nginx 用发行版自带版本。"; return 1; }
    ok "nginx.org 官方仓库已添加（${id} ${codename}，mainline 分支）。"
    return 0
}

install_deps() {
    info "安装系统依赖（python3 / git / nginx / ufw / certbot）…"
    export DEBIAN_FRONTEND=noninteractive
    wait_for_apt
    # A race can still occur between the lock check and apt-get (another process may grab the lock), so update / install each retry 3 times before giving up. 等锁与 apt-get 之间仍可能有竞态（锁刚被别的进程抢走），update / install 各重试 3 次，失败才退出。
    local attempt=0
    until apt-get update -qq; do
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && die "apt-get update 多次失败，请检查网络与 apt 源。"
        warn "apt-get update 失败（可能锁被占），15 秒后重试…"
        sleep 15
        wait_for_apt
    done
    setup_nginx_repo || true
    attempt=0
    # --force-confold: keep an existing nginx config on the machine (do not overwrite user edits; no effect on a fresh install). --force-confold：机器上已有 nginx 配置时保留它（不覆盖用户改过的配置；新装时没影响）。
    until apt-get install -y -qq -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        python3 python3-venv python3-pip git nginx \
        ufw curl wget openssl iproute2 procps psmisc >/dev/null; do
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && die "apt 安装依赖失败，请检查网络与 apt 源。"
        warn "apt 安装失败（可能锁被占），15 秒后重试…"
        sleep 15
        wait_for_apt
    done
    ok "系统依赖已就绪（nginx $(nginx -v 2>&1 | head -n 1)）。"
}

# ---------- 交互提问 ----------
prompt_config() {
    if [ -f "$ENV_FILE" ]; then
        info "检测到已有配置 $ENV_FILE，跳过提问（更新模式）。"
        FRESH_INSTALL=0
        # Update mode also honors MBOT_INSTALL_CERT_METHOD (otherwise it defaults to http and an env-passed cfdns is ignored). Invalid values fall back to http without interrupting the update. 更新模式也认 MBOT_INSTALL_CERT_METHOD（否则默认走 http，环境变量传了 cfdns 也不生效）。非法值回落 http，不中断更新。
        CERT_METHOD="${MBOT_INSTALL_CERT_METHOD:-http}"
        case "$CERT_METHOD" in
            http|cfdns) : ;;
            *) warn "MBOT_INSTALL_CERT_METHOD='$CERT_METHOD' 无效（只能是 http/cfdns），用 http。"; CERT_METHOD="http" ;;
        esac
        return 0
    fi
    FRESH_INSTALL=1
    echo
    echo "=== M_Bot 安装配置（Token 留空则只装面板；密码/域名可留空） ==="
    # Token: env var > interactive input > empty (panel only, no bot). Token：环境变量 > 交互输入 > 留空（只装面板，不装机器人）。
    # Every read needs || true: under nohup / non-interactive runs stdin may be EOF, read returns non-zero and set -e kills the script. On EOF the variable stays empty and is handled as not provided below. 注意：read 后面都要加 || true——nohup / 非交互运行时 stdin 可能是 EOF，read 返回非零，set -e 会直接杀掉脚本。EOF 时变量保持为空，后面按“未提供”处理（Token 缺了只装面板，密码/域名缺了走默认值）。
    TOKEN="${MBOT_INSTALL_TOKEN:-}"
    if [ -z "$TOKEN" ]; then
        printf "Telegram 机器人 Token（@BotFather 处获得，留空则只装面板）："
        read -r TOKEN || true
    fi
    if [ -n "$TOKEN" ]; then
        case "$TOKEN" in
            *:*[A-Za-z0-9_-]*) : ;;
            *) die "Token 格式不对，应为 数字:字母数字 的形式。" ;;
        esac
        BOT_ENABLED=1
    else
        BOT_ENABLED=0
        info "未提供 Token：只装面板，不装机器人。"
        info "以后想加机器人：在 $ENV_FILE 里填 MBOT_TELEGRAM_TOKEN 与 MBOT_ALLOWED_USERS 后重跑安装即可。"
    fi
    USER_ID="${MBOT_INSTALL_USER_ID:-}"
    if [ "$BOT_ENABLED" = "1" ]; then
        if [ -z "$USER_ID" ]; then
            printf "你的 Telegram 用户 ID（纯数字，@userinfobot 发 /start 查看）："
            read -r USER_ID || true
        fi
        case "$USER_ID" in
            *[!0-9]*|"") die "用户 ID 必须是纯数字。" ;;
        esac
    fi
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
    CERT_METHOD="${MBOT_INSTALL_CERT_METHOD:-http}"
    # Cloudflare credentials: read up front when a domain is set (needed for DNS auto-binding, and mandatory for cfdns). Priority: env vars (incl. CF_Key/CF_Email) > [cloudflare] section of $INSTALL_DIR/oci_config > interactive input (only asked for cfdns). Cloudflare 凭据：有域名时都先读一份（DNS 自动绑定要用；cfdns 验证更是必须）。优先级：环境变量（含 CF_Key/CF_Email）> OCI 配置文件 $INSTALL_DIR/oci_config 的 [cloudflare] 段 > 交互输入（只在 cfdns 时问）。
    if [ -n "$DOMAIN" ]; then
        load_cf_credentials || true
    fi
    # Certificate method: auto-detect, no prompt.
    # - CF credentials set (CF_API_TOKEN, or CF_API_KEY + CF_EMAIL, or CF_Key + CF_Email) and a domain is given -> cfdns (Cloudflare DNS-01), the domain cert is issued automatically.
    # - No CF credentials but a domain is given -> http (port-80 validation), domain cert.
    # - No domain given (empty input = skip) -> IP cert (setup_nginx handles it; CERT_METHOD is unused).
    # MBOT_INSTALL_CERT_METHOD still overrides explicitly (http/cfdns).
    # 证书验证方式：自动检测，不再提问。
    # - 已设置 CF 凭据（CF_API_TOKEN，或 CF_API_KEY + CF_EMAIL，或 CF_Key + CF_Email）且填了域名 -> cfdns（Cloudflare DNS-01），自动申请域名证书。
    # - 没 CF 凭据但填了域名 -> http（80 端口验证），申请域名证书。
    # - 没填域名（留空即跳过）-> IP 证书（setup_nginx 里处理，CERT_METHOD 不用）。
    # MBOT_INSTALL_CERT_METHOD 仍可显式指定（http/cfdns）。
    if [ -z "${MBOT_INSTALL_CERT_METHOD+x}" ]; then
        if [ -n "$DOMAIN" ] && { [ -n "$CF_API_TOKEN" ] || { [ -n "$CF_API_KEY" ] && [ -n "$CF_EMAIL" ]; }; }; then
            CERT_METHOD="cfdns"
            info "检测到 Cloudflare 凭据，域名证书走 Cloudflare DNS 验证（cfdns），自动申请。"
        else
            CERT_METHOD="http"
        fi
    fi
    case "$CERT_METHOD" in
        http|cfdns) : ;;
        *) die "验证方式只能是 http 或 cfdns。" ;;
    esac
    if [ "$CERT_METHOD" = "cfdns" ]; then
        if [ -z "$CF_API_TOKEN" ] && [ -z "$CF_API_KEY" ]; then
            printf "Cloudflare API Token（Zone:DNS:Edit + Zone:Zone:Read 权限）："
            read -r -s CF_API_TOKEN || true
            echo
        fi
        if [ -z "$CF_API_TOKEN" ] \
                && { [ -z "$CF_API_KEY" ] || [ -z "$CF_EMAIL" ]; }; then
            die "cfdns 需要 Cloudflare 凭据（Token，或 Key + 邮箱），都没有就选 http。"
        fi
    fi
}

# ---------- 代码 ----------
# Update installed code to the latest: git pull first; on fast-forward failure use fetch + reset to force-sync (only git-tracked code files; the env file at $INSTALL_DIR/.mbot.env, runtime data and __pycache__ are not in git and are untouched). If both fail, exit instead of silently installing with stale code. 把已安装的代码更新到最新版：先 git pull，快进失败时改用 fetch + reset 强制同步（只动 git 跟踪的代码文件；环境文件在 $INSTALL_DIR/.mbot.env、运行时数据与 __pycache__ 都不在 git 里，不受影响）；两条路都失败时直接退出，绝不静默用旧代码继续装。
update_code() {
    info "更新代码到最新版…"
    if (cd "$INSTALL_DIR" && git pull --ff-only); then
        ok "代码已更新到最新版。"
        return 0
    fi
    warn "git pull 失败，改用 fetch + reset 强制同步到最新版…"
    (cd "$INSTALL_DIR" && git fetch origin main \
        && git reset --hard origin/main) \
        || die "代码更新失败：拿不到最新版，停止安装（请检查网络，或手动处理 $INSTALL_DIR 的本地改动）。"
    ok "代码已强制同步到最新版。"
}

installed_version() {
    if [ -f "$INSTALL_DIR/VERSION" ]; then
        cat "$INSTALL_DIR/VERSION"
    else
        echo "unknown"
    fi
}

install_code() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        update_code
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
    ok "代码已就绪（版本 v$(installed_version)）。"
}

setup_venv() {
    info "创建虚拟环境并安装 Python 依赖（含 oci-cli）…"
    [ -d "$VENV_DIR" ] || python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" \
        || die "pip 安装依赖失败，请检查网络。"
    # OCI calls use the official OCI CLI (subprocess); it is not in requirements.txt, so install it separately into the venv and point MBOT_OCI_CLI at it. OCI 调用走官方 OCI CLI（subprocess），requirements.txt 里没有，这里单独装进 venv，MBOT_OCI_CLI 指到它。
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
    # Pass the password to Python via an env var to avoid shell quoting/escaping issues. 密码经环境变量传给 Python，避免 shell 引用转义问题。
    # PYTHONPATH must point at the install dir: the script's cwd is not necessarily /opt/mbot, and without it python -c cannot import web.auth. PYTHONPATH 必须指到安装目录：安装脚本的当前目录不一定是 /opt/mbot，python -c 不加 PYTHONPATH 就 import 不到 web.auth。
    hash="$(MBOT_PW="$ADMIN_PASSWORD" PYTHONPATH="$INSTALL_DIR" \
        "$VENV_DIR/bin/python" -c \
        "import os; from web.auth import hash_password; print(hash_password(os.environ['MBOT_PW']))" \
        2>/tmp/mbot_hash_err)" \
        || { cat /tmp/mbot_hash_err >&2; die "生成密码哈希失败。"; }
    [ -n "$hash" ] || die "生成密码哈希失败（空哈希）。"
    # Compartment is derived from the OCI config's tenancy (the root compartment is the tenancy); when unreadable, omit the line (never write a placeholder again) and let the panel derive it from the tenancy at startup. compartment：从 OCI 配置的 tenancy 推导（根 compartment 即 tenancy）；读不到时不写这行（绝不再写占位符），面板启动时再从 tenancy 推导。
    local compartment_line=""
    local tenancy
    tenancy="$(read_tenancy_from_oci_config 2>/dev/null || true)"
    if [ -n "$tenancy" ]; then
        compartment_line="MBOT_COMPARTMENT_DEFAULT=$tenancy"
        info "从 OCI 配置读到 tenancy，已写入 MBOT_COMPARTMENT_DEFAULT。"
    else
        warn "未在 OCI 配置里找到 tenancy，未写 MBOT_COMPARTMENT_DEFAULT（面板会在你放好 OCI 配置后自动推导）。"
    fi
    cat > "$ENV_FILE" <<EOF
# M_Bot runtime config (generated by install.sh, mode 600, do not share this file). M_Bot 运行配置（install.sh 生成，600 权限，不要分享此文件）。
MBOT_TELEGRAM_TOKEN=$TOKEN
MBOT_ALLOWED_USERS=$USER_ID
MBOT_WEB_PASSWORD_HASH=$hash
MBOT_WEB_SECRET=$secret
$compartment_line
MBOT_OCI_CLI=$VENV_DIR/bin/oci
MBOT_TGBOT_DATA_DIR=$INSTALL_DIR/tgbot/data
MBOT_WEB_DATA_DIR=$INSTALL_DIR/web/data
EOF
    chmod 600 "$ENV_FILE"
    if [ "${GENERATED_PASSWORD:-0}" = "1" ]; then
        printf '%s\n' "$ADMIN_PASSWORD" > "$ADMIN_PASSWORD_FILE"
        chmod 600 "$ADMIN_PASSWORD_FILE"
    fi
    mkdir -p "$INSTALL_DIR/tgbot/data" "$INSTALL_DIR/web/data"
    ok "环境文件已写入。"
}

# ---------- 统一配置文件 ----------
# Write the unified txt config (M_bot_config.conf: oci / cloudflare /
# bark, plus the reserved sections) when it does not exist yet. OCI
# profiles come from the OCI config file ($INSTALL_DIR/oci_config, if present;
# old ~/oci_config and ~/.oci/config are still read as fallbacks);
# Cloudflare credentials come from the CF_* shell vars gathered by
# prompt_config (env vars take priority over the [cloudflare] section
# of the OCI config); bark is left empty for the user to fill later.
# The file layout (with bilingual comments) comes from
# mbot_config.save_config, so the format lives in one place. Values
# pass to Python via env vars to avoid shell quoting issues.
# 写入统一配置文件（M_bot_config.conf：oci / cloudflare / bark 三节，
# 另加预留节），不存在时才写。OCI profile 从 OCI 配置文件
# （$INSTALL_DIR/oci_config，有的话；旧的 ~/oci_config 与 ~/.oci/config
# 仍作回退）读；Cloudflare 凭据从 prompt_config 收集
# 的 CF_* 变量来（环境变量优先于 OCI 配置里的 [cloudflare] 段）；
# bark 留空、用户以后填。文件排版（双语注释）由 mbot_config.save_config
# 生成，格式只在一处维护。值经环境变量传给 Python，避免 shell 引用
# 转义问题。注意：CF_API_TOKEN 在 txt 里没有位置，不写进去（它继续
# 留在 env 文件里经 systemd 生效）。
write_config_txt() {
    [ -f "$CONFIG_FILE" ] && { info "统一配置文件已存在，不覆盖。"; return 0; }
    info "写入统一配置文件 $CONFIG_FILE（600 权限）…"
    CF_JSON_KEY="${CF_API_KEY:-}" \
    CF_JSON_EMAIL="${CF_EMAIL:-}" MBOT_CONFIG_PATH="$CONFIG_FILE" \
    MBOT_INSTALL_DIR="$INSTALL_DIR" \
    "$VENV_DIR/bin/python" - <<'PYEOF_INNER' || die "写入统一配置文件失败。"
import configparser
import os
import sys

sys.path.insert(0, os.environ["MBOT_INSTALL_DIR"])
import mbot_config

path = os.environ["MBOT_CONFIG_PATH"]
cfg = mbot_config.empty_config()
# OCI profiles from the OCI config file (first existing file wins).
oci_cfg = ""
for candidate in (os.path.join(os.environ["MBOT_INSTALL_DIR"],
                               "oci_config"),
                  os.path.expanduser("~/oci_config"),
                  os.path.expanduser("~/.oci/config")):
    if os.path.isfile(candidate):
        oci_cfg = candidate
        break
if oci_cfg:
    parser = configparser.ConfigParser(
        default_section="__oci_no_defaults__", interpolation=None)
    try:
        parser.read(oci_cfg, encoding="utf-8")
        for section in parser.sections():
            items = {k: v.strip() for k, v in parser.items(section)}
            if section.lower() == "cloudflare":
                if items.get("cf_key") and "key" not in cfg["cloudflare"]:
                    cfg["cloudflare"]["key"] = items["cf_key"]
                if items.get("cf_email") and "email" not in cfg["cloudflare"]:
                    cfg["cloudflare"]["email"] = items["cf_email"]
                continue
            cfg["oci"]["profiles"][section] = items
    except configparser.Error:
        pass
# Cloudflare credentials from the installer (env vars win).
if os.environ.get("CF_JSON_KEY"):
    cfg["cloudflare"]["key"] = os.environ["CF_JSON_KEY"]
if os.environ.get("CF_JSON_EMAIL"):
    cfg["cloudflare"]["email"] = os.environ["CF_JSON_EMAIL"]
mbot_config.save_config(cfg, path)
PYEOF_INNER
    ok "统一配置文件已写入 $CONFIG_FILE。"
}

# ---------- systemd 单元 ----------
# Services that actually exist on this machine (panel always, bot only when installed). start/stop/restart/status/log/upgrade use this list to avoid calling systemctl on a missing bot service (which would error out). 本机上实际存在的 M_Bot 服务（面板一定有，机器人只在装了时有）。start/stop/restart/status/log/upgrade 都走它，避免对不存在的机器人服务调 systemctl（会报错退出）。
mbot_services() {
    printf '%s' "$WEB_SERVICE"
    if [ -f "/etc/systemd/system/${BOT_SERVICE}.service" ]; then
        printf ' %s' "$BOT_SERVICE"
    fi
}

running_services() {
    # On upgrade only restart services that are running or enabled: a service deliberately stopped and disabled (e.g. bot kept but unused) must not be pulled up on its own. 升级时只重启“在跑或已设自启”的服务：被特意停掉并禁用的服务（如机器人留着不用）不要擅自拉起来。
    local svc
    for svc in $(mbot_services); do
        if systemctl is-active -q "$svc" 2>/dev/null \
                || systemctl is-enabled -q "$svc" 2>/dev/null; then
            printf '%s ' "$svc"
        fi
    done
}

write_systemd_units() {
    # In update mode prompt_config did not run, so BOT_ENABLED is unset: decide from whether the env file has a Token. 更新模式下 prompt_config 没跑，BOT_ENABLED 未设：从 env 文件看有没有 Token 决定要不要机器人服务。
    # Note: grep defaults to BRE, where + in .+ is a literal plus that never matches; test non-empty with . only (an old regex used to delete the bot by mistake). 注意：grep 默认是 BRE，.+ 里的 + 是字面加号、永远匹配不到，所以只用 . 测“值非空”（旧正则曾把机器人误删）。
    if [ -z "${BOT_ENABLED:-}" ]; then
        if grep -q "^MBOT_TELEGRAM_TOKEN=." "$ENV_FILE" 2>/dev/null; then
            BOT_ENABLED=1
        elif [ -f "/etc/systemd/system/${BOT_SERVICE}.service" ] \
                && ! [ -f "$ENV_FILE" ]; then
            # Env file lost but the bot unit still exists: keep the bot, do not tear it down on our own. env 文件丢了但机器人单元还在：保留机器人，不擅自拆掉。
            BOT_ENABLED=1
        else
            BOT_ENABLED=0
        fi
    fi
    info "写入 systemd 单元（$WEB_SERVICE$([ "$BOT_ENABLED" = "1" ] && echo " / $BOT_SERVICE")）…"
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
# The panel and bot both read/write the env file through web/config.py's ENV_FILE (which defaults to the install dir); the installer env lives at $INSTALL_DIR/.mbot.env and must be pointed at explicitly, or account add/remove writes land in a stray file. 面板与机器人读写 env 文件都走 web/config.py 的 ENV_FILE（它默认指向安装目录）；安装版 env 在 $INSTALL_DIR/.mbot.env，必须显式指过去，否则删账号 env_removed:false、加账号写进 stray 文件。
Environment="MBOT_WEB_ENV_FILE=${ENV_FILE}"
Environment="MBOT_CONFIG_FILE=${CONFIG_FILE}"
ExecStart=${VENV_DIR}/bin/uvicorn web.app:app --host 127.0.0.1 --port ${INTERNAL_PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    if [ "$BOT_ENABLED" = "1" ]; then
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
Environment="MBOT_WEB_ENV_FILE=${ENV_FILE}"
Environment="MBOT_CONFIG_FILE=${CONFIG_FILE}"
ExecStart=${VENV_DIR}/bin/python -m tgbot.bot
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        # Must use restart, not --now: in update mode the service is already running, --now does not restart it and the old process keeps running old code. 必须用 restart 不用 --now：更新模式下服务已在跑，--now 不重启，旧进程会一直跑旧代码。
        systemctl enable "$WEB_SERVICE" "$BOT_SERVICE" \
            || die "设置服务自启失败。"
        systemctl restart "$WEB_SERVICE" "$BOT_SERVICE" \
            || die "启动服务失败，请看 journalctl -u $WEB_SERVICE / -u $BOT_SERVICE。"
        ok "服务已启动（开机自启）。"
        return 0
    fi
    if [ -f "/etc/systemd/system/${BOT_SERVICE}.service" ]; then
        systemctl stop "$BOT_SERVICE" 2>/dev/null || true
        systemctl disable "$BOT_SERVICE" 2>/dev/null || true
        rm -f "/etc/systemd/system/${BOT_SERVICE}.service"
        info "已停掉旧的机器人服务（本次只装面板）。"
    fi
    systemctl daemon-reload
    systemctl enable "$WEB_SERVICE" \
        || die "设置面板服务自启失败。"
    systemctl restart "$WEB_SERVICE" \
        || die "启动面板服务失败，请看 journalctl -u $WEB_SERVICE."
    ok "面板服务已启动（开机自启）；未装机器人。"
}

# ---------- nginx 反代 ----------
# Panel nginx hardening (generic parts of the user's everyday Cloudreve reverse-proxy config for example.com). 面板 nginx 安全加固（用户常用 nginx.conf 的通用部分）。
# Source: the user's everyday Cloudreve reverse-proxy config (example.com). 来源：用户平时用的 Cloudreve 反代配置（example.com）。
# Only generic hardening is taken, not business-specific bits: the Cloudreve proxy address, the xray 8443 fallback, client_max_body_size 0 (panel uploads cap at 100m; 0 means unlimited). 只取通用加固项，不取业务专属项：Cloudreve 反代地址、xray 8443 回落、client_max_body_size 0（面板上传走 100m 上限，0 是无限上传）。
# Adds the missing security response headers (vuln test L-1) and blocks scanner crawlers. 修 ARM 漏洞测试 L-1（安全响应头全缺）并挡扫描器爬虫。
# NGINX_CONF is a vhost file under conf.d, included by nginx.conf's http block, so the map directives at the top of the file are in http context and legal. NGINX_CONF 是 conf.d 下的 vhost 文件，由 nginx.conf 的 http 块 include，所以文件顶部的 map 指令在 http 上下文，合法。
# Single-quoted definition: $ inside stays literal nginx variables when written to the config file. 单引号定义：内容里的 $ 是 nginx 变量，保持原样写进配置文件。
NGINX_SECURITY_HEADERS='    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;'
NGINX_HTTP_MAPS='    # map 哈希桶：正则较长，默认 64 字节桶会溢出报错。
    map_hash_bucket_size 128;
    map_hash_max_size 512;

    map $http_cf_connecting_ip $clientRealIp {
        ""      $remote_addr;
        default $http_cf_connecting_ip;
    }

    # Malicious-crawler filter (scanners only; generic library UAs like python/libwww/java are NOT blocked, so user scripts calling the panel API keep working). 恶意爬虫甄别（只拦扫描器；python/libwww/java 等通用库 UA 不拦，以免挡住用户用脚本调面板 API）。
    map $http_user_agent $is_bad_bot {
        default 0;
        "~*nmap|sqlmap|nikto|zgrab|masscan|shodan|fofa|censys|nuclei|scanner" 1;
    }'
NGINX_BAD_BOT_GUARD='        if ($is_bad_bot) { return 403; }'
# Is port 443 free for the panel (no non-nginx listener)? 443 空不空闲（有没有非 nginx 进程在监听）？
# Returns 0 (true) when 443 is free or only nginx holds it; 1 when another service (e.g. xray) holds it, or ss is missing.
# 443 空闲（或只有 nginx 在听）时返回 0；被别的服务（如 xray）占了、或没有 ss 命令时返回 1。
port_443_free() {
    if ! command_exists ss; then
        return 1
    fi
    ! ss -ltnp 2>/dev/null | grep ':443 ' | grep -qv nginx
}

# Preferred panel port: 443 (standard HTTPS) when free, else PANEL_PORT (9527).
# 面板优先端口：443 空闲时用 443（标准 HTTPS 端口），被占时回落 PANEL_PORT（9527）。
# Echoes the port number. 输出端口号。
pick_panel_port() {
    if port_443_free; then
        echo 443
    else
        echo "$PANEL_PORT"
    fi
}

write_nginx_selfsigned() {
    local cert_dir="$SELFSIGN_DIR"
    local port="${PANEL_LISTEN_PORT:-$(pick_panel_port)}"
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
# M_Bot panel reverse proxy (generated by install.sh, self-signed cert, listens on ${port}). M_Bot 面板反代（install.sh 生成，自签证书，监听 ${port}）。
${NGINX_HTTP_MAPS}
server {
    listen ${port} ssl;
    listen [::]:${port} ssl;
    server_name _;

    ssl_certificate ${cert_dir}/fullchain.pem;
    ssl_certificate_key ${cert_dir}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
${NGINX_SECURITY_HEADERS}

    client_max_body_size 100m;

    location / {
${NGINX_BAD_BOT_GUARD}
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$clientRealIp;
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
    ok "nginx 反代已生效（自签证书，端口 ${port}）。"
}

write_nginx_domain() {
    local domain="$1"
    # certbot only issues certs (certonly --webroot) and never edits the nginx config: we write the 443 vhost ourselves, because certbot --nginx fails its reload when 443 is held by xray and the issued cert would be wasted. certbot 只发证、不改 nginx 配置（certonly --webroot），443 的 vhost 我们自己写：certbot --nginx 会在 443 被 xray 占了时重载失败，证发了也白发。
    if command_exists ss; then
        local holder
        holder="$(ss -ltnp 2>/dev/null | grep ':80 ' | grep -v nginx | head -n 1 || true)"
        if [ -n "$holder" ]; then
            warn "80 端口已被占用：${holder}，域名证书验证不了，回落自签证书。"
            CERT_OK=0
            write_nginx_selfsigned
            return 0
        fi
    fi
    if ! command_exists certbot; then
        info "安装 certbot（域名证书用）…"
        wait_for_apt
        apt-get install -y -qq certbot >/dev/null 2>&1 \
            || warn "certbot 安装失败，只能用自签证书。"
    fi
    if command_exists certbot; then
        info "申请 Let's Encrypt 证书（域名 $domain，需该域名已解析到本机）…"
        write_nginx_acme_vhost "$domain" || die "nginx 验证配置写入失败。"
        if certbot certonly --webroot -w "$ACME_WEBROOT" -d "$domain" \
                --non-interactive --agree-tos \
                --register-unsafely-without-email >/dev/null 2>&1; then
            # Reload nginx after renewal: certbot's own timer renews certs, but without a deploy hook nginx keeps serving the old cert. 续期后重载 nginx：certbot 自带定时续期（certbot.timer），不挂 deploy hook 的话续了新证 nginx 还在用旧证。
            mkdir -p /etc/letsencrypt/renewal-hooks/deploy
            printf '#!/bin/sh\nsystemctl reload nginx\n' \
                > /etc/letsencrypt/renewal-hooks/deploy/mbot-reload-nginx.sh
            chmod +x /etc/letsencrypt/renewal-hooks/deploy/mbot-reload-nginx.sh
            if write_nginx_domain_https "$domain" \
                    "/etc/letsencrypt/live/${domain}/fullchain.pem" \
                    "/etc/letsencrypt/live/${domain}/privkey.pem" 1; then
                ok "Let's Encrypt 证书已申请并生效。"
                CERT_OK=1
                CERT_KIND=domain
                return 0
            fi
        fi
        warn "certbot 申请证书失败（域名可能未解析到本机），回落自签证书。"
    else
        warn "未装 certbot，回落自签证书。"
    fi
    CERT_OK=0
    write_nginx_selfsigned
}

# ---------- lego（IP 证书 / Cloudflare DNS-01） ----------
# Read CF credentials from the [cloudflare] section of $INSTALL_DIR/oci_config (old fallbacks ~/oci_config, ~/.oci/config): cf_token / cf_key (cf_account_key) / cf_email. When usable, set CF_API_TOKEN / CF_API_KEY / CF_EMAIL and return 0, else return 1 (caller asks the user instead). 从 OCI 配置文件（$INSTALL_DIR/oci_config，旧版回退 ~/oci_config 与 ~/.oci/config）的 [cloudflare] 段读 CF 凭据：cf_token / cf_key（cf_account_key）/ cf_email。读到可用的就设 CF_API_TOKEN / CF_API_KEY / CF_EMAIL 三个变量并返回 0，否则返回 1（调用方改问用户）。
# Parsed with python3 (configparser, section names case-insensitive; install_deps already installed python3 and prompt_config runs after it). 用 python3 解析（configparser，段名大小写不敏感；install_deps 已装好 python3，prompt_config 在它之后跑）。
read_cf_from_oci_config() {
    local cfg=""
    for f in "$INSTALL_DIR/oci_config" "$HOME/oci_config" "$HOME/.oci/config"; do
        [ -f "$f" ] && cfg="$f" && break
    done
    [ -n "$cfg" ] || return 1
    local out
    out="$(python3 -c '
import configparser, sys
p = configparser.ConfigParser(interpolation=None)
try:
    p.read(sys.argv[1], encoding="utf-8")
except configparser.Error:
    sys.exit(1)
sec = next((s for s in p.sections() if s.lower() == "cloudflare"), None)
if not sec:
    sys.exit(1)
items = {k: v.strip() for k, v in p.items(sec)}
token = items.get("cf_token", "")
key = items.get("cf_key") or items.get("cf_account_key", "")
email = items.get("cf_email", "")
if not (token or (key and email)):
    sys.exit(1)
print(token)
print(key)
print(email)
' "$cfg" 2>/dev/null)" || return 1
    CF_API_TOKEN="$(printf '%s' "$out" | sed -n '1p')"
    CF_API_KEY="$(printf '%s' "$out" | sed -n '2p')"
    CF_EMAIL="$(printf '%s' "$out" | sed -n '3p')"
    return 0
}

# Read the tenancy OCID from the [DEFAULT] section of $INSTALL_DIR/oci_config (old fallbacks ~/oci_config, ~/.oci/config); the root compartment is the tenancy. Used at install time to fill MBOT_COMPARTMENT_DEFAULT instead of a placeholder (a placeholder makes the panel instance list 502 Invalid compartmentId and backup creation 404). 从 OCI 配置文件（$INSTALL_DIR/oci_config，旧版回退 ~/oci_config 与 ~/.oci/config）的 [DEFAULT] 段读 tenancy OCID（根 compartment 即 tenancy）。安装时用它填 MBOT_COMPARTMENT_DEFAULT，不再写占位符（占位符会让面板实例列表报 502 “Invalid compartmentId”、创建备份报 404）。
# Return 1 when unreadable (caller omits the line; the panel derives it from the tenancy at startup). 读不到时返回 1（调用方不写这行，面板启动时再从 tenancy 推导）。
# Note: configparser treats [DEFAULT] as the default section, so it must be renamed to be readable. 注意 configparser 默认把 [DEFAULT] 当默认段，必须换名才能读到。
read_tenancy_from_oci_config() {
    local cfg=""
    for f in "$INSTALL_DIR/oci_config" "$HOME/oci_config" "$HOME/.oci/config"; do
        [ -f "$f" ] && cfg="$f" && break
    done
    [ -n "$cfg" ] || return 1
    python3 -c '
import configparser, sys
p = configparser.ConfigParser(
    default_section="__oci_no_defaults__", interpolation=None)
try:
    p.read(sys.argv[1], encoding="utf-8")
except configparser.Error:
    sys.exit(1)
sec = next((s for s in p.sections() if s.lower() == "default"), None)
if not sec:
    sys.exit(1)
tenancy = dict(p.items(sec)).get("tenancy", "").strip()
if not tenancy:
    sys.exit(1)
print(tenancy)
' "$cfg" 2>/dev/null
}

load_cf_credentials() {
    CF_API_TOKEN="${MBOT_INSTALL_CF_API_TOKEN:-${CF_API_TOKEN:-}}"
    CF_API_KEY="${MBOT_INSTALL_CF_API_KEY:-${CF_API_KEY:-${CF_Key:-}}}"
    CF_EMAIL="${MBOT_INSTALL_CF_EMAIL:-${CF_EMAIL:-${CF_Email:-}}}"
    if [ -z "$CF_API_TOKEN" ] && [ -z "$CF_API_KEY" ]; then
        if read_cf_from_oci_config; then
            info "从 OCI 配置文件 [cloudflare] 段读到 Cloudflare 凭据，直接用它。"
        fi
    fi
    [ -n "$CF_API_TOKEN" ] && return 0
    [ -n "$CF_API_KEY" ] && [ -n "$CF_EMAIL" ] && return 0
    return 1
}

# ---------- Cloudflare DNS 自动绑定 ----------
# A domain must resolve to this machine before issuing a cert: when it does not (or points elsewhere), use Cloudflare credentials to create/update the A record to this machine, then wait for DNS to take effect. 域名解析到本机是发证的前提：域名还没解析（或指到别处）时，有 Cloudflare 凭据就自动建/改 A 记录指到本机，再等解析生效。
# Credentials via CF_API_TOKEN (Bearer) or CF_API_KEY + CF_EMAIL (X-Auth-Key). 凭据走 CF_API_TOKEN（Bearer）或 CF_API_KEY + CF_EMAIL（X-Auth-Key）。
# Failure only returns 1; the caller warns and continues (cert issuance then fails and falls back to self-signed; install is not interrupted). 失败只返回 1，调用方警告后继续（发证会失败并回落自签，不中断安装）。
cf_api() {
    # $1 method, $2 path, $3 JSON body (optional). Prints the response body on success. $1 方法，$2 路径，$3 JSON body（可空）。成功打印 response body。
    local method="$1" path="$2" body="${3:-}"
    local auth=()
    if [ -n "${CF_API_TOKEN:-}" ]; then
        auth=(-H "Authorization: Bearer $CF_API_TOKEN")
    else
        auth=(-H "X-Auth-Email: $CF_EMAIL" -H "X-Auth-Key: $CF_API_KEY")
    fi
    if [ -n "$body" ]; then
        curl -s -m 20 -X "$method" "https://api.cloudflare.com/client/v4$path" \
            "${auth[@]}" -H "Content-Type: application/json" -d "$body" 2>/dev/null
    else
        curl -s -m 20 -X "$method" "https://api.cloudflare.com/client/v4$path" \
            "${auth[@]}" 2>/dev/null
    fi
}

public_ip() {
    local ip
    ip="$(curl -s -m 5 https://api.ipify.org 2>/dev/null || true)"
    [ -n "$ip" ] || ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "$ip"
}

dns_points_here() {
    local domain="$1" ip="$2"
    [ -n "$ip" ] || return 1
    python3 - "$domain" "$ip" <<'PYEOF' 2>/dev/null
import socket, sys
try:
    sys.exit(0 if sys.argv[2] in socket.gethostbyname_ex(sys.argv[1])[2] else 1)
except Exception:
    sys.exit(1)
PYEOF
}

# Bind the domain to this machine: return 0 if already resolving here; otherwise create/update the A record with CF credentials (DNS-only, proxy OFF — proxying blocks http-01 validation), then wait up to 60 seconds for DNS to take effect. Return 0 means the domain now points at this machine. 域名自动绑定到本机：已解析到本机直接返回 0；否则有 CF 凭据就建/改 A 记录（DNS-only，不开代理——代理会挡 http-01 验证），然后最多等 60 秒看解析生效。返回 0 表示域名已指到本机。
ensure_dns_points_here() {
    local domain="$1"
    local ip
    ip="$(public_ip)"
    if dns_points_here "$domain" "$ip"; then
        ok "域名 $domain 已解析到本机 $ip。"
        return 0
    fi
    load_cf_credentials || {
        warn "域名 $domain 未解析到本机 $ip，且没有 Cloudflare 凭据自动绑定。"
        warn "请手动添加 DNS A 记录：$domain → $ip（不开启 CF 代理），再重跑安装；本次先继续，证书可能申请失败并回落自签。"
        return 1
    }
    info "域名 $domain 未解析到本机，正在用 Cloudflare API 自动绑定…"
    local zone="" zone_id="" rest="$domain"
    local resp
    while [ -n "$rest" ]; do
        resp="$(cf_api GET "/zones?name=$rest")"
        zone_id="$(printf '%s' "$resp" | python3 -c "import json,sys; r=json.load(sys.stdin).get('result') or []; print(r[0]['id'] if r else '')" 2>/dev/null || true)"
        if [ -n "$zone_id" ]; then
            zone="$rest"
            break
        fi
        rest="${rest#*.}"
        case "$rest" in
            *.*) : ;;
            *) rest="" ;;
        esac
    done
    [ -n "$zone_id" ] || { warn "Cloudflare 里找不到域名 $domain 所属的 zone（凭据权限不足或域名不在此账号下）。"; return 1; }
    resp="$(cf_api GET "/zones/$zone_id/dns_records?type=A&name=$domain")"
    local rec_id
    rec_id="$(printf '%s' "$resp" | python3 -c "import json,sys; r=json.load(sys.stdin).get('result') or []; print(r[0]['id'] if r else '')" 2>/dev/null || true)"
    local body="{\"type\":\"A\",\"name\":\"$domain\",\"content\":\"$ip\",\"ttl\":120,\"proxied\":false}"
    if [ -n "$rec_id" ]; then
        resp="$(cf_api PUT "/zones/$zone_id/dns_records/$rec_id" "$body")"
    else
        resp="$(cf_api POST "/zones/$zone_id/dns_records" "$body")"
    fi
    local ok_flag
    ok_flag="$(printf '%s' "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin).get('success', False))" 2>/dev/null || true)"
    [ "$ok_flag" = "True" ] || { warn "Cloudflare API 建/改 A 记录失败（见上面返回）。"; return 1; }
    ok "DNS 记录已更新：$domain → $ip（DNS-only），等解析生效…"
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        dns_points_here "$domain" "$ip" && { ok "域名 $domain 解析已生效。"; return 0; }
        sleep 5
    done
    warn "DNS 记录已建，但 60 秒后解析仍未生效（可能是 DNS 缓存），证书申请可能失败。"
    return 1
}

# Let's Encrypt issues certs for public IPs since 2026-01-15, but only shortlived (160 hours, ~6 days), and validation is http-01 (port 80) or tls-alpn-01 (port 443) only — no dns-01. certbot needs 5.4+ for IP certs, which Debian/Ubuntu apt cannot provide, so IP certs and Cloudflare DNS-01 both use lego (single-file Go binary, v4.22+ supports --profile; tested working with v5.7.0; --email may be empty, lego registers with noemail@example.com). Let's Encrypt 从 2026-01-15 起正式给公网 IP 发证，但只发 shortlived（有效期 160 小时，约 6 天），验证只能用 http-01（80 端口）或 tls-alpn-01（443 端口），不能用 dns-01。certbot 要 5.4+ 才支持 IP 证书，Debian/Ubuntu 的 apt 装不到这么新的版本，所以 IP 证书与 Cloudflare DNS-01 统一用 lego（单文件 Go 二进制，v4.22+ 支持 --profile，v5.7.0 实测可用；--email 可留空，lego 会用 noemail@example.com 注册账号）。
install_lego() {
    if [ -x "$LEGO_BIN" ] && "$LEGO_BIN" --version 2>/dev/null | grep -q "$LEGO_VERSION"; then
        return 0
    fi
    local arch
    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) warn "lego 不支持当前架构 $(uname -m)，只能用自签证书。"; return 1 ;;
    esac
    local url="https://github.com/go-acme/lego/releases/download/${LEGO_VERSION}/lego_${LEGO_VERSION}_linux_${arch}.tar.gz"
    local tmp
    tmp="$(mktemp -d)"
    info "下载 lego ${LEGO_VERSION}（${arch}）…"
    if ! curl -sL -m 120 -o "$tmp/lego.tar.gz" "$url"; then
        warn "lego 下载失败，只能用自签证书。"
        rm -rf "$tmp"
        return 1
    fi
    if tar -xzf "$tmp/lego.tar.gz" -C "$tmp" lego 2>/dev/null \
            && install -m 755 "$tmp/lego" "$LEGO_BIN"; then
        rm -rf "$tmp"
        ok "lego 已安装：$("$LEGO_BIN" --version 2>/dev/null | head -n 1)"
        return 0
    fi
    warn "lego 解压/安装失败，只能用自签证书。"
    rm -rf "$tmp"
    return 1
}

# lego credential file (mode 600, only the given variables; the CF token never enters shell history and is never printed; the renewal script sources it before each run). lego 凭据文件（600 权限，只放给定的变量；CF Token 不进 shell 历史、不打印；续期脚本每次跑之前 source 它）。
# lego's Cloudflare credential names: CF_DNS_API_TOKEN (token) or CF_API_KEY + CF_API_EMAIL (global key + email). lego 的 Cloudflare 凭据名：CF_DNS_API_TOKEN（Token）或 CF_API_KEY + CF_API_EMAIL（Global Key + 邮箱）。
write_lego_env() {
    mkdir -p "$LEGO_DIR"
    chmod 700 "$LEGO_DIR"
    {
        [ -n "${CF_API_TOKEN:-}" ] && printf 'CF_DNS_API_TOKEN=%s\n' "$CF_API_TOKEN"
        [ -n "${CF_API_KEY:-}" ] && printf 'CF_API_KEY=%s\n' "$CF_API_KEY"
        [ -n "${CF_EMAIL:-}" ] && printf 'CF_API_EMAIL=%s\n' "$CF_EMAIL"
    } > "$LEGO_DIR/env" || true
    chmod 600 "$LEGO_DIR/env"
}

# lego deploy hook: reload nginx after a cert is created/renewed so the new cert takes effect. lego deploy hook：证书创建/续期成功后重载 nginx，让新证书生效。
# Kept as a standalone script file to avoid a space-containing command string after --deploy-hook (easy to misquote when the renewal script builds the command). 单独存成脚本文件，避免 --deploy-hook 后面跟带空格的命令串（续期脚本里拼命令时引用容易错）。
write_lego_deploy_hook() {
    cat > "$LEGO_DIR/deploy-hook.sh" <<'EOF'
#!/bin/sh
# lego deploy hook (generated by install.sh): reload nginx after a cert update. lego deploy hook（install.sh 生成）：证书更新后重载 nginx。
systemctl reload nginx 2>/dev/null || systemctl restart nginx
EOF
    chmod 700 "$LEGO_DIR/deploy-hook.sh"
}

# Renewal script + cron: in lego v5, run doubles as renewal and skips when outside the renewal window; shortlived certs renew at half life (~3 days), so every 2 days is right. $1 = the lego args used at issuance (a space-free token string, replayed verbatim on renewal; credentials are read from /etc/lego/env each time, never written into the script). 证书续期脚本 + cron：lego v5 里 run 兼续期，证书没到续期窗口时直接跳过；shortlived 证书默认剩一半寿命（约 3 天）时续，每 2 天跑一次正好。$1 = 发证时的 lego 参数（无空格的 token 串，续期原样重跑；凭据每次从 /etc/lego/env 读，不写进脚本）。
write_renew_script() {
    cat > "$RENEW_SCRIPT" <<EOF
#!/bin/bash
# M_Bot cert renewal (generated by install.sh): IP certs last only 160 hours, run every 2 days; domain certs auto-renew before expiry; lego skips when outside the renewal window. M_Bot 证书续期（install.sh 生成）：IP 证书只有 160 小时有效期，每 2 天跑一次；域名证书到期前自动续；未到续期窗口时 lego 直接跳过。
set -euo pipefail
[ -f $LEGO_DIR/env ] && set -a && . $LEGO_DIR/env && set +a || true
exec $LEGO_BIN run --path $LEGO_DIR --accept-tos $1
EOF
    chmod 700 "$RENEW_SCRIPT"
    printf '0 3 */2 * * root %s >> /var/log/mbot-cert-renew.log 2>&1\n' \
        "$RENEW_SCRIPT" > "$RENEW_CRON"
    chmod 644 "$RENEW_CRON"
    ok "证书自动续期已设置（每 2 天凌晨 3 点）。"
}

# Port-80 vhost for IP-cert validation: serves only ACME validation files, everything else 404 (the panel is not on port 80). server_name is the IP itself, because http-01 validation requests use the IP as Host and match exactly (other port-80 sites on the machine cannot steal the validation request). IP 证书验证用的 80 端口 vhost：只 serve ACME 验证文件，其余请求一律 404（面板不在 80 上）。server_name 写 IP 本身，http-01 的验证请求 Host 就是 IP，能精确匹配上（机器上若还有别的 80 端口站点也不会抢走验证请求）。
# Port-80 ACME validation vhost: serves only validation files, else 404. 80 端口的 ACME 验证 vhost：只 serve 验证文件，其余 404。
# server_name may be an IP (IP cert) or a domain (certbot http-01). server_name 可以是 IP（IP 证书）或域名（certbot http-01）。
write_nginx_acme_vhost() {
    local name="$1"
    mkdir -p "$ACME_WEBROOT/.well-known/acme-challenge"
    cat > "$NGINX_CONF" <<EOF
# M_Bot panel reverse proxy (generated by install.sh, ACME validation in progress). M_Bot 面板反代（install.sh 生成，ACME 验证中）。
server {
    listen 80;
    listen [::]:80;
    server_name ${name};

    location /.well-known/acme-challenge/ {
        root ${ACME_WEBROOT};
    }

    location / {
        return 404;
    }
}
EOF
    nginx -t >/dev/null 2>&1 || die "nginx 配置校验失败，请检查 $NGINX_CONF。"
    systemctl reload nginx 2>/dev/null || systemctl restart nginx \
        || die "nginx 重载失败，请看 journalctl -u nginx。"
}

write_nginx_lego_ip() {
    local ip="$1"
    local port="${PANEL_LISTEN_PORT:-$(pick_panel_port)}"
    local crt="$LEGO_CERT_DIR/${ip}.crt" key="$LEGO_CERT_DIR/${ip}.key"
    [ -f "$crt" ] && [ -f "$key" ] \
        || { warn "lego 证书文件不存在：$crt"; return 1; }
    cat > "$NGINX_CONF" <<EOF
# M_Bot panel reverse proxy (generated by install.sh, Let's Encrypt IP cert, ${ip}). M_Bot 面板反代（install.sh 生成，Let's Encrypt IP 证书，${ip}）。
${NGINX_HTTP_MAPS}
server {
    listen 80;
    listen [::]:80;
    server_name ${ip};

    location /.well-known/acme-challenge/ {
        root ${ACME_WEBROOT};
    }

    location / {
        return 404;
    }
}

server {
    listen ${port} ssl;
    listen [::]:${port} ssl;
    server_name ${ip};

    ssl_certificate ${crt};
    ssl_certificate_key ${key};
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
${NGINX_SECURITY_HEADERS}

    client_max_body_size 100m;

    location / {
${NGINX_BAD_BOT_GUARD}
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$clientRealIp;
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
    ok "nginx 反代已生效（Let's Encrypt IP 证书，端口 ${port}）。"
}

# Full config after a domain cert is issued (shared by lego cfdns and certbot http): panel on 443 ssl, or ${PANEL_PORT} when 443 is held by xray or similar (the cert stays valid; just include the port in the address); port 80 redirects. 域名拿到证书后的完整配置（lego cfdns 与 certbot http 共用）：面板走 443 ssl，443 已被 xray 之类服务占了时改走 ${PANEL_PORT}（证书照样有效，地址带端口就行）；80 端口做跳转。
# $1 domain, $2 cert fullchain, $3 private key, $4 whether to keep a validation entry on port 80 (certbot renewal uses webroot http-01, so port 80 must serve validation files; lego cfdns renews via DNS and needs none). $1 域名，$2 证书 fullchain，$3 私钥，$4 是否在 80 上留验证入口（certbot 续期用 webroot http-01，80 必须能 serve 验证文件；lego cfdns 续期走 DNS，不用留）。
# Sets CERT_PORT on success, returns 0; returns 1 on failure. 成功时设 CERT_PORT，返回 0；失败返回 1。
write_nginx_domain_https() {
    local domain="$1" crt="$2" key="$3" acme_on_80="${4:-0}"
    [ -f "$crt" ] && [ -f "$key" ] \
        || { warn "证书文件不存在：$crt"; return 1; }
    local port="${PANEL_LISTEN_PORT:-$(pick_panel_port)}"
    local redirect="https://\$host\$request_uri"
    if [ "$port" != "443" ]; then
        warn "443 端口已被占用，面板改走 ${port} 端口。"
        redirect="https://\$host:${port}\$request_uri"
    fi
    CERT_PORT="$port"
    local acme_location=""
    if [ "$acme_on_80" = "1" ]; then
        acme_location="    location /.well-known/acme-challenge/ {
        root ${ACME_WEBROOT};
    }
"
    fi
    cat > "$NGINX_CONF" <<EOF
# M_Bot panel reverse proxy (generated by install.sh, Let's Encrypt, domain ${domain}). M_Bot 面板反代（install.sh 生成，Let's Encrypt，域名 ${domain}）。
${NGINX_HTTP_MAPS}
server {
    listen ${port} ssl;
    listen [::]:${port} ssl;
    server_name ${domain};

    ssl_certificate ${crt};
    ssl_certificate_key ${key};
    ssl_protocols TLSv1.2 TLSv1.3;
    server_tokens off;
${NGINX_SECURITY_HEADERS}

    client_max_body_size 100m;

    location / {
${NGINX_BAD_BOT_GUARD}
        proxy_pass http://127.0.0.1:${INTERNAL_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$clientRealIp;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 60s;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name ${domain};
${acme_location}    location / {
        return 301 ${redirect};
    }
}
EOF
    nginx -t >/dev/null 2>&1 || die "nginx 配置校验失败，请检查 $NGINX_CONF。"
    systemctl reload nginx 2>/dev/null || systemctl restart nginx \
        || die "nginx 重载失败，请看 journalctl -u nginx。"
    ok "nginx 反代已生效（Let's Encrypt 证书，域名 ${domain}，端口 ${port}）。"
}

write_nginx_lego_domain() {
    local domain="$1"
    write_nginx_domain_https "$domain" \
        "$LEGO_CERT_DIR/${domain}.crt" "$LEGO_CERT_DIR/${domain}.key" 0
}

request_ip_cert() {
    local ip
    ip="$(curl -s -m 5 https://api.ipify.org 2>/dev/null || true)"
    [ -n "$ip" ] || { warn "拿不到公网 IP，只能用自签证书。"; return 1; }
    # Let's Encrypt does not issue certs for private/reserved addresses. Let's Encrypt 不给内网/保留地址发证。
    case "$ip" in
        10.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|192.168.*|127.*|0.*)
            warn "公网 IP 是内网地址 $ip，Let's Encrypt 不给内网 IP 发证，只能用自签证书。"
            return 1 ;;
    esac
    if command_exists ss; then
        local holder
        holder="$(ss -ltnp 2>/dev/null | grep ':80 ' | grep -v nginx | head -n 1 || true)"
        if [ -n "$holder" ]; then
            warn "80 端口已被占用：${holder}，IP 证书验证不了，只能用自签证书。"
            return 1
        fi
    fi
    install_lego || return 1
    write_lego_env
    write_lego_deploy_hook
    write_nginx_acme_vhost "$ip" || return 1
    info "申请 Let's Encrypt IP 证书（$ip，shortlived，有效期约 6 天，自动续期）…"
    local args="--profile shortlived --http --http.webroot $ACME_WEBROOT -d $ip --deploy-hook $LEGO_DIR/deploy-hook.sh"
    if ( [ -f "$LEGO_DIR/env" ] && set -a && . "$LEGO_DIR/env" && set +a || true; \
         "$LEGO_BIN" run --path "$LEGO_DIR" --accept-tos $args >/dev/null 2>&1 ); then
        write_renew_script "$args"
        if write_nginx_lego_ip "$ip"; then
            CERT_OK=1
            CERT_KIND=ip
            ok "Let's Encrypt IP 证书已生效（每 2 天自动续期）。"
            return 0
        fi
    fi
    warn "IP 证书申请失败（可能 80 端口未对外放行），回落自签证书。"
    return 1
}

request_domain_cert_cfdns() {
    local domain="$1"
    install_lego || return 1
    write_lego_env
    write_lego_deploy_hook
    info "申请 Let's Encrypt 证书（域名 $domain，Cloudflare DNS 验证）…"
    # lego's DNS-01 propagation check uses the local system resolver by default; OCI's resolver (169.254.169.254) negative-caches new records for 1800 seconds, so cfdns always fails. Use public resolvers 1.1.1.1 / 8.8.8.8 instead. lego 的 DNS-01 传播检查默认走本机系统解析器；OCI 的解析器（169.254.169.254）对新建记录有 1800 秒负缓存，cfdns 必失败。改走公共解析器 1.1.1.1 / 8.8.8.8。
    local args="--dns cloudflare --dns.resolvers 1.1.1.1:53,8.8.8.8:53 -d $domain --deploy-hook $LEGO_DIR/deploy-hook.sh"
    # Keep a copy of lego's output as a log (for failure diagnosis; it used to go entirely to /dev/null, and the error misleadingly blamed token permissions when it was often a propagation-check timeout). lego 输出留一份日志（失败时定位用；以前全丢 /dev/null，报错还误导说 Token 权限不对，实际常是传播检查超时）。
    local lego_log="$LEGO_DIR/lego-last.log"
    if ( [ -f "$LEGO_DIR/env" ] && set -a && . "$LEGO_DIR/env" && set +a || true; \
         "$LEGO_BIN" run --path "$LEGO_DIR" --accept-tos $args >"$lego_log" 2>&1 ); then
        write_renew_script "$args"
        if write_nginx_lego_domain "$domain"; then
            CERT_OK=1
            CERT_KIND=domain
            ok "Let's Encrypt 证书已生效（Cloudflare DNS 验证）。"
            return 0
        fi
    fi
    warn "Cloudflare DNS 验证失败（可能是 DNS 传播未完成、Token 权限不对或域名不在该账号下），详见 $lego_log，回落自签证书。"
    return 1
}

setup_nginx() {
    # When 9527 is held by a non-nginx process: fine if the panel runs on 443 (443 free), just warn; otherwise the panel needs 9527 and cannot bind, so stop the holder first. 9527 已被非 nginx 进程占用时：443 空闲、面板走 443 就没关系，只警告；否则面板要走 9527，绑不上只能先停掉它。
    if command_exists ss; then
        local holder
        holder="$(ss -ltnp 2>/dev/null | grep ":${PANEL_PORT} " | head -n 1 || true)"
        if [ -n "$holder" ]; then
            case "$holder" in
                *nginx*) : ;;  # 已是我们自己的 nginx，重装时正常
                *)
                    if port_443_free; then
                        warn "端口 ${PANEL_PORT} 已被占用：${holder}。443 空闲，面板走 443，不影响。"
                    else
                        die "端口 ${PANEL_PORT} 已被占用：${holder}。请先停掉它再安装。"
                    fi
                    ;;
            esac
        fi
    fi
    # 面板优先走 443（标准 HTTPS 端口），被占时回落 PANEL_PORT（9527）；下面所有写配置的函数都用这个端口。
    PANEL_LISTEN_PORT="$(pick_panel_port)"
    if [ -n "${DOMAIN:-}" ]; then
        ensure_dns_points_here "$DOMAIN" || true
        if [ "${CERT_METHOD:-http}" = "cfdns" ]; then
            request_domain_cert_cfdns "$DOMAIN" || write_nginx_selfsigned
        else
            write_nginx_domain "$DOMAIN"
        fi
    else
        request_ip_cert || write_nginx_selfsigned
    fi
}

# SSH port: actual sshd listening port > sshd_config > 22. SSH 端口：sshd 实际监听端口 > sshd_config > 22。
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

# ---------- 本机防火墙 ----------
# Auto-detect the running firewall and open the ports this project needs: SSH port (current actual port) + 80 + 443 (with a domain) + 9527 (panel port). 自动识别在运行的防火墙类型并放行本项目需要的端口：SSH 端口（当前实际端口）+ 80 + 443（有域名时）+ 9527（面板端口）。
# Supports ufw / firewalld / nftables / iptables; when none is running, prefer enabling ufw (if installed), otherwise ports are open by default and this is skipped. 支持 ufw / firewalld / nftables / iptables；都没在跑时优先用 ufw（装了就启用），否则端口默认就是通的，跳过。
# Check order matters: on Debian 12+ UFW runs on an nft backend, so its chains appear in the nft ruleset — detect UFW first or UFW is misread as bare nft. 注意检测顺序：UFW 在 Debian 12+ 上走 nft 后端，nft ruleset 里有 ufw 的链——必须先认 UFW，否则会把 UFW 误判成裸 nft。
detect_firewall() {
    if command_exists ufw && ufw status 2>/dev/null | grep -q "Status: active"; then
        echo "ufw"; return 0
    fi
    if command_exists firewall-cmd && firewall-cmd --state 2>/dev/null | grep -q "^running$"; then
        echo "firewalld"; return 0
    fi
    if command_exists nft && nft list ruleset 2>/dev/null | grep -qE "table (inet|ip) filter"; then
        echo "nft"; return 0
    fi
    if command_exists iptables; then
        local policy
        policy="$(iptables -S INPUT 2>/dev/null | head -n 1 || true)"
        if [ "$policy" != "-P INPUT ACCEPT" ]; then
            echo "iptables"; return 0
        fi
        if iptables -S INPUT 2>/dev/null | grep -qE "^-A INPUT .*(-j DROP|-j REJECT)"; then
            echo "iptables"; return 0
        fi
    fi
    echo "none"
}

# Ports the panel needs (excluding SSH): 80 always + the panel's listen port (443 when free, else 9527). 面板需要的端口（不含 SSH）：80 始终 + 面板实际监听的端口（443 空闲时走 443，被占时走 9527）。
# Do not open 9527 when the panel uses 443: nothing listens on 9527 then, and opening it could expose someone else's service later. 面板走 443 时不放行 9527：那时 9527 上没东西，放行只会以后暴露别人的服务。
panel_ports() {
    local ports="80"
    local panel_port
    panel_port="$(pick_panel_port)"
    if [ "$panel_port" = "443" ]; then
        ports="$ports 443"
    else
        # 443 被占，面板回落 PANEL_PORT；只有它没被别的服务占时才放行。
        if ! command_exists ss || ! ss -ltnp 2>/dev/null | grep ":${PANEL_PORT} " | grep -qv nginx; then
            ports="$ports ${PANEL_PORT}"
        fi
    fi
    echo "$ports"
}

# Ports to open (space-separated): SSH always + panel ports. Port 80 is unconditional: IP-cert issuance and renewal need port 80. 需要放行的端口（空格分隔）：SSH 始终 + 面板端口。80 必须无条件：IP 证书验证（申请与续期）要用 80。
firewall_ports() {
    echo "$(detect_ssh_port) $(panel_ports)"
}

setup_firewall() {
    local fw
    fw="$(detect_firewall)"
    info "本机防火墙：$fw（放行 $(firewall_ports)）…"
    case "$fw" in
        ufw) _firewall_ufw ;;
        firewalld) _firewall_firewalld ;;
        nft) _firewall_nft ;;
        iptables) _firewall_iptables ;;
        none)
            if command_exists ufw; then
                info "没有在运行的防火墙，用 ufw（启用它）。"
                _firewall_ufw
            else
                info "本机没有在运行的防火墙，端口默认都是通的，跳过。"
            fi
            ;;
    esac
}

# UFW: open the SSH port first (actual sshd listening port > sshd_config > 22), or ufw enable locks us out. UFW：开启前先放行 SSH 端口（sshd 实际监听端口 > sshd_config > 22），否则 ufw enable 会把自己锁在外面。
_firewall_ufw() {
    local port
    for port in $(firewall_ports); do
        ufw allow "$port/tcp" >/dev/null 2>&1 || true
    done
    if ufw status 2>/dev/null | grep -q "Status: inactive"; then
        ufw --force enable >/dev/null 2>&1 || warn "ufw 开启失败，请手动检查。"
        ok "UFW 已开启（已放行 SSH + 面板端口）。"
    else
        ok "UFW 规则已添加（防火墙本来就是开着的）。"
    fi
    # Oracle cloud images (Ubuntu) ship /etc/iptables/rules.v4 with an INPUT -j REJECT (only port 22 allowed) sitting ahead of UFW's chains: it blocks every UFW allow rule, so panel ports look like No route to host from outside while local curl works. The REJECT is redundant once UFW is on (UFW denies inbound by default), so remove it. Oracle 云镜像（Ubuntu）自带 /etc/iptables/rules.v4，INPUT 链里有一条 -j REJECT（只放行 22），位置在 UFW 的链前面，会把 UFW 的放行规则全部挡住——面板端口从外网看就是 No route to host，本机 curl 却正常。UFW 开启后这条 REJECT 是多余的（UFW 默认入站就是拒绝），删掉它。
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        remove_shadow_reject 4
        remove_shadow_reject 6
    fi
}

_firewall_firewalld() {
    local port
    for port in $(firewall_ports); do
        firewall-cmd --permanent --add-port="$port/tcp" >/dev/null 2>&1 || true
    done
    firewall-cmd --reload >/dev/null 2>&1 || warn "firewalld 重载失败，请手动检查。"
    ok "firewalld 规则已添加（--permanent，重启仍在）。"
}

# nftables: find the filter table's input chain and insert allow rules at its head (insert goes first, so a trailing drop cannot block them; existing rules are not duplicated). nftables：找到 filter 表的 input 链，在链首插入放行规则（insert 放最前面，避免被链尾的 drop 挡住；已有的不重复加）。
# When the filter table exists but has no input chain: an entirely empty table (no chains/rules, e.g. Ubuntu 24.04's table ip/ip6 filter {}) falls back to UFW (for persistent rules); a table with rules is left alone with a warning (it may hold user rules). filter 表存在但没有 input 链时：表全是空表（无任何链/规则，如 Ubuntu 24.04 的 table ip/ip6 filter {}）就回落到 UFW（规则持久化）；已有规则的表不动，只警告（可能有用户自定义规则）。
_firewall_nft() {
    local chain=""
    local table
    for table in "inet filter" "ip filter" "ip6 filter"; do
        if nft list chain $table input >/dev/null 2>&1; then
            chain="$table input"
            break
        fi
    done
    if [ -z "$chain" ]; then
        local has_rules=0
        for table in "inet filter" "ip filter" "ip6 filter"; do
            if nft list table $table 2>/dev/null | grep -qE "^[[:space:]]*(chain|set|map|flowtable|meter|quota|limit|counter)"; then
                has_rules=1
                break
            fi
        done
        if [ "$has_rules" = "0" ] && command_exists ufw; then
            info "nft filter 表是空的、没有 input 链，回落到 UFW 放行端口。"
            _firewall_ufw
            return 0
        fi
        warn "nft 有 filter 表但找不到 input 链，请手动放行端口。"
        return 0
    fi
    local port
    for port in $(firewall_ports); do
        if ! nft list chain $chain 2>/dev/null | grep -q "tcp dport $port accept"; then
            nft insert rule $chain tcp dport "$port" accept 2>/dev/null \
                || warn "nft 放行 $port 失败，请手动检查。"
        fi
    done
    ok "nft 规则已添加。"
    warn "nft 规则只在当前生效，重启后会丢（除非写进 /etc/nftables.conf）；建议改用 ufw 或 firewalld 管理。"
}

_firewall_iptables() {
    local port ipt
    for ipt in iptables ip6tables; do
        command_exists "$ipt" || continue
        for port in $(firewall_ports); do
            "$ipt" -C INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1 \
                || "$ipt" -I INPUT -p tcp --dport "$port" -j ACCEPT >/dev/null 2>&1 \
                || warn "$ipt 放行 $port 失败，请手动检查。"
        done
    done
    ok "iptables 规则已添加。"
    warn "iptables 规则只在当前生效，重启后会丢（除非装了 iptables-persistent 或写进 /etc/iptables/rules.v4）；建议改用 ufw 或 firewalld 管理。"
}

# ---------- 云端安全列表（OCI） ----------
# The local firewall is not enough: the OCI instance's security list (security group) must also allow 80/443/9527, or traffic still cannot reach the panel from outside. Added automatically here via the OCI API. 本机防火墙开了还不够：OCI 实例的安全列表（安全组）那层也要放行 80/443/9527，否则外网照样进不来。这里经 OCI API 自动加规则。
# Only on OCI (confirmed by reading instance metadata); auth prefers the existing OCI config file ($INSTALL_DIR/oci_config), falling back to instance principal (needs a dynamic group + policy). 只在 OCI 上跑（读实例元数据确认）；认证优先用已有的 OCI 配置文件（$INSTALL_DIR/oci_config），没有时试实例主体（需动态组 + 策略）。
# Any step failing only warns and prints manual steps; install is not interrupted. 任何一步失败都只警告 + 打印手动操作步骤，安装不中断。
open_cloud_ports() {
    local meta
    meta="$(curl -s -m 3 http://169.254.169.254/opc/v1/instance/ 2>/dev/null || true)"
    [ -n "$meta" ] || { info "不是 OCI 实例（无元数据服务），跳过云端放行。"; return 0; }
    echo "$meta" | grep -q "ocid1.instance" || { info "元数据不是 OCI 格式，跳过云端放行。"; return 0; }
    local oci_bin="$VENV_DIR/bin/oci"
    [ -x "$oci_bin" ] || { warn "OCI CLI 不在 $oci_bin，跳过云端放行。"; return 0; }
    local ports
    ports="$(panel_ports)"
    info "OCI 实例：检查云端安全列表是否放行 $ports …"
    if _oci_open_ports "$oci_bin" "$ports"; then
        CLOUD_OPENED=1
        return 0
    fi
    CLOUD_OPENED=0
    warn "云端放行没完成（见上面原因），请手动在 OCI 控制台放行："
    warn "  Networking → VCN → 子网 → 安全列表 → 添加入站规则：来源 0.0.0.0/0，TCP 目标端口 $ports。"
    return 0
}

_oci_open_ports() {
    local oci_bin="$1" ports="$2"
    local auth_args=""
    if [ ! -f "$INSTALL_DIR/oci_config" ] && [ ! -f "$HOME/oci_config" ] && [ ! -f "$HOME/.oci/config" ]; then
        auth_args="--auth instance_principal"
    fi
    export SUPPRESS_LABEL_WARNING=True
    local vnic_id
    vnic_id="$(curl -s -m 3 http://169.254.169.254/opc/v1/vnics/ 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['vnicId'])" 2>/dev/null || true)"
    [ -n "$vnic_id" ] || { warn "读不到主网卡 ID（元数据异常）。"; return 1; }
    local vnic
    vnic="$("$oci_bin" $auth_args network vnic get --vnic-id "$vnic_id" --output json 2>/dev/null)" \
        || { warn "OCI API 调用失败（无可用认证：既没有 OCI 配置文件，实例主体也没配动态组/策略）。"; return 1; }
    local subnet_id nsg_ids
    subnet_id="$(printf '%s' "$vnic" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['subnet-id'])" 2>/dev/null || true)"
    nsg_ids="$(printf '%s' "$vnic" | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['data'].get('nsg-ids') or []))" 2>/dev/null || true)"
    [ -n "$subnet_id" ] || { warn "读不到子网 ID。"; return 1; }
    if [ -n "$nsg_ids" ]; then
        # When the VNIC has an NSG attached, security-list rules do not apply; add NSG rules instead (additive). 网卡挂了 NSG 时安全列表规则不生效，只加 NSG 规则（additive）。
        local nsg
        for nsg in $nsg_ids; do
            _oci_add_nsg_rules "$oci_bin" "$auth_args" "$nsg" "$ports" || return 1
        done
        return 0
    fi
    local sl_ids
    sl_ids="$("$oci_bin" $auth_args network subnet get --subnet-id "$subnet_id" --output json 2>/dev/null \
        | python3 -c "import json,sys; print(' '.join(json.load(sys.stdin)['data'].get('security-list-ids') or []))" 2>/dev/null || true)"
    [ -n "$sl_ids" ] || { warn "读不到安全列表 ID。"; return 1; }
    local sl
    for sl in $sl_ids; do
        _oci_add_sl_rules "$oci_bin" "$auth_args" "$sl" "$ports" || return 1
    done
    return 0
}

# Given the existing ingress rules JSON and a port list, output the completed rules JSON. 给定现有 ingress 规则 JSON 与端口列表，输出补齐后的规则 JSON。
# With $3 = "nsg", output only the new rules (NSG rules add is incremental) and include the direction field; otherwise output the full set (security-list update replaces the whole list). $3 = "nsg" 时只输出新增的规则（NSG rules add 是增量的），并带 direction 字段；默认输出全量（安全列表 update 是全量替换）。
# When everything is already allowed, output nothing and exit 2 (caller skips the API call). 全部已放行时不输出、退出 2（调用方据此跳过 API 调用）。
# Already-allowed means: source 0.0.0.0/0 with protocol=all, or a TCP rule whose port range covers the port (no tcp-options means all TCP ports); NSG rules only consider direction=INGRESS. 已放行的判定：来源 0.0.0.0/0 且 protocol=all（全协议），或 TCP 规则的端口范围覆盖该端口（无 tcp-options 视为全部 TCP 端口）；NSG 规则只看 direction=INGRESS 的。
_oci_rules_with_ports() {
    python3 - "$1" "$2" "${3:-}" <<'PYEOF'
import json, sys
rules = json.loads(sys.argv[1] or "[]")
ports = [int(p) for p in sys.argv[2].split()]
nsg_mode = len(sys.argv) > 3 and sys.argv[3] == "nsg"

def covered(port):
    for r in rules:
        if r.get("direction") not in (None, "INGRESS"):
            continue
        if r.get("source") != "0.0.0.0/0":
            continue
        if r.get("protocol") == "all":
            return True
        if r.get("protocol") != "6":
            continue
        opts = r.get("tcp-options") or {}
        rng = opts.get("destination-port-range") or {}
        if not rng:
            return True
        if rng.get("min", 0) <= port <= rng.get("max", 65535):
            return True
    return False

missing = [p for p in ports if not covered(p)]
if not missing:
    sys.exit(2)
added = []
for p in missing:
    rule = {"source": "0.0.0.0/0", "source-type": "CIDR_BLOCK",
            "protocol": "6", "description": "M_Bot panel",
            "is-stateless": False,
            "tcp-options": {"destination-port-range": {"min": p, "max": p}}}
    if nsg_mode:
        rule["direction"] = "INGRESS"
    added.append(rule)
out = added if nsg_mode else rules + added
# update only accepts clean fields: strip null option keys from the GET response. update 只接受干净的字段：去掉 GET 返回的 null 选项键。
clean = [{k: v for k, v in r.items() if v is not None} for r in out]
print(json.dumps(clean))
PYEOF
}

_oci_add_sl_rules() {
    local oci_bin="$1" auth_args="$2" sl_id="$3" ports="$4"
    local current new_rules rc
    current="$("$oci_bin" $auth_args network security-list get --security-list-id "$sl_id" --output json 2>/dev/null \
        | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['data'].get('ingress-security-rules') or []))" 2>/dev/null || true)"
    [ -n "$current" ] || { warn "读安全列表失败。"; return 1; }
    new_rules="$(_oci_rules_with_ports "$current" "$ports")"
    rc=$?
    if [ "$rc" = "2" ]; then
        ok "云端安全列表已放行 $ports，无需改动。"
        return 0
    fi
    [ "$rc" = "0" ] || { warn "生成安全列表规则失败。"; return 1; }
    "$oci_bin" $auth_args network security-list update --security-list-id "$sl_id" \
        --ingress-security-rules "$new_rules" --force >/dev/null 2>&1 \
        || { warn "更新安全列表失败（权限不足或 API 错误）。"; return 1; }
    ok "云端安全列表已放行 $ports。"
    return 0
}

_oci_add_nsg_rules() {
    local oci_bin="$1" auth_args="$2" nsg_id="$3" ports="$4"
    local current new_rules rc
    current="$("$oci_bin" $auth_args network nsg rules list --nsg-id "$nsg_id" --output json 2>/dev/null \
        | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('data') or []))" 2>/dev/null || true)"
    [ -n "$current" ] || { warn "读 NSG 规则失败。"; return 1; }
    new_rules="$(_oci_rules_with_ports "$current" "$ports" "nsg")"
    rc=$?
    if [ "$rc" = "2" ]; then
        ok "云端 NSG 已放行 $ports，无需改动。"
        return 0
    fi
    [ "$rc" = "0" ] || { warn "生成 NSG 规则失败。"; return 1; }
    "$oci_bin" $auth_args network nsg rules add --nsg-id "$nsg_id" \
        --security-rules "$new_rules" >/dev/null 2>&1 \
        || { warn "更新 NSG 失败（权限不足或 API 错误）。"; return 1; }
    ok "云端 NSG 已放行 $ports。"
    return 0
}

# Remove the image-shipped INPUT REJECT rule that sits ahead of UFW's chains. 删掉镜像自带的、挡在 UFW 链前面的 INPUT REJECT 规则。
# Only the INPUT rule; FORWARD and InstanceServices (OCI metadata service) are untouched. 只删 INPUT 那条；FORWARD 与 InstanceServices（OCI 元数据服务）不动。
# $1: 4 or 6. $1: 4 或 6。
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
    if [ -f "/etc/systemd/system/${BOT_SERVICE}.service" ]; then
        if systemctl is-active --quiet "$BOT_SERVICE"; then
            ok "机器人服务运行中。"
        elif systemctl is-enabled --quiet "$BOT_SERVICE" 2>/dev/null; then
            warn "机器人服务未运行（常见原因是 Token 不对）！看日志：journalctl -u $BOT_SERVICE -n 50"
            fail=1
        else
            info "机器人服务已停用，跳过检查。"
        fi
    else
        info "未装机器人服务，跳过检查。"
    fi
    # The internal port is uvicorn (the panel itself) and the external port is nginx (the proxy). Both must be up: checking only the external port reports healthy when uvicorn is dead (nginx still listens). 内部端口是 uvicorn（面板本身），外部端口是 nginx（反代）。两个都要在：只查外部端口会在 uvicorn 挂了时误报正常（nginx 自己还在监听）。
    if command_exists ss && ss -ltn 2>/dev/null | grep -q ":${INTERNAL_PORT} "; then
        ok "面板内部端口 ${INTERNAL_PORT} 监听中。"
    else
        warn "面板内部端口 ${INTERNAL_PORT} 未监听，uvicorn 可能未起来。"
        fail=1
    fi
    # External port: 443 for a domain deploy (9527 when 443 is taken), 9527 without a domain. 外部端口：域名部署走 443（443 被占时走 9527），无域名走 9527。
    # Check that an nginx process holds 9527 or 443, not just that the port listens: 443 may be held by xray or similar, and a bare port check would falsely report healthy. 查“nginx 进程占着 9527 或 443”而不是只查端口在听：443 可能被 xray 之类服务占着，只查端口会误报正常。
    if command_exists ss && ss -ltnp 2>/dev/null | grep nginx | grep -qE ":(${PANEL_PORT}|443) "; then
        ok "nginx 反代监听中。"
    else
        warn "nginx 反代未监听（${PANEL_PORT} 或 443），面板可能无法访问。"
        fail=1
    fi
    return "$fail"
}

# ---------- 安装后自检 ----------
# Script the manual verification checklist: cert, login page, real login, redirect. 把人工验证的那几项做到脚本里：证书、登录页、真实登录、跳转。
# Only on first install (in update mode the password/domain are not in memory, so login cannot be verified). 只在首次安装时跑（更新模式下密码/域名不在内存里，没法验登录）。
# $1: panel admin password (empty skips the real-login check). $1：面板管理员密码（可空，空时跳过真实登录那项）。
self_test() {
    local password="${1:-}"
    local base_url ip host port
    ip="$(public_ip)"
    if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
        if [ "${CERT_PORT:-443}" = "443" ]; then
            base_url="https://${DOMAIN}"
        else
            base_url="https://${DOMAIN}:${CERT_PORT}"
        fi
    else
        panel_port="${PANEL_LISTEN_PORT:-$PANEL_PORT}"
        if [ "$panel_port" = "443" ]; then
            base_url="https://${ip}"
        else
            base_url="https://${ip}:${panel_port}"
        fi
    fi
    host="$(printf '%s' "$base_url" | sed -E 's|https?://([^:/]+).*|\1|')"
    port="$(printf '%s' "$base_url" | sed -E 's|.*:([0-9]+)$|\1|')"
    [ "$port" = "$base_url" ] && port=443
    # Self-signed certs need curl -k; LE certs are strictly validated (a bad chain fails). 自签证书时 curl 要 -k；LE 证书严格校验（链不对就报失败）。
    local k=""
    [ "${CERT_KIND:-selfsigned}" = "selfsigned" ] && k="-k"

    echo
    echo "--- 安装自检 ---"

    local cert_line="跳过"
    if [ "${CERT_KIND:-selfsigned}" = "domain" ] || [ "${CERT_KIND:-selfsigned}" = "ip" ]; then
        local cert_text not_after
        cert_text="$(echo | openssl s_client -connect "$host:$port" -servername "$host" 2>/dev/null | openssl x509 -noout -issuer -subject -dates -checkend 0 2>/dev/null || true)"
        not_after="$(printf '%s' "$cert_text" | grep "notAfter" | cut -d= -f2)"
        if printf '%s' "$cert_text" | grep -q "Let's Encrypt" \
            && printf '%s' "$cert_text" | grep -q "Certificate will not expire" \
            && printf '%s' "$cert_text" | grep -q "$host"; then
            cert_line="通过（Let's Encrypt，有效至 $not_after）"
        else
            cert_line="失败（证书内容不对）"
        fi
    else
        if echo | openssl s_client -connect "$host:$port" -servername "$host" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | grep -q "subject="; then
            cert_line="通过（自签证书，握手正常）"
        else
            cert_line="失败（握手失败）"
        fi
    fi
    echo "证书：$cert_line"

    local code page_line
    code="$(curl -s $k -o /dev/null -w "%{http_code}" --max-time 15 "$base_url/login" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then
        page_line="通过（$base_url/login 返回 200）"
    else
        page_line="失败（返回 $code）"
    fi
    echo "登录页：$page_line"

    local login_line="跳过（无密码）"
    if [ -n "$password" ]; then
        local body cookie_file
        body="$(python3 -c "import json,sys; print(json.dumps({'username': 'admin', 'password': sys.argv[1]}))" "$password")"
        cookie_file="/tmp/mbot_selftest_cookies.txt"
        code="$(curl -s $k -o /dev/null -w "%{http_code}" --max-time 15 \
            -c "$cookie_file" -H "Content-Type: application/json" \
            -d "$body" "$base_url/api/auth/login" 2>/dev/null || echo 000)"
        if [ "$code" = "200" ] && grep -q "mbot_session" "$cookie_file" 2>/dev/null; then
            login_line="通过（admin 登录成功，会话 cookie 已设置）"
        else
            login_line="失败（登录接口返回 $code）"
        fi
        rm -f "$cookie_file"
    fi
    echo "真实登录：$login_line"

    local redirect_line="跳过（无域名或证书未成功）"
    if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
        local loc
        loc="$(curl -s -o /dev/null -w "%{http_code} %{redirect_url}" --max-time 10 "http://${DOMAIN}/login" 2>/dev/null || echo "000")"
        case "$loc" in
            301\ https://*) redirect_line="通过（http 自动跳 https）" ;;
            *) redirect_line="失败（返回 $loc）" ;;
        esac
    fi
    echo "http 跳转：$redirect_line"

    # Port 80 reachability from outside needs no separate test: a successful http-01 issuance already proves it. 80 端口外网可达不用再测：http-01 发证成功本身就是证明。
    if { [ "${CERT_KIND:-selfsigned}" = "domain" ] || [ "${CERT_KIND:-selfsigned}" = "ip" ]; } \
        && [ "${CERT_METHOD:-http}" = "http" ]; then
        echo "外网 80 端口：可达（http-01 发证成功已证明）"
    fi
}

print_access_info() {
    local ip
    ip="$(curl -s -m 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
    echo
    echo "==================== 安装完成 ===================="
    # Only give the domain URL when the cert was really issued: after a certbot failure falls back to self-signed, the panel serves on IP:9527 and a domain URL would mislead (the domain does not resolve). 域名地址只在证书真的申请成功时才给：certbot 失败回落自签后，面板走的是 IP:9527，给域名地址会误导用户（域名根本不解析）。
    if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
        if [ "${CERT_PORT:-443}" = "443" ]; then
            echo "面板地址：https://${DOMAIN}/login"
        else
            echo "面板地址：https://${DOMAIN}:${CERT_PORT}/login（443 端口被占用，面板改走 ${CERT_PORT} 端口）"
        fi
    else
        if [ -n "${DOMAIN:-}" ]; then
            echo "域名证书申请失败，已回落自签证书（域名解析到本机后可重跑安装）。"
        fi
        panel_port="${PANEL_LISTEN_PORT:-$PANEL_PORT}"
        if [ "$panel_port" = "443" ]; then
            echo "面板地址：https://${ip}/login"
        else
            echo "面板地址：https://${ip}:${panel_port}/login"
        fi
        if [ "${CERT_KIND:-selfsigned}" = "ip" ]; then
            echo "（Let's Encrypt IP 证书，有效期约 6 天，cron 每 2 天自动续期）"
        else
            echo "（自签证书，浏览器提示不受信任时点“继续访问”即可）"
        fi
    fi
    echo "用户名：admin"
    if [ "${GENERATED_PASSWORD:-0}" = "1" ]; then
        echo "密码：${ADMIN_PASSWORD}（只显示这一次，也存在 $ADMIN_PASSWORD_FILE）"
    else
        echo "密码：你刚才输入的密码"
    fi
    echo
    echo "下一步："
    if [ "${BOT_ENABLED:-1}" = "1" ]; then
        echo "  1. 在 Telegram 里找你的机器人，发 /uploadapi 上传 OCI 配置"
        echo "     （安装目录的 oci_config + API 私钥），或手动放到服务器的安装目录（oci_config）与 /root/（API 私钥）下。"
        echo "  2. 发 /oracle 打开菜单，点“切换账号/区域”确认账号已接通。"
    else
        echo "  1. 把 OCI 配置手动放到服务器的安装目录下（oci_config + API 私钥，私钥放 /root/），"
        echo "     面板里切换账号即可用；或在 $ENV_FILE 里填 Token 后重跑安装补装机器人。"
    fi
    if [ "${CLOUD_OPENED:-0}" = "1" ]; then
        echo "云端安全列表已自动放行（80/443/9527），本机防火墙也已放行。"
    else
        echo "如果浏览器打不开面板：检查云厂商的安全组/安全列表是否放行了"
        if [ -n "${DOMAIN:-}" ] && [ "${CERT_OK:-0}" = "1" ]; then
            echo "  ${CERT_PORT:-443} 端口（本机防火墙已放行，云上那层也要放行）。"
        else
            echo "  ${PANEL_LISTEN_PORT:-$PANEL_PORT} 端口（本机防火墙已放行，云上那层也要放行）。"
            if [ "${CERT_KIND:-selfsigned}" = "ip" ]; then
                echo "IP 证书续期要走 80 端口验证，云上安全组也要放行 80。"
            fi
        fi
    fi
    echo "管理命令：bash install.sh start|stop|restart|status|log|upgrade|auto-upgrade|setup-auto-upgrade|uninstall"
    echo "=================================================="
}

# ---------- 主流程 ----------
cmd_install() {
    require_root
    detect_os
    # Install deps before prompting: random password generation needs openssl, which minimal images may not preinstall. 先装依赖再提问：提问里的随机密码生成要用到 openssl，精简镜像上 openssl 未必预装。
    install_deps
    prompt_config
    install_code
    setup_venv
    write_env_file
    write_config_txt
    write_systemd_units
    if [ "$FRESH_INSTALL" = "1" ]; then
        # Order: open both firewall layers (local + cloud) before requesting certs — http-01 needs port 80 reachable from outside, and a blocked cloud layer fails issuance. 顺序：先放行两层防火墙（本机 + 云端），再申请证书——http-01 验证要 80 端口从外网能通，云上没放行会发证失败。
        setup_firewall
        open_cloud_ports
        setup_nginx
    elif [ ! -f "$NGINX_CONF" ]; then
        # Update mode with a lost nginx config: rebuild it from the env var domain (self-signed when none given). 更新模式但 nginx 配置丢了：按环境变量的域名重建（没给就自签）。
        DOMAIN="${MBOT_INSTALL_DOMAIN:-}"
        setup_nginx
    else
        info "nginx 配置已存在，跳过（想换域名请删 $NGINX_CONF 后重跑）。"
    fi
    health_check || warn "健康检查有告警，见上面日志。"
    setup_auto_upgrade
    if [ "$FRESH_INSTALL" = "1" ]; then
        self_test "$ADMIN_PASSWORD"
        print_access_info
    else
        echo
        echo "更新完成：代码已更新到 v$(installed_version)，服务已重启。"
        echo "查看状态：bash install.sh status"
    fi
}

# Auto-upgrade cron: daily at 04:00 run install.sh auto-upgrade; when already latest it only logs and does not restart services, and only upgrades on a new version. Use a temp copy to run it, so a git pull updating install.sh itself does not rewrite the running script. MBOT_INSTALL_NO_AUTO_UPGRADE non-empty removes the cron file and skips. 定时自动升级的 cron 设置：每天凌晨 4 点跑一次 install.sh auto-upgrade；已是最新版时只记日志不重启服务，有新版时才升级。用临时副本执行，避免 git pull 更新 install.sh 本身时正在运行的脚本被改写。MBOT_INSTALL_NO_AUTO_UPGRADE 非空时删掉 cron 文件并跳过。
setup_auto_upgrade() {
    if [ -n "${MBOT_INSTALL_NO_AUTO_UPGRADE:-}" ]; then
        rm -f "$UPGRADE_CRON"
        info "已跳过自动升级设置（MBOT_INSTALL_NO_AUTO_UPGRADE）。"
        return 0
    fi
    printf '%s root (cp %s/install.sh /tmp/mbot-install-run.sh && bash /tmp/mbot-install-run.sh auto-upgrade) >> %s 2>&1\n' \
        "$AUTO_UPGRADE_SCHEDULE" "$INSTALL_DIR" "$UPGRADE_LOG" > "$UPGRADE_CRON"
    chmod 644 "$UPGRADE_CRON"
    # 去重：root 本地 crontab 里旧的 mbot 自动升级任务会和新 cron.d 双份执行。
    # 只删同时含 auto-upgrade 与 mbot/install.sh 的行，其余内容不动；
    # crontab 为空或无 crontab 命令时不报错。
    # Only delete lines containing both auto-upgrade and mbot/install.sh; leave everything else alone. No error when crontab is empty or the crontab command is missing. 只删同时含 auto-upgrade 与 mbot/install.sh 的行，其余内容不动；crontab 为空或无 crontab 命令时不报错。
    if command -v crontab >/dev/null 2>&1; then
        local _cron_tmp
        _cron_tmp="$(mktemp)"
        if crontab -l > "$_cron_tmp" 2>/dev/null; then
            if grep -qE "auto-upgrade.*(mbot|install\.sh)|(mbot|install\.sh).*auto-upgrade" "$_cron_tmp"; then
                grep -vE "auto-upgrade.*(mbot|install\.sh)|(mbot|install\.sh).*auto-upgrade" "$_cron_tmp" > "${_cron_tmp}.new" || true
                crontab "${_cron_tmp}.new" 2>/dev/null || true
                rm -f "${_cron_tmp}.new"
            fi
        fi
        rm -f "$_cron_tmp"
    fi
    ok "定时自动升级已设置（每天凌晨 4 点，有新版时才升级）。"
}

# Bark notification on auto-upgrade failure (silently skipped when Bark is unconfigured or disabled). $1 = short failure reason. Any error in the notification itself is swallowed; it must never affect the upgrade flow. 自动升级失败时 Bark 通知（Bark 未配置或未开启时静默跳过）。$1 = 失败原因简述。通知本身的任何异常都吞掉，绝不能影响升级流程。
_notify_upgrade_failure() {
    local reason="$1"
    MBOT_TGBOT_DATA_DIR="$INSTALL_DIR/tgbot/data" \
        "$VENV_DIR/bin/python" - "$INSTALL_DIR" "$UPGRADE_LOG" "$reason" \
        2>/dev/null <<'PYEOF' || true
import socket
import sys
sys.path.insert(0, sys.argv[1])
try:
    from tgbot.bark import send_bark
    ok, err = send_bark(
        "M_Bot 自动升级失败",
        "主机 %s：%s，详见 %s。"
        % (socket.gethostname(), sys.argv[3], sys.argv[2]),
        group="upgrade")
    if not ok:
        print("Bark 通知未发出：%s" % err)
except Exception as exc:  # noqa: BLE001 - 通知失败不影响升级流程
    print("Bark 通知异常：%s" % exc)
PYEOF
}

# Check whether a newer nginx is available in apt (installed vs candidate version). Returns 0 when an update is available, 1 when already latest or unavailable. 检查 apt 里有无更新的 nginx（已装版 vs 候选版）：有新版返回 0，已是最新或查不到返回 1。
nginx_update_available() {
    local installed candidate
    installed=$(apt-cache policy nginx 2>/dev/null | awk '/^[[:space:]]*Installed:/ {print $2}')
    candidate=$(apt-cache policy nginx 2>/dev/null | awk '/^[[:space:]]*Candidate:/ {print $2}')
    [ -n "$candidate" ] && [ "$candidate" != "(none)" ] || return 1
    [ -z "$installed" ] || [ "$installed" = "(none)" ] && return 0
    [ "$installed" = "$candidate" ] && return 1
    dpkg --compare-versions "$candidate" gt "$installed" 2>/dev/null
}

# Upgrade nginx to the apt candidate version (config kept: --force-confold, same as install_deps), then validate and reload. Returns 0 on success, 1 on failure. 把 nginx 升到 apt 候选版（--force-confold 保留现有配置，与 install_deps 一致），校验通过后重载；成功返回 0，失败返回 1。
upgrade_nginx() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -qq -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        --only-upgrade nginx >/dev/null 2>&1 || return 1
    nginx -t >/dev/null 2>&1 || return 1
    systemctl reload nginx 2>/dev/null || systemctl restart nginx 2>/dev/null || return 1
    return 0
}

cmd_auto_upgrade() {
    # Scheduled auto-upgrade (invoked by cron): check mbot and nginx for new versions independently; when both are already latest, only log and touch nothing; upgrade only the ones with a new version; on failure log and send a Bark notification (when Bark is configured and enabled). 定时自动升级（cron 调用）：分别检查 mbot 与 nginx 有无新版；两者都已是最新时只记日志、不碰服务；只升有新版的那一个；失败时记日志并 Bark 通知（Bark 配好且已开启时）。
    require_root
    [ -d "$INSTALL_DIR/.git" ] || die "未安装（$INSTALL_DIR 不存在），先运行不带参数的安装。"
    cd "$INSTALL_DIR" || die "无法进入 $INSTALL_DIR。"
    info "自动升级检查开始（当前版本 v$(installed_version)，nginx $(nginx -v 2>&1 | head -n 1)）。"
    local mbot_update=0 nginx_update=0 failed=""
    if ! git fetch origin main --quiet 2>/dev/null; then
        warn "拉取远端失败，跳过本次 mbot 检查。"
        _notify_upgrade_failure "拉取远端失败"
    else
        local local_head remote_head
        local_head=$(git rev-parse HEAD)
        remote_head=$(git rev-parse origin/main)
        [ "$local_head" != "$remote_head" ] && mbot_update=1
    fi
    # Refresh apt lists (best effort: failure only skips the nginx check, never the mbot upgrade). 刷新 apt 列表（尽力而为：失败只跳过 nginx 检查，不影响 mbot 升级）。
    wait_for_apt
    local attempt=0
    until apt-get update -qq 2>/dev/null; do
        attempt=$((attempt + 1))
        [ "$attempt" -ge 3 ] && break
        warn "apt-get update 失败（可能锁被占），15 秒后重试…"
        sleep 15
        wait_for_apt
    done
    if [ "$attempt" -ge 3 ]; then
        warn "apt-get update 多次失败，跳过本次 nginx 检查。"
    elif nginx_update_available; then
        nginx_update=1
    fi
    if [ "$mbot_update" = "0" ] && [ "$nginx_update" = "0" ]; then
        info "mbot 与 nginx 都已是最新版，无需升级。"
        return 0
    fi
    if [ "$mbot_update" = "1" ]; then
        info "发现 mbot 新版，开始升级…"
        # Run the upgrade steps in a subshell: if update_code fails, die exits only the subshell and the outer layer can catch the failure and send a Bark notification; do not use local inside the subshell. 升级步骤放子 shell 里跑：update_code 失败时 die 只退出子 shell，外层能捕获失败并 Bark 通知；子 shell 里不要用 local。
        if (
            set -e
            update_code
            "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
            "$VENV_DIR/bin/pip" install --quiet oci-cli
            to_restart=$(running_services)
            if [ -n "$to_restart" ]; then
                # shellcheck disable=SC2086
                systemctl restart $to_restart
            fi
            health_check
        ); then
            ok "mbot 自动升级完成（版本 v$(installed_version)）。"
        else
            warn "mbot 自动升级失败，服务保持原状，见日志 $UPGRADE_LOG。"
            failed="mbot"
        fi
    fi
    if [ "$nginx_update" = "1" ]; then
        info "发现 nginx 新版，开始升级…"
        if upgrade_nginx; then
            ok "nginx 自动升级完成（版本 $(nginx -v 2>&1 | head -n 1)）。"
        else
            warn "nginx 自动升级失败，见日志 $UPGRADE_LOG。"
            failed="${failed:+$failed + }nginx"
        fi
    fi
    if [ -n "$failed" ]; then
        _notify_upgrade_failure "升级步骤失败（$failed）"
    else
        ok "自动升级完成。"
    fi
}

cmd_upgrade() {
    require_root
    [ -d "$INSTALL_DIR/.git" ] || die "未安装（$INSTALL_DIR 不存在），先运行不带参数的安装。"
    info "升级：拉最新代码 → 装依赖 → 重启服务…"
    update_code
    "$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    "$VENV_DIR/bin/pip" install --quiet oci-cli
    local to_restart
    to_restart=$(running_services)
    if [ -n "$to_restart" ]; then
        # shellcheck disable=SC2086
        systemctl restart $to_restart
    fi
    health_check || warn "健康检查有告警，见上面日志。"
    ok "升级完成（版本 v$(installed_version)）。"
}

cmd_uninstall() {
    require_root
    echo "即将卸载 M_Bot：停服务、删 systemd 单元、删 nginx 反代、删 $INSTALL_DIR。"
    echo "环境文件 $ENV_FILE、统一配置文件 $CONFIG_FILE 与密码文件 $ADMIN_PASSWORD_FILE 会保留（凭据不删）。"
    echo "证书续期 cron 与脚本、定时自动升级 cron 与日志会删掉；/etc/lego 里的证书与 CF Token 保留（重装还能用）。"
    printf "确认卸载？输入 yes 继续："
    read -r ans || true
    [ "$ans" = "yes" ] || { echo "已取消。"; exit 0; }
    systemctl stop "$WEB_SERVICE" "$BOT_SERVICE" 2>/dev/null || true
    systemctl disable "$WEB_SERVICE" "$BOT_SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${WEB_SERVICE}.service" \
        "/etc/systemd/system/${BOT_SERVICE}.service"
    rm -f "$RENEW_CRON" "$RENEW_SCRIPT"
    rm -f "$UPGRADE_CRON" "$UPGRADE_LOG"
    systemctl daemon-reload
    rm -f "$NGINX_CONF"
    nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
    rm -rf "$INSTALL_DIR"
    ok "已卸载。凭据仍在 $ENV_FILE 与 $CONFIG_FILE（想彻底删掉请手动 rm 它们）。"
}

# ---------- 入口 ----------
case "${1:-}" in
    "")         cmd_install ;;
    start)      systemctl start $(mbot_services) && echo "已启动。" ;;
    stop)       systemctl stop $(mbot_services) && echo "已停止。" ;;
    restart)    systemctl restart $(mbot_services) && echo "已重启。" ;;
    status)     systemctl status $(mbot_services) --no-pager ;;
    log)        journalctl $(mbot_services | sed 's/^/-u /;s/ / -u /g') -n 100 --no-pager ;;
    upgrade)    cmd_upgrade ;;
    auto-upgrade) cmd_auto_upgrade ;;
    setup-auto-upgrade) setup_auto_upgrade ;;
    uninstall)  cmd_uninstall ;;
    *)
        echo "用法：$0 [start|stop|restart|status|log|upgrade|auto-upgrade|setup-auto-upgrade|uninstall]"
        echo "不带参数：首次运行安装，再次运行更新并重启。"
        exit 1
        ;;
esac
