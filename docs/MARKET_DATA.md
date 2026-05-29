# Market Data Pipeline

This document describes how funding, open interest, and feed health flow into the trading bot (v3.1+).

## Architecture

```
Hyperliquid WS (activeAssetCtx) ──┐
Hyperliquid REST predictedFundings ─┼──► TradingEngine ──► MarketEvent ──► Strategies
CEX aggregator (30s poll) ─────────┤                              └──► Dashboard
Binance futures (liq + L/S) ────────┘
```

## Data sources

| Source | Interval | Fields | Normalization |
|--------|----------|--------|---------------|
| HL WebSocket `activeAssetCtx` | Real-time | `funding`, `openInterest` (coin units) | Funding scaled to **8h** in engine |
| HL REST `predictedFundings` | ~90s | BinPerp, HlPerp, BybitPerp rates + intervals | Parsed to **8h** per venue |
| CEX aggregator | ~30s | Binance, Bybit, OKX funding + OI USD | Rates normalized to **8h** before average |
| Coinalyze (optional) | With CEX poll | Aggregated funding/OI | 8h if key set (`COINALYZE_API_KEY`) |
| Binance futures feed | ~300s | Global long/short account ratio | Ratio 0–1 |

**Important:** HL WebSocket does **not** include `predFunding`. Do not treat missing fields as `0.0`.

## MarketEvent funding fields

| Field | Meaning |
|-------|---------|
| `funding` | Best current rate (HL WS 8h, else CEX avg) |
| `predicted_funding` | HL INFO HlPerp 8h, else CEX predicted avg |
| `funding_avg` / `predicted_funding_avg` | CEX aggregator (8h-normalized) |
| `predicted_funding_by_venue` | `{"HlPerp": …, "BinPerp": …, "BybitPerp": …}` |
| `oi_total` | HL open interest (coin) |
| `oi_total_aggregated` | Sum of CEX OI (USD) |
| `market_data_health` | `green` \| `yellow` \| `red` |
| `market_data_stale` | True when CEX or HL predicted cache is stale |

Strategies should use `resolve_effective_funding()` from `src/exchanges/funding_normalize.py` instead of raw `0` defaults.

## Feed health

Per-symbol health (`src/data/market_data_health.py`):

- **green** — CEX ok (≥ `min_exchanges_for_green`) and HL predicted ok, not stale
- **yellow** — Stale cache or fewer than min CEX sources
- **red** — No usable CEX and no HL predicted

Fleet summary: `_market_data_health_summary` on the engine.

- Dashboard: Socket.IO `market_data_health`, REST `/api/market_data_health`
- Telegram: alert if overall **red** > `alert_red_after_sec` (default 300s)

When `market_data.block_funding_strategies_on_red: true`, **FundingExtreme** and **FundingArbitrage** skip entries on red feeds.

## Configuration (`config/settings.yaml`)

```yaml
market_data:
  hl_predicted_funding_poll_sec: 90
  funding_poll_sec: 30
  funding_stale_max_sec: 300
  min_exchanges_for_green: 2
  block_funding_strategies_on_red: true
  max_venue_spread: 0.001
```

Secrets: `COINALYZE_API_KEY` in env or vault (optional).

## Operational checks

```bash
python scripts/audit_market_data.py   # exit 1 if any symbol red/missing
python tests/test_market_data_funding.py
python tests/test_market_data_phase4.py
```

## Strategy usage

| Strategy | Funding logic |
|----------|----------------|
| **FundingExtreme** (`mean_reversion`) | `resolve_effective_funding()`, dynamic percentiles, cross-exchange confirm |
| **FundingArbitrage** | Cross-asset spread on resolved 8h rates; `auto_enable` when spread > costs |

## Fallback order

1. Fresh HL `predictedFundings` (HlPerp 8h)
2. CEX `predicted_funding_avg` / `funding_avg`
3. Stale cache up to `funding_stale_max_sec` (flagged stale + yellow health)
4. No signal if all missing or health red (when gated)
