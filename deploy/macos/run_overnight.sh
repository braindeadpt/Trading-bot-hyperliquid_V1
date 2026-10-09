#!/bin/bash
# Overnight research suite — daily 05:00. pm2 cron_restart.
# macOS equivalent of Windows task 'Hyperliquid Overnight Research'.
set -euo pipefail
cd "$(dirname "$0")/../.."

mkdir -p logs
# pm2 start/resurrect runs the script immediately — real work only inside the
# cron window (05:00-05:10 local). pm2's cron double-fires at ~:59:57 and
# :00:00 — the early fire skips here instead of being SIGINT'd mid-run
# (pm2.log evidence 2026-09-30..10-09, systematic on all 10 nights).
# FORCE_RUN=1 bypasses for manual runs.
# CRON_GUARD_NOW (HH:MM) overrides the clock for testing only.
if [ "${FORCE_RUN:-0}" != "1" ]; then
  _now="${CRON_GUARD_NOW:-$(date '+%H:%M')}"
  case "$_now" in
    05:0[0-9]|05:10) ;;
    *) echo "[$(date '+%F %T')] skip: fora da janela cron (now=$_now)" \
         >> logs/overnight_nightly_cron.log; exit 0 ;;
  esac
fi

set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/overnight_nightly.py >> logs/overnight_nightly_cron.log 2>&1
