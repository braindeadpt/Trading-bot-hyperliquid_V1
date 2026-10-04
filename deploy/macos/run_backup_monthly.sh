#!/bin/bash
# Monthly verified research backup — day 1, 04:00. pm2 cron_restart.
# macOS equivalent of Windows task 'Hyperliquid Monthly Backup'.
# --backup-root is explicit: the script default (D:/hyperliquid_backup) is
# Windows-only and would otherwise resolve as a literal 'D:' dir under cwd.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs /Users/noder/hyperliquid_backup
exec ./.venv/bin/python -u -X utf8 scripts/ops/backup_research_data.py --tag monthly --backup-root /Users/noder/hyperliquid_backup >> logs/backup_monthly_out.log 2>> logs/backup_monthly_err.log
