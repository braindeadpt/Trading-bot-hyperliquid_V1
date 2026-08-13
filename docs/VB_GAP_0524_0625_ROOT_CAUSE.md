# VB não tradeou entre 05-24 e 06-25 — causa raiz: gate de funding stale no replay

**Data:** 2026-08-13 · **Investigação:** full-trace do backtest VB na janela do gap
**Scripts usados:** `scripts/vb_regime_forensics.py` (dados) + trace instrumentado do
`BacktestEngine` (logs) · **Veredito:** NÃO é filtro de candles, NÃO é bug — é o
gate `replay_data_quality` a rejeitar todos os sinais porque o **funding
pré-06-26 era esparso** (2.9 rows/dia vs 2619/dia depois).

## O que se sabia

O forensics do VB (80d) mostrou o primeiro trade em **06-26** — um buraco de
~5 semanas (05-18/05-24 → 06-25) apesar de os candles existirem.

## O que a investigação provou (em 5 camadas)

| # | Verificação | Resultado |
|---|---|---|
| 1 | Candles 1m no gap | **Completos**: 46.199/símbolo, 0 gaps > 2min, timestamps consistentes (fim do minuto), volume > 0 em 100% |
| 2 | Candles 15m/1h no gap | Válidos: 0 anómalos, ranges reais (max 3-5.5% em 06-04..06-07) |
| 3 | Condições da estratégia | **A estratégia DISPAROU 64 sinais reais** no gap (confiança 0.73-0.95, squeeze ativo, volume surge 1.5-5×) — "condições nunca ativas" é falso |
| 4 | Gate de qualidade de dados | **63/64 sinais rejeitados por `replay_data_quality`** — 13 `replay_funding_stale:no_series` + 50 `replay_funding_stale:Xms` (X = 6-28 min) |
| 5 | Estado do funding no DB | **Gap: 2.9 rows/dia** (primeiro funding 05-30 16:00) · **Ativo: 2.619 rows/dia** (poll 30s) — salto exatamente em **06-26** |

## A causa raiz

O gate `replay_data_quality` (`src/backtest/replay_data_quality.py`) exige funding
**fresco a < 5 min** (`max_funding_stale_ms: 300_000` em `config/settings.yaml`,
`require_funding: true`). O funding no período pré-06-26 era **esparso** — amostrado
~a cada 8h (2.9 rows/dia, 79 rows em 27 dias), provavelmente de um backfill inicial
com intervalo largo. Resultado:

* Cada sinal do VB era avaliado contra o funding mais recente, que estava sempre
  6-28 min velho → `replay_funding_stale` → rejeição.
* 13 sinais antes de 05-30 16:00 nem tinham série → `no_series`.

O salto de densidade para 1.452 rows/dia em 06-26 (dia em que o funding passou a
ser recolhido em tempo real pelo bot, poll a cada 30s) é exatamente o dia do
primeiro trade do VB. **O gate estava a funcionar como desenhado** — recusando
operar com dados de funding que o desenho considera insuficientemente frescos.

## Implicações

1. **Não é defeito do VB nem do gate.** O buraco é um artefacto de dados: o funding
   pré-06-26 não tinha a densidade que o contrato de qualidade exige.
2. **Não deve ser "corrigido" baixando o stale threshold.** O gate protege o bot
   contra decisões com funding velho; afrouxá-lo para "tapar" o buraco seria
   escolher dados de pior qualidade.
3. **A leitura honesta:** o VB **não foi testado** no período 05-24..06-25 — essa
   janela é um "não-teste" (todos os sinais bloqueados), não um "0 trades porque o
   mercado não cooperou". Os estudos que usam o forensics devem tratar 05-18..06-25
   como ausência de dados válidos, não como regime onde o VB não opera.
4. **Ação possível (opcional):** se houver uma fonte de funding histórica densa
   (ex. Hyperliquid REST), re-backfill 05-18..06-25 e re-correr — aí o VB teria a
   janela completa. Sem isso, o buraco permanece documentado como limitação de dados.

## Evidência

* Trace: `logs/vb_gap_final.log` (63 rejeições com reason completo + 64 sinais)
* Densidade funding: query em `funding_history` (GAP 79 rows/27d vs ACTIVE 110k/42d)
* Gate: `src/backtest/replay_data_quality.py:138-146` (require_funding → stale check)
* Config: `config/settings.yaml:950-958` (`max_funding_stale_ms: 300000`)
