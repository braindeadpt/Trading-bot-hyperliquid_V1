# ATR Percentile Long-History Revalidation

Generated: 2026-08-09T22:08:38.630868+00:00
HL candles: `data/live/bot.db` · Binance spot proxy: `data/research/binance_spot_proxy.db`

## Verdict

### **(C)** — Signal disappears or fails to clear costs on the long sample with date-block inference — likely an artifact of the original ~83-day vol regime. Do not build. Archive.

- Long-sample BE RT: **-2.53 bps** (short-sample was 34.51 bps)
- Date-block edge CI (gross−11bps): **[-35.03, 3.91] bps**
- Vol regimes surviving taker CI: 0/3

## Task 2 — Binance spot ↔ HL proxy

- Overlap bars: 24996 · atr% corr: **0.981** · binary agree: **97.1%** · usable: **True**
- Rule: usable if corr≥0.80 and binary agree≥0.85 (predeclared)
- **HYPE:** no Binance SPOT klines — backfill used **USD-M futures** (`fapi`) from listing ~2025-05-30. BTC/ETH/SOL are true spot. Declared, not silent.

| symbol | n | corr | signal agree |
|---|---:|---:|---:|
| BTC | 7063 | 0.988 | 97.7% |
| ETH | 7068 | 0.989 | 97.8% |
| HYPE | 3741 | 0.947 | 93.4% |
| SOL | 7065 | 0.985 | 97.6% |

Note: sub-period block 6 (2026-04→08) is roughly the original HL sample window and is one of only two blocks with positive post-cost mean — consistent with a regime artifact.

## Task 3 — Long revalidation (date-block inference)

- Symbol-day non-overlap trades: 2598 · **n_independent_dates: 723** (this is the inference N)
- Overlapping obs BE: -2.53 bps · tt: -13.53 bps
- Date-block mean(gross−11bps): -15.72 bps · CI [-35.03, 3.91] · P(>0)=0.064

### Quintiles (overlapping, for shape)

| Q | signal_mean | n | gross | tt |
|---:|---:|---:|---:|---:|
| Q1 | 0.078 | 49787 | 2.85bps | -8.15bps |
| Q2 | 0.274 | 49970 | 0.99bps | -10.01bps |
| Q3 | 0.484 | 49654 | 5.80bps | -5.20bps |
| Q4 | 0.701 | 49937 | -3.69bps | -14.69bps |
| Q5 | 0.910 | 49490 | -18.67bps | -29.67bps |

### Sub-period stability (≥6 blocks of dates)

| block | dates | n | gross | tt | >taker? |
|---:|---|---:|---:|---:|:---:|
| 1 | 2024-08-16→2024-12-13 | 120 | 8.32bps | -2.68bps | n |
| 2 | 2024-12-14→2025-04-13 | 121 | -15.25bps | -26.25bps | n |
| 3 | 2025-04-14→2025-08-11 | 120 | 19.02bps | 8.02bps | Y |
| 4 | 2025-08-12→2025-12-10 | 121 | -37.76bps | -48.76bps | n |
| 5 | 2025-12-11→2026-04-09 | 120 | -34.43bps | -45.43bps | n |
| 6 | 2026-04-10→2026-08-08 | 121 | 31.86bps | 20.86bps | Y |

### Vol regimes (BTC 30d rvol terciles)

Method: BTC 30d realized-vol terciles on daily closes · cuts q33=0.3577139667396863 q66=0.4478782827195349

| regime | n_dates | gross | tt | edge CI | survives? |
|---|---:|---:|---:|---|:---:|
| low_vol | 234 | 14.47bps | 3.47bps | [-24.48987021897915, 32.23262535285012] | n |
| mid_vol | 234 | -8.27bps | -19.27bps | [-54.15790455385564, 14.421570101114368] | n |
| high_vol | 242 | -25.64bps | -36.64bps | [-72.08212009619206, -0.8924032091435998] | n |

### Per-symbol

| symbol | n_trades | n_dates | gross | tt |
|---|---:|---:|---:|---:|
| BTC | 723 | 723 | -7.71bps | -18.71bps |
| ETH | 723 | 723 | 8.77bps | -2.23bps |
| HYPE | 429 | 429 | -27.07bps | -38.07bps |
| SOL | 723 | 723 | -1.66bps | -12.66bps |

## Continuação

Arquivar no backlog. Não construir. Não procurar variantes deste sinal.
