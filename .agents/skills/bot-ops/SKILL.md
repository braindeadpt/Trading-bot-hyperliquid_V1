---
name: bot-ops
description: Operate the bot — start/stop/status, instance lock, paper reset, watchdog supervisor, nightly runner, dashboard health. Windows-first.
triggers:
  - user
  - model
allowed-tools:
  - exec
  - read
  - grep
  - glob
---

# Bot operations runbook

Canonical references: `docs/INCIDENT_RUNBOOK.md`, `docs/RESEARCH_WATCHDOGS.md`,
`README.md` (run modes), `scripts/ops/reset_paper_session.py`.

## Lifecycle

```powershell
# Start paper (default). Do NOT start a second instance — bot.lock prevents
# double-boot but a stale second process can still double-emit.
python main.py --mode paper                     # foreground
python run_with_recovery.py --mode paper        # crash-recovery wrapper (24/7)
quickstart.bat                                  # paper + opens dashboard

# Stop — ALWAYS the project stopper, never blanket taskkill on python.exe
powershell -ExecutionPolicy Bypass -File scripts\stop_bot_instances.ps1 -ProjectRoot "C:\Users\Braindead\Documents\trading-bot-hyperliquid"

# Health
curl.exe http://localhost:5000/health           # status/circuit_breaker/ws_healthy
```

## Instance lock & zombie check

- Lock file: `data/live/bot.lock` — cleared by the stopper script.
- If the bot won't start, check for a stale lock or a second `main.py` process
  (`Get-Process python` + command line via CIM). Two instances = double
  execution risk even with the lock.

## Paper session reset

`python scripts/ops/reset_paper_session.py` (dry-run) / `--apply` — real
persistent reset of the paper session (positions, trades, equity anchor). This
was the fix for the 10% drawdown CB deadlock re-tripping every UTC day.
After `--apply`, the Fase-10 evidence window restarts — say so in reports.

## Watchdogs & nightly research

- Supervisor (all five read-only gates, one shared state file):
  `python scripts/research/research_watchdog_supervisor.py --once` /
  `--force` / daemon mode every 6h. State: `data/research/research_watchdogs_state.json`.
- Nightly runner: `python scripts/research/overnight_nightly.py` (queue-driven),
  `--dry-run`, `--heartbeat`. Status: `data/research/overnight_experiments/NIGHTLY_STATUS.json`.
- Both are READ-ONLY evidence gates — they never trade or touch the OMS.

## Dashboard

`http://localhost:5000` — Flask + Socket.IO. `/api/gate` shows the Fase-10 OOS
gate live. If auth is enabled, a dashboard token is required (`X-Dashboard-Token`
header or `?token=`).

## Never do

- `taskkill /F /IM python.exe` blanket — kills unrelated Python.
- Delete `data/live/bot.db` to "fix" state — use reset script or reconcile.
- Edit the live DB while the bot is running (WAL — still risky).
