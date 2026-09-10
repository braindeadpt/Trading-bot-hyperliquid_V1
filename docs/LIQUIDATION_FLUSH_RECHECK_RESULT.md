# Liquidation Flush Recheck — 30-day real-feed comparison

_Generated 2026-09-10T20:23:11+00:00 by `scripts/research/liquidation_flush_recheck.py`._

**Real feed span at trigger: 32.1 days (53270 okx/bybit events).**

## The cell under test

| Parameter | Value |
|---|---|
| Symbol | ETH |
| Threshold | p90 of dominant-minute notional (recomputed on this sample) |
| Direction | fade |
| Hold | 30 min |
| Stop-loss | none (no-op in v2) |

## Comparison

| Metric | v2 baseline (08-09..08-13) | recheck (30d) | delta |
|---|---|---|---|
| n | 46 | 290 | +244 |
| win rate | 50.0% | 50.7% | +0.7pp |
| profit factor | 2.353 | 1.487 | -0.866 |
| avg net | +6.98 bps | +1.98 bps | -5.00 bps |
| total net | +321 bps | +575 bps | +254 bps |

## Verdict

**INCONCLUSIVE — insufficient sample or marginal edge**

## Context

* Live/shadow evidence so far: n=47, WR 48.9%, PF 2.23, avg +6.37 bps (shadow-live backfill 08-09..08-13 (simulation parity), 7d paper run started 08-13).
* Simulation JSON: `C:\Users\Braindead\Documents\trading-bot-hyperliquid\data\backtests\liquidation_flush_shadow_v2_20260910_212310.json`.
* Caveats: okx/bybit feed, not Hyperliquid; 30 days still modest for regime diversity.
