# Feed Contamination Audit — Binance fstream outage

Generated: 2026-08-09
Incident: Binance USD-M **fstream WebSocket delivers 0 messages** on this
network since **2026-06-29** (confirmed: futures `@aggTrade` 0/20s; spot OK;
`fapi` REST HTTP 200).

This document answers: which prior conclusions still stand, which must be
redone, and what was fixed in code.

---

## 0. The most important fix

**Silent-feed alerting** (`FeedSilenceMonitor` in
`src/data/market_data_health.py`, wired in the engine + dashboard
`/api/market_data_health.feed_silence`).

A contracted feed that produces nothing for N hours now logs **ERROR**,
notifies, and marks **degraded**. Applied to:

| feed | default silence threshold |
|------|---------------------------|
| `liquidation_binance` | 6h |
| `binance_perp` | 1h |
| `funding_cex` / `funding_hl` | 1h |
| `taker_split` | 1h |

This is more important than any alternate data source: six weeks of silent
failure contaminated analyses because nobody was told the pipe was empty.

---

## 1. What the DB actually contains

| table | span | n | provenance |
|-------|------|--:|------------|
| `binance_perp_prices` | 2026-05-30 → **2026-06-29 12:13 UTC** | ~140k | REST klines / prior backfill (real prices) |
| `liquidation_events` | 2026-06-08 → **2026-06-29 12:12 UTC** | 5636 | **100% `source='proxy'`** |
| `candles_15m` (screening span) | 2026-05-18 → ~2026-08-09 | ~27k | HL |

### Critical correction vs initial assumption

There are **zero** `source='binance'` force-order rows in `bot.db`.
The June liquidation history is the **same candle+OI heuristic** as live
`_accumulate_liquidation_proxy` (`src/data/external_feeds_backfill.py`),
not live `@forceOrder`. Real Binance liquidations were never persisted
here — fstream being dead explains why, but the table was never “real”
even before 29/06.

---

## 2. How missing data was treated (this is what invalidated TOPs)

### Feature screening (`scripts/feature_screening.py`)

| family | columns | treatment after 29/06 | severity |
|--------|---------|----------------------|----------|
| **BASIS** | `basis`, `basis_z_7d`, `basis_velocity_1h` | `merge_asof(..., backward)` **forward-fills the last 29/06 Binance perp price** onto every later 15m bar | **Severe** — stale level masquerades as live basis for ~40 days |
| **LIQ** | `liq_*` | No new events → bar aggregates → **zeros** (not NaN) | Moderate — looks like “no liquidations” rather than “feed dead” |
| Funding / OI / CVD / ret_lag / vol / time | candle-native or HL tables | Unaffected by fstream | OK |

**Conclusion:** the original report’s claim that BASIS and LIQ “don’t make
TOP” on the **full** window is **invalid** for those families. Other TOP
families (mean-reversion `ret_lag_*`, vol regime, calendar `dow`) do **not**
depend on these feeds and **remain standing**.

### Reversion cost test

Uses only HL OHLCV + `ret_lag_*`. **Unaffected.** Verdict (C) stands.

### Baseline-signal gate

| strategy | uses liq / bn_perp? | impact |
|----------|---------------------|--------|
| ChecklistMeta | **yes — `w_liquidation` scored whenever `liquidation_side_5m` set; did not check provenance** | Live + any replay with proxy stats contaminated |
| LiquidationCatcher | requires `liquidation_data_source == "binance"` | Correctly refused proxy; silent (no trades) |
| LeadLag (`perp_lead`) | requires `binance_perp_mid` | **Non-functional since 29/06** (no ticks) |
| CVD / VWAP / VB / others | no | Unaffected at feed layer |

### Live ChecklistMeta trades

| metric | value |
|--------|------:|
| CM trades in `trades` | 185 |
| First / last entry | 2026-06-30 → 2026-08-08 |
| Trades **on/after 2026-06-29** | **185 (100%)** |
| Trades with `liq_*` in `signal_metadata.components` | **163 (88%)** |
| `liquidation_data_source` in snapshots | always absent / None |

So nearly all live CM paper trades scored a **proxy** liquidation component
(0.5 pts ≈ 12.5% of `score_threshold` 4.0) while provenance was blank
because `_accumulate_liquidation_proxy` appended events **without** setting
`acc["source"]="proxy"`.

---

## 3. Status of prior conclusions

| conclusion | status |
|------------|--------|
| Feature screening pipeline controls (pos/neg) | **STANDS** |
| Mean-reversion TOP + cost-test verdict **(C)** | **STANDS** |
| CVD feature note (B1=86, strategy closed) | **STANDS** |
| Full-window “BASIS/LIQ not TOP” | **INVALID** — redo on valid windows / after backfill |
| ChecklistMeta baseline FAIL → demote | **Re-check** with `w_liquidation=0` (see §5) |
| LeadLag shadow “running” since 29/06 | **Fiction** — no perp mid feed |

---

## 4. Code fixes applied (no `.env`; bot not restarted by this work)

1. **Proxy provenance:** `_accumulate_liquidation_proxy` sets `acc["source"]="proxy"`.
2. **ChecklistMeta:** scores `w_liquidation` **only if**
   `liquidation_data_source == "binance"` (proxy ≡ absent).
3. **FeedSilenceMonitor** + ERROR/notifier + dashboard field.
4. **Backfill CLI:** `scripts/backfill_binance_perp_prices.py --from-gap`.
5. **Valid-window re-screen:** `scripts/feature_screening_basis_liq_valid.py`.
6. **CM control gate:** `scripts/baseline_gate_cm_no_liq.py` (in-memory
   `w_liquidation=0`).

### Config change (confirmed 2026-08-09)

```yaml
market_data:
  liquidation_source: binance   # was: auto — absence is now visible
```

Applied in `config/settings.yaml`. Restart paper bot to load.

---

## 5. ChecklistMeta gate re-check

Script: `python scripts/baseline_gate_cm_no_liq.py --seeds 100 --folds W2,W3`

| fold | prior (w_liq=0.5) | control (w_liq=0) |
|------|-------------------|-------------------|
| W2 | B1≈48, n=146, FAIL | B1=49, n=148, PF=0.41, **FAIL** |
| W3 | B1≈43, n=215, FAIL | B1=47, n=215, PF=0.26, **FAIL** |

**Demotion stands.** Proxy liquidation scoring was real contamination on live
trades (163/185) but **immaterial** to the baseline-gate FAIL — same class as
the OIR ablation (M1≈M2≈M3).

---

## 6. LeadLag / REST polling

- `perp_lead` needs **1s-class** Binance mark updates (`impulse_window_ms`
  default 10_000).
- REST `fapi` works; 1m klines backfill restores **research/basis** history.
- Polling `premiumIndex` every 1–2s is **marginally** compatible with a 10s
  impulse window but is not equivalent to `@markPrice@1s`, adds jitter, and
  still fails if the network blocks futures WS for other reasons.
- **Declaration:** while fstream is dead, **LeadLag `perp_lead` is not
  viable on this network** — do not leave it in shadow pretending to run.
  `basis` mode (spot) is a separate path (spot WS still works).

---

## 7. Follow-ups

1. Confirm `liquidation_source: binance` (or keep `auto` now that proxy is
   ignored by CM).
2. After perp backfill: optional full-window BASIS re-screen.
3. Real liquidations: need a working force-order path (different network /
   relay) before LIQ features are testable as *real*.
4. Restart paper bot to load provenance + silence monitor (when you choose).
