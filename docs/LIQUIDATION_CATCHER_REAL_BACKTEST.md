# LiquidationCatcher — real-feed backtest (08-09+)

**Data:** 2026-08-14 · **Commit (script):** script `scripts/backtest_liquidation_catcher_real.py`

## Porquê este backtest

O research DB guarda candles HL mas **zero liquidações** — o aggregator persiste
cada evento real no live `data/live/bot.db` (okx/bybit/proxy), não no research
store. O engine de replay lê candles **e** liquidações do mesmo DB, por isso um
backtest directo sobre o research DB nunca teria feed de liquidações.

O script monta um DB de backtest dedicado: candles 1m do research DB (HL
provenance) + liquidações reais (okx/bybit) + funding do live DB, para o
período **08-09 → 08-13** (5 dias completos), e corre o LiquidationCatcher
force-enabled com o contrato de produção (`require_real_liquidation_data: true`).

## Feed real por símbolo

| Símbolo | Candles 1m | Liquidações reais | Funding |
|---|---|---|---|
| BTC | 7.185 | 4.932 (okx/bybit) | 13.653 |
| ETH | 7.189 | 5.045 (okx/bybit) | 13.653 |

## Veredito do contrato de produção (strict)

```
fidelity_tier : refused_insufficient_feeds
refused       : True
  reason: BTC:candles:gap_exceeds:600000ms>120000ms
  reason: ETH:candles:gap_exceeds:540000ms>120000ms
LiquidationCatcher: tier=tier_a_hl_ohlc  tier_a=True  missing=-  liq_provenance=real
```

**Dois factos separados:**

1. **Liquidações: provenance `real` → LiquidationCatcher é Tier A.** Com
   9.977 liquidações reais (okx/bybit) no replay, o classifier de provenance
   (`is_real_liquidation_source`) marca `real`, `tier_a_eligible=True`, sem
   `missing_feeds` — a decisão do contrato para a estratégia é exactamente a
   que os testes de paridade pinam (`tier_a_hl_ohlc` + `liq_provenance=real`).

2. **O replay inteiro é REFUSED por gaps de candles 1m.** O research DB 1m tem
   lacunas reais de 4–10 minutos (amostragem contínua não contínua no venue),
   e o contrato strict com `gap_intervals: 2` (máx. 2 min) recusa o replay
   **antes** de qualquer estratégia correr — mesmo com o feed de liquidações
   perfeito. Isto é o guard a funcionar: o research DB 1m actual não é
   production-grade para replay strict nesta janela.

## Resultado do backtest (modo degraded, refuse=false)

Para medir o comportamento da estratégia (não o contrato), o script corre o
replay com `refuse_insufficient_feeds=false` e reporta o manifest com o tier
efectivo (degraded coverage + provenance real):

| Métrica | Valor |
|---|---|
| **n_trades** | 16 |
| **win_rate** | 0.0% (0/16) |
| **profit_factor** | 0.000 |
| **total_pnl_usd** | **−142.42** |
| **avg_trade** | −8.90 USD |
| **expectancy_r** | −0.178 |

**Padrão dominante — 16/16 exits por `liquidation_stop_out` após 1 minuto:**

| entry | exit | hold | símbolo | side | pnl |
|---|---|---|---|---|---|
| 08-09 22:14 | 22:15 | 1 min | BTC | short | −4.22 |
| 08-09 22:14 | 22:15 | 1 min | ETH | short | −10.78 |
| 08-10 00:43 | 00:44 | 1 min | BTC | long | −8.89 |
| 08-10 13:34 | 13:35 | 1 min | BTC | long | −5.11 |
| … | | | | | |

## Leitura

O LiquidationCatcher entra quando há um flush de liquidações (notional ≥ $5.7M,
count ≥ 18 na janela de 5m). O **stop-out por liquidação** — que valida o side
da posição quando a janela de liquidações reais o confirma — dispara **no
minuto seguinte**: o mesmo flush que gerou o sinal valida o side e força a
saída. O resultado é o pior dos dois mundos: a estratégia entra no pico do
flush e o stop-out sai logo depois, transformando o fade de cascata numa perda
sistemática de ~1 minuto.

Isto é evidência OOS (fora da amostra que calibrou os thresholds em 08-09) a
favor de **não promover** o LiquidationCatcher com a configuração actual: o
sinal de entrada e o stop-out de saída são alimentados pela **mesma** janela de
liquidações, criando um loop entrada→stop-out que sangra.

## Artefactos

- Script: `scripts/backtest_liquidation_catcher_real.py`
- JSON completo: `data/backtests/liq_catcher_real_20260813.json` (metrics +
  manifest com `strategy_fidelity[LiquidationCatcher]` e `liq_provenance`)
- Manifest: `data_source=sqlite_hl_research` · `fidelity_tier=tier_b_proxy_microstructure`
  (degraded) · `strategy_fidelity[LiquidationCatcher] = tier_a_hl_ohlc / real`
