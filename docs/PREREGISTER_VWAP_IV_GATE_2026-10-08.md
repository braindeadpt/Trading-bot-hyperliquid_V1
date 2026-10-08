# PREREGISTER — VWAPDeviation `iv_gate_shadow` confirmation sample

Date: 2026-10-08
Status: ACTIVE — evaluation happens once, at the end, when `indep_n >= 60`.

## Hypothesis

`VWAPDeviation::iv_gate_shadow` has positive expectancy. The discovery signal
(dedupe recheck, `shadow_dedupe_recheck.py`, run 2026-10-08): raw n=158 →
indep n=30, net PF 1.337, WR 63.3%, mean +0.14%/trade. That sample is
**discovery only** and does not count toward confirmation.

## Frozen parameters (unchanged — exactly what produced the n=30)

`strategy.vwap_deviation` in `config/settings.yaml`, verbatim:

```yaml
z_threshold: 2.5
min_adx: 15.0
volume_surge: 1.5
max_adx: 25.0
require_oir_confirm: true
oir_threshold: 0.4
max_funding_opposite: 0.005
base_size_pct: 0.01
max_size_pct: 0.03
stop_loss_atr_multiplier: 2.0
take_profit_r_multiple: 2.0
max_hold_hours: 4
min_confidence: 0.70
confidence_extreme: 0.85
signal_throttle_ms: 300000
use_session_filter: true
session_start_utc_h: 7
session_end_utc_h: 22
session_allow_extreme_z: true
session_extreme_z: 4.0
exit_z_threshold: 0.3
```

Variant emission: `iv_gate_shadow` — the routed-signal IV classification
(DVOL percentile, `IV_HIGH_PCT` = 66.7, `reason="iv_gate:{high|low|unknown}"`).
Since VWAPDeviation now lives in the shadow pool, the shadow path mirrors the
same `route_phase08_signals` conditioning (regime gate `range`/`low_vol`,
dedicated sequential-contradiction guard, best-of-confidence pick) before
recording — see `TradingEngine._route_shadow_signals`.

Known sampling-frame differences vs the discovery sample (disclosed, not
fixable): signals are no longer governor-filtered, and the routing competitor
set is the shadow pool rather than the Aug–Sep execution ensemble.

## Cutoff

- Confirmation sample = `shadow_decisions` rows with
  `strategy='VWAPDeviation'`, `variant='iv_gate_shadow'`, and
  `timestamp_ms >` the committer timestamp (ms) of the git commit that adds
  this document and the shadow-list change.
- The 30 existing independent trades are excluded.

## Stopping rule

Accumulate until `indep_n >= 60` under the evaluator's independence rule
(one open simulated position per `(strategy, variant, symbol)`; a decision
counts only if `entry_ts >= exit_ts` of the last counted outcome).
No interim inspection to decide stopping — the result is read exactly once.

## Decision criterion (one shot)

On the confirmation sample:

- `net_profit_factor > 1.0` AND `net_expectancy_r > 0` → candidate for a
  follow-on protocol (not automatic promotion).
- Otherwise → **verdict C**, no second attempt, no re-tuning.

## Cost model

Tier-0 HL perps (taker 0.045%/side) + slippage + funding — the evaluator's
existing net-cost model, unchanged.

## Amendment 2026-10-08 — expiry date (added while the board is still sealed)

Every preregistered confirmation must carry an expiry. Added here before any
confirmation metric has ever been visible (the `iv_gate_shadow#confirm`
board is sealed — only counts have been exposed).

**Derivation (frozen method):** discovery window 2026-08-13 14:34 UTC →
2026-09-17 13:01 UTC = 34.94 d yielded 30 independent outcomes
→ rate 0.859 indep/day → expected time to the n=60 target
E[T] = 69.9 d → **expiry = commit_ts + 2 × E[T] ≈ 2027-02-25**
(`confirmation_expiry_ms = 1803564432000`).

**Rule:** if `indep_n < 60` when the expiry passes, the seal lifts for the
final read and the verdict is **C by rule** (insufficient evidence within
the preregistered window) — no extension, no second attempt. If `indep_n`
reaches 60 earlier, the decision criterion above applies at that read.

**Caveat (disclosed, not fixable):** the discovery rate was measured under
the pre-2026-10-08 emission path (VWAP competing in the execution ensemble)
and the Aug–Sep regime mix. The shadow-pool path may emit at a different
rate; the ×2 margin absorbs part of that, and a persistently trending
market (ADX > 25) can legitimately starve the sample — that outcome is a
valid negative result, not a malfunction.
