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
python scripts/reregister_phase10_tier0_fees.py
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

Via `scripts/evaluate_shadow_outcomes.py` / shadow panel (gross + **net**):

- `n_evaluated` ≥ 30 in 90d, else `INCONCLUSIVE (frequency insufficient)`
- net PF > 1 and net expectancy_R > 0
- mean funding coverage ≥ 0.90 else net gate = `INCONCLUSIVE` (never PASS)
- B1 / random-direction ≥ p95 with ≥200 seeds when powered
- no promotion without baseline-signal gate PASS (AGENTS.md §12)

### C. Full-depth L2 research (only new investigation)

Recorder: `market_data.l2_recording` at **1s × 25 levels**.

1. Daily audit: `python scripts/l2_recording_audit.py`
2. After **≥30 valid days**: `python scripts/feature_screening_l2_depth.py`
3. FDR + date-cluster bootstrap + tier-0 cost / AS. Objective: execution /
   fill-AS information — **not** build MM or directional strategy without
   economic survivor.

## Weekly ops

```bash
python scripts/paper_oos_weekly_report.py
python scripts/evaluate_shadow_outcomes.py --since-days 14 --persist
python scripts/phase10_check_gate.py --no-register
python scripts/l2_recording_audit.py
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
