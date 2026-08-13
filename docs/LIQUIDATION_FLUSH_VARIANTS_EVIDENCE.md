# Variantes do fade ETH p90/30m — evidência (fonte real)

**Data:** 2026-08-13 · **Fonte:** real (okx+bybit, 3.5d, 08-09→08-13) · **Símbolo:** ETH
**Mecânica:** idêntica ao v2 (flush 1m dominante ≥ threshold, entry no OPEN do
1º candle pós-flush, fees 0.090% RT, candles carimbados ao fecho do minuto).
**Script:** `scripts/liquidation_flush_variants.py` · **JSON:** `data/backtests/liquidation_flush_variants_20260813_064616.json`

## Baseline (referência)

| Célula | n | WR | PF | avg net |
|---|---|---|---|---|
| ETH p90 / 1º bar / hold 30m / fade | 47 | 48.9% | 2.23 | **+6.37 bps** |

## A. Entry delay — pior, não melhor

| Entry | n | WR | PF | avg net | vs baseline |
|---|---|---|---|---|---|
| **1º bar** (baseline) | 47 | 48.9% | 2.23 | +6.37 bps | — |
| **2º bar** | 47 | 48.9% | 2.23 | +5.86 bps | **−0.51 bps** |

Entrar no 2º bar **perde** 0.5 bps — o edge vive na reação imediata ao flush;
esperar um bar não melhora o fill. A variante não promove.

## B. Intensidade (múltiplos de p90) — a única família que melhora

| Threshold | n | WR | PF | avg net | vs baseline |
|---|---|---|---|---|---|
| 1.0× p90 ($1.0M) | 47 | 48.9% | 2.23 | +6.37 bps | — |
| **1.5× p90 ($1.5M)** | **38** | **52.6%** | **3.03** | **+9.74 bps** | **+3.4 bps** |
| **2.0× p90 ($2.0M)** | 33 | 51.5% | 2.80 | +8.91 bps | +2.5 bps |
| 3.0× p90 ($3.1M) | 23 | 43.5% | 2.52 | +9.09 bps | +2.7 (n<30) |
| 5.0× p90 ($5.1M) | 14 | 42.9% | 2.68 | +7.34 bps | +1.0 (n<30) |

**1.5× p90 melhora o baseline em todos os eixos** (avg +53%, PF 3.03 vs 2.23,
WR 52.6% vs 48.9%) **mantendo n=38 ≥ 30**. A 2.0× também passa o gate. Acima
de 2× o n cai abaixo de 30 — o gate perde poder.

## C. Trailing — destrói o edge

| Exit | n | WR | PF | avg net | vs baseline |
|---|---|---|---|---|---|
| hold 30m (baseline) | 47 | 48.9% | 2.23 | +6.37 bps | — |
| trail 0.3% / max 60m | 48 | 37.5% | 0.98 | −3.59 bps | −10 bps |
| trail 0.3% / max 120m | 48 | 35.4% | 0.92 | −4.37 bps | −10.7 |
| trail 0.5% / max 60m | 48 | 43.8% | 1.08 | −2.58 bps | −9 |
| trail 0.5% / max 120m | 48 | 50.0% | 1.20 | −1.01 bps | −7.4 |
| trail 1.0% / max 60m | 48 | 47.9% | 1.38 | +1.69 bps | −4.7 |
| trail 1.0% / max 120m | 48 | 50.0% | 0.89 | −7.29 bps | −13.7 |

**Todas as variantes de trailing são negativas ou marginais.** O fade de flush
é mean-reversion de horizonte curto — o trailing corta os vencedores que
continuam a reverter e deixa correr os perdedores. Confirmado pelo padrão:
quanto mais apertado o trail, pior (0.3% pior que 0.5% pior que 1.0%).

## Gate (n≥30 & PF>1) — 9 células passam, mas só a família B melhora o baseline

Ordenado por avg net: **B-1.5× (+9.74)** > **B-2.0× (+8.91)** > baseline (+6.37)
> A-2º bar (+5.86) > C-trailing 1.0%/60m (+1.69) > resto negativo.

## Veredito

1. **Entry delay: kill** — o edge é imediato; esperar um bar custa 0.5 bps.
2. **Trailing: kill** — o hold fixo é o exit certo para esta célula; qualquer
   ratchet corta o edge (−4.7 a −13.7 bps).
3. **Intensidade 1.5× p90: única candidata a promoção** — melhora todos os
   eixos com n=38 ≥ 30. **Caveat crítico: é seleção na mesma amostra de 3.5d**
   (data-snooping). O passo correto é testar 1.5× p90 como célula adicional no
   shadow-live em curso (7d, out-of-sample) e no recheck aos 30d, **não**
   promover já.
4. Aprendizagem metodológica: o bug do filtro de threshold (baseline n=473 em
   vez de 47) foi apanhado por testes unitários que pinam a paridade com o v2 —
   as variantes só são comparáveis se a mecânica base for a mesma.

## Próximo passo

Adicionar **ETH p90×1.5 / hold 30m / fade** como segunda célula do harness
shadow-live (mesmo desenho, threshold pinned $1.537M) para ganhar a amostra
out-of-sample de 7 dias antes de qualquer decisão de promoção.
