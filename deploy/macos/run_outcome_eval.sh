#!/bin/bash
# Shadow outcome eval — persists 14-day scoreboards. pm2 cron_restart every 3h at :20.
# macOS equivalent of Windows task 'Hyperliquid Shadow Outcome Eval'.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/evaluate_shadow_outcomes.py --persist --since-days 14 >> logs/shadow_eval_cron.log 2>&1
