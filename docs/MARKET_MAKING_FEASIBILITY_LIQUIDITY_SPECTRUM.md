# MM Feasibility — Liquidity Spectrum Addendum

Generated: 2026-08-10T01:19:38.269516+00:00
Collection: 12.0 min REST poll of l2Book + recentTrades (bot/sampler untouched).
Maker fee: **1.5 bps/side** (HL perps tier-0 base 0.015%).

## Why this addendum

The primary study (`docs/MARKET_MAKING_FEASIBILITY.md`) concluded **(C)** on BTC/ETH/SOL/HYPE — the most liquid perps, where spreads are tightest. This addendum asks whether a **less liquid** region of the HL perp universe flips `half_spread − AS − fee` positive.

## Cautions (declared)

- Thin markets often have HIGHER adverse selection — informed flow concentrates there.
- Inventory neutralization is harder/more expensive when the book is thin.
- Gaps and violent moves are more frequent on low-liquidity perps.
- Low volume ⇒ few fills; edge/fill can be positive while $/day is irrelevant — we report both.
- REST window is minutes, not weeks — AS CIs are wider than the 31d primary study.
- Fill-share uses a FIFO lot/touch proxy; HL matching may differ.
- Live sampler was NOT extended; bot was not interrupted.

## Method

1. Rank all non-delisted perps by `dayNtlVlm` via `metaAndAssetCtxs`.
2. Select ~12 symbols: anchors BTC/ETH/SOL/HYPE + log-spaced ranks.
3. Poll `l2Book` + `recentTrades` for N minutes (not extending the live sampler).
4. Same equation as primary study; also extrapolate fills/day and $/day.

Universe size: **177** perps. Selected: BTC, ETH, SOL, HYPE, PUMP, CASHCAT, CRV, kNEIRO, MORPHO, NOT, GAS, SOPH.

## Verdict: **(C)**

Edge/fill point-positive on NOT, GAS, SOPH but estimated fills/day or $/day are too small to matter — and their AS samples are **underpowered** (n=2 / 14 / 10). No practically relevant MM zone across the spectrum.

### Reading the spectrum

| Zone | What happens |
|------|----------------|
| Top (BTC…HYPE) | Half-spread ≪ 1.5 bps fee → dead even at AS=0 |
| Upper-mid (PUMP, CASHCAT) | Half-spread clears the fee **if AS=0**, but measured AS ≥ half-spread → edge ≤0 (toxicity caution confirmed) |
| Mid (CRV, kNEIRO, MORPHO) | AS explodes or still eats the edge (CRV AS10 ≈ 19 bps) |
| Bottom (NOT, GAS, SOPH) | Wide spreads → **point** edge >0, but AS n too small to trust, and **$/day ≪ \$1** |

So there is **no** volume/OI threshold where the equation is both positive *and* economically material under retail tier-0 fees.

## Results by liquidity rank

| rank | symbol | day$ vlm | half p50 | AS10 [CI] | edge/fill | edge if AS=0 | fills/day est | $/day est | +ve? |
|---:|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| 0 | BTC | 784,135,262 | 0.08 | 0.42 [0.27,0.55] n=654 | -1.84 | -1.42 | 3.5 | -0.03 | n |
| 1 | ETH | 261,571,301 | 0.26 | 0.75 [0.48,1.04] n=558 | -1.99 | -1.24 | 6.0 | -0.06 | n |
| 2 | SOL | 125,038,117 | 0.07 | 0.50 [0.13,0.94] n=343 | -1.94 | -1.43 | 18.4 | -0.18 | n |
| 3 | HYPE | 87,363,799 | 0.09 | 1.25 [0.94,1.55] n=573 | -2.65 | -1.41 | 659.7 | -8.75 | n |
| 4 | PUMP | 56,120,350 | 1.80 | 3.93 [2.46,5.55] n=522 | -3.62 | 0.30 | 452.5 | -8.20 | n |
| 9 | CASHCAT | 18,602,055 | 6.31 | 5.04 [2.22,8.15] n=156 | -0.23 | 4.81 | 531.0 | -0.06 | n |
| 19 | CRV | 3,855,684 | 0.62 | 19.34 [13.44,24.70] n=57 | -20.22 | -0.88 | 204.0 | -12.07 | n |
| 40 | kNEIRO | 1,339,267 | 3.34 | 3.26 [-2.05,8.24] n=27 | -1.41 | 1.84 | 111.0 | -0.20 | n |
| 84 | MORPHO | 364,477 | 1.04 | 1.59 [nan,nan] n=18 | -2.05 | -0.46 | 84.0 | -0.25 | n |
| 174 | NOT | 44,620 | 13.68 | 0.00 [nan,nan] n=2 | 12.18 | 12.18 | 5.7 | 0.35 | Y |
| 175 | GAS | 42,850 | 4.72 | 0.18 [nan,nan] n=14 | 3.04 | 3.22 | 72.0 | 0.20 | Y |
| 176 | SOPH | 32,378 | 4.98 | -0.25 [nan,nan] n=10 | 3.72 | 3.48 | 60.0 | 0.74 | Y |

## Liquidity vs edge

Reading down the table (high → low `dayNtlVlm`): does edge/fill become positive at some volume threshold?

Yes — **point** estimates only on NOT / GAS / SOPH (day$ vlm ~\$32k–\$45k). That is **not** a viable zone:
AS sample sizes are 2–14, CIs undefined, and estimated P&L is **\$0.20–\$0.74/day** at a \$50 lot. CASHCAT (day$ ~\$19M) is the cleaner mid-spectrum lesson: half-spread 6.3 bps looks attractive until AS 5.0 bps leaves edge **−0.23**.

Also: REST collection hit intermittent **HTTP 429** — mid series are gappier than ideal; declared as a limitation (does not rescue thin-name point edges).

## Instantaneous full-universe spread sweep (context)

One-shot `l2Book` over 113/113 perps: share with half-spread > 1.5 bps = **39.8%** (n=45).

| bucket by day$ vlm | n | median half-spread | % half>fee |
|---|---:|---:|---:|
| top 10% | 11 | 0.33 | 18% |
| 10–25% | 17 | 0.82 | 18% |
| 25–50% | 28 | 1.13 | 36% |
| 50–75% | 28 | 1.07 | 43% |
| 75–90% | 17 | 1.54 | 53% |
| bottom 10% | 12 | 2.02 | 75% |

## Sampler extension cost (evaluated, not done)

Extending `research_sampler` / live subscriptions to ~12 alts would add WS `l2Book`+`trades` channels and DB write load on `hyperliquid.db`. For a one-shot feasibility answer, **REST polling for 10–20 minutes is enough** and avoids touching the paper bot. Only if a positive zone appears would a multi-day sampler extension be justified.

## Conclusion

Retail market making on Hyperliquid is **DEFINITIVELY closed** across the
liquidity spectrum tested. Wider spreads on thin names do appear, but either
(1) adverse selection cancels them (mid-spectrum), or (2) apparent residual
edge sits on illiquid names with untrustworthy AS samples and negligible
absolute dollars. Do not extend the live sampler for MM and do not build MM
infrastructure.
