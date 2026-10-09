# PREREGISTER — Carry-Shadow: live L2 evidence for the A1 spot–perp carry

Date: 2026-10-09
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
cost and feeds the hurdle calc.

## 1. Signal rules — identical to A1, re-frozen

- Universe: spot base/USDC pairs mapping to a perp (name or `U`+perp),
  PIT liquidity gates (spot ≥$100k, perp ≥$1M median 30d daily notional,
  ≥30d history) — identical to A1.
- `f_ann(t)` = mean of last 24 hourly funding rates × 24×365.
- **Enter** long spot + short perp when `f_ann ≥ +15%` and pair in-universe.
- **Exit** when 24h-mean funding < 0 for 48h consecutive. Both legs maker
  at touch. No re-optimization of X/Y/thresholds — ever.

## 2. Margin management (the new piece — fixed a priori)

Two branches by pair eligibility:

**PM pairs** (base asset in {HYPE}; UBTC pending verification):
capital = 1.0× spot + 0.10× USDC buffer; PM auto-borrows, no manual
rebalance; measure realized borrow APR as carry cost.

**USDC-margin pairs** — **proportional deleverage rule** (fixed):
- Initial committed capital = **1.0×** notional spot + **M0 = 0.60×**
  notional margin ⇒ effective leverage 1.67× (inside the mandated 1–2×).
- Margin fraction `mf = margin_equity / current_notional`, checked on
  every margin update (live: each L2 tick / 1min; sim: each 2h bar).
- **Trigger**: `mf < M = 0.45` → close fraction `k` of BOTH legs (taker)
  such that `mf = M0` after the close: `k = 1 − equity/(M0 × N_t)`.
- **Floor**: if required `keep = 1−k ≤ 0.05` → close the episode entirely.
- Liquidation check: `equity ≤ maint×N_t` with `maint = 10%` notional
  (conservative for low-leverage alts; majors are ~3.1%) → counts as a
  hypothetical liquidation → kill criterion §5.
- M/M0 chosen by reasoning, not A1 data: M0=0.60 survives ≈+50–57% adverse
  before maintenance; M=0.45 fires after ≈12–15% adverse drift, early
  enough that a ≤12% gap between checks cannot reach maintenance.

**Why deleverage and not sell-spot→margin (the literal original spec):**
in-sample on the 12 A1 episodes the sell-spot rule produced **0
liquidations but −2,574 bps total** (vs +2,707): selling only the spot
converts the hedge into a residual net-short that bleeds on runners
(PUMP −5,690bps, ZEC −4,813bps, both fully unwound). Deleveraging keeps
delta-neutrality — **the hedge IS the hypothesis** — and the same sim
gives **0 liquidations, +1,500 bps total** (−1,207bps of foregone funding
is the insurance cost; worst episode −89bps vs −5,690). Sanity marked
IN-SAMPLE, not evidence; numbers reported for the record.

| Episode | sell-spot rule | deleverage rule |
|---|---|---|
| PURR | −836 | +682 |
| PUMP | −5,690 (unwind) | +76 (18% left open) |
| HYPE | −9,710 | +213 (41% left) |
| ENA | −8,051 | −89 (35% left) |
| ZEC 06-15 | −22,491 (unwind) | +189 (12% left) |
| ZEC 05-04 | −6,183 | +26 (34% left) |
| others | −1,309…−3,663 | +1…+114 |
| **total** | **−2,574** | **+1,500** |

## 3. Economic hurdle (fixed at prereg date)

- Risk-free reference: **rf = 4.0%/yr** (3M T-bill / USDC yield proxy,
  fixed 2026-10-09 — the number, not the instrument, is frozen).
- **Hurdle: net return on committed capital ≥ rf + 4pp = 8.0%/yr** over
  the shadow window. Fails → verdict C even if PF > 1. Committed capital
  = 1.0× + M0 = 1.6× notional (non-PM) or ~1.1× (PM), deployed-time
  weighted; return measured net of measured fills, rebalances, funding,
  and PM borrow interest.

## 4. What the shadow measures live (L2 only — no orders)

- **Maker fills, both legs**: fill rate within 15 min, time-to-fill,
  unlegged-event frequency + duration, effective cost per entry/exit
  (mark at decision vs fill), % entries never filled.
- **Margin events**: hypothetical deleverages (count, trigger price, cost
  at measured spreads), margin-equity path, worst distance to `maint`.
- **PM branch**: borrow accrued vs funding received.
- **Funding**: received vs `fundingHistory` prediction per episode.
- **Episode accounting**: hypothetical entry/exit marks, clustered
  episodes (24h rule), net bps on committed capital.

## 5. Kill criteria — ANY ONE kills the study (verdict C)

1. Measured effective cost that drags expected episode edge below **3×**
   the measured round-trip cost.
2. Maker fill rate **< 40% within 15 min** on either leg (pooled across
   filled episodes) — unlegged risk dominates below this.
3. Final read misses the **8.0%/yr committed-capital hurdle** (§3).
4. **One hypothetical liquidation** under the active margin rule (§2).
5. Read condition: verdict taken at **≥10 closed clustered episodes** or
   at **expiry 2027-12-26** (= 2 × A1's observed 221 days for 10
   clusters), whichever comes first; at expiry with n < 10 → B at best.

## 6. Verdict mapping

- **A**: ≥10 closed clustered episodes, all §5 alive, net ≥ 8%/yr on
  committed capital, fill gate passed → candidate for a *separate*
  execution preregistration (not automatic promotion).
- **B**: evidence positive but incomplete (n ∈ [5,10) at expiry, or one
  secondary gate marginal) — keep collecting, no promotion.
- **C**: any §5 trigger, or net ≤ 0.

## 7. Isolation (enforced)

- Separate module `src/research/carry_shadow/` — reads L2 + funding feeds
  read-only, writes ONLY `data/research/carry_shadow.db`. No writes to
  `bot.db`, `settings.yaml`, `.env`, strategy registry, or engine calls.
- Outside `config_hash` (no config change). Jev execution and the OOS
  window untouched.
- Isolation test mirrors `test_g_shadow_routing_mirror_never_touches_execution`:
  identical feeds → zero engine/execution calls.
- Logged as mid-window hash-neutral code in
  `docs/PAPER_OOS_90D_PROTOCOL.md` code-change table at implementation.

## 8. Reproducibility

- Module: `src/research/carry_shadow/` (new); wrapper task:
  `deploy/macos/run_carry_shadow.sh` + PM2 app (separate file, not the
  jev ecosystem entry — decided at implementation).
- Raw L2/funding observations persisted in `data/research/carry_shadow.db`
  (append-only) — the full episode ledger is auditable offline.
