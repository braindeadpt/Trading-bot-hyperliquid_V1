#!/bin/bash
# Monthly verified research backup — day 1, 04:00. pm2 cron_restart.
# macOS equivalent of Windows task 'Hyperliquid Monthly Backup'.
# --backup-root is explicit: the script default (D:/hyperliquid_backup) is
# Windows-only and would otherwise resolve as a literal 'D:' dir under cwd.
set -euo pipefail
cd "$(dirname "$0")/../.."

mkdir -p logs
# pm2 start/resurrect runs the script immediately — real work only inside the
# cron window (day 1, 04:00 local ±10min). FORCE_RUN=1 bypasses for manual
# runs. CRON_GUARD_NOW (HH:MM) / CRON_GUARD_DOM (DD) are test-only overrides.
if [ "${FORCE_RUN:-0}" != "1" ]; then
  _now="${CRON_GUARD_NOW:-$(date '+%H:%M')}"
  _dom="${CRON_GUARD_DOM:-$(date '+%d')}"
  case "$_dom:$_now" in
    01:03:5*|01:04:0[0-9]|01:04:10) ;;
    *) echo "[$(date '+%F %T')] skip: fora da janela cron (dom=$_dom now=$_now)" \
         >> logs/backup_monthly_out.log; exit 0 ;;
  esac
fi

set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p /Users/noder/hyperliquid_backup
exec ./.venv/bin/python -u -X utf8 scripts/ops/backup_research_data.py --tag monthly --backup-root /Users/noder/hyperliquid_backup >> logs/backup_monthly_out.log 2>> logs/backup_monthly_err.log
