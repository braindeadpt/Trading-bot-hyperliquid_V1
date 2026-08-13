# VB long-only + follow-through — mesma amostra forense, nova simulação

**Data:** 2026-08-13 · **Script:** `scripts/vb_long_followthrough_variant.py`
**Fonte:** `data/backtests/vb_forensics_20260813_040003.csv` (83 trades) + `candles_15m`

## A variante (backlog #1.5)

"Exigir que o candle **depois** do breakout se mantenha do lado certo da banda
antes de entrar". O VB quebra a banda BB(20, 2.0) no fecho do bar 15m; o
backtest enche nesse fecho (`entry_time` = ts do bar de breakout, `entry_price`
≈ close + slippage — **verificado empiricamente**). A regra FT espera um bar:
só mantém o trade se o bar i+1 fechar do lado da quebra (long: close > upper;
short: close < lower). Combinada com **long-only** (direção (a) do forensics:
shorts WR 7.7%, −$66.32).

Duas vistas da mesma variante:
* **FT filter (upper bound)** — trades sobreviventes com entradas originais.
* **FT delayed entry (tradeable)** — entrada no OPEN do bar i+2 (o bar após a
  confirmação), PnL recomputado do mesmo exit_price — o custo real da regra.

## Validação da indexação

**83/83 trades com bar de breakout reconstruído; 83/83 sinais reproduzidos**
(close do bar de breakout além da banda) — a reconstrução bate bit-a-bit com o
backtest. Banda BB idêntica à implementação da estratégia (teste de paridade
incluído).

## Resultados

| Fatia | n | WR | PF | net |
|---|---|---|---|---|
| **BASELINE (todos)** | 83 | 16.9% | 0.45 | **−81.79** |
| long-only | 44 | 25.0% | 0.77 | −15.47 |
| expansion-only (gate live) | 8 | 37.5% | 5.26 | +17.51 |
| **long-only + FT (upper bound)** | **37** | 29.7% | 1.01 | **+0.67** |
| **long-only + FT (delayed entry)** | **37** | 43.2% | 1.11 | **+5.13** |
| short-only + FT (referência) | 27 | 11.1% | 0.43 | −19.29 |

### long-only + FT por regime

| Regime | n | WR | PF | net |
|---|---|---|---|---|
| **expansion** | **6** | 50.0% | 36.16 | **+21.02** |
| low_vol | 11 | 27.3% | 0.98 | −0.32 |
| trend | 20 | 25.0% | 0.44 | **−20.03** |

## Leitura

1. **A combinação remove quase todo o sangramento** (−81.79 → +0.67/+5.13),
   mas **não cria um edge robusto** — PF 1.01–1.11, margem de ruído sobre
   ~$10k. Não é promoção; é a melhor fatia vista até agora.
2. **O edge concentra-se em expansion** (+21.02, n=6) — precisamente o regime
   que o gate live já permite. A variante **refina o gate existente**, não abre
   um novo.
3. **trend longs + FT continuam a sangrar (−20.03)** — o follow-through NÃO
   resgata os longs em trend. E **shorts + FT continuam mortos** (−19.29): o
   lado short é estrutural, independente da confirmação.
4. **Surpresa verificada:** o delayed entry (open do bar i+2) é **≥** ao upper
   bound (+5.13 vs +0.67). A entrada original é no fecho do spike (o extremo);
   a confirmação espera o pullback — gaps medianos ≈ 0 (18/37 negativos), mas
   melhor fill nos perdedores (perdas menores) e pior nos vencedores (ganhos
   menores), o que sobe a WR. A confirmação **não custa** neste caso (ao
   contrário do fade 1m, onde custava meio bps).

## Veredito

A variante long-only + FT **sobrevive à triagem** (fatia positiva mesmo com o
custo da confirmação), mas o sinal é fraco e 100% na mesma amostra de 80d que
motivou todos os estudos (seleção na amostra). O passo honesto: se o VB
continuar vivo, testar a célula **expansion + long + FT** (thresholds pinned,
sem re-fit) em shadow-live contra dados novos. Nenhuma promoção deriva deste
estudo.

## Implementação

* `scripts/vb_long_followthrough_variant.py` — reconstrução BB(20,2) idêntica à
  estratégia, validação 83/83 do sinal, fatias comparativas, JSON persistido
  (`data/backtests/vb_long_ft_variant_*.json`).
* 8 testes unitários (paridade BB, confirmação long/short, rejeição de
  confirmação falhada, ajuste de PnL do delayed entry, sinal do gap, trades sem
  contexto).
