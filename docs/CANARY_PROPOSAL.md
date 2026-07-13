# Canary Configuration Proposal (Fase 10) — PROPOSED, NOT APPLIED

*Generated 2026-07-13. Every current-config value cited below was read
directly from `config/settings.yaml` in this session — see file/line
references inline. This document proposes a configuration; it does not
change `config/settings.yaml` or any other file, and no script in this repo
applies it automatically.*

---

## Status: proposal only

> **Nothing in this document is active.** The YAML snippet in §6 is a diff
> to review, not a change that has been made. Activation requires **both**:
> 1. The Fase 10 gate (`python scripts/phase10_check_gate.py`) showing a real
>    **PASS** on all four criteria — `min_trades ≥ 100`,
>    `profit_factor ≥ 1.20`, `expectancy_r > 0.0`, `max_drawdown_pct ≤ 5.0` —
>    not "insufficient data," an actual PASS. See `docs/MAINNET_READINESS.md`
>    for the current (FAIL/INSUFFICIENT_DATA on all four) status.
> 2. Explicit human confirmation on top of that gate PASS.
>
> Mainnet auto-activation is never performed by any script in this
> repository. This proposal exists so the target configuration is written
> down and reviewable *before* that decision point, not to pre-empt it.

---

## 1. Risk per trade — proposed 0.05–0.10% of equity

**Current config** (`config/settings.yaml:69`, read this session):
```yaml
risk:
  per_trade_risk_pct: 1.0   # % of capital at risk per trade
```

**Proposed canary value: 0.05–0.10%** — a 10x to 20x reduction from the
current 1.0%. This is the single largest lever in the canary: it caps the
dollar impact of any one bad trade to a small fraction of what the bot risks
today, while the strategy set is proven live on real (if small) capital
rather than paper.

---

## 2. Daily loss limit — proposed 0.3–0.5%

**Current config** (`config/settings.yaml:66`, read this session):
```yaml
risk:
  max_daily_loss_pct: 3.0   # circuit breaker trigger
```

Note there is also a stricter mainnet override already defined
(`config/settings.yaml:930`, under `mode_overrides.mainnet.risk`):
```yaml
mode_overrides:
  mainnet:
    risk:
      max_daily_loss_pct: 2.0     # down from 3.0
      max_position_size_pct: 3.0  # down from 5.0
```
So mainnet already tightens this from 3.0% to 2.0% today — but that is still
4–6.7x looser than the proposed canary range.

**Proposed canary value: 0.3–0.5%** — a further ~4-10x reduction versus the
existing mainnet override (2.0%), and ~6-10x versus the base config (3.0%).
At canary size this caps a single bad day to a very small, pre-agreed loss
before the daily circuit breaker halts entries.

---

## 3. Exactly one strategy — recommend VWAPDeviation

Both currently live-executing strategies are `paper_only: true`
(`config/settings.yaml:673`, `strategy.phase08.paper_only`) and neither has
been proven out-of-sample. The canary proposal is to run **exactly one** of
the two, not both.

**Historical paper-trading data point** (`docs/BASELINE_V3_1_47.md`, table
dated 2026-05-25 → 2026-07-09):

| Strategy | Trades | PnL USD | Win rate | Profit factor |
|---|---|---|---|---|
| VWAPDeviation | 7 | +$3.10 | 85.7% | 4.27 |
| VolatilityBreakout | 8 | −$109.24 | 12.5% | 0.01 |

That table is now stale. Re-queried `data/live/bot.db` directly on
2026-07-13 (full history to date, same two strategies):

| Strategy | Trades | PnL USD |
|---|---|---|
| VWAPDeviation | 8 | +$15.62 |
| VolatilityBreakout | 10 | −$96.96 |

Direction is unchanged (VWAPDeviation positive, VolatilityBreakout
negative) but the exact figures moved between the two snapshots — a
reminder that this is a moving, small-sample number, not a fixed fact.
Neither snapshot counts toward the Fase 10 gate: both predate the frozen
window (`window_start_ms` = 2026-07-13, current window trade_count = 0
per `scripts/phase10_check_gate.py`).

**Recommendation: VWAPDeviation**, based on this data — it is the only one
of the two currently-live strategies with positive historical PnL in both
snapshots.

**Explicit caveat — do not overstate this:** 7 trades is nowhere near
statistically significant. A profit factor of 4.27 on 7 trades and an 85.7%
win rate can trivially be noise; the Fase 10 gate itself requires ≥100
trades before treating profit factor as meaningful for exactly this reason.
This recommendation is a directional data point to break a tie between two
similarly-unproven strategies, not evidence that VWAPDeviation has a proven
edge. VolatilityBreakout's −$109.24 over 8 trades is also too small a sample
to conclude it has negative edge, though it is the weaker of the two
candidates by every metric in this table.

---

## 4. Kelly sizing — DISABLED

**Current config** (`config/settings.yaml:640`, read this session):
```yaml
strategy:
  kelly:
    enabled: false   # Phase08: Kelly out of execution until OOS proven
```

Kelly sizing is **already disabled** in the current config — the canary
proposal keeps it that way (no change needed here). This is listed
explicitly because the Fase 10 brief calls it out as a requirement, and it's
worth confirming in writing that the current state already satisfies it
rather than silently assuming so.

---

## 5. Scale-up rule — proposed NEW mechanism, not yet implemented

**Proposed rule:** after each approved block of 50 canary trades, position
size may increase by at most +25%, subject to human review/approval before
each step-up (not automatic).

**Current state, verified this session:** no such mechanism exists today.
`src/core/kelly_sizer.py`'s `KellySizer` computes a position-size multiplier
purely from rolling trade statistics (`min_trades`, `lookback_trades`,
`max_multiplier`/`min_multiplier` bounds) — it has no concept of "blocks of
N trades requiring separate human approval before scaling," and it is
disabled anyway per §4. `src/core/risk_manager.py` was also checked; it has
no block-based or approval-gated scale-up logic. **This is a new control
that would need to be built for the canary phase** — most likely as a
config flag plus a simple counter/gate in the risk manager (block trade
count, cap the per-block size increase, require an explicit config change
or operator action to advance to the next block) rather than an automatic
scheduler. It is described here as a design intent for the canary phase, not
as something already working.

---

## 6. Proposed YAML diff (NOT applied)

The following is what would change in `config/settings.yaml` if/when a human
decides to activate the canary. This is a proposal for review — it has not
been written to the file.

```diff
 risk:
   initial_capital: 10_000.0
   max_positions: 3
   max_daily_trades: 0
-  max_daily_loss_pct: 3.0           # circuit breaker trigger
+  max_daily_loss_pct: 0.4           # CANARY: 0.3-0.5% range, pick one value before activation
   circuit_breaker_drawdown_pct: 10.0
   circuit_breaker_recovery_pct: 50.0
-  per_trade_risk_pct: 1.0           # % of capital at risk per trade
+  per_trade_risk_pct: 0.075         # CANARY: 0.05-0.10% range, pick one value before activation
   max_position_size_pct: 5.0
   leverage_max: 10.0
   ...

 strategy:
   kelly:
-    enabled: false                 # Phase08: Kelly out of execution until OOS proven
+    enabled: false                 # CANARY: stays disabled — no change
   ...

   phase08:
     enabled: true
-    paper_only: true               # tier_a_hl_ohlc = data cert only; VB/VWAP paper until Phase06 OOS
+    paper_only: false              # CANARY: live execution begins — requires gate PASS + human confirmation first
     execution_strategies:
-      - VolatilityBreakout
-      - VWAPDeviation
+      - VWAPDeviation              # CANARY: exactly one strategy; VolatilityBreakout removed for canary phase
     shadow_strategies:
       - CVDOrderFlow
       - OrderBookScalper
       - FundingArbitrage
       - FundingMomentum
       - SpotPerpCarry
       - ChecklistMeta

+  # CANARY: proposed new mechanism, NOT YET IMPLEMENTED in src/core/risk_manager.py
+  # or src/core/kelly_sizer.py as of 2026-07-13. Needs to be built before this
+  # section has any effect.
+  canary_scale_up:
+    enabled: false                 # flip only once the mechanism is actually implemented
+    trades_per_block: 50
+    max_increase_per_block_pct: 25
+    require_human_approval_per_block: true
```

Both `per_trade_risk_pct` and `max_daily_loss_pct` are shown here as
specific point values (0.075%, 0.4%) purely as an example inside the stated
ranges (0.05–0.10% and 0.3–0.5% respectively) — the exact value within each
range is a decision for whoever activates the canary, not fixed by this
proposal.

---

## Summary table

| Parameter | Current (`config/settings.yaml`) | Proposed canary | Change |
|---|---|---|---|
| `risk.per_trade_risk_pct` | 1.0% | 0.05–0.10% | ~10-20x reduction |
| `risk.max_daily_loss_pct` | 3.0% (2.0% under `mode_overrides.mainnet`) | 0.3–0.5% | ~4-10x reduction |
| `strategy.phase08.execution_strategies` | VolatilityBreakout, VWAPDeviation (2, both paper-only) | VWAPDeviation only (1, live) | 2 → 1 strategy |
| `strategy.kelly.enabled` | `false` | `false` (unchanged) | none |
| Scale-up rule | none | +25% max per approved block of 50 trades | new, not yet implemented |
| `strategy.phase08.paper_only` | `true` | `false` | paper → live (canary capital only) |

**Reminder:** none of the above is applied. Activation requires Fase 10 gate
PASS (all 4 criteria, see `docs/MAINNET_READINESS.md`) plus explicit human
confirmation.
