# VB stop-out A/B — impacto do stop-out por liquidação calibrado no sangramento dos shorts

## Pergunta

O stop-out por liquidação (`liquidation_stop_out`), com o floor **calibrado ao
p90 real multi-venue (2.5M, 2026-08-14)**, corta o sangramento dos shorts do VB
(WR 7.7%, net -$66.32 — a maior fatia de perda do forense)?

## Método

`scripts/vb_stopout_ab.py` corre o **mesmo** backtest VB (config de produção,
janela congelada 05-18→08-07, BTC/ETH/SOL/HYPE) duas vezes:

| Run | Floor do stop-out | O que mede |
|---|---|---|
| **baseline (off)** | `float("inf")` — desligado | o trade set pré-stop-out, onde os shorts sangram |
| **calibrado (on)** | `2_500_000` (constante) | os mesmos trades com a janela de liquidações a poder sair primeiro |

O override é hash-neutral (`BacktestConfig.liquidation_stopout_min_notional_usd`,
default `None` → constante calibrada, paridade live/backtest intacta — pinado por
`TestLiquidationStopoutParity.test_stopout_floor_override_reaches_the_decision`).

## Resultado — ZERO interceptações

```
OVERALL   baseline: n= 83 WR=16.9% net=$  -81.79
          stop-out: n= 83 WR=16.9% net=$  -81.79     ← idêntico

SHORTS    baseline: n= 39 WR= 7.7% net=$  -66.32
          stop-out: n= 39 WR= 7.7% net=$  -66.32     ← idêntico

Shorts saídos por liquidation_stop_out: 0
Shorts baseline que o stop-out interceptou: 0
```

Os dois runs são **bit-a-bit idênticos** (83 trades, mesmos exit reasons, mesmo
PnL). O stop-out calibrado **não altera nada** no VB nesta janela.

## Porquê — o achado estrutural (não é falha do gate)

A causa não é o floor estar alto — é a **janela congelada não conter o feed de
liquidações**:

| Feed de liquidações | Cobertura | Dentro da janela 05-18→08-07? |
|---|---|---|
| **proxy** (estimado, ~30% do volume) | 06-08 → 06-29 | sim (parcial) |
| **okx + bybit reais** | 08-09 → hoje | **não** (começa 2 dias depois do fim da janela) |

Cruzamento temporal com os 39 shorts do VB (todos vivem 06-28 → 08-07):

* só **2 shorts** têm vida a sobrepor o feed proxy (06-28);
* **0 shorts** têm vida a sobrepor o feed real (08-09+);
* mesmo com sensibilidade máxima (floor 0), apenas **3 janelas** short-dominantes
  coincidem com um short aberto — e com o floor calibrado de 2.5M, **zero**.

O stop-out por liquidação é um gate alimentado pela janela de liquidações; numa
janela onde o único feed é o proxy (06-08→06-29, com overlap de 2 dias sobre os
trades) e os trades vivem maioritariamente em julho, o gate **não tem o que
comer**. O resultado nulo é a verdade dos dados, não uma limitação do harness.

## Leitura operacional

1. **O teste real do stop-out vive na PRÓXIMA janela.** 08-09+ já acumulou
   **13.268 eventos reais okx+bybit** (a calibração do floor usou-os). Quando a
   janela congelada se mover para incluir 08-09+, o mesmo harness mede o impacto
   com o feed real — é o teste que falta, não um re-tune do floor.
2. **Não há evidência aqui para re-calibrar o floor.** O p90 de 2.5M foi medido
   nos dados reais (correto); o nulo é falta de cobertura da janela, não excesso
   de floor.
3. **O harness está pronto e pinado.** O override `None → constante` preserva a
   paridade live/backtest (teste dedicado), e `--off`/`--on` permitem re-correr
   a A/B a qualquer momento com um `FULL_START/FULL_END` atualizado.

## Artefactos

* CSV baseline: `data/backtests/vb_stopout_off_20260814_121433.csv` (83 trades)
* CSV calibrado: `data/backtests/vb_stopout_on_20260814_122124.csv` (83 trades)
* JSON: `data/research/vb_stopout_ab.json` (gitignored)
* Script: `scripts/vb_stopout_ab.py` · testes: `tests/test_vb_stopout_ab.py`
