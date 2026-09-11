# Overnight research ledger — append-only

One block per experiment: hypothesis, window set, baseline vs variant per
window, verdict, and the one sentence a human needs to audit the decision.
Morning report (latest session) on top. Verdicts are DRAFTED here — advisory
evidence only; promotion runs through shadow + watchdog recheck with a human
reading the dashboard (research_program.md: the agent proposes, the gates
judge, the human enforces).

This preamble is the reference for the loop's two contracts (queue format
and verdict schema) plus one worked example from a real session. It is
rewritten verbatim on every session by the runner — edit it in
`scripts/research/overnight_runner.py` (LEDGER_HEADER), never here.

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
| **DISCARD** | catastrophic window ∨ no majority ∨ PF≤1 — including the case "improved everywhere but still loses money": less bad than the baseline is not an edge. Checked **before** the n gate: a negative result does not need n≥30 to be negative, so a small-n DISCARD is still DISCARD, never parked as INCONCLUSIVE |
| **INCONCLUSIVE** | only reached once the result is otherwise positive (majority improved ∧ PF>1 ∧ no catastrophic window): fewer than 2 valid windows (a majority of one is not multi-window evidence) ∨ n<30 ∨ noise gate failed (paired delta not beyond the window sign-flip null) — parked with the evidence bar attached. n≥30 is a **KEEP** precondition, never a DISCARD precondition |
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
## Morning report — 2026-09-11 09:01 UTC — family `hype_vwap_refine`

- span: 2026-03-13..2026-09-08 (6 non-overlapping windows of 30d) · symbols: HYPE, BTC, ETH
- verdicts: 0 KEEP · 0 INCONCLUSIVE · 2 DISCARD
- artifact: `data\research\overnight_experiments\20260911_090104_hype_vwap_refine.json`

---
### 2026-09-11 09:01 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 09:01 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 08:01 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 08:01 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 07:01 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 07:01 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 06:01 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 06:01 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 05:03 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 05:03 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 04:03 UTC — hype_vwap_refine/HYPE z_threshold=3.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-406.32 n=370 PF=0.827
- per-window delta: 2026-03(-22.68), 2026-04(+98.64), 2026-05(+89.2), 2026-06(-42.31), 2026-07(+27.07), 2026-08(+7.28)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+157.21 (n=51, p=0.1719, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=370 (gate >=30); aggregate PF=0.827 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-11 04:03 UTC — hype_vwap_refine/HYPE z_threshold=4.0 — DISCARD
- hypothesis: parameter variant
- windows: 2026-03-13..2026-09-08 (6 windows of 30d, non-overlapping)
- baseline (baseline (production 2.5σ)): net=-563.52 n=454 PF=0.821
- variant: net=-432.31 n=348 PF=0.804
- per-window delta: 2026-03(-51.45), 2026-04(+73.65), 2026-05(+160.72), 2026-06(-116.04), 2026-07(+50.15), 2026-08(+14.18)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=163, p=1.0, within sign-flip null); ETH delta=+0.00 (n=156, p=1.0, within sign-flip null); HYPE delta=+131.20 (n=29, p=0.2969, within sign-flip null)
- reasons: windows improved 4/6 (majority=yes); aggregate n=348 (gate >=30); aggregate PF=0.804 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:13 UTC — vwap_deceleration/decel_retrace_0.15 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline): net=-151.09 n=251 PF=0.9
- variant: net=-181.86 n=166 PF=0.83
- per-window delta: 2026-05(-3.13), 2026-06(-141.31), 2026-07(+18.79), 2026-08(+94.88)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-8.82 (n=66, p=0.6875, within sign-flip null); ETH delta=-15.30 (n=61, p=0.625, within sign-flip null); HYPE delta=-6.71 (n=39, p=0.625, within sign-flip null)
- reasons: windows improved 2/4 (majority=no); aggregate n=166 (gate >=30); aggregate PF=0.83 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:13 UTC — vwap_deceleration/decel_retrace_0.30 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline): net=-151.09 n=251 PF=0.9
- variant: net=-243.24 n=148 PF=0.748
- per-window delta: 2026-05(-34.76), 2026-06(-107.81), 2026-07(+5.47), 2026-08(+44.95)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-50.31 (n=59, p=0.625, within sign-flip null); ETH delta=+21.86 (n=53, p=0.4375, within sign-flip null); HYPE delta=-63.79 (n=36, p=0.875, within sign-flip null)
- reasons: windows improved 2/4 (majority=no); aggregate n=148 (gate >=30); aggregate PF=0.748 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:13 UTC — vwap_deceleration/decel_counter_close — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline): net=-151.09 n=251 PF=0.9
- variant: net=-311.11 n=148 PF=0.685
- per-window delta: 2026-05(-64.04), 2026-06(-197.8), 2026-07(+30.26), 2026-08(+71.56)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-31.01 (n=59, p=0.6875, within sign-flip null); ETH delta=-68.14 (n=56, p=0.6875, within sign-flip null); HYPE delta=-60.90 (n=33, p=0.75, within sign-flip null)
- reasons: windows improved 2/4 (majority=no); aggregate n=148 (gate >=30); aggregate PF=0.685 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exhaustion/no_vol_filter — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod surge=1.5)): net=-151.09 n=251 PF=0.9
- variant: net=-368.9 n=419 PF=0.844
- per-window delta: 2026-05(-194.77), 2026-06(-34.99), 2026-07(+197.51), 2026-08(-185.56)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-62.52 (n=164, p=0.75, within sign-flip null); ETH delta=-130.14 (n=154, p=0.8125, within sign-flip null); HYPE delta=-25.15 (n=101, p=0.625, within sign-flip null)
- reasons: windows improved 1/4 (majority=no); aggregate n=419 (gate >=30); aggregate PF=0.844 (gate >1.0); catastrophic window: worst variant -368.36 vs baseline worst -182.80 (>2.0x)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exhaustion/exhaust_decay — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod surge=1.5)): net=-151.09 n=251 PF=0.9
- variant: net=-242.26 n=234 PF=0.833
- per-window delta: 2026-05(-33.2), 2026-06(-225.75), 2026-07(+74.4), 2026-08(+93.38)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-192.04 (n=86, p=0.8125, within sign-flip null); ETH delta=+33.18 (n=81, p=0.4375, within sign-flip null); HYPE delta=+67.68 (n=67, p=0.5, within sign-flip null)
- reasons: windows improved 2/4 (majority=no); aggregate n=234 (gate >=30); aggregate PF=0.833 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exhaustion/exhaust_below_mean — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod surge=1.5)): net=-151.09 n=251 PF=0.9
- variant: net=-82.48 n=168 PF=0.893
- per-window delta: 2026-05(-33.2), 2026-06(-80.02), 2026-07(+34.6), 2026-08(+147.23)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-0.30 (n=61, p=0.5625, within sign-flip null); ETH delta=+33.15 (n=60, p=0.4375, within sign-flip null); HYPE delta=+35.67 (n=47, p=0.125, within sign-flip null)
- reasons: windows improved 2/4 (majority=no); aggregate n=168 (gate >=30); aggregate PF=0.893 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exit_econ/exit_z_threshold=0.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod exits)): net=-151.09 n=251 PF=0.9
- variant: net=-121.51 n=251 PF=0.919
- per-window delta: 2026-05(+15.83), 2026-06(+3.92), 2026-07(-3.46), 2026-08(+13.29)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+5.78 (n=102, p=0.125, within sign-flip null); ETH delta=+10.93 (n=99, p=0.5, within sign-flip null); HYPE delta=+12.82 (n=50, p=0.5, within sign-flip null)
- reasons: windows improved 3/4 (majority=yes); aggregate n=251 (gate >=30); aggregate PF=0.919 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exit_econ/exit_z_threshold=0.15 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod exits)): net=-151.09 n=251 PF=0.9
- variant: net=-167.31 n=247 PF=0.888
- per-window delta: 2026-05(+0.26), 2026-06(+5.32), 2026-07(+4.44), 2026-08(-26.24)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+16.71 (n=101, p=0.25, within sign-flip null); ETH delta=-54.41 (n=96, p=1.0, within sign-flip null); HYPE delta=+21.41 (n=50, p=0.5, within sign-flip null)
- reasons: windows improved 3/4 (majority=yes); aggregate n=247 (gate >=30); aggregate PF=0.888 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exit_econ/take_profit_r_multiple=1.5 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod exits)): net=-151.09 n=251 PF=0.9
- variant: net=-151.09 n=251 PF=0.9
- per-window delta: 2026-05(+0.0), 2026-06(+0.0), 2026-07(+0.0), 2026-08(+0.0)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=+0.00 (n=102, p=1.0, within sign-flip null); ETH delta=+0.00 (n=99, p=1.0, within sign-flip null); HYPE delta=+0.00 (n=50, p=1.0, within sign-flip null)
- reasons: windows improved 0/4 (majority=no); aggregate n=251 (gate >=30); aggregate PF=0.9 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-10 23:12 UTC — vwap_exit_econ/max_hold_hours=2 — DISCARD
- hypothesis: parameter variant
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (baseline (prod exits)): net=-151.09 n=251 PF=0.9
- variant: net=-430.16 n=280 PF=0.699
- per-window delta: 2026-05(+14.94), 2026-06(-199.35), 2026-07(-22.99), 2026-08(-71.67)
- symbol slices (ADVISORY — the cell verdict is the only gate): BTC delta=-116.38 (n=107, p=0.875, within sign-flip null); ETH delta=-105.86 (n=108, p=0.75, within sign-flip null); HYPE delta=-56.90 (n=65, p=0.75, within sign-flip null)
- reasons: windows improved 1/4 (majority=no); aggregate n=280 (gate >=30); aggregate PF=0.699 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 20:17 UTC — iv_thresholds/high_iv>63.3 — INCONCLUSIVE
- hypothesis: the high_iv regime concentrates both strategies' edge (IV_PERCENTILE_REGIME_GATE / IV_HIGH_ONLY_AB_SPLIT); sweeping the cut tests how much of the bleed the implicit-vol signal removes — 63.3/66.7/70 = lower tercile/canonical/strict
- windows: 2026-06-14..2026-09-08 (3 windows of 30d, non-overlapping)
- baseline (no gate (baseline)): net=-162.75 n=174 PF=0.544
- variant: net=-42.64 n=28 PF=0.239
- per-window delta: 2026-06(+11.47), 2026-07(+95.33), 2026-08(+13.31)
- reasons: windows improved 3/3 (majority=yes); aggregate n=28 (gate >=30); aggregate PF=0.239 (gate >1.0); n<30 — park with evidence bar attached (IV-gate n=13 precedent)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 20:17 UTC — iv_thresholds/high_iv>66.7 — INCONCLUSIVE
- hypothesis: the high_iv regime concentrates both strategies' edge (IV_PERCENTILE_REGIME_GATE / IV_HIGH_ONLY_AB_SPLIT); sweeping the cut tests how much of the bleed the implicit-vol signal removes — 63.3/66.7/70 = lower tercile/canonical/strict
- windows: 2026-06-14..2026-09-08 (3 windows of 30d, non-overlapping)
- baseline (no gate (baseline)): net=-162.75 n=174 PF=0.544
- variant: net=-32.7 n=24 PF=0.29
- per-window delta: 2026-06(+11.47), 2026-07(+102.21), 2026-08(+16.37)
- reasons: windows improved 3/3 (majority=yes); aggregate n=24 (gate >=30); aggregate PF=0.29 (gate >1.0); n<30 — park with evidence bar attached (IV-gate n=13 precedent)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 20:17 UTC — iv_thresholds/high_iv>70.0 — INCONCLUSIVE
- hypothesis: the high_iv regime concentrates both strategies' edge (IV_PERCENTILE_REGIME_GATE / IV_HIGH_ONLY_AB_SPLIT); sweeping the cut tests how much of the bleed the implicit-vol signal removes — 63.3/66.7/70 = lower tercile/canonical/strict
- windows: 2026-06-14..2026-09-08 (3 windows of 30d, non-overlapping)
- baseline (no gate (baseline)): net=-162.75 n=174 PF=0.544
- variant: net=-25.64 n=17 PF=0.325
- per-window delta: 2026-06(+11.47), 2026-07(+102.21), 2026-08(+23.43)
- reasons: windows improved 3/3 (majority=yes); aggregate n=17 (gate >=30); aggregate PF=0.325 (gate >1.0); n<30 — park with evidence bar attached (IV-gate n=13 precedent)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 17:30 UTC — vwap_thresholds/z=2.5+HYPE:3.0 — DISCARD
- hypothesis: per-symbol thresholds: HYPE trades later and thinner, so its fade plausibly needs a wider 3.0σ band; a single 2.5σ threshold treats all listings as the same animal
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (z=2.5 (baseline)): net=-151.09 n=251 PF=0.9
- variant: net=-37.27 n=240 PF=0.973
- per-window delta: 2026-05(+0.0), 2026-06(+137.82), 2026-07(-14.48), 2026-08(-9.52)
- reasons: windows improved 1/4 (majority=no); aggregate n=240 (gate >=30); aggregate PF=0.973 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 17:30 UTC — vwap_thresholds/z=3.0 all — DISCARD
- hypothesis: a uniformly stricter band trades less everywhere and filters low-quality extensions at the cost of missed valid ones
- windows: 2026-05-18..2026-09-08 (4 windows of 30d, non-overlapping)
- baseline (z=2.5 (baseline)): net=-151.09 n=251 PF=0.9
- variant: net=-119.02 n=184 PF=0.892
- per-window delta: 2026-05(-5.91), 2026-06(+59.43), 2026-07(-0.14), 2026-08(-21.31)
- reasons: windows improved 1/4 (majority=no); aggregate n=184 (gate >=30); aggregate PF=0.892 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 16:15 UTC — flush_fade/delay=0 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-325.67 n=54 PF=0.306
- per-window delta: 2026-08(+61.64), 2026-08(+0.0), 2026-08(+77.69), 2026-09(+6.44)
- reasons: windows improved 3/4 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.306 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 16:15 UTC — flush_fade/delay=1 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-287.04 n=54 PF=0.337
- per-window delta: 2026-08(+49.84), 2026-08(+0.0), 2026-08(+108.66), 2026-09(+25.9)
- reasons: windows improved 3/4 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.337 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 16:15 UTC — flush_fade/delay=5 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-412.42 n=55 PF=0.231
- per-window delta: 2026-08(+17.95), 2026-08(+0.0), 2026-08(+33.68), 2026-09(+7.39)
- reasons: windows improved 3/4 (majority=yes); aggregate n=55 (gate >=30); aggregate PF=0.231 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 15:25 UTC — flush_fade/delay=0 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-325.67 n=54 PF=0.306
- per-window delta: 2026-08(+61.64), 2026-08(+0.0), 2026-08(+77.69), 2026-09(+6.44)
- reasons: windows improved 3/4 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.306 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 15:25 UTC — flush_fade/delay=1 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-287.04 n=54 PF=0.337
- per-window delta: 2026-08(+49.84), 2026-08(+0.0), 2026-08(+108.66), 2026-09(+25.9)
- reasons: windows improved 3/4 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.337 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 15:25 UTC — flush_fade/delay=5 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (delay=0 stopout=ON): net=-471.44 n=54 PF=0.042
- variant: net=-412.42 n=55 PF=0.231
- per-window delta: 2026-08(+17.95), 2026-08(+0.0), 2026-08(+33.68), 2026-09(+7.39)
- reasons: windows improved 3/4 (majority=yes); aggregate n=55 (gate >=30); aggregate PF=0.231 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 12:23 UTC — flush_fade/delay=0 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (): net=-471.44 n=54 PF=0.042
- variant: net=-325.67 n=54 PF=0.306
- per-window delta: 2026-08(+61.64), 2026-08(+None), 2026-08(+77.69), 2026-09(+6.44)
- reasons: excluded 1 no-evidence window(s) (both cells n=0 — absence of data, not improvement); windows improved 3/3 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.306 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 12:23 UTC — flush_fade/delay=1 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (): net=-471.44 n=54 PF=0.042
- variant: net=-287.04 n=54 PF=0.337
- per-window delta: 2026-08(+49.84), 2026-08(+None), 2026-08(+108.66), 2026-09(+25.9)
- reasons: excluded 1 no-evidence window(s) (both cells n=0 — absence of data, not improvement); windows improved 3/3 (majority=yes); aggregate n=54 (gate >=30); aggregate PF=0.337 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

### 2026-09-09 12:23 UTC — flush_fade/delay=5 stopout=OFF — DISCARD
- hypothesis: the fade needs the flush to revert; the stop-out exits on the same window that generated the signal — bypassing it removes the loop
- windows: 2026-08-09..2026-09-09 (4 windows of 10d, non-overlapping)
- baseline (): net=-471.44 n=54 PF=0.042
- variant: net=-412.42 n=55 PF=0.231
- per-window delta: 2026-08(+17.95), 2026-08(+None), 2026-08(+33.68), 2026-09(+7.39)
- reasons: excluded 1 no-evidence window(s) (both cells n=0 — absence of data, not improvement); windows improved 3/3 (majority=yes); aggregate n=55 (gate >=30); aggregate PF=0.231 (gate >1.0)
- audit line: verdict DRAFTED by overnight_runner — advisory; promotion only via shadow + watchdog recheck.

---
### 2026-09-10 — CORRECTION to Night 3 (`iv_thresholds`, 2026-09-09 20:17 UTC)

`decide()` had a gate-ordering bug: `n < N_GATE` was checked BEFORE the
majority/PF/catastrophic checks, so a negative result with small n was
parked as INCONCLUSIVE instead of DISCARDed. The three `iv_thresholds`
verdicts above were all decided under that buggy ordering. The fix makes
`n >= 30` a precondition for **KEEP only**, never a precondition for
DISCARD (research_program.md, `scripts/research/overnight_runner.py::decide`).

Re-applying the corrected rule to the numbers already on record above
(unchanged — this is a re-read, not a re-run):

- `high_iv>63.3`: PF=0.239 (n=28) — PF<=1 → **DISCARD**, not INCONCLUSIVE.
- `high_iv>66.7`: PF=0.29 (n=24) — PF<=1 → **DISCARD**, not INCONCLUSIVE.
- `high_iv>70.0`: PF=0.325 (n=17) — PF<=1 → **DISCARD**, not INCONCLUSIVE.

All three windows improved on the baseline (majority=yes in each block
above), but the aggregate variant still loses money in every cut. This is
exactly the "improved everywhere but still loses money" DISCARD case named
in research_program.md: the IV gate reduces the bleed but does not create
an edge. Under the old ordering these looked like "park with the bar
attached" (n<30, direction noted) — under the corrected ordering a
losing PF never earns that benefit of the doubt, however small n is.

This is a correction note only — the original blocks above are NOT
edited (the ledger is append-only and a test enforces that the historical
text survives verbatim).
- audit line: correction DRAFTED by manual audit review — advisory; the
  original session's raw JSON artifact is unchanged.
