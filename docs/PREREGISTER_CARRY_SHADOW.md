# PREREGISTER — Carry-Shadow: live L2 evidence for the A1 spot–perp carry

Date: 2026-10-09 (v2 — owner-reviewed, margin rule amended)
Status: FROZEN — committed before any code. Parameters are the primary spec;
nothing may be tuned on live observations.

Upstream: `PREREGISTER_SPOTPERP_CARRY_2026-10-06.md` (A1, verdict A) +
`CARRY_SPOTPERP_FEASIBILITY.md` §robustness-addendum (A− provisional: real-
capital return ~4–9%/yr, 5/12 episodes breach a +50% margin buffer).

## 0. Margin model answer (researched at prereg date)

HL account abstraction modes (docs "Account abstraction modes" / "Portfolio
margin"): **Standard/Manual** keeps spot and perp as separate ledgers (spot
cannot collateralize perps). **Unified** shares USDC only. **Portfolio
Margin (PM)** — spot balances collateralize perp positions; explicitly
designed for carry ("a spot balance is offset by a short perps position,
collateralized by the spot balance… no trading cost to rebalance over
significant price ranges"). Eligible collateral at prereg date: **HYPE
LTV 0.65, BTC LTV 0.50, USDC, USDT**; requires account value >$10k (or
>$5M weighted volume) and <$25M. All HIP-3 DEXs included.

Consequence: only the **HYPE** pair gets native spot-collateralized carry
today (UBTC counting as BTC collateral is UNVERIFIED — flagged as a shadow
measurement question). Every other pair (PURR, MON, ZEC, ENA, PUMP, SOL,
ETH, XPL…) runs on USDC margin and needs an explicit margin rule → §2.
PM also charges **borrow interest** — measured live, it is a real carry
cost and feeds the hurdle calc (§4).

## 1. Signal rules — identical to A1, re-frozen

- Universe: spot base/USDC pairs mapping to a perp (name or `U`+perp),
  PIT liquidity gates (spot ≥$100k, perp ≥$1M median 30d daily notional,
  ≥30d history) — identical to A1.
- `f_ann(t)` = mean of last 24 hourly funding rates × 24×365.
- **Enter** long spot + short perp when `f_ann ≥ +15%` and pair in-universe.
- **Exit** when 24h-mean funding < 0 for 48h consecutive. Both legs maker
  at touch. No re-optimization of X/Y/thresholds — ever.

## 2. Margin management (the new piece — fixed a priori)

Three variants were evaluated in-sample on the 12 A1 episodes (IN-SAMPLE
sanity, marked as such, not evidence):

| Variant | Liquidations | Total net |
|---|---|---|
| No margin rule (A1 baseline) | 5/12 | +2,707 bps |
| Sell-spot → top margin | 0/12 | −2,574 bps |
| **Proportional deleverage** | **0/12** | **+1,500 bps** |

The choice is **justified a priori, not by the in-sample PnL**: delta-
neutrality is the hypothesis itself. Sell-spot breaks it (the residual is
a naked short that bleeds on runners); deleverage preserves it by closing
a fraction of BOTH legs. That deleverage also scored best in-sample is
corroboration, not the reason.

Two branches by pair eligibility:

**PM pairs** (base asset in {HYPE}; UBTC pending verification):
capital = 1.0× spot + 0.10× USDC buffer; PM auto-borrows, no manual
rebalance; measure realized borrow APR as carry cost.

**USDC-margin pairs** — **proportional deleverage rule** (fixed):
- Initial committed capital = **1.0×** notional spot + **M0 = 0.60×**
  notional margin ⇒ effective leverage 1.67× (inside the mandated 1–2×).
- Margin fraction `mf = margin_equity / current_notional`, checked on
  every margin update (live: each L2 tick / 1min; sim: each 2h bar).
- **Trigger**: `mf < M` → close fraction `k` of BOTH legs (taker) such
  that `mf = M0` after the close: `k = 1 − equity/(M0 × N_t)`.
- **Floor**: if required `keep = 1−k ≤ 0.05` → close the episode entirely.
- **Liquidation**: `equity ≤ maint(pair) × N_t` → hypothetical
  liquidation → kill §5.
- **Per-pair M/M0 by fixed rule** (not hand-tuned): `maint` is the real
  HL maintenance fraction = `1/(2 × maxLeverage)` from `meta`:

  | perp | maxLev | maint |
  |---|---|---|
  | BTC | 40× | 1.25% |
  | ETH | 25× | 2.0% |
  | SOL | 20× | 2.5% |
  | HYPE, PUMP, ENA, XPL, ZEC, AVAX, WLD, TRUMP, FARTCOIN | 10× | 5.0% |
  | BERA, MON | 5× | 10.0% |
  | PURR, MEGA, STABLE, AZTEC | 3× | 16.67% |

  Rule: `M_pair = max(0.45, maint + 0.15)`, `M0_pair = M_pair + 0.15`
  (i.e., the trigger never sits <15pp above maintenance; defaults keep
  M=0.45/M0=0.60 for every pair in the current table — PURR's trigger
  distance is 28.3pp, the tightest).

## 3. Gap risk (measured on the A1 window, 2h bars — API minimum)

Largest single-2h adverse move per episode vs trigger→maintenance
distance:

| Episode | Max adverse 2h | Trigger dist | Jumps? |
|---|---|---|---|
| **PURR** | **21.5%** | **28.3pp** | no — but only ~7pp headroom |
| ZEC | 14.9% | 40.0pp | no |
| HYPE | 13.0% | 40.0pp | no |
| PUMP | 11.8% | 40.0pp | no |
| ENA | 10.7% | 40.0pp | no |
| others | ≤7.7% | 35–44pp | no |

1h granularity is unmeasurable historically (API serves ≥2h only); the
live shadow measures gaps at L2 tick cadence, not 2h. **Known risk:
PURR** — lowest maxLev (3× ⇒ maint 16.67%), tightest distance, largest
observed 2h jumps. A "deleverage failed by gap" event (margin check
finds `equity ≤ maint×N_t` before a trigger could fire) is a kill event
equivalent to a hypothetical liquidation (§5.4).

## 4. Economic hurdle (fixed at prereg date)

- Risk-free reference: **rf = 4.0%/yr** (3M T-bill / USDC yield proxy,
  fixed 2026-10-09 — the number, not the instrument, is frozen).
- **Hurdle: net return on committed capital ≥ rf + 4pp = 8.0%/yr** over
  the shadow window. Fails → verdict C even if PF > 1. Committed capital
  = 1.0× + M0 = 1.6× notional (non-PM) or ~1.1× (PM), deployed-time
  weighted; return net of ALL live-measured costs:
  - effective maker entry/exit costs (mark→fill slippage per leg),
  - each deleverage event (taker both legs on the closed fraction),
  - funding effectively received vs predicted per episode,
  - PM borrow interest (HYPE pair, and BTC if UBTC collateral confirmed).

## 5. What the shadow measures live (L2 only — no orders)

- **Maker fills, both legs**: fill rate within 15 min, time-to-fill,
  unlegged-event frequency + duration, effective cost per entry/exit
  (mark at decision vs fill), % entries never filled.
- **Margin events**: hypothetical deleverages (count, trigger price, cost
  at measured spreads), margin-equity path, worst distance to `maint`,
  gap-check passes (`equity ≤ maint` without a prior trigger).
- **PM branch**: borrow accrued vs funding received.
- **Funding**: received vs `fundingHistory` prediction per episode.
- **ADL (observed-risk log, NOT a kill criterion)**: auto-deleveraging
  events on the symbol if the API exposes them — recorded as tail-risk
  evidence.
- **Episode accounting**: hypothetical entry/exit marks, clustered
  episodes (24h rule), net bps on committed capital.

## 6. Kill criteria — ANY ONE kills the study (verdict C)

1. Measured effective cost that drags expected episode edge below **3×**
   the measured round-trip cost — **interim check**: after the first
   ≥5 closed episodes, if the measured edge fails 3× → immediate death.
2. Maker fill rate **< 40% within 15 min** on either leg — **interim
   check**: after ≥20 entry/exit attempts per leg, fill <40% →
   immediate death (don't wait for the final read).
3. Final read misses the **8.0%/yr committed-capital hurdle** (§4).
4. **One hypothetical liquidation** under the active margin rule —
   including "deleverage failed by gap" (§3).
5. Read condition: verdict at **≥10 closed clustered episodes** or at
   **expiry 2027-12-26** (= 2 × A1's observed 221 days for 10 clusters),
   whichever first; expiry with n < 10 → B at best.
6. **6-month review 2027-04-09**: if <3 closed clustered episodes, the
   observed rate is logged and the human decides whether the expiry
   stands — decision on episode COUNT only, never on PnL.

## 7. Verdict mapping

- **A**: ≥10 closed clustered episodes, all §6 alive, net ≥ 8%/yr on
  committed capital, fill gate passed → candidate for a *separate*
  execution preregistration (not automatic promotion).
- **B**: evidence positive but incomplete (n ∈ [5,10) at expiry, or one
  secondary gate marginal) — keep collecting, no promotion.
- **C**: any §6 trigger, or net ≤ 0.

## 8. Isolation (enforced)

- Separate module `src/research/carry_shadow/` — reads L2 + funding feeds
  read-only, writes ONLY `data/research/carry_shadow.db`. No writes to
  `bot.db`, `settings.yaml`, `.env`, strategy registry, or engine calls.
- Outside `config_hash` (no config change). Jev execution and the OOS
  window untouched.
- Isolation test mirrors `test_g_shadow_routing_mirror_never_touches_execution`:
  identical feeds → zero engine/execution calls.
- Logged as mid-window hash-neutral code in
  `docs/PAPER_OOS_90D_PROTOCOL.md` code-change table at implementation.

## 9. Reproducibility

- Module: `src/research/carry_shadow/` (new); wrapper task:
  `deploy/macos/run_carry_shadow.sh` + PM2 app (separate entry, not the
  jev ecosystem entry — decided at implementation).
- Raw L2/funding observations persisted in `data/research/carry_shadow.db`
  (append-only) — the full episode ledger is auditable offline.

## 10. Instrumentation amendment — unlegged-risk measurement (2026-10-09)

**Scope: measurement only.** No entry rules, exit rules, fill windows,
gates, or kill criteria change. Approved by the owner; constrained to
`src/research/carry_shadow/` plus its status script and tests.

Section 5 already requires "unlegged-event frequency + duration" — the
original schema recorded duration and leg flags but **not** the price
excursion during the naked window, so the loose-leg risk this study exists
to measure was invisible (observed live: ENA 306 s unlegged, MON 4.5 bps
unwind cost, with no mid-path data). Added:

- **`leg_fill` event** per leg fill: which leg, fill price, and a BBO+mid
  snapshot of **both** legs at fill time.
- **BBO+mid snapshot of both legs** on `unlegged_unwind` and on completed
  `fill` events (same snapshot format).
- **`unlegged_max_adverse_bps`** (episode column): while exactly one leg
  is filled, the worst adverse excursion of the *missing* leg's mid,
  measured in bps against that leg's **own mid at first-fill time** —
  the reference is basis-free, so the metric isolates price movement
  *during* the unlegged window. For a pending sell leg, adverse =
  `ref − mid`; for a pending buy leg, adverse = `mid − ref`. Latched
  (max) across the whole unlegged window.
- **`unlegged_basis_bps`** (episode column + `leg_fill` events): the
  spot–perp mid basis `(perp_mid − spot_mid)/spot_mid` at first-fill
  time — context only, kept separate from the excursion metric.
- **`unlegged_s`** (episode column): total seconds spent unlegged,
  persisted on the episode row at `fill`/`aborted` (previously only in
  the event payload).
- **Status report**: per coin — entry attempts, aborts, accumulated
  unwind cost, and unlegged stats (avg / p90 / max of
  `unlegged_max_adverse_bps` and duration).

**Backfill policy:** episodes 1–4 (pre-instrumentation) keep `NULL` in
`unlegged_s`/`unlegged_max_adverse_bps`/`unlegged_basis_bps` — no
backfill, the columns
distinguish "measured" from "pre-instrumentation" rather than rewriting
history. Status aggregation excludes NULLs.

Migration: `ALTER TABLE episodes ADD COLUMN` guarded by a `PRAGMA
table_info` check — existing ledgers migrate on next daemon open; the
status script tolerates pre-migration schemas.

## 11. Gap semantics for non-margin state (approved 2026-10-09)

**Status: approved — measurement only.** Applies **forward-only from its
deploy commit** (visible in the code-change log): no recomputation of
earlier attempts or gaps, no backfill.

`gap_unverified=0` means "open-episode margin was replayed through the
gap via candles" — it does **not** mean "nothing was lost": the catchup
does not cover pending entry legs or entry decisions. From this
amendment on:

- `gap_unverified=1` on the episode when a **gap > 5 s** overlaps a
  pending entry leg; a `gap_entry_unverified` event is emitted when a
  gap > 5 s overlaps an eligible entry window for a pair with no episode
  (there is no row to mark — the event is the record).
- Conservative fill rule inside a gap: a pending maker leg **does not**
  fill during the gap (aligned with the strict-cross rule — no tape
  evidence, no fill). If the leg's window expires inside the gap, the
  attempt counts as an abort at gap end (`expire_entry_window`); the
  loose leg is unwound at the **worst 1m candle price inside the gap**
  (spot low when selling the spot leg back, perp high when buying the
  perp short back — `unwind_adverse_bps`/`unwind_candle_iv` on the
  abort event) **plus taker**, never the gap-end mid. When candles are
  unavailable the unwind is charged taker only and the episode stays
  `gap_unverified`-flagged.
- `gap_s:<YYYYMMDD>:<pair>` counters in meta accumulate gap seconds per
  pair per day; the status report shows them and the final readout
  mentions any pair over **1% gap time**.

Rationale: distinguishes "margin verified" from "tape coverage" — a
long flap could silently skip entries under today's flagging.

## 12. Strict-fill size sensitivity (approved 2026-10-09)

**Status: approved — measurement only.** Applies **forward-only from its
deploy commit**, in the same daemon restart as §11. The strict
quote-crossing gate is unchanged; this amendment only measures *how
much* size plausibly executed when a fill occurred.

Motivation (observed 2026-10-09, first 4 episodes): strict filled 7/8
legs while the trade-volume proxy filled 1/8. The two are not ordered —
strict measures quote crossing, the proxy measures aggressor volume —
and ZEC-spot / MON₄-spot strict fills with **zero** at-or-better tape
show the gap is quote repricing through the level (cancellations), not
fills-with-known-size. On a real CLOB those crosses would have hit our
order, but with unknown size.

Per strict fill:

- **`crossed_visible_usd`** (leg attr + `leg_fill` event): USD resting
  on opposing levels that **strictly** cross our price at the fill tick
  — for a resting buy, ask levels `px < price`; sell, bids `px > price`
  (same strictness as the gate). `NULL` when the snapshot carried no
  depth: unmeasured ≠ zero.
- **`crossing_size_usd`** (fill_stats): `crossed_visible_usd` +
  at-or-better aggressor tape volume accumulated over the maker window
  (same accumulator as the proxy context).
- **`fill_frac_est`** (fill_stats per leg; episode column = min of leg
  fracs, written only when both legs strict-filled):
  `min(1, crossing_size_usd / need_usd)` with
  `need_usd = SHADOW_NOTIONAL_USD * q`.

**Predeclared secondary analysis** at the final readout — never alters
the primary verdict:

- `weighted_fill_rate` = Σ `fill_frac_est` / n attempts (same
  denominator as the strict rate).
- `ann_ret_committed_weighted`: episode PnL weighted by the episode's
  `fill_frac_est`, over measured episodes only (`measured_eps`
  reported; NULLs are counted, not guessed). For like-for-like
  comparison the readout also emits `ann_ret_committed_measured` —
  the primary formula restricted to the same measured subset and its
  own committed-capital mean.
- `weighted_below_hurdle`: if the weighted annualized return falls
  below 8%/yr the readout says so explicitly (`note` field).

**Backfill policy:** episodes 1–4 keep `NULL` in
`crossing_size_usd`/`fill_frac_est` — no retroactive size claims.

Migration: `ALTER TABLE` guarded by `PRAGMA table_info`, same pattern
as §10.
