#!/bin/bash
# Unlock-shadow daemon — long-running (docs/PREREGISTER_UNLOCK_SHADOW.md).
# Daily PIT unlock-calendar snapshots + hypothetical beta-hedged short
# windows; zero orders, zero engine contact. Evidence lands in
# data/research/unlock_shadow.db; CRYPTORANK_API_KEY comes via .env.
set -euo pipefail
# lives under deploy/macos/ (not copied to root — single source of truth)
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/run_unlock_shadow.py
