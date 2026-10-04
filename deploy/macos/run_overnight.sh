#!/bin/bash
# Overnight research suite — daily 05:00. pm2 cron_restart.
# macOS equivalent of Windows task 'Hyperliquid Overnight Research'.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/overnight_nightly.py >> logs/overnight_nightly_cron.log 2>&1
