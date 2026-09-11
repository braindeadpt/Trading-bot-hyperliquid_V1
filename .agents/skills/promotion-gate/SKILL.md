---
name: promotion-gate
description: Checklist for any strategy promotion/demotion/registry change — baseline-signal gate, precedents, docs to update. Run before touching execution_strategies.
triggers:
  - user
  - model
allowed-tools:
  - read
  - grep
  - glob
  - exec
---

# Strategy promotion / demotion gate

Canonical references: `AGENTS.md` §12 (baseline-signal gate), `docs/RESEARCH_BACKLOG.md`,
`docs/STRATEGY_AUDIT.md`, `src/strategies/factory.py` (`_STRATEGY_REGISTRY`).

## Non-negotiable rule

No strategy enters `strategy.phase08.execution_strategies` (or re-enters
`_STRATEGY_REGISTRY` after retirement) without a **baseline_signal_gate: PASS**
on the preregister manifest. INCONCLUSIVE grandfathering applies only to
strategies already in execution (precedent: VWAPDeviation, n<30 while the
sample accumulates) — it is never a promotion path.

## Checklist for a promotion proposal

1. **Evidence** — baseline-signal-gate PASS, or frozen-window OOS gate PASS
   (`scripts/ops/phase10_check_gate.py`). Post-hoc sweeps are hypotheses, not
   evidence. KEEP verdicts from the overnight runner are *shadow candidates*,
   not promotions.
2. **Path** — execution promotion only after a shadow period + watchdog
   recheck. Registry changes are code-only (config_hash is frozen — YAML
   `execution_strategies` changes require re-registration + documented
   justification via `scripts/ops/reregister_phase10_*.py`).
3. **Demotion precedent** — FAIL → demote to shadow (ChecklistMeta precedent:
   `scripts/ops/demote_checklist_meta_for_baseline_fail.py`). Retired
   strategies stay importable for research harnesses but are never
   instantiated by the factory.
4. **Docs to update** — `AGENTS.md` §1 (live registry list), `docs/STRATEGY_AUDIT.md`,
   `docs/RESEARCH_BACKLOG.md` verdict line, `docs/MAINNET_READINESS.md` if the
   gate table changes.

## Never do

- Re-register the frozen window to make a config change "fit".
- Promote on a single-window or single-seed result.
- Re-register a retired strategy without a fresh baseline-signal-gate PASS.
