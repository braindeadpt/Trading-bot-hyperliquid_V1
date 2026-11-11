# Feature Screening — 24m candle-only re-screen

Generated: 2026-08-09T23:31:41.245498+00:00
DB: `data/research/binance_spot_proxy.db`
Symbols: BTC, ETH, SOL, HYPE
Bars: 252,283 · unique dates: **731** · span 2024-08-09 → 2026-08-09
Inference: bar-level Spearman IC; **date-cluster bootstrap** p-values (n_boot=200, independent unit = UTC date); FDR BH α=0.05. Newey–West HAC p is diagnostic only. Do **not** aggregate feature/return to daily means before IC — that turns short-horizon fade into spurious day-momentum.

## Exclusions (declared)

- **funding:** funding_history empty in proxy DB; rate series not candle-OHLCV
- **oi:** oi_history empty; OI not in spot klines
- **basis:** binance_perp_prices empty; needs live perp mark
- **liquidations:** liquidation_events empty; short real-feed history
- **cvd_taker:** buy/sell volume not reliable on spot kline proxy; tape feed short
- `mins_to_funding_reset` retained as **calendar/clock** only (no funding-rate feed).

## Pipeline controls

- Horizon 15m: positive control rank **#1/19** (IC=0.3765, n_dates=731)
- Horizon 15m: negative |IC| max=0.0013 (mean=0.0010)
- Horizon 1h: positive control rank **#1/19** (IC=0.8137, n_dates=731)
- Horizon 1h: negative |IC| max=0.0023 (mean=0.0018)
- Horizon 4h: positive control rank **#1/19** (IC=0.3853, n_dates=731)
- Horizon 4h: negative |IC| max=0.0022 (mean=0.0021)
- Horizon 24h: positive control rank **#1/19** (IC=0.1572, n_dates=730)
- Horizon 24h: negative |IC| max=0.0045 (mean=0.0020)

**Validation:** positive near-top: PASS; negatives |IC|≈0: PASS

## TOP survivors (strict gate)

Gate: FDR + |mono|≥0.8 + same-sign IC in **≥5/6** date blocks + **≥2/3** vol regimes + ≥3/4 symbols.

| feature | h | IC | p_date | q_FDR | mono | blocks | regimes | sym | n_dates |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ret_lag_15m | 15m | -0.0402 | 5.00e-03 | 0.025 | -1.00 | 6/6 | 3/3 | 4/4 | 731 |
| ret_lag_15m | 1h | -0.0300 | 1.00e-02 | 0.043 | -0.90 | 6/6 | 3/3 | 4/4 | 731 |
| rvol_1h | 4h | 0.0196 | 1.00e-02 | 0.043 | 1.00 | 5/6 | 3/3 | 4/4 | 731 |
| atr_percentile_7d | 1h | 0.0138 | 5.00e-03 | 0.025 | 1.00 | 5/6 | 3/3 | 4/4 | 730 |
| bb_width | 1h | 0.0104 | 5.00e-03 | 0.025 | 0.90 | 6/6 | 3/3 | 4/4 | 731 |

## Cost test (survivors + FDR∩mono + prior-82d candle cells)

| feature | h | why | BE RT bps | edge mean | CI vs 11bps | n_dates | clears? |
|---|---|---|---:|---:|---|---:|:---:|
| atr_percentile_7d | 1h | survivor | -2.05 | -12.51 | [-17.8, -7.2] | 730 | n |
| atr_percentile_7d | 24h | fdr_mono_or_prior82d | -4.94 | -16.67 | [-40.7, 7.4] | 729 | n |
| bb_width | 1h | survivor | 1.85 | -9.63 | [-14.2, -4.0] | 731 | n |
| dow | 24h | fdr_mono_or_prior82d | -12.68 | -22.29 | [-45.2, 1.8] | 730 | n |
| ret_lag_15m | 15m | survivor | 4.11 | -6.92 | [-9.4, -4.1] | 731 | n |
| ret_lag_15m | 1h | survivor | 5.56 | -5.37 | [-10.0, -0.6] | 731 | n |
| ret_lag_1h | 1h | fdr_mono_or_prior82d | 2.20 | -9.03 | [-13.0, -4.1] | 731 | n |
| ret_lag_4h | 4h | fdr_mono_or_prior82d | 2.84 | -9.06 | [-16.5, -1.4] | 731 | n |
| rvol_1h | 4h | survivor | 6.81 | -4.41 | [-13.4, 4.9] | 731 | n |

## Verdict

### **(C)** — Feature(s) survived statistical gates but failed the 11 bps cost test (or CI straddles zero) — not exploitable at bot taker costs.

Five cells clear FDR + mono + ≥5/6 blocks + ≥2/3 regimes (`ret_lag_15m@{15m,1h}`,
`rvol_1h@4h`, `atr_percentile_7d@1h`, `bb_width@1h`). **None** clear taker RT 11 bps:
best survivor BE is `rvol_1h@4h` at **6.81 bps**; best fade BE is `ret_lag_15m@1h` at
**5.56 bps**. Date-block edge CIs are ≤0 or straddle zero after costs.

**Practical conclusion:** on 24 months / ~731 dates, candle-derived directional
features do not beat the bot’s taker cost book. Reorient toward inverted-cost
families (market making / spread capture) — L2 recording on `E:` is the enabling
path; do not build another candle-fade or vol-drag strategy.

## Comparison vs 82-day screening

| 82d TOP | 24m candle status |
|---|---|
| dow@24h (IC=+0.219, calendar) | IC=0.0357, FDR=n, blocks=5/6, regimes=2/3, survives=False |
| atr_percentile_7d@24h (IC=-0.159, revalidated (C)) | IC=0.0011, FDR=n, blocks=3/6, regimes=1/3, survives=False |
| oi_delta_24h@24h (IC=+0.135, EXCLUDED here (OI feed)) | excluded (OI feed) |
| ret_lag_4h@4h (IC=-0.065, cost-closed) | IC=-0.0200, FDR=Y, blocks=5/6, regimes=3/3, survives=False (BE 2.84≪11) |
| ret_lag_1h@1h (IC=-0.064, cost-closed) | IC=-0.0308, FDR=Y, blocks=6/6, regimes=3/3, survives=False (BE 2.20≪11) |

**What the longer sample changed:**

1. **`atr_percentile_7d@24h` and `dow@24h`** — 82d “TOP” cells collapse under
   date-cluster inference on 731 dates (artifacts / unstable). Matches the
   dedicated ATR long revalidation **(C)**.
2. **`ret_lag_*` mean reversion** — still real (negative IC, FDR, monotone on
   short horizons, stable across blocks/regimes/symbols) but **economically
   dead** at 11 bps (BE ~2–6 bps), same lesson as the short-horizon cost test.
3. **Small positive vol/width ICs at 1h–4h** appear with power that n≈70 dates
   could not resolve; they still fail costs (BE≤6.8 bps).
4. **`oi_delta`** not re-tested here (no OI in proxy DB).

## Full ranking (candidates)

| feature | h | IC | p_date | q_FDR | FDR | mono | blocks | regimes | sym | n_dates | n_bars |
|---|---|---:|---:|---:|:---:|---:|---:|---:|---:|---:|---:|
| ret_lag_15m | 15m | -0.0402 | 5.00e-03 | 0.025 | Y | -1.00 | 6/6 | 3/3 | 4/4 | 731 | 252275 |
| ret_lag_1h | 15m | -0.0390 | 5.00e-03 | 0.025 | Y | -0.60 | 6/6 | 3/3 | 4/4 | 731 | 252263 |
| dow | 24h | 0.0357 | 6.00e-01 | 0.813 | n | 0.50 | 5/6 | 2/3 | 4/4 | 730 | 251899 |
| ret_lag_24h | 4h | -0.0351 | 2.00e-02 | 0.080 | n | -0.10 | 5/6 | 2/3 | 4/4 | 730 | 251835 |
| dist_to_vwap_1d | 4h | -0.0315 | 5.00e-03 | 0.025 | Y | -0.30 | 6/6 | 2/3 | 4/4 | 731 | 252175 |
| ret_lag_1h | 1h | -0.0308 | 5.00e-03 | 0.025 | Y | -0.60 | 6/6 | 3/3 | 4/4 | 731 | 252251 |
| ret_lag_15m | 1h | -0.0300 | 1.00e-02 | 0.043 | Y | -0.90 | 6/6 | 3/3 | 4/4 | 731 | 252263 |
| dist_to_vwap_1d | 1h | -0.0289 | 5.00e-03 | 0.025 | Y | -0.30 | 6/6 | 3/3 | 4/4 | 731 | 252223 |
| hour_cos | 4h | 0.0283 | 3.00e-02 | 0.112 | n | 1.00 | 5/6 | 3/3 | 4/4 | 731 | 252219 |
| ret_lag_24h | 1h | -0.0276 | 5.00e-03 | 0.025 | Y | -0.10 | 6/6 | 3/3 | 4/4 | 730 | 251883 |
| ret_lag_4h | 15m | -0.0266 | 5.00e-03 | 0.025 | Y | -0.40 | 6/6 | 3/3 | 4/4 | 731 | 252215 |
| ret_lag_4h | 1h | -0.0256 | 6.00e-02 | 0.189 | n | -0.10 | 6/6 | 3/3 | 4/4 | 731 | 252203 |
| atr_percentile_7d | 4h | 0.0222 | 7.00e-02 | 0.210 | n | 0.80 | 5/6 | 1/3 | 4/4 | 730 | 251787 |
| dist_to_vwap_1d | 15m | -0.0217 | 5.00e-03 | 0.025 | Y | -0.30 | 6/6 | 3/3 | 4/4 | 731 | 252235 |
| dist_to_vwap_1d | 24h | -0.0211 | 1.40e-01 | 0.336 | n | -0.10 | 4/6 | 2/3 | 4/4 | 730 | 251855 |
| ret_lag_4h | 4h | -0.0200 | 5.00e-03 | 0.025 | Y | -0.20 | 5/6 | 3/3 | 4/4 | 731 | 252155 |
| rvol_1h | 4h | 0.0196 | 1.00e-02 | 0.043 | Y | 1.00 | 5/6 | 3/3 | 4/4 | 731 | 252203 |
| ret_lag_1h | 4h | -0.0173 | 8.00e-02 | 0.229 | n | -0.40 | 5/6 | 3/3 | 4/4 | 731 | 252203 |
| ret_lag_15m | 4h | -0.0141 | 5.00e-02 | 0.176 | n | -0.40 | 6/6 | 3/3 | 4/4 | 731 | 252215 |
| ret_lag_24h | 15m | -0.0139 | 2.60e-01 | 0.503 | n | -0.10 | 6/6 | 3/3 | 4/4 | 730 | 251895 |
| atr_percentile_7d | 1h | 0.0138 | 5.00e-03 | 0.025 | Y | 1.00 | 5/6 | 3/3 | 4/4 | 730 | 251835 |
| rvol_1h | 1h | 0.0126 | 9.90e-01 | 1.000 | n | 0.40 | 5/6 | 3/3 | 3/4 | 731 | 252251 |
| ret_lag_24h | 24h | -0.0125 | 5.20e-01 | 0.780 | n | -0.10 | 3/6 | 2/3 | 4/4 | 729 | 251515 |
| ret_lag_4h | 24h | -0.0121 | 5.00e-03 | 0.025 | Y | 0.00 | 4/6 | 2/3 | 4/4 | 730 | 251835 |
| rvol_24h | 24h | -0.0112 | 5.10e-01 | 0.780 | n | 0.80 | 4/6 | 2/3 | 3/4 | 730 | 251803 |
| bb_width | 1h | 0.0104 | 5.00e-03 | 0.025 | Y | 0.90 | 6/6 | 3/3 | 4/4 | 731 | 252191 |
| hour_cos | 1h | 0.0101 | 1.30e-01 | 0.336 | n | 0.80 | 5/6 | 3/3 | 4/4 | 731 | 252267 |
| dow | 4h | 0.0100 | 6.60e-01 | 0.843 | n | 0.20 | 4/6 | 2/3 | 4/4 | 731 | 252219 |
| adx_14 | 4h | -0.0085 | 8.20e-01 | 0.984 | n | -0.20 | 4/6 | 2/3 | 3/4 | 731 | 252115 |
| ret_lag_1h | 24h | -0.0084 | 2.20e-01 | 0.455 | n | 0.00 | 5/6 | 2/3 | 4/4 | 730 | 251883 |
| autocorr_ret_1d | 4h | 0.0080 | 5.50e-01 | 0.786 | n | 0.50 | 3/6 | 2/3 | 3/4 | 731 | 252119 |
| bb_width | 4h | 0.0079 | 2.00e-01 | 0.444 | n | 0.80 | 4/6 | 2/3 | 4/4 | 731 | 252143 |
| autocorr_ret_1d | 24h | -0.0076 | 1.00e+00 | 1.000 | n | 0.10 | 2/6 | 2/3 | 2/4 | 730 | 251799 |
| hour_sin | 4h | -0.0072 | 8.60e-01 | 0.985 | n | -0.40 | 3/6 | 2/3 | 4/4 | 731 | 252219 |
| hour_cos | 24h | -0.0072 | 6.40e-01 | 0.835 | n | 0.80 | 5/6 | 3/3 | 4/4 | 730 | 251899 |
| rvol_24h | 1h | 0.0071 | 3.40e-01 | 0.618 | n | 0.60 | 4/6 | 2/3 | 3/4 | 731 | 252171 |
| ret_lag_15m | 24h | -0.0064 | 1.20e-01 | 0.327 | n | -0.30 | 5/6 | 2/3 | 4/4 | 730 | 251895 |
| rvol_24h | 4h | 0.0058 | 1.40e-01 | 0.336 | n | 0.20 | 4/6 | 1/3 | 3/4 | 731 | 252123 |
| rvol_1h | 15m | 0.0053 | 5.50e-01 | 0.786 | n | 0.90 | 5/6 | 3/3 | 3/4 | 731 | 252263 |
| atr_percentile_7d | 15m | 0.0049 | 8.90e-01 | 0.989 | n | 0.70 | 4/6 | 2/3 | 4/4 | 730 | 251847 |
| autocorr_ret_1d | 1h | 0.0047 | 9.60e-01 | 1.000 | n | 0.50 | 5/6 | 2/3 | 3/4 | 731 | 252167 |
| dow | 1h | 0.0047 | 5.20e-01 | 0.780 | n | 0.20 | 3/6 | 2/3 | 3/4 | 731 | 252267 |
| hour_cos | 15m | 0.0043 | 4.80e-01 | 0.780 | n | 0.80 | 5/6 | 3/3 | 3/4 | 731 | 252279 |
| bb_width | 15m | 0.0033 | 2.20e-01 | 0.455 | n | 0.30 | 3/6 | 2/3 | 4/4 | 731 | 252203 |
| hour_sin | 1h | -0.0032 | 4.30e-01 | 0.754 | n | 0.30 | 4/6 | 2/3 | 4/4 | 731 | 252267 |
| bb_width | 24h | -0.0028 | 9.20e-01 | 1.000 | n | 0.90 | 2/6 | 2/3 | 1/4 | 730 | 251823 |
| hour_sin | 24h | 0.0028 | 9.80e-01 | 1.000 | n | 0.90 | 3/6 | 2/3 | 3/4 | 730 | 251899 |
| dow | 15m | 0.0025 | 6.00e-02 | 0.189 | n | 0.20 | 5/6 | 2/3 | 4/4 | 731 | 252279 |
| rvol_1h | 24h | -0.0024 | 8.50e-01 | 0.985 | n | 0.90 | 2/6 | 2/3 | 2/4 | 730 | 251883 |
| mins_to_funding_reset | 1h | 0.0023 | 2.00e-01 | 0.444 | n | 0.90 | 3/6 | 3/3 | 3/4 | 731 | 252267 |
| adx_14 | 1h | -0.0022 | 4.40e-01 | 0.754 | n | -0.10 | 4/6 | 2/3 | 3/4 | 731 | 252163 |
| mins_to_funding_reset | 15m | -0.0018 | 5.10e-01 | 0.780 | n | 0.50 | 3/6 | 3/3 | 4/4 | 731 | 252279 |
| hour_sin | 15m | -0.0018 | 9.80e-01 | 1.000 | n | 0.60 | 4/6 | 2/3 | 3/4 | 731 | 252279 |
| rvol_24h | 15m | 0.0017 | 6.10e-01 | 0.813 | n | 0.60 | 3/6 | 2/3 | 2/4 | 731 | 252183 |
| autocorr_ret_1d | 15m | 0.0015 | 6.10e-01 | 0.813 | n | 0.30 | 4/6 | 2/3 | 3/4 | 731 | 252179 |
| mins_to_funding_reset | 4h | -0.0015 | 7.50e-01 | 0.918 | n | 0.30 | 4/6 | 1/3 | 2/4 | 731 | 252219 |
| atr_percentile_7d | 24h | 0.0011 | 3.10e-01 | 0.581 | n | 0.70 | 3/6 | 1/3 | 2/4 | 729 | 251467 |
| adx_14 | 15m | 0.0009 | 2.60e-01 | 0.503 | n | -0.20 | 3/6 | 3/3 | 2/4 | 731 | 252175 |
| adx_14 | 24h | 0.0006 | 6.80e-01 | 0.850 | n | 0.00 | 3/6 | 2/3 | 2/4 | 730 | 251795 |
| mins_to_funding_reset | 24h | 0.0006 | 8.70e-01 | 0.985 | n | -0.30 | 4/6 | 2/3 | 3/4 | 730 | 251899 |

## Continuação

Arquivar. Não construir estratégia candle-direcional. Preferir famílias com
estrutura de custos invertida (MM / captura de spread) — já no backlog; dados L2
a acumular em `data/research/l2_books`.
