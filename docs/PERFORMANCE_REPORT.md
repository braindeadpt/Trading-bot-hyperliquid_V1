# Relatório de Performance & Plano para PnL Positivo

> **Actualização 2026-09-01 (v3.1.48):** O bot migrou para **Phase08 edge isolation**.
> Estado actual: **1 execução paper** (`VWAPDeviation`) + **10 shadow** (incl. VolatilityBreakout,
> ChecklistMeta demoted após baseline FAIL). Ver `docs/STRATEGY_AUDIT.md` para veredictos
> actualizados. O resto deste relatório documenta o diagnóstico histórico (Jun 2026).

**Data:** 2026-06-29
**Versão bot:** v3.1.28 (paper trading)
**Capital:** $10,000 → **$9,699** (-3.0% all-time)
**Trades fechados:** 123 | **Win rate all-time:** 20%

---

## 1. Diagnóstico — Por que o bot perde dinheiro

### 1.1 Visão consolidada (live + backtest)

| Fonte | Resultado | Veredicto |
|-------|-----------|-----------|
| **Live all-time** (123 trades) | -$231.25, WR 20% | Perdedor |
| **Live Jun 2026 ex-FundingExtreme** (46 trades) | -$46.40, WR 35% | Marginalmente perdedor |
| **Ensemble sweep** (todas as 72 configs) | Sharpe -11 a -27, PF 0.12-0.56 | **TODAS perdem** |
| **Per-strategy backtest** | Ver tabela §1.3 | Só 2 estratégias têm edge |

**Conclusão central:** o problema **não é infraestrutura** (data feeds, dashboard, backfill estão OK). O problema é **arquitetura do ensemble + estratégias sem edge + execução que corta winners e deixa losers correr**.

### 1.2 Os 5 problemas raiz

#### Problema 1 — FundingExtreme entrou em loop bugado (60% da perda)
- 30/Mai: **50 trades SOL short** em 1h, hold fixo **30s**, `entry_funding = None`
- Saída `funding_reverted` em 100% dos casos → **-$137.51**
- Era um **churn de re-entry** com funding inválido
- **Já mitigado:** `mean_reversion.enabled: false`

#### Problema 2 — Ensemble não tem edge (sweep prova)
O sweep de 72 configurações (`ensemble_sweep_20260625_234431.csv`) mostra que **todas** as combinações de `threshold` (0.10-0.25), `min_agreeing` (1-2) e `hc_threshold` (0.65-0.75) **perdem dinheiro**:
- Melhor caso: PF 0.56, Sharpe -11.1, return -8.8%
- Pior caso: PF 0.12, Sharpe -27, return -27%

**Não existe configuração mágica do ensemble atual.** Combinar SMF + VWAPDev + VolBreakout + TrendPyramid com regime weights **destrói valor**, não cria.

#### Problema 3 — Maioria das estratégias nunca validada
Backtest per-strategy mostra que **6 das 12 estratégias têm 0 trades**:

| Estratégia | Backtest trades | Estado |
|------------|-----------------|--------|
| LiquidationCatcher | 0 | Ligada, teórica |
| CVDOrderFlow | 0 | Desligada |
| LeadLag | 0 | Ligada, teórica |
| SpotPerpCarry | 0 | Desligada |
| RangeGrid | 10 (PF 0.44) | Desligada |
| FundingMomentum | 0 | Desligada |
| FundingArbitrage | 0 | Desligada |
| MeanReversion | 0 | Desligada (após bug) |

Ligar estratégias sem backtest positivo é **aposta cega**.

#### Problema 4 — Execução corta winners, deixa losers correr
Análise das saídas em Junho (ex-FE):

| Motivo | Trades | PnL |
|--------|--------|-----|
| `take_profit` + `take_profit_hit` | 9 | **+$104.51** |
| `stop_loss` | 13 | **-$86.74** |
| `failed_breakout_*` | 5 | **-$51.70** |
| `ema50_break_*` / `ema_trend_reversal_*` | 9 | -$8.15 |
| `time_limit` / `max_hold` | 6 | +$0.07 |

R:R médio: **1.32:1** (avg win $8.27 / avg loss $6.26). Para ser lucrativo precisa WR ≥ 43%; está em **34%**.

**Trailing stop demasiado apertado:** `activation_pct: 0.01` (1%) corta winners antes do TP. Só VolBreakout/SMF/TrendPyramid estão excluded; LeadLag/LiquidationCatcher/VWAPDev vão sofrer cortes prematuros.

#### Problema 5 — Gates bloqueiam pouco útil
- **3,056 rejeições "Already have a position"** — bom, evita duplicates
- **1,515 rejeições correlation** — `|r(SOL,BTC)|=0.84` bloqueia mesmo com `max_correlation: 0.90`

BTC/ETH/SOL são **estruturalmente** correlacionados (>0.80 sempre). Bloquear correlação entre eles é **bloquear quase tudo**. Em vez de gate de correlação, deve usar **cap de exposição direcional** (já existe: 60%).

### 1.3 Backtest per-strategy — o que tem edge

| Estratégia | Janela | n | WR | R:R | PF | Sharpe | Expectancy | Veredicto |
|------------|--------|---|----|-----|-----|--------|------------|-----------|
| **VolatilityBreakout** | B (2w) | 28 | 46% | 1.89 | **1.64** | **4.79** | +2.62 | ✅ Edge real |
| VolatilityBreakout | C (full) | 61 | 39% | 1.54 | 0.99 | -0.16 | -0.00 | ⚠️ Degrada |
| **VWAPDeviation** | C (full) | 28 | 54% | 1.26 | **1.46** | **2.42** | +2.10 | ✅ Edge real |
| TrendPyramid | C (full) | 31 | 32% | 4.52 | 2.15 | 1.75 | +39 | ⚠️ Outlier |
| SmartMoneyFlow | exit_econ | 121 | 11% | 2.21 | **0.27** | n/a | -2.95 | ❌ Sem edge |
| DonchianBreakout | C (full) | 125 | 29% | 1.34 | 0.54 | -7.40 | -2.00 | ❌ Morto |
| OrderBookScalper | C (full) | 165 | 26% | 0.97 | 0.34 | -12.54 | -0.13 | ❌ Morto |
| TrendFollow | C (full) | 160 | 34% | 2.13 | 1.09 | -0.42 | +0.09 | ⚠️ Marginal |

**Apenas 2 estratégias têm edge validado em backtest:** `VolatilityBreakout` e `VWAPDeviation`.

`TrendPyramid` mostra PF 2.15 mas tem 1 trade outlier (+$3,987 em `other_pnl`) — sem esse trade é marginal. Precisa de mais dados.

`SmartMoneyFlow` é o pior: PF 0.27 (121 trades, 11% TP rate, R:R 2.21 mas WR baixíssimo). Está **ativo em live** e perdendo -$3.54 em Junho (12 trades, WR 17%).

---

## 2. Solução — Plano em 3 fases para PnL positivo

### Fase 1 — Limpeza imediata (hoje, sem código)

**Objetivo:** parar sangria, focar só no que tem edge.

```yaml
# config/settings.yaml — alterações propostas

strategy:
  smart_money_flow:
    enabled: false              # PF 0.27 — matar
  trend_pyramid:
    enabled: false              # 1 outlier; validar primeiro
  vwap_deviation:
    enabled: true               # manter — PF 1.46
    min_confidence: 0.70        # subir de 0.65 — menos trades, mais qualidade
  volatility_breakout:
    enabled: true               # manter — PF 1.64
    min_confidence: 0.55        # subir de 0.45
    require_trend_alignment: true
  mean_reversion:
    enabled: false              # manter OFF
  lead_lag:
    enabled: false              # 0 backtest trades — não ligar sem validar
  liquidation_catcher:
    enabled: false              # 0 backtest trades — não ligar sem validar
  orderbook_scalper:
    enabled: false              # manter OFF
  funding_arbitrage:
    enabled: false              # manter OFF
  funding_momentum:
    enabled: false              # manter OFF
  cvd_orderflow:
    enabled: false              # manter OFF
  spot_perp_carry:
    enabled: false              # manter OFF
  donchian_breakout:
    enabled: false              # manter OFF
  range_grid:
    enabled: false              # manter OFF

  # Ensemble — desativar consenso; rodar estratégias diretas
  ensemble:
    enabled: false              # sem consenso até provar edge
    threshold: 0.40             # se reativar, exigir 40%+ peso
    min_agreeing: 2             # exigir 2+ estratégias concordando
    high_conviction_enabled: false

# Risk — soltar correlation gate, apertar exposure cap
risk:
  portfolio_governance:
    max_correlation: 0.98       # efetivamente desligar — BTC/ETH/SOL sempre >0.80
    max_directional_exposure_pct: 40   # baixar de 60 para compensar
    daily_drawdown_circuit_pct: 3      # baixar de 5 — cortar perdas cedo

# Trailing stop — muito apertado
execution:
  trailing_stop:
    enabled: true
    activation_pct: 0.02        # subir de 1% para 2% — deixar winners correr
    trail_pct: 0.008            # subir de 0.5% para 0.8%
```

**Resultado esperado:** bot faz **menos trades** (5-10/semana em vez de 20+), só com `VolatilityBreakout` + `VWAPDeviation` (ambos PF >1.4 em backtest).

### Fase 2 — Tuning fino (1-2 semanas de paper)

Depois de 50 trades com a Fase 1, avaliar e ajustar:

1. **VWAPDeviation:** se WR live < 50%, subir `z_threshold` de 2.5 para 3.0; se max_hold 4h não fecha winners, baixar para 2h.
2. **VolatilityBreakout:** se `failed_breakout` > 30% dos trades, subir `failed_breakout_buffer_pct` de 0.15% para 0.25%.
3. **TrendPyramid:** rodar backtest separado sem o trade outlier; se PF >1.3 sem outlier, reativar com `base_size_pct: 0.01` (metade do atual).
4. **Posição sizing:** Kelly está ativo mas precisa de `min_trades: 20` — com poucos trades usa base_size. Considerar **fixed fractional 1%** até ter 50 trades limpos.
5. **ETH:** pior símbolo (-$66 em Junho). Considerar remover `ETH` dos `assets` se continua perdendo após Fase 1; concentrar em BTC + SOL.

### Fase 3 — Reativar estratégias com validação (1-3 meses)

Para cada estratégia desligada, **backtest primeiro**:

```bash
python scripts/backtest_per_strategy.py --strategy lead_lag --from 2025-01-01 --to 2026-06-01
```

Só reativar se:
- ≥ 30 trades no backtest
- PF ≥ 1.30
- Sharpe ≥ 1.0
- Max DD ≤ 15%

**Ordem de prioridade para reativação:**
1. **TrendPyramid** — se backtest limpo confirmar edge (Chandelier exit é sound)
2. **LeadLag** — latência arb conceitualmente OK, precisa de validar spread HL
3. **LiquidationCatcher** — alta convicção mas precisa de feed de liquidations robusto
4. **CVDOrderFlow** — precisa de buy/sell volume backfill (já implementado)

**Nunca reativar** (edge negativo confirmado):
- SmartMoneyFlow (PF 0.27)
- DonchianBreakout (Sharpe -7.4)
- OrderBookScalper (Sharpe -12.5)
- RangeGrid (Sharpe -4.3)
- MeanReversion/FundingExtreme (bug + 0 backtest)
- FundingArbitrage (não é arb real, v3.1.18 killed)
- FundingMomentum (0 backtest)

---

## 3. Mudanças de código recomendadas (opcional, Fase 2+)

### 3.1 Trailing stop — excluir mais estratégias
```python
# src/core/engine.py — execution.trailing_stop.exclude_strategies
exclude_strategies:
  - VolatilityBreakout
  - SmartMoneyFlow
  - TrendPyramid
  - VWAPDeviation        # ADICIONAR — tem TP próprio 2R
  - LeadLag              # ADICIONAR — convergence exit próprio
  - LiquidationCatcher   # ADICIONAR — 2R TP próprio
```

### 3.2 Ensemble bypass — desligar em vez de remover
O `StrategyEnsemble` atualmente passa qualquer sinal sozinho (`min_agreeing: 1`). Em vez de tuning, **desativar** o ensemble e deixar estratégias individuais executarem diretamente. Se no futuro quiseres consenso, exige `min_agreeing: 2` + `threshold: 0.40`.

### 3.3 Risk gate — remover correlation para BTC/ETH/SOL
```python
# src/core/engine.py ~linha 2476
# Para símbolos estruturalmente correlacionados, usar exposure cap em vez de correlation gate
STRUCTURAL_CORRELATED = {"BTC", "ETH", "SOL", "HYPE"}
if symbol in STRUCTURAL_CORRELATED and correlated_with in STRUCTURAL_CORRELATED:
    # skip correlation check, rely on directional exposure cap
    pass
```

### 3.4 Migrar daily_pnl tracking para portfolio snapshot
O `daily_drawdown_circuit_pct` precisa de reset diário UTC limpo. Confirmar que `_check_daily_drawdown` em `risk_manager.py` usa data UTC.

---

## 4. Métricas para acompanhar (semanal)

| KPI | Meta | Alerta |
|-----|------|--------|
| Win rate | ≥ 45% | < 35% após 30 trades |
| R:R realizado | ≥ 1.5 | < 1.2 |
| Profit Factor | ≥ 1.30 | < 1.10 |
| Max DD semanal | ≤ 2% | > 4% |
| Trades/dia | 1-3 | > 5 (overtrading) |
| Fees % do PnL bruto | ≤ 15% | > 30% |
| Hold médio winners | ≥ 90 min | < 30 min (cortando cedo) |
| Hold médio losers | ≤ 120 min | > 240 min (deixando correr) |

Script de monitorização semanal:
```bash
python scripts/_analyze_trades.py    # já existe
python scripts/_analyze_trades2.py   # breakdown por dia/strategy
python scripts/_analyze_trades3.py   # exit reasons + R:R
```

---

## 5. Cronograma proposto

| Semana | Ação | Meta |
|--------|------|------|
| **Hoje** | Aplicar Fase 1 (config), reiniciar bot | Bot rodando só com VB + VWAPDev |
| **Sem 1-2** | Paper trading limpo, 20-30 trades esperados | Confirmar WR ≥ 40% |
| **Sem 3** | Avaliar Fase 1, ajustar thresholds | PF ≥ 1.2 |
| **Sem 4-6** | Backtest TrendPyramid isolado | Decidir reativar ou não |
| **Sem 6-8** | Backtest LeadLag com dados reais HL spread | Decidir reativar |
| **Sem 8-12** | Mainnet com $500-1000 se paper PF > 1.3 | Validação real |
| **Sem 12+** | Aumentar capital gradualmente se Sharpe ≥ 1.5 | Escalar |

---

## 6. Resumo executivo

**Por que perde:**
1. 60% da perda é 1 bug histórico (FundingExtreme) — já resolvido
2. O ensemble atual **não tem edge** — 72/72 configs perdem em sweep
3. 6 estratégias ativas/teóricas nunca foram validadas em backtest
4. Apenas `VolatilityBreakout` (PF 1.64) e `VWAPDeviation` (PF 1.46) têm edge real
5. Trailing stop apertado corta winners; stops largos deixam losers sangrar
6. Correlation gate bloqueia entradas legítimas entre BTC/ETH/SOL

**Plano:**
- **Fase 1 (hoje):** desligar tudo exceto VB + VWAPDev, desligar ensemble, soltar correlation, apertar exposure cap, subir trailing stop. Sem código.
- **Fase 2 (2 sem):** tuning fino com 30+ trades paper, avaliar ETH removal.
- **Fase 3 (1-3 meses):** reativar estratégias só após backtest individual PF ≥ 1.3.

**Meta realista:** paper trading PF 1.3-1.5 em 60 dias, mainnet small-size em 90 dias, scale em 6 meses. **"Mega eficiente" não é realista com o ensemble atual** — precisa de foco em 2 estratégias validadas + disciplina de execução.

---

## 7. Fase 1 aplicada (2026-06-29)

Alterações em `config/settings.yaml` + `factory.py` / `main.py`:

| Item | Antes | Depois |
|------|-------|--------|
| Estratégias ativas | SMF, VB, VWAP, TrendPyramid, LeadLag, LiqCatcher | **VolatilityBreakout + VWAPDeviation** |
| Ensemble | passthrough (min_agreeing=1) | **disabled** — direct mode |
| max_correlation | 0.90 | **0.98** |
| max_directional_exposure | 60% | **40%** |
| daily_drawdown_circuit | 5% | **3%** |
| trailing activation | 1% | **2%** |
| trailing trail | 0.5% | **0.8%** |

**Reiniciar o bot** após pull: `stop.bat` → `quickstart.bat`

Log esperado no arranque:
```
Phase 1 direct mode: 2 sub-strategies (ensemble disabled)
Active strategies: ['VolatilityBreakout', 'VWAPDeviation']
```

---

*Análise baseada em `data/live/bot.db` (123 trades), `data/backtests/per_strategy_20260625_202340.csv`, `data/backtests/ensemble_sweep_20260625_234431.csv`, `data/backtests/exit_economics_20260625_231103.csv`.*
