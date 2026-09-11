---
name: research-experiment
description: Run or propose a strategy research experiment under research_program.md discipline — hard invariants, window discipline, verdict vocabulary, ledger format.
triggers:
  - user
  - model
permissions:
  deny:
    - Write(config/settings.yaml)
    - Write(src/**)
    - Write(main.py)
    - Write(data/live/**)
    - Write(.env)
---

# Research experiment discipline

Canonical references: `research_program.md` (the invariant + verdict schema),
`data/research/overnight_experiments/QUEUE.md` (what is READY/BLOCKED),
`docs/OVERNIGHT_RESEARCH_LOG.md` (append-only ledger, morning report on top).

## Hard invariants — violation means the experiment is BLOCKED, not done

- NEVER edit `config/settings.yaml`, `src/`, `main.py`, `.env`, or the live DB. Variants are CLI flags / config-dict overrides on harness scripts only.
- NEVER re-register the Fase-10 frozen window, disable `assert_config_matches_preregister`, or bypass `tests/test_config_hash_frozen.py`.
- NEVER edit strategy code to make a variant look better. If a variant needs code changes → log `NEEDS-CODE` in `docs/RESEARCH_BACKLOG.md` for a human.
- Check config-hash implications (`python -c "from src.utils.config import compute_config_hash; ..."`) before proposing any override that persists.

## Experiment unit (micro-preregistration — fixed BEFORE running)

1. **Hypothesis** — one falsifiable sentence.
2. **Family + harness** — which `scripts/research/` runner executes it (see `QUEUE.md` for READY families; `scripts/research/overnight_runner.py --family ...` is the gated runner).
3. **Window set** — non-overlapping windows chosen ONCE per family, never per result. Post-hoc window selection is forbidden.
4. **Metrics** — primary = net PnL after tier-0 costs (taker 4.5 bps / maker 1.5 bps). Everything else is diagnostics, never the verdict.
5. **Baseline** — same harness, same windows, overrides absent.

## Verdict vocabulary (emit exactly these)

- **KEEP** — ≥2 valid windows ∧ strict majority improved ∧ aggregate n≥30 ∧ PF>1 ∧ no catastrophic window (>2× baseline worst) ∧ paired sign-flip noise gate passed (one-sided α=0.10; K=4 practical floor — K=3 can never KEEP) → shadow candidate, never execution.
- **DISCARD** — catastrophic window ∨ no majority ∨ PF≤1. "Less bad than baseline" is not an edge. Checked BEFORE the n gate — small-n DISCARD is still DISCARD.
- **INCONCLUSIVE** — otherwise-positive but <2 valid windows ∨ n<30 ∨ noise gate failed.
- **BLOCKED** — no window cells survived, or requires forbidden changes.

Verdicts are ADVISORY drafts. Promotion runs through shadow + watchdog recheck with a human reading the dashboard. Never promote, demote, or edit the registry as a side effect.

## Output

- JSON artifact → `data/research/overnight_experiments/` (gitignored)
- Ledger block appended to `docs/OVERNIGHT_RESEARCH_LOG.md` — hypothesis, windows, baseline vs variant aggregates, per-window delta, reasons, audit line.
