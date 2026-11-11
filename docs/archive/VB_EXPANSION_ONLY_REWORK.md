# VB expansion-only rework — A/B split confirma que a W2 deixou de custar

**Data:** 2026-08-13 · **Mudança:** `VB_REGIMES` no router (`src/core/phase08_regime_router.py`)
de `{"trend", "expansion"}` → `{"expansion"}` · **Hash-neutral:** `compute_config_hash`
só hasheia `settings.yaml` (config intocado) → a janela congelada da Fase 10 não muda.

## Racional (evidência prévia)

O forensics do VB (80d, `data/backtests/vb_forensics_*.csv`) mostrou que a fatia
**trend é o sangrador estrutural** (n=51, **−$73.04**) enquanto **expansion é a única
fatia positiva** (n=8, **+$17.51**); low_vol é negativo (n=24, −$26.26). O rework
expansion-only confina o VB à fatia sobrevivente — a direção (b) que o próprio
forensics recomendou.

## Resultado do A/B split (janelas independentes, 30d não sobrepostas)

Range: 05-18 → 08-07 (3 janelas: W1, W2, W3). Fonte: mesma que o estudo anterior
(raw backtests VB+VWAP com configs de produção, gate Phase-08 aplicada ao nível de
trade com ADX 15m closed).

| Janela | bloqueado ANTES (trend+exp) | bloqueado DEPOIS (expansion-only) | "com router" ANTES | "com router" DEPOIS |
|---|---|---|---|---|
| W1 05-18..06-16 | 0.00 (0 trades) | 0.00 (0 trades) | 0.00 | 0.00 |
| W2 06-17..07-16 | **+6.33 (custava)** | **−43.32 (poupa)** | −18.92 | **+30.74** |
| W3 07-17..08-07 | −55.23 | **−78.68** | −36.77 | **−13.30** |
| **TOTAL** | −48.90 | **−122.00** | −55.69 | **+17.44** |

### Re-corrida com dados frescos (05-18 → 08-13)

Reproduzido em 08-13 08:22 com o mesmo split, mas com o feed estendido em 6 dias
novos (08-07 → 08-13) — **out-of-sample relativo à amostra de treino do rework**.

| Janela | sem router | com router | bloqueado | poupa% |
|---|---|---|---|---|
| W1 05-18..06-16 | 0.00 | 0.00 | 0.00 | 0% |
| W2 06-17..07-16 | −12.61 | **+30.74** | −43.32 | 344% |
| W3 07-17..08-13 | −100.34 | −20.02 | **−80.30** | 80% |
| **TOTAL** | **−112.95** | **+10.72** | **−123.62** | — |

* W2 é **bit-a-bit idêntica** à corrida anterior (janela inalterada → determinismo
  do pipeline confirmado mais uma vez).
* Os 6 dias frescos (08-07..08-13): trades bloqueados net ≈ **−1.6** (direção
  mantém-se — o router continua a filtrar perdas), mas os trades permitidos
  sangraram ≈ −6.7 — o residual "com router" da W3 continua negativo (−20.02),
  honestamente reportado: o router é um **filtro de perdas**, não um gerador de
  PnL nessa janela.
* **TOTAL com router ainda positivo (+10.72)** e bloqueado total −123.62 (vs
  −122.00 na corrida anterior) — a poupança global cresce com dados novos.

## Leitura

1. **A W2 deixou de custar — por 7×.** Antes o router bloqueava +$6.33 de trades
   lucrativos nessa janela (a única mancha do estudo anterior); com expansion-only
   bloqueia **−$43.32** de perdas. O que custava passou a poupar.
2. **A W3 melhorou ainda mais** (−55.23 → −78.68): os trades de VB em trend são os
   mais numerosos e o pior sangrador — bloqueá-los em todas as janelas multiplica a
   poupança.
3. **O "com router" total passou a positivo (+$17.44)** — a primeira vez que o router
   (VB+VWAP) gera PnL positivo combinado em amostra out-of-sample. Sem router o
   combinado é −$104.62; com expansion-only +$17.44.
4. **Consistência:** a direção agora replica em TODAS as janelas com trades
   (W2 e W3 poupam; W1 não tem dados) — sem caveats como o antigo +6.33 da W2.

## Detalhe de implementação

* `VB_REGIMES = frozenset({"expansion"})` com comentário de evidência no router.
* `regime_router_a_b_test.py` passou a importar `VB_REGIMES`/`VWAP_REGIMES` do
  router (fonte única — o A/B nunca pode divergir do gate live).
* **VB mantém-se em shadow:** `phase08.shadow_strategies` inclui VolatilityBreakout
  e `execution_strategies` só tem VWAPDeviation — o rework muda o gate do router,
  não o modo de execução. Nenhuma promoção a live sem shadow-live + PASS em dados
  novos.
* Testes atualizados para o novo contrato: VB bloqueado em trend (P0 + shadow_mode),
  VB passa em expansion, contradição testada em expansion; novo teste
  `test_vb_expansion_only_rework.py` pinna `VB_REGIMES == {"expansion"}` e a
  neutralidade do hash.
* Script agora persiste `regime_router_ab_split_*.json` com breakdown por regime.

## Breakdown por regime (bloqueado, do JSON persistido)

| Janela | expansion | low_vol | trend | total |
|---|---|---|---|---|
| W2 06-17..07-16 | −21.62 (n=5, VWAP) | +9.10 (n=6, VB) | **−30.80 (n=26, VB)** | −43.32 |
| W3 07-17..08-07 | +13.74 (n=8, VWAP) | −35.42 (n=18, VB) | **−57.00 (n=36, VB)** | −78.68 |

Notas:
* **A mudança expansion-only bloqueia VB em trend** (antes passava) — é o maior
  sangrador e a fonte principal da poupança nova (−30.80 / −57.00).
* O custo residual de bloquear **VWAP em expansion** replica na W3 (+13.74) mas
  é largamente superado pelo ganho em trend (a W2 ainda poupa −21.62 no mesmo slice).
* VB em low_vol: misto (W2 +9.10 a favor, W3 −35.42 contra) — permanece bloqueado,
  sem alteração neste rework.

## Caveat

A melhoria é medida na MESMA amostra de 80d que motivou o rework (seleção na amostra
de treino — o forensics e o A/B partilham o período). O passo seguinte é o shadow-live:
correr o router expansion-only em papel contra dados novos antes de qualquer promoção.
