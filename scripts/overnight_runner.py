#!/usr/bin/env python3
"""Overnight experiment runner — the autoresearch loop, gated by our discipline.

Implements the runner described in ``research_program.md`` (repo root): an
agent (or cron) sweeps ONE strategy's parameter grid across ALL non-
overlapping windows of the configured research span, compares every cell
against a same-conditions baseline, applies the verdict rules (KEEP requires
majority-of-windows improvement + aggregate n>=30 + PF>1 + no catastrophic
window + paired delta rejecting the window sign-flip null at one-sided
alpha=0.10 — an exact paired per-window randomization test), and writes:

  * per-experiment JSON artifacts -> data/research/overnight_experiments/ (gitignored)
  * the append-only ledger        -> docs/OVERNIGHT_RESEARCH_LOG.md (morning report on top)

Design constraints (research_program.md, hard rules):
  * sweep knobs are CLI/config-dict ONLY — no strategy-code edits, no
    settings.yaml writes, no window re-registration (config-hash invariant);
  * the window split comes from ``split_windows`` (regime-router A/B pattern)
    — non-overlapping windows, chosen once per family, never per-result;
  * verdicts are drafted automatically but ADVISORY: promotion still runs
    through shadow + watchdog recheck with a human reading the dashboard.

Usage:
  # LiquidationCatcher flush-fade sweep (delay x stopout), non-overlapping 30d windows
  python scripts/overnight_runner.py --family flush_fade --start 2026-05-18 --end 2026-08-07

  # VWAP per-symbol z_threshold sweep (Night 2, QUEUE.md): baseline 2.5σ vs
  # 3.0σ HYPE-only vs 3.0σ everywhere
  python scripts/overnight_runner.py --family vwap_thresholds --start 2026-05-18 --end 2026-09-08

  # IV high/low cut sweep (Night 3 accumulator, QUEUE.md — K-capped by DVOL
  # coverage: INCONCLUSIVE/DISCARD are the only possible verdicts):
  python scripts/overnight_runner.py --family iv_thresholds --start 2026-06-14 --end 2026-09-08 --symbols BTC,ETH,SOL,HYPE

  # Self-test: verdict + report logic only, canned results, no backtests:
  python scripts/overnight_runner.py --selftest
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# NOTE: the non-overlapping split is REUSED from the regime-router A/B
# (``split_windows``), imported lazily inside sweep() so that --selftest and
# the verdict/report unit tests never pull the backtest engine stack.

ARTIFACT_DIR = ROOT / "data" / "research" / "overnight_experiments"
LEDGER_PATH = ROOT / "docs" / "OVERNIGHT_RESEARCH_LOG.md"
LEDGER_HEADER = """# Overnight research ledger — append-only

One block per experiment: hypothesis, window set, baseline vs variant per
window, verdict, and the one sentence a human needs to audit the decision.
Morning report (latest session) on top. Verdicts are DRAFTED here — advisory
evidence only; promotion runs through shadow + watchdog recheck with a human
reading the dashboard (research_program.md: the agent proposes, the gates
judge, the human enforces).

This preamble is the reference for the loop's two contracts (queue format
and verdict schema) plus one worked example from a real session. It is
rewritten verbatim on every session by the runner — edit it in
`scripts/overnight_runner.py` (LEDGER_HEADER), never here.

## Queue format (`data/research/overnight_experiments/QUEUE.md`)

One entry per experiment, ordered by night. States:

- **READY** — family wired in the runner; the entry runs as-is.
- **NEEDS-WIRING** — requires a new family function (config-dict override
  surface only); goes under "queued after wiring".
- **BLOCKED** — a measured gate is not met; the entry names the reopen gate
  so future sessions check a number instead of re-arguing an idea.
- **NEEDS-CODE** — strategy-code change required; human decision only.
- **CLOSED** — definitive verdict on file; do not re-litigate.

Required fields per entry: **hypothesis** (one falsifiable sentence, fixed
a priori), **harness** (the exact runner command), **window set** (chosen
once per family — never per result; rigor note when a variant was selected
on any overlapping sample), **grid** (cells × windows ≤ the 20-run/night
budget), **evidence bar** (what KEEP requires here), and — when honest —
**expected verdict** (an evidence-starved session says so upfront).
Spare budget is never spent on unplanned windows.

## Verdict schema (enforced by `decide()`, restating research_program.md)

| Verdict | Condition (all must hold for KEEP) |
|---|---|
| **KEEP** | ≥2 valid windows ∧ strict majority improved ∧ aggregate n≥30 ∧ PF>1 ∧ no window worse than 2× the baseline's worst ∧ noise gate passed (exact paired per-window sign-flip test, one-sided alpha=0.10 — the unit is the window PAIR so regime correlation is preserved; p = fraction of sign patterns with sum ≥ observed; with all K windows favoring the variant p = 2^−K, so K=3 cannot clear alpha and K=4 is the practical floor; skipped with an explicit reason when per-trade PnL is unavailable) → **shadow candidate** with a named shadow path |
| **INCONCLUSIVE** | fewer than 2 valid windows (a majority of one is not multi-window evidence) ∨ n<30 ∨ noise gate failed (paired delta not beyond the window sign-flip null) — parked with the evidence bar attached |
| **DISCARD** | no majority ∨ PF≤1 ∨ catastrophic window — including the case "improved everywhere but still loses money": less bad than the baseline is not an edge |
| **BLOCKED** | no window cells survived to compare (or the experiment needs forbidden changes — program-level BLOCKED, logged, never run) |

Reasons vocabulary the runner emits: `windows improved X/Y`,
`aggregate n=` / `aggregate PF=` (gate values),
`excluded N no-evidence window(s)` (both cells n=0 — absence of data,
never scored as improvement), `catastrophic window:`, `noise gate:`
(paired sign-flip p-value, alpha, K, method; `skipped:` when data is
missing), and the SHADOW CANDIDATE line on KEEP.

Per-experiment ledger block fields: hypothesis · window span + count ·
baseline tag and aggregate (net, n, PF) · variant aggregate · per-window
delta (`n/e` marks a no-evidence window) · reasons · audit line. The JSON
artifact (same data, machine-readable, gitignored) carries per-cell
details: `total_pnl_usd`, `n_trades`, `gross_win/loss_usd`,
`trades_summary`, `trade_pnls` + `trade_symbols` (the per-window paired
noise gate and its per-symbol slices need these), `manifest`, the
`noise_gate` dict per variant (p_value, alpha, method, per-window deltas),
and `symbol_gates` per variant (the SAME paired sign-flip test restricted
to each symbol — **advisory only**: it answers "does the variant fix symbol
X specifically?" but never feeds the verdict; the cell-level gate is the
only promotion gate, and a great slice on a losing cell is a forensics
lead, not an edge).

## Worked example — Night 1, flush_fade `delay=0 stopout=OFF` (real run)

The stop-out bypass improved the baseline in **3 of 3** valid windows
(+145.77 net over the baseline, n=54) — under autoresearch's
"keep on any improvement" this is a KEEP. It is a **DISCARD**: the variant
still loses money (PF=0.306); beating a bleeding baseline is not an edge.
This is the PF gate doing the winner's-curse work, and the reason verdicts
cite numbers, not vibes:

```
### 2026-09-09 12:23 UTC — flush_fade/delay=0 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the
  same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-325.67 n=54 PF=0.306
- per-window delta: 2026-08(+61.64), 2026-08(n/e), 2026-08(+77.69), 2026-09(+6.44)
- reasons: excluded 1 no-evidence window(s) (both cells n=0 — absence of
  data, not improvement); windows improved 3/3 (majority=yes); aggregate
  n=54 (gate >=30); aggregate PF=0.306 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion
  only via shadow + watchdog recheck.
```

(The `n/e` window is the 08-19..08-28 feed gap — zero real liquidation
events, bot idle; the verdict stands on 3 windows. Sessions run after the
noise-model commit would add a `noise gate:` line here; this run predates
it, and its per-trade PnL is not stored in the artifact, so none is shown.)

---
"""


# ---------------------------------------------------------------------------
# Verdict rules (research_program.md) — pure functions so tests need no engine.
# ---------------------------------------------------------------------------

N_GATE = 30
PF_GATE = 1.0
CATASTROPHIC_MULT = 2.0

# Noise model (research_program.md "Gate discipline vs autoresearch"): a
# point delta on n≈20–80 trades is dominated by luck — the winner's curse
# at work. KEEP requires the paired aggregate delta to reject the
# window-level null at the one-sided 10% level, not merely a positive
# delta.
#
# Why paired: the windows are regimes — both sides trade the same flush
# clusters in the same minutes. The resampling unit is therefore the
# window PAIR: the variant−baseline delta of that window, computed from
# both sides' trade lists together. Window-level correlation is preserved
# BY CONSTRUCTION — a window's delta magnitude is never decomposed.
#
# The null (H0: no edge — within a pair, which side is "variant" is
# exchangeable) is the EXACT randomization (sign-flip) distribution: all
# 2^K sign patterns of the K paired deltas, enumerated when K ≤ 12 (4096
# patterns), seeded sampling beyond. The gate passes iff
#   p = P_H0(sum ≥ observed) ≤ alpha   (alpha = one-sided 10%)
# Exactness beats an interpolated percentile of a coarse discrete null:
# the floor is principled — with all-K windows favoring the variant,
# p = 2^−K, so K=3 floors at 12.5% (cannot clear 10%) and K=4 clears at
# 6.25%. Three windows can never KEEP on the noise gate; four can.
BOOTSTRAP_RESAMPLES = 2000      # sampled patterns when K > SIGNFLIP_MAX_EXACT_K
BOOTSTRAP_CI_PCTL = 90          # reported null percentile (informative)
BOOTSTRAP_SEED = 20260909       # sampling seed for K > SIGNFLIP_MAX_EXACT_K
SIGNFLIP_MAX_EXACT_K = 12       # 2^12 = 4096 patterns, enumerated exactly
NOISE_ALPHA = (100 - BOOTSTRAP_CI_PCTL) / 100.0  # one-sided 0.10


def np_percentile(values: Sequence[float], pct: int) -> float:
    """Linear-interpolated percentile (numpy's convention) without numpy."""
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return float(s[f] + (s[c] - s[f]) * (k - f))


def paired_window_deltas(baseline_windows: Sequence[Dict[str, Any]],
                         variant_windows: Sequence[Dict[str, Any]],
                         ) -> Optional[List[float]]:
    """Observed per-window paired deltas (variant − baseline), by window.

    Each side's window PnL is the sum of that window's ``trade_pnls`` — the
    pair moves together, so every source of within-window correlation
    (same flush cluster, same minutes, same regime) is preserved in the
    delta. Returns ``None`` when per-trade data is MISSING on any valid
    window (legacy artifacts): the caller skips the gate rather than
    inventing numbers. A double-zero window (both sides empty — absence of
    data) is skipped as no-evidence; a one-sided empty window yields a
    delta against zero on that side (legitimate: different entry
    conditions). Error cells are skipped.
    """
    deltas: List[float] = []
    for b, v in zip(baseline_windows, variant_windows):
        if "error" in b or "error" in v:
            continue
        bp = b.get("trade_pnls")
        vp = v.get("trade_pnls")
        if not isinstance(bp, (list, tuple)) or not isinstance(vp, (list, tuple)):
            return None
        if not bp and not vp:
            continue  # double-zero window: absence of data, not evidence
        deltas.append(sum(float(x) for x in vp) - sum(float(x) for x in bp))
    return deltas


def signflip_null(
    deltas: Sequence[float], resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Tuple[List[float], str]:
    """Null distribution of the paired sign-flip randomization test.

    * K <= SIGNFLIP_MAX_EXACT_K: all 2^K sign patterns enumerated — exact
      and fully deterministic (no RNG anywhere in the verdict).
    * K > SIGNFLIP_MAX_EXACT_K: ``resamples`` seeded random patterns —
      reproducible, resolution 1/resamples.

    Returns ``(null_aggs, method_label)``. Shared by the cell-level gate
    and the per-symbol slices so both use the SAME randomization test.
    """
    k = len(deltas)
    if k <= SIGNFLIP_MAX_EXACT_K:
        null_aggs = []
        for mask in range(1 << k):
            s = 0.0
            for i in range(k):
                s += deltas[i] if (mask >> i) & 1 else -deltas[i]
            null_aggs.append(s)
        return null_aggs, f"paired_window_signflip_exact_k{k}"
    rng = random.Random(seed)
    null_aggs = []
    for _ in range(resamples):
        s = 0.0
        for i in range(k):
            s += deltas[i] if rng.random() < 0.5 else -deltas[i]
        null_aggs.append(s)
    return null_aggs, f"paired_window_signflip_sampled_k{k}_n{resamples}"


def paired_bootstrap_noise_gate(baseline_windows: Sequence[Dict[str, Any]],
                                variant_windows: Sequence[Dict[str, Any]],
                                alpha: float = NOISE_ALPHA,
                                resamples: int = BOOTSTRAP_RESAMPLES,
                                seed: int = BOOTSTRAP_SEED) -> Dict[str, Any]:
    """Exact paired sign-flip randomization test on the window deltas.

    H0: no edge — within each window pair, which side is "variant" is
    exchangeable, so every sign pattern of the K paired deltas is equally
    likely. p-value = fraction of patterns with aggregate sum ≥ observed
    (ties count — standard randomization-test conservatism). Gate passes
    iff ``p ≤ alpha`` (one-sided 10% by default).

    * K ≤ 12: all 2^K patterns enumerated — exact and fully deterministic
      (no RNG anywhere in the verdict).
    * K > 12: ``resamples`` random sign patterns (seeded) — reproducible,
      resolution 1/resamples.

    The p-value uses the signs AND magnitudes of the paired deltas (any
    negative window raises p above 2^−K), but the floor is real: K=3
    cannot clear alpha=0.10 even with every window improved (p=0.125).

    Returns ``{"evaluated": bool, "pass": bool|None, ...}`` — ``evaluated``
    False (and ``pass`` None) when per-trade data is unavailable; the
    caller then skips the gate and the verdict says so explicitly. Never
    invents numbers.
    """
    deltas = paired_window_deltas(baseline_windows, variant_windows)
    if deltas is None:
        return {"evaluated": False, "pass": None,
                "reason": "per-trade PnL unavailable — noise gate skipped"}
    if not deltas:
        return {"evaluated": False, "pass": None,
                "reason": "no valid window pairs — noise gate skipped"}
    observed = sum(deltas)
    null_aggs, method = signflip_null(deltas, resamples=resamples, seed=seed)
    ge = sum(1 for x in null_aggs if x >= observed - 1e-9)
    p_value = ge / len(null_aggs)
    required = float(np_percentile(null_aggs, BOOTSTRAP_CI_PCTL))
    return {
        "evaluated": True,
        "pass": bool(p_value <= alpha),
        "alpha": alpha,
        "p_value": round(p_value, 4),
        "null_ge_observed_frac": round(p_value, 4),
        "observed_delta": round(observed, 2),
        "required_delta": round(required, 2),  # null p90 — informative
        "null_p" + str(BOOTSTRAP_CI_PCTL): round(required, 2),
        "n_windows": len(deltas),
        "per_window_deltas": [round(d, 2) for d in deltas],
        "method": method,
    }


def _symbol_pnl_map(c: Dict[str, Any]) -> Optional[Dict[str, List[float]]]:
    """Per-symbol PnL map of one cell — attached map, or derived from the
    parallel ``trade_pnls``/``trade_symbols`` lists. ``None`` when neither
    exists (the slice is then skipped, never invented)."""
    m = c.get("trade_pnls_by_symbol")
    if isinstance(m, dict):
        return m
    pnls = c.get("trade_pnls")
    syms = c.get("trade_symbols")
    if (not isinstance(pnls, (list, tuple))
            or not isinstance(syms, (list, tuple))
            or len(pnls) != len(syms)):
        return None
    out: Dict[str, List[float]] = {}
    for p, s in zip(pnls, syms):
        out.setdefault(str(s), []).append(float(p))
    return out


def symbol_noise_gate(
    baseline_windows: Sequence[Dict[str, Any]],
    variant_windows: Sequence[Dict[str, Any]],
    symbol: str,
    alpha: float = NOISE_ALPHA,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> Dict[str, Any]:
    """The SAME exact paired sign-flip test, restricted to one symbol.

    ADVISORY ONLY — it never feeds ``decide()``. It answers a different
    question than the cell-level gate: does the variant fix symbol X
    specifically? Deltas stay window-PAIRED (same pairing rule as the
    cell-level gate, so within-window regime correlation is preserved);
    a double-zero window for this symbol is no evidence, not improvement.
    Skipped (never invented) when either side lacks per-symbol PnL for the
    symbol (an attached ``trade_pnls_by_symbol`` map or aligned parallel
    ``trade_pnls``/``trade_symbols`` lists both work). The cell-level
    verdict remains the only promotion gate.
    """
    deltas: List[float] = []
    n_base = n_var = 0
    for bc, vc in zip(baseline_windows, variant_windows):
        if "error" in bc or "error" in vc:
            continue
        bm = _symbol_pnl_map(bc)
        vm = _symbol_pnl_map(vc)
        if bm is None or vm is None:
            return {"evaluated": False, "pass": None, "symbol": symbol,
                    "reason": "per-symbol PnL unavailable — slice skipped"}
        # A present-but-empty map means zero trades for the symbol (e.g. a
        # listing that starts mid-span) — a double-zero window is no
        # evidence (skipped), a one-sided empty is a delta against zero
        # (same rule as the cell-level gate). Missing maps entirely are
        # unavailable data (skip the slice), never zero.
        bp = [float(x) for x in bm.get(symbol, [])]
        vp = [float(x) for x in vm.get(symbol, [])]
        n_base += len(bp)
        n_var += len(vp)
        if not bp and not vp:
            continue  # double-zero window for this symbol: absence of data
        deltas.append(sum(vp) - sum(bp))
    if not deltas:
        return {"evaluated": False, "pass": None, "symbol": symbol,
                "reason": "no valid window pairs for this symbol — slice skipped"}
    observed = sum(deltas)
    null_aggs, method = signflip_null(deltas, resamples=resamples, seed=seed)
    ge = sum(1 for x in null_aggs if x >= observed - 1e-9)
    p_value = ge / len(null_aggs)
    return {
        "evaluated": True,
        "pass": bool(p_value <= alpha),
        "symbol": symbol,
        "alpha": alpha,
        "p_value": round(p_value, 4),
        "observed_delta": round(observed, 2),
        "n_baseline": n_base,
        "n_variant": n_var,
        "n_windows": len(deltas),
        "per_window_deltas": [round(d, 2) for d in deltas],
        "method": method,
        "advisory_only": True,
    }


def cell_pnl(cell: Dict[str, Any]) -> float:
    """Net PnL of one backtest cell; a failed cell scores as None (missing)."""
    if "error" in cell:
        return float("nan")
    return float(cell.get("total_pnl_usd", 0.0))


def trade_pnls_by_symbol(
    cells: Sequence[Dict[str, Any]]
) -> List[Optional[Dict[str, List[float]]]]:
    """Attach per-symbol per-trade PnL to window cells (in place, best-effort).

    Cells carry ``trade_pnls`` plus a parallel ``trade_symbols`` list (same
    order); when both are present and aligned, each cell gains
    ``trade_pnls_by_symbol = {sym: [pnl, ...]}`` for the per-symbol noise
    slices. Cells without symbols (older artifacts, minimal tests) are left
    untouched — the slice is then skipped downstream instead of inventing
    data. Returns the per-cell maps (None where unavailable).
    """
    out: List[Optional[Dict[str, List[float]]]] = []
    for c in cells:
        if "error" in c:
            out.append(None)
            continue
        pnls = c.get("trade_pnls")
        syms = c.get("trade_symbols")
        if (not isinstance(pnls, (list, tuple))
                or not isinstance(syms, (list, tuple))
                or len(pnls) != len(syms)):
            out.append(None)
            continue
        m: Dict[str, List[float]] = {}
        for p, s in zip(pnls, syms):
            m.setdefault(str(s), []).append(float(p))
        c["trade_pnls_by_symbol"] = m
        out.append(m)
    return out


def delta_per_window(baseline: Sequence[Dict[str, Any]],
                     variant: Sequence[Dict[str, Any]]) -> List[Optional[float]]:
    """Per-window PnL delta (variant − baseline), matched by order.

    ``None`` (no delta invented) when either cell failed OR when both cells
    have zero trades — a no-trade window is the absence of evidence, not a
    0.0 "improvement", and must not count toward the majority.
    """
    out: List[Optional[float]] = []
    for b, v in zip(baseline, variant):
        if "error" in b or "error" in v:
            out.append(None)
            continue
        if int(b.get("n_trades", 0)) == 0 and int(v.get("n_trades", 0)) == 0:
            out.append(None)
            continue
        out.append(round(float(v.get("total_pnl_usd", 0.0))
                         - float(b.get("total_pnl_usd", 0.0)), 2))
    return out


def aggregate(cells: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Aggregate PnL / n / PF over window cells (failed cells excluded)."""
    ok = [c for c in cells if "error" not in c]
    pnl = sum(float(c.get("total_pnl_usd", 0.0)) for c in ok)
    n = sum(int(c.get("n_trades", 0)) for c in ok)
    gw = sum(float(c.get("gross_win_usd", 0.0)) for c in ok)
    gl = sum(float(c.get("gross_loss_usd", 0.0)) for c in ok)
    return {"pnl": round(pnl, 2), "n": n,
            "pf": round(gw / gl, 3) if gl > 0 else (float("inf") if gw > 0 else 0.0)}


def decide(baseline_windows: Sequence[Dict[str, Any]],
           variant_windows: Sequence[Dict[str, Any]],
           noise_model: bool = False) -> Tuple[str, List[str]]:
    """Apply the research_program.md verdict rules to a window-set result.

    Returns ``(verdict, reasons)`` with verdict in
    KEEP / DISCARD / INCONCLUSIVE / BLOCKED.

    KEEP requires ALL of:
      * at least 2 non-failed windows (a majority of one is not
        multi-window evidence),
      * strict majority of non-failed windows improved (delta > 0),
      * aggregate n >= 30,
      * aggregate PF > 1,
      * no window degrades worse than CATASTROPHIC_MULT x the baseline's
        own worst window loss (the "no catastrophic window" rule).
    INCONCLUSIVE overrides when aggregate n < 30 (park with the bar attached)
    or when fewer than 2 windows survived.

    ``noise_model=True`` (research_program.md: KEEP must reject the
    paired window-level null, not just print a positive delta) adds one
    more KEEP condition: the exact paired sign-flip randomization test on
    the window deltas must reject H0 at the one-sided 10% level. Only
    surviving-cell verdicts can demote to INCONCLUSIVE with "noise" in
    the reason, so legacy results (no per-trade data) keep their
    historical verdicts.
    """
    deltas = delta_per_window(baseline_windows, variant_windows)
    reasons: List[str] = []
    valid = [d for d in deltas if d is not None]
    no_evidence = len(deltas) - len(valid)
    if not valid:
        return "BLOCKED", ["all window cells failed — nothing to compare"]
    if no_evidence:
        reasons.append(
            f"excluded {no_evidence} no-evidence window(s) "
            "(both cells n=0 — absence of data, not improvement)"
        )

    agg_b = aggregate(baseline_windows)
    agg_v = aggregate(variant_windows)

    improved = sum(1 for d in valid if d > 0)
    majority = improved > len(valid) / 2.0
    reasons.append(
        f"windows improved {improved}/{len(valid)} "
        f"(majority={'yes' if majority else 'no'})"
    )
    reasons.append(f"aggregate n={agg_v['n']} (gate >={N_GATE})")
    reasons.append(f"aggregate PF={agg_v['pf']} (gate >{PF_GATE})")

    # Catastrophic-window rule: worst variant window must not be more than
    # 2x worse than the baseline's own worst window.
    base_losses = [-cell_pnl(c) for c in baseline_windows if "error" not in c and cell_pnl(c) < 0]
    worst_base_loss = max(base_losses) if base_losses else 0.0
    worst_variant_loss = min([cell_pnl(c) for c in variant_windows if "error" not in c])
    catastrophic = (
        worst_variant_loss < 0 and worst_base_loss == 0.0
    ) or (
        worst_base_loss > 0 and worst_variant_loss < -CATASTROPHIC_MULT * worst_base_loss
    )
    if catastrophic:
        reasons.append(
            f"catastrophic window: worst variant {worst_variant_loss:.2f} vs "
            f"baseline worst {-worst_base_loss:.2f} (>{CATASTROPHIC_MULT}x)"
        )

    if len(valid) < 2:
        return "INCONCLUSIVE", reasons + [
            f"only {len(valid)} valid window survived — a majority of one is "
            "not multi-window evidence"
        ]
    if agg_v["n"] < N_GATE:
        return "INCONCLUSIVE", reasons + [
            "n<30 — park with evidence bar attached (IV-gate n=13 precedent)"
        ]
    if catastrophic:
        return "DISCARD", reasons
    if majority and agg_v["pf"] > PF_GATE:
        if noise_model:
            ng = paired_bootstrap_noise_gate(baseline_windows, variant_windows)
            if ng["evaluated"]:
                reasons.append(
                    f"noise gate: paired sign-flip p={ng['p_value']} "
                    f"(alpha={ng['alpha']}, K={ng['n_windows']} windows, "
                    f"{ng['method']}) — "
                    + ("passed" if ng["pass"] else "NOT passed")
                )
                if not ng["pass"]:
                    return "INCONCLUSIVE", reasons + [
                        "noise: paired delta not beyond the window sign-flip "
                        "null — accumulate windows or re-test out-of-sample"
                    ]
            else:
                reasons.append(f"noise gate skipped: {ng['reason']}")
        return "KEEP", reasons + [
            "becomes a SHADOW CANDIDATE — name the shadow path + watchdog recheck; never a direct promotion"
        ]
    return "DISCARD", reasons


# ---------------------------------------------------------------------------
# Families — each returns (grid_tags, run_one) over the shared window runner.
# ---------------------------------------------------------------------------

FLUSH_FADE_GRID: Tuple[Tuple[int, bool], ...] = (
    (0, True),    # baseline: the loop (flush -> stop-out next minute)
    (0, False),   # stop-out bypass only
    (1, False),   # bypass + 1-min confirmation delay
    (5, False),   # bypass + 5-min delay
    (10, False),  # bypass + 10-min delay
    (10, True),   # delay only, stop-out still on
)


def flush_fade_family() -> Tuple[List[str], Callable[..., Dict[str, Any]], Callable[..., Any]]:
    """Wire the flush-fade family to the existing LiquidationCatcher harness.

    Imports inside this function so ``--selftest`` never pulls torch-heavy
    modules (the backtest engine imports the full stack).
    """
    from scripts.backtest_liquidation_catcher_real import (  # noqa: E402
        _prepare_db, run_cell,
    )
    from src.utils.config import load_config  # noqa: E402

    cfg = load_config(str(ROOT / "config" / "settings.yaml"))

    def run_one(start: str, end: str, symbols: List[str],
                delay_min: int, stopout_on: bool) -> Dict[str, Any]:
        s_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        e_ms = int(datetime.strptime(end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999000,
            tzinfo=timezone.utc).timestamp() * 1000)
        bt_db = _prepare_db(cfg, symbols, s_ms, e_ms)
        return run_cell(cfg, bt_db, symbols, s_ms, e_ms,
                        delay_min=delay_min, stopout_on=stopout_on, verbose=False)

    tags = [f"delay={d} stopout={'ON' if s else 'OFF'}" for d, s in FLUSH_FADE_GRID]
    return tags, run_one, cfg


FAMILIES = {
    "flush_fade": flush_fade_family,
}


# ---------------------------------------------------------------------------
# Family: vwap_thresholds — per-symbol VWAP z_threshold sweep (Night 2).
#
# QUEUE.md preregistration: a single 2.5σ threshold treats BTC and HYPE as
# the same animal; HYPE trades later (data from 06-19 only) and thinner, so
# its fade plausibly needs a wider band. Grid: production 2.5σ everywhere
# (baseline) vs 3.0σ HYPE-only vs 3.0σ everywhere. Override surface is a
# config-dict on top of the production strategy.vwap_deviation section —
# NOTHING else moves (session filter, exits, confidence all as production).
# ---------------------------------------------------------------------------

VWAP_THRESHOLDS_GRID: Tuple[Dict[str, float], ...] = (
    {},            # baseline: production z_threshold everywhere
    {"HYPE": 3.0}, # 3.0σ HYPE-only
    {"*": 3.0},    # 3.0σ everywhere
)


def resolve_z(overrides: Dict[str, float], symbol: str, base_z: float) -> float:
    """Per-symbol z_threshold: explicit symbol wins, then ``*``, then base."""
    if symbol in overrides:
        return float(overrides[symbol])
    if "*" in overrides:
        return float(overrides["*"])
    return float(base_z)


def vwap_thresholds_family() -> Tuple[List[str], Callable[..., Dict[str, Any]], Callable[..., Any]]:
    """Wire the vwap_thresholds family to the vwap trend-vs-fade harness.

    Reuses that harness's ``light_replay`` (15m confirm bars, 1h VWAP, tier-0
    fee model: commission + slippage per side) — the same engine that produced
    the session-filter winners. Each symbol is replayed separately (the
    strategy carries one z_threshold per instance), each with the configured
    initial capital — the SAME convention for baseline and variants, so the
    comparison stays internally consistent; cross-symbol capital sharing is
    out of scope for the sweep. ``require_oir_confirm`` is disabled exactly as
    the harness's own fade path does (light replay has no OIR feed).

    Imports inside this function so ``--selftest`` never pulls the strategies.
    """
    from scripts.backtest_vwap_trend_vs_fade import light_replay  # noqa: E402
    from src.data.database import Database  # noqa: E402
    from src.strategies.vwap_deviation import VWAPDeviation  # noqa: E402
    from src.utils.config import load_config  # noqa: E402

    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    fade_section = dict(cfg.get("strategy.vwap_deviation", {}) or {})
    base_z = float(fade_section.get("z_threshold", 2.5))
    db = Database(str(cfg.get("database.path", "data/live/bot.db")))
    initial_capital = float(
        cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
    )
    commission_pct = float(cfg.get("backtest.commission_pct", 0.04))
    slippage_bps = float(cfg.get("backtest.slippage_bps", 2.0))

    def tag_for(ov: Dict[str, float]) -> str:
        if not ov:
            return f"z={base_z} (baseline)"
        if "*" in ov:
            return f"z={ov['*']} all"
        rest = ",".join(f"{k}:{v}" for k, v in sorted(ov.items()))
        return f"z={base_z}+{rest}"

    def run_one(start: str, end: str, symbols: List[str],
                z_overrides: Dict[str, float]) -> Dict[str, Any]:
        s_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        e_ms = int(datetime.strptime(end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999000,
            tzinfo=timezone.utc).timestamp() * 1000)
        trades_all: List[Dict[str, Any]] = []
        per_symbol: Dict[str, Dict[str, Any]] = {}
        try:
            for sym in symbols:
                z = resolve_z(z_overrides, sym, base_z)
                section = dict(fade_section)
                section["z_threshold"] = z
                section["enabled"] = True
                section["require_oir_confirm"] = False
                strategy = VWAPDeviation(section)
                res = light_replay(
                    db, strategy, [sym], s_ms, e_ms,
                    bar_tf="15m",
                    initial_capital=initial_capital,
                    commission_pct=commission_pct,
                    slippage_bps=slippage_bps,
                )
                trs = list(res.get("trades", []) or [])
                for t in trs:
                    t["z_threshold"] = z
                    t["trade_symbol"] = sym
                trades_all.extend(trs)
                per_symbol[sym] = {
                    "z_threshold": z,
                    "n_trades": len(trs),
                    "total_pnl_usd": round(sum(float(t.get("pnl_usd", 0.0)) for t in trs), 2),
                }
        except Exception as exc:  # noqa: BLE001 — a failed cell must not kill the sweep
            return {"error": f"{type(exc).__name__}: {exc}", "z_overrides": dict(z_overrides)}

        pnls = [float(t.get("pnl_usd", 0.0)) for t in trades_all]
        exits: Dict[str, Dict[str, float]] = {}
        for t in trades_all:
            r = str(t.get("exit_reason") or "unknown")
            exits.setdefault(r, {"n": 0, "pnl_usd": 0.0})
            exits[r]["n"] += 1
            exits[r]["pnl_usd"] += float(t.get("pnl_usd", 0.0))
        return {
            "z_overrides": dict(z_overrides),
            "n_trades": len(pnls),
            "total_pnl_usd": round(sum(pnls), 2),
            "gross_win_usd": round(sum(p for p in pnls if p > 0), 2),
            "gross_loss_usd": round(abs(sum(p for p in pnls if p < 0)), 2),
            "trade_pnls": [round(p, 2) for p in pnls],
            "trade_symbols": [str(t.get("trade_symbol") or t.get("symbol") or "")
                              for t in trades_all],
            "trades_summary": {
                k: {"n": int(v["n"]), "pnl_usd": round(v["pnl_usd"], 2)}
                for k, v in sorted(exits.items())
            },
            "per_symbol": per_symbol,
            "cost_model": {"commission_pct": commission_pct, "slippage_bps": slippage_bps},
            "sizing_convention": "per-symbol isolated capital (same for baseline and variants)",
        }

    tags = [tag_for(ov) for ov in VWAP_THRESHOLDS_GRID]
    return tags, run_one, cfg


# ---------------------------------------------------------------------------
# Family: iv_thresholds — high/low-IV cut sweep (Night 3).
#
# QUEUE.md preregistration: the IV gate variant "both strategies only in
# high_iv" showed +42.99 USD (n=13) on the single 05-18..08-07 window and
# was never confirmed robustly on independent windows. This family sweeps
# the high_iv cut itself — the canonical cut lives in
# src/data/dvol_feed.py (IV_HIGH_PCT = 66.7): baseline = NO IV gate (raw
# strategies) vs high_iv-only at 63.3 (lower tercile) / 66.7 (canonical) /
# 70 (strict). The gate is applied POST-HOC per trade — a filter on the
# SAME raw trade set the production shadow decision (iv_gate_shadow)
# classifies — so no strategy code, settings.yaml, or frozen-window value
# moves (allowed surface).
#
# DVOL loads from the persisted research DB (dvol_daily, written by the
# production DvolFeed) — NO network fetch: an overnight run must not
# depend on Deribit being reachable. Missing coverage is a hard error (a
# silent empty classification would fabricate evidence).
#
# Statistical reality (QUEUE.md, data facts 2026-09-09): DVOL starts
# 2026-06-14, so the historical span clamps to 06-14..09-08 → K=3 windows.
# At K=3 the exact sign-flip noise gate cannot pass (all-positive floors
# at p=2^-3 = 12.5% > alpha=0.10) — this session is the pre-declared
# ACCUMULATOR: INCONCLUSIVE/DISCARD are the only possible verdicts; the
# run grows the artifact sample and exercises the harness end-to-end.
# ---------------------------------------------------------------------------

IV_THRESHOLDS_GRID: Tuple[Optional[float], ...] = (
    None,   # baseline: no IV gate — the raw strategies as production runs them
    63.3,   # high_iv-only, lower tercile cut
    66.7,   # high_iv-only, canonical cut (matches the +42.99 evidence)
    70.0,   # high_iv-only, strict cut
)

IV_THRESHOLDS_SPAN = ("2026-06-14", "2026-09-08")  # DVOL-bounded; see block docstring

# Raw-trade cache: the engine pass per (window, symbols) is IDENTICAL for
# every cut (the gate only filters), so a 4-cell sweep pays one engine run
# per window per strategy, not one per cell.
_IV_RAW_CACHE: Dict[Tuple[str, str, Tuple[str, ...]], List[Dict[str, Any]]] = {}


def apply_iv_gate(
    raw: List[Dict[str, Any]], cut: Optional[float]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split raw trades into (kept, blocked) at the cut — pure, no DB.

    A trade is kept iff its trailing-30d IV percentile (last completed DVOL
    day, no lookahead) is strictly above the cut. Trades with no percentile
    (``_iv_pct`` None — before DVOL coverage) are ALWAYS blocked: an
    unclassifiable trade must never silently survive an IV gate.
    """
    if cut is None:
        return list(raw), []
    kept: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for t in raw:
        pct = t.get("_iv_pct")
        if pct is not None and float(pct) > float(cut):
            kept.append(t)
        else:
            blocked.append(t)
    return kept, blocked


def iv_cell_from_raw(
    raw: List[Dict[str, Any]], kept: List[Dict[str, Any]],
    blocked: List[Dict[str, Any]], cut: Optional[float],
    spec_names: Sequence[str],
) -> Dict[str, Any]:
    """Build the runner's per-window cell dict from a raw/kept split."""
    pnls = [float(t.get("pnl_usd", 0.0) or 0.0) for t in kept]
    return {
        "iv_cut": cut,
        "n_trades": len(pnls),
        "total_pnl_usd": round(sum(pnls), 2),
        "gross_win_usd": round(sum(p for p in pnls if p > 0), 2),
        "gross_loss_usd": round(abs(sum(p for p in pnls if p < 0)), 2),
        "trade_pnls": [round(p, 2) for p in pnls],
        "trade_symbols": [str(t.get("symbol") or "") for t in kept],
        "n_raw": len(raw),
        "n_no_iv": sum(1 for t in raw if t.get("_iv_pct") is None),
        "n_blocked": len(blocked),
        "blocked_pnl_usd": round(
            sum(float(t.get("pnl_usd", 0.0) or 0.0) for t in blocked), 2
        ),
        "n_by_strategy": {
            name: sum(1 for t in kept if t.get("_strategy") == name)
            for name in spec_names
        },
    }


def _load_dvol_series(
    symbols: List[str], start_ms: int, end_ms: int
) -> Tuple[Dict[str, List[Tuple[int, Optional[float]]]], List[Tuple[int, Optional[float]]]]:
    """Per-symbol daily IV-percentile series from the persisted ``dvol_daily``.

    Same shape the A/B harness builds from ``fetch_dvol`` — without touching
    the network. Both currencies are loaded (SOL/HYPE inherit the BTC index
    via ``dvol_series_for``). Raises if a currency has no closes: an
    overnight run must fail loud rather than classify everything low_iv.
    """
    from src.data.dvol_feed import (  # noqa: E402
        DVOL_WINDOW_DAYS,
        build_iv_percentile,
        dvol_series_for,
    )
    from src.data.research_database import ResearchDatabase  # noqa: E402

    rdb = ResearchDatabase.open()
    try:
        raw: Dict[str, List[Tuple[int, float]]] = {}
        for ccy in ("BTC", "ETH"):
            # +60d lookback so the first labels have a full trailing window
            rows = rdb.load_dvol_daily(ccy, start_ms - 60 * 86_400_000, end_ms)
            if not rows:
                raise RuntimeError(
                    f"dvol_daily has no {ccy} closes — run the bot's DVOL "
                    f"feed first; refusing to fabricate IV classifications"
                )
            raw[ccy] = rows
    finally:
        rdb.close()
    btc_iv = build_iv_percentile(raw["BTC"], DVOL_WINDOW_DAYS)
    eth_iv = build_iv_percentile(raw["ETH"], DVOL_WINDOW_DAYS)
    return {s: dvol_series_for(s, btc_iv, eth_iv) for s in symbols}, btc_iv


def iv_thresholds_family() -> Tuple[List[str], Callable[..., Dict[str, Any]], Callable[..., Any]]:
    """Wire the iv_thresholds family — post-hoc IV-gate cut sweep.

    Raw trades come from the regime-router harness (``run_strategy``: full
    production gate chain, router OFF — the same raw-trade convention as the
    regime-router and iv_high_only A/Bs). The gate itself is applied post-hoc
    per trade exactly like ``iv_high_only_ab_split`` and the production shadow
    decision: keep iff trailing-30d DVOL percentile > cut. SOL/HYPE inherit
    the BTC index (global proxy rule).

    Imports inside this function so ``--selftest`` never pulls the strategies.
    """
    from scripts.iv_high_only_ab_split import SPECS  # noqa: E402
    from scripts.regime_router_a_b_test import ms, run_strategy  # noqa: E402
    from src.data.database import Database  # noqa: E402
    from src.data.dvol_feed import iv_pct_at  # noqa: E402
    from src.utils.config import load_config  # noqa: E402

    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(str(cfg.get("database.path", "data/live/bot.db")))
    spec_names = [name for name, _cls, _path in SPECS]

    def tag_for(cut: Optional[float]) -> str:
        return "no gate (baseline)" if cut is None else f"high_iv>{cut}"

    def run_one(start: str, end: str, symbols: List[str],
                cut: Optional[float]) -> Dict[str, Any]:
        try:
            s_ms, e_ms = ms(start), ms(end, True)
            key = (start, end, tuple(symbols))
            raw = _IV_RAW_CACHE.get(key)
            if raw is None:
                trades: List[Dict[str, Any]] = []
                for name, cls, path in SPECS:
                    for t in run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols):
                        t["_strategy"] = name
                        trades.append(t)
                iv_by_sym, btc_iv = _load_dvol_series(symbols, s_ms, e_ms)
                for t in trades:
                    series = iv_by_sym.get(str(t.get("symbol")), btc_iv)
                    t["_iv_pct"] = iv_pct_at(series, int(t.get("entry_time") or 0))
                _IV_RAW_CACHE[key] = trades
                raw = trades
            kept, blocked = apply_iv_gate(raw, cut)
            return iv_cell_from_raw(raw, kept, blocked, cut, spec_names)
        except Exception as exc:  # noqa: BLE001 — a failed cell must not kill the sweep
            return {"error": f"{type(exc).__name__}: {exc}", "iv_cut": cut}

    tags = [tag_for(c) for c in IV_THRESHOLDS_GRID]
    return tags, run_one, cfg


FAMILIES = {
    "flush_fade": flush_fade_family,
    "vwap_thresholds": vwap_thresholds_family,
    "iv_thresholds": iv_thresholds_family,
}


# ---------------------------------------------------------------------------
# Sweep, artifacts, ledger
# ---------------------------------------------------------------------------

def select_cells(grid_len: int, spec: Optional[str]) -> List[int]:
    """Parse a --cells spec ("0,1,2") into validated grid indices.

    Index 0 is the baseline and is ALWAYS included; a cap session lists
    baseline + the selected variants. Returns a sorted, de-duplicated list.
    """
    if spec is None or not spec.strip():
        return list(range(grid_len))
    idx = sorted({int(x) for x in spec.split(",") if x.strip()})
    if not idx:
        raise ValueError("empty --cells spec")
    for i in idx:
        if not 0 <= i < grid_len:
            raise ValueError(f"cell index {i} out of range 0..{grid_len - 1}")
    if 0 not in idx:
        idx = [0] + idx  # baseline is not optional
    return idx


def sweep(family: str, start: str, end: str, symbols: List[str],
          split_days: int = 30, max_windows: Optional[int] = None,
          cell_spec: Optional[str] = None,
          log=None) -> Dict[str, Any]:
    """Run baseline + the selected grid cells across ALL non-overlapping windows."""
    from scripts.regime_router_a_b_test import split_windows  # noqa: E402

    tags_all, run_one, _cfg = FAMILIES[family]()
    sel = select_cells(len(tags_all), cell_spec)
    tags = [tags_all[i] for i in sel]
    if family == "flush_fade":
        grid_params: List[Any] = [FLUSH_FADE_GRID[i] for i in sel]
    elif family == "vwap_thresholds":
        grid_params = [VWAP_THRESHOLDS_GRID[i] for i in sel]
    elif family == "iv_thresholds":
        grid_params = [IV_THRESHOLDS_GRID[i] for i in sel]
    else:  # generic families: params parallel to tags via sel
        grid_params = list(sel)
    windows = split_windows(start, end, split_days)
    if max_windows:
        windows = windows[:max_windows]

    def sweep_tag(tag: str, params: Any) -> List[Dict[str, Any]]:
        cells: List[Dict[str, Any]] = []
        for w_start, w_end in windows:
            if log:
                log(f"  [{tag}] {w_start}..{w_end} ...")
            try:
                if isinstance(params, tuple):
                    delay, stopout = params
                    cell = run_one(w_start, w_end, symbols, delay, stopout)
                else:
                    cell = run_one(w_start, w_end, symbols, params)
            except Exception as exc:  # noqa: BLE001 — a failed cell must not kill the sweep
                cell = {"error": f"{type(exc).__name__}: {exc}"}
            cells.append(cell)
        return cells

    print(f"family={family} windows={len(windows)} cells={len(tags)} "
          f"span={start}..{end} split={split_days}d symbols={','.join(symbols)} "
          f"selection={tags}")

    baseline = sweep_tag(tags[0], grid_params[0])
    variants: Dict[str, List[Dict[str, Any]]] = {}
    for tag, params in zip(tags[1:], grid_params[1:]):
        variants[tag] = sweep_tag(tag, params)

    results: List[Dict[str, Any]] = []
    for tag, cells in variants.items():
        verdict, reasons = decide(baseline, cells, noise_model=True)
        noise = paired_bootstrap_noise_gate(baseline, cells)
        # Per-symbol slices — the SAME paired sign-flip test restricted to
        # each symbol. Advisory only: they never feed decide(); the
        # cell-level verdict above is the only promotion gate.
        trade_pnls_by_symbol(baseline)
        trade_pnls_by_symbol(cells)
        sym_universe = sorted(
            {s for m in [c.get("trade_pnls_by_symbol") for c in baseline + cells]
             if isinstance(m, dict) for s in m}
        )
        sym_gates = [symbol_noise_gate(baseline, cells, s) for s in sym_universe]
        results.append({
            "tag": tag,
            "windows": windows,
            "baseline_cells": baseline,
            "variant_cells": cells,
            "deltas": delta_per_window(baseline, cells),
            "aggregate_baseline": aggregate(baseline),
            "aggregate_variant": aggregate(cells),
            "noise_gate": noise,
            "symbol_gates": sym_gates,
            "verdict": verdict,
            "reasons": reasons,
            "baseline_tag": tags[0],
        })

    return {
        "family": family,
        "span": {"start": start, "end": end, "split_days": split_days},
        "symbols": symbols,
        "windows": windows,
        "baseline_tag": tags[0],
        "results": results,
    }


def write_artifact(session: Dict[str, Any]) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = ARTIFACT_DIR / f"{ts}_{session['family']}.json"
    path.write_text(json.dumps(session, indent=2, default=str), encoding="utf-8")
    return path


def experiment_block(result: Dict[str, Any], session: Dict[str, Any]) -> str:
    """One ledger block per variant — the audit line a human reads first."""
    span = session["span"]
    ab = result["aggregate_baseline"]
    av = result["aggregate_variant"]
    per_window = ", ".join(
        f"{w[0][:7]}({'+' if (d or 0) >= 0 else ''}{d if d is not None else 'n/e'})"
        for w, d in zip(result["windows"], result["deltas"])
    )
    hyp = {
        "stopout=OFF": "the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop",
        "delay=": "a confirmation delay avoids entering at the flush extreme",
        "z=2.5+": "per-symbol thresholds: HYPE trades later and thinner, so its fade plausibly needs a wider 3.0σ band; a single 2.5σ threshold treats all listings as the same animal",
        "z=3.0 all": "a uniformly stricter band trades less everywhere and filters low-quality extensions at the cost of missed valid ones",
        "high_iv>": "the high_iv regime concentrates both strategies' edge (IV_PERCENTILE_REGIME_GATE / IV_HIGH_ONLY_AB_SPLIT); sweeping the cut tests how much of the bleed the implicit-vol signal removes — 63.3/66.7/70 = lower tercile/canonical/strict",
    }
    hyp_txt = next((v for k, v in hyp.items() if result["tag"].startswith(k)
                    or k in result["tag"]), "parameter variant")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base_tag = result.get("baseline_tag") or session.get("baseline_tag", "")
    lines = [
        f"### {ts} — {session['family']}/{result['tag']} — {result['verdict']}",
        f"- hypothesis: {hyp_txt}",
        f"- windows: {span['start']}..{span['end']} "
        f"({len(result['windows'])} windows of {span['split_days']}d, non-overlapping)",
        f"- baseline ({base_tag}): net={ab['pnl']} n={ab['n']} PF={ab['pf']}",
        f"- variant: net={av['pnl']} n={av['n']} PF={av['pf']}",
        f"- per-window delta: {per_window}",
    ]
    sg = [g for g in (result.get("symbol_gates") or []) if g.get("evaluated")]
    if sg:
        lines.append(
            "- symbol slices (ADVISORY — the cell verdict is the only gate): "
            + "; ".join(
                f"{g['symbol']} delta={g['observed_delta']:+.2f} "
                f"(n={g['n_variant']}, p={g['p_value']}, "
                f"{'beyond' if g['pass'] else 'within'} sign-flip null)"
                for g in sg
            )
        )
    lines += [
        f"- reasons: {'; '.join(result['reasons'])}",
        f"- audit line: verdict DRAFTED by overnight_runner — advisory; "
        f"promotion only via shadow + watchdog recheck.",
        "",
    ]
    return "\n".join(lines)


def write_ledger(session: Dict[str, Any], artifact: Path) -> str:
    """Morning report on top + one block per experiment (append-only body)."""
    kept = [r for r in session["results"] if r["verdict"] == "KEEP"]
    incon = [r for r in session["results"] if r["verdict"] == "INCONCLUSIVE"]
    disc = [r for r in session["results"] if r["verdict"] == "DISCARD"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = [
        f"## Morning report — {ts} — family `{session['family']}`",
        "",
        f"- span: {session['span']['start']}..{session['span']['end']} "
        f"({len(session['windows'])} non-overlapping windows of "
        f"{session['span']['split_days']}d) · symbols: {', '.join(session['symbols'])}",
        f"- verdicts: {len(kept)} KEEP · {len(incon)} INCONCLUSIVE · {len(disc)} DISCARD",
        f"- artifact: `{artifact.relative_to(ROOT)}`",
    ]
    for r in kept:
        av = r["aggregate_variant"]
        report.append(
            f"- **KEEP → shadow candidate**: `{r['tag']}` net={av['pnl']} "
            f"n={av['n']} PF={av['pf']} — wire as shadow-only knob; watchdog "
            f"recheck at n>=30 before any enforcement decision."
        )
    for r in incon:
        av = r["aggregate_variant"]
        binding = r["reasons"][-1] if r["reasons"] else ""
        report.append(
            f"- INCONCLUSIVE: `{r['tag']}` net={av['pnl']} n={av['n']} "
            f"— {binding}"
        )
    report.append("")

    body = "\n".join(experiment_block(r, session) for r in session["results"])

    if LEDGER_PATH.exists():
        old = LEDGER_PATH.read_text(encoding="utf-8")
        # Strip a previous morning report (everything before the first real
        # '###' block) so the newest session lands on top and the history
        # stays blocks-only. Hunt AFTER the header's terminating '---': the
        # reference header embeds a worked example whose fenced lines also
        # begin with '### ' and must never be mistaken for a block start.
        term = old.find("\n---\n")
        searchable = old[term:] if term != -1 else old
        idx = searchable.find("\n### ")
        history = searchable[idx:] if idx != -1 else ""
    else:
        history = ""
    LEDGER_PATH.write_text(
        LEDGER_HEADER + "\n".join(report) + "\n---\n" + body + history,
        encoding="utf-8",
    )
    return str(LEDGER_PATH)


def selftest() -> int:
    """Canned-result validation of decide()/report logic — no engine."""
    base = [{"total_pnl_usd": -50.0, "n_trades": 20, "gross_win_usd": 30, "gross_loss_usd": 80},
            {"total_pnl_usd": -10.0, "n_trades": 15, "gross_win_usd": 40, "gross_loss_usd": 50}]
    good = [{"total_pnl_usd": 5.0, "n_trades": 22, "gross_win_usd": 60, "gross_loss_usd": 55},
            {"total_pnl_usd": 8.0, "n_trades": 18, "gross_win_usd": 70, "gross_loss_usd": 62}]
    v, r = decide(base, good)
    assert v == "KEEP", (v, r)

    # Noise model (research_program.md: KEEP must reject the paired
    # window-level null, not just print a positive delta). The exact
    # sign-flip floor: K=3 all-positive gives p=2^-3=0.125 > alpha=0.10
    # (cannot pass), so the pass case below uses K=4 (p=2^-4=0.0625).
    base4 = base + base
    good4 = good + good
    # collapse the duplicates' n so aggregates stay honest
    base4 = [dict(base[0]), base[1],
             {"total_pnl_usd": -20.0, "n_trades": 12,
              "gross_win_usd": 25, "gross_loss_usd": 45},
             {"total_pnl_usd": -5.0, "n_trades": 10,
              "gross_win_usd": 20, "gross_loss_usd": 25}]
    good4 = [good[0], good[1],
             {"total_pnl_usd": 15.0, "n_trades": 14,
              "gross_win_usd": 65, "gross_loss_usd": 50},
             {"total_pnl_usd": 12.0, "n_trades": 11,
              "gross_win_usd": 60, "gross_loss_usd": 48}]

    def pnl_list(c: Dict[str, Any], offset: float) -> List[float]:
        return [x * 10.0 + offset for x in range(int(c["n_trades"]))]

    base_t = [{**c, "trade_pnls": pnl_list(c, 0.0)} for c in base4]
    good_t = [{**c, "trade_pnls": pnl_list(c, 500.0)} for c in good4]
    v, r = decide(base_t, good_t, noise_model=True)
    assert v == "KEEP" and any("noise gate" in x and "p=0.0625" in x for x in r), (v, r)
    # One window pair where the variant LOSES to the baseline: p doubles to
    # 0.125 > alpha — the same edge no longer clears the noise gate.
    mixed = [dict(good_t[0], trade_pnls=pnl_list(good4[0], 500.0)),
             dict(good_t[1], trade_pnls=pnl_list(good4[1], 500.0)),
             dict(good_t[2], trade_pnls=pnl_list(good4[2], 500.0)),
             dict(good_t[3], trade_pnls=pnl_list(good4[3], -400.0))]
    v, r = decide(base_t, mixed, noise_model=True)
    assert v == "INCONCLUSIVE" and any("noise" in x for x in r), (v, r)
    legacy = [dict(c) for c in good]  # no trade_pnls — gate must skip, not crash
    v, r = decide(base, legacy, noise_model=True)
    assert v == "KEEP" and any("skipped" in x for x in r), (v, r)

    underpowered = [dict(c, n_trades=8) for c in good]
    v, _ = decide(base, underpowered)
    assert v == "INCONCLUSIVE", v

    catastro = [dict(good[0], total_pnl_usd=-150.0), good[1]]
    v, r = decide(base, catastro)
    assert v == "DISCARD" and any("catastrophic" in x for x in r), (v, r)

    failed = [{"error": "boom"}, good[1]]
    v, r = decide(base, failed)
    assert v == "INCONCLUSIVE" and any("valid window" in x for x in r), (v, r)

    mixed = [{"total_pnl_usd": 8.0, "n_trades": 35, "gross_win_usd": 60, "gross_loss_usd": 52},
             {"total_pnl_usd": -60.0, "n_trades": 35, "gross_win_usd": 100, "gross_loss_usd": 160}]
    v, _ = decide(base, mixed)
    assert v == "DISCARD", v  # 1/2 improved, PF<1 — no majority

    session = {"family": "flush_fade", "span": {"start": "2026-05-18", "end": "2026-08-07",
               "split_days": 30}, "symbols": ["BTC", "ETH"],
               "windows": [("2026-05-18", "2026-06-16"), ("2026-06-17", "2026-07-16")],
               "baseline_tag": "delay=0 stopout=ON"}
    result = {"tag": "delay=0 stopout=OFF", "windows": session["windows"],
              "baseline_cells": base, "variant_cells": good,
              "deltas": delta_per_window(base, good),
              "aggregate_baseline": aggregate(base), "aggregate_variant": aggregate(good),
              "verdict": "KEEP", "reasons": ["test"]}
    blk = experiment_block(result, session)
    assert "KEEP" in blk and "audit line" in blk and "per-window delta" in blk
    print("selftest OK:", v, "|", "; ".join(r))
    return 0


def summarize(session: Dict[str, Any], artifact: Path, ledger: Path) -> str:
    """One compact line per variant — the cron log / notifier surface.

    The runner's normal output block stays human-first; this is the compact
    echo the nightly wrapper captures into its status file (and the only
    thing a scheduled task needs to read at a glance).
    """
    lines = [f"overnight: family={session['family']} "
             f"span={session['span']['start']}..{session['span']['end']} "
             f"windows={len(session['windows'])}K",
             f"ledger: {ledger}",
             f"artifact: {artifact}"]
    for r in session["results"]:
        av, ab = r["aggregate_variant"], r["aggregate_baseline"]
        lines.append(
            f"  {r['tag']}: net={av['pnl']:.2f} n={av['n']} "
            f"PF={av['pf']:.3f} vs base {ab['pnl']:.2f} -> {r['verdict']}"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", choices=sorted(FAMILIES), default=None)
    ap.add_argument("--start", default="2026-05-18")
    ap.add_argument("--end", default="2026-08-07")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--split-days", type=int, default=30)
    ap.add_argument("--max-windows", type=int, default=None,
                    help="cap windows (smoke runs); default = all in span")
    ap.add_argument("--cells", default=None,
                    help="comma-separated grid indices to run; 0 is the "
                         "baseline and is always included (default: all)")
    ap.add_argument("--selftest", action="store_true",
                    help="validate verdict/report logic on canned results, no backtests")
    ap.add_argument("--summary", action="store_true",
                    help="compact one-line-per-variant output (cron/scheduled use)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.family:
        ap.error("--family is required (or use --selftest)")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    session = sweep(args.family, args.start, args.end, symbols,
                    split_days=args.split_days, max_windows=args.max_windows,
                    cell_spec=args.cells,
                    log=lambda m: print(m, flush=True))
    artifact = write_artifact(session)
    ledger = write_ledger(session, artifact)

    if args.summary:
        print(summarize(session, artifact, ledger))
        return 0

    print("\n" + "=" * 78)
    for r in session["results"]:
        av, ab = r["aggregate_variant"], r["aggregate_baseline"]
        print(f"  {r['tag']:32} net={av['pnl']:>9.2f} n={av['n']:>3} "
              f"PF={av['pf']:.2f} | baseline net={ab['pnl']:>9.2f} -> {r['verdict']}")
    print("=" * 78)
    print(f"artifact: {artifact}")
    print(f"ledger  : {ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
