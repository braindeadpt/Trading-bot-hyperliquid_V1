# Feature Screening Report

> **CONTAMINATION NOTICE (2026-08-09):** Binance fstream dead since 2026-06-29.
> Full-window BASIS/LIQ TOP exclusions in this file are **invalid** (asof
> forward-fill / zero-fill). See `docs/FEED_CONTAMINATION_AUDIT.md` and the
> valid-window redo `docs/FEATURE_SCREENING_BASIS_LIQ_VALID.md` (BASIS=`NO_TOP`,
> LIQ proxy=`NO_TOP`; real liquidations still untestable). Mean-reversion and
> other non-fstream families in this report **stand**.

Generated: 2026-08-09T10:50:16.719981+00:00
DB: `data/live/bot.db`
Symbols: BTC, ETH, SOL, HYPE
Bar grid: 15m closed candles only
Horizons: 15m, 1h, 4h, 24h
Inference: Newey–West HAC on Spearman rank-products (lag = h−1 bars)
Multiple testing: Benjamini–Hochberg FDR α=0.05 on candidate×horizon

## Point-in-time guarantee

- Features at bar `t` use only that bar’s OHLCV / tape fields and `merge_asof(..., direction='backward')` for funding, OI, and Binance perp (timestamp ≤ bar timestamp).
- Forward return `r(t,t+h) = close[t+h]/close[t] − 1` on the same closed-bar grid; never used as a candidate feature.
- The positive control **intentionally** leaks `fwd_1h + noise` to validate ranking; it is excluded from the FDR family.
- No L2/orderbook history exists in `bot.db` — depth features are out of scope.

## Pipeline controls

- Horizon 15m: positive control rank **#1/40** (IC=0.3717, n=27536)
- Horizon 15m: negative controls |IC| max=0.0109 (mean=0.0051)
- Horizon 1h: positive control rank **#1/40** (IC=0.8069, n=27536)
- Horizon 1h: negative controls |IC| max=0.0094 (mean=0.0067)
- Horizon 4h: positive control rank **#1/40** (IC=0.3794, n=27488)
- Horizon 4h: negative controls |IC| max=0.0078 (mean=0.0063)
- Horizon 24h: positive control rank **#2/40** (IC=0.1696, n=27168)
- Horizon 24h: negative controls |IC| max=0.0081 (mean=0.0049)

**Validation:** positive control near top on every horizon: PASS; negative controls |IC|≈0 (max |IC|=0.0109): PASS.

## Continuation rule

Only features that survive FDR + monotonicity + temporal stability + cross-symbol consistency justify building a strategy. Flow:

`feature with predictive power → simple strategy around it → baseline-signal gate → shadow → execution (PASS required)`

Never the inverse (strategy first, feature rationalization later).

## TOP survivors

| feature | horizon | IC | p_raw | q_FDR | mono | stab | sym | n |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| dow | 24h | 0.2190 | 1.05e-07 | 0.000 | 0.90 | 3/3 | 4/4 | 27168 |
| atr_percentile_7d | 24h | -0.1593 | 5.07e-06 | 0.000 | -1.00 | 2/3 | 4/4 | 26736 |
| oi_delta_24h | 24h | 0.1349 | 2.62e-03 | 0.016 | 0.90 | 2/3 | 3/4 | 20661 |
| ret_lag_4h | 4h | -0.0649 | 1.09e-04 | 0.001 | -0.90 | 3/3 | 4/4 | 27424 |
| ret_lag_1h | 1h | -0.0635 | 1.23e-12 | 0.000 | -1.00 | 3/3 | 4/4 | 27520 |

Full survivor set (10 feature×horizon cells): the five above plus
`ret_lag_1h@{15m,4h}`, `ret_lag_4h@15m`, `rvol_1h@4h`, `ret_lag_15m@1h`
(all mean-reversion / short-horizon vol).

### How to read the TOP (not automatic strategy mandates)

1. **`dow` @ 24h (IC≈+0.22)** — strongest FDR hit, but it is a **calendar**
   effect: day-of-week is constant across a day while 24h forward windows
   overlap heavily. Newey–West mitigates autocorrelation; it does **not**
   prove an executable edge after costs. Treat as “market has weekday
   structure,” not “build DowStrategy.”
2. **`atr_percentile_7d` @ 24h (IC≈−0.16)** and **`rvol_1h` @ 4h** —
   high realized-vol regimes associate with weaker forward returns
   (vol drag / risk-off). Regime filter candidate, not a directional alpha
   by itself.
3. **`oi_delta_24h` @ 24h (IC≈+0.13)** — OI expansion co-moves with next-day
   returns on this window (3/4 symbols). Worth a **simple** long-OI-up /
   fade-OI-down probe only after a costs-aware baseline gate.
4. **`ret_lag_*` (IC≈−0.04 to −0.06)** — short-horizon **mean reversion** on
   15m–4h. Real, monotone, stable across symbols/periods. This is the most
   “strategy-shaped” survivor family, but HL fees/slippage often kill
   sub-1h MR — gate with costs, do not assume free alpha.
5. **What did *not* make TOP:** basis level/z (structurally interesting but
   failed mono and/or FDR on the available Binance-perp overlap), funding
   z-scores, liquidations, ADX, and **CVD divergence**
   (`cvd_price_div_signed` / `cvd_div_strength` — some FDR hits but
   non-monotone / degenerate quintiles; aligns with “weak directional
   info, not a strategy” in `docs/RESEARCH_BACKLOG.md`).

These TOP rows justify **measurement follow-ups / simple probes**, not
immediate `execution_strategies` additions. Next step for any chosen
feature: one minimal strategy → baseline-signal gate → shadow.

## Full ranking (candidates)

Sorted by \|IC\| descending. `fdr_reject` = survives BH at α=0.05. Significance **before** correction = `p_raw < 0.05`; **after** = `fdr_reject`.

| feature | h | IC | p_raw | q_FDR | p<0.05 | FDR | mono | Q means | stab | sym | n |
|---|---|---:|---:|---:|:---:|:---:|---:|---|---:|---:|---:|
| dow | 24h | 0.2190 | 1.05e-07 | 0.000 | Y | Y | 0.90 | -78.7bp,-28.1bp,-41.6bp,64.4bp,79.6bp | 3/3 | 4/4 | 27168 |
| atr_percentile_7d | 24h | -0.1593 | 5.07e-06 | 0.000 | Y | Y | -1.00 | 31.7bp,16.4bp,-14.3bp,-41.9bp,-82.3bp | 2/3 | 4/4 | 26736 |
| oi_delta_24h | 24h | 0.1349 | 2.62e-03 | 0.016 | Y | Y | 0.90 | -46.4bp,-5.8bp,33.2bp,22.6bp,46.0bp | 2/3 | 3/4 | 20661 |
| funding_pred_spread | 24h | -0.0911 | 3.36e-02 | 0.102 | Y | n | -0.40 | 24.2bp,-8.8bp,-38.6bp,-2.4bp | 1/3 | 4/4 | 23646 |
| rvol_24h | 24h | -0.0877 | 6.07e-02 | 0.153 | n | n | -0.90 | 11.4bp,-6.3bp,-4.1bp,-35.0bp,-57.6bp | 2/3 | 3/4 | 27072 |
| bb_width | 24h | -0.0817 | 2.22e-02 | 0.076 | Y | n | -0.90 | 18.2bp,-8.4bp,-26.1bp,-47.4bp,-27.9bp | 2/3 | 4/4 | 27092 |
| atr_percentile_7d | 4h | -0.0739 | 2.98e-05 | 0.000 | Y | Y | -0.80 | 0.7bp,10.0bp,0.3bp,-18.3bp,-7.5bp | 2/3 | 4/4 | 27056 |
| dow | 4h | 0.0734 | 7.63e-05 | 0.001 | Y | Y | 0.80 | -8.1bp,-10.4bp,-7.9bp,11.8bp,9.2bp | 3/3 | 4/4 | 27488 |
| rvol_1h | 24h | -0.0687 | 2.48e-02 | 0.083 | Y | n | -1.00 | 6.3bp,-5.6bp,-23.2bp,-25.3bp,-44.1bp | 2/3 | 4/4 | 27152 |
| basis_z_7d | 24h | -0.0686 | 9.89e-02 | 0.212 | n | n | -0.40 | 46.3bp,-27.8bp,-42.3bp,-52.9bp,12.9bp | 2/3 | 4/4 | 22735 |
| ret_lag_4h | 4h | -0.0649 | 1.09e-04 | 0.001 | Y | Y | -0.90 | 4.8bp,-2.4bp,0.5bp,-3.7bp,-13.7bp | 3/3 | 4/4 | 27424 |
| ret_lag_1h | 1h | -0.0635 | 1.23e-12 | 0.000 | Y | Y | -1.00 | 2.7bp,0.3bp,-0.9bp,-1.1bp,-4.7bp | 3/3 | 4/4 | 27520 |
| funding_level | 24h | 0.0617 | 1.67e-01 | 0.327 | n | n | 0.40 | -6.9bp,-72.4bp,-20.7bp,-7.8bp,24.2bp | 1/3 | 4/4 | 23646 |
| ret_lag_4h | 1h | -0.0596 | 1.27e-09 | 0.000 | Y | Y | -0.60 | 3.3bp,-1.5bp,-1.0bp,-0.8bp,-3.5bp | 3/3 | 4/4 | 27472 |
| basis_velocity_1h | 1h | -0.0565 | 4.18e-11 | 0.000 | Y | Y | -0.70 | 3.3bp,-0.4bp,-3.2bp,-0.6bp,-2.4bp | 3/3 | 4/4 | 24043 |
| ret_lag_1h | 15m | -0.0516 | 8.72e-15 | 0.000 | Y | Y | -0.90 | 1.1bp,-0.2bp,-0.4bp,-0.3bp,-1.1bp | 3/3 | 4/4 | 27532 |
| cvd_price_div_signed | 24h | 0.0485 | 1.11e-02 | 0.047 | Y | Y | nan | -18.4bp,nan,nan,nan,nan | 2/3 | 4/4 | 27152 |
| cvd_div_strength | 24h | 0.0485 | 1.11e-02 | 0.047 | Y | Y | nan | -18.4bp,nan,nan,nan,nan | 2/3 | 4/4 | 27152 |
| autocorr_ret_1d | 4h | 0.0483 | 8.47e-03 | 0.038 | Y | Y | 0.30 | -9.4bp,-2.1bp,-5.5bp,-9.5bp,12.1bp | 3/3 | 4/4 | 27388 |
| ret_lag_1h | 4h | -0.0479 | 5.90e-06 | 0.000 | Y | Y | -0.90 | 2.4bp,-2.6bp,-0.3bp,-3.3bp,-11.5bp | 3/3 | 4/4 | 27472 |
| dist_to_vwap_1d | 24h | 0.0460 | 2.46e-01 | 0.396 | n | n | 1.00 | -35.3bp,-26.2bp,-16.3bp,-13.3bp,-0.8bp | 1/3 | 3/4 | 27124 |
| hour_cos | 4h | -0.0449 | 1.85e-02 | 0.065 | Y | n | -0.60 | 1.4bp,-3.6bp,-2.6bp,0.1bp,-10.7bp | 2/3 | 4/4 | 27488 |
| ret_lag_4h | 15m | -0.0442 | 2.32e-11 | 0.000 | Y | Y | -0.90 | 0.3bp,-0.0bp,-0.4bp,-0.1bp,-0.7bp | 3/3 | 4/4 | 27484 |
| funding_z_30d | 24h | 0.0428 | 3.19e-01 | 0.500 | n | n | 0.60 | -50.5bp,0.2bp,-10.7bp,-35.2bp,21.3bp | 1/3 | 4/4 | 23266 |
| ret_lag_24h | 24h | 0.0428 | 3.25e-01 | 0.503 | n | n | 0.50 | -49.5bp,-8.6bp,-13.4bp,-7.7bp,-10.9bp | 1/3 | 3/4 | 26784 |
| funding_pred_spread | 4h | -0.0425 | 2.55e-02 | 0.083 | Y | n | -0.80 | 3.2bp,-0.4bp,-6.1bp,-2.1bp | 0/3 | 4/4 | 23966 |
| ret_lag_15m | 15m | -0.0422 | 2.12e-10 | 0.000 | Y | Y | -0.60 | 1.1bp,-0.1bp,-0.9bp,-0.8bp,-0.2bp | 3/3 | 4/4 | 27544 |
| dow | 1h | 0.0409 | 1.30e-05 | 0.000 | Y | Y | 0.70 | -1.6bp,-3.2bp,-1.9bp,2.0bp,3.3bp | 3/3 | 4/4 | 27536 |
| dist_to_vwap_1d | 1h | -0.0406 | 6.10e-05 | 0.001 | Y | Y | -0.60 | 0.1bp,-0.6bp,2.0bp,-2.6bp,-2.6bp | 3/3 | 4/4 | 27492 |
| rvol_1h | 4h | -0.0402 | 6.11e-03 | 0.030 | Y | Y | -1.00 | 2.9bp,-2.6bp,-2.6bp,-6.4bp,-6.6bp | 2/3 | 4/4 | 27472 |
| ret_lag_15m | 1h | -0.0387 | 6.71e-10 | 0.000 | Y | Y | -0.90 | 2.3bp,-0.3bp,-1.7bp,-1.6bp,-2.1bp | 3/3 | 4/4 | 27532 |
| bb_width | 4h | -0.0385 | 2.77e-02 | 0.087 | Y | n | -0.40 | 4.3bp,0.9bp,-8.3bp,-13.5bp,2.4bp | 3/3 | 3/4 | 27412 |
| cvd_price_div_signed | 4h | 0.0373 | 1.73e-04 | 0.001 | Y | Y | nan | -3.1bp,nan,nan,nan,nan | 2/3 | 4/4 | 27472 |
| cvd_div_strength | 4h | 0.0373 | 1.73e-04 | 0.001 | Y | Y | nan | -3.1bp,nan,nan,nan,nan | 2/3 | 4/4 | 27472 |
| basis_velocity_1h | 15m | -0.0365 | 1.86e-08 | 0.000 | Y | Y | -0.50 | 0.9bp,-0.4bp,-0.7bp,-0.0bp,-0.6bp | 3/3 | 4/4 | 24055 |
| dist_to_vwap_1d | 15m | -0.0356 | 5.33e-08 | 0.000 | Y | Y | -0.70 | -0.1bp,-0.3bp,0.8bp,-0.5bp,-0.9bp | 3/3 | 4/4 | 27504 |
| cvd_1h | 1h | -0.0340 | 7.46e-05 | 0.001 | Y | Y | -0.80 | 1.9bp,-1.6bp,0.0bp,-1.9bp | 2/3 | 3/4 | 27524 |
| basis_z_7d | 4h | -0.0319 | 9.11e-02 | 0.199 | n | n | 0.00 | 12.0bp,-12.3bp,-8.8bp,-2.1bp,1.0bp | 2/3 | 4/4 | 23055 |
| oi_delta_1h | 4h | -0.0284 | 3.67e-02 | 0.106 | Y | n | -0.70 | 1.4bp,1.3bp,4.8bp,0.5bp,-4.1bp | 2/3 | 3/4 | 21349 |
| funding_level | 4h | 0.0276 | 1.55e-01 | 0.310 | n | n | 0.90 | -5.5bp,-8.4bp,-2.4bp,-0.2bp,2.8bp | 0/3 | 3/4 | 23966 |
| atr_percentile_7d | 1h | -0.0272 | 5.30e-03 | 0.028 | Y | Y | -0.60 | -0.4bp,2.0bp,0.8bp,-5.6bp,-0.4bp | 3/3 | 4/4 | 27104 |
| basis_z_7d | 1h | -0.0270 | 6.19e-03 | 0.030 | Y | Y | -0.10 | 3.3bp,-3.3bp,-1.3bp,-0.3bp,-1.0bp | 2/3 | 4/4 | 23103 |
| adx_14 | 4h | -0.0267 | 1.27e-01 | 0.265 | n | n | -0.10 | 5.3bp,-5.1bp,-5.8bp,-4.9bp,-4.0bp | 2/3 | 3/4 | 27384 |
| autocorr_ret_1d | 1h | 0.0260 | 5.95e-03 | 0.030 | Y | Y | 0.30 | -2.5bp,0.0bp,-1.7bp,-3.4bp,4.1bp | 3/3 | 4/4 | 27436 |
| rvol_24h | 4h | -0.0254 | 1.80e-01 | 0.328 | n | n | -0.90 | 0.8bp,-0.7bp,-3.5bp,-3.4bp,-7.6bp | 2/3 | 2/4 | 27392 |
| liq_notional_15m | 4h | 0.0252 | 8.40e-02 | 0.193 | n | n | nan | -3.0bp,nan,nan,nan,nan | 2/3 | 4/4 | 27488 |
| dist_to_vwap_1d | 4h | -0.0249 | 1.94e-01 | 0.336 | n | n | -0.10 | -0.5bp,-7.2bp,3.8bp,-6.8bp,-4.0bp | 2/3 | 3/4 | 27444 |
| autocorr_ret_1d | 24h | 0.0248 | 5.25e-01 | 0.713 | n | n | 0.00 | -6.0bp,-41.7bp,-43.7bp,-45.9bp,45.7bp | 2/3 | 3/4 | 27068 |
| hour_cos | 24h | -0.0239 | 2.01e-01 | 0.345 | n | n | -0.90 | -15.9bp,-17.4bp,-17.8bp,-17.7bp,-23.1bp | 3/3 | 3/4 | 27168 |
| funding_chg_8h | 4h | -0.0233 | 1.88e-01 | 0.335 | n | n | -0.40 | 3.0bp,-1.8bp,-7.5bp,-8.7bp,1.3bp | 2/3 | 3/4 | 23838 |
| cvd_1h | 4h | -0.0230 | 3.48e-02 | 0.102 | Y | n | -0.40 | 0.8bp,-6.1bp,-0.5bp,-1.5bp | 2/3 | 3/4 | 27476 |
| cvd_4h | 1h | -0.0223 | 1.85e-02 | 0.065 | Y | n | -0.20 | 0.4bp,-1.5bp,-0.9bp,0.1bp | 2/3 | 4/4 | 27476 |
| ret_lag_15m | 4h | -0.0213 | 2.10e-04 | 0.001 | Y | Y | -0.70 | -0.9bp,-2.8bp,-2.6bp,-2.7bp,-6.2bp | 3/3 | 4/4 | 27484 |
| basis_velocity_1h | 4h | -0.0208 | 3.45e-02 | 0.102 | Y | n | -0.10 | 4.5bp,-2.3bp,-14.3bp,-2.1bp,0.3bp | 3/3 | 4/4 | 23995 |
| oi_delta_1h | 15m | -0.0207 | 4.74e-03 | 0.027 | Y | Y | -0.80 | 0.6bp,0.6bp,0.1bp,-0.9bp,-0.1bp | 3/3 | 4/4 | 21409 |
| liq_notional_15m | 24h | 0.0205 | 4.31e-01 | 0.621 | n | n | nan | -18.3bp,nan,nan,nan,nan | 1/3 | 2/4 | 27168 |
| cvd_4h | 15m | -0.0205 | 1.21e-03 | 0.008 | Y | Y | 0.20 | 0.1bp,-0.3bp,-0.5bp,0.1bp | 2/3 | 4/4 | 27488 |
| mins_to_funding_reset | 1h | -0.0196 | 2.70e-02 | 0.086 | Y | n | -0.30 | 0.4bp,-2.3bp,2.7bp,-2.9bp,-1.9bp | 2/3 | 4/4 | 27536 |
| mins_to_funding_reset | 15m | -0.0190 | 1.79e-03 | 0.011 | Y | Y | -0.60 | 0.4bp,-0.5bp,0.9bp,-1.1bp,-0.8bp | 3/3 | 4/4 | 27548 |
| funding_pred_spread | 1h | -0.0187 | 5.98e-02 | 0.153 | n | n | -0.60 | 0.3bp,0.5bp,-1.5bp,-0.5bp | 0/3 | 4/4 | 24014 |
| hour_sin | 24h | 0.0187 | 3.10e-01 | 0.491 | n | n | 0.60 | -19.0bp,-21.0bp,-20.7bp,-14.2bp,-16.7bp | 2/3 | 3/4 | 27168 |
| hour_cos | 1h | -0.0187 | 5.36e-02 | 0.143 | n | n | -0.30 | -0.8bp,0.5bp,-1.7bp,3.5bp,-5.1bp | 2/3 | 3/4 | 27536 |
| liq_notional_1h | 24h | 0.0186 | 5.86e-01 | 0.767 | n | n | nan | -18.3bp,nan,nan,nan,nan | 1/3 | 2/4 | 27168 |
| funding_chg_24h | 24h | 0.0185 | 5.64e-01 | 0.745 | n | n | 0.30 | 6.3bp,-24.7bp,-58.3bp,-18.8bp,21.0bp | 2/3 | 3/4 | 23262 |
| basis_z_7d | 15m | -0.0185 | 5.14e-03 | 0.028 | Y | Y | 0.00 | 1.3bp,-0.9bp,-0.7bp,-0.2bp,-0.2bp | 2/3 | 4/4 | 23115 |
| cvd_price_div_signed | 1h | 0.0179 | 1.63e-02 | 0.060 | Y | n | nan | -1.2bp,1.2bp,nan,nan,nan | 1/3 | 4/4 | 27520 |
| cvd_div_strength | 1h | 0.0179 | 1.63e-02 | 0.060 | Y | n | nan | -1.2bp,1.2bp,nan,nan,nan | 1/3 | 4/4 | 27520 |
| funding_chg_8h | 1h | -0.0173 | 7.23e-02 | 0.179 | n | n | -0.70 | 1.2bp,1.2bp,-2.0bp,-3.9bp,0.2bp | 2/3 | 4/4 | 23886 |
| cvd_1h | 24h | -0.0172 | 1.46e-01 | 0.297 | n | n | -0.40 | 4.5bp,-40.3bp,0.9bp,-2.9bp | 2/3 | 3/4 | 27156 |
| cvd_15m | 15m | -0.0172 | 6.71e-03 | 0.031 | Y | Y | -0.40 | 0.4bp,-0.6bp,0.4bp,-0.3bp | 3/3 | 3/4 | 27548 |
| rvol_1h | 1h | -0.0170 | 5.88e-02 | 0.153 | n | n | -0.90 | 1.2bp,-1.0bp,-0.8bp,-1.1bp,-2.0bp | 2/3 | 4/4 | 27520 |
| taker_buy_ratio | 15m | -0.0170 | 1.50e-02 | 0.058 | Y | n | -0.50 | 0.3bp,0.1bp,0.3bp,-0.6bp,0.2bp | 2/3 | 4/4 | 18434 |
| liq_notional_1h | 4h | 0.0157 | 3.56e-01 | 0.539 | n | n | nan | -3.0bp,nan,nan,nan,nan | 1/3 | 3/4 | 27488 |
| cvd_1h | 15m | -0.0155 | 1.44e-02 | 0.058 | Y | n | 0.40 | -0.0bp,-0.4bp,-0.0bp,0.1bp | 2/3 | 3/4 | 27536 |
| adx_14 | 24h | -0.0153 | 4.98e-01 | 0.683 | n | n | -0.10 | -4.2bp,-26.1bp,-22.1bp,-23.4bp,-15.8bp | 2/3 | 3/4 | 27064 |
| oi_delta_24h | 4h | 0.0151 | 4.74e-01 | 0.663 | n | n | 0.80 | -1.8bp,-3.3bp,2.8bp,2.7bp,6.9bp | 2/3 | 2/4 | 20981 |
| dow | 15m | 0.0147 | 1.27e-02 | 0.052 | Y | n | 0.70 | -0.4bp,-0.6bp,-0.6bp,0.6bp,0.7bp | 3/3 | 3/4 | 27548 |
| liq_side_imbalance_1h | 4h | 0.0143 | 1.75e-01 | 0.327 | n | n | nan | -3.3bp,1.4bp,nan,nan,nan | 2/3 | 4/4 | 27488 |
| rvol_24h | 1h | -0.0141 | 1.47e-01 | 0.297 | n | n | -0.80 | 0.3bp,0.3bp,-1.3bp,-0.9bp,-1.9bp | 2/3 | 4/4 | 27440 |
| ret_lag_24h | 1h | -0.0140 | 1.72e-01 | 0.327 | n | n | 0.50 | 0.3bp,-3.3bp,0.4bp,-1.6bp,0.5bp | 2/3 | 3/4 | 27152 |
| funding_chg_24h | 1h | -0.0131 | 1.77e-01 | 0.327 | n | n | -0.30 | 2.1bp,-0.4bp,-3.2bp,-2.0bp,0.3bp | 3/3 | 3/4 | 23630 |
| oi_price_divergence | 4h | -0.0127 | 1.69e-01 | 0.327 | n | n | nan | 0.8bp,nan,nan,nan,nan | 2/3 | 3/4 | 21349 |
| ret_lag_24h | 15m | -0.0127 | 5.35e-02 | 0.143 | n | n | -0.10 | 0.0bp,-0.8bp,0.2bp,-0.3bp,-0.0bp | 2/3 | 4/4 | 27164 |
| taker_buy_ratio | 1h | -0.0126 | 8.21e-02 | 0.193 | n | n | -0.30 | 1.2bp,-0.5bp,1.3bp,-1.3bp,0.2bp | 2/3 | 3/4 | 18422 |
| oi_delta_1h | 24h | 0.0125 | 3.75e-01 | 0.563 | n | n | 0.30 | -6.4bp,3.1bp,29.4bp,11.9bp,-6.2bp | 2/3 | 2/4 | 21029 |
| atr_percentile_7d | 15m | -0.0122 | 4.85e-02 | 0.134 | Y | n | -0.50 | -0.2bp,0.3bp,0.3bp,-0.8bp,-0.6bp | 3/3 | 4/4 | 27116 |
| funding_chg_8h | 15m | -0.0121 | 4.80e-02 | 0.134 | Y | n | -0.70 | 0.4bp,0.2bp,-0.6bp,-0.7bp,-0.1bp | 3/3 | 4/4 | 23898 |
| taker_buy_ratio | 4h | -0.0119 | 1.26e-01 | 0.265 | n | n | -0.30 | 2.6bp,-0.3bp,1.3bp,-0.1bp,0.0bp | 3/3 | 3/4 | 18374 |
| oi_delta_1h | 1h | -0.0119 | 2.48e-01 | 0.396 | n | n | -0.60 | 0.6bp,0.1bp,1.5bp,-1.0bp,-0.1bp | 3/3 | 2/4 | 21397 |
| cvd_15m | 1h | -0.0112 | 8.45e-02 | 0.193 | n | n | 0.00 | 0.8bp,-1.8bp,1.5bp,-1.2bp | 2/3 | 3/4 | 27536 |
| funding_chg_24h | 15m | -0.0108 | 8.22e-02 | 0.193 | n | n | -0.30 | 0.6bp,-0.2bp,-0.8bp,-0.5bp,0.1bp | 3/3 | 4/4 | 23642 |
| taker_buy_ratio | 24h | -0.0106 | 2.13e-01 | 0.360 | n | n | -0.30 | 16.2bp,2.5bp,-4.2bp,-4.0bp,7.7bp | 2/3 | 3/4 | 18054 |
| hour_cos | 15m | -0.0105 | 8.34e-02 | 0.193 | n | n | -0.10 | -0.6bp,0.5bp,-0.5bp,1.0bp,-1.3bp | 3/3 | 4/4 | 27548 |
| bars_since_liq_cluster | 24h | -0.0103 | 8.41e-01 | 0.911 | n | n | -0.10 | 7.8bp,96.3bp,-41.8bp,-12.8bp,11.8bp | 1/3 | 2/4 | 20417 |
| cvd_price_div_signed | 15m | 0.0102 | 9.11e-02 | 0.199 | n | n | nan | -0.3bp,0.5bp,nan,nan,nan | 2/3 | 3/4 | 27532 |
| cvd_div_strength | 15m | 0.0102 | 9.11e-02 | 0.199 | n | n | nan | -0.3bp,0.5bp,nan,nan,nan | 2/3 | 3/4 | 27532 |
| cvd_4h | 24h | -0.0100 | 6.40e-01 | 0.808 | n | n | -0.20 | 2.4bp,-39.7bp,-2.2bp,-0.3bp | 2/3 | 3/4 | 27108 |
| funding_level | 1h | 0.0083 | 4.14e-01 | 0.602 | n | n | 0.60 | -0.6bp,-2.8bp,-0.7bp,0.6bp,0.2bp | 0/3 | 3/4 | 24014 |
| liq_notional_15m | 15m | 0.0082 | 2.18e-01 | 0.363 | n | n | nan | -0.2bp,nan,nan,nan,nan | 2/3 | 2/4 | 27548 |
| autocorr_ret_1d | 15m | 0.0082 | 1.74e-01 | 0.327 | n | n | 0.20 | -0.5bp,-0.1bp,-0.7bp,-0.6bp,0.9bp | 3/3 | 3/4 | 27448 |
| funding_pred_spread | 15m | -0.0082 | 1.94e-01 | 0.336 | n | n | -0.60 | 0.0bp,0.2bp,-0.4bp,-0.1bp | 0/3 | 4/4 | 24026 |
| funding_z_30d | 1h | -0.0081 | 4.35e-01 | 0.621 | n | n | 0.30 | -1.3bp,0.3bp,0.2bp,-2.9bp,0.6bp | 3/3 | 3/4 | 23634 |
| adx_14 | 15m | 0.0080 | 1.83e-01 | 0.330 | n | n | 0.50 | -0.0bp,-0.5bp,-0.5bp,0.1bp,0.1bp | 2/3 | 3/4 | 27444 |
| funding_chg_8h | 24h | -0.0079 | 7.33e-01 | 0.863 | n | n | -0.30 | 20.9bp,-33.2bp,-43.1bp,-42.3bp,15.1bp | 1/3 | 3/4 | 23518 |
| basis | 24h | 0.0077 | 8.74e-01 | 0.919 | n | n | -0.30 | 22.0bp,-88.0bp,11.8bp,-18.4bp,-11.1bp | 1/3 | 1/4 | 23691 |
| liq_side_imbalance_1h | 15m | 0.0077 | 2.36e-01 | 0.387 | n | n | nan | -0.1bp,-0.9bp,nan,nan,nan | 2/3 | 3/4 | 27548 |
| rvol_24h | 15m | -0.0075 | 2.19e-01 | 0.363 | n | n | -0.90 | 0.0bp,0.1bp,-0.2bp,-0.2bp,-0.6bp | 2/3 | 3/4 | 27452 |
| cvd_4h | 4h | -0.0073 | 6.48e-01 | 0.811 | n | n | 0.40 | 1.3bp,-6.5bp,-2.9bp,1.3bp | 2/3 | 2/4 | 27428 |
| hour_sin | 4h | 0.0070 | 6.88e-01 | 0.833 | n | n | 0.20 | -1.3bp,-5.4bp,-4.8bp,-0.2bp,-3.6bp | 2/3 | 3/4 | 27488 |
| cvd_15m | 24h | -0.0065 | 3.85e-01 | 0.566 | n | n | 0.00 | 2.6bp,-41.7bp,10.0bp,-4.3bp | 2/3 | 4/4 | 27168 |
| liq_side_imbalance_1h | 1h | 0.0063 | 4.65e-01 | 0.656 | n | n | nan | -0.7bp,0.2bp,nan,nan,nan | 2/3 | 3/4 | 27536 |
| cvd_15m | 4h | -0.0060 | 3.83e-01 | 0.566 | n | n | 0.60 | -0.6bp,-6.7bp,1.6bp,-0.3bp | 2/3 | 3/4 | 27488 |
| basis | 4h | 0.0060 | 7.63e-01 | 0.879 | n | n | -0.30 | 4.8bp,-17.5bp,0.8bp,-1.5bp,-0.1bp | 1/3 | 2/4 | 24011 |
| rvol_1h | 15m | -0.0059 | 3.39e-01 | 0.519 | n | n | -0.30 | 0.1bp,-0.4bp,-0.3bp,-0.4bp,0.1bp | 2/3 | 4/4 | 27532 |
| ret_lag_4h | 24h | 0.0059 | 7.87e-01 | 0.887 | n | n | -0.10 | -27.5bp,-16.8bp,-7.6bp,-9.3bp,-30.5bp | 1/3 | 2/4 | 27104 |
| bb_width | 1h | -0.0059 | 5.35e-01 | 0.721 | n | n | 0.00 | 0.5bp,-0.0bp,-2.6bp,-3.8bp,2.4bp | 2/3 | 3/4 | 27460 |
| adx_14 | 1h | 0.0057 | 5.51e-01 | 0.734 | n | n | 0.50 | -0.2bp,-2.0bp,-2.4bp,0.9bp,0.2bp | 2/3 | 3/4 | 27432 |
| oi_price_divergence | 24h | -0.0053 | 6.13e-01 | 0.781 | n | n | nan | 6.4bp,nan,nan,nan,nan | 2/3 | 3/4 | 21029 |
| liq_notional_15m | 1h | 0.0045 | 6.08e-01 | 0.781 | n | n | nan | -0.7bp,nan,nan,nan,nan | 1/3 | 2/4 | 27536 |
| bars_since_liq_cluster | 1h | 0.0045 | 6.81e-01 | 0.831 | n | n | 0.20 | -0.8bp,4.4bp,-1.5bp,-0.3bp,0.4bp | 1/3 | 2/4 | 20785 |
| basis_velocity_1h | 24h | -0.0042 | 6.60e-01 | 0.819 | n | n | -0.10 | 13.4bp,-21.2bp,-64.2bp,-16.2bp,4.1bp | 1/3 | 3/4 | 23675 |
| funding_chg_24h | 4h | -0.0041 | 8.22e-01 | 0.903 | n | n | -0.10 | 6.1bp,-6.0bp,-14.7bp,-3.7bp,5.3bp | 1/3 | 2/4 | 23582 |
| mins_to_funding_reset | 4h | -0.0041 | 7.34e-01 | 0.863 | n | n | 0.60 | -7.2bp,-4.7bp,1.2bp,-0.4bp,-4.1bp | 2/3 | 3/4 | 27488 |
| mins_to_funding_reset | 24h | 0.0038 | 4.87e-01 | 0.674 | n | n | 0.70 | -20.8bp,-20.4bp,-16.9bp,-15.0bp,-18.5bp | 1/3 | 3/4 | 27168 |
| oi_delta_24h | 1h | 0.0037 | 7.44e-01 | 0.863 | n | n | 0.40 | -0.0bp,0.1bp,0.3bp,-0.4bp,2.0bp | 2/3 | 2/4 | 21029 |
| funding_z_30d | 4h | -0.0035 | 8.60e-01 | 0.917 | n | n | 0.40 | -5.9bp,-1.8bp,0.4bp,-7.3bp,1.6bp | 3/3 | 2/4 | 23586 |
| funding_level | 15m | 0.0033 | 6.08e-01 | 0.781 | n | n | 0.80 | -0.1bp,-0.7bp,-0.1bp,0.2bp,-0.0bp | 0/3 | 2/4 | 24026 |
| liq_notional_1h | 1h | -0.0032 | 7.39e-01 | 0.863 | n | n | nan | -0.7bp,nan,nan,nan,nan | 1/3 | 3/4 | 27536 |
| funding_z_30d | 15m | -0.0028 | 6.75e-01 | 0.831 | n | n | 0.30 | -0.4bp,0.1bp,-0.0bp,-0.7bp,0.3bp | 3/3 | 2/4 | 23646 |
| oi_delta_24h | 15m | -0.0026 | 7.13e-01 | 0.855 | n | n | 0.30 | 0.1bp,0.2bp,0.1bp,-0.3bp,0.5bp | 1/3 | 2/4 | 21041 |
| bars_since_liq_cluster | 4h | -0.0026 | 9.02e-01 | 0.935 | n | n | -0.10 | 0.5bp,14.1bp,-6.4bp,-1.7bp,1.7bp | 2/3 | 2/4 | 20737 |
| oi_price_divergence | 1h | -0.0024 | 7.75e-01 | 0.885 | n | n | nan | 0.2bp,nan,nan,nan,nan | 2/3 | 2/4 | 21397 |
| basis | 15m | -0.0017 | 7.88e-01 | 0.887 | n | n | -0.10 | 0.6bp,-1.1bp,-0.2bp,0.0bp,-0.1bp | 3/3 | 2/4 | 24071 |
| liq_side_imbalance_1h | 24h | 0.0017 | 8.75e-01 | 0.919 | n | n | nan | -20.4bp,14.4bp,nan,nan,nan | 1/3 | 2/4 | 27168 |
| ret_lag_1h | 24h | -0.0016 | 8.83e-01 | 0.921 | n | n | -0.30 | -26.3bp,-11.9bp,-6.0bp,-18.5bp,-29.3bp | 2/3 | 1/4 | 27152 |
| oi_price_divergence | 15m | 0.0016 | 8.20e-01 | 0.903 | n | n | nan | 0.1bp,nan,nan,nan,nan | 2/3 | 3/4 | 21409 |
| hour_sin | 15m | -0.0014 | 8.21e-01 | 0.903 | n | n | -0.30 | 0.1bp,-0.2bp,-0.3bp,-0.3bp,-0.1bp | 1/3 | 2/4 | 27548 |
| bb_width | 15m | -0.0012 | 8.36e-01 | 0.911 | n | n | 0.00 | 0.1bp,0.0bp,-0.7bp,-0.8bp,0.6bp | 2/3 | 3/4 | 27472 |
| bars_since_liq_cluster | 15m | -0.0012 | 8.59e-01 | 0.917 | n | n | 0.20 | -0.2bp,1.0bp,-0.4bp,-0.1bp,0.1bp | 2/3 | 2/4 | 20797 |
| basis | 1h | -0.0005 | 9.60e-01 | 0.973 | n | n | -0.10 | 1.5bp,-3.6bp,-0.7bp,-0.1bp,-0.1bp | 2/3 | 2/4 | 24059 |
| liq_notional_1h | 15m | 0.0004 | 9.46e-01 | 0.973 | n | n | nan | -0.2bp,nan,nan,nan,nan | 1/3 | 1/4 | 27548 |
| ret_lag_15m | 24h | 0.0003 | 9.56e-01 | 0.973 | n | n | -0.30 | -29.0bp,-12.7bp,-7.8bp,-13.2bp,-29.1bp | 1/3 | 3/4 | 27164 |
| hour_sin | 1h | -0.0003 | 9.79e-01 | 0.986 | n | n | -0.10 | 0.3bp,-1.2bp,-1.6bp,-1.1bp,0.2bp | 2/3 | 2/4 | 27536 |
| ret_lag_24h | 4h | 0.0001 | 9.95e-01 | 0.995 | n | n | 0.30 | -1.7bp,-6.4bp,-4.1bp,-5.7bp,2.9bp | 1/3 | 2/4 | 27104 |

## Control rows (excluded from FDR family)

| feature | h | IC | p_raw | mono | n | rank_in_h |
|---|---|---:|---:|---:|---:|---:|
| CONTROL_POS_leaky_forward | 15m | 0.3717 | 0.00e+00 | 1.00 | 27536 | 1 |
| CONTROL_NEG_rand_c | 15m | 0.0109 | 6.98e-02 | 0.50 | 27548 | 18 |
| CONTROL_NEG_rand_a | 15m | -0.0042 | 4.81e-01 | -0.50 | 27548 | 30 |
| CONTROL_NEG_rand_b | 15m | -0.0003 | 9.66e-01 | 0.70 | 27548 | 40 |
| CONTROL_POS_leaky_forward | 1h | 0.8069 | 0.00e+00 | 1.00 | 27536 | 1 |
| CONTROL_NEG_rand_b | 1h | -0.0094 | 1.24e-01 | -0.60 | 27536 | 26 |
| CONTROL_NEG_rand_a | 1h | -0.0084 | 1.62e-01 | -0.60 | 27536 | 27 |
| CONTROL_NEG_rand_c | 1h | 0.0025 | 6.81e-01 | 0.20 | 27536 | 37 |
| CONTROL_POS_leaky_forward | 24h | 0.1696 | 1.18e-67 | 1.00 | 27168 | 2 |
| CONTROL_NEG_rand_b | 24h | 0.0081 | 1.86e-01 | 0.70 | 27168 | 28 |
| CONTROL_NEG_rand_a | 24h | -0.0036 | 5.68e-01 | -0.30 | 27168 | 36 |
| CONTROL_NEG_rand_c | 24h | -0.0032 | 6.10e-01 | -0.20 | 27168 | 37 |
| CONTROL_POS_leaky_forward | 4h | 0.3794 | 0.00e+00 | 1.00 | 27488 | 1 |
| CONTROL_NEG_rand_a | 4h | -0.0078 | 2.05e-01 | -0.70 | 27488 | 29 |
| CONTROL_NEG_rand_c | 4h | -0.0058 | 3.43e-01 | -0.80 | 27488 | 34 |
| CONTROL_NEG_rand_b | 4h | 0.0053 | 3.81e-01 | 0.90 | 27488 | 35 |

## Notes

- Candidate tests in FDR family: 144 (features×horizons with finite p).
- Pre-FDR hits (p_raw<0.05): 52; post-FDR rejects: 34.
- CVD divergence is included as `cvd_price_div_signed` / `cvd_div_strength` (feature re-evaluation per RESEARCH_BACKLOG — not a CVDOrderFlow reopen).
- Basis coverage is limited to the Binance-perp overlap window; n_eff drops where `bn_perp` is missing.
