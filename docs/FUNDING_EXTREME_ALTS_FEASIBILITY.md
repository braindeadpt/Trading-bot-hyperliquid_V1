# A2 — Extreme-funding mean reversion in non-major HL perps

Preregistration: `docs/PREREGISTER_FUNDING_EXTREME_ALTS_2026-10-06.md` (commit `d0b35fe`, frozen before data).
Script: `scripts/research/funding_extreme_alts_feasibility.py` — reproduce with `--skip-download` against `data/backtests/funding_mr_a2/funding_mr_a2_cache.db` (207 MB, 230 symbols × funding+2h candles).
Raw result JSON: `data/backtests/funding_mr_a2/funding_mr_a2_results.json` (pull 2026-10-09).

## Verdict: **C — KILL** (per frozen criteria)

Validation 2026-03-01 → 2026-10-09, H=48h fixed, re-arm band [p20,p80]:

| Metric | Value | A-threshold |
|---|---|---|
| Independent episodes | 2691 | ≥ 30 ✓ |
| Pooled mean NET edge | **−38.4 bps/episode** | ≥ 3× cost — FAIL |
| Pooled mean GROSS edge | −25.6 bps | — |
| Median measured cost | 12.3 bps RT | — |
| Random percentile | **12** | ≥ 90 — FAIL |
| Long side (fade extreme-neg) | +42.9 bps (n=840) | — |
| Short side (fade extreme-pos) | **−75.3 bps** (n=1851) | — |
| Both directions positive | no | required — FAIL |

Not just "no edge": the real signal sits at the **12th percentile of random
entries** — extreme funding is mildly *anti*-predictive at 48h. Funding paid/
received is already inside these numbers (shorts collected the extreme
positive funding and still lost −75 bps — adverse price moves dominate the
carry).

## The asymmetry is the only live finding

- **LONG at extreme-negative funding: +43 bps/ep net** — the only profitable
  side. Extreme-negative funding marks capitulation that bounces.
- **SHORT at extreme-positive funding: −75 bps/ep** — extreme-positive marks
  squeezes that keep squeezing; the funding collected doesn't cover the drift.

A long-only variant is a *different hypothesis* — it would need its own
preregistration, and a single asymmetric side is exactly the artifact the
both-directions clause was written to catch. Logged as a research note only.

## Discovery vs validation — the split earned its keep

Discovery-window diagnostics (same spec, same data source) looked tradeable:

| Horizon | n | mean net bps | median |
|---|---|---|---|
| 8h | 2343 | +11.9 | +4.6 |
| 24h | 2343 | −8.4 | −7.1 |
| 48h | 2343 | **+33.6** | +7.4 |
| 72h | 2343 | +34.0 (median −22.9) | fat tail |

Validation then produced −38 bps pooled. The regime flipped: discovery-era
extreme funding partially reverted; 2026H2-era extremes persisted. Any
parameter chosen on discovery would have shipped a loser — exactly the
failure mode the frozen split exists to prevent.

## Cost model measurements (Q2)

- `l2Book` half-spreads measured live for 174 symbols; median total RT cost
  **13.6 bps** (9 fees + 1.5×half-spread + 2 impact buffer at $5k).
- 56 symbols (delisted/no live book) charged the p90 fallback — conservative,
  keeps their pre-delisting history in the sample.

## Caveats

- 2h-bar granularity (API minimum); entry marks at bar close.
- p90/p10 on trailing-180d needs ≥30d history — newest listings enter late.
- The 12th-percentile result is real signal (anti-MR), not noise — flipping
  signs is NOT permitted by this preregistration; it would be a new study.

## Decision

Family closed per verdict C. Nothing to shadow list. The A1 carry verdict
(A) stands independently.
