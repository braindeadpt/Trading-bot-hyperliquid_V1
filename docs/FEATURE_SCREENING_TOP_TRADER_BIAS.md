# Feature screening probe — top-trader bias (level & delta)

Gerado: 2026-09-16 01:38 UTC · pipeline reutilizado: `screen_cell` (date-block bootstrap) + `benjamini_hochberg` + `survives_strict`.

## Amostra

Bias samples: 63280 (BTC=14781, ETH=14792, SOL=13257, HYPE=20450) · janela 2026-08-11 14:03 → 2026-09-16 01:35 UTC · grid 15m · candles: 13260 barras em 37 datas.

**Aviso de suficiência:** o gate estrito exige ≥20 datas (bootstrap), ≥6 subperíodos, ≥3 regimes e ≥3 símbolos. Com a janela atual (37 datas) o gate é **estruturalmente inatingível** — os ICs abaixo são evidência direcional, não decisão.

## Tabela de células (candidatas + controlos)

| feature | h | IC | p_NW | p_boot | n_bars | n_dates | mono | syms | per | reg | FDR | GATE |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tt_bias_level | 24h | -0.217 | 4.36e-04 | 1.20e-01 | 12876 | 36 | -1.00 | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 4h | -0.073 | 6.18e-03 | 2.85e-01 | 13196 | 36 | -1.00 | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 24h | 0.048 | 6.38e-02 | 3.15e-01 | 12812 | 36 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 1h | -0.041 | 2.72e-03 | 3.00e-02 | 13244 | 37 | -1.00 | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 15m | 0.029 | 6.63e-04 | 1.50e-02 | 13252 | 37 | nan | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 24h | 0.027 | 1.30e-01 | 6.50e-01 | 12860 | 36 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 24h | 0.023 | 1.14e-01 | 4.15e-01 | 12872 | 36 | nan | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 4h | 0.017 | 9.43e-02 | 6.55e-01 | 13192 | 36 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 15m | -0.017 | 5.68e-02 | 2.15e-01 | 13256 | 37 | -1.00 | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 1h | -0.016 | 1.66e-01 | 7.05e-01 | 13180 | 37 | nan | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 15m | 0.009 | 2.96e-01 | 4.35e-01 | 13240 | 37 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 1h | 0.008 | 3.24e-01 | 5.15e-01 | 13240 | 37 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 4h | 0.006 | 6.75e-01 | 2.05e-01 | 13180 | 36 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 1h | 0.003 | 7.48e-01 | 7.85e-01 | 13228 | 37 | nan | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 15m | 0.003 | 7.31e-01 | 8.80e-01 | 13192 | 37 | nan | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 4h | -0.001 | 9.50e-01 | 3.55e-01 | 13132 | 36 | nan | 1/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 1h | 0.780 | 0.00e+00 | 2.50e-03 | 13244 | 37 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 4h | 0.367 | 1.14e-143 | 2.50e-03 | 13196 | 36 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 15m | 0.357 | 0.00e+00 | 2.50e-03 | 13244 | 37 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 24h | 0.159 | 1.50e-30 | 2.50e-03 | 12876 | 36 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 1h | -0.012 | 1.67e-01 | 9.85e-01 | 13244 | 37 | -0.60 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 15m | -0.011 | 2.24e-01 | 9.60e-01 | 13256 | 37 | -0.70 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 1h | -0.010 | 2.74e-01 | 9.90e-01 | 13244 | 37 | -0.20 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 15m | -0.008 | 3.56e-01 | 5.40e-01 | 13256 | 37 | -0.60 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 15m | -0.006 | 5.25e-01 | 8.80e-01 | 13256 | 37 | -0.70 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 1h | -0.005 | 5.30e-01 | 5.25e-01 | 13244 | 37 | -0.20 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 24h | 0.005 | 5.72e-01 | 7.70e-01 | 12876 | 36 | 0.30 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 4h | -0.005 | 5.95e-01 | 8.60e-01 | 13196 | 36 | -0.10 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 4h | 0.003 | 7.34e-01 | 3.35e-01 | 13196 | 36 | 0.30 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 4h | -0.003 | 7.80e-01 | 8.70e-01 | 13196 | 36 | -0.30 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 24h | -0.002 | 8.01e-01 | 5.65e-01 | 12876 | 36 | -0.30 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 24h | 0.000 | 9.81e-01 | 6.70e-01 | 12876 | 36 | 0.10 | 1/4 | 0/0 | 0/0 | n | não |

## Veredito: NÃO sobrevive ao gate

Nenhuma célula passou `survives_strict`. Motivo dominante: amostra insuficiente (datas < 20 ⇒ p_boot indefinido ⇒ FDR sem rejeições). Nenhuma estratégia deve ser construída sobre este sinal até a janela de bias ≥ 20 datas.

**Requisitos para o gate:** re-correr quando `top_trader_bias_samples` cobrir ≥20 datas (≈3 semanas de polling a 60s). O script é idempotente — basta relançar com mais dados.
