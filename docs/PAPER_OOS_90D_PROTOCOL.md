# Paper / OOS 90-day evidence protocol

Frozen: 2026-08-10 (tier-0 fee alignment cycle)

## Purpose

Run a **forward-only** paper evidence cycle after closing candle/MM/OI/tape/XS
momentum families. Success is a reproducible PASS **or** a clean close — not
“find a strategy that looks good”.

## Scope (locked)

| Item | Value |
|------|-------|
| Mode | paper only (`phase08.paper_only: true`) |
| Execution | `VWAPDeviation` only (control / sample accumulation) |
| Shadow | existing Phase08 shadow list — no new strategies |
| Fees | HL perps **tier-0**: taker **0.045%**/side, maker **0.015%**/side |
| Mainnet | blocked |
| GoldRush OOS | not allowed until readiness validated |

## Economic correction

Prior config understated maker (0.01) and used taker 0.035 (Tier-2+). Aligning
fees **invalidates** the prior Phase10 window counter. Re-register with:

```bash
python scripts/ops/reregister_phase10_tier0_fees.py
```

Then **coordinated restart** of the paper bot (`stop.bat` / `start.bat` or
recovery wrapper). Do not restart mid-edit from scripts.

## Gates (frozen a priori)

### A. Paper execution control (VWAPDeviation)

Evaluated on **real paper fills** in `data/live/bot.db` since
`window_start_ms` (Phase10 manifest):

- calendar days ≥ **90**
- closed trades ≥ **30**
- net PF > 1, expectancy_R > 0
- max drawdown ≤ Phase10 frozen max
- costs use tier-0 model (already in execution)

Even on PASS: **remain paper** — mainnet promotion is out of scope.

### B. Shadow strategies

Via `scripts/research/evaluate_shadow_outcomes.py` / shadow panel (gross + **net**):

- `n_evaluated` ≥ 30 in 90d, else `INCONCLUSIVE (frequency insufficient)`
- net PF > 1 and net expectancy_R > 0
- mean funding coverage ≥ 0.90 else net gate = `INCONCLUSIVE` (never PASS)
- B1 / random-direction ≥ p95 with ≥200 seeds when powered
- no promotion without baseline-signal gate PASS (AGENTS.md §12)

### C. Full-depth L2 research (only new investigation)

Recorder: `market_data.l2_recording` at **1s × 25 levels**.

1. Daily audit: `python scripts/research/l2_recording_audit.py`
2. After **≥30 valid days**: `python scripts/research/feature_screening_l2_depth.py`
3. FDR + date-cluster bootstrap + tier-0 cost / AS. Objective: execution /
   fill-AS information — **not** build MM or directional strategy without
   economic survivor.

## Weekly ops

```bash
python scripts/research/paper_oos_weekly_report.py
python scripts/research/evaluate_shadow_outcomes.py --since-days 14 --persist
python scripts/ops/phase10_check_gate.py --no-register
python scripts/research/l2_recording_audit.py
```

Do **not** decide on mid-window snapshots. Formal verdict only at day 90
(or when Phase10 window criteria are met, whichever is later for VWAP).

## Forbidden

- Adding strategies to `execution_strategies` without baseline PASS
- Parameter fishing / lookback grids on closed families
- Treating shadow gross scoreboards as edge
- Restarting GoldRush-backed OOS
- Mainnet enablement

## Artifacts

- Manifest: `data/research/paper_oos_90d/manifest.json` (written at re-register)
- Weekly reports: `data/research/paper_oos_90d/weekly/`
- Protocol (this file): `docs/PAPER_OOS_90D_PROTOCOL.md`

## Code-change log (mid-window, hash-neutral)

The config hash cannot see code changes, so every code change touching the
live process during the frozen window is logged here.

| Date | Commit | Change | Why hash-neutral / isolation |
|------|--------|--------|------------------------------|
| 2026-10-08 | `a70b3cb` | `TradingEngine._route_shadow_signals` — the shadow signal pool now passes through `route_phase08_signals` (own `SequentialContradictionGuard`, never shared with the execution guard) and emits `router_blocked` / `iv_gate_shadow` rows exactly like the routed path. Needed because `iv_gate_shadow` was previously emitted only on the best routed *execution* signal — pruned strategies could never accumulate it, which would silently kill the VWAP iv_gate preregistration. | Observability-only: writes shadow_decisions rows only. Isolation asserted by `test_g_shadow_routing_mirror_never_touches_execution` — identical events produce zero `_process_entry_signal`/`_persist_decision` calls and leave the execution seq-guard state untouched. |
| 2026-10-08 | `a70b3cb` | `shadow_outcome_evaluator` — `independent_outcomes` (one open position per symbol), `n_independent`/`n_overlapped` on boards, metrics + gates read the independent set, pre-simulation overlap dedup, preregistered-confirmation seal (`<variant>#confirm` boards expose counts only until target). | Read path only — computes and persists scoreboards; never feeds execution. `shadow_strategies` membership verified hash-neutral (4077927fec6a880c → 4077927fec6a880c). |
| 2026-10-09 | `ca37d22` + `de1901e` + `2e26023` | Deployed `d12af34→2e26023` in one pm2 restart (2026-10-09 09:28:09 UTC): (a) removed refuted strategies OBS/TopTraderFlow/ChecklistMeta (class files gone; evaluator config sections kept for historical recompute); (b) dashboard v2 — main page answers "closer to net-positive PnL?", ops moved to `/ops`, unified decision_feed, persisted-board shadow panel with bootstrap PF CI, exec↔sim divergence (25 bps), hypothesis counters public under seal; (c) auxiliary pm2 crons `jev-judge`/`outcome-eval`/`watchdogs` found dead since 07-10/29-09 and re-armed. | Runtime-neutral: `config/settings.yaml` untouched, `config_hash` verified 4077927fec6a880c before restart; evaluator regression old-vs-new identical on all 25 persisted boards; dashboard reads research DB read-only and never runs in-process evaluation. Isolation suite green (`test_g_shadow_routing_mirror_never_touches_execution`, `test_h_shadow_mirror_no_fallback_promotion_vwap`). |
| 2026-10-09 | `2d9dc20` | `src/research/carry_shadow/` — standalone shadow daemon for the A1 spot–perp carry study (`docs/PREREGISTER_CARRY_SHADOW.md`). Own WS/REST pollers on public HL market data, hypothetical-episode ledger in `data/research/carry_shadow.db`, proportional-deleverage margin sim, interim kill gates (fill rate <40%@15min after ≥20 leg attempts, edge <3× after ≥5 closed eps, any hypothetical liquidation/gap_kill), final readout at ≥10 clustered closed eps vs the 8%/yr committed-capital hurdle. | Separate process, zero orders, imports nothing from `src.core`/`src.strategies`/`config` — asserted by `tests/test_carry_shadow.py` (static import-graph guard + config-hash-neutral guard + stub-fetcher lifecycle). Writes only its own ledger; not wired into `main.py`, pm2 entry pending owner review. |
| 2026-10-09 | `5008879` | carry-shadow adjustments pre-startup (owner-reviewed): spot WS coin = `pair_name` from spotMeta (`@{index}`; `PURR/USDC` the only named pair), 1h re-discovery; `book_missing` event + meta counter when a funding-eligible candidate lacks a leg book; WS-gap catch-up via `candleSnapshot` (1m→2h fallback) replaying worst-price through the margin rule — maintenance = liquidation/gap_kill, trigger = deleverage; unverifiable gaps flag `gap_unverified=1` (counted separately at readout, never silently dropped); `trades` subscription + `fill_proxy` (aggressor USD volume at our price or better within 15min) recorded in `fill_stats` **as context only — the strict-cross 40%@15min gate is unchanged**; 60s heartbeat meta incl. `subs_active`, `book_missing_count`, `restart_count`, per-coin `book_seen`/`trades`; per-leg `spread_bps` recorded on each entry_attempt (observed only — PIT liquidity gate identical to A1, no new filter). `SHADOW_NOTIONAL_USD = 10000` fixed a priori — it is only the denominator of the fill proxy, not a tuned parameter. Separate pm2 app `carry-shadow`; dashboard `/ops` feed row committed separately (`feat(ops)`) and goes live only with the next approved bot restart — the dashboard runs inside the `hyperliquid` process. | Same isolation as `2d9dc20` — ledger/guards unchanged; 21/21 module tests incl. @N fixture, book_missing, gap liq/delev/missing, trade proxy. `ecosystem.config.js` adds a new app entry only; `hyperliquid`/`jev-judge` entries untouched. Startup: `pm2 start --only carry-shadow` does not restart the bot. |
| 2026-10-09 | `d462a8d` | (a) VWAP iv_gate Amendment 3 activation: `_restore_candles_from_db` now injects **closed** candles into `_shadow_strategies` (previously only `self._strategies` — observed `WARM-UP 3/24` 2.3h after the 09:28 boot). Exec injection byte-identical; shadow drops the in-progress candle. **Amendment 3 scope: shadow startup only — signal rules, confirmation cutoff, and the confirmation `n` threshold are unchanged.** (b) JevJudge restart double-fire defence: `_last_signaled` (symbol → `jev_decision_ts_ms`) persisted write-through to `runtime_state['jev_last_signaled_v1']` on every emit and restored in `_recover_state` before events flow; kill count dedups by verdict ts — later rows on the same `jev_decision_ts_ms` excluded as `duplicate_verdict` (`jev_eval --mark-dupes` annotates, never deletes; `duplicate_verdict_excluded` surfaced on the hypothesis row). Retroactive scan: 0 duplicate ts across 48 trades since 2026-10-04 — n=12 stands. | `config_hash` unchanged (no config touched). Tests: restart-simulated JevJudge fires 0 on a consumed verdict, refire demonstrated without the restore; evaluator split/mark idempotent; dashboard exclusion. 100 tests green across the four touched files. Candle-restore caveat registered: a long downtime leaves a hole between last DB candle and now (VWAP(24h) slightly biased until old candles roll off); dedup is by timestamp so no double-counting. |
| 2026-10-09 | deploy `d12af34`→`786e610` | Single planned restart at **2026-10-09 12:53:16 UTC** (pm2 `hyperliquid` restart 5, pid 89341): Amendment 3 shadow warm-up restore + Jev `_last_signaled` persistence + `duplicate_verdict` dedup (`d462a8d`), aux-cron hardening (`e05fe2e`, `4e67813` — stale-pid lock takeover + SIGINT/SIGTERM lock release), aux_jobs `/ops` sys-strip + carry_shadow feed row (`8534dc3`), carry-shadow daemon adjustments (`5008879`). `jev-judge`/`outcome-eval` converted from pm2 `cron_restart` to long-running loops (boundary-aligned `src.utils.cron_loop`, per-run timeouts 4min/20min, singleton locks) — `pm2 delete`+`start`, `pm2 save` with the new dump. `carry-shadow` started under pm2 (heartbeat live before the bot restart). | `config_hash` verified `4077927fec6a880c` pre-restart; full suite 1915 passed on the host; zero config/execution-rule changes. Post-restart: shadow `WARM-UP` complete at boot via restore (24/24), aux_jobs + carry_shadow green on `/ops`. |
| 2026-10-09 | `5913781` | outcome-eval loop phase correction: the retired cron was `20 */3 * * *` (fires confirmed in pm2.log at 00:20/03:20/06:20/09:20 — 3h cadence, the "105 skipped" figure counted lock-skip log lines, not scheduler fires). The 10800s loop aligned to :00; `cron_loop` gained an `offset` parameter (`5913781`), then `--local` alignment (`3100af9`) because pm2 cron fires on server-local wall-clock — `EVAL_LOOP_OFFSET_S=1200 --local` lands on local :20 exactly like `20 */3`, DST-safe (verified: wake 15:20:00 WEST). `pm2 delete`+`start` of outcome-eval only (pid 93273, then 95663) — no bot restart, `pm2 save` redone. aux_jobs thresholds verified coherent: run-age alert 4h and persist-age 5h vs 3h cadence (≈1.3×/1.7× interval — alert fires on the first missed run plus slack, tighter than the nominal 3× guideline). | Hash-neutral ops change — `src/utils/cron_loop.py` + wrapper + test only; `config_hash` untouched. |

## Operational deviations

| Date | Event | Impact | Resolution |
|------|-------|--------|------------|
| 2026-10-07 08:10 → 10-09 10:31 UTC | `jev-judge` pm2 cron dropped: pm2's cron scheduler logged a double Deregister/Register pair at 08:10 and the re-registration was lost after the in-flight run was SIGINT'd (same double-fire race SIGKILLed an `outcome-eval` run at 10-09 00:20, orphaning its PID lock — and `_lock_acquired` reported the dead holder as "still running", silently skipping 105 consecutive fires until 10-09 ~10:50). No reboot, no OOM, no manual stop. | `jev_verdicts` silent 50.7h. **Contamination audit: zero** — the strategy's 90min consumption TTL refused every stale verdict; 0 JevJudge decisions/trades opened in the window, kill count unaffected (12/100 stands). | `jev-judge` re-armed 10-09 10:31; `pm2 save` + launchd `com.PM2` agent bootstrapped (plist existed since 09-29 but was never loaded — resurrections were unarmed). Rule fixed regardless of zero occurrences: trades opened on a >2h verdict are flagged `stale_verdict=1` (rows kept, never deleted) and excluded from the kill count (`e05fe2e`); stale refusals now log WARNING. outcome-eval lock hardened (same commit); `/ops` sys-strip now shows jev_verdicts + outcome-eval + watchdogs heartbeats in red with the corrective action. |
