# OI Backfill Viability — sources & depths

Generated: 2026-08-10
Purpose: decide whether `oi_delta_24h` (BE 19.6 bps, edge CI [−39,+39] on ~66 HL dates) can be resolved with more data.

## Local baseline

| DB | `oi_history` span | Notes |
|----|-------------------|--------|
| `data/live/bot.db` | 2026-06-05 → 2026-08-10 (~**65 days**, 4 symbols) | HL-native live sampler |
| `data/research/hyperliquid.db` | was empty → **backfilled** | see below |

Prior long-horizon cost test used the ~66d live sample → **INCONCLUSIVE** (powered enough for point BE>11 but CI includes large losses).

## Source survey

| Source | Historical OI? | Max depth (measured 2026-08-10) | Granularity | Request cost |
|--------|----------------|----------------------------------|-------------|--------------|
| **Binance** `fapi` `/futures/data/openInterestHist` | Yes | **~30 days** (1d/4h); ~21d @1h with limit=500 | 5m–1d | 1 req / ≤500 bars; windowing needed inside 30d |
| **Binance** `/fapi/v1/openInterest` | Current only | — | snapshot | 1/symbol |
| **Bybit** `/v5/market/open-interest` | Yes | **1h ≈ 667d**; **4h/1d ≈ 6y** (paginated) | 5min–1d | ≤200/page; ~49 pages/symbol for 400d@1h |
| **OKX** `/api/v5/public/open-interest-history` | Yes (docs) | **HTTP 403** from this host (geo/WAF) | 5m–1D | n/a here |
| **Hyperliquid** INFO | **No** public OI history | candles have no OI; `openInterest*` types 422 | live `metaAndAssetCtxs` / WS ctx only | — |
| **Coinalyze** `/v1/open-interest` | Current used by funding aggregator | History endpoint not verified this run (API key not in probe shell env); free tier shares **40 req/min** with funding/liq | live | Must stay within documented budget (`coinalyze_poll_sec≥600` for liq) |

## Decision

**YES — extend to ≥6–12 months via Bybit 1h OI.**

Executed: `scripts/backfill_oi_bybit_research.py --days 400` → `data/research/hyperliquid.db`
Stored **38,400** rows (BTC/ETH/SOL/HYPE), **2025-07-06 → 2026-08-10** (~400d / ~13 months).

### Declared limitations of the backfill

- Bybit linear OI is a **CEX proxy**, not Hyperliquid-native OI.
- Forward returns for screening use Binance spot 15m proxy (cross-venue).
- Do **not** write this series into `bot.db`.

If Bybit had been unavailable, the correct status would have been:
**OI family INCONCLUSIVE for lack of data** (not “no edge”), with observed BE 19.6 / CI [−39,+39] parked until ≥6–12 months of independent dates exist.
