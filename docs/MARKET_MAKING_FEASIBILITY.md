# Market Making Feasibility — Hyperliquid

Generated: 2026-08-10T01:03:11.001740+00:00
DB (read-only): `data/research/hyperliquid.db`
L2 books: `E:\hyperliquid_research\l2_books`
Window (metrics DB): 2026-07-10 → 2026-08-10 (31.0 days)

## Scope

Economic **viability** study only. No quoting logic, no strategy, no production config changes. The study is allowed to conclude that MM is **not** viable at retail access.

## Limitations (declared)

- Metrics DB covers ~1 month of derived L2 (mid/spread/depth USD/OIR) — no queue levels.
- Full depth books on E: exist only since 2026-08-09 (hours–days, not months).
- l2_snapshots sampling interval is multi-second — 1s AS is soft / next-sample.
- No measured colocated latency, cancel success, or true FIFO queue position.
- AS assumes MM was the unique counterparty at the print — overstates toxicity exposure for a small quote size.
- Inventory 'take-all-flow' is a worst-case upper bound, not expected inventory.
- Maker rebates require material share of exchange maker volume — excluded from primary equation.

## Fundamental equation

```
edge_per_fill ≈ half_spread − adverse_selection − maker_fee
edge_RT_opt  ≈ spread − 2·AS − 2·maker_fee   # both sides fill
```

Inventory cost is reported separately (not subtracted into the point estimate) because it depends on risk limits / skew — see § Inventory.

## Fees / rebate (documented, not assumed)

Source: [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) (fetched 2026-08-10).

| Context | Maker (bps/side) | Notes |
|---|---:|---|
| **Perps tier 0 base (retail)** | **1.5** | 0.015% — primary assumption |
| Tier 2 (>\$25M 14d) | 0.8 | 0.008% |
| Tier 4 (>\$500M) | 0.0 | maker only |
| Maker rebate (≥0.5% maker share) | −0.1 | **not retail-realistic** |
| Bot `execution.maker_orders.maker_fee_pct` | 1.0 | config 0.01% — **below** current HL base; do not use as truth |
| Bot directional taker RT (ref) | 11.0 | 3.5 fee + 2 slip ×2 |

Primary verdict uses **tier-0 maker 1.5 bps/side**. Rebate tiers require ≥0.5% of exchange maker volume — out of scope for this account class.

## Overall verdict: **(C)**

Equation ≤ 0 on all symbols at retail tier-0 maker fees after measured adverse selection (10s). MM does not invert the directional cost problem at this access level.

Positive tier0 point: none; CI-robust: none; tier2 point: none; tier0 with 60s AS: none.

### Structural killer (even before AS)

On all four symbols, **median half-spread < tier-0 maker fee (1.5 bps)**:

| symbol | half-spread p50 | maker fee | half − fee (AS=0) |
|--------|----------------:|----------:|------------------:|
| BTC | 0.078 | 1.5 | **−1.42** |
| ETH | 0.266 | 1.5 | **−1.23** |
| SOL | 0.067 | 1.5 | **−1.43** |
| HYPE | 0.089 | 1.5 | **−1.41** |

So adverse selection is not even required to reject retail MM at the touch: **the fee alone exceeds spread capture**. AS (0.8–2.5 bps at 10s) makes it worse. Posting *wider* than touch could raise capture but then fill rate collapses behind the queue (see competition section).

## Per-symbol equation (AS horizon = 10s)

| symbol | spread p50 | half p50 | AS 10s [CI] | fee | edge/fill [CI] | edge RT opt | +ve? |
|---|---:|---:|---:|---:|---:|---:|:---:|
| BTC | 0.155 | 0.078 | 0.821 [0.799,0.842] | 1.5 | -2.243 [-2.265,-2.221] | -4.486 | n |
| ETH | 0.531 | 0.266 | 1.263 [1.233,1.294] | 1.5 | -2.498 [-2.528,-2.468] | -4.996 | n |
| SOL | 0.134 | 0.067 | 1.280 [1.254,1.307] | 1.5 | -2.713 [-2.740,-2.687] | -5.427 | n |
| HYPE | 0.178 | 0.089 | 2.462 [2.419,2.503] | 1.5 | -3.873 [-3.914,-3.830] | -7.746 | n |

### Adverse selection by horizon

| symbol | AS 1s | AS 10s | AS 60s | median realized lag 10s (ms) |
|---|---:|---:|---:|---:|
| BTC | 0.762 | 0.821 | 0.811 | 12687 |
| ETH | 1.198 | 1.263 | 1.328 | 12699 |
| SOL | 1.194 | 1.280 | 1.343 | 12710 |
| HYPE | 2.326 | 2.462 | 2.397 | 12698 |

Positive AS = mid moved against the MM who was the trade's counterparty. l2_snapshots sample ~every 5384 ms median — the **1s** bucket is often resolved at the next sample (declare as soft).

## Hours where edge > 0 (tier-0, AS=global 10s)

- **BTC:** none (n_pos=0/24)
- **ETH:** none (n_pos=0/24)
- **SOL:** none (n_pos=0/24)
- **HYPE:** none (n_pos=0/24)

## Competition / fill realism (L2 books on E:)

Depth window: ~2.3 hours across files (started 2026-08-09). Metrics DB has **no** level sizes — queue estimates use books only.

| symbol | touch USD p50 | BBO change rate | BBO gap p50 (ms) | est queue wait (s) | est fills/h @1 lot |
|---|---:|---:|---:|---:|---:|
| BTC | 385405/544418 | 49.6% | 5443 | 11554.3 | 0.3 |
| ETH | 218451/273822 | 55.0% | 5437 | 4902.9 | 0.7 |
| SOL | 67689/39720 | 66.2% | 5413 | 4376.4 | 0.8 |
| HYPE | 2873/6290 | 80.0% | 5392 | 1336.0 | 2.7 |

## Inventory risk (tape proxies)

| symbol | max \|inv\| USD if take-all-flow | 1m \|imb\| p50 | same-sign run p50 (min) | zero-cross gap p50 (ms) |
|---|---:|---:|---:|---:|
| BTC | 32,870,531 | 1.00 | 2.0 | 2769726 |
| ETH | 11,422,263 | 1.00 | 2.0 | 279940 |
| SOL | 3,517,086 | 1.00 | 2.0 | 1170090 |
| HYPE | 5,011,433 | 1.00 | 2.0 | 12494 |

Take-all-flow inventory is a **worst-case upper bound**. Real retail MM fills a tiny fraction of tape — scale inventory risk by fill share.

## Comparison vs directional 11 bps

Directional candle strategies needed ~11 bps RT to break even and had best BE ~6.8 bps (verdict C). MM flips the sign of the spread term:

- **BTC:** MM edge/fill ≈ **-2.24 bps** vs paying 11 bps RT to harvest ~1–7 bps gross directional — better structure, but absolute edge is ≤0 at tier-0.
- **ETH:** MM edge/fill ≈ **-2.50 bps** vs paying 11 bps RT to harvest ~1–7 bps gross directional — better structure, but absolute edge is ≤0 at tier-0.
- **SOL:** MM edge/fill ≈ **-2.71 bps** vs paying 11 bps RT to harvest ~1–7 bps gross directional — better structure, but absolute edge is ≤0 at tier-0.
- **HYPE:** MM edge/fill ≈ **-3.87 bps** vs paying 11 bps RT to harvest ~1–7 bps gross directional — better structure, but absolute edge is ≤0 at tier-0.

## What would need to be true for (B)→(A)

- Tier-0 edge best on BTC = -2.243 bps — need 2.243 bps more (tighter AS, wider postable spread, or lower fee).
- Reach fee tier ≥2 (>$25M 14d) and/or staking discounts — still check: positive at tier2 on none.
- Demonstrate selective quoting that avoids toxic flow so AS(10s) falls below half-spread − fee (requires live latency + cancel skill).
- Accumulate weeks of L2 books to validate queue wait / fill rate before sizing inventory capital.

## Conclusion

Market making is **not** economically viable as a retail-tier Hyperliquid
strategy under the measured arithmetic: **touch half-spreads are ≪ maker fees**,
and measured adverse selection adds another 0.8–2.5 bps against the counterparty.
Closing this path is as valuable as a positive finding: do not build MM
infrastructure expecting the spread alone to pay. Revisit only if you can
(1) post and *fill* at widths ≫ 3 bps net of queue, (2) reach fee tiers / rebates
that zero maker cost, and (3) prove toxic-flow avoidance that cuts AS below
residual capture — none of which are given for this account class.
