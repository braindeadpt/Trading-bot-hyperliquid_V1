# Funding Carry - Fase 1 (historico): resultado, 2026-11-22

Pre-registo: `notes/fundcarry-prereg-2026-11-21.md` (commit f166227, intacto). Script: `scripts/research/fundcarry_phase1.py`; modulo: `src/research/fundcarry/core.py`; JSON completo: `data/backtests/fundcarry/phase1_result.json`; payloads brutos em cache: `data/backtests/fundcarry/raw/`.

## VEREDICTO: **KILL (C)**  (K=39 janelas completas; sobrevivencia controlada: True)

Todos os criterios falham (ver tabela). Regra da pre-registacao: qualquer falha com K >= 4 -> KILL (C), sem segunda volta.

## Decisoes de definicao (congeladas ANTES de calcular o veredicto)

Ficheiro `data/backtests/fundcarry/DECISIONS_FROZEN.txt`, escrito pelo script a 2026-11-21T23:45:47.353250+00:00 (depois de descarregar candles/meta e verificar que nao ha buracos nos candles e que a API serve dados de moedas delistadas; antes de correr o backtest). sha256 registado no JSON: `6351131c4a698a7e...` (canonicalizado para LF em 2026-11-22 — o valor originalmente registado, `2f436e596db461f3`, era o hash da copia Windows/CRLF; conteudo inalterado, ver emenda no fim).

```
frozen_at_utc: 2026-11-21T23:45:47.353250+00:00
FUNDCARRY PHASE 1 - operational definitions (fixed BEFORE any result was computed)
1. Universe PIT: each Monday D, candidates = main-dex perps in meta (INCLUDING
   isDelisted=True). Eligible iff: not stable / no 'dex:' prefix; history
   (D - first 1d candle with trades n>0) >= 90 d; all 30 daily candles
   D-30..D-1 present (so a coin delisted before D drops out). Ranked by
   resmom_shadow.universe.select_universe (mean close*volume, 30 d), top 25.
   BTC ranks in, is never a leg. A week is VALID only if exactly 25 eligible
   names exist; first rebalance = first valid week.
2. Signal needs: >= 61 finite daily closes (60 returns for beta, vol30,
   resid15 via resmom_shadow.signal.compute_signals) AND >= 120 hourly funding
   observations in the 7 d before D. Names lacking either are not leg
   candidates that week. s = -mean(hourly funding, [D-7d, D)).
3. Ranking ties broken by symbol (asc). Quintile = floor(0.2*25)=5 per leg
   (resmom build_portfolio; min(n_legs, pool//2)). 1/vol30 weights, gross 0.5
   per leg through beta-neutral solve as in resmom portfolio.py (unsolvable ->
   0.5/0.5), THEN the 10% cap: |w|>0.10 (of GROSS_TOTAL=1.0) is cut and the
   excess goes to the other legs of the same side proportionally to their
   current weights, iterated until none exceeds. If every leg of a side is at
   the cap the leftover excess is DROPPED (side gross < pre-cap).
4. Fill fidelity: entry = open of the Monday 1d candle, exit = open of the next
   Monday (forward uses 1m open after 00:05; HL serves only the last 5000 1m
   candles). If a coin has no candle at that open (delisted mid-week) the last
   available close (<=10 d back) is used.
5. Accounting (resmom precedent): fixed gross NOTIONAL 10,000 USD; weekly
   return = (price pnl + funding pnl - cost) / running NAV; NAV compounds with
   pnl. Cost = 6.5 bps * sum|w_new - w_prev| * 10,000 charged at the weekly
   rebalance (first week = full entry). No exit cost after the last week.
6. Funding: real hourly HL fundingHistory events with entry <= t < exit; paid
   on notional qty*price(t), price(t) = linear interpolation of daily opens.
   Long pays rate>0, short receives. A held coin-week with no funding events
   is counted and reported (contributes 0).
7. Windows: 30-d non-overlapping from the first valid rebalance; week -> window
   floor((rebalance - first)/30d). Window COMPLETE iff its end <= exit of the
   last closed week. K = complete windows; K < 4 -> INCONCLUSIVE. Window
   return = compounded weekly net returns of its weeks. Last closed week:
   rebalance + 7d <= most recent Monday 00:00 UTC.
8. Sharpe = sqrt(52)*mean/std(ddof=1) of ALL closed weekly net returns.
   Nulls: 200 random-direction books per week (random 2*n_legs names from that
   week's pool, first half long; resmom nulls.py method) through the same
   weights/cap/costs/FUNDING, seed sha256('fundcarry-null|<week_ms>|<i>').
   PASS cond.2 needs Sharpe > 0.5 AND > p95 (linear interpolation, resmom
   percentile) of the 200 null Sharpes.
9. Sign-flip: one-sided on the K window returns, p = share of the 2^K sign
   patterns with sum >= observed (ties count; exact K<=12). K>12: 200,000
   seeded samples (seed 20261121), p=(count+1)/(N+1). Majority = positives >
   K/2. Cond.3 = majority AND p <= 0.10.
10. Ex-top-3 (resmom read.py): sum over symbols of (price pnl + funding pnl)
    minus the 3 largest symbol sums. Costs are not allocated (precedent);
    a cost-allocated variant is reported as a diagnostic only.
11. Survivorship: controlled iff the API returns candles AND funding for
    isDelisted coins and >=1 of them enters a weekly universe. If not
    controlled: FAIL stays FAIL, PASS becomes 'PASS-provisorio: sobrevivencia
    nao controlada'. Meta-completeness (coins removed from meta entirely)
    cannot be verified and is reported as a caveat.
12. Verdict: all 4 criteria with K>=4 -> PASS; any failure -> KILL (C);
    K<4 -> INCONCLUSIVO. No second round.
13. ResMom diagnostic: arm A (resmom signal.py: resid15/vol30, frozen
    build_portfolio, no cap) on the same history, fills, costs and funding.
```

Diferenca de fidelidade declarada: fill no open diario de segunda (o forward usa o open do 1.o minuto depois de 00:05; a HL so serve as ultimas 5000 velas de 1m). Funding valorizado com preco interpolado entre opens diarios.

## Cobertura

- Periodo: 1.o rebalance 2023-08-28, ultimo 2026-11-09 (saida 2026-11-16); 168 rebalances semanais, 168 semanas fechadas; 0 semanas saltadas.
- K = 39 janelas completas de 30 d (mais 1 janela final incompleta, fora do K mas dentro do Sharpe/cumulativo).
- Universo: 25 por semana (sempre 25), pool de pernas media 24 (sem BTC); 102 moedas distintas passaram pelo universo.
- Moedas delistadas depois e incluidas no universo PIT: 9 (FTM, IP, LAUNCHCOIN, MATIC, MKR, RNDR, TON, VINE, ZEREBRO); a meta da API lista 56 delistadas, todas com candles. Caveat: nao da para verificar que a meta contem TODAS as moedas alguma vez listadas.
- Qualidade: 4 usos de preco-fallback (ultimo close), 0 coin-semanas detidas sem funding.

## Criterios

| Criterio | Observado | Limiar | Passa |
|---|---|---|---|
| 1. Retorno liquido acumulado > 0 | -56.74% | 0 | NAO |
| 2a. Sharpe anualizado > 0,5 | -0.287 | 0.500 | NAO |
| 2b. Sharpe > p95 dos 200 nulos | -0.287 | 0.731 | NAO |
| 3a. Janelas com retorno > 0 (maioria) | 17/39 | >19.5 | NAO |
| 3b. Sign-flip p (unilateral) | 0.7089 | 0.10 | NAO |
| 4. PnL ex-top-3 (USD) | -11,113 | 0 | NAO |

Sign-flip: monte_carlo (200000 amostras, seed 20261121), 17/39 janelas positivas.

Retorno acumulado -56.74%; NAV final 4,326 USD de 10 000; Sharpe -0.287; media semanal -0.27%, desvio 6.65%; drawdown maximo -68.33%.

## Nulos (200 carteiras de direccao aleatoria, mesmo universo/pesos/tecto/custos/funding)

Sharpe: p5 -0.782, p25 -0.340, p50 -0.061, p75 0.305, p95 0.731, p99 1.186; media -0.025. Sharpe real -0.287 = percentil 29.5 dos nulos (seed tag `fundcarry-null`, sha256 por semana/indice).

## Decomposicao do PnL (USD, nocional 10 000, soma das 168 semanas)

- Preco: -8,543
- Funding (recebido - pago): 3,929
- Custos: -1,060
- Liquido: -5,674

O carry de funding existe (positivo, cerca de +39% do nocional) mas e esmagado pelo movimento de preco (cerca de -85%): o risco declarado de carry anulado por momentum / squeezes nas pernas short materializou-se.

## Turnover e custos
- Turnover semanal medio (soma |dw|, 0-2): 0.971; custo total 1,060 USD = 10.60% do nocional.
- Tecto de 10%: activo em 168/168 semanas; gross medio apos tecto 0.943 (minimo 0.761). Com 5 pernas por lado e tecto 10% cada lado fica limitado a 0,5, por isso o excesso nao colocavel e descartado (definicao 3).

## Janelas de 30 dias (retorno liquido composto; funding/preco/custos em % do nocional, somas simples)

| k | inicio | fim | semanas | completa | retorno liquido | funding | preco | custos |
|---|---|---|---|---|---|---|---|---|
| 0 | 2023-08-28 | 2023-09-27 | 5 | sim | 2.96% | 1.71% | 1.55% | 0.30% |
| 1 | 2023-09-27 | 2023-10-27 | 4 | sim | 3.19% | 0.97% | 2.60% | 0.28% |
| 2 | 2023-10-27 | 2023-11-26 | 4 | sim | 0.96% | 1.08% | 0.25% | 0.31% |
| 3 | 2023-11-26 | 2023-12-26 | 5 | sim | -2.61% | 2.61% | -5.09% | 0.31% |
| 4 | 2023-12-26 | 2024-01-25 | 4 | sim | -7.09% | 0.74% | -7.87% | 0.28% |
| 5 | 2024-01-25 | 2024-02-24 | 4 | sim | -7.65% | 1.59% | -8.78% | 0.23% |
| 6 | 2024-02-24 | 2024-03-25 | 4 | sim | -35.76% | 1.61% | -33.43% | 0.23% |
| 7 | 2024-03-25 | 2024-04-24 | 5 | sim | -13.82% | 0.96% | -8.63% | 0.29% |
| 8 | 2024-04-24 | 2024-05-24 | 4 | sim | -9.82% | 0.69% | -5.35% | 0.22% |
| 9 | 2024-05-24 | 2024-06-23 | 4 | sim | 24.89% | 0.68% | 10.74% | 0.29% |
| 10 | 2024-06-23 | 2024-07-23 | 5 | sim | 4.62% | 1.56% | 1.29% | 0.26% |
| 11 | 2024-07-23 | 2024-08-22 | 4 | sim | -17.71% | 0.85% | -10.93% | 0.28% |
| 12 | 2024-08-22 | 2024-09-21 | 4 | sim | -8.93% | 0.76% | -4.87% | 0.20% |
| 13 | 2024-09-21 | 2024-10-21 | 4 | sim | -3.42% | 1.18% | -2.50% | 0.17% |
| 14 | 2024-10-21 | 2024-11-20 | 5 | sim | 26.49% | 1.14% | 10.36% | 0.29% |
| 15 | 2024-11-20 | 2024-12-20 | 4 | sim | 33.99% | 0.48% | 18.02% | 0.30% |
| 16 | 2024-12-20 | 2025-01-19 | 4 | sim | 9.04% | 1.40% | 5.30% | 0.21% |
| 17 | 2025-01-19 | 2025-02-18 | 5 | sim | 11.74% | 1.01% | 8.54% | 0.37% |
| 18 | 2025-02-18 | 2025-03-20 | 4 | sim | 7.33% | 0.66% | 6.00% | 0.25% |
| 19 | 2025-03-20 | 2025-04-19 | 4 | sim | -11.29% | 0.89% | -11.26% | 0.22% |
| 20 | 2025-04-19 | 2025-05-19 | 4 | sim | -6.60% | 0.99% | -6.24% | 0.25% |
| 21 | 2025-05-19 | 2025-06-18 | 5 | sim | -14.23% | 0.93% | -11.68% | 0.30% |
| 22 | 2025-06-18 | 2025-07-18 | 4 | sim | 9.57% | 0.59% | 6.06% | 0.26% |
| 23 | 2025-07-18 | 2025-08-17 | 4 | sim | -1.88% | 0.42% | -1.51% | 0.29% |
| 24 | 2025-08-17 | 2025-09-16 | 5 | sim | -5.47% | 0.30% | -3.84% | 0.37% |
| 25 | 2025-09-16 | 2025-10-16 | 4 | sim | 3.36% | 0.89% | 1.68% | 0.29% |
| 26 | 2025-10-16 | 2025-11-15 | 4 | sim | 2.41% | 0.68% | 1.25% | 0.24% |
| 27 | 2025-11-15 | 2025-12-15 | 4 | sim | -13.33% | 1.28% | -10.59% | 0.24% |
| 28 | 2025-12-15 | 2026-01-14 | 5 | sim | -1.60% | 0.54% | -1.20% | 0.33% |
| 29 | 2026-01-14 | 2026-02-13 | 4 | sim | -19.27% | 1.75% | -13.26% | 0.28% |
| 30 | 2026-02-13 | 2026-03-15 | 4 | sim | 6.19% | 1.41% | 1.84% | 0.19% |
| 31 | 2026-03-15 | 2026-04-14 | 5 | sim | -1.33% | 0.96% | -1.39% | 0.27% |
| 32 | 2026-04-14 | 2026-05-14 | 4 | sim | -1.67% | 0.72% | -1.32% | 0.26% |
| 33 | 2026-05-14 | 2026-06-13 | 4 | sim | 1.86% | 0.48% | 0.72% | 0.26% |
| 34 | 2026-06-13 | 2026-07-13 | 4 | sim | 2.91% | 0.48% | 1.28% | 0.26% |
| 35 | 2026-07-13 | 2026-08-12 | 5 | sim | -8.77% | 2.64% | -6.96% | 0.35% |
| 36 | 2026-08-12 | 2026-09-11 | 4 | sim | -5.49% | 0.76% | -3.21% | 0.23% |
| 37 | 2026-09-11 | 2026-10-11 | 4 | sim | -22.61% | 0.30% | -10.43% | 0.27% |
| 38 | 2026-10-11 | 2026-11-10 | 5 | sim | 21.56% | 0.59% | 7.44% | 0.36% |

## PnL por simbolo (preco + funding, sem custos, como `read.py`)
- Total -4,614 USD; top-3: 2,657, 2,066, 1,776; ex-top-3 = -11,113. Variante com custos alocados por simbolo (diagnostico): -12,085 (mesmo sinal).

Top 10: TAO 2,657, WLD 2,066, SUI 1,776, BNB 1,118, AAVE 982, kBONK 839, XPL 750, SNX 629, LTC 596, VIRTUAL 550

Bottom 10: kPEPE -5,506, ENA -1,483, ZEC -1,331, HYPE -1,294, NEAR -1,212, VVV -1,036, KAITO -987, APT -823, SOL -795, STX -791

## Diagnosticos vs ResMom braco A (mesma historia, fills, custos e funding)

- Correlacao de Spearman do sinal carry vs sinal ResMom: media semanal -0.118 (mediana -0.102); semanas com corr > 0,5: 0.00%; < -0,5: 4.76%.
- Correlacao dos retornos semanais: Pearson -0.144, Spearman -0.105. Sobreposicao de pernas do mesmo lado: 17.32%.
- Conclusao: baixa / ligeiramente negativa; NAO e momentum disfarcado de carry. (ResMom A neste backtest: retorno 59.52%, Sharpe 0.67; descritivo, nao validado.)

## Limitacoes
- Fills em open diario, nao 1m (declarado). Funding valorizado com preco interpolado.
- Sobrevivencia controlada via meta `isDelisted` (9 delistadas entraram no universo); completude da meta nao verificavel. O veredicto e KILL, pelo que o vies (que favorece a estrategia) nao altera a conclusao.
- Tecto 10% com 5 pernas/lado torna a carteira quase equal-weight; consequencia directa da regra congelada.

## Emenda 2026-11-22 — diagnostico funding recebido/pago (linha 62 do prereg)

O resultado selado (`phase1_result.json`) reporta apenas o funding liquido
(+3,929.15 USD em `decomposition_usd.funding`). A linha 62 do
pre-registo pede a decomposicao entre funding recebido e pago; esse
diagnostico faltava no artefacto selado.

Foi calculado DEPOIS do veredicto, a partir da mesma cache local, sem
novos downloads e sem alterar nenhuma definicao nem o par selado
(`phase1_result.json` / `DECISIONS_FROZEN.txt` mantem-se; o sha256
canonico LF `6351131c…` do ficheiro de decisoes e o registado no
resultado). O calculo esta em
`data/backtests/fundcarry/phase1_diag_funding_split.json`
(commit do codigo usado: 17af2a5), produzido por
`--diag-funding-split` em `scripts/research/fundcarry_phase1.py`.

Resultado (USD, sobre o nocional fixo de 10.000 por semana):

| perna | recebido | pago | liquido |
|-------|----------|------|---------|
| short | +3 927.64 | -95.67 | +3 831.97 |
| long  | +1 803.26 | -1 706.07 | +97.19 |
| total | +5 730.90 | -1 801.74 | **+3 929.15** |

Verificacao obrigatoria: recebido - pago = +3 929.15 USD, igual ao
funding liquido selado (`delta` 0.0, `ok: true` no ficheiro de
diagnostico).

Leitura: as pernas short entregaram o carry esperado (+3.8k liquido) e
as long foram aproximadamente neutras em funding (+97). O veredicto
KILL (C) vem inteiramente do movimento de preco (-8 543 USD), nao do
carry. Este diagnostico e posterior ao veredicto e nao muda nenhum dos
quatro criterios; o veredicto KILL (C) mantem-se.
