# Liquidation Flush Shadow-Live (ETH p90 / hold 30m / fade) — 7-day paper run

**Status: RUNNING** (started 2026-08-13 05:36 UTC, PID tracked in
`logs/liquidation_flush_shadow_live.out`). Planned window: 7 days
(→ 2026-08-20). This doc is the operational contract; the live state
file (`data/research/liquidation_flush_shadow_live_state.json`, gitignored)
is the source of truth for the ledger.

## What this is

The v2 simulation (scripts/liquidation_flush_shadow.py) found one cell that
approached the baseline gate on the REAL liquidation source (okx+bybit):
**ETH, p90 of dominant-minute notional, hold 30m, fade, no SL** — n=46,
WR 50.0%, PF 2.35, avg +7.0 bps (evidence: `data/backtests/liquidation_flush_shadow_v2_evidence.md`).
The same cell is negative on the proxy sample (−7.6 bps), so the edge needs
confirmation on out-of-sample data. This harness runs that exact cell in
**shadow/live** — real feed, real candles, paper fills — for 7 days, and
reports the live P&L against the simulated baseline.

## The executed cell (pinned, byte-identical to v2)

| Parameter | Value |
|---|---|
| Symbol | ETH |
| Flush definition | 1-minute bucket where dominant-side notional ≥ threshold |
| Threshold | **$1,024K** (p90 of dominant-minute notional, real sample 08-09..08-13) |
| Direction | fade (against the flush: liq side=long → buy, side=short → sell) |
| Entry | OPEN of the first 1m candle with ts ≥ flush_minute_ms + 60s (candle m+1) |
| Exit | CLOSE of the candle at entry index + 30 |
| Fees | 0.045% × 2 (0.090% RT) |
| Stop-loss | none (v2 evidence: SL 1–2% is a no-op in this cell) |
| Sources | `liquidation_events` where source in (okx, bybit) — the `REQUIRE_REAL_LIQUIDATION` rule |

The threshold is **pinned**, not re-fit live, so the live cell is exactly the
simulated one — no selection on new data.

## Fidelity checks (backfill == simulation)

On startup the harness backfills all minutes whose candles exist (tagged
`kind=backfill`) using the same entry/exit semantics as the simulation, then
continues live (`kind=live`). This doubles as a parity check:

| Metric | Simulation (current) | Backfill | Δ |
|---|---|---|---|
| n | 47 | 47 | 0 |
| WR | 48.9% | 48.9% | 0 |
| PF | 2.23 | 2.23 | 0 |
| avg net | +6.4 bps | +6.37 bps | ~0 |

(The v2 report said n=46/+7.0 — the real sample has grown since 04:38; the
harness replicates the *current* simulation exactly.)

One bug found and fixed during bring-up: the fade side was initially
inverted (executed continuation). Caught by the backfill-vs-simulation
parity check — entry timestamps matched, side did not.

## Mechanics (live)

* Polls `data/live/bot.db` every 15 s (read-only).
* Candle convention verified in DB: `candles_1m.ts % 60_000 == 59_999`
  (ts = END of minute). Entry uses `bisect_left(ts_list, m*60_000 + 60_000)`
  exactly as the simulation does.
* A flush minute is finalized only when its own candle exists (minute
  closed), so late-arriving events within the minute are still counted.
* State is persisted atomically after every mutation — the monitor can be
  killed and relaunched at any time; on restart it resumes (backfills any
  gap, dedupes scanned minutes, completes open positions).
* Known limitation: fills use the candle OPEN, not the tick — identical to
  the simulation's fill assumption, but observable ~60 s after the real open.

## How to monitor

```bash
tail -f logs/liquidation_flush_shadow_live.out            # live events + reports
python scripts/liquidation_flush_shadow_live.py --once    # single backfill pass + report (safe to run while monitor runs)
python -c "import json; s=json.load(open('data/research/liquidation_flush_shadow_live_state.json')); print(len(s['trades']), 'trades,', len(s['open_positions']), 'open')"
```

The monitor prints a full report when state changes or every 10 minutes.

## Decision rule (at 7 days, or earlier if n is decisive)

Compare the LIVE subset against the baseline (n=46, WR 50%, PF 2.35, +7.0 bps):

* **n ≥ 30 and avg ≥ +3 bps and PF > 1.2** → edge confirmed out-of-sample;
  promote to a real strategy proposal (sizing, SL study, venue execution).
* **n ≥ 30 and avg < 0 or PF < 1** → hypothesis dead; kill it.
* **n < 30** → insufficient sample; extend the window.

Caveats that persist regardless of outcome: the feed is okx/bybit, not
Hyperliquid (HL liqs don't appear in it), and ~5 days of real data carry
little regime diversity. The live run adds up to 7 days of fresh,
out-of-sample evidence on top of the 5-day sample.
