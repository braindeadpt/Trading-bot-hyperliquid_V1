#!/usr/bin/env bash
# install.sh — one-time provisioning of the Hyperliquid bot on the Oracle A1
# VPS (Ubuntu 24.04 ARM64, shared with the Meridian bot).
#
# Run as a sudo-capable user:   sudo bash deploy/install.sh
#
# What it does:
#   1. Creates the `hyperliquid` system user and the /srv/hyperliquid layout.
#   2. Installs build tooling + uv, then Python 3.14 (Ubuntu ships 3.12).
#   3. Creates the venv and installs requirements.txt.
#      - Packages without cp314 aarch64 wheels (pandas/numpy/scipy and the
#        small aiohttp/flask C-exts) build from source — that's why the
#        toolchain step exists. See docs/VPS_MIGRATION.md §deps for the
#        audit and the fallback (pin bump vs Python 3.13).
#   4. Installs the systemd units + timers (enabled, NOT started — the bot
#      starts only after secrets and the migrated DB are in place).
#
# It does NOT: copy secrets, copy the research DB, or start hyperliquid-bot.
# Those are deliberate human steps in docs/VPS_MIGRATION.md.
set -euo pipefail

APP=/srv/hyperliquid/app
DATA=/srv/hyperliquid/data
BACKUPS=/srv/hyperliquid/backups
ENVDIR=/etc/hyperliquid
UNIT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd"
SVC_USER=hyperliquid

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash deploy/install.sh" >&2
    exit 1
fi

# ── 1. user + layout ──────────────────────────────────────────────────────
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /srv/hyperliquid --shell /usr/sbin/nologin "$SVC_USER"
fi
mkdir -p "$APP" "$DATA/research" "$DATA/l2_books" "$BACKUPS" \
         "$APP/data/live" "$APP/data/research" "$APP/logs" "$ENVDIR"
chown -R "$SVC_USER:$SVC_USER" /srv/hyperliquid

# ── 2. toolchain + uv + python 3.14 ───────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    build-essential pkg-config gfortran \
    libffi-dev libssl-dev libopenblas-dev \
    meson ninja-build

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv python install 3.14

# ── 3. venv + deps ────────────────────────────────────────────────────────
# The repo must already be at $APP (git clone done by the operator — see
# docs/VPS_MIGRATION.md §3). We never clone here on the user's behalf.
if [[ ! -f "$APP/requirements.txt" ]]; then
    echo "ERROR: $APP has no requirements.txt — clone the repo there first:" >&2
    echo "  sudo -u $SVC_USER git clone -b feat/vps <repo-url> $APP" >&2
    exit 1
fi
sudo -u "$SVC_USER" uv venv --python 3.14 "$APP/.venv"
# Report which pins will build from source (no aarch64 cp314 wheel) before
# the real install, so a scipy/pandas compile is a conscious event.
echo "== dependency resolution probe (source builds are listed) =="
sudo -u "$SVC_USER" uv pip install --python "$APP/.venv/bin/python" \
    --dry-run -r "$APP/requirements.txt" || true
sudo -u "$SVC_USER" uv pip install --python "$APP/.venv/bin/python" \
    -r "$APP/requirements.txt"

# ── 4. env file + systemd ─────────────────────────────────────────────────
if [[ ! -f "$ENVDIR/bot.env" ]]; then
    install -m 600 -o "$SVC_USER" -g "$SVC_USER" \
        "$(dirname "$UNIT_SRC")/bot.env.example" "$ENVDIR/bot.env"
    echo ">> $ENVDIR/bot.env created from example — fill in secrets via scp."
fi

install -m 644 "$UNIT_SRC"/*.service "$UNIT_SRC"/*.timer /etc/systemd/system/
systemctl daemon-reload

for t in backup overnight watchdogs shadow-eval jev wallet-fills; do
    systemctl enable "hyperliquid-$t.timer"
done
systemctl enable hyperliquid-bot.service hyperliquid-dashboard.service

cat <<'EOF'

== install done — services are ENABLED but not started ==

Next (docs/VPS_MIGRATION.md):
  1. scp secrets into /etc/hyperliquid/bot.env  (chmod 600, owner hyperliquid)
  2. scp data/vault.enc -> /srv/hyperliquid/app/data/vault.enc (chmod 600)
  3. Migrate the research DB per the runbook (bot stopped, sha256-verified)
  4. systemctl start hyperliquid-bot  (human decision — never automatic)
  5. systemctl start --all the six timers when feeds should resume
EOF
