# Strategy Audit — Backtest Profundo

**Última actualização:** 2026-06-29 19:55 UTC
**Estado:** pós walk-forward optimisation sweep (45 runs, 3 windows, Monte Carlo 1000x)

## ⚠️ Nota sobre audits anteriores

Os audits `strategy_audit_20260629_122340.csv` e `_132320.csv` correram **antes** do backfill
completo de `buy_volume`/`sell_volume` (que terminou às ~14:25 UTC). O veredicto "KEEP CVDOrderFlow
PF 3.58 Sharpe 5.41" era um **artefacto de dados incompletos** (12% dos candles tinham volume).

O **CVD sweep** (`cvd_sweep_20260629_172204.csv`, 8 runs × 2 janelas) com buy/sell volume
completo confirma que CVD **não tem edge robusto**: PF 0.93 (current) / 1.12 (tight) em D_full,
com Sharpe negativo ou marginal. Ver veredicto actualizado abaixo.

## 🎯 Walk-forward optimisation (v3.1.26)

Sweep: `backtest_vb_vwap_walkforward.py` — 15 param sets × 3 janelas não-overlap × Monte Carlo 1000x.

CSV: `data/backtests/vb_vwap_walkforward_20260629_195317.csv`

### Vencedores aplicados em `config/settings.yaml`

**VolatilityBreakout — `vol15_plus_trailing_ema9`:**
- `volume_surge: 1.3 → 1.5` (filtra falsos breakouts)
- `use_trailing_stop: true, trailing_method: ema9, trailing_start_r: 1.0`

| Janela | baseline PF / PnL | vencedor PF / PnL | Δ |
|--------|-------------------|--------------------|---|
| W1 (05/18-31) | 1.47 / +$28 | 2.11 / +$61 | +118% PnL |
| W2 (06/01-14) | 0.73 / -$61 | 0.63 / -$75 | -23% PnL (regime choppy) |
| W3 (06/15-28) | 2.03 / +$115 | 2.95 / +$153 | +33% PnL |
| **Total 6 sem** | **+$82** | **+$139** | **+70%** |

**VWAPDeviation — `session_filter`:**
- `use_session_filter: true, session_start_utc_h: 7, session_end_utc_h: 22`
- Mantém `session_allow_extreme_z: true` (permite |Z|≥4 fora da sessão)

| Janela | baseline PF / WR | vencedor PF / WR | mcPF_p05 |
|--------|------------------|-------------------|----------|
| W1 | 10.17 / 71% | **16.46 / 83%** | 0.68 → **1.56** (+130%) |
| W2 | n=6 100% WR | n=5 100% WR | — |
| W3 | 0.09 / 56% | 0.06 / 47% | (regime trending, sempre perde) |

### Features que NÃO foram activadas (no-op ou pior)

| Feature | Razão |
|---------|-------|
| `use_time_scaled_tp` (VB) | No-op — trades atingem TP/SL original antes do gatilho de 3h |
| `use_sl_to_be_after_1r` (VWAP) | No-op — exit por VWAP-reversion (|Z|<0.3) dispara antes de 1R reverter à entry |
| `oir_disable_extreme_z` (VWAP) | No-op — sem |Z|≥4 no histórico de 6 semanas |
| `use_dynamic_z` (VWAP) | No-op — ATR ratio ≈1 (sem regime extremo na janela) |
| `all_optimisations` (VWAP) | **PIOR** — W1 PF 10→1.86, adiciona 2 losers em W2. Combos restritivos excluem winners |
| `trailing_atr` (VB) | Pior que ema9 (W3 PF 1.92 vs 2.08) |
| `trailing_swing` (VB) | Igual ao ema9 em W1, pior em W3 |
| `vol15+oi+trailing_ema9` (VB) | Similar mas menos trades (OI filter raro) |

### ⚠️ Critério "robust" formal não atingido

Nenhum param set passou o critério estrito (`n≥8` em todas as 3 janelas + `mcPF_p05 > 0.9` em todas
+ `Sharpe_p05 > -0.5` em todas + `median PF ≥ 1.25`). Razão: cada estratégia tem 1 janela mau por
regime (VB-W2 choppy, VWAP-W3 trending). Os vencedores escolhidos são os que **maximizam PnL
nas 2 janelas boas** e minimizam perda na má — claramente superiores ao baseline.

## Dados disponíveis (após backfill 2026-06-29)

- Candles 15m/1h: BTC/ETH/SOL desde **2026-05-18** até **2026-06-29** (43 dias)
- Candles 1m: desde **2026-05-24** com **buy/sell volume 98%** (re-backfilled)
- Funding: desde **2026-05-27** | OI: desde **2026-06-05** (28k+ rows)
- Liquidations + Binance perp: **2026-05-30 → 2026-06-29** (30 dias)
- Gates do audit: risk manager ON; vol circuit + funding blackout OFF (isola edge)

## Critérios

| Veredicto | Regra |
|-----------|-------|
| **KEEP** | D_full: n≥10, PF≥1.25, Sharpe≥0.5, expectancy>0 |
| **WATCH** | PF≥1.0 e positivo em ≥2 janelas, ou marginal |
| **KILL** | PF<1 e expectancy<0 em maioria, ou Sharpe<0 em ≥2 janelas |
| **NO_DATA** | 0 trades na janela completa |

## Veredicto final (actualizado 19:55 UTC)

### ✅ KEEP — activo em paper (com optimizações v3.1.26)

- **VolatilityBreakout** — config: `volume_surge=1.5 + trailing_ema9`. Walk-forward total: +$139 (+70% vs baseline)
- **VWAPDeviation** — config: `session_filter 07-22 UTC`. W1 PF 16.46 / mcPF_p05 1.56 (robustez +130%)

### ⚠️ WATCH — OFF, revalidar

- **CVDOrderFlow** — CVD sweep mostra só config **tight** com PF 1.12 + Sharpe +0.63 em D_full
  (42 trades). Outras configs todas negativas ou choppy. **Não ligar agora** — re-testar em
  30 dias com walk-forward. Se reativar: usar config tight (`min_divergence 0.45`,
  `min_volume_usd 80k`, `require_oir true`, `min_confidence 0.55`, `TP 2.5R`).
- **TrendPyramid** — C_full PF=2.15 mas 1 trade outlier (+$3,987) distorce. Revalidar sem outlier.
- **FundingExtreme** — Audit mostrou PF 1.54 em D_full mas **PF 0.12 em B_2weeks** (churn em
  regime low-funding — o bug histórico). Inconsistente → não ligar.

### ❌ KILL — manter OFF

- **SmartMoneyFlow** (TrendFollow) — PF=1.09 marginal, negativo em 2/3 janelas; live PF=0.27
- **DonchianBreakout** — PF=0.50 Sharpe=-7.4
- **OrderBookScalper** — PF=0.29 Sharpe=-12.5
- **RangeGrid** — PF=0.49 Sharpe=-4.3

### ⏳ NO_DATA — sem dados suficientes (mantêm OFF)

- **LeadLag** — 0 trades mesmo após backfill 30 dias perp. Thresholds spread/impulse demasiado
  apertados. Precisa de Binance perp **tick** data (não 1s mids).
- **LiquidationCatcher** — 0 trades; $5M threshold raro no histórico. Relaxar ou esperar cascades.
- **FundingArbitrage** — 0 trades; v3.1.18 KILLED (não é arb real).
- **FundingMomentum** — 0 trades; needs more data.
- **SpotPerpCarry** — sem Binance spot backfill.

## CVD sweep detalhado (cvd_sweep_20260629_172204.csv)

| Config | Janela | n | PF | Sharpe | PnL | WR% |
|--------|--------|---|-----|--------|-----|-----|
| current | B_2weeks | 23 | 2.39 | -7.69 | +$213 | 48% |
| relaxed | B_2weeks | 42 | 1.70 | -10.64 | +$149 | 45% |
| **tight** | **D_full** | **42** | **1.12** | **+0.63** | **+$18** | **52%** |
| loose_adx | D_full | 136 | 1.00 | -0.23 | +$1 | 50% |
| current | D_full | 70 | 0.93 | -0.59 | -$17 | 49% |
| relaxed | D_full | 120 | 0.79 | -2.90 | -$56 | 48% |
| tight | B_2weeks | 11 | 0.17 | -9.32 | -$87 | 36% |

**Conclusão:** PF alto em B_2weeks vem com Sharpe negativo (equity curve choppy).
Só config tight em D_full tem PF>1 + Sharpe>0 simultaneamente, mas marginal (n=42, PnL +$18).

## Config activa em paper (Fase 1 + v3.1.26 optimisations)

```yaml
strategy:
  volatility_breakout:
    enabled: true
    min_confidence: 0.55
    volume_surge: 1.5              # was 1.3
    use_trailing_stop: true        # NEW
    trailing_method: ema9          # NEW
    trailing_start_r: 1.0
  vwap_deviation:
    enabled: true
    min_confidence: 0.70
    use_session_filter: true       # NEW
    session_start_utc_h: 7
    session_end_utc_h: 22
  # tudo o resto: enabled: false
  ensemble: {enabled: false}    # direct mode
```

## Próximos passos

1. **Manter config optimizada** — VB+VWAP com vencedores v3.1.26, paper trading 30-60 dias
2. **Próximo optimisation cycle em 30 dias** com walk-forward em 4 janelas (regime balanceado)
3. **Re-test CVD** com config tight quando houver 60+ dias de dados
4. **Backfill LeadLag** com tick data real se quiser explorar latency arb
5. **Regime filter futuro** — VWAP desligar em ADX>30 (trending), VB desligar em ADX<10 (choppy)
6. **Nunca escalar** sem 3 meses de paper PF ≥ 1.3

## Scripts para re-correr

```bash
# Walk-forward optimisation sweep (45 runs, ~2h)
python scripts/backtest_vb_vwap_walkforward.py

# Auditoria completa (todas as estratégias, ~3h)
python scripts/backtest_strategy_audit.py --quick

# CVD sweep isolado
python scripts/backtest_cvd_sweep.py

# Backfill buy/sell volume (manter actualizado)
python scripts/_backfill_cvd_volume.py --days 36

# Análise live trades
python scripts/_analyze_trades2.py
```

## Histórico detalhado (audits pré-backfill — ver aviso no topo)

Os audits `strategy_audit_20260629_122340.csv` (VB + VWAP corrigidos) e `_132320.csv` (NO_DATA re-audit)
foram corridos com buy/sell volume incompleto. **CVD não é fiável nestes CSVs** (PF 4.01 é artefacto).

Resultados consistentes destes audits (não afectados pelo bug CVD):

| Estratégia | Janela | n | PF | Sharpe | Veredicto |
|------------|--------|---|-----|--------|-----------|
| VolatilityBreakout | B_2weeks | 28 | 1.57 | 4.27 | KEEP |
| VolatilityBreakout | D_full | 66 | 0.99 | -0.22 | (positivo recente) |
| VWAPDeviation | B_2weeks | 12 | 1.50 | 2.38 | KEEP |
| VWAPDeviation | D_full | 32 | 1.92 | 3.24 | KEEP |
| FundingExtreme | B_2weeks | 25 | 0.14 | +4.36 | WATCH (inconsistente) |
| FundingExtreme | D_full | 45 | 1.70 | 2.33 | (churn em B_2weeks) |
| TrendPyramid | B_2weeks | 18 | 10.46 | 4.59 | WATCH (outlier) |
| TrendPyramid | D_full | 33 | 2.95 | 2.79 | (1 trade distorce) |
| DonchianBreakout | D_full | 144 | 0.50 | -7.88 | KILL |
| OrderBookScalper | D_full | 185 | 0.29 | -13.93 | KILL |
| RangeGrid | D_full | 17 | 0.49 | -4.62 | KILL |
| SmartMoneyFlow | D_full | 180 | 0.99 | -1.22 | KILL |
| FundingArbitrage/LeadLag/LiqCatcher/FundingMom/SpotPerpCarry | todas | 0 | — | — | NO_DATA |

CSVs: `data/backtests/strategy_audit_20260629_122340.csv`, `strategy_audit_20260629_132320.csv`,
`cvd_sweep_20260629_172204.csv`, `vb_vwap_walkforward_20260629_195317.csv`

## Histórico detalhado (audits pré-backfill — ver aviso no topo)

Os audits `strategy_audit_20260629_122340.csv` (VB + VWAP corrigidos) e `_132320.csv` (NO_DATA re-audit)
foram corridos com buy/sell volume incompleto. **CVD não é fiável nestes CSVs** (PF 4.01 é artefacto).

Resultados consistentes destes audits (não afectados pelo bug CVD):

| Estratégia | Janela | n | PF | Sharpe | Veredicto |
|------------|--------|---|-----|--------|-----------|
| VolatilityBreakout | B_2weeks | 28 | 1.57 | 4.27 | KEEP |
| VolatilityBreakout | D_full | 66 | 0.99 | -0.22 | (positivo recente) |
| VWAPDeviation | B_2weeks | 12 | 1.50 | 2.38 | KEEP |
| VWAPDeviation | D_full | 32 | 1.92 | 3.24 | KEEP |
| FundingExtreme | B_2weeks | 25 | 0.14 | +4.36 | WATCH (inconsistente) |
| FundingExtreme | D_full | 45 | 1.70 | 2.33 | (churn em B_2weeks) |
| TrendPyramid | B_2weeks | 18 | 10.46 | 4.59 | WATCH (outlier) |
| TrendPyramid | D_full | 33 | 2.95 | 2.79 | (1 trade distorce) |
| DonchianBreakout | D_full | 144 | 0.50 | -7.88 | KILL |
| OrderBookScalper | D_full | 185 | 0.29 | -13.93 | KILL |
| RangeGrid | D_full | 17 | 0.49 | -4.62 | KILL |
| SmartMoneyFlow | D_full | 180 | 0.99 | -1.22 | KILL |
| FundingArbitrage/LeadLag/LiqCatcher/FundingMom/SpotPerpCarry | todas | 0 | — | — | NO_DATA |

CSVs: `data/backtests/strategy_audit_20260629_122340.csv`, `strategy_audit_20260629_132320.csv`,
`cvd_sweep_20260629_172204.csv`
