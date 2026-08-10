# Feature Screening — 24m price STRUCTURE (enlarged FDR family)

Generated: 2026-08-10T02:35:54.674061+00:00
DB: `data/research/binance_spot_proxy.db`
Symbols: BTC, ETH, SOL, HYPE
Bars: 252,283 · dates: **731** · 2024-08-09 → 2026-08-09
FDR family size: **176** cells (base candles 15 + structure 29 features × 4 horizons). BH α=0.05.
Expectation stated a priori: **low** survival odds (same candle information; derived strategies already FAIL with power).

## Confirmation lags (look-ahead contract)

Pivot confirmation **k=3** bars each side → a swing at index `i` is first usable at `i+k`. Trailing Donchian / Bollinger / breakout-vs-prior-window features have lag **0** (past window only).

| feature | confirmation lag (15m bars) |
|---|---:|
| `CONTROL_LOOKAHEAD_dist_future_high_20` | -20 **DELIBERATE FUTURE LEAK** |
| `bars_since_break_hi_100` | 0 |
| `bars_since_break_hi_20` | 0 |
| `bars_since_break_hi_50` | 0 |
| `bars_since_break_lo_100` | 0 |
| `bars_since_break_lo_20` | 0 |
| `bars_since_break_lo_50` | 0 |
| `bb_pctb_20` | 0 |
| `breakout_mag_hi_atr_100` | 0 |
| `breakout_mag_hi_atr_20` | 0 |
| `breakout_mag_hi_atr_50` | 0 |
| `breakout_mag_lo_atr_100` | 0 |
| `breakout_mag_lo_atr_20` | 0 |
| `breakout_mag_lo_atr_50` | 0 |
| `channel_slope_100` | 0 |
| `channel_slope_20` | 0 |
| `channel_slope_50` | 0 |
| `donchian_pos_100` | 0 |
| `donchian_pos_20` | 0 |
| `donchian_pos_50` | 0 |
| `range_compress_100` | 0 |
| `range_compress_20` | 0 |
| `range_compress_50` | 0 |
| `dist_nearest_sr_atr` | 3 |
| `dist_nearest_sr_pct` | 3 |
| `dist_pivot_hi_atr` | 3 |
| `dist_pivot_hi_pct` | 3 |
| `dist_pivot_lo_atr` | 3 |
| `dist_pivot_lo_pct` | 3 |
| `level_strength_touches` | 3 |

## Pipeline controls

- Horizon 15m: positive leak control rank **#1/49** (IC=0.3765)
- Horizon 15m: **look-ahead control** rank **#2/49** (IC=0.1000) — must be near top
- Horizon 15m: negative |IC| max=0.0013
- Horizon 1h: positive leak control rank **#1/49** (IC=0.8137)
- Horizon 1h: **look-ahead control** rank **#2/49** (IC=0.0728) — must be near top
- Horizon 1h: negative |IC| max=0.0023
- Horizon 4h: positive leak control rank **#1/49** (IC=0.3853)
- Horizon 4h: **look-ahead control** rank **#2/49** (IC=0.0454) — must be near top
- Horizon 4h: negative |IC| max=0.0022
- Horizon 24h: positive leak control rank **#1/49** (IC=0.1572)
- Horizon 24h: **look-ahead control** rank **#15/49** (IC=0.0108) — must be near top
- Horizon 24h: negative |IC| max=0.0045

**Validation:** look-ahead control near-top: FAIL; positive leak near-top: PASS; 15m: pos_ctrl=#1; 15m: lookahead_ctrl=#2; 1h: pos_ctrl=#1; 1h: lookahead_ctrl=#2; 4h: pos_ctrl=#1; 4h: lookahead_ctrl=#2; 24h: pos_ctrl=#1; 24h: lookahead_ctrl=#15

## TOP survivors (strict gate on enlarged FDR)

| feature | family | h | IC | p_date | q_FDR | mono | blocks | regimes | sym |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ret_lag_15m | candle | 15m | -0.0402 | 5.00e-03 | 0.029 | -1.00 | 6/6 | 3/3 | 4/4 |
| bb_pctb_20 | structure | 15m | -0.0396 | 5.00e-03 | 0.029 | -0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | structure | 1h | 0.0360 | 5.00e-03 | 0.029 | 0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | structure | 15m | 0.0349 | 5.00e-03 | 0.029 | 0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_atr | structure | 15m | 0.0343 | 5.00e-03 | 0.029 | 0.90 | 6/6 | 3/3 | 4/4 |
| ret_lag_15m | candle | 1h | -0.0300 | 1.00e-02 | 0.048 | -0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | structure | 4h | 0.0275 | 5.00e-03 | 0.029 | 1.00 | 5/6 | 3/3 | 4/4 |
| rvol_1h | candle | 4h | 0.0196 | 1.00e-02 | 0.048 | 1.00 | 5/6 | 3/3 | 4/4 |
| atr_percentile_7d | candle | 1h | 0.0138 | 5.00e-03 | 0.029 | 1.00 | 5/6 | 3/3 | 4/4 |
| bb_width | candle | 1h | 0.0104 | 5.00e-03 | 0.029 | 0.90 | 6/6 | 3/3 | 4/4 |

## Side distribution (survivors)

| feature | h | rule | % long | % short | uni? | action |
|---|---|---|---:|---:|:---:|---|
| rvol_1h | 4h | median_split | 50.0 | 50.0 | n | median_split (default) |
| atr_percentile_7d | 1h | median_split | 49.9 | 50.1 | n | median_split (default) |
| bb_width | 1h | median_split | 50.0 | 50.0 | n | median_split (default) |
| ret_lag_15m | 15m | median_split | 50.0 | 50.0 | n | median_split (default) |
| ret_lag_15m | 1h | median_split | 50.0 | 50.0 | n | median_split (default) |
| bb_pctb_20 | 15m | median_split | 50.0 | 50.0 | n | median_split (default) |
| dist_pivot_hi_atr | 15m | median_split | 50.0 | 50.0 | n | median_split (default) |
| dist_pivot_hi_pct | 15m | median_split | 50.0 | 50.0 | n | median_split (default) |
| dist_pivot_hi_pct | 1h | median_split | 50.0 | 50.0 | n | median_split (default) |
| dist_pivot_hi_pct | 4h | median_split | 50.0 | 50.0 | n | median_split (default) |

## Cost test

Taker RT **11 bps**. Maker (fee **1.5 bps/side**, RT 3.0 bps) only if gross BE ≥ 4.0 bps (optimistic — no fill/AS haircut in this pass).

| feature | h | rule | BE bps | edge | CI | %long | clears 11? | maker? |
|---|---|---|---:|---:|---|---:|:---:|---|
| rvol_1h | 4h | median_split | 2.08 | -9.45 | [-16.1, -1.1] | 50 | n | n/a |
| atr_percentile_7d | 1h | median_split | 1.19 | -10.03 | [-14.6, -4.8] | 50 | n | n/a |
| bb_width | 1h | median_split | 1.29 | -10.13 | [-14.1, -6.0] | 50 | n | n/a |
| ret_lag_15m | 15m | median_split | 4.10 | -6.93 | [-9.3, -4.1] | 50 | n | edge=1.1 clear_opt=n |
| ret_lag_15m | 1h | median_split | 5.67 | -5.21 | [-9.7, -0.7] | 50 | n | edge=2.8 clear_opt=n |
| bb_pctb_20 | 15m | median_split | 0.95 | -10.19 | [-12.2, -7.9] | 50 | n | n/a |
| dist_pivot_hi_atr | 15m | median_split | 1.37 | -9.69 | [-11.8, -7.4] | 50 | n | n/a |
| dist_pivot_hi_pct | 15m | median_split | 0.53 | -10.70 | [-13.2, -8.1] | 50 | n | n/a |
| dist_pivot_hi_pct | 1h | median_split | 2.10 | -9.52 | [-14.2, -4.7] | 50 | n | n/a |
| dist_pivot_hi_pct | 4h | median_split | 5.92 | -6.71 | [-14.1, 0.2] | 50 | n | edge=1.3 clear_opt=n |

## Verdict

### **(C)** — Structure features that cleared statistical gates failed the 11 bps cost test (or none cleared). Classic TA structure joins the closed candle-feature space.

## Comparison vs strategies already gated FAIL

| strategy | structure concept | gate result | feature-screen read |
|---|---|---|---|
| SFPReversion | S/R pivots | FAIL n=93/173 | Individual continuous structure features do not contradict strategy FAIL results: either no FDR/stability survival, or survival without economically meaningful BE — same information, same death. |
| VARejection | value-area / range position | FAIL n=44/99 | Individual continuous structure features do not contradict strategy FAIL results: either no FDR/stability survival, or survival without economically meaningful BE — same information, same death. |
| DonchianBreakout | Donchian channel breakout | FAIL B1=0.5/22 | Individual continuous structure features do not contradict strategy FAIL results: either no FDR/stability survival, or survival without economically meaningful BE — same information, same death. |
| VolatilityBreakout | BB breakout | FAIL B1=39 | Individual continuous structure features do not contradict strategy FAIL results: either no FDR/stability survival, or survival without economically meaningful BE — same information, same death. |
| VWAPTrend | anchored VWAP trend | FAIL PF 0.67 / 1498 trades | Individual continuous structure features do not contradict strategy FAIL results: either no FDR/stability survival, or survival without economically meaningful BE — same information, same death. |

## Structure-only ranking (candidates in FDR)

| feature | h | IC | p_date | q_FDR | FDR | mono | blocks | regimes | sym |
|---|---|---:|---:|---:|:---:|---:|---:|---:|---:|
| donchian_pos_20 | 15m | -0.0405 | 5.00e-03 | 0.029 | Y | -0.70 | 6/6 | 3/3 | 4/4 |
| bb_pctb_20 | 15m | -0.0396 | 5.00e-03 | 0.029 | Y | -0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | 1h | 0.0360 | 5.00e-03 | 0.029 | Y | 0.90 | 6/6 | 3/3 | 4/4 |
| donchian_pos_20 | 1h | -0.0356 | 5.00e-03 | 0.029 | Y | -0.60 | 6/6 | 3/3 | 4/4 |
| dist_pivot_lo_atr | 15m | -0.0352 | 5.00e-03 | 0.029 | Y | -0.70 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | 15m | 0.0349 | 5.00e-03 | 0.029 | Y | 0.90 | 6/6 | 3/3 | 4/4 |
| bb_pctb_20 | 1h | -0.0348 | 5.00e-03 | 0.029 | Y | -0.70 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_atr | 15m | 0.0343 | 5.00e-03 | 0.029 | Y | 0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_lo_pct | 15m | -0.0313 | 5.00e-03 | 0.029 | Y | -0.60 | 6/6 | 3/3 | 4/4 |
| donchian_pos_50 | 1h | -0.0303 | 5.00e-03 | 0.029 | Y | -0.10 | 6/6 | 3/3 | 4/4 |
| donchian_pos_50 | 15m | -0.0301 | 5.00e-03 | 0.029 | Y | -0.30 | 6/6 | 3/3 | 4/4 |
| bars_since_break_lo_50 | 4h | -0.0298 | 1.00e-02 | 0.048 | Y | -0.50 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_atr | 1h | 0.0298 | 5.00e-03 | 0.029 | Y | 0.80 | 6/6 | 3/3 | 4/4 |
| donchian_pos_100 | 1h | -0.0298 | 3.00e-02 | 0.115 | n | -0.10 | 6/6 | 3/3 | 4/4 |
| range_compress_100 | 4h | -0.0288 | 1.00e-01 | 0.279 | n | -1.00 | 4/6 | 3/3 | 4/4 |
| dist_pivot_lo_atr | 1h | -0.0286 | 2.00e-02 | 0.082 | n | -0.30 | 6/6 | 3/3 | 4/4 |
| channel_slope_50 | 4h | -0.0284 | 4.00e-02 | 0.150 | n | 0.00 | 5/6 | 2/3 | 4/4 |
| bars_since_break_lo_100 | 4h | -0.0277 | 7.00e-02 | 0.216 | n | -0.20 | 6/6 | 2/3 | 4/4 |
| donchian_pos_100 | 4h | -0.0276 | 5.00e-03 | 0.029 | Y | 0.50 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | 4h | 0.0275 | 5.00e-03 | 0.029 | Y | 1.00 | 5/6 | 3/3 | 4/4 |
| bars_since_break_hi_20 | 4h | 0.0254 | 1.00e-01 | 0.279 | n | 0.60 | 6/6 | 3/3 | 4/4 |
| bars_since_break_hi_50 | 4h | 0.0253 | 5.00e-03 | 0.029 | Y | 0.30 | 5/6 | 3/3 | 4/4 |
| donchian_pos_100 | 15m | -0.0247 | 1.10e-01 | 0.289 | n | -0.40 | 6/6 | 3/3 | 4/4 |
| bars_since_break_hi_20 | 1h | 0.0243 | 2.00e-02 | 0.082 | n | 0.90 | 6/6 | 3/3 | 4/4 |
| dist_pivot_lo_pct | 1h | -0.0229 | 2.00e-02 | 0.082 | n | -0.30 | 6/6 | 3/3 | 4/4 |
| bars_since_break_lo_50 | 1h | -0.0220 | 5.00e-03 | 0.029 | Y | -0.50 | 6/6 | 3/3 | 4/4 |
| channel_slope_20 | 4h | -0.0220 | 2.00e-01 | 0.414 | n | -0.50 | 4/6 | 2/3 | 4/4 |
| bars_since_break_lo_20 | 1h | -0.0220 | 5.00e-03 | 0.029 | Y | -0.30 | 6/6 | 3/3 | 4/4 |
| donchian_pos_50 | 4h | -0.0218 | 5.00e-02 | 0.176 | n | 0.60 | 5/6 | 3/3 | 4/4 |
| bb_pctb_20 | 4h | -0.0212 | 1.00e-02 | 0.048 | Y | -0.30 | 5/6 | 3/3 | 4/4 |
| bars_since_break_hi_100 | 4h | 0.0212 | 1.00e-02 | 0.048 | Y | -0.20 | 6/6 | 3/3 | 4/4 |
| donchian_pos_20 | 4h | -0.0203 | 2.00e-01 | 0.414 | n | 0.00 | 5/6 | 3/3 | 4/4 |
| bars_since_break_hi_50 | 1h | 0.0189 | 5.00e-03 | 0.029 | Y | 0.10 | 6/6 | 3/3 | 4/4 |
| bars_since_break_hi_20 | 15m | 0.0187 | 2.00e-02 | 0.082 | n | 0.70 | 6/6 | 3/3 | 4/4 |
| dist_pivot_hi_atr | 4h | 0.0171 | 1.30e-01 | 0.327 | n | 0.30 | 5/6 | 2/3 | 4/4 |
| channel_slope_50 | 1h | -0.0169 | 1.00e-02 | 0.048 | Y | 0.00 | 5/6 | 2/3 | 4/4 |
| dist_pivot_lo_atr | 4h | -0.0167 | 3.70e-01 | 0.632 | n | 0.00 | 6/6 | 3/3 | 4/4 |
| channel_slope_20 | 1h | -0.0165 | 1.10e-01 | 0.289 | n | 0.10 | 5/6 | 3/3 | 4/4 |
| bars_since_break_hi_100 | 24h | 0.0163 | 4.30e-01 | 0.670 | n | 0.30 | 5/6 | 2/3 | 3/4 |
| bars_since_break_lo_100 | 1h | -0.0162 | 6.00e-01 | 0.796 | n | -0.20 | 6/6 | 3/3 | 4/4 |
| range_compress_100 | 1h | -0.0156 | 2.00e-01 | 0.414 | n | -0.90 | 5/6 | 3/3 | 4/4 |
| dist_nearest_sr_pct | 1h | -0.0156 | 3.80e-01 | 0.637 | n | -0.90 | 5/6 | 3/3 | 4/4 |
| bars_since_break_lo_20 | 15m | -0.0154 | 1.00e-02 | 0.048 | Y | -0.30 | 6/6 | 3/3 | 4/4 |
| bars_since_break_lo_20 | 4h | -0.0152 | 8.00e-01 | 0.926 | n | 0.30 | 5/6 | 3/3 | 4/4 |
| dist_nearest_sr_pct | 4h | -0.0152 | 1.60e-01 | 0.366 | n | -0.90 | 6/6 | 3/3 | 4/4 |
| dist_nearest_sr_atr | 1h | -0.0150 | 7.00e-02 | 0.216 | n | -0.90 | 5/6 | 3/3 | 4/4 |
| channel_slope_100 | 4h | -0.0147 | 6.20e-01 | 0.796 | n | 0.70 | 6/6 | 1/3 | 4/4 |
| dist_nearest_sr_atr | 4h | -0.0140 | 8.00e-02 | 0.235 | n | -0.80 | 6/6 | 3/3 | 4/4 |
| dist_nearest_sr_pct | 24h | -0.0133 | 1.10e-01 | 0.289 | n | -0.40 | 6/6 | 2/3 | 4/4 |
| dist_nearest_sr_atr | 15m | -0.0132 | 5.60e-01 | 0.770 | n | -0.50 | 5/6 | 3/3 | 4/4 |
| range_compress_50 | 4h | -0.0132 | 5.00e-02 | 0.176 | n | -0.30 | 5/6 | 2/3 | 4/4 |
| dist_nearest_sr_pct | 15m | -0.0130 | 8.80e-01 | 0.961 | n | -0.90 | 5/6 | 3/3 | 4/4 |
| bars_since_break_hi_100 | 1h | 0.0127 | 1.70e-01 | 0.384 | n | -0.10 | 6/6 | 3/3 | 4/4 |
| dist_nearest_sr_atr | 24h | -0.0126 | 7.00e-02 | 0.216 | n | -0.80 | 6/6 | 2/3 | 4/4 |
| breakout_mag_lo_atr_50 | 4h | -0.0121 | 1.50e-01 | 0.347 | n | -0.70 | 4/6 | 3/3 | 4/4 |
| dist_pivot_hi_pct | 24h | 0.0120 | 8.00e-02 | 0.235 | n | 0.70 | 4/6 | 2/3 | 3/4 |
| dist_pivot_lo_pct | 24h | -0.0117 | 2.20e-01 | 0.430 | n | 0.00 | 5/6 | 2/3 | 4/4 |
| channel_slope_20 | 24h | -0.0114 | 7.50e-01 | 0.886 | n | 0.00 | 4/6 | 2/3 | 3/4 |
| dist_pivot_hi_atr | 24h | 0.0114 | 6.20e-01 | 0.796 | n | 0.70 | 5/6 | 2/3 | 4/4 |
| bars_since_break_lo_50 | 24h | -0.0111 | 7.60e-01 | 0.892 | n | 0.10 | 4/6 | 2/3 | 3/4 |
| bars_since_break_lo_50 | 15m | -0.0103 | 1.40e-01 | 0.333 | n | -0.60 | 6/6 | 3/3 | 4/4 |
| bars_since_break_hi_50 | 15m | 0.0096 | 2.70e-01 | 0.506 | n | 0.20 | 6/6 | 3/3 | 4/4 |
| breakout_mag_hi_atr_100 | 24h | 0.0096 | 7.20e-01 | 0.868 | n | 0.10 | 4/6 | 2/3 | 2/4 |
| donchian_pos_100 | 24h | -0.0096 | 3.00e-01 | 0.550 | n | 0.00 | 4/6 | 2/3 | 4/4 |
| donchian_pos_50 | 24h | -0.0096 | 6.30e-01 | 0.799 | n | 0.00 | 5/6 | 2/3 | 4/4 |
| bb_pctb_20 | 24h | -0.0096 | 3.00e-01 | 0.550 | n | 0.00 | 5/6 | 2/3 | 4/4 |
| range_compress_100 | 24h | -0.0093 | 2.10e-01 | 0.425 | n | -1.00 | 4/6 | 2/3 | 4/4 |
| dist_pivot_lo_pct | 4h | -0.0092 | 9.20e-01 | 0.970 | n | -0.10 | 4/6 | 2/3 | 3/4 |
| dist_pivot_lo_atr | 24h | -0.0091 | 1.40e-01 | 0.333 | n | -0.10 | 5/6 | 1/3 | 4/4 |
| channel_slope_20 | 15m | -0.0088 | 2.00e-02 | 0.082 | n | -0.10 | 5/6 | 3/3 | 4/4 |
| breakout_mag_lo_atr_50 | 1h | -0.0086 | 4.10e-01 | 0.662 | n | -0.40 | 5/6 | 3/3 | 4/4 |
| channel_slope_50 | 24h | -0.0085 | 3.70e-01 | 0.632 | n | 0.30 | 3/6 | 2/3 | 3/4 |
| donchian_pos_20 | 24h | -0.0084 | 3.00e-02 | 0.115 | n | -0.10 | 5/6 | 1/3 | 4/4 |
| range_compress_50 | 1h | -0.0082 | 2.00e-01 | 0.414 | n | -0.50 | 5/6 | 2/3 | 4/4 |
| breakout_mag_lo_atr_100 | 1h | -0.0079 | 5.80e-01 | 0.785 | n | -0.30 | 6/6 | 3/3 | 4/4 |
| bars_since_break_hi_20 | 24h | 0.0079 | 5.60e-01 | 0.770 | n | -0.40 | 4/6 | 2/3 | 3/4 |
| breakout_mag_lo_atr_100 | 4h | -0.0069 | 7.00e-01 | 0.850 | n | -0.30 | 5/6 | 3/3 | 3/4 |
| range_compress_50 | 24h | -0.0066 | 3.90e-01 | 0.648 | n | -1.00 | 3/6 | 2/3 | 4/4 |
| level_strength_touches | 24h | 0.0061 | 8.90e-01 | 0.961 | n | nan | 4/6 | 2/3 | 2/4 |
| breakout_mag_lo_atr_50 | 24h | -0.0055 | 3.50e-01 | 0.616 | n | -0.10 | 4/6 | 2/3 | 2/4 |
| … | | | | | | | | | (36 more in JSON) |

## Look-ahead audit note

`CONTROL_LOOKAHEAD_dist_future_high_20` is built with `Series.shift(-1).rolling(20)` — an intentional HIGH look-ahead. Causal structure features use only lag≥0 windows; pivots wait `k=3`. Run: `python scripts/lookahead_audit.py --paths scripts/feature_screening_24m_structure.py` and expect the deliberate control line to match LOOKAHEAD-001.
