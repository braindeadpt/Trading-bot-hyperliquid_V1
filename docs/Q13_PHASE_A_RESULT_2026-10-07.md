# Q13 Phase A — wallet markout persistence screen (result)

**Date:** 2026-10-07
**Screen:** `python -X utf8 scripts/research/wallet_markout_screen.py` (DB opened read-only via `file:...?mode=ro`)
**Data:** `/Users/noder/hyperliquid_research/hyperliquid.db` (14.70 GB), collector stopped 2026-10-07.

This document is the **pre-registration + result** record. The methodology
below was fixed in writing before the screen was run; no parameter was
tuned after seeing the output.

## Method (fixed before run)

- **Universe:** all wallets in `top_trader_fills` (leaderboard-selected —
  durability bias disclosed below; the original QUEUE entry says 69
  wallets, widened to ~251 on 2026-09-17 top-150 vlm ∪ top-100 pnl).
- **Symbols:** BTC, ETH, SOL, HYPE.
- **Horizons:** 5m, 15m, 1h (we have 1m candles; the papers used 2–10s
  L4 data — longer horizons are a weaker claim, recorded not hidden).
- **Fills:** taker only (`crossed=1`), signed markout
  `d·(m(t+h)−p)/p·1e4` bps, `d=+1` buy / `−1` sell.
- **Score:** per-wallet notional-weighted mean markout.
- **Usable window (per symbol):**
  `[max(first_taker_fill, first_candle), min(last_taker_fill, last_candle − 3_600_000)]`
  — the intersection of continuous `candles_1m` coverage (clipped by the
  longest horizon) and the available fill range. Fills inside the window
  but landing in a candle gap still count toward the time-split boundary
  but contribute no markout (counted in `rejected_no_candle`).
- **Split:** halves by fill time at the median usable-window taker fill,
  pooled across symbols.
- **Unlock rule (verbatim QUEUE.md):** `rho >= 0.30` with `>= 20` wallets
  having `>= 30` taker fills in EACH half, at `>= 1` horizon.
- **Kill criteria (verbatim):** Phase A rho < 0.30 -> dead.

## Diagnostics (fixed before run — not decision rules)

- Per-horizon permutation p: `N_PERM = 10_000` within-set permutations of
  the half-2 wallet→score assignment; `p = P(rho_perm >= rho_obs)`.
- Family diagnostic: `10_000` draws of one shared wallet permutation over
  the union of the three ok-sets — `P(max_h rho_perm >= 0.30)`, the chance
  of a pass at ≥1 of 3 correlated horizons under the null. Shared-σ
  approximation: pairs whose permuted partner leaves a horizon's ok-set
  are dropped for that horizon.
- Seed `PERM_SEED = 20261007` (fixed in code).

## Candle coverage audit (run 2026-10-07, read-only)

| symbol | taker fills | candles_1m | gaps >5m | usable-window fills |
|---|---:|---:|---:|---:|
| BTC | 365,267 | 370,015 | 104 | 352,458 |
| ETH | 333,434 | 370,145 | 107 | 326,763 |
| SOL | 125,536 | 370,077 | 104 | 124,525 |
| HYPE | 423,003 | 371,128 | 103 | 414,865 |

Candle coverage starts `1768132919999` (2026-01-10) — usable fill history
is bounded by candle start, not by the fills themselves (oldest fills are
from 2024).

Gaps >30min per symbol (all shared — bot downtime): ~11–14 each. Largest:
**124.7h ≈ 5.2 days** (`1790189099999 → 1790637899999`, 2026-09-23 18:44 → 2026-09-28 23:24 UTC —
the Windows→Mac migration window). Second: ~82–84h (July). Cluster of
6–10h gaps late Aug / early Sep.

**Known gap check:** the `08-15..09-01` candles_1m gap (backfilled
+91k rows in Sep) has **not** returned — zero gaps overlapping that
window.

## Results

Run 2026-10-07 on the live DB (read-only). 1,218,899 usable-window taker
fills across 167 wallets; split-half boundary `t=1790250472097`
(2026-09-24 11:47 UTC) — median usable-window taker fill, pooled symbols.

| horizon | n_wallets | rho | p_perm (10k) | fills h1 | fills h2 | rejected_no_candle | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| 5m  | 53 | +0.055 | 0.3475 | 512,712 | 392,829 | 313,358 | FAIL |
| 15m | 51 | −0.070 | 0.6804 | 516,159 | 393,797 | 308,943 | FAIL |
| 1h  | 53 | −0.255 | 0.9669 | 510,222 | 393,962 | 314,715 | FAIL |

Family diagnostic: `P(any horizon rho >= 0.30 under null | observed n,
10,000 shared perms) = 0.0456` — the permissive 0.30 threshold over 3
correlated horizons gives ~4.6% chance of a spurious pass; it did not
happen, and observed rhos are well inside null noise (p_perm 0.35–0.97).

`rejected_no_candle` ≈ 26% of in-window fills per horizon — fills whose
`t+h` lands in a candles_1m gap (the >30min gaps above plus many sub-5min
gaps not individually listed). Sufficient data remains: >390K scored
fills per half at every horizon.

**Verdict: FAIL — rho < 0.30 at every horizon with sufficient n
(51–53 ≥ 20 wallets). Q13 is dead; Phase B is not unlocked.**

The collector stays OFF (its stated continuation rationale — "data is
cheap" — was invalidated by the measured ~325 MB/day cost; see QUEUE.md
amendment). No Phase B built; thresholds unchanged.

## Limitation found on audit (2026-10-07) — read before relying on the FAIL

The split boundary (2026-09-24 11:47 UTC) lies **inside** the 124.7h migration
gap (09-23 18:44 → 09-28 23:24). Fills in the gap count toward the median-fill
boundary but cannot be scored, so the scored half 2 spans only ~8 days
(≈09-28 → 10-06) against ~8.5 months (≈01-11 → 09-23) for half 1. A 8-day
half is a noisy ranking of wallets, which lowers the power to detect real
persistence. The verdict follows the pre-registered rule and is not marginal
(p_perm 0.35–0.97), but it is a weaker kill than "sufficient n" suggests.
A sensitivity check (split at the calendar midpoint, or at the median of
SCORED fills) was **not run**; it would be a diagnostic, not a new decision rule.

## Selection-bias disclosure (verbatim intent from QUEUE.md)

The universe is leaderboard wallets (durability/volume/pnl selected), not
random wallets — conditioned on already-profitable traders. Fine for "is
there an informed sub-cohort"; must be disclosed with every number.

## Collector-stop context (Tarefa 9)

- `wallet-fills` pm2 app stopped + deleted (cron would resurrect `stop`);
  definition preserved in `ecosystem.config.js`; `pm2 save` written.
- Measured cost while running: ~444 MB/day total DB growth;
  `top_trader_fills` ~305K rows/24h (~250 MB/day incl. indexes);
  `trade_tape` ~103 MB/day.
- The QUEUE assumption "data is cheap" is invalidated by measurement —
  see dated amendment appended to QUEUE.md.
