# Paper / OOS 90-day evidence protocol

Frozen: 2026-08-10 (tier-0 fee alignment cycle)

## Purpose

Run a **forward-only** paper evidence cycle after closing candle/MM/OI/tape/XS
momentum families. Success is a reproducible PASS **or** a clean close — not
“find a strategy that looks good”.

## Scope (locked)

| Item | Value |
|------|-------|
| Mode | paper only (`phase08.paper_only: true`) |
| Execution | `VWAPDeviation` only (control / sample accumulation) |
| Shadow | existing Phase08 shadow list — no new strategies |
| Fees | HL perps **tier-0**: taker **0.045%**/side, maker **0.015%**/side |
| Mainnet | blocked |
| GoldRush OOS | not allowed until readiness validated |

## Economic correction

Prior config understated maker (0.01) and used taker 0.035 (Tier-2+). Aligning
fees **invalidates** the prior Phase10 window counter. Re-register with:

```bash
python scripts/ops/reregister_phase10_tier0_fees.py
```

Then **coordinated restart** of the paper bot (`stop.bat` / `start.bat` or
recovery wrapper). Do not restart mid-edit from scripts.

## Gates (frozen a priori)

### A. Paper execution control (VWAPDeviation)

Evaluated on **real paper fills** in `data/live/bot.db` since
`window_start_ms` (Phase10 manifest):

- calendar days ≥ **90**
- closed trades ≥ **30**
- net PF > 1, expectancy_R > 0
- max drawdown ≤ Phase10 frozen max
- costs use tier-0 model (already in execution)

Even on PASS: **remain paper** — mainnet promotion is out of scope.

### B. Shadow strategies

Via `scripts/research/evaluate_shadow_outcomes.py` / shadow panel (gross + **net**):

- `n_evaluated` ≥ 30 in 90d, else `INCONCLUSIVE (frequency insufficient)`
- net PF > 1 and net expectancy_R > 0
- mean funding coverage ≥ 0.90 else net gate = `INCONCLUSIVE` (never PASS)
- B1 / random-direction ≥ p95 with ≥200 seeds when powered
- no promotion without baseline-signal gate PASS (AGENTS.md §12)

### C. Full-depth L2 research (only new investigation)

Recorder: `market_data.l2_recording` at **1s × 25 levels**.

1. Daily audit: `python scripts/research/l2_recording_audit.py`
2. After **≥30 valid days**: `python scripts/research/feature_screening_l2_depth.py`
3. FDR + date-cluster bootstrap + tier-0 cost / AS. Objective: execution /
   fill-AS information — **not** build MM or directional strategy without
   economic survivor.

## Weekly ops

```bash
python scripts/research/paper_oos_weekly_report.py
python scripts/research/evaluate_shadow_outcomes.py --since-days 14 --persist
python scripts/ops/phase10_check_gate.py --no-register
python scripts/research/l2_recording_audit.py
```

Do **not** decide on mid-window snapshots. Formal verdict only at day 90
(or when Phase10 window criteria are met, whichever is later for VWAP).

## Forbidden

- Adding strategies to `execution_strategies` without baseline PASS
- Parameter fishing / lookback grids on closed families
- Treating shadow gross scoreboards as edge
- Restarting GoldRush-backed OOS
- Mainnet enablement

## Artifacts

- Manifest: `data/research/paper_oos_90d/manifest.json` (written at re-register)
- Weekly reports: `data/research/paper_oos_90d/weekly/`
- Protocol (this file): `docs/PAPER_OOS_90D_PROTOCOL.md`

## Code-change log (mid-window, hash-neutral)

The config hash cannot see code changes, so every code change touching the
live process during the frozen window is logged here.

| Date | Commit | Change | Why hash-neutral / isolation |
|------|--------|--------|------------------------------|
| 2026-10-08 | `a70b3cb` | `TradingEngine._route_shadow_signals` — the shadow signal pool now passes through `route_phase08_signals` (own `SequentialContradictionGuard`, never shared with the execution guard) and emits `router_blocked` / `iv_gate_shadow` rows exactly like the routed path. Needed because `iv_gate_shadow` was previously emitted only on the best routed *execution* signal — pruned strategies could never accumulate it, which would silently kill the VWAP iv_gate preregistration. | Observability-only: writes shadow_decisions rows only. Isolation asserted by `test_g_shadow_routing_mirror_never_touches_execution` — identical events produce zero `_process_entry_signal`/`_persist_decision` calls and leave the execution seq-guard state untouched. |
| 2026-10-08 | `a70b3cb` | `shadow_outcome_evaluator` — `independent_outcomes` (one open position per symbol), `n_independent`/`n_overlapped` on boards, metrics + gates read the independent set, pre-simulation overlap dedup, preregistered-confirmation seal (`<variant>#confirm` boards expose counts only until target). | Read path only — computes and persists scoreboards; never feeds execution. `shadow_strategies` membership verified hash-neutral (4077927fec6a880c → 4077927fec6a880c). |
| 2026-10-09 | `ca37d22` + `de1901e` + `2e26023` | Deployed `d12af34→2e26023` in one pm2 restart (2026-10-09 09:28:09 UTC): (a) removed refuted strategies OBS/TopTraderFlow/ChecklistMeta (class files gone; evaluator config sections kept for historical recompute); (b) dashboard v2 — main page answers "closer to net-positive PnL?", ops moved to `/ops`, unified decision_feed, persisted-board shadow panel with bootstrap PF CI, exec↔sim divergence (25 bps), hypothesis counters public under seal; (c) auxiliary pm2 crons `jev-judge`/`outcome-eval`/`watchdogs` found dead since 07-10/29-09 and re-armed. | Runtime-neutral: `config/settings.yaml` untouched, `config_hash` verified 4077927fec6a880c before restart; evaluator regression old-vs-new identical on all 25 persisted boards; dashboard reads research DB read-only and never runs in-process evaluation. Isolation suite green (`test_g_shadow_routing_mirror_never_touches_execution`, `test_h_shadow_mirror_no_fallback_promotion_vwap`). |
