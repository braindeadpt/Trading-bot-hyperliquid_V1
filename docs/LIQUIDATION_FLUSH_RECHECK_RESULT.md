# Liquidation Flush Recheck — 30-day real-feed comparison

_Generated 2026-09-11T00:05:12+00:00 by `scripts/research/liquidation_flush_recheck.py`._

**Real feed span at trigger: 32.3 days (54284 okx/bybit events).**

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
| n | 46 | 293 | +247 |
| win rate | 50.0% | 50.5% | +0.5pp |
| profit factor | 2.353 | 1.485 | -0.868 |
| avg net | +6.98 bps | +1.93 bps | -5.05 bps |
| total net | +321 bps | +566 bps | +245 bps |

## Verdict

**INCONCLUSIVE — insufficient sample or marginal edge**

## Context

* Live/shadow evidence so far: n=47, WR 48.9%, PF 2.23, avg +6.37 bps (shadow-live backfill 08-09..08-13 (simulation parity), 7d paper run started 08-13).
* Simulation JSON: `C:\Users\Braindead\Documents\trading-bot-hyperliquid\data\backtests\liquidation_flush_shadow_v2_20260911_010512.json`.
* Caveats: okx/bybit feed, not Hyperliquid; 30 days still modest for regime diversity.
