#!/bin/bash
# Jev verdict feeder — one pass, then exit.
# Scheduled by pm2 (cron_restart "*/5 * * * *", autorestart false), the
# macOS equivalent of the Windows task Hyperliquid-Jev-Shadow.
# Asks Jev once/hour/symbol (internal cap), refreshes data/live/jev_latest.json
# which the JevJudge strategy reads. That file is a contracted feed
# (jev_verdicts) on the FeedSilenceMonitor — if this stops, the bot alerts.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/jev_shadow_judge.py --once
