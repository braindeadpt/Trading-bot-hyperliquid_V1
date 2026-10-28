#!/bin/bash
# Wallet fills collector — one pass. Scheduled by pm2 cron_restart.
# macOS equivalent of Windows task 'Hyperliquid-Wallet-Fills-Collector'.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/wallet_fills_collector.py --once >> logs/wallet_fills_cron.log 2>&1
