# PREREGISTER — UnlockShadow: token-unlock cliff short, forward-registered

Date: 2026-10-14
Status: FROZEN — committed before any code reads live data. The rules below
are the contract; nothing may be tuned on live observations. Any change
requires a new dated preregistration, not an edit of this file.

Hypothesis: large, scheduled cliff unlocks predictably pressure the token's
price. We short the token hedged by a beta-scaled long BTC leg around each
eligible event. This shadow study only *counts and measures* — zero real
orders, hypothetical ledger only (`src/research/unlock_shadow/`).

## 1. Point-in-time calendar

- Daily snapshot at **00:30 UTC** of two independent sources, each stored
  raw with its timestamp and sha256. Tamper-evident, replayable.
- **Source A — DefiLlama:**
  `https://defillama-datasets.llama.fi/emissionsIndex` (universe) and
  `/emissions/{slug}` (per-protocol schedule).
- **Source B — CryptoRank vesting** (public API, free key via env
  `CRYPTORANK_API_KEY`); if unavailable, **Tokenomist public** is used and
  the chosen source is recorded on every snapshot row.

## 2. Eligible event

- **Cliff unlock only** — not linear. Excluded categories:
  noncirculating / farming / staking / burned, and allocations that are
  followed on-chain.
- Size: **≥ 2%** of the unlocked (circulating-unlocked) supply.
- Cross-source requirement: the event must be present in **BOTH** sources
  in the snapshot of **T−6 or earlier**, matching within **±1 day** on the
  date and **±25%** on the amount.
- An event present in only one source = **SINGLE** — recorded and
  reported descriptively, but outside the gate (never traded).

## 3. Liquidity filter and merge

- **ADV filter:** 7-day average daily volume on Hyperliquid ≥ **$10M**,
  measured at T−5.
- **Merge:** events of the same token less than **14 days** apart merge
  into the first one (one trade covers the cluster).

## 4. Simulated trade

- **Short the token** from 00:00 UTC of **T−5** to 00:00 UTC of **T+2**
  (7-day window), **$10k notional**.
- **Long BTC hedge leg** with notional = `beta_30d × token_notional`,
  where beta is the 30-day beta of daily token returns vs BTC returns,
  clamped to **[0.5, 2.0]**.
- Prices: HL **mid at 00:00 UTC** plus estimated L2 slippage on
  Hyperliquid for a $10k order, recorded per leg.
- Costs: **4.5 bps per side per leg** (taker round-trip = 9 bps) plus
  **real HL funding** accrued on both legs over the window.

## 5. Concurrent placebo

- For each eligible event, **3 placebo windows** of 7 days on the same
  token with **no unlock ≥ 0.5% within ±14 days**, same trading rules,
  drawn at registration time with deterministic
  **seed = sha256(token, T)**.
- Placebos drawn into future windows get identical live pricing; a
  placebo later invalidated by a newly-announced unlock inside its ±14d
  exclusion is marked `invalidated`, never silently dropped.

## 6. Reading / verdict gates

- Read at **n ≥ 15 closed eligible events** or at **2027-10-14**,
  whichever comes first. Until then the status board exposes **counts
  only** (registered / open / closed / eligible vs single) — no PnL,
  no bps.
- **PASS** requires ALL of:
  1. mean net return > 0,
  2. profit factor > 1.2,
  3. mean(event set) − mean(placebo set) > 0,
  4. total net profit excluding the top-3 trades > 0.
- Fewer than 15 eligible events at expiry → **INCONCLUSIVE**.
- A single trade below **−3000 bps net** triggers a manual review flag.

## 6a. Implementation notes (2026-10-14, source-shape fixes)

Normative details of how the frozen rules map onto the live DefiLlama
payload shapes (verified against `notes/unlocks/02_events.py`
semantics). These pin *how* §1–§2 read the declared sources; they do not
change the rules themselves.

- `emissionsIndex` answers `{"data": [...]}` — the index rows are the
  `data` array (a bare list is also tolerated).
- **Token map** (per `02_events.py`): `s = tokenPrice[0].symbol` (upper);
  `coin = s` when `s` is an HL perp, else `"k"+s` when that is an HL perp,
  else a miss. `{BTC, ETH, AI}` are excluded (DefiLlama's `AI` is Gensyn,
  not HL's `AI`). `slug = protocolSlug`. The map is rebuilt inside every
  daily snapshot and stored with its sha256; misses are written to the
  ledger log.
- **Source-A events come only from `metadata.events`** of
  `/emissions/{slug}`: `unlockType == "cliff"` **and** `category` inside
  the whitelist `{insiders, privateSale, publicSale, airdrop, ecosystem,
  liquidity, community, Uncategorized, other, launch}` (all other
  categories — noncirculating, farming, staking, burned, … — are
  recorded as `excluded`, never eligible). Amount = sum of `noOfTokens`
  (nulls ignored), aggregated per UTC day. `documentedData` is the
  realized history — **never** an event source.
- **Ex-post on-chain exclusion** (§2 "allocations followed on-chain"):
  the recipient is taken from the event description
  (`unlocked from (.+?) on {` or `} of (.+?) tokens`); the event is
  excluded when a `metadata.notes` entry names that recipient and also
  documents tracking (`track` + `transfer|claim|onchain|on-chain|
  outflow|event`, case-insensitive).
- **2% denominator**: sum `unlocked` across all
  `documentedData.data[*].data[*]` per timestamp and take the **last
  available point** in the stored snapshot (in forward operation no
  later point exists). `supplyMetrics.adjustedSupply` is deliberately
  not used — it would move the threshold off the backtest baseline.
  Denominator 0/missing ⇒ `pct = None` ⇒ event ineligible, logged.

## 7. Isolation contract (tested)

- Package imports nothing from `src.core` / `src.strategies` /
  `src.dashboard` / `src.backtest` / engine data paths.
- Writes only `data/research/unlock_shadow.db` (own `unlock_*` tables).
- Reads no `settings.yaml` / `.env` — config-hash neutral by construction
  (the CryptoRank key is read straight from `os.getenv`, never loaded
  through the config layer).
- Places zero orders — hypothetical ledger rows only.

## Emenda 1 (2026-10-15)

Approved by the owner before any sim/PnL existed. Reason: source B only
exists paid (CryptoRank Pro; Tokenomist answers 401) — the dual-source
gate would yield zero eligible events forever.

- The gate becomes **single-source**: DefiLlama, point-in-time via our
  frozen snapshots.
- **ELEGIBLE** = cliff ≥ 2% (rule A unchanged), present in the DefiLlama
  00:30 UTC snapshot of **T−6**, with `first_seen ≤` that snapshot.
- Canonical date and amount = the ones in the T−6 snapshot. Later
  DefiLlama edits are ignored: the trade runs on the frozen date.
- Source B keeps being attempted, as the descriptive flag
  **`b_confirmed`**, and never enters the gate.
- Events whose T−6 predates the first real snapshot (2026-10-14) are not
  eligible. The first possible eligible T is **2026-10-21**.
- Everything else is unchanged: ADV, the T−5→T+2 window, beta [0.5; 2],
  costs, the 3× placebo, the read at n ≥ 15 or 2027-10-14, the PASS
  criteria, and the −3000 bps review trigger.
