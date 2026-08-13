# Research watchdogs — auto-rerun evidence gates (unified supervisor)

One background process (**`scripts/research_watchdog_supervisor.py`**) re-runs
research probes once enough out-of-sample data accumulates, so a gate decision
is never blocked on a human remembering to relaunch a script. All gates are
**read-only evidence gates** — they run probes and write decision reports;
nothing here trades or touches the OMS.

| Gate | Trigger | Probe re-run | Report |
|---|---|---|---|
| Top-trader bias screening | `top_trader_bias_samples` ≥ **20 datas** | `scripts/feature_screening_top_trader_bias.py --json-out` | `docs/TOP_TRADER_BIAS_RECHECK_RESULT.md` |
| Liquidation flush recheck | real feed (okx/bybit) ≥ **30 dias** | `scripts/liquidation_flush_shadow.py` | `docs/LIQUIDATION_FLUSH_RECHECK_RESULT.md` |
| IV gate shadow recheck | **≥ 30 closed trades** com decisão IV | `scripts/iv_gate_shadow_recheck.py` (join + slices via `scripts/iv_gate_shadow_vs_pnl.py`) | `docs/IV_GATE_SHADOW_RECHECK_RESULT.md` |

All gates share **one** gitignored state file
(`data/research/research_watchdogs_state.json`) with a per-gate subtree
(`top_trader_bias` / `liquidation_flush` / `iv_gate_shadow`, each
`{triggered, runs}`), so a restart never re-fires a completed gate and the
dashboard reads a single source of truth. On first run the supervisor migrates
the legacy per-gate files (`top_trader_bias_recheck_state.json` /
`liquidation_flush_recheck_state.json`) so an already-consumed trigger is
not re-fired after the upgrade.

The probe/verdict/report helpers live in the per-gate scripts
(`scripts/top_trader_bias_recheck.py`,
`scripts/liquidation_flush_recheck.py`,
`scripts/iv_gate_shadow_recheck.py`); the supervisor imports them — no
duplication, single source of truth. The IV gate trigger reuses the exact join
from `scripts/iv_gate_shadow_vs_pnl.py`, so the watchdog and its report can
never disagree about what counts as a matched IV decision.

## Why these triggers

* **Bias screening** — `survives_strict` requires ≥20 datas for the date-block
  bootstrap; below that `p_boot` is undefined and FDR cannot reject. An early
  run is directional evidence only (see `docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`).
* **Flush recheck** — the v2 baseline (n=46, PF 2.35) was ~4 days of real feed;
  the decision rule says re-run at 30 days before promoting/killing
  (see `docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md`).
* **IV gate shadow** — the IV gate is shadow-only (the router records the
  high/low-IV class per routed trade but never blocks); the backtest evidence
  (high_iv-only at threshold 66.7, +42.99 USD) was n=13, below the n≥30
  evidence gate. The watchdog re-runs the live comparison once ≥30 closed
  trades carry an IV decision and decides **shadow vs enforcement**
  (see `docs/IV_HIGH_ONLY_AB_SPLIT.md`).

## Running

```bash
# One-shot check of ALL gates (exits after; no-op until a trigger is met)
python scripts/research_watchdog_supervisor.py --once

# Daemon mode (check all gates every 6h, re-run probes on triggers, then watch-only)
nohup python -u scripts/research_watchdog_supervisor.py > logs/research_watchdogs.out 2>&1 &

# Smoke test / manual checkpoint (runs all probes now without consuming triggers)
python scripts/research_watchdog_supervisor.py --force --once
```

The supervisor is idempotent: each trigger is recorded in the shared state
file, so a restart never re-runs a completed gate. `--force` runs the probes
without marking the triggers consumed. The original per-gate scripts still
exist as the metric/report home and can still be run individually, but the
supervisor is the single recommended entry point.

## Verdict rules

* **Bias screening** — any candidate cell surviving the strict gate → `GATE
  PASS` (promote to preregister); none surviving at ≥20 datas → `GATE FAIL`
  (kill the signal); below 20 datas → `INCONCLUSIVE`.
* **Flush recheck** — n≥30 and avg≥+3bps and PF>1.2 → `CONFIRMED`; n≥30 and
  (avg<0 or PF<1) → `DEAD`; otherwise → `INCONCLUSIVE`.
* **IV gate shadow** — n≥30 closed with an IV decision and high_iv net>0 and
  low_iv net≤0 → `PROMOTE` (enforce the gate at threshold 66.7); otherwise →
  `REJECT` (sample contradicts the backtest direction — keep shadow, never
  silently flip the router); below n=30 → `INCONCLUSIVE`. The decision is a
  **recommendation + report only**: flipping the router from shadow to
  enforcement is a deliberate, reviewed change outside the watchdog's scope.

### PROMOTE alert (human-in-the-loop)

A `PROMOTE` verdict also fires an **alert** (Telegram/Discord, via the same
`AlertNotifier` the bot uses — config `alerts.*` / `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` / `DISCORD_WEBHOOK_URL`, resolved the same way as
`main.py`). The alert carries the **exact diff**: the high_iv/low_iv slice
numbers (net PnL + WR + n), the IV threshold (66.7) and the recheck report
path — everything an operator needs to review and flip the router from
shadow to enforcement. The watchdog never flips anything itself; the alert is
best-effort (a missing/disabled notifier logs and continues — it never blocks
the gate or the state write).

## Dashboard

The “Research watchdogs” panel (`GET /api/research_watchdogs`) shows, for each
gate: current progress vs target (dates/days/trades), sample count, whether
the trigger has fired, and the last run verdict. It reads the same DBs + the
shared state file the supervisor writes, so the panel always reflects the
live evidence position. A broken DB/state degrades that one row, never the
whole panel.

The IV gate row also shows the **projected decision** (`projected` in the
payload, rendered under the Status column): the direction the current slices
point to **before the n>=30 trigger fires** — `→ PROMOTE` or `→ REJECT` with
the high/low net PnL, flagged `(proj)` while provisional (n<30). The
projection reuses the exact same rule as the watchdog verdict
(`project_decision` in `scripts/iv_gate_shadow_recheck.py`, minus the
n-gate), so the panel and the watchdog can never disagree about the
direction — the operator watches the decision form instead of waiting for
the run.

## Tests

* `tests/test_research_watchdog_supervisor.py` — shared-state round-trip,
  legacy migration, and all gate triggers firing exactly once (idempotent).
* `tests/test_top_trader_bias_recheck.py` /
  `tests/test_liquidation_flush_recheck.py` /
  `tests/test_iv_gate_shadow_recheck.py` — the per-gate metric/verdict/report
  helpers the supervisor imports.
* `tests/test_research_watchdog_status.py` — dashboard payload shape and that
  all thresholds match the scripts they report on.
