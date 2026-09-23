#!/usr/bin/env bash
# install.sh — one-time provisioning of the Hyperliquid bot on the Oracle A1
# VPS (Ubuntu 24.04 ARM64, shared with the Meridian bot).
#
# Run as a sudo-capable user:   sudo bash deploy/install.sh
#
# What it does:
#   1. Creates the `hyperliquid` system user and the /srv/hyperliquid layout.
#   2. Installs a minimal toolchain + uv, then Python 3.13 (Ubuntu ships
#      3.12). 3.13 is chosen over 3.14 deliberately: every pinned dep —
#      including numpy 2.2.5 / pandas 2.2.3 / scipy 1.15.3 — ships a cp313
#      aarch64 wheel, so the install is all-binary (PyPI-verified
#      2026-09). On cp314 the scientific stack has NO wheels and would
#      compile for ~an hour on 2 ARM cores. See docs/VPS_MIGRATION.md §1a.
#   3. Creates the venv and installs requirements.txt. The toolchain is a
#      safety net only — if the dry-run probe below lists any sdist build,
#      STOP and report before continuing.
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
# Canonical user creation (hyperliquid + dev group wiring) lives in the
# machine runbook — Meridian repo docs/VPS-SETUP.md §1. This is a fallback
# so install.sh also works standalone: it never modifies an existing user.
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir /srv/hyperliquid --shell /usr/sbin/nologin "$SVC_USER"
fi
mkdir -p "$APP" "$DATA/research" "$DATA/l2_books" "$BACKUPS" \
         "$APP/data/live" "$APP/data/research" "$APP/logs" "$ENVDIR"
chown -R "$SVC_USER:$SVC_USER" /srv/hyperliquid

# ── 2. toolchain + uv + python 3.13 ───────────────────────────────────────
# Minimal safety-net toolchain only — every pinned dep has a cp313 aarch64
# wheel (§1a of the runbook). gfortran/openblas/meson/ninja were needed for
# a scipy source build on 3.14; on 3.13 nothing should compile. If a future
# dep change introduces a source build, reconsider this list then.
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    build-essential pkg-config libffi-dev libssl-dev

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv python install 3.13

# ── 3. venv + deps ────────────────────────────────────────────────────────
# The repo must already be at $APP (git clone done by the operator — see
# docs/VPS_MIGRATION.md §3). We never clone here on the user's behalf.
if [[ ! -f "$APP/requirements.txt" ]]; then
    echo "ERROR: $APP has no requirements.txt — clone the repo there first:" >&2
    echo "  sudo -u $SVC_USER git clone -b feat/vps <repo-url> $APP" >&2
    exit 1
fi
sudo -u "$SVC_USER" uv venv --python 3.13 "$APP/.venv"
# Tripwire: on cp313 aarch64 every pin should resolve to a wheel. If the
# dry-run lists an sdist/source build, report it before installing — a
# compile on 2 ARM cores is a conscious event, never a surprise.
echo "== dependency resolution probe (expect: zero source builds) =="
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
