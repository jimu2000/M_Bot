#!/usr/bin/env python3
"""install.sh 一键安装脚本的静态测试（不执行安装，只做语法与内容检查）。

真实安装验证在首尔测试机上跑（见 docs/verification-phase5.md 的
v1.20.0 一节），这里只保证脚本进仓库时语法正确、关键行为都在、
没有硬编码的真实凭据。
"""

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install.sh")
README = os.path.join(ROOT, "README.md")

ONELINER = ("wget -O install.sh "
            "https://raw.githubusercontent.com/jimu2000/M_Bot/main/install.sh "
            "&& chmod +x install.sh && bash install.sh")


class TestInstallScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SCRIPT, encoding="utf-8") as f:
            cls.text = f.read()

    def test_bash_syntax_ok(self):
        result = subprocess.run(["bash", "-n", SCRIPT],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"bash -n 失败：{result.stderr}")

    def test_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK),
                        "install.sh 必须可执行（chmod +x）")

    def test_oneliner_url(self):
        # 一键命令用 raw.githubusercontent.com（仓库无 Releases，raw 今天就能用）
        self.assertIn(
            "https://raw.githubusercontent.com/jimu2000/M_Bot/main/install.sh",
            self.text)

    def test_subcommands(self):
        for cmd in ("start", "stop", "restart", "status", "log",
                    "upgrade", "uninstall"):
            self.assertIn(f"    {cmd})", self.text,
                          f"缺管理子命令 {cmd}")

    def test_install_paths(self):
        # 新安装用 mbot 命名（与旧部署的 rbot-clone 路径区分开）
        self.assertIn('INSTALL_DIR="/opt/mbot"', self.text)
        self.assertIn('WEB_SERVICE="mbot-web"', self.text)
        self.assertIn('BOT_SERVICE="mbot-bot"', self.text)
        self.assertIn('ENV_FILE="/root/.mbot.env"', self.text)
        self.assertIn('ADMIN_PASSWORD_FILE="/root/.mbot-admin-password"',
                      self.text)
        self.assertNotIn("rbot-clone", self.text,
                         "新安装脚本不应引用旧部署路径 rbot-clone")

    def test_debian_ubuntu_only(self):
        self.assertIn("debian|ubuntu", self.text)

    def test_installs_oci_cli(self):
        # requirements.txt 里没有 oci-cli，安装脚本必须单独装进 venv
        self.assertIn("pip\" install --quiet oci-cli", self.text)
        self.assertIn("MBOT_OCI_CLI=$VENV_DIR/bin/oci", self.text)

    def test_env_file_permissions(self):
        self.assertIn('chmod 600 "$ENV_FILE"', self.text)
        self.assertIn('chmod 600 "$ADMIN_PASSWORD_FILE"', self.text)

    def test_nginx_reverse_proxy(self):
        self.assertIn("proxy_pass http://127.0.0.1:${INTERNAL_PORT};", self.text)
        self.assertIn('proxy_set_header Upgrade', self.text)
        self.assertIn("nginx -t", self.text)

    def test_selfsigned_and_certbot(self):
        self.assertIn("openssl req -x509", self.text)
        # certbot 只发证不改 nginx 配置（certonly --webroot）：--nginx
        # 会在 443 被 xray 占了时重载失败；不用 python3-certbot-nginx。
        self.assertIn("certbot certonly --webroot", self.text)
        self.assertNotIn("python3-certbot-nginx", self.text)

    def test_firewall_ssh_safeguard(self):
        # 放行端口列表里必须含 SSH 端口（detect_ssh_port），UFW 开启前
        # 不能把自己锁外面；UFW 分支仍有 --force enable。
        self.assertIn("detect_ssh_port", self.text)
        ports_fn = self.text.split("firewall_ports()")[1].split("}")[0]
        self.assertIn("detect_ssh_port", ports_fn)
        self.assertIn('ufw allow "$port/tcp"', self.text)
        self.assertIn("ufw --force enable", self.text)

    def test_apt_lock_wait(self):
        # 新装系统首次启动 unattended-upgrades 占着 apt 锁，直接 apt-get
        # 会失败退出（首尔测试机实测）：必须等锁 + 重试，不能裸奔。
        self.assertIn("wait_for_apt", self.text)
        self.assertIn("apt_locked", self.text)
        self.assertIn("until apt-get update -qq; do", self.text)
        self.assertIn("until apt-get install -y -qq", self.text)

    def test_reads_eof_safe(self):
        # nohup / 非交互运行时 stdin 可能是 EOF，read 返回非零，
        # set -e 会直接杀掉脚本（首尔测试机实测）：每个 read 都要 || true。
        reads = re.findall(r"^\s*read -r.*$", self.text, re.M)
        self.assertGreater(len(reads), 0, "脚本里没有 read？")
        for line in reads:
            self.assertTrue(line.rstrip().endswith("|| true"),
                            f"read 缺 || true：{line.strip()}")

    def test_hash_generation_uses_pythonpath(self):
        # 密码哈希调 web.auth.hash_password：安装脚本的当前目录不一定
        # 是安装目录，必须用 PYTHONPATH 指到安装目录（首尔测试机实测）。
        self.assertIn('PYTHONPATH="$INSTALL_DIR"', self.text)

    def test_update_mode_keeps_config(self):
        # 已有环境文件时不重复提问、不覆盖（更新重启不丢配置）
        self.assertIn('FRESH_INSTALL=0', self.text)
        self.assertIn('[ -f "$ENV_FILE" ] && { info "环境文件已存在，不覆盖。"; return 0; }',
                      self.text)

    def test_no_hardcoded_secrets(self):
        # 脚本里不允许出现真实 Token / 密码 / IP / OCID
        self.assertNotRegex(self.text, r"\b\d{9,10}:[A-Za-z0-9_-]{20,}\b")
        self.assertNotRegex(self.text, r"ocid1\.[a-z]+\.oc1\.\.[a-z0-9]{10,}")
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.text)
        # 169.254.169.254 是云元数据链路本地地址（OCI/AWS/GCP 通用常量），
        # 不是真实服务器 IP。
        allowed = {"127.0.0.1", "0.0.0.0", "169.254.169.254"}
        self.assertEqual(set(ips) - allowed, set(),
                         f"脚本里出现非回环 IP：{set(ips) - allowed}")

    def test_panel_binds_loopback_only(self):
        self.assertIn("--host 127.0.0.1 --port ${INTERNAL_PORT}", self.text)

    def test_internal_port_differs_from_panel_port(self):
        # uvicorn 内部端口必须和 nginx 对外端口不同：nginx 绑
        # 0.0.0.0:9527 后，uvicorn 再绑 127.0.0.1:9527 会报
        # address already in use（首尔测试机实测）。
        m_panel = re.search(r"PANEL_PORT=(\d+)", self.text)
        m_internal = re.search(r"INTERNAL_PORT=(\d+)", self.text)
        self.assertIsNotNone(m_panel)
        self.assertIsNotNone(m_internal)
        self.assertNotEqual(m_panel.group(1), m_internal.group(1))
        self.assertIn("proxy_pass http://127.0.0.1:${INTERNAL_PORT};",
                      self.text)

    def test_shadow_reject_cleanup(self):
        # Oracle 云镜像自带的 iptables REJECT 挡在 UFW 链前面，
        # 会把面板端口挡住（外网 No route to host，本机正常；
        # 首尔测试机实测）：UFW 开启后必须清理它（实时链 + 持久文件）。
        self.assertIn("remove_shadow_reject 4", self.text)
        self.assertIn("remove_shadow_reject 6", self.text)
        self.assertIn('rules.bak.mbot', self.text)
        self.assertIn("^-A INPUT -j REJECT", self.text)

    def test_cert_fallback_reports_ip_url(self):
        # certbot 失败回落自签后，面板地址必须给 IP:9527 而不是域名
        # （域名不解析，给域名地址会误导用户；首尔测试机实测）。
        self.assertIn("CERT_OK=1", self.text)
        self.assertIn("CERT_OK=0", self.text)
        self.assertIn('[ "${CERT_OK:-0}" = "1" ]', self.text)

    def test_firewall_always_allows_panel_port(self):
        # 9527 默认放行，不能只在"没域名"分支里放：域名证书失败
        # 回落自签时面板走的就是 9527（首尔测试机实测）；9527 被
        # 别的服务占了时才跳过（反代绑不上，放行只会暴露别人的服务）。
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn("${PANEL_PORT}", ports_fn)
        # 9527 在无条件行里（只受"被占用"条件约束），443 只在域名分支里。
        panel_pos = ports_fn.find("${PANEL_PORT}")
        domain_pos = ports_fn.find('[ -n "${DOMAIN:-}" ]')
        self.assertLess(panel_pos, domain_pos)

    def test_firewall_always_allows_port_80(self):
        # 80 必须始终放行：IP 证书 http-01 验证（申请与续期）都要用，
        # 不能只在"有域名"分支里放。
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn('"80"', ports_fn)
        port80_pos = ports_fn.find('"80"')
        domain_pos = ports_fn.find('[ -n "${DOMAIN:-}" ]')
        self.assertLess(port80_pos, domain_pos)

    def test_panel_port_skipped_when_occupied(self):
        # 9527 被别的服务占了时：防火墙与云端都不放行它（反代绑不上，
        # 放行只会暴露别人的服务）；setup_nginx 里域名 + 443 空闲时
        # 只警告继续，否则才报错退出。
        self.assertIn("panel_ports()", self.text)
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn('grep ":${PANEL_PORT} "', ports_fn)
        self.assertIn("grep -qv nginx", ports_fn)
        self.assertIn('ports="$(panel_ports)"', self.text)
        nginx_fn = self.text.split("setup_nginx()")[1].split(
            "detect_ssh_port()")[0]
        self.assertIn("port443_free", nginx_fn)
        self.assertIn("面板走 443，不影响", nginx_fn)

    def test_readme_quickstart(self):
        with open(README, encoding="utf-8") as f:
            readme = f.read()
        # 中英两个快速开始章节都要有一键命令；旧的手动安装节改名保留
        self.assertEqual(readme.count(ONELINER), 2,
                         "README 中英两节应各有一条一键命令")
        self.assertIn("### 快速开始\n", readme)
        self.assertIn("### Quick start\n", readme)
        self.assertIn("### 手动安装", readme)
        self.assertIn("### Manual install", readme)
    def test_lego_pinned_and_arch_aware(self):
        # lego 版本钉死（升级前要在测试机上验命令行没变），
        # amd64/arm64 都要能下（OCI 免费机很多是 ARM）。
        self.assertIn('LEGO_VERSION="v5.', self.text)
        self.assertIn("install_lego()", self.text)
        self.assertIn("github.com/go-acme/lego/releases/download", self.text)
        self.assertIn('x86_64|amd64) arch="amd64"', self.text)
        self.assertIn('aarch64|arm64) arch="arm64"', self.text)

    def test_ip_cert_shortlived_webroot(self):
        # 无域名自动申请 IP 证书：必须走 shortlived（LE 只给 IP 发
        # 短效证），http-01 用 webroot（nginx 在跑，不能用 standalone
        # 抢 80 端口）；内网 IP 直接拒绝（LE 不给内网 IP 发证）。
        self.assertIn("request_ip_cert()", self.text)
        self.assertIn("--profile shortlived", self.text)
        self.assertIn("--http.webroot", self.text)
        self.assertIn("10.*|172.1[6-9].*", self.text)
        self.assertIn("192.168.*", self.text)
        self.assertIn("api.ipify.org", self.text)

    def test_cfdns_prompt_and_token(self):
        # 有域名时问验证方式（http/cfdns），cfdns 要 CF API Token，
        # 输入不回显，Token 只进 /etc/lego/env（600），不进命令行。
        self.assertIn("MBOT_INSTALL_CERT_METHOD", self.text)
        self.assertIn("http|cfdns", self.text)
        self.assertIn("CF_DNS_API_TOKEN", self.text)
        self.assertIn("--dns cloudflare", self.text)
        self.assertIn("read -r -s CF_API_TOKEN", self.text)
        self.assertIn('chmod 600 "$LEGO_DIR/env"', self.text)

    def test_cert_renewal_automation(self):
        # IP 证书只有 160 小时：续期脚本 + 每 2 天 cron；lego v5 里
        # run 兼续期；deploy hook 在证书更新后重载 nginx。
        self.assertIn("write_renew_script()", self.text)
        self.assertIn("0 3 */2 * * root", self.text)
        self.assertIn("deploy-hook.sh", self.text)
        self.assertIn("systemctl reload nginx", self.text)

    def test_ip_nginx_vhost(self):
        # IP 路径的 nginx 配置：80 端口只 serve 验证文件，面板走
        # 9527 ssl 且证书指 lego 的文件；80 vhost 要保留（续期还要用）。
        self.assertIn("write_nginx_acme_vhost()", self.text)
        self.assertIn("write_nginx_lego_ip()", self.text)
        self.assertIn("location /.well-known/acme-challenge/ {", self.text)
        self.assertIn('ssl_certificate ${crt};', self.text)
        self.assertIn("LEGO_CERT_DIR", self.text)

    def test_domain_443_fallback_to_panel_port(self):
        # cfdns 域名路径：443 被别的服务占了时面板改走 9527，
        # 地址栏也要带上端口，不能给裸域名地址误导用户。
        self.assertIn("CERT_PORT", self.text)
        self.assertIn("write_nginx_lego_domain()", self.text)
        self.assertIn('https://${DOMAIN}:${CERT_PORT}/login', self.text)

    def test_health_check_nginx_aware(self):
        # 健康检查不能只查"端口在听"：443 可能被 xray 占着，
        # 要查 nginx 进程占着 9527 或 443 才算反代起来了。
        self.assertIn('ss -ltnp 2>/dev/null | grep nginx | grep -qE ":(${PANEL_PORT}|443) "',
                      self.text)

    def test_nginx_official_repo(self):
        # nginx 必须装最新版：加 nginx.org 官方仓库（stable 分支），
        # 不是发行版自带旧版；仓库 URL 里发行版名按系统换，不能
        # 写死 debian；密钥放专用 keyring + signed-by；加不上时
        # 回落发行版版本，安装不中断。
        self.assertIn("setup_nginx_repo()", self.text)
        self.assertIn("https://nginx.org/keys/nginx_signing.key", self.text)
        self.assertIn("nginx.org/packages/", self.text)
        self.assertIn("lsb_release", self.text)
        self.assertIn("apt-transport-https", self.text)
        self.assertIn("nginx-archive-keyring.gpg", self.text)
        self.assertIn("Pin-Priority: 900", self.text)
        self.assertIn("setup_nginx_repo || true", self.text)
        self.assertIn("nginx 用发行版自带版本", self.text)

    def test_nginx_install_preserves_config(self):
        # 机器上已有 nginx 配置时不覆盖（--force-confold）。
        self.assertIn("--force-confold", self.text)

    def test_nginx_version_logged(self):
        # 装完把实际装上的 nginx 版本打出来，用户一眼确认是不是最新。
        deps = self.text.split("install_deps()")[1].split(
            "# ----------")[0]
        self.assertIn("nginx -v", deps)

    def test_cfdns_reads_oci_config(self):
        # cfdns 凭据先看 OCI 配置文件 [cloudflare] 段（cf_token，或
        # cf_key/cf_account_key + cf_email），有就直接用，不用再问；
        # Key + 邮箱也要写进 lego env（不止 Token）。
        self.assertIn("read_cf_from_oci_config()", self.text)
        self.assertIn("$HOME/oci_config", self.text)
        self.assertIn('s.lower() == "cloudflare"', self.text)
        self.assertIn("cf_account_key", self.text)
        self.assertIn("CF_API_KEY=%s", self.text)
        self.assertIn("CF_API_EMAIL=%s", self.text)
        self.assertIn("MBOT_INSTALL_CF_API_KEY", self.text)

    def test_firewall_detection(self):
        # 防火墙自动识别：ufw / firewalld / nft / iptables 四种都认，
        # UFW 必须在 nft 之前判（UFW 走 nft 后端，ruleset 里有它的链，
        # 先判 nft 会误判）；都没在跑时返回 none。
        self.assertIn("detect_firewall()", self.text)
        self.assertIn("setup_firewall()", self.text)
        detect_fn = self.text.split("detect_firewall()")[1].split(
            "firewall_ports()")[0]
        self.assertIn('echo "ufw"', detect_fn)
        self.assertIn('echo "firewalld"', detect_fn)
        self.assertIn('echo "nft"', detect_fn)
        self.assertIn('echo "iptables"', detect_fn)
        self.assertIn('echo "none"', detect_fn)
        self.assertLess(detect_fn.find('echo "ufw"'),
                        detect_fn.find('echo "nft"'))
        self.assertIn("_firewall_ufw", self.text)
        self.assertIn("_firewall_firewalld", self.text)
        self.assertIn("_firewall_nft", self.text)
        self.assertIn("_firewall_iptables", self.text)

    def test_firewall_backends(self):
        # 四种后端的放行命令都要有；nft/iptables 插链首（drop 前面），
        # 且要提醒重启会丢、建议换 ufw/firewalld。
        self.assertIn("firewall-cmd --permanent --add-port=", self.text)
        self.assertIn("nft insert rule", self.text)
        self.assertIn('-I INPUT -p tcp --dport "$port" -j ACCEPT',
                      self.text)
        self.assertIn("重启后会丢", self.text)

    def test_oci_cloud_ports(self):
        # OCI 云端安全列表经 API 自动放行：读实例元数据确认是 OCI，
        # 走 vnic → 子网 → 安全列表（或 NSG）；认证优先配置文件，
        # 没有时试实例主体；任何失败只警告 + 打印手动步骤，安装不中断
        # （open_cloud_ports 里不能有 die）。
        self.assertIn("open_cloud_ports()", self.text)
        self.assertIn("169.254.169.254/opc/v1/instance/", self.text)
        self.assertIn("169.254.169.254/opc/v1/vnics/", self.text)
        self.assertIn("network vnic get", self.text)
        self.assertIn("network subnet get", self.text)
        self.assertIn("network security-list update", self.text)
        self.assertIn("--force", self.text)
        self.assertIn("network nsg rules add", self.text)
        self.assertIn("--auth instance_principal", self.text)
        self.assertIn("手动在 OCI 控制台放行", self.text)
        cloud_fn = self.text.split("open_cloud_ports()")[1].split(
            "_oci_open_ports()")[0]
        self.assertNotIn("die ", cloud_fn)
        self.assertIn("CLOUD_OPENED=1", self.text)

    def test_oci_rules_builder(self):
        # 规则补齐逻辑：protocol=all 视为全放行；TCP 按端口范围判；
        # 全部已放行时退出 2 跳过 API 调用；NSG 模式只输出新增规则
        # 且带 direction 字段；GET 返回的 null 键要去掉再传给 update。
        self.assertIn("_oci_rules_with_ports()", self.text)
        self.assertIn('r.get("protocol") == "all"', self.text)
        self.assertIn("sys.exit(2)", self.text)
        self.assertIn('rule["direction"] = "INGRESS"', self.text)
        self.assertIn("if v is not None", self.text)

    def test_bot_token_optional(self):
        # Token 可留空：留空只装面板，不建机器人服务；更新模式下从
        # env 文件看有没有 Token 决定；管理命令只操作实际存在的服务。
        self.assertIn("留空则只装面板", self.text)
        self.assertIn("BOT_ENABLED=0", self.text)
        self.assertIn("BOT_ENABLED=1", self.text)
        self.assertIn("mbot_services()", self.text)
        self.assertIn('systemctl restart $(mbot_services)', self.text)
        self.assertNotIn('die "Token 不能为空', self.text)

    def test_certbot_webroot_443_fallback(self):
        # certbot 域名路径：certonly --webroot 发证 + 共用 HTTPS 写入
        # 函数（443 被占改走 9527）；续期要挂 deploy hook 重载 nginx；
        # 80 上留验证入口（续期走 webroot http-01）。
        self.assertIn("write_nginx_domain_https()", self.text)
        self.assertIn("certbot certonly --webroot", self.text)
        self.assertIn("renewal-hooks/deploy/mbot-reload-nginx.sh", self.text)
        self.assertIn("acme_on_80", self.text)
        self.assertIn('if [ "$acme_on_80" = "1" ]', self.text)

    def test_load_cf_credentials(self):
        # CF 凭据统一加载：环境变量 > OCI 配置 [cloudflare] 段；
        # Token 或 Key+邮箱齐全才算可用；绝不交互提问。
        self.assertIn("load_cf_credentials()", self.text)
        loader = self.text.split("load_cf_credentials()")[1].split(
            "cf_api()")[0]
        self.assertIn("MBOT_INSTALL_CF_API_TOKEN", loader)
        self.assertIn("read_cf_from_oci_config", loader)
        self.assertIn('return 1', loader)
        self.assertNotIn("printf ", loader)

    def test_dns_auto_bind(self):
        # 域名自动绑定：发证前校验解析，没解析到本机就用 CF API
        # 建/改 A 记录（DNS-only，不开代理），等解析生效；没凭据
        # 只警告不中断；setup_nginx 里发证前必须先调它。
        self.assertIn("ensure_dns_points_here()", self.text)
        self.assertIn("dns_points_here()", self.text)
        self.assertIn("cf_api()", self.text)
        self.assertIn('/zones?name=', self.text)
        self.assertIn("/dns_records", self.text)
        self.assertIn('\\"proxied\\":false', self.text)
        self.assertIn("解析已生效", self.text)
        nginx_fn = self.text.split("setup_nginx()")[1].split(
            "detect_ssh_port()")[0]
        self.assertIn('ensure_dns_points_here "$DOMAIN" || true', nginx_fn)
        # 绑定必须在发证之前
        self.assertLess(nginx_fn.find("ensure_dns_points_here"),
                        nginx_fn.find("request_domain_cert_cfdns"))

    def test_self_test(self):
        # 安装后自检：证书（颁发者/有效期/域名匹配）、登录页 200、
        # 真实登录（调登录接口 + 会话 cookie）、http→https 301；
        # 自签证书时 curl 用 -k，LE 证书严格校验。
        self.assertIn("self_test()", self.text)
        st = self.text.split("self_test()")[1].split(
            "print_access_info()")[0]
        self.assertIn("openssl s_client", st)
        self.assertIn("Let's Encrypt", st)
        self.assertIn("Certificate will not expire", st)
        self.assertIn("/api/auth/login", st)
        self.assertIn("mbot_session", st)
        self.assertIn("301\\ https://*", st)
        self.assertIn('k="-k"', st)
        self.assertIn("安装自检", st)

    def test_install_runs_self_test(self):
        # 首次安装跑完健康检查后必须跑自检（带管理员密码），
        # 更新模式不跑（密码/域名不在内存里）。
        install_fn = self.text.split("cmd_install()")[1].split(
            "cmd_upgrade()")[0]
        self.assertIn('self_test "$ADMIN_PASSWORD"', install_fn)
        self.assertLess(install_fn.find("health_check"),
                        install_fn.find("self_test"))

    def test_update_code_fallback_and_fail_loud(self):
        # v1.37.0：一键安装必须拿到最新版。update_code 先 git pull，
        # 失败时 fetch + reset 强制同步；两条路都失败就 die，
        # 绝不静默用旧代码继续（旧文案“用现有代码继续”已删）。
        self.assertIn("update_code() {", self.text)
        update_fn = self.text.split("update_code() {")[1].split(
            "\n}\n")[0]
        self.assertIn("git pull --ff-only", update_fn)
        self.assertNotIn("2>/dev/null", update_fn)
        self.assertIn("git fetch origin main", update_fn)
        self.assertIn("git reset --hard origin/main", update_fn)
        self.assertIn("die \"代码更新失败", update_fn)
        self.assertNotIn("用现有代码继续", self.text)

    def test_install_and_upgrade_use_update_code(self):
        # 安装（已有代码时）与 upgrade 子命令都走同一个 update_code。
        install_fn = self.text.split("install_code() {")[1].split(
            "\n}\n")[0]
        self.assertIn("update_code", install_fn)
        upgrade_fn = self.text.split("cmd_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("update_code", upgrade_fn)

    def test_version_reported_after_install(self):
        # 装完/升完打印版本号，用户能直接看到拿到的是哪个版本。
        self.assertIn("installed_version() {", self.text)
        self.assertIn('ok "代码已就绪（版本 v$(installed_version)）。"',
                      self.text)
        self.assertIn('ok "升级完成（版本 v$(installed_version)）。"',
                      self.text)
        self.assertIn("代码已更新到 v$(installed_version)", self.text)

    def test_keyring_write_noninteractive(self):
        # v1.37.0：密钥环文件已存在时 gpg --dearmor 会弹 Overwrite 提问，
        # 把无人值守的安装挂起；必须加 --batch --yes 直接覆盖。
        self.assertIn("gpg --batch --yes --dearmor -o "
                      "/usr/share/keyrings/nginx-archive-keyring.gpg",
                      self.text)
        self.assertNotIn("gpg --dearmor -o ", self.text)

    def test_bot_enabled_grep_is_bre_safe(self):
        # v1.37.1：grep 默认是 BRE，.+ 里的 + 是字面加号、永远匹配不到，
        # 曾导致重跑安装误判“没 Token”而删掉机器人服务。
        self.assertNotIn("MBOT_TELEGRAM_TOKEN=.+", self.text)
        self.assertIn('grep -q "^MBOT_TELEGRAM_TOKEN=."', self.text)

    def test_systemd_units_restart_services(self):
        # v1.37.1：更新模式下服务已在跑，enable --now 不重启，旧进程会
        # 一直跑旧代码；必须 enable + restart。
        fn = self.text.split("write_systemd_units() {")[1].split(
            "\n}\n")[0]
        self.assertNotIn("enable --now", fn)
        self.assertIn('systemctl restart "$WEB_SERVICE" "$BOT_SERVICE"',
                      fn)
        self.assertIn('systemctl restart "$WEB_SERVICE"', fn)

    def test_update_mode_keeps_bot_when_unit_exists(self):
        # v1.37.1：env 文件丢了但机器人单元还在时，保留机器人，不擅自拆掉。
        fn = self.text.split("write_systemd_units() {")[1].split(
            "\n}\n")[0]
        self.assertIn('! [ -f "$ENV_FILE" ]', fn)

    def test_upgrade_restarts_only_running_or_enabled(self):
        # v1.38.1：升级不要拉起被特意停掉并禁用的服务（如只用面板
        # 时禁用的机器人）；只重启在跑或已设自启的服务。
        self.assertIn("running_services() {", self.text)
        fn = self.text.split("running_services() {")[1].split(
            "\n}\n")[0]
        self.assertIn('systemctl is-active -q "$svc"', fn)
        self.assertIn('systemctl is-enabled -q "$svc"', fn)
        up = self.text.split("cmd_upgrade() {")[1].split("\n}\n")[0]
        self.assertIn("$(running_services)", up)
        self.assertNotIn("$(mbot_services)", up)


if __name__ == "__main__":
    unittest.main()
