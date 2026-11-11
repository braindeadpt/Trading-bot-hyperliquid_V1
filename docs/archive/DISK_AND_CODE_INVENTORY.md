# Disk & Code Inventory (cleanup proposal)

**Generated:** 2026-08-09  
**Updated:** 2026-08-09 (Fase 1 executed under explicit confirmation)  
**Bot:** paper-running — no process interruption; live/`hyperliquid.db` not touched except authorized LIXO under `data/research/`.  
**Portfolio board:** `data/backtests/parity_diag/PORTFOLIO_STATUS_BOARD.md`  
**Backlog:** `../ops/RESEARCH_BACKLOG.md`

Totals **before** Fase 1 (approx.):

| Area | Size | Files |
|------|-----:|------:|
| `data/` | **~4.1 GB** | 235 |
| `logs/` | **~286 MB** | 84 |
| `scripts/` | ~1.1 MB | 97 `.py` |
| `src/` | ~7 MB | 426 |
| `tests/` | ~5.5 MB | 346 |
| **Repo working tree (excl. `.git`)** | **~4.4 GB** | — |

### Fase 1 status — **DONE** (2026-08-09)

| Item | Action | Freed |
|------|--------|------:|
| 76× `data/research/_test_tape_*.db` | **deleted** | 22.6 MB |
| `fills/schema_peek_20260715_14.lz4` | **deleted** after SHA256 == `fills/20260715_14.lz4` | 65.4 MB |
| `logs/backtest_baseline.txt` | **deleted** — no active cite in `docs/` or `data/backtests/` (only this inventory) | 94.3 MB |
| **Total freed** | | **~182 MB** |

**Not touched:** `hyperliquid.db` (+WAL), `fills/20260715_14.lz4`, `fills_recent.lz4`, live DBs, YAML, strategy code. Fases 2–4 still pending confirmation.

---

## 1. Disk inventory (classified)

Legend:

- **OPERATIONAL** — needed by the running paper bot / recovery
- **INVESTIGAÇÃO** — may be needed again for research (esp. market-making / tape)
- **ARTEFACTO** — reproducible report/result (keep one canonical + regen command)
- **LIXO** — temp/test leftovers with no lasting value

### 1.1 Large items (≥5 MB)

| Path | Size | mtime (UTC) | Class | If removed, you lose… | Proposed action |
|------|-----:|-------------|-------|----------------------|-----------------|
| `data/research/hyperliquid.db` | **3.3 GB** | 2026-08-09 | **INVESTIGAÇÃO** | HL research tape: ~17.5M `trade_tape` rows, ~1.7M `l2_snapshots`, candles, shadow decisions, feed health. **Primary raw material for any future market-making / microstructure study.** Not live trading state. | **KEEP.** Do not class as LIXO. Optional: VACUUM/archive old partitions later — only with your explicit yes. |
| `data/research/hyperliquid.db-wal` | 16 MB | 2026-08-09 | OPERATIONAL (sidecar) | WAL for research DB while samplers write | KEEP while research sampling enabled; shrinks after clean checkpoint |
| `data/live/bot.db` | **209 MB** | 2026-08-09 | **OPERATIONAL** | Live/paper trades, candles, portfolio, OMS state | **NEVER delete** |
| `data/live/bot.db-wal` | 5.4 MB | 2026-08-09 | OPERATIONAL | Live WAL | KEEP |
| `data/live/bot_ruleset_validate.db` | 198 MB | 2026-08-08 | ARTEFACTO / INVESTIGAÇÃO | Snapshot used for ruleset validation windows | KEEP until Phase10 window closed + documented; then archive off-disk |
| `data/live/bot_backtest.db` | 81 MB | 2026-08-08 | ARTEFACTO | Backtest snapshot DB | KEEP or offsite archive; regenerable via `scripts/_make_backtest_db.py` / candle backfill if schema known |
| `data/live/archive/bot.db.bak-20260703` | 85 MB | 2026-07-03 | ARTEFACTO | Point-in-time backup | Retention policy: keep N newest; rest → cold storage |
| `data/live/archive/bot.db.backup-20260525-*` | 18 MB | 2026-05-25 | ARTEFACTO | Older backup | Same |
| `data/research/fills/20260715_14.lz4` | **65 MB** | 2026-07-16 | INVESTIGAÇÃO | Node fills archive (kept) | KEEP — twin `schema_peek_*` removed in Fase 1 |
| `data/research/fills_recent.lz4` | 65 MB | 2026-07-16 | INVESTIGAÇÃO | Recent fills extract | KEEP for MM / rebuild scripts (`download_recent_fills`, `hl_node_trades_rebuild`) |
| ~~`logs/backtest_baseline.txt`~~ | ~~90 MB~~ | — | — | Removed Fase 1 (uncited) | — |
| `logs/bot.log` (+ rotated dated) | ~195 MB | ongoing | OPERATIONAL + ARTEFACTO | Runtime history; forensics | Policy below |
| `logs/restart_v3148.txt` | 10 MB | 2026-08-08 | ARTEFACTO | Restart capture for v3.1.48 | KEEP until window closed, then archive |

### 1.2 Research DB contents (why 3.3 GB is not “junk”)

Read-only peek (`mode=ro`): **23 tables**. Dominant:

| Table | Approx rows | Role |
|-------|------------:|------|
| `trade_tape` | **17,455,451** | Tick/tape with **true side** — CVD / taker split / adverse selection (≈10/07→09/08) |
| `l2_snapshots` | 1,703,400 | **Derived metrics only** (mid, spread_bps, bid/ask depth USD, **OIR**) — **not** level book |
| `candles_*` | ~1.5M combined | Research candles |
| `feed_health_snapshots` | 304,808 | Feed quality history |
| `microstructure_gaps` | 280,964 | Gap audit |
| `shadow_decisions` | 70,198 | Shadow panel history |

Empty/near-empty in research DB: `trades`, `funding_history`, `oi_history`, `liquidation_events`, etc. (those live mainly in `data/live/bot.db`).

**Future use:** high-value **INVESTIGAÇÃO** for OIR/CVD re-evaluation on the ~1 month window; **not** sufficient alone for market-making (no levels / queue). Keep. Do not VACUUM/delete without explicit Phase 4 decision. See backlog note “Real OIR + tape window” and `../ops/L2_BOOK_RECORDING.md` role split.

### 1.3 Clear LIXO candidates

| Path | Size | Notes |
|------|-----:|-------|
| ~~`_test_tape_*.db`~~ | ~~23 MB~~ | **Removed Fase 1** |
| ~~`fills/schema_peek_*`~~ | ~~65 MB~~ | **Removed Fase 1** (SHA identical to `20260715_14.lz4`) |
| `logs/*_test*.txt`, `*_err.txt` | ~0.1 MB | One-shot test captures — still optional |
| Root `test.log`, `test_results.txt` | 0 B | Empty stubs — still optional |
| `data/tmp*` / stray YAML temps (if present) | varies | Confirm unused |

### 1.4 Logs retention policy (proposal — not executed)

Current: **84 files, ~286 MB**; `bot.log*` ≈195 MB; single `backtest_baseline.txt` ≈90 MB.

| Class | Keep online | Action |
|-------|-------------|--------|
| `bot.log` (current) | Always | RotatingFileHandler already |
| `bot.log.YYYY-MM-DD` / `bot.log.N` | Last **14–30 days** | Older → `python scripts/archive_logs.py --days 14` (gz under `logs/archive/YYYY-MM/`) |
| `security_audit_*.log` | Last **10** | Older → same archive |
| One-shot dumps (`backtest_baseline.txt`, `restart_*.txt`, `*_test*.txt`) | Case-by-case | After cited investigations closed → archive or delete with confirm |
| Cap target | **≤100–150 MB** hot logs | Rest gzipped offline |

Existing tool: `scripts/archive_logs.py` (gz + delete source). Prefer **archive** over hard delete.

### 1.5 Live DB snapshot retention (proposal)

| File | Policy |
|------|--------|
| `bot.db` | Eternal (ops) |
| `bot_ruleset_validate.db` | Keep while Fase 10 frozen window active; then cold archive |
| `bot_backtest.db` | Keep 1 current; older regenerable |
| `archive/*.bak*` | Keep **2 newest**; older → external disk |

Estimated reclaim if Phase 1–2 approved: **~150–280 MB** without touching research tape.

---

## 2. Code inventory

### 2.1 Strategies (`src/strategies/`)

Cross-check: `factory.py` registry, `settings.yaml` `enabled`, Phase08 execution/shadow, `PORTFOLIO_STATUS_BOARD.md`, `RESEARCH_BACKLOG.md`.

| Module | In factory? | YAML `enabled` | Phase08 role (live YAML) | Board / backlog | Archive proposal |
|--------|:-----------:|:--------------:|--------------------------|-----------------|------------------|
| `vwap_deviation.py` | Y | true | **execution** (sole) | grandfather INCONCLUSIVE | **KEEP active** |
| `checklist_meta.py` | Y | true | listed **shadow** (demotion pending) | FAIL → demote | KEEP; apply demote script when you confirm |
| `volatility_breakout.py` | Y | true | shadow | FAIL | KEEP shadow |
| `cvd_orderflow.py` | Y | false | shadow | superseded by p90 FAIL | KEEP (shadow wiring) |
| `cvd_orderflow_p90.py` | Y | (section via cvd) | research variant | **FAIL final — stop tuning** | Candidate for `_archive/` after unhook factory + tests |
| `orderbook_scalper.py` | Y | false | shadow | Tier B INCONCLUSIVE | KEEP |
| `lead_lag.py` | Y | false | shadow | Tier B | KEEP |
| `liquidation_catcher.py` | Y | false | shadow | Tier B | KEEP |
| `funding_arbitrage.py` | Y | true\* | shadow | **archived regime** | KEEP file; note YAML `enabled:true` contradicts KILLED comment — do **not** YAML-edit without re-register |
| `funding_momentum.py` | Y | false | shadow | **archived regime** | KEEP (shadow list) |
| `spot_perp_carry.py` | Y | false | shadow | **archived regime** | KEEP (shadow list) |
| `trend_pyramid.py` | Y | false | off | 0 wins / INCONCLUSIVE | Soft-archive candidate (still in factory/ensemble weights) |
| `range_grid.py` | Y | false | off | INCONCLUSIVE | Soft-archive candidate |
| `donchian_breakout.py` | Y | false | off | FAIL | KEEP research |
| `sfp_reversion.py` | Y | false | off | FAIL | KEEP research |
| `va_rejection.py` | Y | false | off | FAIL | KEEP research |
| `trend_follow.py` / SmartMoneyFlow | Y | false | off | FAIL | KEEP |
| `mean_reversion.py` | Y | false | off | OFF Sharpe | KEEP |
| **`vwap_trend.py`** | **N** | false KILLED | not wired | cost-test reject | **Best archive candidate** (no factory) |
| **`opening_range_breakout.py`** | **N** | n/a | not wired | backlog ORB defect | **Best archive candidate** (research-only) |
| `base.py`, `indicators.py`, `ensemble.py`, `factory.py`, `time_filters.py`, `diagnostics.py` | infra | — | — | — | KEEP |

\* `funding_arbitrage.enabled: true` in YAML but comment says KILLED; Phase08 loads it only as shadow via force. Changing YAML would invalidate frozen window — out of scope for cleanup.

#### Archive move impact (before any move)

| Target | Production imports (`src/`, `main.py`) | Tests | Preregister / reports |
|--------|----------------------------------------|-------|------------------------|
| `vwap_trend` | None in factory/engine | `tests/test_vwap_trend.py`, script `backtest_vwap_trend_vs_fade.py` | Docs + settings section comments only |
| `opening_range_breakout` | None in factory | `tests/test_opening_range_breakout.py` | `RESEARCH_BACKLOG.md` §2 |
| `cvd_orderflow_p90` | **factory + shadow_panel tests** | `test_shadow_panel.py` | `BASELINE_SIGNAL_GATE`, backlog CVD section, calibration docs |
| SpotPerp / Funding\* / TrendPyramid | **factory, engine (FundingArbitrage pair scan!), shadow lists, ensemble weights** | Many | Board + backlog — **do not move without full unwire plan** |

**Recommendation:** Phase 3 archive only **`vwap_trend`** + **`opening_range_breakout`** first (not in factory). Leave regime-archived strategies in place until shadow panel / engine paths are explicitly unwired in a separate change (and after frozen-window policy allows).

### 2.2 Scripts (`scripts/` — 97 files)

#### A. Ops / reusable (KEEP)

`archive_logs.py`, `backfill_*.py`, `deduplicate_candles.py`, `db_cleanup.py`, `reset_paper_session.py`, `run_ci_tests.py`, `seed_db.py`, `lookahead_audit.py`, `hl_research_backfill.py`, `goldrush_research_backfill.py`, …

#### B. Research / reusable (KEEP — regenerate artefacts)

| Script | Regenerates / supports |
|--------|------------------------|
| `feature_screening.py` | `FEATURE_SCREENING_REPORT.md` |
| `reversion_cost_test.py` | `REVERSION_COST_TEST.md` |
| `long_horizon_cost_test.py` | `LONG_HORIZON_COST_TEST.md` |
| `baseline_signal_gate.py` / `baseline_signal_test.py` / `baseline_portfolio_scan.py` | gate + board |
| `phase10_*.py`, `reregister_phase10_deadlock_fix.py`, `demote_checklist_meta_*.py` | frozen window ops |
| `evaluate_shadow_outcomes.py` | shadow scoreboards |
| `build_liquidation_map.py`, `analyze_liquidation_reactions.py` | liq map |
| `calibrate_*.py` | CVD/liq/spoof calibrations |
| `backtest_*_walkforward.py`, `walk_forward.py` | historical WF reports |
| `backtest_vwap_trend_vs_fade.py` | VWAP trend reject evidence |

#### C. Diagnostic / one-shot (candidates to archive under `scripts/_archive/` or `scripts/_debug/`)

Prefix `_` and `_debug/`: ~27 files (`_analyze_trades*.py`, `_investigate_sl.py`, `_parity_*.py`, `_oir_variant_*.py`, …).  
Also: `manual/manual_socketio_*.py`, `peek_node_fills_schema.py`, `diagnose_*` once superseded by v2, `check_v3_1_14.py`.

**These do not need deletion** — move-to-archive is enough to declutter `scripts/`.

#### D. Misc / review individually

`exit_economics_*.py`, `compare_exit_economics_backtest.py`, `generate_strategy_audit_report.py`, `audit_performance.py` — may still support docs; treat as research until confirmed unused.

### 2.3 Tests

98+ files under `tests/` — **do not cull** as disk savings (only ~5.5 MB). Archive strategy moves must update or relocate paired tests (`test_vwap_trend.py`, `test_opening_range_breakout.py`, `test_shadow_panel.py` for p90).

---

## 3. Safety checks (pre-removal gates)

For **every** item before any future delete/move:

| Check | How |
|-------|-----|
| Production import | `rg` in `src/`, `main.py`, `audit_all.py` |
| Tests | `rg` in `tests/` |
| Preregister / manifests | `data/research/phase10/*.json`, `phase08_preregister` |
| Active reports | `docs/*.md`, `PORTFOLIO_STATUS_BOARD.md`, backlog |
| Frozen window | **Any `settings.yaml` change invalidates Fase 10** — cleanup must not edit YAML |
| Running bot | Never delete `data/live/bot.db*` or kill processes |

### Quick results already done

| Item | Prod import | Tests | Preregister | Report refs | Verdict |
|------|:-----------:|:-----:|:-----------:|:-----------:|---------|
| `_test_tape_*.db` | no | generated by tests, not committed deps | no | no | Safe Phase 1 |
| `schema_peek_*.lz4` | no | no | no | weak | Safe Phase 1 **if** hash == fills twin |
| `logs/backtest_baseline.txt` | no | no | no | possible old notes | Phase 1 archive |
| `hyperliquid.db` | research sampler writes | network tests may use | path in config research | YES — MM future | **Phase 4 only, your call** |
| `vwap_trend.py` | not factory | yes | no | yes (killed) | Phase 3 move OK |
| `opening_range_breakout.py` | not factory | yes | no | backlog §2 | Phase 3 move OK |
| `cvd_orderflow_p90.py` | **factory** | yes | docs | yes | Phase 3 only after unwire |
| Funding\*/SpotPerp/TrendPyramid | **factory + engine** | yes | shadow YAML | board | **Defer** |

---

## 4. Phased plan (no execution without your OK)

### Fase 1 — Lixo evidente — **EXECUTED** (~**182 MB** freed)

1. ✅ Deleted `data/research/_test_tape_*.db` (76 files, 22.6 MB).  
2. ✅ Deleted `fills/schema_peek_20260715_14.lz4` after SHA256 match with `20260715_14.lz4`.  
3. ✅ Deleted `logs/backtest_baseline.txt` (94.3 MB) — cite check: only this inventory referenced it; no active report in `docs/` or `data/backtests/`.  
4. ⏸ Tiny `*_test*.txt` / empty stubs left (immaterial) — not in confirmed set.

**Did not touch:** `hyperliquid.db`, other research corpus, live DBs, strategy code, YAML.

### Fase 2 — Artefactos reproduzíveis (~**50–200 MB** depending on snapshot policy)

1. Document regen commands next to each kept artefact (already partially in docs).  
2. Offsite or delete **older** `data/live/archive/*.bak*` keeping 2 newest.  
3. Decide fate of `bot_backtest.db` / `bot_ruleset_validate.db` **after** frozen window ends (not mid-window).  
4. Deduplicate `data/backtests/*_latest.json` vs stamped copies (tiny savings).

### Fase 3 — Arquivo de código (disk ≈0; clarity++)

Move **not delete**:

| Move to `src/strategies/_archive/` | Also move |
|------------------------------------|-----------|
| `vwap_trend.py` | `tests/test_vwap_trend.py`, optionally script stays with note |
| `opening_range_breakout.py` | `tests/test_opening_range_breakout.py` |

Optional later (separate PR after unwire):

- `cvd_orderflow_p90.py` + factory entry removal  
- `scripts/_*.py` → `scripts/_archive/`

**Do not** move Funding\*/SpotPerp/TrendPyramid until engine shadow lists and `FundingArbitrage` pair scan are redesigned.

Update: `AGENTS.md` strategy count blurb if you want docs accurate — optional, not required for archive.

### Fase 4 — Dados de investigação (your explicit decision)

| Option | Space | Risk |
|--------|------:|------|
| **A. Keep full `hyperliquid.db`** | 0 freed | Correct default for MM avenue |
| **B. Cold-copy off machine + keep last N days tape** | up to ~3 GB | Loses deep history unless backup verified |
| **C. Drop only unused empty tables / VACUUM** | small | Low risk after backup |
| **D. Delete fills lz4** | ~190 MB | Breaks node-rebuild / schema work |

**Recommendation:** choose **A** until market-making is formally rejected in the backlog. Then reconsider B with a verified external backup.

---

## 5. Estimated reclaim summary

| Phase | Approx recoverable | Risk |
|------:|-------------------:|------|
| 1 | **85–180 MB** | Low |
| 2 | **50–200 MB** | Medium (snapshots) |
| 3 | ~0 MB (clarity) | Medium if factory-wired modules moved carelessly |
| 4 | **0–3.3 GB** | High — your call on MM future |

**Cumulative without Phase 4:** roughly **0.15–0.4 GB**.  
**With Phase 4 cold-archive of research DB:** up to **~3.5 GB** on the machine (if backup exists elsewhere).

---

## 6. What this pass did / did not do

- ✅ Full classified inventory (disk + strategies + scripts)  
- ✅ Safety greps for rejected modules and research tape role  
- ✅ Phased plan with estimates  
- ✅ **Fase 1 deletions only** (authorized LIXO; ~182 MB)  
- ❌ No Fases 2–4; no YAML edits; no strategy moves; no `.env`; bot not interrupted  
- ✅ Registered microstructure finding (metrics vs depth) in backlog + L2 doc

---

## 7. How to proceed

Reply with which phase(s) to execute next, e.g.:

- `Fase 2: …`  
- `Fase 3: só vwap_trend + ORB`  
- `Fase 4: manter hyperliquid.db`

Until then, Fases 2–4 remain proposals only.
