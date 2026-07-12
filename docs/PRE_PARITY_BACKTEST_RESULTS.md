# Pre-Parity Backtest Results Inventory (Fase 05)

**Effective sizing / gate parity begins at:** `backtest.sizing_version: phase05-risk-at-equity-v1`  
**Phase 05 completion commit:** see `git log` after Fase 05 gate closure (not retroactive).

The run manifest (`manifest` key on `BacktestEngine.run()` results) labels **new** runs only.  
It does **not** retro-tag files written before Phase 05. This document is the authoritative inventory of **invalid / non-comparable** historical outputs.

---

## Why older results are not comparable

| Defect (pre-Fase 05) | Impact |
|----------------------|--------|
| `notional = capital × size_pct` | Ignored stop-distance risk sizing; inflated/deflated notionals vs live |
| Dual Kelly (`backtest.use_kelly` vs `strategy.kelly.enabled`) | Backtest could size differently from live with same YAML |
| Missing correlation gate in replay | Portfolio heat / correlation rejections not simulated |
| Missing replay data-quality gate | Entries allowed on gappy/stale DB windows live would block |
| TCA without L2 always used proxy slippage silently | Tier conflated with production-grade OHLC runs |
| Stop/TP on close only (no 1m high/low) | Optimistic fills vs intrabar pessimistic live modelling |
| No run manifest | Cannot verify commit, config hash, or sizing version |

---

## Untracked local outputs (pre-parity by definition)

All paths below were generated **before** Phase 05 gate parity and lack `sizing_version` / `config_hash` manifests.

### `data/backtests/` (CSV / TXT sweeps)

| File pattern | Notes |
|--------------|-------|
| `cvd_sweep_*.csv`, `cvd_sweep_output.txt` | Pre-parity sizing; proxy microstructure |
| `ensemble_sweep_*.csv`, `ensemble_sweep_output.txt` | Dual Kelly era |
| `exit_economics_*.csv`, `exit_optimisation_output.txt` | Pre-intrabar stop/TP |
| `per_strategy_*.csv` | Per-strategy audit, no gate parity |
| `strategy_audit_*.csv` | Strategy audit sweep |
| `vb_vwap_walkforward_*.csv/json` | Walk-forward without correlation/data-quality gates |
| `sfp_sweep_*.txt`, `va_sweep_output.txt` | Parameter sweeps |
| `audit_nodata_output.txt`, `audit_run_output.txt`, `run_output.txt` | Ad-hoc run logs |
| `exit_baseline.txt` | Exit economics baseline |

### Scripts producing pre-parity artifacts

| Script | Status |
|--------|--------|
| `scripts/backtest_*_walkforward.py` | Outputs invalid until re-run with Phase 05 engine + manifest |
| `scripts/backtest_ensemble_sweep.py` | Invalid (dual Kelly, no manifest) |
| `scripts/backtest_cvd_sweep.py` | Invalid |
| `scripts/compare_exit_economics_backtest.py` | Invalid |
| `scripts/_backtest_forensic_sol_eth.py` | Explicitly marked "understates live fidelity" |
| `scripts/walk_forward.py` | Invalid until updated to `build_backtest_config_from_yaml` + manifest |

---

## How to mark a result valid post-Fase 05

A comparable backtest run **must** include in its result dict:

```json
{
  "manifest": {
    "sizing_version": "phase05-risk-at-equity-v1",
    "config_hash": "<16-char sha256>",
    "fidelity_tier": "tier_a_ohlc_funding | tier_b_proxy_microstructure | tier_b_tca_proxy | ...",
    "gate_parity_version": "phase05-gates-v1",
    "pre_parity_results_invalid": false
  }
}
```

Runs with `sizing_version` missing or different, or `pre_parity_results_invalid: true`, must not be used for promotion decisions.

---

## Pre-OOS consolidation inventory (Fase consolidacao)

**Date:** 2026-07-10  
**Scope:** Gate parity closure, statistics hardening, Phase08 preregister — **no OOS / holdout runs**.

### Parity closures

| Item | Status | Location |
|------|--------|----------|
| Correlation + exposure gate shared live/backtest | Done | `SignalPipeline` + `RiskManager` |
| TCA strict rejects missing L2; proxy Tier B only | Done | `execution.tca_mode` / `backtest.tca_mode` |
| Replay data quality / freshness gate | Done | `ReplayDataQualityGate` |
| Entry debounce documented + reproduced | Done | `docs/ENTRY_DEBOUNCE.md`, parity tests |
| Gate manifest documents shared vs live-only | Done | `SignalPipeline.gate_manifest()` |

### Statistics closures

| Item | Status | Location |
|------|--------|----------|
| Block bootstrap UTC contiguous days (no weekday fallback) | Done | `monte_carlo.group_trades_into_blocks` |
| HoldoutGuard persistent across processes | Done | `holdout_ledger.json` |
| Sharpe crypto calendar / Calmar CAGR / expectancy_R | Done | `metrics.py`, phase06 tests |
| DSR/PBO labeled internal proxies | Done | `statistical_validation.py` |

### Phase08 closures

| Item | Status | Location |
|------|--------|----------|
| Immutable preregister + experiment_id + hash | Done | `phase08_preregister.json` |
| ADX 15m closed candles only | Done | engine `_make_candle_callback` |
| Sequential contradictory signal block | Done | `SequentialContradictionGuard` |
| Raw trade recorder async queue + backpressure | Done | `ResearchMicrostructureRecorder` |

### Pre-parity artifacts (unchanged — still invalid)

All files listed in the sections above remain **non-comparable** until re-run with
`gate_parity_version: phase05-gates-v1` manifest. No holdout or walk-forward OOS
was executed during consolidation.

---

## Live paper DB metrics (baseline)

Live metrics in `data/live/bot.db` remain valid **operational** records but are not automatically comparable to backtests produced before Phase 05. See `docs/BASELINE_V3_1_47.md` for the frozen live baseline.
