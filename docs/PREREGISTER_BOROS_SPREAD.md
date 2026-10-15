# PREREGISTER — BorosLockedSpread: cross-venue fixed-funding carry, forward-registered

Date: 2026-10-15
Status: FROZEN — committed before any code reads live data. The rules below
are the contract; nothing may be tuned on live observations. Any change
requires a new dated preregistration amendment, not an edit of the rules.

(The source note `notes/boros-spread-prereg-2026-10-15.md` was not present in
any accessible path on 2026-10-15; this document reproduces the approved
rules verbatim from the owner's instruction.)

Hypothesis: Boros YU markets let us lock a cross-venue fixed-funding spread
(receive the richer fixed leg, pay the cheaper one, neutralize the floating
legs with perps). This shadow only *counts and measures* — zero real
orders, zero API keys, hypothetical ledger only (`src/boros_shadow/`).

## Fixed rules

- New isolated module (`src/boros_shadow/`), own DB tables `boros_*`.
- No changes to Jev, VWAP, CarryA1, UnlockShort, `config_hash`
  (`4077927fec6a880c`), or `runtime_state`.
- Parameters live as code constants, not editable config. Any change
  requires a forward amendment.
- No aggregate-PnL endpoint before the read. Counts only: entries,
  near-entries (≥5%), open, closed.

## Phase 1 — collector + paper entries

1. Hourly at **:05 UTC**: GET
   `https://api.boros.finance/open-api/v1/markets?skip=0&limit=100`
   (paginate with `skip`). Per market keep: `marketId`, `imData.name`,
   `imData.maturity`, `metadata.isSpreadMarket`, `data.bestBid`,
   `data.bestAsk`, `data.floatingApr`, `data.notionalOI`,
   `config.takerFee`, `extConfig.settleFeeRate` (both fees ×1e18).
   Keep only markets with a Hyperliquid leg (name starts `"Hyperliquid "`,
   or spread `"Hyperliquid X - Venue Y"`) and the plain YU markets of the
   B venues: Binance, Gate, OKX, Bybit, Lighter, KuCoin.
2. Same minute: read the perp L2 books on HL (`info` `l2Book`) and each
   B venue via public APIs — Binance `fapi /fapi/v1/depth`, Gate
   `/api/v4/futures/usdt/order_book`, OKX `/api/v5/market/books`, Bybit
   `/v5/market/orderbook`, KuCoin and Lighter best-effort. A failing
   venue is marked `unavailable` for that snapshot and skipped.
   The raw snapshot is stored compressed.
3. Opportunity math exactly as specified:
   - `N = $10,000` per leg. Direction = the side that **receives the
     larger fixed**.
   - Gross fixed = best of: plain-YU (bid on the rich venue − ask on the
     cheap venue) or the spread market.
   - Costs: taker VIP0 (HL 4.5, Binance 5, Gate 5, OKX 5, Bybit 5.5,
     Lighter 0, KuCoin 6 bps) plus VWAP slippage for N, ×2 (open+close).
     Boros: taker fee and settle fee pro-rata to maturity, plus
     1 bp/year slippage per Boros leg.
   - Capital = `0.2·N` + `0.1·N` per Boros leg.
   - Enter if net on capital ≥ **8%/yr**, maturity in **[14, 90] days**,
     asset ∈ **{BTC, ETH, HYPE}**.
   - Max **1 open per (asset, pair, maturity)** and **1 new entry per
     asset per day**.
   - Store the trade with all entry prices.

## Phase 2 — close + realized PnL (only after Phase 1 is in production)

5. At maturity, close on the following snapshot (real taker + VWAP).
   Realized PnL = `fixed × N × T` + (real perp funding − Boros-settled
   floating) + basis drift − real costs. Sources: HL `fundingHistory`,
   Binance `/fapi/v1/fundingRate`, Gate
   `/api/v4/futures/usdt/funding_rate`; Boros settlement at
   `historical-data.boros.finance`.
6. Hypothetical liquidations, hourly:
   - POOLED: basis drift consumes `0.2·N` down to MM of 0.5%/leg.
   - Boros: YU MTM crosses `0.1·N`.
   - SPLIT 3x: descriptive only.
   Kill flags per this document; PnL stays sealed until
   **n = 10 clusters** or **2027-06-30**.
7. Status script with counts and kill flags only.
