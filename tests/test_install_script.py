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
        self.assertIn("certbot --nginx", self.text)

    def test_ufw_ssh_safeguard(self):
        # UFW 开启前必须先放行 SSH 端口（detect_ssh_port），不能把自己锁外面
        self.assertIn("detect_ssh_port", self.text)
        self.assertIn('ufw allow "$ssh_port/tcp"', self.text)
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
        allowed = {"127.0.0.1", "0.0.0.0"}
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

    def test_ufw_always_allows_panel_port(self):
        # 9527 必须始终放行，不能只在"没域名"分支里放：域名证书失败
        # 回落自签时面板走的就是 9527（首尔测试机实测）。
        ufw_section = self.text.split("setup_ufw()")[1].split(
            "# Oracle 云镜像")[0]
        self.assertIn('ufw allow "${PANEL_PORT}/tcp"', ufw_section)
        # 9527 的放行必须在"域名分支"之前（无条件），不能只在 else 里
        panel_pos = ufw_section.find('ufw allow "${PANEL_PORT}/tcp"')
        domain_pos = ufw_section.find('if [ -n "${DOMAIN:-}" ]')
        self.assertLess(panel_pos, domain_pos)
        self.assertNotIn("else\n        ufw allow", ufw_section)

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
        self.assertIn("write_nginx_ip_acme()", self.text)
        self.assertIn("write_nginx_lego_ip()", self.text)
        self.assertIn("location /.well-known/acme-challenge/ {", self.text)
        self.assertIn('ssl_certificate ${crt};', self.text)
        self.assertIn("LEGO_CERT_DIR", self.text)

    def test_ufw_always_allows_port_80(self):
        # 80 必须始终放行：IP 证书 http-01 验证（申请与续期）都要用，
        # 不能只在"有域名"分支里放。
        ufw_section = self.text.split("setup_ufw()")[1].split(
            "# Oracle 云镜像")[0]
        self.assertIn('ufw allow 80/tcp', ufw_section)
        port80_pos = ufw_section.find('ufw allow 80/tcp')
        domain_pos = ufw_section.find('if [ -n "${DOMAIN:-}" ]')
        self.assertLess(port80_pos, domain_pos)

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


if __name__ == "__main__":
    unittest.main()
