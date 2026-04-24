# 📊 BACKTEST LEVERAGE OPTIMIZATION — Resultados

> Gerado em: 2026-04-25 00:57:49
> Combinações testadas: 20
> Combinações válidas (com trades): 20

---

## 📋 Sumário Executivo

Este relatório apresenta os resultados de um grid search sistemático
sobre todas as combinações de **leverage** (1x, 2x, 3x, 5x, 10x) e **timeframe**
(5m, 15m, 30m, 1h) para a estratégia de momentum Hyperliquid (BTC).

### Metodologia
- Capital inicial: $10,000
- Tamanho de posição: 10% do capital (máx. $100) por trade
- Stop loss: 2% no preço (impacto no collateral = 2% × leverage)
- Trailing stop: ativa após +1.5% de lucro no preço
- Taxas: 0.035% por lado (Hyperliquid taker fee)
- Dados: base de dados SQLite com candles, OI e funding rate

### Score Composto
As combinações são ordenadas por um score que penaliza drawdown e risco de liquidação:
```
Score = Return% − (Drawdown% × 2) − (LiqRisk% × 10) + (ProfitFactor × 5)
```

---

## 🏆 Top 3 Combinações Recomendadas

### #1 — L10x @ 15m (Score: 13.2)

| Métrica | Valor |
|---|---|
| **Total Return** | +2.83% |
| **Max Drawdown** | 0.71% |
| **Sharpe Ratio** | 0.35 |
| **Profit Factor** | 2.35 |
| **Win Rate** | 70.3% |
| **Total Trades** | 37 |
| **Avg Trade Return** | +7.7283% |
| **Worst Losing Streak** | 3 |
| **Liquidation Risk** | 0.0% |
| **Liquidation Count** | 0 |

**Longs:**
- Trades: 16 | Win Rate: 62.5% | PF: 2.13 | PnL: $+102.68

**Shorts:**
- Trades: 21 | Win Rate: 76.2% | PF: 2.52 | PnL: $+183.27

**Veredito:**
✅ **EXCELENTE** — Combinação robusta, pronta para forward test

### #2 — L5x @ 15m (Score: 12.5)

| Métrica | Valor |
|---|---|
| **Total Return** | +1.40% |
| **Max Drawdown** | 0.36% |
| **Sharpe Ratio** | 0.35 |
| **Profit Factor** | 2.35 |
| **Win Rate** | 70.3% |
| **Total Trades** | 37 |
| **Avg Trade Return** | +3.8642% |
| **Worst Losing Streak** | 3 |
| **Liquidation Risk** | 0.0% |
| **Liquidation Count** | 0 |

**Longs:**
- Trades: 16 | Win Rate: 62.5% | PF: 2.13 | PnL: $+51.34

**Shorts:**
- Trades: 21 | Win Rate: 76.2% | PF: 2.52 | PnL: $+91.63

**Veredito:**
✅ **EXCELENTE** — Combinação robusta, pronta para forward test

### #3 — L3x @ 15m (Score: 12.2)

| Métrica | Valor |
|---|---|
| **Total Return** | +0.83% |
| **Max Drawdown** | 0.22% |
| **Sharpe Ratio** | 0.34 |
| **Profit Factor** | 2.35 |
| **Win Rate** | 70.3% |
| **Total Trades** | 37 |
| **Avg Trade Return** | +2.3185% |
| **Worst Losing Streak** | 3 |
| **Liquidation Risk** | 0.0% |
| **Liquidation Count** | 0 |

**Longs:**
- Trades: 16 | Win Rate: 62.5% | PF: 2.13 | PnL: $+30.80

**Shorts:**
- Trades: 21 | Win Rate: 76.2% | PF: 2.52 | PnL: $+54.98

**Veredito:**
✅ **EXCELENTE** — Combinação robusta, pronta para forward test

---

## 📊 Tabela Comparativa Completa

| Leverage | TF | Return% | DD% | Sharpe | PF | WR% | Trades | Avg Trade% | Liq Risk% | Streak | Score |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 10x | 15m | +2.8% | 0.7% | 0.35 | 2.35 | 70.3% | 37 | +7.7283% | 0.0% | 3 | 13.2 |
| 5x | 15m | +1.4% | 0.4% | 0.35 | 2.35 | 70.3% | 37 | +3.8642% | 0.0% | 3 | 12.5 |
| 3x | 15m | +0.8% | 0.2% | 0.34 | 2.35 | 70.3% | 37 | +2.3185% | 0.0% | 3 | 12.2 |
| 2x | 15m | +0.5% | 0.1% | 0.34 | 2.35 | 70.3% | 37 | +1.5457% | 0.0% | 3 | 12.0 |
| 1x | 15m | +0.3% | 0.1% | 0.32 | 2.35 | 70.3% | 37 | +0.7728% | 0.0% | 3 | 11.9 |
| 1x | 1h | +0.0% | 0.0% | 0.07 | 1.25 | 50.0% | 2 | +0.2761% | 0.0% | 1 | 6.2 |
| 2x | 1h | +0.0% | 0.0% | 0.08 | 1.25 | 50.0% | 2 | +0.5521% | 0.0% | 1 | 6.2 |
| 3x | 1h | +0.0% | 0.1% | 0.09 | 1.25 | 50.0% | 2 | +0.8282% | 0.0% | 1 | 6.1 |
| 5x | 1h | +0.0% | 0.1% | 0.09 | 1.25 | 50.0% | 2 | +1.3803% | 0.0% | 1 | 6.1 |
| 1x | 5m | +0.1% | 0.2% | 0.07 | 1.25 | 55.0% | 40 | +0.2314% | 0.0% | 4 | 5.9 |
| 10x | 1h | +0.1% | 0.2% | 0.10 | 1.25 | 50.0% | 2 | +2.7607% | 0.0% | 1 | 5.9 |
| 1x | 30m | +0.0% | 0.1% | 0.05 | 1.20 | 60.0% | 25 | +0.1565% | 0.0% | 5 | 5.7 |
| 2x | 5m | +0.2% | 0.4% | 0.09 | 1.25 | 55.0% | 40 | +0.4629% | 0.0% | 4 | 5.6 |
| 2x | 30m | +0.1% | 0.3% | 0.07 | 1.20 | 60.0% | 25 | +0.3131% | 0.0% | 5 | 5.5 |
| 3x | 5m | +0.2% | 0.6% | 0.09 | 1.25 | 55.0% | 40 | +0.6943% | 0.0% | 4 | 5.4 |
| 3x | 30m | +0.1% | 0.4% | 0.08 | 1.20 | 60.0% | 25 | +0.4696% | 0.0% | 5 | 5.2 |
| 5x | 5m | +0.4% | 0.9% | 0.10 | 1.25 | 55.0% | 40 | +1.1572% | 0.0% | 4 | 4.8 |
| 5x | 30m | +0.2% | 0.7% | 0.09 | 1.20 | 60.0% | 25 | +0.7827% | 0.0% | 5 | 4.7 |
| 10x | 5m | +0.9% | 1.8% | 0.10 | 1.25 | 55.0% | 40 | +2.3144% | 0.0% | 4 | 3.6 |
| 10x | 30m | +0.4% | 1.4% | 0.09 | 1.20 | 60.0% | 25 | +1.5654% | 0.0% | 5 | 3.5 |

---

## 📈 Análise de Trade-offs (Return vs Risk)

### Regra Geral Observada

| Leverage | Característica | Recomendação |
|---|---|---|
| **1x** | Baixo risco, baixo retorno | Capital preservação, aprendizagem |
| **2x** | Risco moderado, retorno decente | **Sweet spot** para paper money / testnet |
| **3x** | Risco notável, retorno acelerado | Apenas com stop loss apertado e monitorização |
| **5x** | Alto risco, volatilidade extrema | Não recomendado sem experiência confirmada |
| **10x** | Risco de liquidação real | **Evitar** — probabilidade de wipeout significativa |

### Timeframes

| Timeframe | Vantagem | Desvantagem |
|---|---|---|
| **5m** | Entradas rápidas, mais oportunidades | Mais noise, mais falsos sinais |
| **15m** | Equilíbrio ótimo (recomendado) | Menos trades que 5m |
| **30m** | Sinais mais limpos, menos stress | Menor frequência de entrada |
| **1h** | Tendências de longo prazo | Muito poucos trades, lag elevado |

### Conclusão

A combinação **L10x @ 15m** apresenta o melhor score bruto (**13.2**) devido ao excelente profit factor (2.35) e baixo drawdown (0.71%). No entanto, **recomenda-se cautela com leverage extremo** para quem está em fase de aprendizagem.

**Recomendação por nível de experiência:**

| Perfil | Leverage recomendado | Timeframe | Porquê |
|---|---|---|---|
| **Iniciante (paper money)** | **L2x–L3x @ 15m** | 15m | Risco controlado, métricas sólidas, margem para erros |
| **Intermédio (testnet)** | **L5x @ 15m** | 15m | Return aceitável, ainda com drawdown baixo (<1%) |
| **Avançado (mainnet)** | **L10x @ 15m** | 15m | Máxima eficiência, mas exige gestão de risco impecável |

**Começar com:**
- **L3x @ 15m** — Score 12.2, Return +0.83%, DD apenas 0.22%
- Este é o **sweet spot** para quem está a aprender: métricas excelentes (PF 2.35, WR 70.3%) com risco muito contido

O timeframe **15m domina** em todas as métricas. Os outros timeframes (5m, 30m, 1h) apresentam resultados significativamente inferiores e não são recomendados para esta estratégia com os parâmetros atuais.

---

*Relatório gerado automaticamente por `scripts/optimize_leverage.py` em 2026-04-25 00:57:49*
