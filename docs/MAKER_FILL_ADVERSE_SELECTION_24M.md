# Maker Fill + Adverse Selection — 24m candle proxy

Generated: 2026-08-10T00:54:22.451315+00:00
DB: `data/research/binance_spot_proxy.db`
Symbols: BTC, ETH, SOL, HYPE
Bars: 252,283 · dates ≈ 731 · 2024-08-09 → 2026-08-09

## Limitations (declared)

- Data are **Binance spot 15m OHLC** — **no L2 / queue**. Fill models use next-bar price penetration only.
- **M1 (naive touch)** is an **UPPER BOUND**, never an estimate.
- Adverse selection: **fill-bar close**, **+15m**, **+1h** (1m/5m not observable on this grid).
- Subtracting fill-bar AS from full-hold gross is **conservative** (partial double-count of the start of the path).
- Maker RT band: **2 bps** (1+1) primary; **4 bps** sensitivity. Taker RT reference remains **11 bps**.
- Results always as **interval [M1 .. M3_worst]** — never a single point.

## Fill models

| Model | Rule |
|---|---|
| M1 | Next bar touches limit (high/low) |
| M2 | Penetration ≥ **1 bps** beyond limit |
| M3 | Penetration ≥ **2,3,4,5 bps** (sweep) |

Fill price = resting limit at `close[t]` (not the bar extreme). Hold starts at fill bar `t+1`, exit at `close[t+1+H]`.

## Frozen signal rules

- **rvol_1h@4h:** side=+1 if rvol_1h>rolling_median_96 else -1 (positive IC); hold=4h
- **ret_lag_15m@1h:** side=-sign(ret_lag_15m) fade (negative IC); hold=1h

Note: the 24m screening taker cost test for `rvol_1h` used `sign(feature)` with always-positive rvol → effectively always-long (BE 6.81 bps). This maker test uses a **median-split** so the feature actually selects side. Taker-path BE on that rule is reported separately.

## Verdict rules

- **(A)** Survives **M2 and all M3** with fill-bar AS subtracted at 2 bps maker RT, AS ≤ fee savings (11−2=9 bps), n_fills≥30.
- **(B)** Survives only M1 and/or M2 → optimistic upper bound; do not build.
- **(C)** Does not survive → screening (C) confirmed under maker too.

## Overall verdict: **(C)**

Primary `rvol_1h@4h` verdict (C); secondary `ret_lag_15m@1h` verdict (C). Neither clears M2∩M3 with AS subtracted on the overlapping sample.

## Results — Overlapping (every signal bar)

### rvol_1h@4h — verdict **(C)**

Does not survive M2∩M3 with measured AS subtracted. Maker execution does not rescue the signal; screening verdict (C) confirmed for maker too.

- Signals: **252,087**
- Taker-path BE (this rule): **1.46 bps** (screening ref 6.81; net vs 11 bps = -9.54)
- Interval fill rate M1→M3: **[99.1% .. 79.1%]**
- Interval gross BE M1→M3: **[1.23 .. -4.62] bps**
- Interval net (2 bps RT − fill-bar AS) M1→M3: **[-1.00 .. -12.62] bps**
- Interval AS fill-bar M1→M3: **[0.22 .. 5.99] bps** (fee savings at 2 bps RT = 9.0 bps)
- Interval AS +15m M1→M3: **[0.06 .. 5.83] bps**

| model | pen | fill% | n_fills | gross BE | AS fill | AS +15m | AS +1h | net−AS @2bps | net−AS @4bps | AS>sav? |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| M1_touch | 0 | 99.1% | 249771 | 1.23 | 0.22 | 0.06 | -0.32 | -1.00 | -3.00 | n |
| M2_pen_1bps | 1 | 92.1% | 232202 | -0.79 | 2.14 | 1.98 | 1.60 | -4.93 | -6.93 | n |
| M3_pen_2bps | 2 | 88.8% | 223852 | -1.78 | 3.11 | 2.97 | 2.63 | -6.88 | -8.88 | n |
| M3_pen_3bps | 3 | 85.5% | 215438 | -2.75 | 4.08 | 3.94 | 3.61 | -8.82 | -10.82 | n |
| M3_pen_4bps | 4 | 82.3% | 207454 | -3.61 | 5.02 | 4.87 | 4.51 | -10.63 | -12.63 | n |
| M3_pen_5bps | 5 | 79.1% | 199289 | -4.62 | 5.99 | 5.83 | 5.47 | -12.62 | -14.62 | n |

Survival flags: M1=False M2=False M3_all=False {'M3_pen_2bps': False, 'M3_pen_3bps': False, 'M3_pen_4bps': False, 'M3_pen_5bps': False}

### ret_lag_15m@1h — verdict **(C)**

Does not survive M2∩M3 with measured AS subtracted. Maker execution does not rescue the signal; screening verdict (C) confirmed for maker too.

- Signals: **251,216**
- Taker-path BE (this rule): **1.09 bps** (screening ref 5.56; net vs 11 bps = -9.91)
- Interval fill rate M1→M3: **[99.0% .. 78.3%]**
- Interval gross BE M1→M3: **[0.73 .. -5.15] bps**
- Interval net (2 bps RT − fill-bar AS) M1→M3: **[-1.22 .. -12.96] bps**
- Interval AS fill-bar M1→M3: **[-0.05 .. 5.81] bps** (fee savings at 2 bps RT = 9.0 bps)
- Interval AS +15m M1→M3: **[-0.58 .. 5.30] bps**

| model | pen | fill% | n_fills | gross BE | AS fill | AS +15m | AS +1h | net−AS @2bps | net−AS @4bps | AS>sav? |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| M1_touch | 0 | 99.0% | 248717 | 0.73 | -0.05 | -0.58 | -0.73 | -1.22 | -3.22 | n |
| M2_pen_1bps | 1 | 91.7% | 230425 | -1.21 | 1.87 | 1.36 | 1.21 | -5.08 | -7.08 | n |
| M3_pen_2bps | 2 | 88.3% | 221790 | -2.22 | 2.86 | 2.36 | 2.22 | -7.08 | -9.08 | n |
| M3_pen_3bps | 3 | 84.8% | 213113 | -3.26 | 3.86 | 3.37 | 3.26 | -9.11 | -11.11 | n |
| M3_pen_4bps | 4 | 81.5% | 204797 | -4.20 | 4.82 | 4.33 | 4.20 | -11.02 | -13.02 | n |
| M3_pen_5bps | 5 | 78.3% | 196625 | -5.15 | 5.81 | 5.30 | 5.15 | -12.96 | -14.96 | n |

Survival flags: M1=False M2=False M3_all=False {'M3_pen_2bps': False, 'M3_pen_3bps': False, 'M3_pen_4bps': False, 'M3_pen_5bps': False}

## Results — Non-overlapping (step by hold)

### rvol_1h@4h — verdict **(C)**

Does not survive M2∩M3 with measured AS subtracted. Maker execution does not rescue the signal; screening verdict (C) confirmed for maker too.

- Signals: **15,756**
- Taker-path BE (this rule): **1.49 bps** (screening ref 6.81; net vs 11 bps = -9.51)
- Interval fill rate M1→M3: **[99.0% .. 78.7%]**
- Interval gross BE M1→M3: **[1.14 .. -5.30] bps**
- Interval net (2 bps RT − fill-bar AS) M1→M3: **[-0.94 .. -13.38] bps**
- Interval AS fill-bar M1→M3: **[0.08 .. 6.08] bps** (fee savings at 2 bps RT = 9.0 bps)
- Interval AS +15m M1→M3: **[-0.30 .. 5.51] bps**

| model | pen | fill% | n_fills | gross BE | AS fill | AS +15m | AS +1h | net−AS @2bps | net−AS @4bps | AS>sav? |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| M1_touch | 0 | 99.0% | 15605 | 1.14 | 0.08 | -0.30 | 0.27 | -0.94 | -2.94 | n |
| M2_pen_1bps | 1 | 91.9% | 14482 | -1.12 | 2.04 | 1.62 | 2.17 | -5.16 | -7.16 | n |
| M3_pen_2bps | 2 | 88.6% | 13967 | -1.82 | 3.00 | 2.52 | 3.25 | -6.82 | -8.82 | n |
| M3_pen_3bps | 3 | 85.3% | 13443 | -2.98 | 4.07 | 3.57 | 4.32 | -9.06 | -11.06 | n |
| M3_pen_4bps | 4 | 82.0% | 12914 | -4.37 | 5.12 | 4.65 | 5.41 | -11.49 | -13.49 | n |
| M3_pen_5bps | 5 | 78.7% | 12404 | -5.30 | 6.08 | 5.51 | 6.28 | -13.38 | -15.38 | n |

Survival flags: M1=False M2=False M3_all=False {'M3_pen_2bps': False, 'M3_pen_3bps': False, 'M3_pen_4bps': False, 'M3_pen_5bps': False}

### ret_lag_15m@1h — verdict **(C)**

Does not survive M2∩M3 with measured AS subtracted. Maker execution does not rescue the signal; screening verdict (C) confirmed for maker too.

- Signals: **62,994**
- Taker-path BE (this rule): **0.98 bps** (screening ref 5.56; net vs 11 bps = -10.02)
- Interval fill rate M1→M3: **[99.0% .. 78.1%]**
- Interval gross BE M1→M3: **[0.68 .. -5.07] bps**
- Interval net (2 bps RT − fill-bar AS) M1→M3: **[-1.29 .. -12.86] bps**
- Interval AS fill-bar M1→M3: **[-0.04 .. 5.80] bps** (fee savings at 2 bps RT = 9.0 bps)
- Interval AS +15m M1→M3: **[-0.54 .. 5.26] bps**

| model | pen | fill% | n_fills | gross BE | AS fill | AS +15m | AS +1h | net−AS @2bps | net−AS @4bps | AS>sav? |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|
| M1_touch | 0 | 99.0% | 62343 | 0.68 | -0.04 | -0.54 | -0.68 | -1.29 | -3.29 | n |
| M2_pen_1bps | 1 | 91.7% | 57762 | -1.18 | 1.88 | 1.31 | 1.18 | -5.07 | -7.07 | n |
| M3_pen_2bps | 2 | 88.2% | 55548 | -2.28 | 2.86 | 2.35 | 2.28 | -7.14 | -9.14 | n |
| M3_pen_3bps | 3 | 84.7% | 53371 | -3.32 | 3.89 | 3.40 | 3.32 | -9.20 | -11.20 | n |
| M3_pen_4bps | 4 | 81.4% | 51250 | -4.22 | 4.84 | 4.34 | 4.22 | -11.07 | -13.07 | n |
| M3_pen_5bps | 5 | 78.1% | 49205 | -5.07 | 5.80 | 5.26 | 5.07 | -12.86 | -14.86 | n |

Survival flags: M1=False M2=False M3_all=False {'M3_pen_2bps': False, 'M3_pen_3bps': False, 'M3_pen_4bps': False, 'M3_pen_5bps': False}

## Fee savings vs adverse selection

Taker RT 11 bps → maker 2 bps saves **9 bps**; maker 4 bps saves **7 bps**. If measured AS (15m markout against the fill) exceeds that saving, maker fees do not fix economics — adverse selection ate the rebate.

## Conclusion

Maker execution does **not** rescue these directional candle signals. Screening verdict **(C)** is confirmed for maker as well; the directional candle-feature family is **FINAL** under this cost model. The fill/AS machinery remains useful for future MM research.
