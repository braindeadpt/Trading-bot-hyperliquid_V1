# Cross-Sectional Slow Momentum Feasibility

Generated: 2026-08-10T12:22:04.379652+00:00
Panel cache: `C:/Users/Braindead/Documents/trading-bot-hyperliquid/data/backtests/xs_momentum/hl_daily_panel.db`

## Scope

Measurement only — no strategy module, no production config changes, no promotion.
Object: whether **relative** multi-day momentum (long winners / short losers) clears
retail Hyperliquid costs after closed ≤24h directional / MM / OI / tape families.

## Corrections applied (vs draft prompt)

- MM map: liquid half-spread ≪ maker fee; thin books fail on AS / economics, not fee>spread everywhere.
- Primary fees = **tier-0**: taker **4.5 bps/side**, maker **1.5 bps/side** (not 3.5 unless Tier 2 proven).
- Turnover = `Σ|Δweights|/gross`; costs on traded notional only (no ×2 double-count).
- Signal close `t` → execute open `t+1`.
- Funding PIT; coverage <90% blocks verdict A.
- One a-priori PRIMARY spec (no OOS search). Sensitivities reported separately.
- Delistings force-closed on last tradable bar; no forward fill of missing data.
- Capacity: 1% ADV participation clip.

## Task 1 — Data viability

- Decision: **GO**
- Universe (excl. HIP-3): 232 (delisted flag: 55)
- With 1d candles: 232; ≥700d: 119; median days: 767
- Concurrent symbols (monthly): min **131**, max 191
- Delisted with history: 55 (survivorship includable via HL candleSnapshot)
- Funding: HL `fundingHistory` paginates to ~900d (hourly); study requires ≥90% position-day coverage

- HL candleSnapshot retains ~900d 1d history for many names from ~2024-02-22.
- Delisted names remain queryable via candleSnapshot (survivorship includable).
- No candles_1d in live research DB; panel cached under data/backtests/xs_momentum/.

## Pre-registered PRIMARY

```json
{
  "name": "xs_mom_primary_v1",
  "lookback_days": 30,
  "rebalance_days": 7,
  "n_long": 10,
  "n_short": 10,
  "min_age_days": 30,
  "min_adv_usd_20d": 1000000.0,
  "max_universe_symbols": 60,
  "vol_lookback_days": 20,
  "gross_long": 1.0,
  "gross_short": 1.0,
  "fee_taker_bps_side": 4.5,
  "fee_maker_bps_side": 1.5,
  "primary_fee_leg": "taker",
  "slip_base_bps_side": 2.0,
  "slip_illiquid_extra_bps": 5.0,
  "adv_illiquid_usd": 2000000.0,
  "participation_cap": 0.01,
  "capital_usd": 100000.0,
  "warmup_days": 60
}
```

## Frozen PASS criteria

```json
{
  "min_oos_calendar_days": 730,
  "min_oos_rebalances": 100,
  "min_random_rank_percentile": 95.0,
  "min_net_pf": 1.0,
  "require_net_expectancy_gt0": true,
  "max_abs_beta_btc_global": 0.2,
  "max_abs_beta_btc_crash": 0.3,
  "max_symbol_contrib_share": 0.35,
  "max_best_year_share": 0.7,
  "require_funding_coverage": true,
  "min_funding_coverage": 0.9
}
```

## Primary results (evaluation window)

- Window: 2024-05-03 → 2026-08-10 (830 days, 119 rebalances)
- Net total return: **-0.1279**
- Sharpe (net, √365): 0.181
- PF (net daily): 1.027
- Expectancy / rebalance: -0.00107
- Max DD: -0.4821
- Mean turnover (Σ|Δw|/gross): 0.856
- Approx ann cost drag (bps of gross): 290.2
- βBTC: 0.043 (crash days: 0.3135886729929995)
- Funding PnL (equity frac): -0.1096 | coverage 1.000
- Fee/slip PnL: -0.0917 / -0.0544
- Yearly: {"2024": 0.14891446423810994, "2025": 0.00015022177144619064, "2026": -0.2410103341881472}
- Capacity hit rate @1% ADV: 0.020780856423173802

## Baselines

- Random ranks (200 seeds): PF p50=0.947 p95=1.088; ret p50=-0.3241
- Momentum PF percentile vs random: **79.5**
- BTC buy&hold: {'total_return': 0.028787567062845243, 'n_days': 891}
- Cash: 0

## Sensitivities (not for verdict)

- `sens_lb14`: ret=1.5008 PF=1.158 Sharpe=1.02 TO=1.119
- `sens_rebal14`: ret=-0.3196 PF=1.000 Sharpe=-0.00 TO=1.142
- `sens_n5`: ret=1.3944 PF=1.136 Sharpe=0.90 TO=0.931
- `sens_maker`: ret=-0.0729 PF=1.035 Sharpe=0.23 TO=0.856

**Hard rule:** sensitivities are diagnostic only. They share the same evaluation
window as PRIMARY (no nested holdout). Picking `lb14` / `n5` after seeing these
numbers would be classic post-hoc search — forbidden by the study protocol.
PRIMARY remains the only verdict carrier.

## Verdict: **(C)**

Failed conditions: ['random_rank_pct=79.5<95.0', 'not_profitable_net', '|beta_crash|=0.3135886729929995', 'no_multi_year_positive']

### Interpretation

**FAIL** on the a-priori PRIMARY (30d lookback, weekly, 10/10, tier-0 taker).

Why this is not a near-miss worth tuning:
- Net return **−12.8%** over 830 days; 2026 alone **−24%**.
- Random-rank PF percentile **79.5** (need ≥95) — directional rank info is weak
  after costs, not “almost there”.
- Funding alone contributed **≈ −11pp** equity; fee+slip ≈ **−14.6pp**.
- Mean turnover **0.86** of gross per weekly rebalance — the economic premise
  (“low turnover so 11 bps stop dominating”) **failed for this construction**
  (~290 bps/year approx cost drag on gross).
- Global βBTC ≈ 0.04 looks fine, but crash-day β ≈ 0.31 exceeds the frozen cap.

Do **not** build a strategy. Do **not** promote. Do **not** re-run with
lookback/N fishing. Archive as another cost-killed family at retail HL access.

## Limitations

- HL public history begins ~2024-02 for many perps (~2.5y), not a multi-cycle decade sample.
- ADV from daily base volume × close; OI filter not applied (HL OI history short).
- Liquid universe = ADV≥$1M and top-60 by median ADV (pre-registered cap).
- Primary assumes taker rebalance; maker fill probability / AS not modelled here.
- Open-to-open holding returns; intraday gaps at delist approximated by last bar flatten.

JSON artifact: `C:/Users/Braindead/Documents/trading-bot-hyperliquid/data/backtests/xs_momentum/xs_momentum_feasibility.json`

