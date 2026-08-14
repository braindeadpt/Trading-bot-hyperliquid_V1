# Feed Age Creep — lead-time validation

O detector antecipa os silêncios reais? Para cada feed contratado, o script cruza os daily max ages (`feed_age_history`) com os dias de degradação (max age ≥ threshold — a condição que dispara `FEED SILENT`) e mede quantos dias antes o detector (regra `staircase` de produção) disparou.

- Regra: 5d não-decrescentes, crescimento ≥ 15% do threshold, último dia ≥ 25% do threshold

| Feed | Dias | Degradados | Episódios | Antecipados | Same-day | Misses | Lead médio (d) |
|---|---|---|---|---|---|---|---|
| `funding_cex` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `funding_hl` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `l2_book_recording` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `liquidation_bybit` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `liquidation_coinalyze_check` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `liquidation_okx` | 0 | 0 | 0 | 0 | 0 | 0 | — |
| `taker_split` | 0 | 0 | 0 | 0 | 0 | 0 | — |

**Sem episódios de degradação na janela — nada a validar.**

## Detalhe por episódio

| Feed | Início | Fim | Fire | Lead |
|---|---|---|---|---|

_Gerado por `scripts/validate_feed_age_creep_leadtime.py` — read-only, nunca trade._