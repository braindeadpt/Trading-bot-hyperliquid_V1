# PREREGISTER — A2: extreme funding mean-reversion in alts

Date: 2026-10-06
Status: FROZEN — committed before any run. Parameters below are the primary
spec; nothing in them may be tuned on the validation window.

## Hypothesis

On non-major Hyperliquid perps, a funding rate in the symbol's own top/bottom
percentile marks crowded positioning that partially unwinds over the next
8–72h: the perp returns toward its non-extreme path AND the fade collects the
extreme funding while it persists. Admission rule (same as A1): expected
gross edge ≥ 3× measured round-trip cost for the bot's size.

Two questions, both answered on data:
- Q1: does per-symbol extreme funding predict the perp's forward return over
  8–72h, **net of funding paid/received during the hold**?
- Q2: does the real cost per symbol (measured spread + impact at the bot's
  ~$5k clip) fit inside the edge?

## Data (public HL `info` API, cached raw under `data/backtests/funding_mr_a2/`)

- `fundingHistory` (hourly) — every non-major perp, from its listing.
- `candleSnapshot` interval `2h` (minimum interval served since the API
  dropped sub-2h bars — verified 2026-10-06) — mark-price series for
  forward returns + PIT volume for liquidity.
- `metaAndAssetCtxs` at pull time for universe enumeration (delisted coins
  stay in via candle/funding history — survivorship includable).
- No local recorder data (it is empty for funding — verified 2026-10-06).

## Universe (PIT)

- All HL perps **except** BTC, ETH, SOL, HYPE.
- A symbol enters the tradable set on day d if its median daily notional
  (2h candles: vol × close) over trailing 30d ≥ **$250k**. Justification:
  $5k position ⇒ ≤2% participation; below this, spread/impact are
  unmeasureable and books are untradeable for us.
- ≥30 days of funding history before a symbol can emit a signal (the
  percentile needs a past).
- Delisted coins contribute their pre-delisting history.

## Fixed rule (primary spec — frozen)

- `f24(t)` = mean of last 24 hourly funding rates for the symbol.
- Percentile: `f24` is ranked against the symbol's own trailing **180d**
  distribution of `f24` (min 30d history required — else no signal).
- **Signal:** `f24 ≥ p90` → SHORT perp; `f24 ≤ p10` → LONG perp.
- **Re-arm:** after an episode, the symbol emits again only once `f24`
  re-enters the [p20, p80] band — one signal per extreme episode, never a
  re-fire inside the same regime.
- **Hold: H = 48h fixed** (primary). 8h/24h/72h reported as diagnostics
  only — they are not alternative specs and cannot be tuned on validation.
- Entry/exit both legs at the signal close (mark price); funding accrued
  during the hold is credited/debited from `fundingHistory` — this is the
  "net of funding paid/received" clause of Q1.

## Cost model (fixed)

- Taker both sides: fee 4.5 bps × 2 = **9 bps RT** (tier-0).
- Spread+impact per symbol, measured at study time: current `l2Book`
  half-spread × **1.5** (haircut for staleness) + **2 bps** impact buffer at
  a $5k clip, capped by the ≤2% participation gate (symbols failing the gate
  are excluded from tradable episodes, not just charged more).
- Net episode edge = mid-to-mark forward return over H + funding accrued −
  RT cost. A long at extreme-negative funding pays the funding; a short at
  extreme-positive receives it — both are accounted inside the hold.

## Metrics

- Per-horizon gross edge (bps), net edge after cost, per symbol and pooled.
- n of **independent episodes** (per symbol, non-overlapping; a 48h episode
  blocks new signals for that symbol while open).
- Random control: same episode durations, entry times uniform-random within
  the symbol's in-universe window, seed 42, ≥200 runs → percentile of the
  real episode stream's net edge inside the random distribution.

## Split (temporal — no tuning on validation)

- Window: **2025-06-01 → 2026-10-05**.
- **Discovery** 2025-06-01 → 2026-02-28 — descriptive stats only
  (funding distributions, persistence autocorr, horizon diagnostics).
- **Validation** 2026-03-01 → 2026-10-05 — frozen spec scored here.

## Verdict (fixed)

- **A (candidate → shadow-list eligible):** validation pooled net edge
  ≥ 3× median measured RT cost AND ≥ 30 independent episodes AND
  random percentile ≥ 90 AND positive in BOTH long-extreme and
  short-extreme directions (no single-side artifact).
- **B (watch):** net edge > 0 but n ∈ [15, 30) or percentile ∈ [75, 90)
  or only one direction positive.
- **C (kill):** net edge ≤ 0, or percentile < 75, or n < 15, or cost
  exceeds edge for the majority of episodes.

## Reproducibility

- Script: `scripts/research/funding_extreme_alts_feasibility.py` (new).
- Raw API payloads cached under `data/backtests/funding_mr_a2/` —
  `--skip-download` reproduces identical numbers.
- No writes to `bot.db`, no `.env`, no `settings.yaml`, no engine changes.
