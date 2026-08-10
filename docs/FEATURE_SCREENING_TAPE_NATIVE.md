# Feature Screening — Tape-native CVD / aggression / real OIR

Generated: 2026-08-10T01:49:24.813197+00:00
DB (read-only): `data/research/hyperliquid.db`
Span: 2026-07-10 → 2026-08-10 (29 dates)

## Expectation (declared before results)

Flow features live on **short horizons** where statistical power is high but retail taker costs (11 bps) are implacable. Prior candle `ret_lag@15m` BE was ≤0.95 bps. This screen expects statistical signal and unlikely tradable edge — run to **close the space**, not to find a winner.

## Limitations

- Only ~1 month of real tape/OIR — date-cluster n is modest; block gate relaxed to 2/3.
- CVD/aggression aggregated to 15m from trade_tape side B/A.
- OIR from l2_snapshots metrics (real), not candle proxy.
- Prior CVDOrderFlow used candle-derived volume — this closes the real-tape variant.
- Maker fee 1.5 bps/side (corrected); only referenced if BE≥4.

## Verdict: **(C)**

2 statistical survivor(s); none clear 11 bps (as expected for short-horizon flow).

cvd_delta_1h@1h BE=3.64 CI=[-16.541510068493505, 2.8131005858824625]; cvd_delta_15m@15m BE=-9.42 CI=[-45.1722787440794, -6.223768510398461]

## Survivors + cost

| feature | h | IC | q_FDR | mono | BE bps | clears? |
|---|---|---:|---:|---:|---:|:---:|
| cvd_delta_1h | 1h | -0.0452 | 0.048 | -0.90 | 3.64 | n |
| cvd_delta_15m | 15m | -0.0248 | 0.048 | -0.60 | -9.42 | n |

## Full ranking

| feature | h | IC | p_date | q_FDR | mono | survives |
|---|---|---:|---:|---:|---:|:---:|
| cvd_delta_15m | 15m | -0.0248 | 0.004 | 0.048 | -0.60 | Y |
| cvd_delta_15m | 1h | -0.0180 | 0.016 | 0.077 | -0.10 | n |
| cvd_delta_15m | 4h | -0.0115 | 0.024 | 0.082 | -0.70 | n |
| cvd_delta_1h | 15m | -0.0282 | 0.024 | 0.082 | -0.10 | n |
| cvd_delta_1h | 1h | -0.0452 | 0.004 | 0.048 | -0.90 | Y |
| cvd_delta_1h | 4h | -0.0305 | 0.016 | 0.077 | -0.70 | n |
| aggr_imbalance_15m | 15m | -0.0204 | 0.072 | 0.216 | -0.40 | n |
| aggr_imbalance_15m | 1h | -0.0224 | 0.016 | 0.077 | -0.60 | n |
| aggr_imbalance_15m | 4h | -0.0093 | 0.28 | 0.611 | -0.40 | n |
| mean_trade_size_usd | 15m | 0.0089 | 0.792 | 0.968 | 0.30 | n |
| mean_trade_size_usd | 1h | 0.0019 | 0.968 | 0.968 | 0.40 | n |
| mean_trade_size_usd | 4h | -0.0185 | 0.48 | 0.768 | 0.90 | n |
| trade_intensity | 15m | 0.0052 | 0.936 | 0.968 | 0.50 | n |
| trade_intensity | 1h | -0.0139 | 0.248 | 0.611 | -0.80 | n |
| trade_intensity | 4h | -0.0243 | 0.736 | 0.968 | -0.90 | n |
| oir | 15m | 0.0220 | 0.4 | 0.727 | 0.80 | n |
| oir | 1h | 0.0197 | 0.896 | 0.968 | 0.80 | n |
| oir | 4h | 0.0382 | 0.424 | 0.727 | 0.70 | n |
| oir_chg_15m | 15m | 0.0104 | 0.856 | 0.968 | 0.40 | n |
| oir_chg_15m | 1h | 0.0099 | 0.512 | 0.768 | 0.40 | n |
| oir_chg_15m | 4h | 0.0047 | 0.936 | 0.968 | 0.20 | n |
| oir_chg_1h | 15m | 0.0260 | 0.264 | 0.611 | 0.60 | n |
| oir_chg_1h | 1h | 0.0126 | 0.816 | 0.968 | 0.60 | n |
| oir_chg_1h | 4h | 0.0227 | 0.32 | 0.640 | 0.70 | n |
