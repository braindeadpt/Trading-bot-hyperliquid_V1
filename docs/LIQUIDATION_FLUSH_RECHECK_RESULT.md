# Liquidation Flush Recheck — 30-day real-feed comparison

_Generated 2026-08-13T05:42:20+00:00 by `scripts/liquidation_flush_recheck.py`._

**Real feed span at trigger: 3.5 days (10606 okx/bybit events).**

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
| n | 46 | 47 | +1 |
| win rate | 50.0% | 48.9% | -1.1pp |
| profit factor | 2.353 | 2.228 | -0.125 |
| avg net | +6.98 bps | +6.37 bps | -0.61 bps |
| total net | +321 bps | +299 bps | -22 bps |

## Verdict

**CONFIRMED — promote to strategy proposal**

## Context

* Live/shadow evidence so far: n=47, WR 48.9%, PF 2.23, avg +6.37 bps (shadow-live backfill 08-09..08-13 (simulation parity), 7d paper run started 08-13).
* Simulation JSON: `C:\Users\Braindead\Documents\trading-bot-hyperliquid\data\backtests\liquidation_flush_shadow_v2_20260813_064220.json`.
* Caveats: okx/bybit feed, not Hyperliquid; 30 days still modest for regime diversity.
