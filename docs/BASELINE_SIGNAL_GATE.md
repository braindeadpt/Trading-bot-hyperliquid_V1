# Baseline-signal gate

Permanent research → execution portão. Companion to `AGENTS.md` §12.

## Criterion

```
PASS  ⇔  B1 ≥ p95   AND   n_trades ≥ 30   AND   expectancy > 0 (PF > 1)
```

- **B1** (primary): real timing, random direction — isolates directional information.
- **B2/B3**: complementary (timing / both).
- **INCONCLUSIVE** if `n_trades < 30`: not tested — never kill on this alone.
- **INCONCLUSIVE (frequency insufficient):** cannot reach n≥30 in a quarter →
  not validatable / not usable (not a FAIL).
- Report **which** condition failed on every FAIL.

### First demotion precedent (2026-08-09)

ChecklistMeta powered FAIL on W2+W3 → demoted from `execution_strategies` to
shadow. VWAPDeviation retained (underpowered INCONCLUSIVE grandfather only).
See `scripts/demote_checklist_meta_for_baseline_fail.py`.

### Cautionary example

SmartMoneyFlow W3: B1 percentile **96**, PF **~0.27** → would pass a
percentile-only gate while losing money. Three-condition gate → **FAIL
(`not_profitable`)**.

## How to run

```bash
python scripts/baseline_signal_gate.py --strategy NAME --folds W2,W3 --seeds 200 --gate
```

Exit codes: `0=PASS`, `1=FAIL`, `2=INCONCLUSIVE`.

Portfolio board: `data/backtests/parity_diag/BASELINE_PORTFOLIO_GATE_REPORT.md`.

## Preregister

- **Hard at entry:** cannot add a non-legacy strategy to
  `strategy.phase08.execution_strategies` without `baseline_signal_gate: PASS`.
- **Soft for legacy paper set:** `ChecklistMeta`, `VWAPDeviation` may boot
  without the field (`LEGACY_EXECUTION_WITHOUT_BASELINE_GATE`).
- See `assert_can_promote_to_execution` /
  `assert_baseline_signal_gate` in `src/research/phase08_preregister.py`.

## Not validatable (0 trades in current candle replay)

CVDOrderFlow, SpotPerpCarry, FundingMomentum, OrderBookScalper,
FundingArbitrage, LeadLag, LiquidationCatcher — not promotable until a powered
run exists. Tier-B feed gaps that only hurt the strategy make the test
**conservative** vs the strategy when a powered run becomes possible.
