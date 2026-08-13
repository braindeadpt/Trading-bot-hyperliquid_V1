# Research watchdogs — auto-rerun evidence gates (unified supervisor)

One background process (**`scripts/research_watchdog_supervisor.py`**) re-runs
research probes once enough out-of-sample data accumulates, so a gate decision
is never blocked on a human remembering to relaunch a script. Both gates are
**read-only evidence gates** — they run probes and write decision reports;
nothing here trades or touches the OMS.

| Gate | Trigger | Probe re-run | Report |
|---|---|---|---|
| Top-trader bias screening | `top_trader_bias_samples` ≥ **20 datas** | `scripts/feature_screening_top_trader_bias.py --json-out` | `docs/TOP_TRADER_BIAS_RECHECK_RESULT.md` |
| Liquidation flush recheck | real feed (okx/bybit) ≥ **30 dias** | `scripts/liquidation_flush_shadow.py` | `docs/LIQUIDATION_FLUSH_RECHECK_RESULT.md` |

Both gates share **one** gitignored state file
(`data/research/research_watchdogs_state.json`) with a per-gate subtree
(`top_trader_bias` / `liquidation_flush`, each `{triggered, runs}`), so a
restart never re-fires a completed gate and the dashboard reads a single
source of truth. On first run the supervisor migrates the legacy per-gate
files (`top_trader_bias_recheck_state.json` /
`liquidation_flush_recheck_state.json`) so an already-consumed trigger is
not re-fired after the upgrade.

The probe/verdict/report helpers live in the per-gate scripts
(`scripts/top_trader_bias_recheck.py`, `scripts/liquidation_flush_recheck.py`);
the supervisor imports them — no duplication, single source of truth.

## Why these triggers

* **Bias screening** — `survives_strict` requires ≥20 datas for the date-block
  bootstrap; below that `p_boot` is undefined and FDR cannot reject. An early
  run is directional evidence only (see `docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`).
* **Flush recheck** — the v2 baseline (n=46, PF 2.35) was ~4 days of real feed;
  the decision rule says re-run at 30 days before promoting/killing
  (see `docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md`).

## Running

```bash
# One-shot check of BOTH gates (exits after; no-op until a trigger is met)
python scripts/research_watchdog_supervisor.py --once

# Daemon mode (check both gates every 6h, re-run probes on triggers, then watch-only)
nohup python -u scripts/research_watchdog_supervisor.py > logs/research_watchdogs.out 2>&1 &

# Smoke test / manual checkpoint (runs both probes now without consuming triggers)
python scripts/research_watchdog_supervisor.py --force --once
```

The supervisor is idempotent: each trigger is recorded in the shared state
file, so a restart never re-runs a completed gate. `--force` runs the probes
without marking the triggers consumed. The two original per-gate scripts still
exist as the metric/report home and can still be run individually, but the
supervisor is the single recommended entry point.

## Verdict rules

* **Bias screening** — any candidate cell surviving the strict gate → `GATE
  PASS` (promote to preregister); none surviving at ≥20 datas → `GATE FAIL`
  (kill the signal); below 20 datas → `INCONCLUSIVE`.
* **Flush recheck** — n≥30 and avg≥+3bps and PF>1.2 → `CONFIRMED`; n≥30 and
  (avg<0 or PF<1) → `DEAD`; otherwise → `INCONCLUSIVE`.

## Dashboard

The “Research watchdogs” panel (`GET /api/research_watchdogs`) shows, for each
gate: current progress vs target (dates/days), sample count, whether the
trigger has fired, and the last run verdict. It reads the same DBs + the
shared state file the supervisor writes, so the panel always reflects the
live evidence position. A broken DB/state degrades that one row, never the
whole panel.

## Tests

* `tests/test_research_watchdog_supervisor.py` — shared-state round-trip,
  legacy migration, and both gate triggers firing exactly once (idempotent).
* `tests/test_top_trader_bias_recheck.py` / `tests/test_liquidation_flush_recheck.py`
  — the per-gate metric/verdict/report helpers the supervisor imports.
* `tests/test_research_watchdog_status.py` — dashboard payload shape and that
  both thresholds match the scripts they report on.
