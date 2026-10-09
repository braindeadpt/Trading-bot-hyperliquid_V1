# A1 — Spot–perp funding carry feasibility (delta-neutral, maker)

Preregistration: `docs/PREREGISTER_SPOTPERP_CARRY_2026-10-06.md` (commit `d0b35fe`, frozen before data).
Script: `scripts/research/carry_spot_perp_feasibility.py` — reproduce with `--skip-download` against `data/backtests/carry_a1/carry_a1_cache.db`.
Raw result JSON: `data/backtests/carry_a1/carry_a1_results.json` (pull 2026-10-09).

## Verdict: **A** — shadow-list eligible per prereg criteria

Validation window 2026-03-01 → 2026-10-09 (frozen spec, untuned):

| Metric | Value | A-threshold |
|---|---|---|
| Net ann. return on deployed notional | **+13.9%** | > 0 |
| Independent episodes (non-overlapping per pair) | **12** | ≥ 10 |
| Mean episode net | **225.6 bps** | ≥ 3× cost |
| Median modelled cost/episode | 12.0 bps | — |
| Edge ÷ cost | **~18.8×** | ≥ 3× |
| Random-entry percentile | **100 / 100** | ≥ 90 |
| Max drawdown (daily mark-to-market, funding+basis) | 3.24% | — |
| Deployed pair-days | 710 | — |

All four A-gates pass with margin.

## What the money actually is

| Pair | Entry → exit | Days | Funding bps | Basis bps | Cost bps | Net bps | Open at pull |
|---|---|---|---|---|---|---|---|
| PURR | 06-02 → (open) | 129 | +1029 | +80 | 13.3 | +1096 | yes |
| ZEC | 06-15 → (open) | 115 | +412 | −1 | 12.0 | +399 | yes |
| HYPE | 05-22 → (open) | 140 | +407 | +8 | 11.5 | +403 | yes |
| PUMP | 08-09 → (open) | 61 | +280 | −5 | 13.6 | +262 | yes |
| BTC | 08-23 → (open) | 47 | +123 | +11 | 8.0 | +126 | yes |
| ETH | 08-22 → (open) | 48 | +134 | +4 | 8.8 | +129 | yes |
| SOL | 08-22 → (open) | 48 | +107 | −2 | 9.5 | +95 | yes |
| MON | 09-04 → (open) | 35 | +187 | −92 | 12.4 | +83 | yes |
| ZEC | 05-04 → 06-06 | 33 | +58 | +24 | 12.0 | +71 | no |
| MON | 04-14 → 04-26 | 13 | +15 | +67 | 12.4 | +69 | no |
| MON | 05-08 → 05-17 | 10 | +19 | +16 | 12.4 | +23 | no |
| ENA | 09-07 → (open) | 32 | +133 | **−169** | 11.7 | **−48** | yes |

Funding collected, not basis convergence, is the PnL engine (basis ≈ noise
around zero except ENA −169bps and MON −92bps — the real risk term).

## Discovery-window structure (why the edge exists)

- Funding is **persistent, not noise**: hourly autocorr 0.34–0.93 at 1h,
  still positive at 24h/7d on most pairs.
- Mean annualized funding is **positive almost everywhere** (PURR +25%,
  PUMP +19%, XPL +17%, HYPE +15%, BTC/ETH +9.5%) — structural long bias of
  perp demand vs spot.
- Extremes are fat: PURR p99 = 353% ann., ZEC p99 = 133%.

## Integrity caveats (read before believing the A)

1. **9 of 12 episodes were still open at pull date** — counted at last mark
   including full round-trip cost, but their funding-accrued-so-far assumes
   continued receipt; realized exits may differ.
2. **Effective independence is lower than n=12**: BTC/ETH/SOL all entered
   08-22/23 — one cross-market funding regime, not three bets. Treat
   effective n as ≈ 7–8.
3. **Survivors only**: dead spot legs (TRUMP @9, BERA @117, WLD @224,
   MEGA @257) never satisfied the $100k/day spot gate — the universe is
   what remains (10 qualifying pairs). Pairs absent entirely from `spotMeta`
   can't carry.
4. **Cost model is maker-at-touch + 30% unfill haircut on proxy vol** —
   measured static half-spreads (HYPE 0.9/0.06, PURR 3.1/6.1 bps) are
   consistent, but live maker fill probability is unverified. A 24/7
   resting maker leg also carries adverse-selection cost not modelled.
5. Spot–perp **basis risk on U-wrapped legs** (UBTC, UETH, USOL…) is real
   but small in-sample (|basis| < ~3 bps mean for majors).
6. 2h candles (API minimum since sub-2h was dropped) — the entry/exit and
   funding accrual resolution is coarse; realized fills could differ at
   sub-2h granularity.

## Next step if pursued

Shadow-list eligible per prereg. Before any live/paper sizing: measure
real maker fill rates on the spot leg (the binding constraint) and the
basis tail on U-wrapped pairs, then run shadow with size caps from the
liquidity gates.
