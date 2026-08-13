# Feature screening probe — top-trader bias (level & delta)

Gerado: 2026-08-13 03:45 UTC · pipeline reutilizado: `screen_cell` (date-block bootstrap) + `benjamini_hochberg` + `survives_strict`.

## Amostra

Bias samples: 6155 (BTC=1965, ETH=1930, SOL=272, HYPE=1988) · janela 2026-08-11 14:03 → 2026-08-13 03:44 UTC · grid 15m · candles: 600 barras em 3 datas.

**Aviso de suficiência:** o gate estrito exige ≥20 datas (bootstrap), ≥6 subperíodos, ≥3 regimes e ≥3 símbolos. Com a janela atual (3 datas) o gate é **estruturalmente inatingível** — os ICs abaixo são evidência direcional, não decisão.

## Tabela de células (candidatas + controlos)

| feature | h | IC | p_NW | p_boot | n_bars | n_dates | mono | syms | per | reg | FDR | GATE |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| tt_bias_level | 24h | -0.401 | 1.37e-01 | nan | 216 | 2 | -0.40 | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 24h | -0.196 | 2.41e-01 | nan | 152 | 2 | -0.30 | 4/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 24h | -0.183 | 5.87e-02 | nan | 200 | 2 | -0.30 | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 24h | -0.116 | 9.78e-02 | nan | 212 | 2 | -0.30 | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 4h | 0.078 | 4.53e-01 | nan | 472 | 2 | 0.30 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 1h | -0.065 | 3.09e-01 | nan | 520 | 3 | -0.80 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 15m | 0.064 | 1.10e-01 | nan | 580 | 3 | 0.60 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 15m | 0.064 | 1.14e-01 | nan | 592 | 3 | 0.40 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 1h | 0.054 | 2.15e-01 | nan | 580 | 3 | 0.40 | 2/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 1h | -0.045 | 4.17e-01 | nan | 568 | 3 | -0.80 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 1h | -0.044 | 5.08e-01 | nan | 584 | 3 | -0.50 | 1/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 4h | -0.038 | 7.82e-01 | nan | 536 | 2 | 0.20 | 1/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_15m | 4h | 0.020 | 6.99e-01 | nan | 532 | 2 | 0.00 | 1/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_4h | 15m | -0.007 | 8.72e-01 | nan | 532 | 3 | -0.40 | 3/4 | 0/0 | 0/0 | n | não |
| tt_bias_level | 15m | -0.006 | 8.74e-01 | nan | 596 | 3 | -0.10 | 0/4 | 0/0 | 0/0 | n | não |
| tt_bias_delta_1h | 4h | -0.003 | 9.61e-01 | nan | 520 | 2 | 0.00 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 1h | 0.861 | 5.41e-59 | nan | 584 | 3 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 4h | 0.481 | 4.94e-15 | nan | 536 | 2 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 15m | 0.397 | 4.16e-20 | nan | 584 | 3 | 1.00 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_POS_leaky_forward | 24h | 0.212 | 2.14e-05 | nan | 216 | 2 | 0.60 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 24h | -0.097 | 1.14e-01 | nan | 216 | 2 | -0.60 | 1/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 4h | 0.095 | 3.68e-02 | nan | 536 | 2 | 0.90 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 4h | 0.088 | 3.70e-02 | nan | 536 | 2 | 0.70 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 4h | 0.085 | 5.11e-02 | nan | 536 | 2 | 0.70 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 24h | 0.065 | 9.66e-03 | nan | 216 | 2 | 0.60 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 24h | -0.064 | 4.39e-01 | nan | 216 | 2 | -0.50 | 1/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 15m | 0.063 | 1.29e-01 | nan | 596 | 3 | 0.60 | 3/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 15m | 0.058 | 1.58e-01 | nan | 596 | 3 | 0.80 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_a | 1h | 0.057 | 1.67e-01 | nan | 584 | 3 | 0.90 | 4/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 1h | 0.028 | 5.19e-01 | nan | 584 | 3 | 0.70 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_c | 15m | -0.026 | 5.24e-01 | nan | 596 | 3 | -0.90 | 2/4 | 0/0 | 0/0 | n | não |
| CONTROL_NEG_rand_b | 1h | 0.020 | 6.31e-01 | nan | 584 | 3 | 0.30 | 2/4 | 0/0 | 0/0 | n | não |

## Veredito: NÃO sobrevive ao gate

Nenhuma célula passou `survives_strict`. Motivo dominante: amostra insuficiente (datas < 20 ⇒ p_boot indefinido ⇒ FDR sem rejeições). Nenhuma estratégia deve ser construída sobre este sinal até a janela de bias ≥ 20 datas.

**Requisitos para o gate:** re-correr quando `top_trader_bias_samples` cobrir ≥20 datas (≈3 semanas de polling a 60s). O script é idempotente — basta relançar com mais dados.
