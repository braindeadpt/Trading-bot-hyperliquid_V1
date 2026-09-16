# Top-Trader Bias Recheck — screening gate at ≥20 dates

_Generated 2026-09-16T01:38:57+00:00 by `scripts/research/top_trader_bias_recheck.py`._

**Trigger: `top_trader_bias_samples` covers 20 datas (63256 amostras, meta n_dates=37).**

## Candidate cells (sorted by |IC|)

| feature | h | IC | p_boot | n_dates | FDR | survives |
|---|---|---|---|---|---|---|
| `tt_bias_level` | 24h | -0.217 | 1.20e-01 | 36 | n | não |
| `tt_bias_level` | 4h | -0.073 | 2.85e-01 | 36 | n | não |
| `tt_bias_delta_4h` | 24h | 0.048 | 3.15e-01 | 36 | n | não |
| `tt_bias_level` | 1h | -0.041 | 3.00e-02 | 37 | n | não |
| `tt_bias_delta_15m` | 15m | 0.029 | 1.50e-02 | 37 | n | não |
| `tt_bias_delta_1h` | 24h | 0.027 | 6.50e-01 | 36 | n | não |
| `tt_bias_delta_15m` | 24h | 0.023 | 4.15e-01 | 36 | n | não |
| `tt_bias_delta_15m` | 4h | 0.017 | 6.55e-01 | 36 | n | não |
| `tt_bias_level` | 15m | -0.017 | 2.15e-01 | 37 | n | não |
| `tt_bias_delta_4h` | 1h | -0.016 | 7.05e-01 | 37 | n | não |
| `tt_bias_delta_1h` | 15m | 0.009 | 4.35e-01 | 37 | n | não |
| `tt_bias_delta_15m` | 1h | 0.008 | 5.15e-01 | 37 | n | não |
| `tt_bias_delta_1h` | 4h | 0.006 | 2.05e-01 | 36 | n | não |
| `tt_bias_delta_1h` | 1h | 0.003 | 7.85e-01 | 37 | n | não |
| `tt_bias_delta_4h` | 15m | 0.003 | 8.80e-01 | 37 | n | não |
| `tt_bias_delta_4h` | 4h | -0.001 | 3.55e-01 | 36 | n | não |

## Verdict

**GATE FAIL — nenhuma célula sobreviveu ao gate estrito**

## Context

* Trigger: `top_trader_bias_samples` cobrir ≥20 datas (≈3 semanas de polling).
* Below 20 datas the bootstrap gate is structurally unreachable — see 
  `docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md`.
