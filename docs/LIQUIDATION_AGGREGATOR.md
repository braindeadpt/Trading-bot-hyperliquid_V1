# Multi-venue liquidation aggregator

Status: implemented 2026-08-09. Replaces the Binance-fstream-only path that has been
dead on this network since 2026-06-29 (`@forceOrder` = 0 msgs; table was 100% `proxy`).

## Research findings (do not skip)

| Venue | Live market-wide liquidations? | Mechanism |
|-------|--------------------------------|-----------|
| **Hyperliquid** | **No (public API)** | Official public `trades` schema is `{coin,side,px,sz,tid,time,hash,users}` — **no** `liquidation` field (sampled 221 trades / 25s, 2026-08-09). `liquidation` exists on **per-user** `WsFill` / `userEvents` only. GoldRush documents `liquidationFills` as **GoldRush-native** with no `wss://api.hyperliquid.xyz/ws` equivalent. Node fill archives remain research-only. Defensive hook kept in `hyperliquid_ws._parse_trades` if HL ever adds the field. **Silence monitor is not contracted for HL** (would false-alarm forever). |
| **OKX** | Yes | WS `liquidation-orders` (`instType=SWAP`) on `wss://ws.okx.com:8443/ws/v5/public`. REST `/api/v5/public/liquidation-orders` confirmed with real `bkPx`/`posSide`/`sz`/`ts`. Startup republishes last **6h** of REST prints into DataBus (engine 5m window still prunes). HYPE → `HYPE-USDT-SWAP`. |
| **Bybit** | Yes | WS `allLiquidation.{SYMBOL}` on `wss://stream.bybit.com/v5/public/linear` (v5; replaces deprecated `liquidation.{symbol}`). HYPE → `HYPEUSDT`. |
| **Binance** | Blocked here | Still in `BinanceFuturesFeed` (`@forceOrder`); fstream unreachable. Kept for when network allows; silence key `liquidation_binance`. |
| **Coinalyze** | Aggregated history | REST `/liquidation-history` (key already used by funding). **Verify-only** — never summed. |

## Aggregation semantics

1. **Per-event attribution is mandatory.** Every DataBus / DB row has `source ∈ {hl,okx,bybit,binance}`.
2. **5m strategy window sums notional across real venues** → *cross-venue market liquidation pressure*. That is a different quantity from single-venue notional; do not compare thresholds calibrated for Binance-only to multi-venue sums without re-calibration.
3. **Coinalyze is never added into the sum.** It already covers Binance/OKX/Bybit (and more). Using it as a third additive source would double-count. Role: coverage cross-check + gap detection only.
4. **Proxy** remains a separate engine path (`market_data.liquidation_source: proxy` or `auto` fallback). Strategies that `require_real_liquidation_data` reject it.

## Provenance enum

`market_data.liquidation_source`:

| Value | Strategy window accepts |
|-------|-------------------------|
| `real` | `hl` / `okx` / `bybit` / `binance` — MarketEvent label becomes `"real"` |
| `binance` | `binance` only (legacy) |
| `proxy` | candle+OI heuristic only |
| `auto` | real venues if present, else proxy |

`LiquidationCatcher` / `ChecklistMeta` accept provenance via `is_real_liquidation_source()` → `"real"` or any wire source in `REAL_LIQUIDATION_SOURCES`.

Applied in `config/settings.yaml` (2026-08-09, operator-confirmed): `liquidation_source: real`.

## PROVISIONAL strategy thresholds

`strategy.liquidation_catcher` (paper / shadow):

| key | value | label |
|-----|------:|-------|
| `min_notional_usd` | 5_700_000 | **PROVISIONAL** — OKX-REST-only 5m-window **p90** ≈ $5.71M |
| `min_liquidation_count` | 18 | **PROVISIONAL** — OKX-REST-only p90 count |

Why p90 not p95: in shadow, extra signals cost nothing and speed n≥30 accumulation.

**Do not read “p90” as multi-venue truth.** Calibration used OKX alone
(`scripts/calibrate_liquidation_thresholds.py` →
`data/backtests/parity_diag/liq_threshold_calibration_okx.json`). The live
window **sums OKX+Bybit**, so the same dollar cut will sit closer to
**~p75–p80 of the aggregated distribution**. Recalibrate from DB rows with
`source ∈ {okx,bybit,…}` after real data accumulates.

Thresholds filter **strategy signals only**. The aggregator / engine persist
every real event to `liquidation_events` regardless — future recalibration is
never blocked by today’s cut.

Mainnet override remains deliberately higher (`mode_overrides.mainnet` $50M)
until a powered real-liq gate exists.

## Limitations (must not be forgotten)

### 1. Forward-only evaluation — no real-liq backtest

Historical `liquidation_events` in this deployment were **100% `source=proxy`**.
There are **no** past genuine prints to replay. LiquidationCatcher cannot be
honestly backtested on this DB; the baseline-signal gate for this name arrives
only after weeks of live real accumulation (n≥30 on a validation fold). Expect
the gate later than for candle-native strategies.

### 2. Execute on HL, measure liquidations elsewhere

We trade Hyperliquid perps but HL has **no** public market-wide liquidation
stream. The signal is OKX+Bybit cascade pressure — a **global-market proxy**,
not venue-native fuel on the book we execute against. Crypto often moves as a
bloc, so the fade thesis can still hold, but it is an explicit **causal leap**:
“CEX liquidation cascade → HL mid overshoot → fade on HL”. Write this into any
future promotion / preregister note; if the gate ever PASSes, re-state the leap
rather than pretending the prints are HL liquidations.

## FeedSilenceMonitor (per source)

| Feed key | Default max silence | Notes |
|----------|---------------------|-------|
| `liquidation_okx` | 6h | Contracted |
| `liquidation_bybit` | 6h | Contracted |
| `liquidation_binance` | 6h | Contracted (expected degraded on this network) |
| `liquidation_coinalyze_check` | 12h | Beat on successful verify poll only |
| `liquidation_hl` | — | **Not enabled** (unsupported venue) |

Partial silence (OKX quiet, Bybit alive) must surface — that is the lesson from the 6-week Binance outage.

## Coinalyze request budget

Free tier ≈ **40 req/min**.

- FundingOIAggregator: ~3 calls × N symbols / `funding_poll_sec` (30s default) → with N=4 ≈ **24 req/min**.
- Liquidation check: **1 call × N / `liquidation_coinalyze_poll_sec`** (default **900s**, floor 600s) → **≈0.27 req/min**.

Keep `liquidation_coinalyze_poll_sec ≥ 600`. Never poll liquidations on the funding cadence.

## Symbol map

| HL | OKX | Bybit | Coinalyze |
|----|-----|-------|-----------|
| BTC | BTC-USDT-SWAP | BTCUSDT | BTCUSDT_PERP.A |
| ETH | ETH-USDT-SWAP | ETHUSDT | ETHUSDT_PERP.A |
| SOL | SOL-USDT-SWAP | SOLUSDT | SOLUSDT_PERP.A |
| HYPE | HYPE-USDT-SWAP | HYPEUSDT | HYPEUSDT_PERP.A |

Missing instruments are logged once and skipped — never silently ignored mid-stream.

## Code

- `src/exchanges/liquidation_event.py` — shared model + provenance helpers
- `src/exchanges/liquidation_aggregator.py` — WS aggregator
- `main.py` — starts aggregator when mode ∈ {real, auto, binance}
- Threshold calibration: `scripts/calibrate_liquidation_thresholds.py` (OKX REST distribution → proposed p90/p95; **does not write YAML**)

## Threshold recalibration

1. Wait until DB has enough **real** rows (`source != proxy`) across venues.
2. Re-run `scripts/calibrate_liquidation_thresholds.py` **and/or** compute
   percentiles on the engine’s summed 5m windows (preferred — matches live
   aggregation semantics).
3. Ask before rewriting YAML; replace the PROVISIONAL comments with the new
   percentile **and** state whether it is single-venue or multi-venue.
