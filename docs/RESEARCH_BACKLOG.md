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

Last updated: 2026-08-08

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
