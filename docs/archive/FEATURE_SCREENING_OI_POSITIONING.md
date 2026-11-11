# Feature Screening — OI / positioning (Bybit proxy)

Generated: 2026-08-10T01:45:34.588362+00:00
OI DB: `data/research/hyperliquid.db` (source=bybit_open_interest (proxy))
Price DB: `data/research/binance_spot_proxy.db`
Span: 2024-08-09 → 2026-08-09 (731 dates)

## Limitations

- OI is Bybit linear perpetual open interest — not Hyperliquid-native.
- Forward returns from Binance spot 15m proxy (cross-venue).
- Maker fee assumption 1.5 bps/side (HL tier-0); maker RT 3 bps only evaluated if BE≥4.
- Prior 66d HL-native OI sample left oi_delta_24h INCONCLUSIVE (BE 19.6, CI straddled 0).

## Verdict: **(C)**

No OI/positioning feature cleared FDR+mono+block gates on the extended sample.

Family does not earn a strategy attempt.

## Controls

- CONTROL_POS_leaky_forward@1h: IC=0.8145 FDR_pass=True
- CONTROL_NEG_rand_a@1h: IC=0.0006 FDR_pass=True
- CONTROL_NEG_rand_b@1h: IC=0.0011 FDR_pass=True
- CONTROL_NEG_rand_c@1h: IC=0.0002 FDR_pass=True

## TOP / survivors

| feature | h | IC | q_FDR | mono | blocks | BE bps | clears 11? |
|---|---|---:|---:|---:|---:|---:|:---:|
_(none)_

## Full ranking (candidates)

| feature | h | IC | p_date | q_FDR | mono | n_dates | survives? |
|---|---|---:|---:|---:|---:|---:|:---:|
| oi_delta_1h | 15m | -0.0002 | 0.153 | 0.765 | 0.90 | 400 | n |
| oi_delta_1h | 1h | -0.0054 | 0.707 | 0.933 | -0.70 | 400 | n |
| oi_delta_1h | 4h | -0.0088 | 0.267 | 0.765 | -0.20 | 400 | n |
| oi_delta_1h | 24h | 0.0031 | 0.973 | 0.973 | -0.20 | 399 | n |
| oi_delta_4h | 15m | -0.0048 | 0.46 | 0.933 | -0.70 | 400 | n |
| oi_delta_4h | 1h | -0.0086 | 0.847 | 0.933 | -0.90 | 400 | n |
| oi_delta_4h | 4h | -0.0121 | 0.86 | 0.933 | -0.80 | 400 | n |
| oi_delta_4h | 24h | 0.0008 | 0.767 | 0.933 | -0.60 | 399 | n |
| oi_delta_24h | 15m | -0.0025 | 0.513 | 0.933 | -0.30 | 399 | n |
| oi_delta_24h | 1h | -0.0006 | 0.94 | 0.973 | -0.30 | 399 | n |
| oi_delta_24h | 4h | 0.0018 | 0.547 | 0.933 | -0.30 | 399 | n |
| oi_delta_24h | 24h | 0.0191 | 0.0733 | 0.765 | 0.20 | 398 | n |
| oi_accel_1h | 15m | 0.0037 | 0.567 | 0.933 | 0.70 | 400 | n |
| oi_accel_1h | 1h | 0.0053 | 0.267 | 0.765 | 1.00 | 400 | n |
| oi_accel_1h | 4h | 0.0001 | 0.8 | 0.933 | -0.10 | 400 | n |
| oi_accel_1h | 24h | -0.0015 | 0.8 | 0.933 | -0.60 | 399 | n |
| oi_z_7d | 15m | -0.0036 | 0.253 | 0.765 | -0.30 | 399 | n |
| oi_z_7d | 1h | -0.0034 | 0.547 | 0.933 | -0.70 | 399 | n |
| oi_z_7d | 4h | -0.0045 | 0.867 | 0.933 | -0.70 | 399 | n |
| oi_z_7d | 24h | -0.0047 | 0.82 | 0.933 | -0.90 | 398 | n |
| oi_price_div_1h | 15m | -0.0007 | 0.453 | 0.933 | nan | 400 | n |
| oi_price_div_1h | 1h | -0.0018 | 0.0867 | 0.765 | nan | 400 | n |
| oi_price_div_1h | 4h | 0.0024 | 0.00667 | 0.187 | nan | 400 | n |
| oi_price_div_1h | 24h | -0.0009 | 0.14 | 0.765 | nan | 399 | n |
| oi_rel_volume_1h | 15m | 0.0010 | 0.173 | 0.765 | 0.60 | 400 | n |
| oi_rel_volume_1h | 1h | -0.0049 | 0.6 | 0.933 | -0.40 | 400 | n |
| oi_rel_volume_1h | 4h | -0.0066 | 0.273 | 0.765 | -0.30 | 400 | n |
| oi_rel_volume_1h | 24h | 0.0052 | 0.74 | 0.933 | 0.00 | 399 | n |
