# Backtest Workflow — Decisões baseadas em dados

## Princípio

**Não ligar/desligar estratégias em live sem backtest isolado primeiro.**

1. Todas as estratégias são testadas **uma a uma** (forced `enabled: true`).
2. Resultados em múltiplas janelas (volátil, 2 semanas, histórico completo).
3. Veredicto automático: **KEEP / WATCH / KILL / NO_DATA**.
4. Só depois — e **só com baseline-signal gate PASS** (B1≥p95 + n≥30 + PF>1) —
   actualizar `config/settings.yaml` `execution_strategies` e paper trading.
   Ver `docs/BASELINE_SIGNAL_GATE.md` / `AGENTS.md` §12.
   INCONCLUSIVO (poucos trades) **não** autoriza kill nem promoção.

## Comandos

```bash
# Auditoria completa (usa bot.db) — ~2h com --quick
python scripts/backtest_strategy_audit.py --quick

# Auditoria com todas as janelas (incl. E_feeds para LeadLag/LiqCatcher)
python scripts/backtest_strategy_audit.py

# Relatório a partir de CSV existente
python scripts/generate_strategy_audit_report.py

# Backtest legado (3 janelas fixas)
python scripts/backtest_per_strategy.py
```

## Outputs

| Ficheiro | Conteúdo |
|----------|----------|
| `docs/STRATEGY_AUDIT.md` | Veredictos + tabelas |
| `data/backtests/strategy_audit_*.csv` | Dados brutos |

## Dados necessários na DB

| Estratégia | Dados |
|------------|-------|
| VolatilityBreakout, VWAPDeviation, TrendPyramid, SMF | Candles 15m/1h ✅ |
| FundingExtreme, FundingArbitrage | `funding_history` |
| LeadLag | `binance_perp_prices` |
| LiquidationCatcher | `liquidation_events` |
| CVDOrderFlow | `buy_volume`/`sell_volume` em candles 1m |
| SpotPerpCarry | Binance spot (não backfilled) |

Backfill antes de re-testar estratégias NO_DATA:

```bash
python scripts/backfill_funding.py
python scripts/backfill_external_feeds.py
python scripts/backfill_candles.py --days 30
```

## Ciclo recomendado (mensal)

1. Backfill dados em falta
2. `python scripts/backtest_strategy_audit.py --quick`
3. Rever `docs/STRATEGY_AUDIT.md`
4. Atualizar `settings.yaml` (só KEEP + WATCH promovidos)
5. Paper 2 semanas → comparar live vs backtest
6. Mainnet só com PF ≥ 1.3 em paper
