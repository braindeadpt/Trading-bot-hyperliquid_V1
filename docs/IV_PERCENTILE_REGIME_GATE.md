# IV-percentile regime gate (Deribit DVOL) — A/B sobre VB + VWAP

Gerado: 2026-08-13 04:22 UTC · janela 2026-05-18 -> 2026-08-07 · gate testado
VB>=p70 / VWAP<=p40 · DVOL window 30d · DVOL diário da API pública da Deribit
(BTC) + ETH vol index; SOL/HYPE usam o BTC como proxy global.

## Resultado (gate testado)

| métrica | sem gate | com gate | bloqueados | high_iv only |
|---|---|---|---|---|
| n | 128 | 29 | 99 | 13 |
| net | -104.59 | -19.87 | -84.72 | +42.99 |

**O gate testado poupa 84.72 USD** (81% do PnL total) — mas ver atribuição
por estratégia: o gate bloqueou VWAP **positivos** em high_iv.

**Variante 'high_iv only' (>p66, ambas as estratégias): +42.99 USD (n=13).**

## Por estratégia

| estratégia | sem gate | com gate | bloqueados |
|---|---|---|---|
| VolatilityBreakout | -81.79 (83) | +11.10 (3) | **-92.89 (80, WR 15%)** |
| VWAPDeviation | -22.80 (45) | -30.97 (26) | **+8.18 (19, WR 53%)** |

## A descoberta contraintuitiva

O gate testado assume **VB em IV alto, VWAP (fade) em IV baixo**. Os dados
dizem o **inverso para o VWAP**:

| regime IV | n | net | VB | VWAP |
|---|---|---|---|---|
| low_iv (<p33) | 72 | -70.80 | -35.68 (51) | -35.12 (21) |
| mid_iv (p33-66) | 43 | -76.78 | -53.79 (27) | -22.98 (16) |
| **high_iv (>p66)** | **13** | **+42.99** | **+7.68 (5)** | **+35.31 (8)** |

**high_iv é o único regime positivo — para AMBAS as estratégias.** O VWAP
fade, que a premissa do gate queria bloquear em IV alto, foi lá que ganhou
(+35.31, n=8). O gate testado bloqueou exatamente esses trades lucrativos
(os 19 VWAP bloqueados são +8.18, não negativos).

## Veredito

1. **O DVOL discrimina regimes — mas na direção oposta à assumida.** O
   regime high_iv (>p66 do percentil 30d) concentra todo o PnL positivo;
   low/mid_iv sangram em ambas as estratégias.
2. **O gate testado melhora o PnL total por acidente:** poupa porque bloqueia
   os 80 VB maus, mas erra no VWAP — os 19 trades que bloqueia são
   **positivos** (+8.18).
3. **Variante sugerida pelos dados:** gate "ambas apenas em high_iv" teria
   mantido 13 trades = **+42.99** (vs -19.87 do gate testado, vs -104.59 sem
   gate). Caveat: n=13 é pequeno e o high_iv é dominado pelo pico de junho.
4. **Sem tocar na janela congelada:** o DVOL é informação implícita nova
   (externa às candles), aplicada post-hoc ao nível do trade — nenhuma
   mudança ao settings.yaml.

**Próximo passo:** A/B da variante "ambas em high_iv" (p>66) com split por
janelas independentes, e comparação direta com o router ADX (realizado vs
implícito) para decidir se o DVOL acrescenta informação ao gate de regime
que o bot já tem.
