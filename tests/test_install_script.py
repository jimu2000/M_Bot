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


if __name__ == "__main__":
    unittest.main()
