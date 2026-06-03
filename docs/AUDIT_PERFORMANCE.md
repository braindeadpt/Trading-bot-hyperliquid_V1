# Auditoria de performance — Hyperliquid Bot v3.1

**Data:** 2026-06-03  
**Fonte:** `data/live/bot.db`, `logs/bot.log`, código em `src/`

---

## Resumo executivo

O bot **não está a “falhar o mercado” de forma uniforme** — o histórico mostra um padrão claro de **destruição de edge por custos + churn**, não ausência total de sinais válidos.

| Métrica (fechados) | Valor |
|--------------------|-------|
| Total trades | 81 |
| PnL total | **≈ −$212.64** |
| Vitórias | 10 (~12%) |
| Últimos 7 dias | 74 trades, **≈ −$193.76** |

**Conclusão:** A maior parte das perdas vem de **FundingExtreme** (50 trades, 0 vitórias, saída `funding_reverted` ~30s). Com a estratégia **desligada** e as correções abaixo, o comportamento esperado melhora significativamente em paper.

---

## 1. Causa raiz #1 — FundingExtreme (churn)

| Campo | Valor |
|-------|-------|
| Trades 7d | 50 |
| PnL | **−$137.51** |
| Vitórias | 0 |
| Saída dominante | `funding_reverted` |
| Hold médio | **~30s** (após `min_hold` do engine) |
| Perda média | **~−0.14%** por trade |

**Mecanismo:** Entrada em funding “extremo” marginal → funding normaliza em segundos → saída com perda pequena mas **garantida** após round-trip (taker ~0.07% + slippage paper).

**Estado atual:**

- `mean_reversion.enabled: false` em `config/settings.yaml`
- Governor desativou FundingExtreme (Sharpe ~−37)
- Código pós-patch: `min_funding_exit_hold_ms: 15min`, `min_profit_before_funding_exit_pct`, limiares absolutos de entrada

**Ação:** Manter OFF até validação em paper com DB limpo. Não reativar só porque o mercado “favorece shorts”.

---

## 2. Causa raiz #2 — Bypass “high-conviction” do ensemble

Com `high_conviction_threshold: 0.75`, **uma única** sub-estratégia com confiança ≥0.75 entra **sem** `min_agreeing: 2`.

**Exemplo real (trade #81):**

- ETH **long** via VolatilityBreakout (squeeze)
- Mercado em tendência bearish → entrada **contra-tendência**
- Saída `failed_breakout_below_mid` após ~92 min, **−$7.83**

**Correções aplicadas:**

- `high_conviction_threshold: 0.90`
- `high_conviction_exclude`: VolatilityBreakout, VWAPDeviation, FundingExtreme
- `threshold` ensemble: 0.12 → **0.14** (exige score combinado ligeiramente maior)

---

## 3. Causa raiz #3 — VolatilityBreakout

| Problema | Efeito |
|----------|--------|
| Sem filtro de tendência | Longs em mercado em queda |
| `failed_breakout_*` ao cruzar BB mid | Saída prematura em volatilidade (trades #76–78, #81) |
| Confiança até 0.95 | Facilitava bypass high-conviction |

**Correções aplicadas (`volatility_breakout.py` + config):**

- `require_trend_alignment`: long só se preço > EMA20 e EMA20 > EMA50; short o inverso
- `failed_breakout_min_hold_ms: 45` min antes de saída por falha
- `failed_breakout_buffer_pct: 0.1%` além da banda média

**Nota positiva:** Trade #79 — ETH short, `take_profit_hit`, **+$20.96** em 7 min — prova que shorts com TP podem funcionar.

---

## 4. Shorts vs longs (7 dias)

| Lado | Trades | PnL | Vitórias |
|------|--------|-----|----------|
| Short | 69 | −$140.86 | 9 |
| Long | 5 | −$52.90 | 1 |

Os shorts perdem **menos por trade em número**, mas o volume veio sobretudo de **FundingExtreme** (shorts contrarian em funding positivo, saída imediata). Não é “o bot não sabe fazer short” — é **estratégia + custos + saídas**.

---

## 5. Outras estratégias (7d)

| Estratégia | Trades | PnL | Notas |
|------------|--------|-----|-------|
| SmartMoneyFlow | 9 | −$31.89 | Revisar filtros em tendência forte |
| VWAPDeviation | 3 | −$18.13 | Mean reversion; trade #80 long −$31.91 stop |
| DonchianBreakout | 6 | −$4.71 | Quase break-even |
| VolatilityBreakout | 6 | −$1.52 | Melhor após filtros; 2 vitórias |

---

## 6. Custos e paper trading

Round-trip típico: **~0.11–0.17%** (taker 0.035%×2 + slippage). Qualquer estratégia com hold <2 min e edge <0.2% **perde sistematicamente**.

`risk.paper_slippage_pct` já foi reduzido (0.05 → 0.02) no commit `4802c0a`.

---

## 7. Monitorização

- `_periodic_summary_loop`: corrigido (`portfolio.current_capital` em vez de `.capital`)
- Script: `python scripts/audit_performance.py`

---

## 8. Checklist pós-deploy

1. `git pull` e **reiniciar** o bot (`stop.bat` / `service.bat`)
2. Confirmar em log: **sem** `Ensemble HIGH-CONVICTION` para VolatilityBreakout/VWAP
3. Confirmar: **sem** entradas FundingExtreme (`mean_reversion.enabled: false`)
4. Opcional: DB novo (`data/live/bot.db` backup) para métricas limpas
5. Correr semanalmente: `python scripts/audit_performance.py`

---

## 9. Prioridades futuras (não implementadas neste patch)

- VWAPDeviation: bloquear longs quando ADX>25 e EMA20<EMA50
- SmartMoneyFlow: rever pesos em regime `trend` bearish
- Reativar FundingExtreme só após N trades paper com hold médio >15 min e Sharpe >0

---

*Gerado após auditoria completa de DB, logs e código. Ver commits `4802c0a` (funding) e alterações em ensemble/volatility_breakout nesta sessão.*
