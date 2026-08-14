# Cadence detector — lead-time validation

O detector `FEED CADENCE` avisa quando o gap actual ultrapassa o **p99 histórico** dos gaps do próprio feed — o sinal de thinning muito antes do `FEED SILENT` aos 6h. Este script valida essa promessa contra o histórico real: walk-forward sobre os `liquidation_events` do OKX no live DB (a série exacta em que o monitor bate), com a mesma regra (`cadence_percentile`, a função partilhada com produção). Um gap que atinge o threshold de 6h é a condição exacta que dispararia `FEED SILENT` — a degradação real.

- Série: 7952 eventos okx · 7951 gaps · 08-09 16:57 → 08-14 04:00 (UTC)
- Detector: p99 com min 100 gaps · história 4000 gaps · threshold silêncio 6.0h

| Métrica | Valor |
|---|---|
| Gaps | 7951 |
| Fires do detector | 117 (0 confirmados · 117 sem confirmação) |
| Degradações (gap ≥ 6h) | 0 |
| Antecipadas | 0 |
| Misses | 0 |
| Lead médio (vs 6h) | — |

**Sem degradações (gap ≥ 6h) na janela — nada a validar.**

## Detalhe por degradação

| Gap | Início | Fim | Duração | p99 base | Lead vs 6h |
|---|---|---|---|---|---|

## Cruzamento com os alertas reais

| Alerta real (`feed_silence_alerts`) | Valor |
|---|---|
| Cadence emitidos pelo monitor | 0 |
| Casados com fires simulados (±900s) | 0 |
| Janela dos alertas reais | — → — |

_Gerado por `scripts/validate_feed_cadence_leadtime.py` — read-only, nunca trade._