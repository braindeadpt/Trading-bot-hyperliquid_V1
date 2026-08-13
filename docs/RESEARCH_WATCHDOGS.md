# Research watchdogs — auto-rerun evidence gates

Two background watchdogs re-run research probes once enough out-of-sample data
accumulates, so a gate decision is never blocked on a human remembering to
relaunch a script. Both are **read-only evidence gates** — they run research
probes and write decision reports; nothing here trades or touches the OMS.

| Watchdog | Trigger | Probe re-run | Report | State (gitignored) |
|---|---|---|---|---|
| Top-trader bias screening | `top_trader_bias_samples` ≥ **20 datas** | `scripts/feature_screening_top_trader_bias.py --json-out` | `docs/TOP_TRADER_BIAS_RECHECK_RESULT.md` | `data/research/top_trader_bias_recheck_state.json` |
| Liquidation flush recheck | real feed (okx/bybit) ≥ **30 dias** | `scripts/liquidation_flush_shadow.py` | `docs/LIQUIDATION_FLUSH_RECHECK_RESULT.md` | `data/research/liquidation_flush_recheck_state.json` |

## Why these triggers

* **Bias screening** — `survives_strict` requires ≥20 datas for the date-block
  bootstrap; below that `p_boot` is undefined and FDR cannot reject. An early
  run is directional evidence only (see `docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`).
* **Flush recheck** — the v2 baseline (n=46, PF 2.35) was ~4 days of real feed;
  the decision rule says re-run at 30 days before promoting/killing
  (see `docs/LIQUIDATION_FLUSH_SHADOW_LIVE.md`).

## Running

```bash
# One-shot check (exits after the check; no-op until the trigger is met)
python scripts/top_trader_bias_recheck.py --once
python scripts/liquidation_flush_recheck.py --once

# Daemon mode (check every 6h, re-run the probe on the trigger, then watch-only)
nohup python -u scripts/top_trader_bias_recheck.py > logs/top_trader_bias_recheck.out 2>&1 &
nohup python -u scripts/liquidation_flush_recheck.py > logs/liquidation_flush_recheck.out 2>&1 &

# Smoke test / manual checkpoint (runs now without consuming the trigger)
python scripts/top_trader_bias_recheck.py --once --force
python scripts/liquidation_flush_recheck.py --once --force
```

Both are idempotent: the trigger is recorded in the state file, so a restart
never re-runs a completed gate. `--force` runs the comparison without marking
the trigger consumed.

## Verdict rules

* **Bias screening** — any candidate cell surviving the strict gate → `GATE
  PASS` (promote to preregister); none surviving at ≥20 datas → `GATE FAIL`
  (kill the signal); below 20 datas → `INCONCLUSIVE`.
* **Flush recheck** — n≥30 and avg≥+3bps and PF>1.2 → `CONFIRMED`; n≥30 and
  (avg<0 or PF<1) → `DEAD`; otherwise → `INCONCLUSIVE`.

## Dashboard

The “Research watchdogs” panel (`GET /api/research_watchdogs`) shows, for each
watchdog: current progress vs target (dates/days), sample count, whether the
trigger has fired, and the last run verdict. It reads the same DBs + state
files the watchdogs write, so the panel always reflects the live evidence
position. A broken DB/state degrades that one row, never the whole panel.

## Tests

* `tests/test_top_trader_bias_recheck.py` — pins the 20-date trigger, the
  verdict rules, the state round-trip, and the probe→recheck JSON contract.
* `tests/test_research_watchdog_status.py` — pins the dashboard payload shape
  and that both thresholds match the scripts they report on.
