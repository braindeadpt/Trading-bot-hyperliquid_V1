# Feature Screening — BASIS & LIQUIDATIONS (valid windows only)

Generated: 2026-08-09T20:14:23.628926+00:00

## Contamination context

- Original full-window TOP exclusion of BASIS/LIQ is **invalid**: `merge_asof` silently forward-filled the last 2026-06-29 Binance perp price across ~40 subsequent days, and liq features saw zeros after the event table ended.
- **`liquidation_events` are 100% `source='proxy'`** (candle+OI heuristic). Zero Binance `@forceOrder` rows exist in `bot.db`. This re-screen therefore measures the *proxy*, not real liquidations.

## Windows

| family | start | end | rows | coverage note |
|---|---|---|---:|---|
| BASIS | 2026-05-30 | 2026-06-29 | 9496 | basis non-NaN 98% |
| LIQ (proxy) | 2026-06-08 | 2026-06-29 | 6907 | nonzero liq bars 27% |

## Verdicts

- **BASIS:** `NO_TOP` — powered sample but no FDR+mono+stab+symbol survivors
- **LIQ (proxy):** `NO_TOP` — powered sample but no FDR+mono+stab+symbol survivors

INCONCLUSIVE ≠ 'no edge'. **Real Binance liquidations remain untestable**
(zero `source='binance'` rows) — that is a separate INCONCLUSIVE, not the
proxy `NO_TOP` above.

## Survivors (if any)

### BASIS: none

### LIQ_proxy: none

## Full candidate cells

| family | feature | h | IC | p_raw | q_FDR | FDR | mono | n |
|---|---|---|---:|---:|---:|:---:|---:|---:|
| BASIS_valid | basis_z_7d | 24h | 0.1008 | 8.50e-02 | 0.255 | n | 0.70 | 8393 |
| BASIS_valid | basis | 24h | 0.0981 | 1.44e-01 | 0.320 | n | 0.70 | 9349 |
| BASIS_valid | basis | 4h | 0.0565 | 4.45e-02 | 0.255 | n | 0.60 | 9349 |
| BASIS_valid | basis_z_7d | 4h | 0.0553 | 2.50e-02 | 0.255 | n | 0.60 | 8393 |
| LIQ_proxy_valid | liq_notional_15m | 4h | 0.0437 | 6.49e-02 | 0.717 | n | nan | 6907 |
| LIQ_proxy_valid | bars_since_liq_cluster | 24h | 0.0345 | 4.05e-01 | 0.717 | n | 1.00 | 6075 |
| LIQ_proxy_valid | bars_since_liq_cluster | 4h | -0.0339 | 2.49e-01 | 0.717 | n | 0.00 | 6075 |
| LIQ_proxy_valid | liq_notional_15m | 24h | 0.0321 | 3.36e-01 | 0.717 | n | nan | 6907 |
| LIQ_proxy_valid | liq_side_imbalance_1h | 4h | 0.0272 | 1.70e-01 | 0.717 | n | 0.40 | 6907 |
| LIQ_proxy_valid | liq_notional_1h | 4h | 0.0249 | 3.90e-01 | 0.717 | n | 0.50 | 6907 |
| LIQ_proxy_valid | liq_notional_1h | 24h | 0.0243 | 5.82e-01 | 0.717 | n | 0.50 | 6907 |
| BASIS_valid | basis_velocity_1h | 4h | -0.0179 | 7.80e-02 | 0.255 | n | -0.30 | 9333 |
| BASIS_valid | basis_velocity_1h | 1h | -0.0162 | 1.60e-01 | 0.320 | n | -0.40 | 9333 |
| LIQ_proxy_valid | liq_side_imbalance_1h | 15m | 0.0155 | 2.01e-01 | 0.717 | n | -0.80 | 6907 |
| BASIS_valid | basis_z_7d | 1h | 0.0145 | 3.27e-01 | 0.560 | n | 0.70 | 8393 |
| LIQ_proxy_valid | liq_notional_15m | 15m | 0.0136 | 2.75e-01 | 0.717 | n | nan | 6907 |
| LIQ_proxy_valid | bars_since_liq_cluster | 15m | -0.0133 | 3.03e-01 | 0.717 | n | 0.00 | 6075 |
| LIQ_proxy_valid | bars_since_liq_cluster | 1h | -0.0115 | 5.38e-01 | 0.717 | n | -0.40 | 6075 |
| LIQ_proxy_valid | liq_side_imbalance_1h | 1h | 0.0115 | 4.79e-01 | 0.717 | n | 0.40 | 6907 |
| LIQ_proxy_valid | liq_notional_15m | 1h | 0.0107 | 5.08e-01 | 0.717 | n | nan | 6907 |
| BASIS_valid | basis | 1h | 0.0095 | 5.42e-01 | 0.813 | n | 0.20 | 9349 |
| LIQ_proxy_valid | liq_notional_1h | 1h | -0.0075 | 6.81e-01 | 0.726 | n | -1.00 | 6907 |
| LIQ_proxy_valid | liq_notional_1h | 15m | -0.0059 | 6.28e-01 | 0.718 | n | 0.50 | 6907 |
| BASIS_valid | basis | 15m | 0.0033 | 7.56e-01 | 0.889 | n | 0.10 | 9349 |
| BASIS_valid | basis_velocity_1h | 15m | -0.0025 | 8.17e-01 | 0.889 | n | -0.20 | 9333 |
| BASIS_valid | basis_velocity_1h | 24h | -0.0017 | 7.86e-01 | 0.889 | n | -0.10 | 9333 |
| BASIS_valid | basis_z_7d | 15m | 0.0015 | 8.89e-01 | 0.889 | n | -0.10 | 8393 |
| LIQ_proxy_valid | liq_side_imbalance_1h | 24h | -0.0010 | 9.64e-01 | 0.964 | n | 0.60 | 6907 |
