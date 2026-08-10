# Long-Horizon Directional Cost Test

Generated: 2026-08-09T21:23:11.139848+00:00
DB: `data/live/bot.db`
Symbols: BTC, ETH, SOL, HYPE
Span: 83.5 days · grid: closed 15m · exit: time stop only.

## Scope

Measurement only — no strategy, no gates, no tuning. Extends `docs/REVERSION_COST_TEST.md` (short-horizon fade, best BE **4.21 bps**) to 12h/24h and to the screening 24h survivors that are **not** reversion.

### Frozen rules

| id | signal | side rule | hold |
|---|---|---|---|
| `ret_lag_*` | closed-bar return over L | **fade** `−sign(signal)` | 12h/24h |
| `oi_delta_24h` | OI %Δ over 24h | **follow** `+sign(signal)` (IC>0) | 24h |
| `atr_percentile_7d` | ATR rank in 7d | **fade_half** short if >0.5 (IC<0) | 24h |
| `dow` | day-of-week 0–6 | split Mon–Wed short / Thu–Sun long (**reference only**) | 24h |

## Cost books

| book | RT |
|---|---:|
| gross_0bps | 0.00bps |
| maker_maker | 2.00bps |
| maker_taker | 6.50bps |
| taker_taker | 11.00bps |

`taker_taker` RT = **11.0 bps**.

## Power rule

Non-overlapping trades at 24h with ~83 days × 4 symbols ≈ 334 max. **If `n_nonoverlap` < 200, a green point estimate is INCONCLUSIVE (underpowered) — never an edge claim.** CIs are block-bootstrap means over the non-overlap trade sample (2000 resamples, seed=42).

## Verdict

### **(A)** — At least one long-horizon directional signal survives taker/taker with n_nonoverlap ≥ 200 and bootstrap CI on (gross − taker RT) clearing zero. First real candidate — build a minimal strategy and run the baseline-signal gate.

#### By family

- **ret_lag:** CLOSED — fade breakeven stays ≤ short-horizon 4.21 bps and turns more negative at 12h/24h; reversion is not exploitable at any tested horizon.
- **oi_delta_24h:** Point BE can exceed 11 bps on overlapping bars, but non-overlap edge CI includes large losses — not awarded A.
- **atr_percentile_7d:** Powered survivor (vol regime, NOT reversion) — see hits.
- **dow:** Reference only — excluded from eligibility.

- `atr_percentile_7d@24h`: BE=34.51bps, tt=23.51bps, n_no=282, edge CI=[2.47, 69.39] bps
- Caveat: n_nonoverlap is only modestly above the power floor (200); treat as a candidate to **gate**, not as a finished strategy.

## Results table

| id | family | n_obs | **n_nonoverlap** | gross E | **BE RT** | BE CI (non-olap) | 0bps | mm cons | **tt** | power |
|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|
| ret_lag_12h@12h | ret_lag | 27327 | **571** | -3.75bps | **-3.75bps** | [-26.76, 3.46] | -3.75bps | -10.17bps | -14.75bps | ok |
| ret_lag_24h@24h | ret_lag | 26934 | **282** | -7.96bps | **-7.96bps** | [-51.33, 16.46] | -7.96bps | -13.89bps | -18.96bps | ok |
| ret_lag_12h@24h | ret_lag | 27135 | **285** | -3.93bps | **-3.93bps** | [-34.38, 26.63] | -3.93bps | -10.19bps | -14.93bps | ok |
| ret_lag_4h@24h | ret_lag | 27235 | **286** | -0.56bps | **-0.56bps** | [-25.64, 40.16] | -0.56bps | -5.78bps | -11.56bps | ok |
| ret_lag_4h@12h | ret_lag | 27427 | **575** | 1.51bps | **1.51bps** | [-10.92, 20.26] | 1.51bps | -4.21bps | -9.49bps | ok |
| oi_delta_24h@24h | oi | 20825 | **219** | 19.62bps | **19.62bps** | [-28.13, 50.06] | 19.62bps | 12.93bps | 8.62bps | ok |
| atr_percentile_7d@24h | vol | 26875 | **282** | 34.51bps | **34.51bps** | [13.47, 80.39] | 34.51bps | 29.25bps | 23.51bps | ok |
| dow@24h† | calendar | 27332 | **286** | 34.74bps | **34.74bps** | [4.68, 70.20] | 34.74bps | 28.89bps | 23.74bps | ref |

† `dow` = descriptive reference only — never a strategy candidate.

### Edge vs taker (non-overlap bootstrap)

| id | mean(gross−11bps) | CI low | CI high | P(edge>0) |
|---|---:|---:|---:|---:|
| ret_lag_12h@12h | -22.15 | -37.76 | -7.54 | 0.002 |
| ret_lag_24h@24h | -28.69 | -62.33 | 5.46 | 0.053 |
| ret_lag_12h@24h | -15.38 | -45.38 | 15.63 | 0.169 |
| ret_lag_4h@24h | -4.03 | -36.64 | 29.16 | 0.414 |
| ret_lag_4h@12h | -6.58 | -21.92 | 9.26 | 0.203 |
| oi_delta_24h@24h | -0.46 | -39.13 | 39.06 | 0.480 |
| atr_percentile_7d@24h | 36.04 | 2.47 | 69.39 | 0.983 |
| dow@24h | 26.60 | -6.32 | 59.20 | 0.940 |

## `dow` caution (Task 3)

Twelve weeks ≈ **12 observations per weekday** before pooling symbols. Overlapping 24h forward windows inflate n_obs; Newey–West / bootstrap cannot invent information. The `dow` row is a **descriptive curiosity** only. It is excluded from verdict eligibility regardless of the number.

Observed: BE=34.74bps, n_nonoverlap=286, tt=23.74bps.

## Continuation

Next: minimal strategy around the powered hit → `baseline_signal_gate` → shadow. No parameter fishing.
