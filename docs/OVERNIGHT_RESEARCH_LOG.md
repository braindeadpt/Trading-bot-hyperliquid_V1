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
`trades_summary`, `trade_pnls` (per-window paired noise gate needs these),
`manifest`, and the `noise_gate` dict per variant (p_value, alpha, method,
per-window deltas).

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
## Morning report — 2026-09-09 16:15 UTC — family `flush_fade`

- span: 2026-08-09..2026-09-09 (4 non-overlapping windows of 10d) · symbols: BTC, ETH
- verdicts: 0 KEEP · 0 INCONCLUSIVE · 3 DISCARD
- artifact: `data\research\overnight_experiments\20260909_121824_flush_fade.json`

---
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
