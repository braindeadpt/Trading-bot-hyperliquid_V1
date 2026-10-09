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

## Robustness addendum (2026-10-09 — same frozen spec/data, X/Y untouched)

Script: `scripts/research/carry_a1_robustness.py` →
`data/backtests/carry_a1/carry_a1_robustness.json`.

### 1. Censoring — closed vs open episodes

| Set | n | Total net bps |
|---|---|---|
| **Closed** (exit rule fired) | 3 | **+162.6** (MON +69.2/12.8d, MON +22.7/9.5d, ZEC +70.7/33.4d) |
| **Open @ pull**, maker exit | 9 | +2544.6 |
| **Open @ pull**, worst-case taker exit (−6bps extra) | 9 | +2490.6 |

Taker exit costs each open episode only −6 bps; at the observed funding
pace (2.2–8.0 bps/day) covering it takes **0.8–2.7 days**. The headline is
*not* manufactured by unrealized positions — but it does rest on them
(93% of net bps are still open).

### 2. Independence — 24h-entry clustering + 1000-run control

Entries within 24h chained into one cluster: **n_clustered = 10**
(SOL+ETH+BTC 08-22/23 merged). The ≥10 gate holds **by exactly one** —
literal A, zero margin. Clustered random control (one common time-shift
per cluster, durations preserved, 1000 runs, seed 42): **percentile 99.5**.

### 3. Concentration — leave-one-symbol-out

| Removed | n | Net ann. on deployed | Edge ÷ cost |
|---|---|---|---|
| — (full) | 12 | +13.9% | 18.8× |
| PURR | 11 | **+10.1%** | 12.2× |
| PUMP | 11 | +13.7% | 18.5× |
| XPL | 12 | +13.9% (XPL emitted no episodes) | 18.8× |

The edge is **carry, not PURR** — without PURR it stays positive and >3×.

### 4. Construction biases

- **Liquidity gate is point-in-time**: `liq_ok` derives from trailing-30d
  rolling medians evaluated at each bar's own day; audit confirms every
  episode entered on an in-universe day. No lookahead.
- **Direction**: all 12 episodes are long-spot/short-perp (entry requires
  f_ann ≥ +15% > 0). No short-spot (impossible on HL) ever emitted.
- **Return on capital**: deployed-only 13.9%/yr → on committed capital
  (spot + margin = **1.5×** notional, sized for +50% spike) **9.3%/yr** →
  on **total reserved capital** (peak 9 concurrent × 1.5, reserved over
  the 151d span) **4.1%/yr**.

### 5. The real finding — the +50% margin buffer is not enough

| Episode | Max adverse move on short | Distance to +50% liq |
|---|---|---|
| ZEC 06-15 | **+221.7%** | **−171.7pp — liquidated** |
| PUMP 08-09 | +165.2% | −115.2pp — liquidated |
| HYPE 05-22 | +69.6% | −19.6pp — liquidated |
| ENA 09-07 | +65.5% | −15.5pp — liquidated |
| ZEC 05-04 | +64.2% | −14.2pp — liquidated |
| SOL | +32.8% | +17.2pp |
| PURR | +28.7% | +21.3pp |
| others | <14% | safe |

**5 of 12 episodes would have been liquidated** at the mandated 50% margin
buffer. Survivors of the worst observed excursion (ZEC +221%) need margin
≈ 2.3× notional ⇒ capital ≈ 3.3× notional ⇒ ann on committed ≈ **4.2%/yr**.

Liquidation-aware re-run (forced unwind at the liq bar's marks, funding to
truncation, zero liquidation penalty — *optimistic*): aggregate +3386 bps,
99th pct stays high. Delta-neutrality means the spot leg's gain offsets the
lost margin — the real dangers are the liquidation penalty, forced-unwind
slippage on thin spot books, and trusting a stale 2h spot mark at exactly
the violent moment (PUMP's spot was +88% at the liq bar vs perp +50% — that
+38pp basis capture is fragile). Either way, the carry survives forced
exits only if the spot leg tracks — which is the assumption this study
can't verify at marks alone.

### Post-addendum verdict

Literal gates: **A stands** (n_clustered = 10 ≥ 10; no-PURR = +10.1% > 0;
liq-aware aggregate still positive). In spirit: **A−, provisional** — the
PnL rests on 9 open episodes, on n at the exact threshold, and on a margin
buffer reality violates. The honest headline number is **~4–9%/yr on real
capital**, not 13.9%.

## Next step if pursued

Shadow-list eligible per prereg. Before any live/paper sizing: measure
real maker fill rates on the spot leg (the binding constraint) and the
basis tail on U-wrapped pairs, then run shadow with size caps from the
liquidity gates.
