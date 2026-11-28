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
        self.assertIn(
            "https://raw.githubusercontent.com/jimu2000/M_Bot/main/install.sh",
            self.text)

    def test_subcommands(self):
        for cmd in ("start", "stop", "restart", "status", "log",
                    "upgrade", "uninstall"):
            self.assertIn(f"    {cmd})", self.text,
                          f"缺管理子命令 {cmd}")

    def test_install_paths(self):
        self.assertIn('INSTALL_DIR="/opt/mbot"', self.text)
        self.assertIn('WEB_SERVICE="mbot-web"', self.text)
        self.assertIn('BOT_SERVICE="mbot-bot"', self.text)
        self.assertIn('ENV_FILE="$INSTALL_DIR/.mbot.env"', self.text)
        self.assertIn(
            'ADMIN_PASSWORD_FILE="$INSTALL_DIR/.mbot-admin-password"',
            self.text)
        self.assertNotIn("rbot-clone", self.text,
                         "新安装脚本不应引用旧部署路径 rbot-clone")

    def test_debian_ubuntu_only(self):
        self.assertIn("debian|ubuntu", self.text)

    def test_installs_oci_cli(self):
        self.assertIn("pip\" install --quiet oci-cli", self.text)
        self.assertIn("MBOT_OCI_CLI=$VENV_DIR/bin/oci", self.text)

    def test_env_file_permissions(self):
        self.assertIn('chmod 600 "$ENV_FILE"', self.text)
        self.assertIn('chmod 600 "$ADMIN_PASSWORD_FILE"', self.text)

    def test_nginx_reverse_proxy(self):
        self.assertIn("proxy_pass http://127.0.0.1:${INTERNAL_PORT};", self.text)
        self.assertIn('proxy_set_header Upgrade', self.text)
        self.assertIn("nginx -t", self.text)

    def test_nginx_security_hardening(self):
        for header in (
            "Strict-Transport-Security",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "Referrer-Policy",
            "Permissions-Policy",
        ):
            self.assertIn(header, self.text)
        self.assertIn("NGINX_SECURITY_HEADERS=", self.text)
        self.assertIn("server_tokens off", self.text)
        self.assertIn("map_hash_bucket_size 128", self.text)
        self.assertIn("map_hash_max_size 512", self.text)
        self.assertIn("map $http_cf_connecting_ip $clientRealIp", self.text)
        self.assertIn("proxy_set_header X-Real-IP \\$clientRealIp;", self.text)
        self.assertIn("map $http_user_agent $is_bad_bot", self.text)
        self.assertIn("nmap|sqlmap|nikto|zgrab|masscan|shodan|fofa", self.text)
        self.assertIn('if ($is_bad_bot) { return 403; }', self.text)
        self.assertNotIn('"~*python|libwww|java"', self.text)
        for var in ("NGINX_HTTP_MAPS", "NGINX_SECURITY_HEADERS",
                    "NGINX_BAD_BOT_GUARD"):
            self.assertGreaterEqual(self.text.count("${" + var + "}"), 3,
                                    f"{var} 未被三个 HTTPS vhost 引用")

    def test_selfsigned_and_certbot(self):
        self.assertIn("openssl req -x509", self.text)
        self.assertIn("certbot certonly --webroot", self.text)
        self.assertNotIn("python3-certbot-nginx", self.text)

    def test_firewall_ssh_safeguard(self):
        self.assertIn("detect_ssh_port", self.text)
        ports_fn = self.text.split("firewall_ports()")[1].split("}")[0]
        self.assertIn("detect_ssh_port", ports_fn)
        self.assertIn('ufw allow "$port/tcp"', self.text)
        self.assertIn("ufw --force enable", self.text)

    def test_apt_lock_wait(self):
        self.assertIn("wait_for_apt", self.text)
        self.assertIn("apt_locked", self.text)
        self.assertIn("until apt-get update -qq; do", self.text)
        self.assertIn("until apt-get install -y -qq", self.text)

    def test_reads_eof_safe(self):
        reads = re.findall(r"^\s*read -r.*$", self.text, re.M)
        self.assertGreater(len(reads), 0, "脚本里没有 read？")
        for line in reads:
            self.assertTrue(line.rstrip().endswith("|| true"),
                            f"read 缺 || true：{line.strip()}")

    def test_hash_generation_uses_pythonpath(self):
        self.assertIn('PYTHONPATH="$INSTALL_DIR"', self.text)

    def test_update_mode_keeps_config(self):
        self.assertIn('FRESH_INSTALL=0', self.text)
        self.assertIn('[ -f "$ENV_FILE" ] && { info "$(L "环境文件已存在，不覆盖。" "Env file already exists; not overwriting.")"; return 0; }',
                      self.text)

    def test_no_hardcoded_secrets(self):
        # No real Token / password / IP / OCID may appear in the script. 脚本里不允许出现真实 Token / 密码 / IP / OCID。
        self.assertNotRegex(self.text, r"\b\d{9,10}:[A-Za-z0-9_-]{20,}\b")
        self.assertNotRegex(self.text, r"ocid1\.[a-z]+\.oc1\.\.[a-z0-9]{10,}")
        ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", self.text)
        # 169.254.169.254 is the cloud metadata link-local address (a generic constant on OCI/AWS/GCP), not a real server IP; 1.1.1.1 / 8.8.8.8 are public DNS resolvers (used by the cfdns propagation check), also not real server IPs. 169.254.169.254 是云元数据链路本地地址（OCI/AWS/GCP 通用常量），不是真实服务器 IP；1.1.1.1 / 8.8.8.8 是公共 DNS 解析器（cfdns 传播检查用），也不是真实服务器 IP。
        allowed = {"127.0.0.1", "0.0.0.0", "169.254.169.254",
                   "1.1.1.1", "8.8.8.8"}
        self.assertEqual(set(ips) - allowed, set(),
                         f"脚本里出现非回环 IP：{set(ips) - allowed}")

    def test_panel_binds_loopback_only(self):
        self.assertIn("--host 127.0.0.1 --port ${INTERNAL_PORT}", self.text)

    def test_internal_port_differs_from_panel_port(self):
        m_panel = re.search(r"PANEL_PORT=(\d+)", self.text)
        m_internal = re.search(r"INTERNAL_PORT=(\d+)", self.text)
        self.assertIsNotNone(m_panel)
        self.assertIsNotNone(m_internal)
        self.assertNotEqual(m_panel.group(1), m_internal.group(1))
        self.assertIn("proxy_pass http://127.0.0.1:${INTERNAL_PORT};",
                      self.text)

    def test_shadow_reject_cleanup(self):
        self.assertIn("remove_shadow_reject 4", self.text)
        self.assertIn("remove_shadow_reject 6", self.text)
        self.assertIn('rules.bak.mbot', self.text)
        self.assertIn("^-A INPUT -j REJECT", self.text)

    def test_cert_fallback_reports_ip_url(self):
        self.assertIn("CERT_OK=1", self.text)
        self.assertIn("CERT_OK=0", self.text)
        self.assertIn('[ "${CERT_OK:-0}" = "1" ]', self.text)

    def test_firewall_always_allows_panel_port(self):
        # 防火墙放行面板实际监听的端口：443 空闲时走 443，被占时走 9527。
        # The firewall opens the panel's actual listen port: 443 when free,
        # else 9527.
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn("pick_panel_port", ports_fn)
        self.assertIn('"443"', ports_fn)
        self.assertIn("${PANEL_PORT}", ports_fn)
        self.assertIn('ports="$(panel_ports)"', self.text)

    def test_firewall_always_allows_port_80(self):
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn('local ports="80"', ports_fn)

    def test_panel_port_skipped_when_occupied(self):
        self.assertIn("panel_ports()", self.text)
        ports_fn = self.text.split("panel_ports()")[1].split(
            "firewall_ports()")[0]
        self.assertIn('grep ":${PANEL_PORT} "', ports_fn)
        self.assertIn("grep -qv nginx", ports_fn)
        self.assertIn('ports="$(panel_ports)"', self.text)
        nginx_fn = self.text.split("setup_nginx()")[1].split(
            "detect_ssh_port()")[0]
        self.assertIn("port_443_free", nginx_fn)
        self.assertIn("面板走 443，不影响", nginx_fn)

    def test_panel_prefers_443_when_free(self):
        # 面板优先走 443（标准 HTTPS 端口），被占时回落 9527：三个写配置的
        # 路径（自签、IP 证书、域名证书）都用同一个优先端口，防火墙与
        # 自检/访问信息也跟着走。
        # The panel prefers 443 (standard HTTPS) when free, else 9527: all
        # three config writers (self-signed, IP cert, domain cert) use the
        # same preferred port, and the firewall, self-test and access info
        # follow it.
        self.assertIn("port_443_free() {", self.text)
        self.assertIn("pick_panel_port() {", self.text)
        self.assertIn("echo 443", self.text)
        self.assertIn('PANEL_LISTEN_PORT="$(pick_panel_port)"', self.text)
        for fn in ("write_nginx_selfsigned() {",
                   "write_nginx_lego_ip() {",
                   "write_nginx_domain_https() {"):
            body = self.text.split(fn)[1].split("\n}\n")[0]
            self.assertIn("pick_panel_port", body,
                          "%s should use pick_panel_port" % fn)
        self.assertIn('"${PANEL_LISTEN_PORT:-$PANEL_PORT}"', self.text)
        # 自签与 IP 证书路径不再写死 9527。
        self.assertNotIn("listen ${PANEL_PORT} ssl;", self.text)

    def test_readme_quickstart(self):
        # README 按 R-Bot 摆法分开：中文 README.md + 英文 md/en/README.md，
        # 各有一条一键命令。中文 README.md (Chinese) and English md/en/README.md
        # each carry one one-liner.
        with open(README, encoding="utf-8") as f:
            readme = f.read()
        with open(os.path.join(ROOT, "md", "en", "README.md"),
                  encoding="utf-8") as f:
            readme_en = f.read()
        self.assertEqual(readme.count(ONELINER), 1,
                         "中文 README 应有一条一键命令")
        self.assertEqual(readme_en.count(ONELINER), 1,
                         "英文 README 应有一条一键命令")
        self.assertIn("## 快速开始\n", readme)
        self.assertIn("## Quick start\n", readme_en)
        self.assertIn("## 手动安装", readme)
        self.assertIn("## Manual install", readme_en)
    def test_lego_pinned_and_arch_aware(self):
        self.assertIn('LEGO_VERSION="v5.', self.text)
        self.assertIn("install_lego()", self.text)
        self.assertIn("github.com/go-acme/lego/releases/download", self.text)
        self.assertIn('x86_64|amd64) arch="amd64"', self.text)
        self.assertIn('aarch64|arm64) arch="arm64"', self.text)

    def test_ip_cert_shortlived_webroot(self):
        self.assertIn("request_ip_cert()", self.text)
        self.assertIn("--profile shortlived", self.text)
        self.assertIn("--http.webroot", self.text)
        self.assertIn("10.*|172.1[6-9].*", self.text)
        self.assertIn("192.168.*", self.text)
        self.assertIn("api.ipify.org", self.text)

    def test_cfdns_prompt_and_token(self):
        self.assertIn("MBOT_INSTALL_CERT_METHOD", self.text)
        self.assertIn("http|cfdns", self.text)
        self.assertIn("CF_DNS_API_TOKEN", self.text)
        self.assertIn("--dns cloudflare", self.text)
        self.assertIn("read -r -s CF_API_TOKEN", self.text)
        self.assertIn('chmod 600 "$LEGO_DIR/env"', self.text)

    def test_cert_method_auto_detect(self):
        # 证书验证方式自动检测，不再交互提问：有 CF 凭据 + 域名 -> cfdns
        # 自动申请；没凭据但有域名 -> http；没域名 -> IP 证书。
        # The cert method is auto-detected, no interactive prompt: CF
        # credentials + domain -> cfdns; domain without creds -> http;
        # no domain -> IP cert.
        self.assertNotIn('printf "域名证书验证方式', self.text,
                         "证书验证方式不应再交互提问，应自动检测")
        self.assertIn("Certificate method: auto-detect", self.text)
        self.assertIn('CERT_METHOD="cfdns"', self.text)
        self.assertIn("检测到 Cloudflare 凭据", self.text)
        # MBOT_INSTALL_CERT_METHOD 仍可显式指定（全新安装与更新模式）。
        self.assertGreaterEqual(
            self.text.count(
                'CERT_METHOD="${MBOT_INSTALL_CERT_METHOD:-http}"'),
            2, "全新安装与更新模式都要从 MBOT_INSTALL_CERT_METHOD 读")

    def test_cf_key_email_env_aliases(self):
        # acme.sh 风格的 CF_Key / CF_Email 环境变量也要认（等同于
        # CF_API_KEY / CF_EMAIL），否则 export 了 CF_Key/CF_Email 的
        # 用户还会被当成没凭据。
        fn = self.text.split("load_cf_credentials() {")[1].split(
            "\n}\n")[0]
        self.assertIn("${CF_Key:-}", fn)
        self.assertIn("${CF_Email:-}", fn)

    def test_cert_renewal_automation(self):
        self.assertIn("write_renew_script()", self.text)
        self.assertIn("0 3 */2 * * root", self.text)
        self.assertIn("deploy-hook.sh", self.text)
        self.assertIn("systemctl reload nginx", self.text)

    def test_ip_nginx_vhost(self):
        self.assertIn("write_nginx_acme_vhost()", self.text)
        self.assertIn("write_nginx_lego_ip()", self.text)
        self.assertIn("location /.well-known/acme-challenge/ {", self.text)
        self.assertIn('ssl_certificate ${crt};', self.text)
        self.assertIn("LEGO_CERT_DIR", self.text)

    def test_domain_443_fallback_to_panel_port(self):
        self.assertIn("CERT_PORT", self.text)
        self.assertIn("write_nginx_lego_domain()", self.text)
        self.assertIn('https://${DOMAIN}:${CERT_PORT}/login', self.text)

    def test_health_check_nginx_aware(self):
        self.assertIn('ss -ltnp 2>/dev/null | grep nginx | grep -qE ":(${PANEL_PORT}|443) "',
                      self.text)

    def test_nginx_official_repo(self):
        self.assertIn("setup_nginx_repo()", self.text)
        self.assertIn("https://nginx.org/keys/nginx_signing.key", self.text)
        self.assertIn("nginx.org/packages/", self.text)
        self.assertIn("nginx.org/packages/mainline/", self.text)
        self.assertIn("lsb_release", self.text)
        self.assertIn("apt-transport-https", self.text)
        self.assertIn("nginx-archive-keyring.gpg", self.text)
        self.assertIn("Pin-Priority: 900", self.text)
        self.assertIn("setup_nginx_repo || true", self.text)
        self.assertIn("nginx 用发行版自带版本", self.text)

    def test_nginx_install_preserves_config(self):
        self.assertIn("--force-confold", self.text)

    def test_nginx_version_logged(self):
        deps = self.text.split("install_deps()")[1].split(
            "# ----------")[0]
        self.assertIn("nginx -v", deps)

    def test_cfdns_reads_oci_config(self):
        self.assertIn("read_cf_from_oci_config()", self.text)
        self.assertIn("$HOME/oci_config", self.text)
        self.assertIn('s.lower() == "cloudflare"', self.text)
        self.assertIn("cf_account_key", self.text)
        self.assertIn("CF_API_KEY=%s", self.text)
        self.assertIn("CF_API_EMAIL=%s", self.text)
        self.assertIn("MBOT_INSTALL_CF_API_KEY", self.text)

    def test_firewall_detection(self):
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
        self.assertIn("firewall-cmd --permanent --add-port=", self.text)
        self.assertIn("nft insert rule", self.text)
        self.assertIn('-I INPUT -p tcp --dport "$port" -j ACCEPT',
                      self.text)
        self.assertIn("重启后会丢", self.text)

    def test_oci_cloud_ports(self):
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
        self.assertIn("_oci_rules_with_ports()", self.text)
        self.assertIn('r.get("protocol") == "all"', self.text)
        self.assertIn("sys.exit(2)", self.text)
        self.assertIn('rule["direction"] = "INGRESS"', self.text)
        self.assertIn("if v is not None", self.text)

    def test_bot_token_optional(self):
        self.assertIn("留空则只装面板", self.text)
        self.assertIn("BOT_ENABLED=0", self.text)
        self.assertIn("BOT_ENABLED=1", self.text)
        self.assertIn("mbot_services()", self.text)
        self.assertIn('systemctl restart $(mbot_services)', self.text)
        self.assertNotIn('die "Token 不能为空', self.text)

    def test_certbot_webroot_443_fallback(self):
        self.assertIn("write_nginx_domain_https()", self.text)
        self.assertIn("certbot certonly --webroot", self.text)
        self.assertIn("renewal-hooks/deploy/mbot-reload-nginx.sh", self.text)
        self.assertIn("acme_on_80", self.text)
        self.assertIn('if [ "$acme_on_80" = "1" ]', self.text)

    def test_load_cf_credentials(self):
        self.assertIn("load_cf_credentials()", self.text)
        loader = self.text.split("load_cf_credentials()")[1].split(
            "cf_api()")[0]
        self.assertIn("MBOT_INSTALL_CF_API_TOKEN", loader)
        self.assertIn("read_cf_from_oci_config", loader)
        self.assertIn('return 1', loader)
        self.assertNotIn("printf ", loader)

    def test_dns_auto_bind(self):
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
        self.assertLess(nginx_fn.find("ensure_dns_points_here"),
                        nginx_fn.find("request_domain_cert_cfdns"))

    def test_self_test(self):
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
        install_fn = self.text.split("cmd_install()")[1].split(
            "cmd_upgrade()")[0]
        self.assertIn('self_test "$ADMIN_PASSWORD"', install_fn)
        self.assertLess(install_fn.find("health_check"),
                        install_fn.find("self_test"))

    def test_update_code_fallback_and_fail_loud(self):
        self.assertIn("update_code() {", self.text)
        update_fn = self.text.split("update_code() {")[1].split(
            "\n}\n")[0]
        self.assertIn("git pull --ff-only", update_fn)
        self.assertNotIn("2>/dev/null", update_fn)
        self.assertIn("git fetch origin main", update_fn)
        self.assertIn("git reset --hard origin/main", update_fn)
        self.assertIn("die \"$(L \"代码更新失败", update_fn)
        self.assertNotIn("用现有代码继续", self.text)

    def test_install_and_upgrade_use_update_code(self):
        install_fn = self.text.split("install_code() {")[1].split(
            "\n}\n")[0]
        self.assertIn("update_code", install_fn)
        upgrade_fn = self.text.split("cmd_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("update_code", upgrade_fn)

    def test_version_reported_after_install(self):
        self.assertIn("installed_version() {", self.text)
        self.assertIn('ok "$(L "代码已就绪（版本 v$(installed_version)）。" "Code ready (version v$(installed_version)).")"',
                      self.text)
        self.assertIn('ok "$(L "升级完成（版本 v$(installed_version)）。" "Upgrade complete (version v$(installed_version)).")"',
                      self.text)
        self.assertIn("代码已更新到 v$(installed_version)", self.text)

    def test_keyring_write_noninteractive(self):
        self.assertIn("gpg --batch --yes --dearmor -o "
                      "/usr/share/keyrings/nginx-archive-keyring.gpg",
                      self.text)
        self.assertNotIn("gpg --dearmor -o ", self.text)

    def test_bot_enabled_grep_is_bre_safe(self):
        self.assertNotIn("MBOT_TELEGRAM_TOKEN=.+", self.text)
        self.assertIn('grep -q "^MBOT_TELEGRAM_TOKEN=."', self.text)

    def test_systemd_units_restart_services(self):
        fn = self.text.split("write_systemd_units() {")[1].split(
            "\n}\n")[0]
        self.assertNotIn("enable --now", fn)
        self.assertIn('systemctl restart "$WEB_SERVICE" "$BOT_SERVICE"',
                      fn)
        self.assertIn('systemctl restart "$WEB_SERVICE"', fn)

    def test_update_mode_keeps_bot_when_unit_exists(self):
        fn = self.text.split("write_systemd_units() {")[1].split(
            "\n}\n")[0]
        self.assertIn('! [ -f "$ENV_FILE" ]', fn)

    def test_upgrade_restarts_only_running_or_enabled(self):
        self.assertIn("running_services() {", self.text)
        fn = self.text.split("running_services() {")[1].split(
            "\n}\n")[0]
        self.assertIn('systemctl is-active -q "$svc"', fn)
        self.assertIn('systemctl is-enabled -q "$svc"', fn)
        up = self.text.split("cmd_upgrade() {")[1].split("\n}\n")[0]
        self.assertIn("$(running_services)", up)
        self.assertNotIn("$(mbot_services)", up)


    def test_auto_upgrade_cron_setup(self):
        fn = self.text.split("setup_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn('"$AUTO_UPGRADE_SCHEDULE"', fn)
        self.assertIn('"$UPGRADE_CRON"', fn)
        self.assertIn('"$UPGRADE_LOG"', fn)
        self.assertIn("cp %s/install.sh /tmp/mbot-install-run.sh", fn)
        self.assertIn("bash /tmp/mbot-install-run.sh auto-upgrade", fn)
        self.assertIn('chmod 644 "$UPGRADE_CRON"', fn)
        self.assertIn('AUTO_UPGRADE_SCHEDULE="0 4 * * *"', self.text)
        self.assertIn('UPGRADE_CRON="/etc/cron.d/mbot-auto-upgrade"',
                      self.text)
        self.assertIn('UPGRADE_LOG="/var/log/mbot-upgrade.log"',
                      self.text)

    def test_auto_upgrade_opt_out(self):
        fn = self.text.split("setup_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("MBOT_INSTALL_NO_AUTO_UPGRADE", fn)
        self.assertIn('rm -f "$UPGRADE_CRON"', fn)

    def test_auto_upgrade_dedups_local_crontab(self):
        fn = self.text.split("setup_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("command -v crontab", fn)
        self.assertIn("crontab -l", fn)
        self.assertIn("grep -vE", fn)
        self.assertIn("auto-upgrade", fn)
        self.assertIn("mbot|install", fn)
        self.assertIn('crontab "${_cron_tmp}.new"', fn)

    def test_auto_upgrade_skips_when_current(self):
        fn = self.text.split("cmd_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("git fetch origin main --quiet", fn)
        self.assertIn("git rev-parse HEAD", fn)
        self.assertIn("git rev-parse origin/main", fn)
        self.assertIn("无需升级", fn)
        self.assertIn("return 0", fn)

    def test_auto_upgrade_restarts_only_running_services(self):
        fn = self.text.split("cmd_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("$(running_services)", fn)
        self.assertNotIn("$(mbot_services)", fn)
        sub = fn.split("if (")[1].split("); then")[0]
        self.assertNotIn("local ", sub)

    def test_auto_upgrade_notifies_on_failure(self):
        fn = self.text.split("_notify_upgrade_failure() {")[1].split(
            "\n}\n")[0]
        self.assertIn("from tgbot.bark import send_bark", fn)
        self.assertIn("MBOT_TGBOT_DATA_DIR", fn)
        self.assertIn('group="upgrade"', fn)
        self.assertIn("2>/dev/null", fn)
        self.assertIn("|| true", fn)
        auto = self.text.split("cmd_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn('_notify_upgrade_failure "拉取远端失败"', auto)
        self.assertIn('_notify_upgrade_failure "升级步骤失败（$failed）"',
                      auto)

    def test_auto_upgrade_checks_nginx(self):
        auto = self.text.split("cmd_auto_upgrade() {")[1].split(
            "\n}\n")[0]
        self.assertIn("apt-get update", auto)
        self.assertIn("nginx_update_available", auto)
        self.assertIn("upgrade_nginx", auto)
        self.assertIn("都已是最新版，无需升级", auto)
        avail = self.text.split("nginx_update_available() {")[1].split(
            "\n}\n")[0]
        self.assertIn("apt-cache policy nginx", avail)
        self.assertIn("Installed:", avail)
        self.assertIn("Candidate:", avail)
        self.assertIn("dpkg --compare-versions", avail)
        up = self.text.split("upgrade_nginx() {")[1].split("\n}\n")[0]
        self.assertIn("--only-upgrade nginx", up)
        self.assertIn("--force-confold", up)
        self.assertIn("nginx -t", up)
        self.assertIn("systemctl reload nginx", up)

    def test_install_sets_up_auto_upgrade(self):
        fn = self.text.split("cmd_install() {")[1].split("\n}\n")[0]
        self.assertIn("setup_auto_upgrade", fn)

    def test_uninstall_removes_upgrade_cron(self):
        fn = self.text.split("cmd_uninstall() {")[1].split("\n}\n")[0]
        self.assertIn('rm -f "$UPGRADE_CRON" "$UPGRADE_LOG"', fn)

    def test_health_check_skips_disabled_bot(self):
        fn = self.text.split("health_check() {")[1].split("\n}\n")[0]
        self.assertIn('systemctl is-enabled --quiet "$BOT_SERVICE"',
                      fn)
        self.assertIn("机器人服务已停用，跳过检查。", fn)

    def test_auto_upgrade_subcommands_dispatched(self):
        self.assertIn("auto-upgrade) cmd_auto_upgrade ;;", self.text)
        self.assertIn("setup-auto-upgrade) setup_auto_upgrade ;;",
                      self.text)

    def test_systemd_unit_sets_mbot_web_env_file(self):
        self.assertGreaterEqual(
            self.text.count('Environment="MBOT_WEB_ENV_FILE=${ENV_FILE}"'),
            2, "面板与机器人 systemd 单元都要设 MBOT_WEB_ENV_FILE")

    def test_unified_config_is_txt(self):
        """统一配置文件是纯文本 M_bot_config.conf，不再是 JSON。"""
        self.assertIn('CONFIG_FILE="$HOME/M_bot_config.conf"',
                      self.text)
        self.assertNotIn("M_bot_config.json", self.text)
        self.assertIn("write_config_txt() {", self.text)
        self.assertNotIn("write_config_json", self.text)
        self.assertIn("mbot_config.save_config", self.text)

    def test_systemd_unit_sets_mbot_config_file(self):
        self.assertGreaterEqual(
            self.text.count('Environment="MBOT_CONFIG_FILE=${CONFIG_FILE}"'),
            2, "面板与机器人 systemd 单元都要设 MBOT_CONFIG_FILE")

    def test_compartment_derived_from_tenancy(self):
        self.assertIn("read_tenancy_from_oci_config", self.text)
        self.assertNotIn(
            "MBOT_COMPARTMENT_DEFAULT=ocid1.tenancy.oc1..xxxxxxxx",
            self.text, "install.sh 不应再把 compartment 占位符写进 env")
        self.assertIn("MBOT_COMPARTMENT_DEFAULT=$tenancy", self.text)

    def test_cfdns_uses_public_resolvers_and_keeps_log(self):
        fn = self.text.split("request_domain_cert_cfdns() {")[1].split(
            "\n}\n")[0]
        self.assertIn("--dns.resolvers 1.1.1.1:53,8.8.8.8:53", fn)
        self.assertIn("lego-last.log", fn)
        self.assertIn("DNS 传播未完成", fn)
        self.assertIn("详见 $lego_log", fn)
        self.assertIn('>"$lego_log" 2>&1', fn)
        self.assertNotIn(">/dev/null 2>&1", fn,
                         "cfdns 的 lego 输出不应再丢进 /dev/null")

    def test_update_mode_reads_cert_method_env(self):
        self.assertGreaterEqual(
            self.text.count(
                'CERT_METHOD="${MBOT_INSTALL_CERT_METHOD:-http}"'),
            2, "全新安装与更新模式都要从 MBOT_INSTALL_CERT_METHOD 读")

    def test_nft_empty_table_falls_back_to_ufw(self):
        fn = self.text.split("_firewall_nft() {")[1].split("\n}\n")[0]
        self.assertIn("_firewall_ufw", fn)
        self.assertIn("回落到 UFW", fn)
        self.assertIn("ip6 filter", fn)

    # ---------- 语言选择（v2.25.5） ----------
    def test_language_selection_exists(self):
        # L() 翻译函数、MBOT_LANG 默认中文、choose_language 与保存文件都在；
        # install/upgrade/uninstall 三个入口都先选语言。
        self.assertIn('MBOT_LANG="${MBOT_LANG:-zh}"', self.text)
        self.assertIn('L() { if [ "${MBOT_LANG:-zh}" = "en" ]; then',
                      self.text)
        self.assertIn("choose_language() {", self.text)
        self.assertIn("MBOT_INSTALL_LANG", self.text)
        self.assertIn(".mbot-lang", self.text)
        for fn in ("cmd_install() {", "cmd_upgrade() {",
                   "cmd_uninstall() {"):
            body = self.text.split(fn)[1].split("\n}\n")[0]
            self.assertIn("choose_language", body, f"{fn} 应先选语言")

    def test_language_prompt_is_bilingual(self):
        # 提问本身必须是中英双语（提问前还没选语言），默认回车是中文。
        self.assertIn("请选择语言 / Select language:", self.text)
        self.assertIn("  1) 中文", self.text)
        self.assertIn("  2) English", self.text)
        self.assertIn("默认 1 中文 / Default 1 Chinese", self.text)

    def test_token_prompt_has_english(self):
        # MBOT_LANG=en 时 Token 提示是英文；默认（中文）时是中文。
        token_line = next(
            ln for ln in self.text.splitlines()
            if "Telegram 机器人 Token" in ln and "L \"" in ln)
        self.assertIn("Telegram bot Token (get it from @BotFather",
                      token_line)
        lang_block = self.text.split(
            "# ---------- 语言 / Language ----------")[1].split(
            "\ncommand_exists()")[0]
        script = ("set -u\n" + lang_block + "\n"
                  "MBOT_LANG=en\n" + token_line + "\n"
                  "MBOT_LANG=zh\n" + token_line + "\n")
        result = subprocess.run(["bash", "-c", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("Telegram bot Token (get it from @BotFather",
                      out, "英文模式下 Token 提示应是英文")
        self.assertIn("Telegram 机器人 Token", out, "中文模式下应是中文")

    def test_language_default_is_chinese(self):
        lang_block = self.text.split(
            "# ---------- 语言 / Language ----------")[1].split(
            "\ncommand_exists()")[0]
        script = ("set -u\n" + lang_block + "\n"
                  "unset MBOT_LANG\n"
                  "L \"中文\" \"English\"\n")
        result = subprocess.run(["bash", "-c", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "中文", "MBOT_LANG 未设时默认中文")

    def test_language_runtime_choices(self):
        # env 变量优先、已保存文件次之、其余静默默认中文（不提问、不报错）。
        lang_block = self.text.split(
            "# ---------- 语言 / Language ----------")[1].split(
            "\ncommand_exists()")[0]
        script = (
            "set -euo pipefail\n"
            "D=$(mktemp -d)\n"
            "INSTALL_DIR=\"$D\"; ENV_FILE=\"$D/.mbot.env\"\n"
            + lang_block + "\n"
            "MBOT_INSTALL_LANG=en; MBOT_LANG=zh; choose_language\n"
            "[ \"$MBOT_LANG\" = en ] || exit 11\n"
            "[ \"$(cat $D/.mbot-lang)\" = en ] || exit 12\n"
            "unset MBOT_INSTALL_LANG\n"
            "printf 'zh\\n' > $D/.mbot-lang\n"
            "MBOT_LANG=en; choose_language < /dev/null\n"
            "[ \"$MBOT_LANG\" = zh ] || exit 13\n"
            "rm -f $D/.mbot-lang\n"
            "MBOT_LANG=en; choose_language < /dev/null\n"
            "[ \"$MBOT_LANG\" = zh ] || exit 14\n"
            "touch $ENV_FILE\n"
            "MBOT_LANG=en; choose_language < /dev/null\n"
            "[ \"$MBOT_LANG\" = zh ] || exit 15\n"
            "rm -rf $D\n"
            "echo OK\n")
        result = subprocess.run(["bash", "-c", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"语言选择分支失败（exit {result.returncode}）"
                         f"：{result.stderr}")
        self.assertIn("OK", result.stdout)

    def test_output_all_bilingual(self):
        # 自查：info/ok/warn/die/echo/printf 的参数里不应有裸中文——
        # 中文只应出现在 L() 的第一个参数里，唯一的例外是语言选择提示本身。
        zh = re.compile(r"[\u4e00-\u9fff]")
        outpat = re.compile(r"\b(info|ok|warn|die|echo|printf)\b")
        prompt = ("请选择语言", "1) 中文", "默认 1 中文")
        bare = []
        for ln in self.text.splitlines():
            s = ln.strip()
            if s.startswith("#") or not zh.search(ln):
                continue
            if not outpat.search(ln):
                continue
            if 'L "' in ln:
                continue
            if any(p in ln for p in prompt):
                continue
            bare.append(ln.strip())
        self.assertEqual(bare, [], f"仍有裸中文输出行：{bare[:5]}")


if __name__ == "__main__":
    unittest.main()
