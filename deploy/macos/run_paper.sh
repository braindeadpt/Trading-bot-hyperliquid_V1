#!/bin/bash
# Paper-mode launcher, supervised by pm2 (process name: hyperliquid).
#
# --skip-preflight was REMOVED 2026-09-29. It had been added to break a
# 160-restart crash-loop, but the real causes of that loop were stale 1h
# candles (since backfilled) and a dead jev_verdicts feed (now fed by the
# jev-judge pm2 cron job). Preflight passes again, so the guard is back on
# duty. Do not re-add the flag to silence a failure — read the report:
#   ./.venv/bin/python -X utf8 scripts/ops/preflight_feed_check.py
set -a; . ./.env; set +a
exec ./.venv/bin/python -u -X utf8 main.py --config config/settings.yaml --mode paper
