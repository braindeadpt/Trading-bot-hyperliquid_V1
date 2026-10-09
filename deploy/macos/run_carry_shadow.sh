#!/bin/bash
# Carry-shadow daemon — long-running (docs/PREREGISTER_CARRY_SHADOW.md).
# Own public-feed WS + REST pollers; hypothetical episodes only — zero orders,
# zero engine contact. Evidence lands in data/research/carry_shadow.db and its
# heartbeat is a pseudo-feed row on the /ops feed-silence panel.
set -euo pipefail
# lives under deploy/macos/ (not copied to root — single source of truth)
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/run_carry_shadow.py
