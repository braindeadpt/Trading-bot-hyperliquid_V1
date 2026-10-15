#!/bin/bash
# Boros-shadow daemon — long-running (docs/PREREGISTER_BOROS_SPREAD.md).
# Hourly Boros markets + perp-L2 snapshots (:05 UTC) + hypothetical
# locked-spread entries; zero orders, zero API keys, zero engine contact.
# Evidence lands in data/research/boros_shadow.db.
set -euo pipefail
# lives under deploy/macos/ (not copied to root — single source of truth)
cd "$(dirname "$0")/../.."
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research
exec ./.venv/bin/python -u -X utf8 scripts/research/run_boros_shadow.py
