# research_program.md — the overnight research org

An agent running research on the trading bot automatically. It modifies
nothing that trades: it proposes strategy variants, evaluates them through
the existing backtest harnesses against the frozen validation window, and
keeps or discards them using the evidence gates this repo already enforces.
You wake up in the morning to a ledger of experiments and (hopefully) one or
two shadow candidates.

Context: this is the port of [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
to trading. The loop is the same (edit → run → compare → keep/discard);
the judge is different — an LLM training run has millions of samples and a
stationary distribution, our backtests have n≈20–80 trades and a drifting
regime. So **the agent proposes and sweeps; the existing statistical gates
remain the judge.** Automated keep/discard without those gates is an
automated overfitting machine.

## The one invariant

**The agent never touches anything that affects execution.** Concretely:

- NEVER edit `config/settings.yaml`, `src/`, or `main.py`. Variants are
  expressed as CLI flags / config overrides on the harness scripts only.
- NEVER re-register the Fase 10 frozen window, never disable
  `assert_config_matches_preregister`, never bypass `tests/test_config_hash_frozen.py`.
- NEVER edit a strategy to make a variant look better. If a variant needs a
  strategy-code change, STOP and file it in `docs/RESEARCH_BACKLOG.md` for a
  human (this is exactly the hash-neutral rework flow used for VB_REGIMES).
- NEVER touch `.env`, the live DB, the OMS, or run the pre-push gate's
  `--preflight` stage against a live process.
- Config-hash implications must be checked (`python -c` on
  `compute_config_hash`) *before* proposing any override that persists.

If you are tempted to edit any of the above to complete an experiment: that
experiment is not yours to run. Log it as `BLOCKED` in the ledger and move on.

## The experiment unit

Every experiment has, fixed **before running** (micro-preregistration):

1. **Hypothesis** — one sentence, falsifiable. "Delaying flush-fade entry by
   1 bar improves net PnL because entry at the flush extreme is the loss driver."
2. **Family + harness** — which script runs it (table below).
3. **Window set** — the frozen window, split into non-overlapping 30d windows
   (same split as `docs/IV_HIGH_ONLY_AB_SPLIT.md`). The window set is chosen
   once per family and never per-result. Post-hoc window selection is
   forbidden (see the archived XS-momentum verdict: sensitivities that look
   better on the same window are "not a reopen license").
4. **Metrics** — primary: net PnL after tier-0 costs (taker 4.5 bps /
   maker 1.5 bps as applicable). Secondary diagnostics: n, WR, PF, per-window
   breakdown, blocked-PnL (for gate variants). One primary metric, like
   autoresearch's `val_bpb`; everything else is diagnostics, never the verdict.
5. **Baseline** — same harness, same windows, overrides absent. Reuse a cached
   baseline only if code state is unchanged (verify via `git status --porcelain`
   being clean on tracked harness inputs); otherwise re-run.

A "run" is one harness invocation producing a JSON result. Budget ~15–30 min
each; an overnight session is **≤ 20 runs**. Unlike autoresearch there is no
5-minute budget — our runs are slower and the sample is the bottleneck, so
throughput is a handful of experiments per night, and that is fine.

## Families and harnesses

| Family | Harness | Override surface |
|---|---|---|
| LiquidationCatcher variants (delay, stopout, hold) | `scripts/backtest_liquidation_catcher_real.py` | `--delay-min`, `--stopout-off`, `--variants` |
| IV gate (DVOL percentile threshold, high/low-only) | `scripts/iv_high_only_ab_split.py` | threshold, target strategy, windows |
| Regime router / VB regime rework | the router A/B split script (`--split-days` pattern) | regime definitions passed as flags |
| Gate rechecks (bias / flush / IV shadow) | `scripts/research_watchdog_supervisor.py` | read-only; never triggered manually except for testing |
| New families | **write a script** following the existing pattern: CLI flags, non-overlapping windows, JSON dump, docs report — never edit strategy code | — |

If a proposed variant does not map onto an existing harness's override
surface, it goes to the backlog as `NEEDS-CODE` — the overnight loop does
not write strategy code.

## The loop

```
read queue (data/research/overnight_experiments/QUEUE.md — the curated,
  gate-checked list; docs/RESEARCH_BACKLOG.md remains the idea archive)
loop until budget exhausted or queue empty:
  1. pick top queued item; write hypothesis + windows + metrics to the ledger
     BEFORE running (append-only, one block per experiment)
  2. run baseline (or verified cache)
  3. run variant across ALL windows in the fixed set
  4. apply the verdict rules below → KEEP / DISCARD / INCONCLUSIVE / BLOCKED
  5. append verdict + numbers to the ledger; move/annotate the backlog item
  6. if 3 consecutive failures within one family, switch family
write morning report → docs/OVERNIGHT_RESEARCH_LOG.md
```

## Verdict rules (the gates are the judge)

Map to the standing rules in `docs/RESEARCH_BACKLOG.md` and
`docs/RESEARCH_WATCHDOGS.md`:

- **KEEP** — variant becomes a *shadow candidate* (never a direct promotion):
  net improvement vs baseline holds in the **majority of non-overlapping
  windows**, aggregate **n ≥ 30**, primary metric **PF > 1**, no single
  window degrades catastrophically (worse than baseline by more than 2× the
  baseline's worst window loss), and the paired aggregate delta rejects the
  **window-level sign-flip null** at one-sided alpha = 0.10 (exact paired
  per-window randomization test — the unit is the window PAIR, so regime
  correlation is preserved; with all K windows favoring the variant,
  p = 2^−K, so K=3 can never clear alpha=0.10 and K=4 clears at 6.25%:
  four valid windows are the practical floor for KEEP). A point delta on
  n≈20–80 trades is dominated by luck; KEEP means *beyond luck*, not
  merely positive. The KEEP ledger entry must name the exact
  shadow path (e.g. "wire as shadow-only knob X, accumulate n, watchdog
  recheck at n≥30").
- **DISCARD** — fails any KEEP condition, or the improvement exists only in
  the aggregate and not in the window breakdown (overlap caveat in disguise),
  or the improvement is within cost-model noise (compare against the fee
  sensitivity noted in the backlog's tier-0 note). This includes a
  catastrophic window, no majority of improved windows, or aggregate
  `PF ≤ 1` — a negative result does not need `n ≥ 30` to be negative; a
  small n undermines a *positive* claim, not a negative one. DISCARD is
  checked before the n gate.
- **INCONCLUSIVE** — the result clears the negative-result checks above
  (no catastrophic window, majority improved, `PF > 1`) but `n < 30`, or
  fewer than 2 windows survived to compare. Park with the evidence bar
  attached, exactly like the IV gate's n=13 verdict: direction is noted,
  promotion is not. `n ≥ 30` is a precondition for **KEEP**, never a
  precondition for **DISCARD**.

**Per-symbol slices are diagnostics, never gates.** The runner also computes
the same paired sign-flip test restricted to each symbol
(`symbol_gates` in the artifact/ledger) so a verdict can say whether a
variant fixes symbol X specifically (e.g. does a wider band repair HYPE).
They are advisory: a slice can be stellar while the cell-level verdict is
DISCARD — that combination is a forensics lead ("the edge lives only in one
listing"), not an override. Promotions are decided exclusively by the
cell-level gates above, computed over the full cell.
- **BLOCKED** — the experiment needs code/config changes you are not allowed
  to make. State what's needed; a human decides.

Keep/discard only ever compares against the baseline on the **same** data and
code state. If anything tracked changed mid-session, re-run the baseline.

## Gate discipline vs autoresearch

Why the verdict rules above look the way they do. autoresearch's loop is
safe because an LLM training run measures on millions of tokens with a
stationary metric — one experiment's measurement is nearly noise-free, so
"keep on any improvement" works there. Our measurement (net PnL over
n≈20–80 trades on a drifting regime) is not noise-free, and every
protection below exists to counter exactly that. Port the throughput;
keep the judge statistical.

### The keep-list (what survives automation)

| Protection | autoresearch | Rule when automating |
|---|---|---|
| **Frozen window** | A fixed val split, never refreshed — but never protected either: ~100 adaptive experiments all peek at the same eval set and silently overfit it | The Fase 10 window stays frozen and is re-registered only by a human with justification (`compute_config_hash` assert + `tests/test_config_hash_frozen.py`). The automation never holds the re-register pen |
| **Non-overlapping windows** | N/A — i.i.d. tokens; overlap is not a concept | Non-overlapping windows are the only aggregation unit. Verdicts cite the window breakdown, never just the aggregate — the overlap caveat is how the same flush gets counted three times |
| **FDR / multiple-testing control** | None. Sequential adaptivity is unbounded: the next experiment is proposed from all previous results | FDR + date-block bootstrap applies to any *family* of tests. A parameter sweep is a family; a night of 20 experiments on one strategy is one multiple-testing event, not 20 independent chances |
| **Minimum evidence (n≥30 ∧ PF>1)** | No n concept — the metric's sample size is huge by construction | n≥30 is our translation of "val_bpb is precise", but only as a **KEEP** precondition: it gates promotion, not rejection. A result that already fails on majority, PF≤1, or a catastrophic window is DISCARD regardless of n. Below the bar (and only when the result is otherwise positive) the verdict is INCONCLUSIVE with the evidence bar attached, never KEEP. Above the bar the paired delta must reject the window sign-flip null (paired per-window randomization) — autoresearch's "any improvement" rule is exactly the don't-copy below |
| **Shadow-then-enforce** | The winning edit is promoted immediately and automatically | A KEEP produces only a shadow candidate with a named shadow path; promotion runs through shadow accumulation + watchdog recheck and is decided by a human |
| **Budget cap** | A fixed 5-minute budget — good for comparability, but no bound on adaptivity | ≤20 runs/night bounds sequential adaptivity per session; the family-switch rule (3 consecutive failures) stops grinding a dead family. Their comparability trick is worth keeping too: fixed budget, one primary metric, fixed harness |

### The two don't-copies

1. **Keep-on-any-improvement.** Correct for millions of tokens; the
   winner's curse at n≈50 — run enough variants and the best-looking one
   is the luckiest one. This is why the KEEP conditions and the
   INCONCLUSIVE verdict exist at all.
2. **Val-only evaluation with no fresh holdout.** Judging candidates only
   on the windows that motivated them is exactly what the frozen window +
   shadow discipline prevents (cf. the VB follow-through rigor flag:
   W1–W3 are in-sample selection data and are never evidence).

The honest summary: autoresearch automates the *cheap* part — measurement
and comparison — because its measurement is trustworthy. Ours is not, so
we automate the same loop while the judge stays statistical and promotion
stays human. The overnight ledger is advisory evidence with a paper trail,
not a deployment pipeline.

## Ledger and morning report

- Raw results: `data/research/overnight_experiments/<ts>_<slug>.json`
  (gitignored, machine-readable, one per experiment).
- Ledger: `docs/OVERNIGHT_RESEARCH_LOG.md` — append-only, one block per
  experiment: hypothesis, windows, baseline vs variant table, verdict, and
  the single sentence a human needs to audit the decision.
- Morning report: top of the same file — session summary (runs, verdicts,
  any KEEP candidates and their shadow path, any BLOCKED items needing a
  human decision).

A human reviews the ledger before any KEEP candidate is wired as shadow.
The overnight loop ends at the ledger; it never opens a PR to `src/`.

## What success looks like

Not "a better bot in the morning". A morning where:
- 10–20 hypotheses that were parked in the backlog were honestly tested,
- 0 rules were broken (git diff shows only docs/ + ledger changes),
- 0–2 candidates survived with the evidence bar they still must clear,
- and every discarded idea has a written reason it can be checked against.
