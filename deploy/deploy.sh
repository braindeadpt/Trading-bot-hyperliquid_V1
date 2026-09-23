#!/usr/bin/env bash
# deploy.sh — update code on the VPS and restart services.
# HUMAN-INVOKED ONLY. Never run from CI, hooks, or timers: deploying is a
# decision, not a consequence of tests passing.
#
#   sudo bash deploy/deploy.sh [git-ref]     # default: origin/feat/vps
#
set -euo pipefail

APP=/srv/hyperliquid/app
SVC_USER=hyperliquid
REF="${1:-origin/feat/vps}"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/deploy.sh [ref]" >&2
    exit 1
fi
if [[ ! -d "$APP/.git" ]]; then
    echo "ERROR: $APP is not a git checkout" >&2
    exit 1
fi

echo "== fetching =="
sudo -u "$SVC_USER" git -C "$APP" fetch --prune origin
echo "== checking out $REF =="
sudo -u "$SVC_USER" git -C "$APP" checkout --detach "$REF"
echo "== syncing deps =="
sudo -u "$SVC_USER" uv pip install --python "$APP/.venv/bin/python" \
    -r "$APP/requirements.txt"

echo "== restarting services =="
systemctl restart hyperliquid-dashboard.service
systemctl restart hyperliquid-bot.service
systemctl daemon-reload

echo "== status =="
systemctl --no-pager --full status hyperliquid-bot.service | head -12
systemctl --no-pager --full status hyperliquid-dashboard.service | head -8
