# Liquidation stop-out floor — calibration against real multi-venue data

Generated: 2026-08-14T10:51:54+00:00
Source: `data\live\bot.db` · venues: `okx + bybit` · step: 60s · window: 5m
Events: 13271 · samples: 6666

## Distribuição do notional dominante da janela de 5m (pooled)

| p50 | p90 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| 0.0M | **2.5M** | 7.0M | 45.1M | 315.2M | 6666 |

## Por símbolo

| symbol | n | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| BTC | 1789 | 0.1M | 15.3M | 33.8M | 130.1M | 315.2M |
| ETH | 2044 | 0.0M | 2.0M | 4.5M | 19.6M | 41.6M |
| HYPE | 1364 | 0.0M | 0.1M | 0.3M | 1.5M | 6.1M |
| SOL | 1469 | 0.0M | 0.1M | 0.1M | 0.8M | 1.4M |

## Calibração do floor (5.0M → 2.5M)

* Floor anterior: `LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD = 5_000_000` (p90 provisional de venue-único, nunca calibrado contra dados reais).
* **p90 real da janela multi-venue: 2.5M** (valor exacto 2.467M).
* **Floor calibrado: 2_500_000** (arredondado do p90 real para um valor limpo, ligeiramente acima do p90 exacto — conservador por construção).

### Leitura

* O default de 5.0M estava **~2× acima** do p90 real (2.5M): o stop-out só dispararia em eventos de cauda extrema — na prática quase nunca (super-calibrado, o exit por liquidação era letra morta).
* p90 (2.5M) = só ~10% das janelas amostradas excedem este valor — um flush acima do p90 é genuinamente raro para os venues contratados.
* O floor é único e global; a sensibilidade **por símbolo** varia com a escala (ver tabela: BTC p90 15.3M vs SOL p90 0.1M) — um floor único sub-calibra BTC e sobre-calibra SOL; o p90 pooled é o ponto médio defensável.
* Recalibrar é uma decisão revista (hash-neutral, no código) — repetível a qualquer momento com `python scripts/calibrate_liquidation_stopout_floor.py`.

*Report regenerado por `python scripts/calibrate_liquidation_stopout_floor.py`*