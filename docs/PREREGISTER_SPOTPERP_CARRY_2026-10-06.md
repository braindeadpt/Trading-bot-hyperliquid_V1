# PREREGISTER — A1: delta-neutral spot–perp funding carry (maker entry)

Date: 2026-10-06
Status: FROZEN — committed before any run. Parameters below are the primary
spec; nothing in them may be tuned on the validation window.

## Hypothesis

On Hyperliquid pairs that have BOTH a USDC spot leg and a perp, persistent
positive funding pays a delta-neutral carry (long spot, short perp) that
survives maker round-trip costs and a conservative non-execution model.
Directional edges died against ~11 bps taker; this is a structural edge with
low turnover — admission rule: expected gross edge per episode ≥ 3× the
round-trip execution cost.

## Data (all public HL `info` API, cached raw under `data/backtests/carry_a1/`)

- `fundingHistory` (hourly, per perp symbol) — full available history.
- `candleSnapshot` interval `2h` for both legs (spot pair + perp). NOTE:
  1h/sub-hourly candles are no longer served by the API (verified
  2026-10-06: `1h` returns empty for BTC; `2h` is the minimum interval).
- `spotMeta` / `metaAndAssetCtxs` for universe + current volumes.
- No local recorder data is used — `funding_history` is empty and
  `candles_1m.funding_rate` is all-NULL in the research DB (verified
  2026-10-06). This study does not depend on the local DB.

## Universe (PIT, rule-based — no cherry-picking)

A pair enters the universe when ALL hold:

1. HL `spotMeta` has a base/USDC spot pair whose base token name equals the
   perp name (`HYPE`↔`HYPE`) or equals `'U'+perp` (unit wrapper: `UBTC`↔`BTC`,
   `UETH`↔`ETH`, …). The U-wrapper introduces residual basis risk — disclosed,
   not hidden.
2. Spot-leg liquidity (PIT): median daily notional (base vol × close, from
   2h candles) over the trailing 30d ≥ **$100k**. Justification: intended
   position $1–5k ⇒ ≤5% participation; below this the spot leg is untradeable
   for us regardless of funding.
3. Perp-leg liquidity (PIT): median daily notional ≥ **$1M** over trailing
   30d (loose — perps are deeper; the spot leg is the binding constraint).
4. ≥30 days of dual-leg history inside the study window.

Current candidate set (2026-10-06 `spotMeta`, before liquidity filters):
PURR/USDC↔PURR, @107 HYPE↔HYPE, @142 UBTC↔BTC, @151 UETH↔ETH,
@156 USOL↔SOL, @162 UFART↔FARTCOIN, @188 UPUMP↔PUMP, @194 UBONK↔BONK,
@206 UENA↔ENA, @210 UXPL↔XPL, @306 UAVAX↔AVAX. Membership is computed
point-in-time — a pair listed mid-window joins when it satisfies 1–4.

## Fixed rule (primary spec — frozen)

- Signal `f_ann(t)` = mean of the last 24 hourly `fundingHistory` rates,
  annualized: `f_ann = mean24h × 24 × 365`.
- **Enter** (long spot + short perp, equal notional, both legs maker at
  touch): when `f_ann ≥ +15%` AND the pair is in-universe.
- **Exit** (both legs maker): when the 24h-mean funding turns negative and
  stays negative for **Y = 48h** (hysteresis vs churn).
- X = 15% APR was chosen ex ante so that a ≥7-day hold yields
  ~15%×7/365 ≈ 29 bps ≈ 3× the modelled round-trip cost (~9–10 bps) —
  the 3× admission rule is encoded in the threshold itself.
- Position sizing is irrelevant to feasibility: results are per unit of
  deployed notional. Capacity is reported via the liquidity gates above.

## Cost model (fixed)

- Maker fee 1.5 bps/side × 2 legs × (entry+exit) = **6 bps round-trip**.
- Non-execution / unlegged risk: if a maker leg fails to fill within 30 min
  the offsetting filled leg is closed at taker. Modelled per episode as
  `p_unfill × adverse_move` with **p_unfill = 30%** (conservative) and
  `adverse_move` = pair's median |30m perp move| proxied as median|2h
  return|/4 from candles (2h is the min served interval — proxy disclosed).
- Spread: maker-at-touch pays ~0 spread by construction; the residual is the
  fill risk above. Static `l2Book` snapshots are recorded for the report;
  recorded `l2_snapshots` for the 4 majors sanity-check the assumption.
- Funding paid while holding comes straight from `fundingHistory` (signed).

## Metrics

- Net annualized return on deployed notional (geometric over the episode
  stream), max drawdown of the episode equity curve, n of **independent**
  episodes (non-overlapping per pair), funding-collected vs basis-convergence
  decomposition.
- Random-entry control: same episode count and durations, entry times
  uniform-random inside the pair's in-universe window, seed 42, ≥200 runs →
  percentile of the strategy's net return within the random distribution.
- Sharpe is NOT the headline (carry PnL is lumpy per episode); reported as
  secondary.

## Split (temporal — no tuning on validation)

- Window: **2025-06-01 → 2026-10-05** (data end = pull date).
- **Discovery** 2025-06-01 → 2026-02-28 — used for the funding distribution /
  persistence descriptive stats and sanity checks ONLY.
- **Validation** 2026-03-01 → 2026-10-05 — the verdict is computed here.
  The X/Y params are already frozen; validation runs them untouched.

## Verdict (fixed)

- **A (candidate → shadow-list eligible):** validation net annualized return
  > 0 AND net episode PnL ≥ 3× modelled RT cost AND n_indep ≥ 10 episodes
  AND random percentile ≥ 90.
- **B (watch — keep collecting, no shadow list):** net return > 0 but
  n_indep < 10 or percentile ∈ [75, 90).
- **C (kill):** net return ≤ 0 on validation, or percentile < 75, or the
  universe ends up with < 2 qualifying pairs (insufficient breadth).

## Reproducibility

- Script: `scripts/research/carry_spot_perp_feasibility.py` (new).
- Raw API payloads cached under `data/backtests/carry_a1/` — rerun with
  `--skip-download` reproduces identical numbers.
- No writes to `bot.db`, no `.env`, no `settings.yaml`, no engine changes.
