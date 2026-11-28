#!/usr/bin/env bash
set -euo pipefail
LOG="/var/log/xray-auto-update.log"
XRAY_BIN="/usr/local/bin/xray"
[ -x "$XRAY_BIN" ] || XRAY_BIN="$(command -v xray)"
CUR_VER="$("$XRAY_BIN" version 2>/dev/null | awk '/^Xray / {print $2; exit}' | sed 's/^v//')"
LATEST_TAG="$(curl -fsSL "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=1" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\?\([^"]*\)".*/\1/p' | head -n1)"
echo "[$(date '+%F %T')] current=${CUR_VER:-none} latest=${LATEST_TAG:-unknown}" >> "$LOG"
if [ -n "$LATEST_TAG" ] && [ "$CUR_VER" != "$LATEST_TAG" ]; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >> "$LOG" 2>&1
  systemctl restart xray 2>/dev/null || true
  echo "[$(date '+%F %T')] upgraded to v${LATEST_TAG}" >> "$LOG"
else
  echo "[$(date '+%F %T')] already up to date" >> "$LOG"
fi
