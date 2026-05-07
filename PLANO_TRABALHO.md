# Hyperliquid Bot — Plano de Trabalho 2026-05-07
## Roadmap para excelência operacional

---

## FASE 0: Fundações (estabilizar o que temos)
**Tempo estimado: 3-4h | Impacto: 🔥🔥🔥 (crítico)**

| # | Tarefa | Descrição | Tempo | Estado |
|---|---|---|---|---|
| 0.1 | Integrar FundingAggregator no engine | O código existe (`src/exchanges/funding_aggregator.py`). É só ligar ao `engine.py` — `_build_market_event` já tem placeholders. Adiciona funding cross-exchange (Binance, Bybit, OKX) e OI agregado ao MarketEvent. | 1.5h | ✅ DONE |
| 0.2 | WebSocket L2 Orderbook Hyperliquid | Subscrever `l2Book` no WS da HL. Criar `Orderbook` dataclass com bids/asks. Emitir para o data_bus. | 1.5h | ✅ DONE |
| 0.3 | Orderbook metrics engine | Calcular OIR (Orderbook Imbalance Ratio), wall detection, depth quality a cada tick. Guardar em `_last_orderbook`. | 1h | ✅ DONE |

---

## FASE 1: Risk & Execution (proteger capital)
**Tempo estimado: 4-5h | Impacto: 🔥🔥🔥 (crítico)**

| # | Tarefa | Descrição | Tempo | Estado |
|---|---|---|---|---|
| 1.1 | ATR-based stop loss | Substituir stops fixos (3.5% / 3%) por ATR(14) × 2.5. Stop dinâmico adapta-se à volatilidade real do asset. | 1h | ✅ DONE |
| 1.2 | Volatility targeting sizing | Position size = base_size × (target_vol / realized_vol_20d). BTC com 25% vol → 1.6x. SOL com 60% vol → 0.5x. | 1.5h | ✅ DONE |
| 1.3 | Trailing take-profit | Breakeven @ +1R. Trailing stop @ +2R (ATR × 1.5). Maximiza tendências que correm. | 1.5h | ✅ DONE |
| 1.4 | Slippage estimation (realista) | Usar L2 book para estimar slippage antes de entrar. Se slippage > 0.2%, rejeita sinal. | 1h | ✅ DONE |

---

## FASE 2: Estratégias v2 (melhorar edge)
**Tempo estimado: 5-6h | Impacto: 🔥🔥 (alto)**

| # | Tarefa | Descrição | Tempo |
|---|---|---|---|
| 2.1 | Regime filter (ADX) | ADX(14) > 25 = tendência → TrendFollow 70%, MeanReversion 30%. ADX < 20 = range → inverte pesos. | 1.5h | ✅ DONE |
| 2.2 | SmartMoneyFlow upgrade | Adicionar: OIR filter (>0.6 confirma long), wall detection (evitar paredes), RSI(14) filter (40-70). | 2h | ✅ DONE |
| 2.3 | FundingExtreme upgrade | Funding percentil(90) dinâmico. Cross-exchange confirmation. OI_delta < 0 filter. Predicted funding check. | 2h | ✅ DONE |
| 2.4 | Cooldown inteligente | Cooldown aumenta após loss (1h → 2h → 4h). Reseta quando funding normaliza ou ADX muda regime. | 1h | ✅ DONE |

---

## FASE 3: Novas Estratégias (diversificar edge)
**Tempo estimado: 8-10h | Impacto: 🔥🔥 (alto)**

| # | Tarefa | Descrição | Tempo |
|---|---|---|---|
| 3.1 | FundingArbitrage | Long funding-negative, short funding-positive. Hedge ratio 1:1. Spread > 1.2%, each leg > 0.5%. | 3h | ✅ DONE |
| 3.2 | VWAPDeviation | Preço afasta >2.5σ do VWAP(1h) + volume >150% média → mean reversion para VWAP. | 2.5h |
| 3.3 | LiquidationCatcher | Monitorizar clusters de liquidations. $50M+ numa direção em <5min → entrar oposto. Stop 1% ATR, TP 2R. | 3h |

---

## FASE 4: Portfolio Heat & Governance
**Tempo estimado: 4-5h | Impacto: 🔥🔥 (alto)**

| # | Tarefa | Descrição | Tempo |
|---|---|---|---|
| 4.1 | Portfolio correlation limit | Max 60% do book na mesma direção (long/short). Evita directional overexposure. | 1.5h |
| 4.2 | Sector exposure cap | Max 30% do capital em crypto como asset class (se tiveres outras classes). | 1h |
| 4.3 | Daily drawdown circuit | Se drawdown diário > 5%, fecha posições abertas e pára entradas até amanhã. | 1.5h |
| 4.4 | Kelly Criterion sizing | Usar win rate + R/R histórico para ajustar size per strategy. Half-Kelly para segurança. | 1.5h |

---

## FASE 5: Observabilidade & Auto-recovery
**Tempo estimado: 3-4h | Impacto: 🔥 (médio)**

| # | Tarefa | Descrição | Tempo |
|---|---|---|---|
| 5.1 | Auto-log monitoring | Heartbeat que lê `fatal_errors.log` a cada 15 min. Alerta em caso de erros novos. | 1.5h |
| 5.2 | Auto-restart on crash | Se o bot crasha, restart automático em paper mode. Notifica com razão do crash. | 1h |
| 5.3 | Dashboard: estratégia drill-down | Clicar numa estratégia na dashboard → ver histórico de sinais, win rate, PnL, parâmetros. | 1.5h |

---

## Resumo por Impacto

| Impacto | Tarefas | Tempo total |
|---|---|---|
| 🔥🔥🔥 Crítico | 0.1-0.3 + 1.1-1.4 | 7-9h |
| 🔥🔥 Alto | 2.1-2.4 + 3.1-3.3 + 4.1-4.4 | 17-21h |
| 🔥 Médio | 5.1-5.3 | 3-4h |
| **TOTAL** | **Tudo** | **~27-34h** |

---

## Como usar este plano

**Opção A — Foco total (recomendado):**
> Fases 0+1 apenas (7-9h). Isto resolve 80% dos problemas. Depois testa 1 semana em paper, mede resultados, e só depois avança.

**Opção B — Agile sprint:**
> 1 tarefa por sessão (1-2h cada). Vais acumulando melhorias sem quebrar o que funciona.

**Opção C — Tudo de uma vez:**
> 27-34h de trabalho contínuo. Risco: mais difícil debugar se algo quebrar. Recompensa: bot completo em 2-3 dias.

---

## Notas
- Todas as tarefas são testáveis em paper mode
- Nenhuma tarefa quebra backward compatibility (podes fazer 1 a 1)
- Cada fase tem valor independente — não precisas completar tudo
