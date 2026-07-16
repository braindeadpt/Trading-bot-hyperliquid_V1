# Liquidation Map — Phase 2 Findings (reaction validation)

*Generated 2026-07-16. Research-only. No strategy was built. Nothing in this
document changes `config/settings.yaml` or live bot wiring.*

---

## Bottom-line recommendation

**Do not build a Fase-3 liquidation-zone strategy yet.**

Approach A (retrospective liquidation fills) found a real marker in the
archive and produced a measurable flush pattern on **N = 15** target-coin
events from a **single hour** of fills — too small for statistical claims.
Approach B (forward-tracking Phase-1 open-position zones) has **4 snapshots /
2 zone-approach events** and yields **zero evidential weight today**.

Accumulate more Approach A hours (tens to hundreds of liquidation events
with trusted candles) **and** run hourly Phase-1 snapshots for ~5+ days
before reconsidering a strategy. A wrong-but-confident go here would feed
bad premises into Fase 10's canary.

---

## 1. Methodological trap (restated)

Phase-1 `liquidation_map_snapshots` store **open positions as of now**.
They cannot answer “did today’s zones predict last week’s move.” Phase 2
therefore used:

| Track | Data | Evidence class today |
|-------|------|----------------------|
| **A — retrospective** | Forced liquidation fills in `node_fills_by_block` + trusted 1m candles | Descriptive small-N only |
| **B — prospective** | Persist zones going forward, score later approaches | Scaffold; **no evidence yet** |

---

## 2. Approach A investigation — raw fill fields

### What we inspected

- Repo fixture `tests/fixtures/node_fills_by_block_sample.ndjson` — synthetic;
  **no** `liquidation` field (by design; Phase-1 address harvest only).
- Real archive: `data/research/fills/20260715_14.lz4`
  (`node_fills_by_block/hourly/20260715/14.lz4`, ~62 MB, downloaded this
  session via `scripts/download_recent_fills.py`).

### Fields found (real hour)

Fill dict keys observed on liquidation-tagged fills include:

`coin`, `px`, `sz`, `side`, `time`, `dir`, `tid`, `oid`, `crossed`, `hash`,
`startPosition`, `closedPnl`, `fee`, `feeToken`, `twapId`, `deployerFee`,
**`liquidation`**.

The `liquidation` object looks like:

```json
{
  "liquidatedUser": "0x4c643d09ece75906a99382a3c2051bd60cba44f6",
  "markPx": "18.685",
  "method": "market"
}
```

**There is no** `dir` string of the form `"Liquidated Long"` / `"Liquidated Short"`.
Liquidations appear as ordinary `dir` values **plus** the `liquidation` object.

In that one hour:

| Metric | Count |
|--------|------:|
| Fills carrying `liquidation` (all coins) | 2940 |
| `dir` among those | Close Long 1403, Open Long 852, Close Short 552, Open Short 66, Short > Long 66, Long > Short 1 |

Dedup rule used for analysis: keep the leg where
`address == liquidation.liquidatedUser` and `dir` ∈ {`Close Long`, `Close Short`}
so liquidated side is unambiguous.

**Verdict:** Approach A is viable. Marker is reliable enough to implement.

---

## 3. Approach A — measured reactions (exact sample)

### Sample definition

- Fills file: **one** hourly object (`20260715/14`).
- Coins: BTC, ETH, SOL, HYPE.
- Events after user-leg + Close Long/Short filter: **15**
  (BTC 6, ETH 6, HYPE 3, SOL **0**).
- Side mix: **12 short** / **3 long** liquidations.
- Candles: research DB `candles_1m`, source **`hl_ws_1m_tape_agg` only**
  in-window (GoldRush deliberately excluded).
- Windows: flush = **5 minutes**, reverse = **30 minutes**; thresholds **0.05%**
  on 1m close returns from entry.

### Results (descriptive — not inferential)

| Metric | Value |
|--------|------:|
| Events with trusted candles | **15 / 15** |
| Flushed (continuation in liq direction) | **12 / 15** (80%) |
| Reversed (opposite move by 30m vs entry) | **0 / 15** (0%) |
| Mean flush return | +0.215% |
| Mean reverse-window return | +0.053% |

By coin (same caveat — tiny N):

| Coin | n | flushed | reversed |
|------|--:|--------:|---------:|
| BTC | 6 | 6 | 0 |
| ETH | 6 | 6 | 0 |
| HYPE | 3 | 0 | 0 |

Interpretation tempered by composition: **12/15 events were short liquidations**
during an hour where price tended to stay elevated — flush-up is easy to
satisfy; a 30m “reverse dump below entry” is hard. This is **not** proof that
liquidation clusters “magnetically reverse.” It is a one-hour description.

Same-hour proxy clusters (liquidation *print* prices, min 2 events/bucket)
were computed for opposite-side distance metadata; with N=15 they do not
support confluence claims.

### Honesty clause (mirrors `docs/CANARY_PROPOSAL.md` discipline)

**N = 15 from one hour is not statistically significant.** Rates above are
sample descriptions. Do not extrapolate to other hours, regimes, or coins.
Do not treat flush_rate=0.8 as a strategy edge.

---

## 4. Approach B — forward scaffold (zero evidence today)

| Metric | Value |
|--------|------:|
| Distinct Phase-1 snapshots in research DB | **4** |
| Zone rows | 489 |
| Zone-approach events scored | **2** |
| Reactions among those 2 | reverse=2 (anecdotal noise) |
| `evidence_ready` | **false** |

### How to accumulate truth (manual schedule — not installed by this phase)

Windows Task Scheduler / cron example (operator runs; agents must not create
the OS task):

```text
# Hourly — reuse Phase 1 CLI (requires local fills for that hour)
python scripts/download_recent_fills.py --hours 1
python scripts/build_liquidation_map.py --from-fills <downloaded.lz4> --execute --max-distance-pct 50

# After accumulation, score:
python scripts/analyze_liquidation_reactions.py --execute --forward-only
```

### Minimally meaningful sample (order-of-magnitude)

`estimate_sample_need()` default: **~50 zone-approach events**.
At ~24 snapshots/day and ~0.5 approaches/snapshot → **~5 calendar days** of
hourly logging. Even at N=50, treat as exploratory — not strategy-grade.
Prefer also expanding Approach A across many archived hours in parallel.

---

## 5. What was implemented

| Path | Role |
|------|------|
| `src/research/liquidation_reaction_analysis.py` | Extract liq events, cluster, measure flush/reverse, forward track |
| `scripts/analyze_liquidation_reactions.py` | CLI (`--dry-run` default; `--execute` for real scan) |
| `tests/test_liquidation_reaction_analysis.py` | Offline unit tests (synthetic math) |
| `docs/LIQUIDATION_MAP_PHASE2_FINDINGS.md` | This report |

Not touched: `config/settings.yaml`, `src/strategies/`, `data/live/` writes,
vault/credentials.

---

## 6. Recommendation detail

1. **Fase 3 strategy: NO** until (a) Approach A covers multiple days/hours with
   N ≫ 15 and stratified by side/coin/regime, and (b) Approach B has dozens of
   post-snapshot approaches with stable reverse vs accelerate rates.
2. **Keep building Phase-1 snapshots hourly** — that is the only way open-position
   maps become scientifically usable.
3. **Continue Approach A** on additional `node_fills_by_block` hours (cheap
   relative to a wrong strategy) using the same trusted candle sources.

---

*Phase 2 complete as research infrastructure + honest small-N readout.
Evidence class: retrospective_small_n + prospective_scaffold.*
