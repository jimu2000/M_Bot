#!/bin/bash
# DD 后安全加固脚本（DD 完成后在实例上跑，root 身份）。
# DD post-install security hardening (runs on the instance after DD, as root).
#
# 由 M_Bot 的 DD 初始化任务传到实例上执行（见 oci/dd_init.py 的 files
# 参数：流量看门狗、安全守护脚本、自动升级脚本、Bark 配置先传上去，
# 再跑本脚本）。本脚本只做“装包 + 写配置 + 起服务 + 配 cron”，
# 不改 SSH 端口、不关密码登录（DD 初始化本身走密码 SSH，关了会断）。
#
# 内容（按用户 2026-11-28 定）：
#   - BBR + TCP 优化：TCP 缓冲调大到 64MB，有 tcp_bbr 模块时切 BBR + fq
#     （与 install.sh 的 setup_bbr() 同款逻辑，另参考 bufanche tcp-tune.sh
#     的 tcp_notsent_lowat / tcp_mtu_probing / tcp_slow_start_after_idle 三项）。
#   - fail2ban：SSH 端口自动识别（跟随系统实际 SSH 端口，不写死 22/14422），
#     jail.local 用用户给的配置。
#   - unattended-upgrades：每日自动检查更新，内核更新后 04:00 自动重启，
#     自动删旧内核与无用依赖。
#   - 流量看门狗 oracle_traffic_ultimate.py：每 10 分钟巡检（文件由 DD 任务传上来）。
#   - 安全守护 vps_security_guard.sh：建基线 + 每 10 分钟检查 + 每周日 rkhunter。
#   - nginx / xray 自动升级脚本 + cron。
#   - 不关密码登录、不改 SSH 端口（这两步从一键安全脚本里去掉，DD 版不要）。
set -e
exec > >(tee -a /var/log/dd-security.log) 2>&1
echo "===== DD 安全加固开始 $(date '+%F %T') ====="

# ---------- SSH 端口自动识别 ----------
# SSH port auto-detection: the actual sshd listening port wins, then
# sshd_config, then 22. fail2ban 与流量看门狗都用这个端口，不写死。
detect_ssh_port() {
    local port=""
    # 1. Actual listening port. 实际监听端口。
    port=$(ss -tlnp 2>/dev/null | grep -oP '(?<=:)\d+(?=.*sshd)' | head -n1 || true)
    if [ -n "$port" ]; then echo "$port"; return; fi
    # 2. sshd -T (authoritative over the raw file). sshd -T（比直接读文件准）。
    port=$(sshd -T 2>/dev/null | grep -i '^port ' | head -n1 | awk '{print $2}' || true)
    if [ -n "$port" ]; then echo "$port"; return; fi
    # 3. Raw sshd_config. 直接读 sshd_config。
    port=$(grep -i '^Port ' /etc/ssh/sshd_config 2>/dev/null | head -n1 | awk '{print $2}' || true)
    if [ -n "$port" ]; then echo "$port"; return; fi
    echo "22"
}
SSH_PORT=$(detect_ssh_port)
echo "检测到 SSH 端口：$SSH_PORT"

# ---------- 装包 ----------
echo "--- 装包：vnstat / python3 / curl / fail2ban / unattended-upgrades / rkhunter ---"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y vnstat python3 curl fail2ban unattended-upgrades apt-listchanges powermgmt-base rkhunter
systemctl enable --now vnstat

# ---------- BBR + TCP 优化（与 install.sh 的 setup_bbr() 同款逻辑） ----------
# 另参考 bufanche tcp-tune.sh（https://bufanche.com/tcp-tune.sh，MIT）的三项：
# tcp_notsent_lowat=131072、tcp_mtu_probing=1、tcp_slow_start_after_idle=0。
# 全部写 /etc/sysctl.conf 与 /etc/modules 持久化；重复跑不重复写（幂等）。
echo "--- BBR + TCP 优化 ---"
persist_sysctl() {
    local key="$1" val="$2"
    local esc_key="${key//./\\.}"
    if grep -q "^${esc_key}=" /etc/sysctl.conf 2>/dev/null; then
        sed -i "s|^${esc_key}=.*|${key}=${val}|" /etc/sysctl.conf
    else
        echo "${key}=${val}" >> /etc/sysctl.conf
    fi
}
for kv in "net.core.rmem_max=67108864" "net.core.wmem_max=67108864" \
          "net.ipv4.tcp_rmem=4096 87380 67108864" "net.ipv4.tcp_wmem=4096 65536 67108864" \
          "net.core.netdev_max_backlog=250000" "net.ipv4.tcp_notsent_lowat=131072" \
          "net.ipv4.tcp_mtu_probing=1" "net.ipv4.tcp_slow_start_after_idle=0"; do
    key="${kv%%=*}"; val="${kv#*=}"
    sysctl -w "$key=$val" >/dev/null 2>&1 || true
    persist_sysctl "$key" "$val"
done
if modprobe tcp_bbr 2>/dev/null && sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw bbr; then
    sysctl -w net.core.default_qdisc=fq >/dev/null 2>&1 || true
    sysctl -w net.ipv4.tcp_congestion_control=bbr >/dev/null 2>&1 || true
    persist_sysctl "net.core.default_qdisc" "fq"
    persist_sysctl "net.ipv4.tcp_congestion_control" "bbr"
    grep -qx "tcp_bbr" /etc/modules 2>/dev/null || echo "tcp_bbr" >> /etc/modules
    echo "BBR 已开启（fq + bbr），TCP 缓冲已调大到 64MB。"
else
    echo "无 tcp_bbr 模块，保持 cubic（TCP 缓冲优化仍生效）。"
fi
echo "当前拥塞算法：$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo 未知)"

# ---------- fail2ban（SSH 端口跟随系统） ----------
echo "--- fail2ban：端口 $SSH_PORT ---"
cat > /etc/fail2ban/jail.local <<EOF
[DEFAULT]
ignoreip = 127.0.0.1/8
bantime = 60000
findtime = 600
maxretry = 1
backend = systemd
banaction = ufw
action = %(action_)s

[sshd]
enabled = true
port = $SSH_PORT
bantime = 864000
findtime = 60000
maxretry = 5
EOF
systemctl enable fail2ban
systemctl restart fail2ban
systemctl status fail2ban --no-pager | head -5 || true

# ---------- unattended-upgrades（每日自动更新，内核更新后 04:00 自动重启） ----------
echo "--- unattended-upgrades ---"
sed -i '/Unattended-Upgrade::Automatic-Reboot/d' /etc/apt/apt.conf.d/50unattended-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot "true";' >> /etc/apt/apt.conf.d/50unattended-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot-Time "04:00";' >> /etc/apt/apt.conf.d/50unattended-upgrades
sed -i 's/\/\/Unattended-Upgrade::Origins-Pattern/Unattended-Upgrade::Origins-Pattern/g' /etc/apt/apt.conf.d/50unattended-upgrades
echo 'Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";' >> /etc/apt/apt.conf.d/50unattended-upgrades
echo 'Unattended-Upgrade::Remove-Unused-Dependencies "true";' >> /etc/apt/apt.conf.d/50unattended-upgrades
systemctl restart unattended-upgrades

# ---------- 流量看门狗（文件由 DD 任务传上来） ----------
if [ -f /usr/local/bin/oracle_traffic_ultimate.py ]; then
    echo "--- 流量看门狗 ---"
    chmod +x /usr/local/bin/oracle_traffic_ultimate.py
    (crontab -l 2>/dev/null | grep -v "oracle_traffic_ultimate.py"; echo "*/10 * * * * /usr/bin/python3 /usr/local/bin/oracle_traffic_ultimate.py check") | crontab -
    python3 /usr/local/bin/oracle_traffic_ultimate.py test || true
else
    echo "流量看门狗文件未传上来，跳过"
fi

# ---------- 安全守护（文件由 DD 任务传上来） ----------
if [ -f /usr/local/bin/vps_security_guard.sh ]; then
    echo "--- 安全守护 ---"
    chmod +x /usr/local/bin/vps_security_guard.sh
    bash -n /usr/local/bin/vps_security_guard.sh
    /usr/local/bin/vps_security_guard.sh || true
    /usr/local/bin/vps_security_guard.sh --test || true
    rkhunter --update 2>&1 | tail -2 || true
    rkhunter --propupd 2>&1 | tail -2 || true
    (crontab -l 2>/dev/null | grep -v "vps_security_guard"; echo '*/10 * * * * /usr/local/bin/vps_security_guard.sh >> /var/log/vps_security_guard.log 2>&1'; echo '0 3 * * 0 /usr/local/bin/vps_security_guard.sh --rkhunter >> /var/log/vps_security_guard_rkhunter.log 2>&1') | crontab -
else
    echo "安全守护文件未传上来，跳过"
fi

# ---------- nginx / xray 自动升级（文件由 DD 任务传上来） ----------
for f in /usr/local/bin/nginx-auto-update.sh /usr/local/bin/xray-auto-update.sh; do
    [ -f "$f" ] && chmod 700 "$f" && bash -n "$f"
done
if [ -f /usr/local/bin/nginx-auto-update.sh ] || [ -f /usr/local/bin/xray-auto-update.sh ]; then
    (crontab -l 2>/dev/null | grep -v 'nginx-auto-update' | grep -v 'xray-auto-update'; echo '30 2 * * * /usr/local/bin/xray-auto-update.sh >> /var/log/xray-auto-update.log 2>&1'; echo '30 3 * * * /usr/local/bin/nginx-auto-update.sh >> /var/log/nginx-auto-update.log 2>&1') | crontab -
fi

echo ""
echo "🛡️ [全自动化完成]："
echo "1. BBR + TCP 优化：缓冲已调大到 64MB，拥塞算法 $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo 未知)"
echo "2. fail2ban：已启用，SSH 端口 $SSH_PORT（跟随系统），5 次失败封 10 天"
echo "3. 系统更新：每日自动检查，内核更新后 04:00 自动重启"
echo "4. 流量看门狗：每 10 分钟巡检，200GB 熔断，SSH 逃生门已保留"
echo "5. 安全守护：每 10 分钟检查，每周日 rkhunter"
echo "6. 磁盘清理：已开启自动删除旧内核与无用依赖"
echo "===== DD 安全加固完成 $(date '+%F %T') ====="
