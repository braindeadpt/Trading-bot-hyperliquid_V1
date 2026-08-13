# IV (DVOL implícito) vs ADX router (realizado) — onde discordam

Gerado: 2026-08-13 10:47 UTC · janela 2026-05-18 -> 2026-08-07 · IV keep = DVOL pct(30d) > 66.7 · ADX keep = VB {expansion} / VWAP {low_vol, unknown} · ADX(14) 15m fechado (range 20 / trend 25).

## Concordância / discordância (mesmo conjunto de trades)

| célula | n | net | WR |
|---|---|---|---|
| ambos mantêm | 4 | +30.24 | 100.0% |
| IV mantém / ADX bloqueia | 9 | +12.75 | 44.4% |
| IV bloqueia / ADX mantém | 25 | -12.81 | 40.0% |
| ambos bloqueiam | 90 | -134.76 | 18.9% |

| keep-set | n | net |
|---|---|---|
| IV (DVOL) | 13 | +42.99 |
| ADX (realizado) | 29 | +17.43 |
| união | 38 | +30.17 |
| interseção (ambos) | 4 | +30.24 |

## Regime ADX × tercil IV (net / n)

| ADX \ IV | low_iv | mid_iv | high_iv | no_iv |
|---|---|---|---|---|
| low_vol | -41.8 / 25 | -9.0 / 16 | +24.4 / 4 | +0.0 / 0 |
| expansion | +10.4 / 8 | -18.6 / 10 | +17.8 / 3 | +0.0 / 0 |
| trend | -39.5 / 39 | -49.2 / 17 | +0.8 / 6 | +0.0 / 0 |

## Interpretação

1. **IV vence as duas células de discordância.** IV mantém 9 trades que o ADX
   bloquearia (**+12.75**) e bloqueia 25 que o ADX manteria (**−12.81**). Ou
   seja: onde discordam, o ADX está a bloquear vencedores e a manter sangria —
   o DVOL implícito prevê o sangramento melhor que o ADX realizado, nesta
   amostra.
2. **A união é pior que o IV sozinho.** IV keep-set = **+42.99 (n=13)**; ADX
   keep-set = +17.43 (n=29); união = +30.17 (n=38). Os 25 trades que o ADX
   acrescenta ao IV são **net negativos** (−12.81) — combiná-los dilui o gate.
   O ADX **não acrescenta** valor em cima do DVOL.
3. **A interseção é a célula mais limpa.** Ambos mantêm: n=4, **+30.24, WR
   100%** — high_iv **e** concordância ADX é o sinal mais forte por trade, mas
   com n=4 é estatisticamente irrelevante.
4. **A tabela conjunta explica o porquê.** A coluna high_iv é positiva em TODOS
   os regimes ADX (+17.8/+24.4/+0.8). O `low_vol` (a "casa" do VWAP no router
   ADX) só é positivo em high_iv (+24.4); os seus slices low/mid sangram
   (−41.8/−9.0). O router ADX mantém o VWAP exatamente nesses slices que o IV
   filtra. Leitura económica: o DVOL implícito **antecipa** o realizado (o IV
   sobe antes do ADX confirmar) — é por isso que ganha onde discordam.

## Veredito

**IV (DVOL implícito) vence nas células de discordância.** IV mantém/ADX
bloqueia net +12.75; IV bloqueia/ADX mantém net −12.81.

**Caveats honestos:** (a) n minúsculo nas células de discordância (9 e 25) e
tudo in-sample (a mesma janela 80d que gerou ambos os gates); (b) os dois
sinais são correlacionados (IV alto e ADX alto co-ocorrem), por isso a
"discordância" é onde desacoplam; (c) o +42.99 do IV continua a ser n=13 e
concentrado no pico de junho (ver `docs/IV_HIGH_ONLY_AB_SPLIT.md`). Isto é
uma evidência direcional de que o DVOL acrescenta informação ao gate de regime
— não uma promoção. O teste real é out-of-sample (08-07→08-13 e shadow-live).

## Contexto

* Ambas as leituras são sobre os mesmos trades (raw backtests, sem gate).
* DVOL é implícito (Deribit, diário, trailing 30d); ADX é realizado (candles 15m). Nenhuma mudança à janela congelada.
* JSON: `iv_vs_adx_disagreement_20260813_114742.json`.

