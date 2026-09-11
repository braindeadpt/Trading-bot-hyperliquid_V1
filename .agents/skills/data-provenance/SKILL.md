---
name: data-provenance
description: Candle/data provider tiers — which source is allowed for OOS-grade work vs research-screening only. Consult before any backfill, backtest, or OOS run.
triggers:
  - user
  - model
allowed-tools:
  - read
  - grep
  - glob
---

# Data provenance tiers

Canonical references: `docs/MAINNET_READINESS.md` §5, `docs/NODE_TRADES_REBUILD.md`,
`src/data/series_metadata.py`, `src/data/candle_providers/`.

## Tiers (decided by measured parity vs `hl_candleSnapshot`, not by marketing)

| Provider | `source=` / venue | Tier | Why |
|---|---|---|---|
| **Hyperliquid official** (`candleSnapshot` WS/REST) | `hl_candle_snapshot` / `hyperliquid` | **OOS-grade** | Venue of record |
| **S3 node-fills rebuild** (`hl_node_trades_rebuild.py`) | `hl_node_trades_rebuild` / `hyperliquid` | **OOS-grade after secondary validation** | Rebuilt from official node S3 archives; `--execute` runs `revalidate_rebuilt_rows` — `pass` requires zero mismatches vs official rollup, `inconclusive` ≠ pass |
| **Coinalyze HL** (`*_PERP.A`) | `coinalyze_hl` | **Research/screening only** | Parity FAIL 2026-09-11: 10,952 OHLC mismatches / 2,850 bars (~0.02% systematic). Unique value: real taker-buy split (`bv`) |
| **GoldRush HyperCore** | `goldrush_hypercore` | **BLOCKED — billing** | All 16 parity cells failed `402 insufficient_credits`; even funded, prior divergence was seen — third-party indexer, never venue-of-record |
| **Bybit perp klines** | `bybit_klines` / venue `bybit` | **Cross-venue PROXY** — explicit opt-in only (`--provider bybit`), never in the auto chain | Different venue entirely |

## Rules

- OOS, parameter tuning, holdout, and performance backtests run ONLY on OOS-grade sources (`hl_candle_snapshot`, validated `hl_node_trades_rebuild`).
- Screening/feature work may use research-grade sources but results carry the caveat in the report header.
- `SeriesMetadata.quality_flags.proxy: true` marks cross-venue data — a backtest on proxy candles is evidence about a *proxy*, never promotion evidence.
- Before trusting any new provider: run `scripts/research/goldrush_parity_diagnostic.py` (or the provider's parity harness) vs official candles. Absence of overlap = `inconclusive`, never a pass.
