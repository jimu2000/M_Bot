#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG="/var/log/nginx-auto-update.log"
CUR_VER="$(nginx -v 2>&1 | sed -n 's/.*nginx\/\([^[:space:]]*\).*/\1/p')"
CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
REPO_URL="http://nginx.org/packages/mainline/debian"
[ -f /etc/apt/sources.list.d/nginx.list ] || {
  curl -fsSL https://nginx.org/keys/nginx_signing.key | gpg --dearmor | tee /usr/share/keyrings/nginx-archive-keyring.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] ${REPO_URL} ${CODENAME} nginx" > /etc/apt/sources.list.d/nginx.list
  printf "Package: *\nPin: origin nginx.org\nPin: release o=nginx\nPin-Priority: 900\n" > /etc/apt/preferences.d/99nginx
}
apt-get -o DPkg::Lock::Timeout=120 update -qq
CANDIDATE_VER="$(apt-cache policy nginx 2>/dev/null | awk '/Candidate:/ {print $2; exit}' | sed 's/[-~].*//')"
echo "[$(date '+%F %T')] current=${CUR_VER:-none} candidate=${CANDIDATE_VER:-unknown}" >> "$LOG"
if [ -n "$CANDIDATE_VER" ] && [ "$CANDIDATE_VER" != "(none)" ] && [ "$CUR_VER" != "$CANDIDATE_VER" ]; then
  cp -r /etc/nginx "/etc/nginx.bak.$(date +%F_%H%M%S)"
  # 2026-11-28 Coraza：模块依赖 nginx-abi，单升 nginx 会把模块弄断，必须一起升
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=120 install -y --only-upgrade -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" nginx libnginx-mod-http-coraza libcoraza1 >> "$LOG" 2>&1
  nginx -t >> "$LOG" 2>&1
  systemctl restart nginx >> "$LOG" 2>&1
  echo "[$(date '+%F %T')] upgraded to ${CANDIDATE_VER}" >> "$LOG"
else
  echo "[$(date '+%F %T')] already up to date" >> "$LOG"
fi
