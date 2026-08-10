# Research Backlog

Ideas parked deliberately, with the evidence bar each must clear before it
touches the bot. Nothing here is approved work — this file exists so good
ideas are not lost *and* not acted on prematurely.

**Standing rule:** the Fase 10 frozen validation window was **re-registered
2026-08-08** after a structural deadlock fix (ChecklistMeta promoted to
execution; sizing 2.0%; governor last-exec protection). Prior window archived
beside `data/research/phase10/phase10_preregister.json`. Any *further* change
to `config/settings.yaml` invalidates the new window — re-register via
`scripts/reregister_phase10_deadlock_fix.py` (or equivalent) with justification;
never disable `assert_config_matches_preregister`. ChecklistMeta promotion is
partly in-sample — OOS walk-forward is mandatory before treating the ruleset
as validated.

**Baseline-signal gate (2026-08-09):** new promotions to
`execution_strategies` require three-condition PASS (B1≥p95 ∧ n≥30 ∧ PF>1).
See `docs/BASELINE_SIGNAL_GATE.md`. Portfolio board:
`data/backtests/parity_diag/PORTFOLIO_STATUS_BOARD.md`.

**First demotion:** ChecklistMeta powered FAIL → move to shadow.
VWAPDeviation sole execution while underpowered.

Last updated: 2026-08-10 (OI backfill + OI/tape screens → both C)

### Fee note — maker_fee_pct underestimated in config (do not rebuild history)

`execution.maker_orders.maker_fee_pct` in `settings.yaml` is **0.01** (1.0 bps)
but Hyperliquid perps tier-0 maker is **0.015% (1.5 bps)** per
https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees.
Prior maker cost tests used the underestimated fee (~33% too low on the fee
leg). **Verdicts still stand** (they failed even with the optimistic fee).
Awaiting explicit confirmation before changing production YAML to `0.015`.

### Archived — OI backfill viability + positioning screen (verdict C)

`docs/OI_BACKFILL_VIABILITY.md`, `scripts/backfill_oi_bybit_research.py`,
`scripts/feature_screening_oi_positioning.py`,
`docs/FEATURE_SCREENING_OI_POSITIONING.md` (2026-08-10):

- HL-native OI only ~66d → prior `oi_delta_24h` BE 19.6 / CI [−39,+39] was
  **INCONCLUSIVE for power**, not a free pass.
- Backfill **possible**: Bybit 1h OI ~667d (Binance OI hist ~30d; HL none; OKX 403 here).
- Stored 400d Bybit proxy OI in research DB (never bot.db).
- Extended screen (731 price dates, ~400 OI dates): **no FDR+mono survivors**.
  Short-sample IC≈0.13 for `oi_delta_24h@24h` collapses to IC≈0.019 (p≈0.07).
- **OI/positioning family CLOSED** under proxy+power — do not build.

### Archived — tape-native CVD/OIR screen (verdict C)

`scripts/feature_screening_tape_native.py` /
`docs/FEATURE_SCREENING_TAPE_NATIVE.md` (2026-08-10):

- Real `trade_tape` + real OIR (~29 dates). Expectation: stats maybe, tradable no.
- 2 survivors (`cvd_delta_1h@1h` BE 3.64; `cvd_delta_15m@15m` BE −9.42) — both fail 11 bps.
- Closes real-tape reopen of CVD family. OIR not a survivor.

### Archived — 24m candle-only feature re-screen (verdict C)

`scripts/feature_screening_24m_candles.py` /
`docs/FEATURE_SCREENING_24M_CANDLES.md` (2026-08-09):

- Proxy DB 24m, **731** UTC dates, bar-level IC + **date-cluster** bootstrap.
- Excluded funding/OI/basis/liq/CVD (empty or short on proxy).
- Controls PASS. Five statistical survivors (`ret_lag_15m`, short-horizon vol
  width) — **all fail 11 bps taker** (best BE 6.81 bps).
- 82d artifacts confirmed dead: `atr_percentile_7d@24h`, `dow@24h`.
- `ret_lag` still real but cost-closed (BE ~2–6 bps).

**Do not build** candle directional strategies. Prefer inverted-cost families
(MM / spread) once L2 history accumulates on `E:`.

### Archived — maker fill + adverse selection on 24m survivors (verdict C)

`scripts/maker_fill_adverse_selection_24m.py` /
`docs/MAKER_FILL_ADVERSE_SELECTION_24M.md` (2026-08-10):

- Same proxy DB (731 dates). OHLC penetration fills only (no L2/queue).
- Models M1 touch / M2 ≥1 bps / M3 ≥2–5 bps sweep; results as **[M1..M3]** interval.
- Measured fill-bar / +15m / +1h adverse selection (not assumed).
- Primary `rvol_1h@4h` and secondary `ret_lag_15m@1h`: **both (C)**.
- Net (2 bps RT − AS) intervals ≈ **[-1 .. -13] bps**; AS rises with penetration
  (~0→6 bps) and turns gross BE negative from M2 onward.
- Screening BE 6.81 for rvol was always-long drift; selective median-split BE ≈1.5.
- Directional candle family **FINAL** under maker costs too. Fill/AS harness kept
  for future MM work.

### Archived — Hyperliquid MM feasibility (verdict C)

`scripts/mm_feasibility_study.py` /
`docs/MARKET_MAKING_FEASIBILITY.md` (2026-08-10):

- 31d HL `l2_snapshots` + sampled `trade_tape` (read-only); ~1–2d depth books on E:.
- Touch half-spread p50 **0.07–0.27 bps** ≪ retail maker fee **1.5 bps** → edge
  negative even with AS=0; measured AS 10s ≈ **0.8–2.5 bps** worsens it.
- No UTC hour positive; queue-ahead at touch is large (hours of wait for 1 lot on BTC).
- **Do not build MM** at this access level. Re-open only with fee tier/rebate +
  proven wider-spread fills + toxic-flow avoidance.

### Archived — MM liquidity-spectrum addendum (verdict C, definitive)

`scripts/mm_feasibility_liquidity_spectrum.py` /
`docs/MARKET_MAKING_FEASIBILITY_LIQUIDITY_SPECTRUM.md` (2026-08-10):

- 177 HL perps ranked by `dayNtlVlm`; 12 symbols REST-polled 12 min (bot untouched).
- Top: fee > half-spread. Mid: wider spread eaten by AS (e.g. CASHCAT −0.23).
- Bottom: point-positive NOT/GAS/SOPH but AS underpowered and **\$/day ≪ 1**.
- **Retail MM on HL closed across the liquidity spectrum.**

### Parked — real OIR + trade_tape window (metrics DB ≠ depth recorder)

**Do not execute yet** — register only. Source: `data/research/hyperliquid.db`
(~10/07→09/08).

`l2_snapshots` (~1.7M rows) store **derived metrics** (mid, spread_bps,
bid/ask depth USD, **OIR**) — **not** the book by levels. `trade_tape`
(~17.5M) has **true side**. Implications:

| # | Finding | Next (when scheduled) |
|---|---------|------------------------|
| a | Real OIR exists for that window → ChecklistMeta / OrderBookScalper **Tier B** (proxy corr≈0.24) may be **re-liftable for 10/07–09/08 only** | Re-gate / re-measure with real OIR, not candle proxy |
| b | Tape side → real CVD / taker split; CVD family was scored on candle-derived volume, not tape | Feature screen / diagnostics only — **not** a CVDOrderFlow reopen |
| c | Still **insufficient for market making** (no levels, no queue position) | Keep `l2_book_recorder` — **not** redundant |
| d | Role split: `research_microstructure` → metrics in `hyperliquid.db`; `l2_book_recorder` → depth JSONL on `E:` | Avoid duplicating the same payload in both writers |

Disk cleanup Fase 1 (2026-08-09): removed `_test_tape_*` + duplicate
`schema_peek_*` + uncited `logs/backtest_baseline.txt` (~182 MB). **Did not**
touch `hyperliquid.db`. See `docs/DISK_AND_CODE_INVENTORY.md`.

### Parked — migrate bulky research off C: SSD → E: HDD

**Do not do now** (Fases 2–4 of disk cleanup still unconfirmed). L2 book
recording writes to `data/research/l2_books`.

~4 GB still on C: that would benefit from the same HDD move:

| path | ~size | note |
|------|------:|------|
| `data/research/hyperliquid.db` | 3.3 GB | research DB — **KEEP** (high-value INVESTIGAÇÃO) |
| fills (research) | ~130 MB | after Fase 1 de-dupe |
| backtest snapshots | ~281 MB | |
| `data/live/archive` | ~104 MB | |

**Keep on SSD:** `data/live/bot.db` (~210 MB) — random access on the trading hot
path. Do not relocate operational DB with the research bulk move.

Full volume map + backup procedure: **`docs/DATA_ARCHITECTURE.md`**
(C:=ops, E:=research, D:=verified backups, VPS=ops-only).

### Parked — L2 resolution bump — **APPLIED 2026-08-10**

Applied: `interval_sec=1.0`, `depth_levels=25`, `retention_days=365`.
Fase 10/08 re-registered (assert kept) — reason: L2 recording resolution only;
does not affect signal or execution. **Restart required** for the running paper
bot to pick up the new knobs. See `docs/DATA_ARCHITECTURE.md`.

### Feature candidate — CVD divergence (NOT a strategy reopen)

`CVDOrderFlow_p90` (thr = aggregate p90 = 0.275) was gated and **FAIL**ed with
power on both folds. Strategy iteration on CVDOrderFlow is **closed** — do not
retune thresholds to chase trades.

| Fold | n | B1 PF %ile | PF | Verdict |
|------|--:|-----------:|---:|---------|
| W2 | 53 | **86** | 0.80 | FAIL (B1&lt;95 + not_profitable) |
| W3 | 71 | **86** | 0.37 | FAIL (B1&lt;95 + not_profitable) |

Enquadramento (keep this exact framing):

- The **feature** (CVD / volume-tape divergence) appears to carry **weak but
  consistent directional information** — B1 = 86 on two independent windows is
  the best directional percentile in the portfolio (ChecklistMeta was 48 / 43).
- The **strategy** built on it still loses money and fails the three-condition
  gate → **closed**. This is not authorization to reopen CVDOrderFlow tuning.
- Revisit only inside a future **feature-screening pipeline** (raw predictive
  power vs forward returns), not as another strategy parameter loop.

**Screening update (2026-08-09):** `scripts/feature_screening.py` included
`cvd_price_div_signed` / `cvd_div_strength`. Some cells clear FDR but fail
monotonicity (degenerate quintiles) and are **not** TOP survivors — same
enquadramento: weak/inconsistent as a *feature block*, not a strategy reopen.
See `docs/FEATURE_SCREENING_REPORT.md`.

Artifacts: `CVD_P90_CALIBRATION.md`, `baseline_gate_CVDOrderFlow_p90.json`.


### Archived — short-horizon mean reversion (cost test)

Screening found genuine short-horizon fade structure (`ret_lag_*`, IC≈−0.06,
monotone, stable). Cost test (`scripts/reversion_cost_test.py`,
`docs/REVERSION_COST_TEST.md`) verdict **(C)**:

- Best gross breakeven RT ≈ **4.21 bps**
  (combo `4h/4h`), vs bot taker RT **11.0 bps**.
- Taker/taker expectancy negative on all tested (L,H).
- Conservative maker (limit fill + ≥2 bps penetration; “any touch” rejected
  as ~98% vacuous) also fails; optimistic maker/maker is an upper bound only
  (green on `4h/4h` / `1h/4h` only — not counted as survival).

**Do not build** a mean-reversion strategy from this feature family at current
HL fee/slip assumptions. Re-open only if maker fills can be measured live with
fill-rate + adverse-selection stats that beat the breakeven, or if fee tier
drops enough that taker RT < breakeven.

### Archived — ret_lag fade through 24h + long-horizon cost scan

`scripts/long_horizon_cost_test.py` / `docs/LONG_HORIZON_COST_TEST.md` (2026-08-09):

- **`ret_lag` fade 15m→24h: CLOSED (not exploitable).** Short-horizon best BE was
  4.21 bps; at 12h/24h gross BE turns ≤0 / more negative. Do not build a fade
  strategy from `ret_lag_*`.
- **Candidate (A):** `atr_percentile_7d@24h` BE≈34.5bps, tt≈23.5bps, n_nonoverlap=282, edge CI clears 0 on non-overlap. Vol regime — **not** reversion. Next: minimal strategy → baseline gate.
- `oi_delta_24h@24h`: overlapping BE≈19.6bps but non-overlap edge CI straddles zero — **not** awarded.
- `dow@24h`: descriptive only (~12 obs/weekday) — never a candidate.

Overall directional-price scan verdict **(A)** solely on the vol-regime signal
above — not on reversion.



### Archived — atr_percentile_7d@24h long revalidation

Long revalidation (`scripts/validate_atr_percentile_long.py`,
`docs/ATR_PERCENTILE_LONG_REVALIDATION.md`) verdict **(C)**:

- Short-sample BE ≈ 34.5 bps looked like (A); long sample /
  date-block inference does not sustain a clear post-cost edge across regimes.
- **Do not build.** Likely regime artifact of the original ~83-day window.



### Archived — regime mismatch (do not fake-run)

These are not bugs; they require market conditions this venue rarely shows.
Keep out of active tuning. Re-open only when the evidence gate trips.

#### SpotPerpCarry
- **Requires:** `predicted_funding ≥ 0.0005` (8h-eq) and R:R≥1 vs 2% basis stop
  ⇒ effective funding_8h ≳ 0.0067.
- **Observed (14d):** `predicted ≥ 0.0005` in **0.012%** of samples;
  `predicted ≥ 0.0067` in **0%**.
- **Evidence gate to reopen:** 30d mean |predicted| funding ≥ 0.0008 **and**
  at least 5 days with predicted ≥ 0.0005; then re-shadow and require n≥30
  before any baseline gate.

#### FundingMomentum
- **Requires:** funding **side flips** across `funding_flip_threshold` (±0.0001).
- **Observed:** live skips almost entirely “same side as previous”; flips
  effectively absent in the current near-zero same-sign regime.
- **Evidence gate to reopen:** ≥8 distinct funding sign flips across the
  asset set in 30d (logged), then shadow + n≥30.

#### FundingArbitrage
- **Requires:** cross-symbol funding pair with usable net spread after costs.
- **Observed:** rare EMIT bursts (e.g. 2026-08-08); unlikely to reach n≥30
  in a quarter at current frequency.
- **Evidence gate to reopen:** ≥30 pair-scan opportunities in 90d (shadow
  would-enter count), then baseline gate. Until then: **frequency insufficient**.

### Live/replay parity notes (2026-08-08)

- **RiskManager sim clock:** all day-boundary circuits use `set_sim_time` /
  `_utc_day()` in backtests (wall-clock `utc_now` left permanent stop-streak
  trips across multi-day folds).
- **Replay coverage/gap:** disabled when `backtest.replay_data_quality.parity_mode`
  is true (default) — no live equivalent.
- **Warm-up:** `backtest.warmup_15m_bars` (default 110) loads bars before
  `start_ms`; entries only after trade start.
- **OIR proxy (ChecklistMeta):** calibrated vs 265 live `entry_oir` values —
  corr≈0.24, gate agree≈56% → **Tier B** in candle replay. Proxy stays ON
  (live has real L2 OIR; `None` is not closer). `w_oir=0.5` on threshold≈4.0
  ≈12.5% of score may bias fills. Do not disable production `oir_gate`/`w_oir`.

---

## 1. Strategy hypotheses from live-trade observation

Raised after reviewing trade #315 (VWAPDeviation HYPE long, −$204, stop
honoured exactly as configured — mechanically correct, but the entry timing
is questionable). These are **hypotheses, not diagnoses**: they look smart
in hindsight, which is precisely why they need out-of-sample testing rather
than intuition.

| # | Hypothesis | Rationale | Status |
|---|---|---|---|
| 1.1 | **Deceleration confirmation** — don't enter on the threshold cross; wait for the z-score to stop making new extremes (e.g. retraced ≥0.15σ off the low, or a 5m candle closing against the move) | Trade #315: z went −1.81 → −2.43 → −2.51 in 6 minutes. The rule fired mid-impulse. Trades worse entry price for better win rate — may well be net negative, hence "test, don't assume" | Blocked on Fase 06 OOS (needs certified data) |
| 1.2 | **Invert the volume filter for mean reversion** — currently requires volume ≥1.5× as "confirmation"; for reversion we arguably want *exhaustion* (seller volume dying), not confirmation of the move | We persist `buy_volume`/`sell_volume`, so this is directly testable. The Fase 2 liquidation study pointed the same way: 12/15 flushes continued short-term | Blocked on Fase 06 OOS |
| 1.3 | **Per-symbol thresholds** — a single 2.5σ threshold treats BTC and HYPE as the same animal; HYPE plausibly needs ≥3σ | Thin-book assets stretch further before reverting | Blocked on Fase 06 OOS |
| 1.4 | **Liquidity-map veto** — don't take a long when a long-liquidation cluster sits between entry and stop (the magnet pulls price exactly where we die) | Depends on the liquidation map having proven predictive value first (see §3) | Blocked on §3 |
| 1.5 | **VolatilityBreakout follow-through** — require the candle *after* the breakout to hold above the range before entering | The `failed_breakout_below_mid` exit already rescues some (trade #314: +$44.65), but not entering the false break is strictly better | Blocked on Fase 06 OOS |
| 1.6 | **Breakout fuel from liquidation clusters** — a breakout heading *into* a short-liquidation cluster has forced buying behind it; one with nothing ahead tends to fizzle | Same infrastructure as 1.4, applied in the opposite direction | Blocked on §3 |

## 2. ORB — known defect

`src/strategies/opening_range_breakout.py` (research-only, not wired to the
live bot).

**Defect:** the volume filter compares the breakout candle against the 20
preceding candles — which, at the NY open, are the dead pre-open period.
The 1.5× threshold therefore passes almost trivially and filters nothing.
This plausibly explains part of the terrible smoke result (9% win rate, 11
trades — mechanical smoke only, never performance evidence).

**Fix to test:** compare against the historical volume *for the same
minute-of-session on prior days*, not the immediately preceding candles.

Cheap, research-only, no dependency on the frozen window. Reasonable next
action whenever ORB work resumes.

## 3. Liquidation map — Phase 3 gate

Phases 1 and 2 are done (`docs/LIQUIDATION_MAP_PHASE2_FINDINGS.md`).
Verdict: **do not build a strategy yet.** Approach A found a genuine
liquidation marker in the archive but only N=15 target-coin events from one
hour (12/15 flush continuation, 0/15 reversal — descriptive only, not
significant). Approach B (forward tracking) has a handful of snapshots and
zero evidence by construction.

**Gate to revisit:** dozens–hundreds of liquidation events (expand Approach
A across many archive hours) plus dozens of forward zone-approach events.
Only then does §1.4 / §1.6 become testable.

Note from the live run: confluence (≥2 overlapping positions per zone)
appears once the address sample is large enough — 25/155 zones at 572
addresses vs 8/68 at 300. Sample size, not time window, was the binding
constraint.

## 4. Options-derived signals (NOT options trading)

**The idea worth keeping:** implied volatility and put/call skew are
historically among the better predictors of spot movement and market
stress. Using IV as an *input* to the existing perp strategies is a
different (and much cheaper) proposition than trading options.

**Where the data should come from — Deribit, not Hypercall.** Deribit has
historically carried the large majority of global BTC/ETH options volume,
publishes a free public API, and provides DVOL (a BTC implied-volatility
index). Mature market, liquid quotes, no experimental SDK in the path.

**On Hypercall specifically** (evaluated 2026-07-22, user-raised):
- On-chain options protocol on Hyperliquid, built by the Synapse team;
  fractional options, RFQ, multi-leg builder.
- Docs state "Mainnet Alpha is live" (as of 2026-07-22).
- The Rust SDK (`github.com/hypercall-public/hypercall-rust`) was at 2
  stars / 1 fork / 1 commit / no release tag, with the README itself
  advising to pin to a tag "once a release tag is available".
- **Statistics caveat:** the widely-quoted "$55B volume / 2.5M users"
  figures are **Hyperliquid ecosystem numbers, not Hypercall's own options
  volume**, which does not appear to be published. Public Hyperliquid data
  is roughly 1.4M users / ~$2.95T cumulative volume. Do not treat ecosystem
  reach as evidence of options-market liquidity.
- Language mismatch: the bot is Python; the SDK is Rust (FFI bridge or
  reimplementation required).

**Why not now, regardless of venue:** trading options requires volatility
surface modelling, greeks and decay handling — a materially harder problem
than directional perps, and the bot has not yet demonstrated edge on the
easier problem (9/100 trades into the frozen window, no strategy validated
out-of-sample). New options venues also tend to have wide spreads and thin
depth, which eats theoretical edge before it materialises.

**Review triggers (both required):**
1. The bot has a closed Fase 10 window with at least one strategy showing
   proven edge.
2. A liquid options data source is available and worth reading — Deribit
   qualifies today; Hypercall would need published, observable options
   volume (not ecosystem stats).

First step when revisited is **IV as a signal**, not options execution.

---

## Discipline note

The recurring pattern this file guards against: a compelling new idea
arrives (a well-known trader's setups, a liquidation map, an options
protocol) while the current work is still mid-validation. Each has been
genuinely interesting. The rule that has served this project well is one
idea at a time, proven with numbers, without abandoning what is already
under test.
