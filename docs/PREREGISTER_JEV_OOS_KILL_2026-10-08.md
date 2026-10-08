# PREREGISTER — JevJudge geometry-v2 kill criterion + random control

Date: 2026-10-08
Status: ACTIVE — the read happens once, when `indep_n >= 100` post-boundary
(or at expiry, whichever comes first).

## Scope

Executed **paper** trades of `JevJudge` (`trades` table, `status='closed'`).
The experiment stays paper-only; this document kills or continues the
experiment — it never promotes it.

## Geometry boundary (why prior trades don't count)

Commit `96f1b15` (2026-10-04 14:39:40 UTC, `ts_ms = 1791124780000`) changed
the exit geometry: SL floor 1% → 0.5%, TP 2R → 1R symmetric, maker entry.
The OOS window restarted there — new economics, new population. Only trades
with `entry_time > 1791124780000` count.

## Sample rule

- Independence: one open position per symbol — a trade counts only if
  `entry_time >= exit_time` of the last counted trade on that symbol (the
  same rule the shadow evaluator applies).
- Target: `indep_n = 100` post-boundary trades.

## Kill criterion (one shot)

At the final read (`indep_n >= 100`):

- `net PF <= 1.0` on recorded `pnl_pct` → **JevJudge off**, no second
  version, no re-tuning. The `phase08_shadow_maker` counterfactual board is
  unaffected (it answers a different question and would need its own
  preregistration to become a hypothesis).
- `net PF > 1.0` → experiment may continue; a promotion path would still
  require the full baseline-signal gate (this doc is not a promotion path).

## Expiry

Rate-based, like every preregister from 2026-10-08 on: the experiment
produced ~90 closed trades over ~17 days pre-boundary (≈5.3/day) and 12 in
the first ~2.2 days post-boundary (≈5.4/day) → E[T to n=100] ≈ 19 days →
**expiry = boundary + 2 × E[T] ≈ 2026-11-11**.

If `indep_n < 100` at expiry → verdict **C** (insufficient evidence within
the window): the experiment is revisited with the owner; the kill criterion
stays armed while it runs.

## Random control

`scripts/research/jev_eval.py` reports the Jev result against a
random-direction baseline: every real post-boundary trade is re-entered at
the same timestamp/symbol with the same reconstructed bracket
(`sl_pct = 2 × signal_metadata.atr_pct`, `tp = 1R`, maker entry + taker exit
fees) but a coin-flip side — fixed seed 42, ≥200 runs. Reported as the
percentile of Jev's modelled net PnL inside the random distribution
(50 = indistinguishable from noise). The percentile is descriptive context
for the read, not the kill criterion — the kill rule is the PF clause above.

## Reproducibility

- Judge calls pin `model: "jev-1.13.0"` (was the moving `jev-latest` alias —
  same resolved version for every call so far; pinning prevents a silent
  upstream swap mid-experiment).
- `jev_decisions.model` already stores the resolved version per row;
  `jev_latest.json` verdicts now carry `model` too, so the executed trade
  is traceable to the model version that produced it.
