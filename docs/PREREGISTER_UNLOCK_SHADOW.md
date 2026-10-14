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

## 7. Isolation contract (tested)

- Package imports nothing from `src.core` / `src.strategies` /
  `src.dashboard` / `src.backtest` / engine data paths.
- Writes only `data/research/unlock_shadow.db` (own `unlock_*` tables).
- Reads no `settings.yaml` / `.env` — config-hash neutral by construction
  (the CryptoRank key is read straight from `os.getenv`, never loaded
  through the config layer).
- Places zero orders — hypothetical ledger rows only.
