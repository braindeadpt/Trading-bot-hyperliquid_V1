# Trading literature — curated source map (2026-09-16)

Legitimate free sources only: author papers, university lecture notes,
central-bank working papers, author blogs. No pirated books — the same
content exists legally for every target author.

Mapped to our data assets: candles (exhausted class), L2 books
(accumulating), liquidation tape, top-trader bias, funding, DVOL.

---

## Tier 1 — Hyperliquid-native research (our exact venue)

These papers study OUR exchange with wallet-level data — the strongest
prior we have that proprietary-data edges exist here.

| Source | Link | What it gives us |
|---|---|---|
| **Public Trader Identity: Adverse Selection and Return Predictability** (Aug 2026) | arxiv.org/pdf/2608.04373 | 17.1B L4 messages, 147k wallets. Wallets ranked by post-trade price impact KEEP their rank across windows (rank corr 0.52) and adding top-wallet flow raises OOS R² on 1s returns by 13.2%. **Direct academic validation of the TopTraderFlow/bias axis** — the signal exists; our job is extraction at our latency horizon |
| **Binance Leads, but Some Wallets Anticipate** (2026) | doi.org/10.21203/rs.3.rs-10147582/v1 | Binance leads HL in price discovery at every window, but a *persistent minority* of HL wallets anticipate. Lead-lag + Hasbrouck methodology we can replicate with our feeds |
| **Trading in the Sunshine or in the Shade** (Jun 2026) | arxiv.org/abs/2606.15715 | Protocol-native TWAPs on HL are visible: hidden metaorders are front-loaded/U-shaped; visible TWAPs get lower costs (-9bps temp impact). Execution-side edge, and the dataset exists publicly |
| **An Open Book: L4 Order Book Data from Hyperliquid** | Zenodo dataset | Full L4 book reconstruction (order/cancel/reject/fill per wallet). Our L2 collector is a subset of this — shows what the full signal surface looks like |
| **Liquidation-Aware Market Making (Oct-2025 cascade thesis + replication pkg)** | doi.org/10.5281/zenodo.20328477 | VPIN flow-toxicity panels for HL, liquidation-aware MM. Replication data public — closest prior to our liquidation tape |

## Tier 2 — Market microstructure (the untested signal class)

| Source | Link | What it gives us |
|---|---|---|
| **Drissi, Oxford Maths — Market Microstructure & Algorithmic Trading lecture notes (2024)** | faycaldrissi.com/files/HFT_2024___Oxford___lecture_notes_2024.pdf | Complete Cartea-Jaimungal course: Almgren-Chriss execution, optimal MM, *§7 optimal trading with predictive signals — "Imbalance and MACD", "Order flow"*. The textbook without the price |
| **Cont, Cucuringu, Zhang — Price Impact of OFI (multi-level)** | arxiv.org/abs/2112.13213 + SSRN 1712822 | OFI → price impact is linear, slope ∝ 1/depth; multi-level OFI integrates via PCA; lagged cross-asset OFI predicts returns (decays fast). **The canonical L2 signal** — maps to our orderbook snapshots |
| Cartea, Jaimungal, Penalva — *Algorithmic and HFT* (2015) | covered by Drissi notes + Cambridge frontmatter | Inventory-skewed quoting, adverse selection as flow informativeness |

## Tier 3 — Statistical discipline (validates + upgrades our pipeline)

| Source | Link | What it gives us |
|---|---|---|
| López de Prado — SSRN page (all papers) | papers.ssrn.com (per_id=434076) | Deflated Sharpe (2460551), Probability of Backtest Overfitting (2326253), 10 Reasons ML Funds Fail (3104816) — our gates already implement the discipline; DSR adds a Sharpe-significance check to verdicts |
| **Joubert — Meta-Labeling: Theory and Framework** | SSRN 4032018 | Consolidated meta-labeling recipe: primary model picks side, secondary ML filters whether-to-trade + sizes. The ONE technique that plugs into our existing strategies without needing a new signal |
| paperswithbacktest.com course pages | free | worked triple-barrier + meta-labeling walkthroughs |

## Tier 4 — Perp-specific mechanics

| Source | Link | What it gives us |
|---|---|---|
| **Angeris et al — Fundamentals of Perpetual Futures** | arxiv.org/abs/2212.06888 | No-arb pricing bounds for perps with costs; implied funding-arb strategy with high Sharpe *in theory* — we measured why it dies in practice (spot leg costs). Reference for funding-momentum variants |
| **BIS WP 1087 — Crypto carry** | bis.org/publ/work1087.pdf | Carry up to 40% p.a., driven by retail trend-chasers + arbitrage-capital limits. Structural explanation of WHO pays the funding — i.e. who we fade |
| Two-tiered funding markets (Math 2026) | doi.org/10.3390/math14020346 | CEX→DEX information flow is one-directional; only 40% of ≥20bps funding spreads survive costs — matches our SpotPerpCarry CLOSED verdict |

## Tier 5 — Practitioner process (free blogs by the authors)

| Source | Link | What it gives us |
|---|---|---|
| **Robert Carver — qoppac.blogspot.com** | "My trading system" post + vol-targeting series | Forecast→position pipeline, vol targeting (SR≈vol target rule), vol attenuation for momentum (cut size when vol high), handcrafted forecast weights. Position-management > signal — his own claim |
| **Ernie Chan — epchan.blogspot.com** | blog archive | First question is always "is this market mean-reverting or momentum-driven" — regime classification BEFORE strategy choice (our Q11/Q12 result confirms: momentum only paid in trending W4) |
| Better System Trader podcast transcripts | bettersystemtrader.com | practitioner process interviews |

## Deliberately excluded

- **fmzquant/TradingView-strategy books & blogs** — exhausted class (~40 dead cells), candle-derived only
- **Market Wizards-style interview books** — valuable discipline reading but zero codifiable rules
- **Connors et al short-term systems** — we already ported and killed one (Q9 sma_rebalance)

---

## Extraction plan (next step)

For each Tier-1/2 source, extract a falsifiable rule → preregister in
QUEUE.md → light_replay/maker harness. Priority order:

1. **Wallet-flow signal** (Public Trader Identity) — validates TopTraderFlow
   direction; our bias feed is the coarse version already collecting
2. **OFI on our L2 snapshots** — needs L2 coverage back (feed stale ~08-10,
   regates when bot uptime accumulates depth)
3. **Meta-labeling on existing signals** — ML filter over VWAPDeviation/
   LiquidationCatcher shadow signals; needs no new data, only labels
4. **Vol targeting / forecast scaling** (Carver) — risk-layer upgrade,
   applicable to whatever survives
