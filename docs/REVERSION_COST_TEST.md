# Reversion Cost Test

Generated: 2026-08-09T19:50:42.583737+00:00
DB: `data/live/bot.db`
Symbols: BTC, ETH, SOL, HYPE
Bar grid: closed 15m only. Exit: time stop only (no SL/TP).

## Rule (frozen — no tuning)

```
signal = ret_lag_L
side   = -sign(signal)   # fade
exit   = close after H bars
```

## Cost books

| book | entry fee | exit fee | entry slip | exit slip | RT |
|---|---:|---:|---:|---:|---:|
| gross_0bps | 0.00bps | 0.00bps | 0.00bps | 0.00bps | 0.00bps |
| maker_maker | 1.00bps | 1.00bps | 0.00bps | 0.00bps | 2.00bps |
| maker_taker | 1.00bps | 3.50bps | 0.00bps | 2.00bps | 6.50bps |
| taker_taker | 3.50bps | 3.50bps | 2.00bps | 2.00bps | 11.00bps |

`taker_taker` RT = **11.0 bps** (matches bot: 3.5bps fee + 2bps slip per side).

## Maker execution model (declared)

1. **Optimistic / UPPER BOUND:** assume 100% fill at signal close with maker fees only (or maker entry + taker exit). This **overstates** edge — limit orders are not always filled and fills are adversely selected. Never treat this column as an estimate.
2. **Conservative (used for verdict B):** resting limit at `close[t]` with **fill price = limit** (not the bar extreme — using the extreme would improve fade entries and invent edge). Fill only if the next 15m bar penetrates the limit by ≥2 bps (long: `low[t+1] ≤ close[t]×(1−2bps)`; short: symmetric). Hold from `t+1` → `close[t+1+H]`. Non-fills skipped. A weaker “any touch” rule filled ~98% of 15m bars and was rejected as vacuous.
Optimistic maker green without conservative green → **not** verdict B.

## Verdict

### **(C)** — Effect is real in the gross/IC sense but not exploitable at realistic costs. Best gross breakeven RT is well below taker RT; conservative maker fails; optimistic maker (if green) is an UPPER BOUND only — not authorization to build.

- Best gross breakeven RT among combos: **4.21 bps** vs taker RT **11.0 bps**.
- Optimistic maker alone looked positive on: `4h/4h` (mm_opt=2.21bps, mt_opt=-2.29bps), `1h/4h` (mm_opt=0.43bps, mt_opt=-4.07bps) — **not** counted as survival.

## Breakeven & cost sweep (overlapping obs = IC sample)

| L/H | n_obs | n_nonoverlap | gross E | **BE RT** | 0bps | mm opt | mm cons | mt cons | **tt** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1h/1h | 27601 | 6912 | 1.62bps | **1.62bps** | 1.62bps | -0.38bps | -3.69bps | -8.19bps | **-9.38bps** |
| 4h/4h | 27531 | 1724 | 4.21bps | **4.21bps** | 4.21bps | 2.21bps | -0.84bps | -5.34bps | **-6.79bps** |
| 1h/4h | 27553 | 1728 | 2.43bps | **2.43bps** | 2.43bps | 0.43bps | -2.95bps | -7.45bps | **-8.57bps** |
| 1h/15m | 27613 | 27613 | 0.35bps | **0.35bps** | 0.35bps | -1.65bps | -4.65bps | -9.15bps | **-10.65bps** |
| 4h/15m | 27591 | 27591 | 0.19bps | **0.19bps** | 0.19bps | -1.81bps | -5.10bps | -9.60bps | **-10.81bps** |
| 15m/1h | 27529 | 6910 | 0.95bps | **0.95bps** | 0.95bps | -1.05bps | -4.65bps | -9.15bps | **-10.05bps** |

Reading the BE column: if breakeven RT ≪ taker RT (11 bps), the effect cannot pay for current execution. Longer H → fewer non-overlap trades → less fee drag *per unit time*, but the per-trade BE is still set by mean gross edge.

## Quintiles (taker/taker) — where the edge lives

### 1h/1h

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.00778 | 5521 | -8.28bps | 44.4% |
| Q2 | -0.00200 | 5520 | -10.84bps | 38.3% |
| Q3 | -0.00001 | 5520 | -11.52bps | 33.6% |
| Q4 | 0.00190 | 5520 | -9.90bps | 37.8% |
| Q5 | 0.00757 | 5520 | -6.38bps | 45.0% |

- Always: -9.38bps (n=27601)
- Extremes only (Q1∪Q5): -7.33bps (n=11041)

### 4h/4h

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.01551 | 5507 | -6.17bps | 47.3% |
| Q2 | -0.00420 | 5506 | -13.22bps | 45.5% |
| Q3 | -0.00012 | 5506 | -9.55bps | 42.8% |
| Q4 | 0.00376 | 5506 | -7.73bps | 44.0% |
| Q5 | 0.01457 | 5506 | 2.74bps | 50.7% |

- Always: -6.79bps (n=27531)
- Extremes only (Q1∪Q5): -1.72bps (n=11013)

### 1h/4h

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.00779 | 5511 | -8.56bps | 47.7% |
| Q2 | -0.00200 | 5510 | -13.40bps | 43.3% |
| Q3 | -0.00002 | 5511 | -13.55bps | 40.8% |
| Q4 | 0.00190 | 5510 | -7.75bps | 44.8% |
| Q5 | 0.00758 | 5511 | 0.40bps | 49.5% |

- Always: -8.57bps (n=27553)
- Extremes only (Q1∪Q5): -4.08bps (n=11022)

### 1h/15m

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.00778 | 5523 | -9.88bps | 36.6% |
| Q2 | -0.00200 | 5522 | -11.16bps | 25.8% |
| Q3 | -0.00002 | 5523 | -11.57bps | 20.1% |
| Q4 | 0.00189 | 5522 | -10.71bps | 25.5% |
| Q5 | 0.00757 | 5523 | -9.94bps | 35.2% |

- Always: -10.65bps (n=27613)
- Extremes only (Q1∪Q5): -9.91bps (n=11046)

### 4h/15m

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.01549 | 5519 | -10.66bps | 35.0% |
| Q2 | -0.00419 | 5518 | -11.03bps | 26.3% |
| Q3 | -0.00010 | 5518 | -11.13bps | 20.4% |
| Q4 | 0.00377 | 5518 | -10.92bps | 25.7% |
| Q5 | 0.01457 | 5518 | -10.30bps | 34.9% |

- Always: -10.81bps (n=27591)
- Extremes only (Q1∪Q5): -10.48bps (n=11037)

### 15m/1h

| Q | signal_mean | n | expectancy | hit_rate |
|---:|---:|---:|---:|---:|
| Q1 | -0.00384 | 5506 | -8.68bps | 43.3% |
| Q2 | -0.00100 | 5506 | -11.23bps | 36.7% |
| Q3 | -0.00002 | 5505 | -12.02bps | 33.0% |
| Q4 | 0.00096 | 5506 | -9.37bps | 37.0% |
| Q5 | 0.00381 | 5506 | -8.94bps | 43.2% |

- Always: -10.05bps (n=27529)
- Extremes only (Q1∪Q5): -8.81bps (n=11012)

## Volatility regime filter (taker/taker, ON/OFF)

Trade only when `rvol_1h` ∈ Q1–Q3 (low/mid) vs always vs high vol Q4–Q5. No threshold tuning.

| L/H | always | rvol Q1–Q3 | rvol Q4–Q5 | helps? |
|---|---:|---:|---:|:---:|
| 1h/1h | -9.38bps | -10.77bps | -7.30bps | n |
| 4h/4h | -6.79bps | -10.11bps | -1.80bps | n |
| 1h/4h | -8.57bps | -11.37bps | -4.37bps | n |
| 1h/15m | -10.65bps | -10.86bps | -10.34bps | n |
| 4h/15m | -10.81bps | -10.99bps | -10.53bps | n |
| 15m/1h | -10.07bps | -11.34bps | -8.16bps | n |

## Continuation

Next: archive in `docs/RESEARCH_BACKLOG.md` with breakeven numbers. Do **not** build a reversion strategy from this family.
