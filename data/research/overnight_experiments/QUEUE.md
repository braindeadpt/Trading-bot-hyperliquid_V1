# Overnight queue — items testable under research_program.md rules

Curated from `docs/RESEARCH_BACKLOG.md` against the data actually on disk as
of **2026-09-09**. One entry per experiment family: hypothesis, harness
(CLI/config-dict surface only — no strategy-code edits), fixed window set,
and the evidence bar the verdict must clear. The runner
(`scripts/research/overnight_runner.py`) executes families in queue order.

**Registry cleanup 2026-09-10:** TrendFollow, MeanReversion, DonchianBreakout,
FundingArbitrage, CVDOrderFlow(+P90), RangeGrid, TrendPyramid, SFPReversion and
VARejection were removed from `_STRATEGY_REGISTRY` (verdicts in
`docs/RESEARCH_BACKLOG.md`). They no longer instantiate — do NOT wire queue
entries or families for retired names; class files remain importable for
research harnesses that reference them directly.

**Window-set guarantee (K=4 floor, enforced per entry):** every READY entry
must declare a window set yielding **at least 4 non-overlapping windows** —
the exact paired sign-flip noise gate cannot pass at K=3 (all-positive
floors at p=2^-3 = 12.5% > alpha=0.10), so a K<4 session can only ever
print INCONCLUSIVE/DISCARD. Entries whose data coverage cannot produce 4
valid windows yet are **BLOCKED with an explicit reopen date**. A data gap
that kills a window mid-session is not a planning failure: the no-evidence
rule demotes the verdict honestly (Night 1 precedent). A K=4 window set is
necessary but not sufficient — the aggregate n>=30 gate still binds.

**Status 2026-09-09: Q6 is READY and fully runnable** (6 windows, both DBs
on disk — research through 07-10, live after) and sits FIRST in the queue:
the nightly wrapper runs the first READY section, so tonight's run is the
Q6 session A (18 runs). **Night 3 is READY (coverage-gated) and comes
after:** the wrapper attempts it nightly, the runner BLOCKs with the
coverage reason until DVOL spans >= ~115d (~2026-10-08), then the K=4
reopen session runs automatically and compares against the accumulator
baseline (net −162.75).

## Data facts measured today (2026-09-09)

| Fact | Value | Consequence |
|---|---|---|
| Real liquidation feed (okx+bybit) | 46,891 events 2026-08-09..2026-09-09, but **one gap 08-19..08-28 (0 events)** — 20 effective days | flush recheck trigger (30d) **NOT YET MET on continuous coverage** — the watchdog supervisor fires it; the overnight runner must not duplicate it (different job: shadow-live comparison, not grid) |
| DVOL (Deribit, E: research DB) | BTC/ETH daily closes **2026-06-14..09-09** — 88 days each, 176 rows | Night 3 **BLOCKED** until ~2026-10-08 (K=4 floor unreachable before) |
| Predicted funding, last 30d | mean \|predicted\| = 0.000087; 1 day ≥ 0.0005 | SpotPerpCarry reopen gate **NOT MET** — measured, do not re-litigate |
| Candles for backtests | live `bot.db` 1m has 556k rows; research DB candle tables are empty | families must assemble the backtest DB from the live DB (the flush harness already does) |
| L2 books (E:) | stale since ~08-10 (bot idle) | maker-fill / zone-approach work remains blocked |

## Night 1 — flush_fade grid — CLOSED (2026-09-09)

**Result: 3× DISCARD** (`delay=0/1/5 stopout=OFF`; artifact
`20260909_121824_flush_fade.json`). Every variant improved most windows but
every PF<1 — improved-but-still-losing is not an edge. Window reality: 10d
windows over 08-09..09-09; W2 (08-19..08-28) had ZERO real liquidation
events (feed gap, bot idle) → excluded as no-evidence by the runner.
**K=3 valid windows is structural for this family on this data — and at
K=3 the noise gate mathematically cannot pass.** Any future flush_fade
confirmation needs 4 clean windows first (prerequisite: ~30d continuous
feed collection, tracked by the watchdog supervisor, not this queue).

- **Hypothesis (fixed):** the stop-out bypass alone (`delay=0 · OFF`) removes
  the exit loop; every confirmation delay dilutes that edge.
- **Harness:** `python scripts/research/overnight_runner.py --family flush_fade`
- **Window set:** 2026-08-09..2026-09-09, split 10d → 4 non-overlapping
  windows (3 valid after the feed gap).
- **Budget actual:** 4 cells × 4 windows = **16 runs OK** (the original
  "6 × 4 = 18" was an arithmetic error — the 6-cell grid would have cost 24
  and breached the cap; the session ran baseline + 3 variants).

## Night 2 — VWAP per-symbol thresholds (backlog §1.3) — CLOSED (2026-09-09)

**Result: both variants DISCARD** (2 DISCARD · 0 KEEP; artifact
`20260909_173039_vwap_thresholds.json`). Variant as a whole improved 1/4
windows with PF<1 — the gate is right. Per-symbol forensics (the real
finding): HYPE baseline 2.5σ n=50 pnl=−108.49 → 3.0σ n=39 pnl=+5.34
(−2.17→+0.14 per trade), but the improvement concentrates in W2 (+137.82);
W3/W4 stay slightly negative. "Less bad" is not an edge; a HYPE-only
deeper dive belongs in the backlog, not the queue.

- **Hypothesis:** a single 2.5σ threshold treats BTC and HYPE as the same
  animal; HYPE plausibly needs ≥3σ.
- **Harness:** new family `vwap_thresholds` (config-dict override of
  `strategy.vwap_deviation` per symbol — allowed surface). Wired and run.
- **Window set:** 2026-05-18..2026-09-08, split 30d → 4 non-overlapping
  windows (K=4 floor met by construction).
- **Budget:** 3 × 4 = **12 runs OK**.

## Q6 — HYPE-only VWAP refinement (§1.3 phase 2) — (READY, wired as `hype_vwap_refine`)

Grid 4 cells preregistered — baseline 2.5σ / `z_threshold: 3.5` /
`z_threshold: 4.0` / `volume_surge: 2.0` (z stays 2.5) — HYPE-only
overrides, BTC/ETH untouched (control in every cell).

- **Harness:** `python scripts/research/overnight_runner.py --family hype_vwap_refine --start 2026-03-13 --end 2026-09-08 --symbols HYPE,BTC,ETH --cells 0,1,2`
- **Window set: 2026-03-13..2026-09-08 split 30d → 6 non-overlapping windows.** W1–W4 read the E: research DB (HYPE 15m+1h from 2026-01-11, ~100% coverage in every planned window; that DB ends exactly 2026-07-10, so W4 seals the seam); W5–W6 read the live `bot.db` — one source per window, preregistered, never mixed mid-window. K=6 makes the sign-flip gate passable (floor p=1/64=0.0156) and aggregate n≥30 clears at every tested parameter (projected n≈45–100).
- **Budget: two preregistered sessions** — A `--cells 0,1,2` (18 runs) = the harness line above; after A lands, repoint the line to B `--cells 0,3` (12 runs); both ≤20, no third session.
- **Kill rule:** no variant improves ≥4/6 windows → §1.3 thin-book hypothesis is dead for this regime, HYPE moves to exclude-candidates, no further threshold iteration (fixed +1σ/+2σ steps and single vs2.0 value prevent grid-shopping).
- **Full design, gate math and the W3/W4-regression warning:** `docs/RESEARCH_BACKLOG.md` → "§1.3 phase 2".

## Night 3 — IV threshold sweep — (READY, coverage-gated; accumulator run done 2026-09-09, reopen auto ~2026-10-08)

- **Why blocked:** the DVOL feed starts **2026-06-14**. The historical
  05-18..09-08 span predates the feed; clamping to 06-14..09-09 yields only
  **3 windows of 30d** — a K=3 session cannot KEEP by construction (p floors
  at 2^-3 = 12.5% > alpha=0.10). Reopen when DVOL spans ≥ ~115d
  (**~ 2026-10-08**), which also gives W1's IV-percentile labels ≥60d of
  history instead of a cold percentile.
- **Optional accumulator run before then:** permitted only on an
  otherwise-empty night, pre-declared expectation INCONCLUSIVE (n was 13
  over 80d); the verdict is K-capped at INCONCLUSIVE. The default is to wait.
- **Harness:** `python scripts/research/overnight_runner.py --family iv_thresholds --start dvol --end dvol --symbols BTC,ETH,SOL,HYPE --cells 0,1,2` (machine-runnable, wired 2026-09-09 — the span is resolved at run time from the persisted `dvol_daily` coverage, no manual date arithmetic at reopen; the runner enforces the K=4 floor itself and prints `-> BLOCKED (coverage …)` while the feed is still short. Grid {63.3, 66.7, 70}: cells 1..3 vs baseline 0.)
- **Window set:** DVOL coverage (starts 2026-06-14), 30d split — K=4 needs
  coverage >= ~115d.
- **Budget when reopened:** 3 × 4 = **12 runs OK** (3×5=15 still within
  the 20-run cap if coverage passes 120d).

**Accumulator session 2026-09-09 (family `iv_thresholds`, 4 cells × 3 windows,
pre-declared INCONCLUSIVE expectation):** 3× INCONCLUSIVE exactly as the K=3
math requires (sign-flip floor p=0.125 > alpha; artifact
`20260909_201701_iv_thresholds.json`). Direction, honest: the gate prunes
most of the bleed — baseline net=−162.75 (n=174, PF 0.54) → kept net=−42.64
(n=28) @63.3 / −32.70 (n=24) @66.7 / −25.64 (n=17) @70, improved 3/3 windows,
every PF still <1. "Less bad" again — the kept slice still loses. W1 caveat
recorded by the family: n_no_iv=20/41 (cold trailing percentile before the
feed had 30d of history) — those trades are blocked, never silently kept.
Reopen gate unchanged: DVOL ≥ ~115d (~2026-10-08) for K=4; the reopen session
compares the cuts against THIS baseline.

## Queued — wired families run when their night comes (Q4/Q5/Q7 wired 2026-09-10)

Window guarantee on wiring: the same 05-18..09-08 30d split yields **4
non-overlapping windows** OK (K=4 floor met by construction; data gaps
demote verdicts honestly). Grid cap: **≤5 cells** (see budget rule).
An entry becomes READY only after the family is wired and reviewed.

- **Q4 — VWAP volume-exhaustion inversion (§1.2) — READY (wired
  2026-09-10):** reversion wants seller volume dying, not 1.5× confirmation.
  `buy_volume`/`sell_volume` persisted → harness-local `_ExhaustionGate`
  vetoes entries where the aggressor side is still accelerating (missing
  buy/sell volume vetoes — silence is no-evidence, never a pass).
  - **Harness:** `python scripts/research/overnight_runner.py --family vwap_exhaustion --start 2026-05-18 --end 2026-09-08`
  - **Grid (4 cells, fixed):** baseline surge=1.5 · `volume_surge:0` ablation
    · surge=0 + aggressor decaying vs prior bar · surge=0 + aggressor below
    own 24-bar mean. **Budget: 4 × 4 = 16 runs.**
- **Q5 — VWAP deceleration confirmation (§1.1) — READY (wired
  2026-09-10):** don't enter mid-impulse; wait for the z-score to stop
  making new extremes. Harness-local `_DecelGate` tracks the running |z|
  excursion extreme from its own 1h deque.
  - **Harness:** `python scripts/research/overnight_runner.py --family vwap_deceleration --start 2026-05-18 --end 2026-09-08`
  - **Grid (4 cells, fixed):** baseline · retrace ≥0.15σ · retrace ≥0.30σ ·
    counter-close (signal bar closes against the move).
    **Budget: 4 × 4 = 16 runs.**
  - **Cannot share a night with Q4** (two full grids exceed 20 runs) — separate
  nights by default.
- **Q2 — VB long-only + follow-through (§1.5) — BLOCKED (K=4 floor; reopen
  ~ 2026-12-05):** **RIGOR NOTE — the variant was selected on the
  2026-05-18..08-07 forensic sample.** Only windows strictly after 08-07
  count as evidence; the 30d split over 08-07..09-09 yields **1** evidence
  window today. Four post-selection windows of 30d complete
  **~ 2026-12-05** (08-07 + 120d). A 7d-window preregistration over
  post-selection data would technically yield 4 windows today but was **not
  chosen**: per-window n~5-8 makes the sign-flip deltas noise. The n≥30
  path is also borderline (~37 over 120d). Prior forensic slice (selection
  data, never evidence): delayed entry n=37, WR 43.2%, PF 1.11, net +5.13.
- **Q7 — VWAPDeviation exit-economics — READY (added + wired 2026-09-10):**
  the 4-window baseline bleeds net −151.09 / PF 0.9 / n=251 (Night 2
  artifact). Entry-side sweeps already failed (z=3.0, per-symbol) — the open
  question is whether the loss is exit-side give-back. Hypothesis (fixed):
  exits at |Z|<0.3 surrender reversion before tier-0 fees clear; a higher
  `exit_z_threshold` or a tighter TP/time-stop improves net PnL on the same
  entries.
  - **Harness:** `python scripts/research/overnight_runner.py --family vwap_exit_econ --start 2026-05-18 --end 2026-09-08`
  - **Grid (5 cells, fixed a priori):** baseline `exit_z=0.3 tp_r=2.0 hold=4h` ·
    `exit_z_threshold: 0.5` · `exit_z_threshold: 0.15` ·
    `take_profit_r_multiple: 1.5` · `max_hold_hours: 2`.
  - **Window set:** 2026-05-18..2026-09-08, 30d split → 4 non-overlapping
    windows (same fixed set as Night 2 — chosen once, never per-result).
  - **Budget:** 5 × 4 = **20 runs** (at cap; run alone).
  - **Evidence bar:** standard KEEP rules; note exit-side variants shift n —
    a cell that cuts n below 30 is INCONCLUSIVE by construction.
- **OIR real-window re-gate (parked → data-blocked):** real OIR exists in
  `hyperliquid.db` `l2_snapshots` (~1.7M rows, window ending 09-09). A 30d
  split over the L2 window yields **2 windows** — K=4 needs ~60+ more days
  of continuous collection (bot running). Revisit ~ 2026-11-28 at the
  earliest.


## NOT testable tonight — gate status table

| Item | Gate to reopen | Status 2026-09-09 |
|---|---|---|
| SpotPerpCarry | 30d mean \|predicted\| ≥ 0.0008 ∧ ≥5 days ≥ 0.0005 | **NOT MET** (0.000087; 1 day) |
| FundingMomentum | ≥8 funding sign flips / 30d | near-zero same-sign regime persists — re-measure when \|predicted\| rises |
| FundingArbitrage | ≥30 pair-scan opportunities / 90d | EMIT bursts rare; frequency insufficient |
| Liq-map veto/fuel (§1.4, §1.6) | Phase 3: dozens of zone events + forward approaches | needs L2 zone data; books stale — requires bot running |
| ret_lag fade (reversion) | live maker fills beat BE 4.21 bps | maker harness ready (`maker_fill_adverse_selection_l2.py`) but L2 data stale |
| CVD / feature screens | — | strategy CLOSED (FDR verdicts); feature pipeline only, never a strategy loop |
| ORB warm-up fix | — | strategy-code change → NEEDS-CODE, human decision ("when ORB work resumes") |
| MM feasibility | — | CLOSED definitive (verdict C ×2) |

## Budget rule

≤20 runs/night. **With the K=4 window floor, a night's grid is capped at
5 cells** (5 × 4 = 20); larger grids need a preregistered `--cells`
selection in the queue entry. Ledger so far: Night 1 = 16 OK, Night 2 =
12 OK. If a family finishes early, **stop** — spare budget is not spent on
unplanned windows (post-hoc window selection is forbidden). Verdicts land
in `docs/OVERNIGHT_RESEARCH_LOG.md`; the flush recheck itself belongs to
the watchdog supervisor, not this queue.
