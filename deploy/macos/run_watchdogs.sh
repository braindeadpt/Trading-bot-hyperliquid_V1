#!/bin/bash
# Research watchdog supervisor — one pass. pm2 cron_restart every 6h.
# macOS equivalent of Windows task 'Hyperliquid Research Watchdogs'.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/research_watchdog_supervisor.py --once >> logs/watchdog_supervisor_cron.log 2>&1
