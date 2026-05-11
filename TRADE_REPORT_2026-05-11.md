# 📊 TRADE ANALYSIS REPORT
## Trading Bot Hyperliquid V1 — Período: 8–11 Mai 2026

---

## 1. Resumo Executivo

| Métrica | Valor |
|---------|-------|
| **Total de Trades** | 16 |
| **Período** | 2026-05-08 a 2026-05-11 (3 dias) |
| **PnL Total** | **-$317.66** 🔴 |
| **Win Rate** | **12.5%** (2W / 7L / 7F) |
| **Média por Trade** | -$19.85 |
| **Maior Ganho** | +$97.38 |
| **Maior Perda** | -$165.06 |
| **Drawdown Máximo** | ~$274.60 (só trades com perda) |

**Veredito:** Estratégia **LiquidationCatcher** está com performance negativa. Win rate muito baixo (<13%) e muitas trades flat (43.8%) indicam problemas na lógica de entrada/saída.

---

## 2. Breakdown por Ativo

| Ativo | Trades | Wins | Losses | Flats | Win Rate | PnL |
|-------|--------|------|--------|-------|----------|-----|
| **BTC** | 13 | 2 | 7 | 4 | 15.4% | -$317.66 |
| **ETH** | 3 | 0 | 0 | 3 | 0.0% | $0.00 |

**Observação:** ETH só teve trades flat — entrou e saiu no mesmo preço (~0s). Possível bug na execução ou condição de saída imediata.

---

## 3. Breakdown por Lado (Side)

| Side | Trades | PnL Total |
|------|--------|-----------|
| Short | 10 | -$289.54 |
| Long | 6 | -$28.12 |

**Insight:** Shorts estão a perder mais. Em mercado de baixa/lateal, os shorts deveriam performar melhor se a estratégia estivesse a capturar liquidations corretamente.

---

## 4. Análise de Motivos de Saída (Exit Reasons)

| Motivo | Count | PnL | Nota |
|--------|-------|-----|------|
| `max_hold_30min` | 8 | -$274.60 | **Principal perda** — trades a forçar saída após 30min |
| `funding_reverted` | 4 | $0.00 | Saída imediata (~0s) — funding normalizou |
| `vwap_reversal_short` | 2 | $0.00 | Saída imediata (~0s) — VWAP reverteu |
| `vwap_reversal_long` | 1 | $0.00 | Saída imediata (~0s) |
| `time_limit_max_hold` | 1 | -$43.06 | Tempo máximo excedido |

**Problema Crítico:** 7 trades (43.8%) duraram **~0 segundos** — entraram e saíram no mesmo preço. Isso sugere:
1. Condição de saída está a ser avaliada imediatamente após entrada
2. `funding_reverted` / `vwap_reversal` está a cancelar trades antes de terem chance de funcionar
3. **Custo de comissão** não está a ser contabilizado (0% PnL mas perdeu comissão)

---

## 5. Análise de Duração

| Métrica | Valor |
|---------|-------|
| Média | 956s (~16min) |
| Mediana | 1347s (~22min) |
| Mínimo | 0.0s |
| Máximo | 1800s (30min) |

**Distribuição bimodal:**
- 7 trades: ~0s (flat)
- 8 trades: ~1800s (30min — max hold)
- 1 trade: ~894s

**Conclusão:** A estratégia ou funciona imediatamente (sai a 0s) ou fica presa até ao time limit de 30min.

---

## 6. 🚨 Problemas Identificados

### 6.1 Duplicação de Trades (BUG)
Trades 9/10, 11/12, 13/14, 15/16 têm **dados idênticos**:
- Mesmo entry_price, exit_price, size, pnl, duration
- Diferem apenas no ID e milissegundos de saída

**Possível causa:** Race condition no `ExecutionEngine` ou sinal duplicado a ser processado 2x.

### 6.2 Saídas Imediatas (43.8% flat)
7 trades saíram a 0s com PnL = $0. Isso não é normal para uma estratégia de liquidation catching.

**Possível causa:**
- `on_position()` a verificar exit condition antes de confirmar preenchimento
- `LiquidationCatcher` a emitir exit signal imediatamente após entry

### 6.3 Time Limit como Stop Loss
8 trades (50%) saíram por `max_hold_30min` com PnL negativo (-$274.60). O time limit está a funcionar como stop loss forçado.

**Sugestão:** Aumentar time limit OU adicionar trailing stop real.

### 6.4 Win Rate de 12.5%
Benchmark para estratégia de mean-reversion/liquidation: win rate deve ser >40%. 12.5% indica:
- Entradas em direção errada (contra-trend em vez de mean-reversion)
- Stop losses muito apertados
- Má identificação de liquidation events

---

## 7. Recomendações

### 🔴 Prioridade Alta

1. **Corrigir duplicação de trades**
   - Adicionar lock no `ExecutionEngine.enter_position()`
   - Verificar se já existe posição aberta antes de executar

2. **Investigar saídas a 0s**
   - Adicionar delay mínimo (ex: 30s) antes de avaliar exit conditions
   - Logar o estado interno do `LiquidationCatcher` no momento da saída

3. **Rever lógica do LiquidationCatcher**
   - Win rate de 12.5% é insustentável
   - Verificar se o proxy de liquidation está a funcionar corretamente
   - Considerar adicionar filtro de tendência (ADX) para evitar contrarian em tendência forte

### 🟡 Prioridade Média

4. **Ajustar time limits**
   - 30min pode ser muito curto para mean-reversion
   - Testar 60min ou 120min

5. **Adicionar comissão aos cálculos**
   - Trades flat a 0s ainda pagam comissão (0.04% Hyperliquid)
   - 7 trades flat = ~$15-20 de comissão não contabilizada

6. **Implementar trailing stop**
   - Substituir time limit hard por trailing stop dinâmico

### 🟢 Prioridade Baixa

7. **Adicionar mais ativos**
   - SOL, ARB, etc. para diversificar

8. **Backtest com dados históricos**
   - Testar LiquidationCatcher em dados de março/abril antes de continuar live

---

## 8. Próximos Passos

1. **Parar** o bot ou reduzir size para mínimo até corrigir bugs
2. **Debug** o `LiquidationCatcher.on_position()` para entender saídas a 0s
3. **Fix** race condition de duplicação
4. **Backtest** com novos parâmetros
5. **Relaunch** com size reduzido (10% do atual)

---

*Report gerado em: 2026-05-11 09:30 UTC*
*Fonte: data/live/bot.db (16 trades)*
