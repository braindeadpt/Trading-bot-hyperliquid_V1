# BASELINE v3.1.47 — Fase 00 (read-only)

**Gerado:** 2026-07-10 (UTC+1)  
**Commit baseline:** `42f377de474d605effea52b81273d13ba9ea0dce` (`42f377d`)  
**Mensagem:** `v3.1.47: forensic chop fixes from live trade analysis`  
**Branch:** `main` (sincronizado com `origin/main`)  
**Modo default:** `paper` (`config/settings.yaml` → `mode: "paper"`)

Este documento congela o estado auditável **antes** de qualquer correção nas fases seguintes. Nenhum ficheiro em `src/` ou `config/` foi alterado nesta fase.

---

## 1. Git e working tree

| Item | Valor |
|------|-------|
| Commit HEAD | `42f377d` (2026-07-09 10:57:44 +0100) |
| Branch | `main` |
| Staged changes | nenhum |
| Ficheiros untracked | `data/backtests/*` (outputs locais), `data/live/` (runtime), `scripts/_backtest_checklist_chop.py`, `scripts/_debug_pnl.py`, `scripts/_baseline_phase00_analyze.py` |

**Milestones de sizing/paridade relevantes (histórico git):**

| Commit | Data | Descrição |
|--------|------|-----------|
| `53dc0f9` | 2026-06-23 | v3.1.19 — backtest passa a usar `RiskManager` real + paridade live |
| `d45cea6` | 2026-07-02 | v3.1.43 — risk-based sizing ($2k–$5k notionals) |
| `4f47657` | 2026-07-07 | v3.1.46 — ChecklistMeta chop gates + cooldown funding |
| `42f377d` | 2026-07-09 | v3.1.47 — forensic: chase filter, SOL 0.5×, daily stop streak |

---

## 2. Config efetiva por modo

Extraído com `load_config` + `_apply_mode_overrides` (sem env `BOT_*`).

### Paper (default)

| Parâmetro | Valor |
|-----------|-------|
| `close_positions_on_shutdown` | `false` |
| `flatten_on_stop` | `false` |
| `leverage_max` | 10.0 |
| `max_daily_loss_pct` | 3.0 |
| `max_daily_trades` | 0 (ilimitado) |
| `max_position_size_pct` | 5.0 |
| `max_positions` | 3 |
| `per_trade_risk_pct` | 1.0 |
| `max_daily_stop_losses` | 4 |
| `symbol_risk_multiplier.SOL` | 0.5 |
| `chase_filter.enabled` | `true` |
| `exchange.mainnet_enabled` | `false` (ausente no YAML → default) |

**Estratégias enabled (paper):** `volatility_breakout`, `vwap_deviation`, `checklist_meta`, `kelly`, `strategy_governance`. Ensemble **desligado** (`ensemble.enabled: false`).

### Testnet

Igual ao paper, exceto:

| Parâmetro | Valor |
|-----------|-------|
| `max_daily_trades` | 50 |
| `lead_lag.enabled` | `true` |

### Mainnet

| Parâmetro | Valor |
|-----------|-------|
| `close_positions_on_shutdown` | `true` |
| `flatten_on_stop` | `true` |
| `leverage_max` | 5.0 |
| `max_daily_loss_pct` | 2.0 |
| `max_daily_trades` | 20 |
| `max_position_size_pct` | 3.0 |
| `lead_lag.enabled` | `false` |
| `spot_perp_carry.enabled` | `true` |
| `range_grid.enabled` | `true` |
| `orderbook_scalper.enabled` | `false` |

**Mainnet continua bloqueado** mesmo com `--mode mainnet`: requer `HYPERLIQUID_MAINNET_ENABLED=1` **e** `exchange.mainnet_enabled: true` (`src/core/execution.py`). Nenhum dos dois está ativo na baseline.

---

## 3. Análise read-only `data/live/bot.db`

**Fonte:** SQLite read-only (`file:...?mode=ro`). Bot **não** foi executado nesta fase.

### 3.1 Resumo global (268 trades fechados, 0 abertos)

| Métrica | Valor |
|---------|-------|
| Período (UTC) | 2026-05-25 → 2026-07-09 |
| Total PnL realizado (`pnl_usd`) | **−$1,222.67** |
| Funding pago (`funding_paid`) | **−$465.75** |
| Profit factor | **0.44** |
| Win rate | **31.0%** (83W / 184L / 1 BE) |
| Expectancy | **−$4.56 / trade** |
| Avg win | **+$11.71** |
| Avg loss | **−$11.93** |
| Max DD (sequência cumulativa de `pnl_usd`) | **$1,272.97** |
| Avg notional (size × entry_price) | ~$2,030 |
| Snapshot DD (`peak_capital` vs `capital`) | 7.12% |

### 3.2 Por estratégia

| Estratégia | Trades | PnL USD | WR% | PF | Avg win | Avg loss |
|------------|--------|---------|-----|-----|---------|----------|
| ChecklistMeta | 126 | −883.24 | 38.9 | 0.46 | 15.50 | −21.62 |
| StrategyEnsemble | 127 | −233.29 | 21.3 | 0.47 | 7.69 | −4.41 |
| VWAPDeviation | 7 | +3.10 | 85.7 | 4.27 | 0.67 | −0.95 |
| VolatilityBreakout | 8 | −109.24 | 12.5 | 0.01 | 0.82 | −15.72 |

> **Nota:** `StrategyEnsemble` com 127 trades históricos — ensemble está **off** na config actual; estes trades são legado.

### 3.3 Por símbolo

| Símbolo | Trades | PnL USD | WR% | PF |
|---------|--------|---------|-----|-----|
| BTC | 49 | −42.17 | 36.7 | 0.77 |
| ETH | 49 | −446.16 | 36.7 | 0.18 |
| HYPE | 50 | −309.37 | 42.0 | 0.57 |
| SOL | 120 | −424.97 | 21.7 | 0.44 |

### 3.4 Por exit reason (top impacto negativo)

| Exit reason | Trades | PnL USD |
|-------------|--------|---------|
| `stop_loss` | 90 | −1,641.70 |
| `funding_reverted` | 50 | −137.51 |
| `failed_breakout_above_mid` | 5 | −59.57 |
| `failed_breakout_below_mid` | 6 | −61.10 |
| `checklist_tp_hit` | 26 | +572.74 |
| `take_profit` / `take_profit_hit` | 14 | +172.47 |

### 3.5 Por dia (últimos 7 dias UTC)

| Dia | Trades | PnL USD | WR% |
|-----|--------|---------|-----|
| 2026-07-03 | 9 | +151.41 | 77.8 |
| 2026-07-04 | 10 | −160.71 | 30.0 |
| 2026-07-05 | 6 | −38.45 | 33.3 |
| 2026-07-06 | 13 | −313.26 | 23.1 |
| 2026-07-07 | 17 | −321.34 | 29.4 |
| 2026-07-08 | 16 | −40.25 | 37.5 |
| 2026-07-09 | 11 | −259.98 | 9.1 |

### 3.6 Segmentação por era de fixes (exit_time UTC)

| Era | Trades | PnL USD | WR% | PF | Avg win | Avg loss |
|-----|--------|---------|-----|-----|---------|----------|
| `pre_v3.1.43` (antes 2026-07-02) | 179 | −235.31 | 28.5 | 0.49 | 4.49 | −3.63 |
| `v3.1.43_to_v3.1.46` (2026-07-02..08) | 78 | −727.38 | 39.7 | 0.51 | 23.96 | −31.96 |
| `v3.1.47_plus` (≥ 2026-07-09) | 11 | −259.98 | 9.1 | 0.00 | 0.12 | −26.01 |

> Amostra pós-v3.1.47: **11 trades num único dia** — insuficiente para conclusões de performance.

### 3.7 Divergência confirmada: snapshot vs trades

| Fonte | Valor |
|-------|-------|
| Soma `pnl_usd` (closed) | −$1,222.67 |
| `portfolio_snapshots` primeiro → último `capital` | $10,000.00 → **$20,600.92** (+$10,600.92) |
| Último snapshot `cash` (meta) | $20,600.92 |
| Salto confirmado 2026-07-02 ~04:39 UTC | +$5,694 num único salto |

**Defeito confirmado:** métricas de `portfolio_snapshots.capital` **não são fiáveis** para PnL histórico nesta baseline. Usar sempre agregados da tabela `trades` para performance realizada.

**Hipótese (não confirmada como root cause):** salto de 2026-07-02 correlaciona temporalmente com deploy de v3.1.43 (risk-based sizing); investigação nas fases seguintes.

---

## 4. Artefactos de backtest — comparabilidade

### 4.1 Critérios de exclusão

Um artefacto é **não comparável** com o motor actual se foi gerado antes de:

1. **v3.1.19** (2026-06-23) — paridade backtest/live (`RiskManager`, vol CB, funding blackout)
2. **v3.1.43** (2026-07-02) — risk-based sizing ($2k–$5k)
3. **v3.1.47** (2026-07-09) — chase filter, SOL 0.5×, `max_daily_stop_losses`

### 4.2 Inventário

| Ficheiro | Data no nome | Pre-19 | Pre-43 | Pre-47 | Veredicto |
|----------|--------------|--------|--------|--------|-----------|
| `per_strategy_20260625_202340.csv` | 2026-06-25 | ✓ | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `ensemble_sweep_20260625_234431.csv` | 2026-06-25 | ✓ | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `exit_economics_20260625_231103.csv` | 2026-06-25 | ✓ | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `strategy_audit_20260629_*.csv` | 2026-06-29 | — | ✓ | ✓ | **NÃO COMPARÁVEL** (sizing + forensic) |
| `cvd_sweep_20260629_172204.csv` | 2026-06-29 | — | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `vb_vwap_walkforward_20260629_*` | 2026-06-29 | — | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `combined_focused_20260630_*` (tracked) | 2026-06-30 | — | ✓ | ✓ | **NÃO COMPARÁVEL** |
| `exit_optimisation_20260630_*` (tracked) | 2026-06-30 | — | ✓ | ✓ | **NÃO COMPARÁVEL** |

**Não existe** na baseline nenhum backtest gerado **após** `42f377d` (v3.1.47). Qualquer validação quantitativa pós-fix terá de ser re-executada nas fases seguintes.

### 4.3 Defeito de paridade confirmado (código, não corrigido nesta fase)

`src/core/execution.py` linha ~335: clamp CRIT-003 usa **`max_position_size_pct = 0.20` hardcoded**, enquanto `risk.max_position_size_pct` efectivo é **5.0%** (paper) / **3.0%** (mainnet). Backtests que passam só pelo `RiskManager` podem divergir do path paper/live na execução.

---

## 5. Auditorias executadas (baseline)

| Comando | Resultado | Exit |
|---------|-----------|------|
| `python main.py --audit` | 0 CRITICAL, 1 HIGH (`AUDIT-005` pré-existente em `crash_recovery.py`), 1 LOW | **0** |
| `python audit_all.py` | Todos os componentes OK | **0** |
| `python scripts/lookahead_audit.py --ci` | **FAIL** — 10 findings (2× HIGH `LOOKAHEAD-002` em `indicators.py:767`, 7× HIGH `LOOKAHEAD-003` em `portfolio.py` funding settle) | **1** |
| `python scripts/run_ci_tests.py` | 13 suites + `test_basic` — **todos passaram** | **0** |

---

## 6. Blockers de mainnet (baseline)

| # | Blocker | Tipo | Evidência |
|---|---------|------|-----------|
| M1 | Dupla confirmação mainnet ausente | **Confirmado** | `exchange.mainnet_enabled` não definido; `HYPERLIQUID_MAINNET_ENABLED` não set |
| M2 | SL/TP software-managed (sem trigger orders SDK) | **Confirmado** | `mode_overrides.mainnet.close_positions_on_shutdown: true`, `flatten_on_stop: true`; comentários em `settings.yaml` |
| M3 | Performance paper negativa | **Confirmado** | PF 0.44, expectancy −$4.56, −$1,222 PnL em 268 trades |
| M4 | Paridade execução sizing | **Confirmado** | CRIT-003 hardcoded 20% vs config 5% |
| M5 | Lookahead audit CI falha | **Confirmado** | `scripts/lookahead_audit.py --ci` exit 1 |
| M6 | Contabilidade snapshot vs trades | **Confirmado** | +$10.6k snapshot delta vs −$1.2k trade PnL |
| M7 | Amostra pós-v3.1.47 insuficiente | **Confirmado** | 11 trades / 1 dia |

---

## 7. Limitações desta baseline

1. **Read-only:** `bot.db` não foi modificado; bot não foi arrancado.
2. **Sem credenciais:** vault, `.env` e API keys não foram inspecionados (por design).
3. **Métrica canónica de PnL:** tabela `trades.pnl_usd` — não `portfolio_snapshots.capital`.
4. **Funding:** `funding_paid` total −$465.75 incluído na DB mas excluído da soma `pnl_usd` acima (campo separado).
5. **Backtests:** todos os artefactos existentes são pré-v3.1.47; nenhum serve como ground truth pós-fix.
6. **AGENTS.md** reporta v3.1.25 no header mas código/commits estão em v3.1.47 — documentação desactualizada (não corrigida nesta fase).

---

## 8. Reproducibilidade

```bash
# Commit
git log -1 --format="%H %ci %s"
git status

# Análise DB (read-only)
python scripts/_baseline_phase00_analyze.py

# Config efectiva por modo
python -c "
from src.utils.config import load_config, _apply_mode_overrides, _coerce_types
from copy import deepcopy
import json
for mode in ('paper','testnet','mainnet'):
    cfg = load_config('config/settings.yaml')
    d = deepcopy(cfg._data); d['mode']=mode
    _apply_mode_overrides(d); _coerce_types(d)
    r = d.get('risk',{})
    print(mode, json.dumps({'leverage_max':r.get('leverage_max'),'max_position_size_pct':r.get('max_position_size_pct'),'max_daily_trades':r.get('max_daily_trades')}))
"

# Auditorias
python main.py --audit
python audit_all.py
python scripts/lookahead_audit.py --ci
python scripts/run_ci_tests.py
```

---

## 9. Checklist de aceitação global (fases seguintes)

Cada fase só é considerada **aceite** quando **todos** os itens aplicáveis passam.

### A. Integridade e segurança

- [ ] **A1** — `python main.py --audit` → 0 CRITICAL (HIGH pré-existente `AUDIT-005` documentado se mantido)
- [ ] **A2** — `python audit_all.py` → exit 0
- [ ] **A3** — `python scripts/lookahead_audit.py --ci` → exit 0 (actualmente **falha**)
- [ ] **A4** — `python scripts/run_ci_tests.py` → exit 0
- [ ] **A5** — Nenhum segredo em diff/commits

### B. Paridade paper / backtest / risco

- [ ] **B1** — `RiskManager.calculate_position_size` e `ExecutionEngine` usam o **mesmo** `max_position_size_pct` da config (sem hardcode 20%)
- [ ] **B2** — Backtest re-executado pós-fix com `use_regime_weights` / gates alinhados ao live
- [ ] **B3** — `portfolio_snapshots.capital` reconcilia com `SUM(trades.pnl_usd)` ± fees/funding dentro de tolerância definida
- [ ] **B4** — Paper permanece default; mainnet requer dupla confirmação explícita

### C. Performance e dados

- [ ] **C1** — Novos backtests datados **após** commit da fase, armazenados em `data/backtests/`
- [ ] **C2** — Artefactos pré-v3.1.47 marcados como legado (não usados para go/no-go)
- [ ] **C3** — Métricas reportadas com N trades, período e era de fix
- [ ] **C4** — Mínimo 30 trades fechados pós-fix antes de decisão de estratégia

### D. Mainnet readiness (fase final apenas)

- [ ] **D1** — Expectancy ≥ 0 e PF ≥ 1.0 em janela rolling 30d paper **pós-fixes**
- [ ] **D2** — SL/TP nativos via SDK **ou** política de flatten documentada e testada
- [ ] **D3** — `HYPERLIQUID_MAINNET_ENABLED` + `exchange.mainnet_enabled` testados em dry-run
- [ ] **D4** — `mode_overrides.mainnet` validado (leverage 5×, max_pos 3%, daily cap 20)
- [ ] **D5** — Estratégias HF desligadas em mainnet (`orderbook_scalper`, `lead_lag`)

### E. Documentação

- [ ] **E1** — Cada fase actualiza secção relevante (ou addendum a este baseline)
- [ ] **E2** — Defeitos confirmados vs hipóteses claramente separados
- [ ] **E3** — Comandos e exit codes exactos registados

---

## 10. Decisões tomadas na Fase 00

| Decisão | Racional |
|---------|----------|
| Métrica canónica = `trades.pnl_usd` | Snapshot capital inflado (+$10.6k vs −$1.2k trades) |
| Todos os backtests existentes = não comparáveis | Gerados antes de v3.1.47 (e maioria antes de v3.1.43) |
| Mainnet permanece bloqueado | M1–M7 activos |
| Nenhuma alteração em `src/` / `config/` | Escopo read-only da fase |
| Script `_baseline_phase00_analyze.py` criado | Reproducibilidade auditável |

---

*Fim do baseline. Próxima fase: correcções conforme roadmap, começando por paridade sizing (B1) e reconciliação de capital (B3).*
