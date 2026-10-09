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
- **`pkill`/`kill` by name or pattern on the Mac host** (ops rule, added
  2026-10-09): pm2-managed processes are touched only via
  `pm2 <cmd> <name>`; manual/duplicate instances only by explicit pid after
  `ps -p <pid> -o command` confirmation. Rationale: a pattern SIGTERM hit
  the supervised `carry-shadow` while cleaning a manual duplicate
  (`docs/INCIDENT_RUNBOOK.md` §7).
- **`pm2 delete`/`start` without a HEAD gate** (ops rule, added 2026-10-09):
  every approved restart bundle must first run
  `git -C /Users/noder/hyperliquid/app rev-parse HEAD` and require it to
  equal the SHA named in the order; abort if different. Rationale: the
  17:28 UTC wrapper-migration start ran the pre-`412c6e2` ecosystem because
  the Mac had not received the pushed commit.
  **Mac git transport**: the repo's `origin` is credential-less HTTPS —
  the Mac cannot fetch/push by itself. Updates arrive via the Windows
  clone: commit on Mac -> `git fetch ssh://noder@192.168.1.118/...` on
  Windows -> `git push origin`; inbound commits go the other way
  (`git push ssh://... <sha>:refs/heads/<tmp>` then ff-merge on the Mac).
  This loop must be verified closed before any restart.


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
| 2026-10-09 | `412c6e2` | Wrapper migration, scope `hyperliquid`+`watchdogs` only: delete/start under `deploy/macos/run_{paper,watchdogs}.sh` at **17:32:44 UTC** (cd fix `1d583591` — cron wrappers resolve repo root via `dirname "$0"/../..`; `run_paper.sh` has no `cd`, inherits pm2 `cwd`). Root copies archived to `~/hyperliquid_archive/2026-10-09/` post-save (`mv`, never `rm`). **`overnight`/`backup-monthly` intentionally untouched**: ecosystem points all four at `deploy/macos/` but the pm2 dump still resolves the old root `run_overnight.sh`/`run_backup_monthly.sh` — temporary divergence until their own approved windows (root copies stay in place, unarchived) — closed same day by `ee67b8c` + the guarded delete/start. | Window 18:25–18:40 local, after the 18:20 outcome-eval run. Jev pre-check passed (`jev_last_signaled_v1` already carried the live HYPE verdict — restore = no refire). Post-start: `config_hash` `4077927fec6a880c`, `_last_signaled` restored (4 syms), zero `WARM-UP` — VWAPDeviation evaluating 25s post-boot; `/api/market_data_health.carry_shadow` exposes `book_age_*`, `ws_down_ms`, `loop_lag_*`, `subs 76/76`, `degraded_reasons`. First watchdogs run executed **immediately** on `pm2 start` (17:28/17:32 UTC entries in `watchdog_supervisor_cron.log`, clean — no `.env` error): `cron_restart` schedules the NEXT fire, it does not defer the first — overnight/backup-monthly migration must start right after a scheduled run (or add a wrapper guard). |
| 2026-10-09 | `ee67b8c` | Guard chosen over post-run timing: `deploy/macos/run_overnight.sh` + `run_backup_monthly.sh` now refuse real work outside ±10min of their cron windows (05:00 daily; day 1 04:00 local) — any `pm2 start`/`resurrect`/reboot-triggered run exits 0 with a `skip: fora da janela cron` line in the job log (`autorestart=false`, so no skip-loop). `FORCE_RUN=1` bypasses for manual runs; `CRON_GUARD_NOW`/`CRON_GUARD_DOM` are test-only clock overrides. `run_watchdogs.sh` intentionally unguarded: its extra runs are idempotent (alerts are transition-gated via shared state — free health check). Migration completed: `pm2 delete`+`start` for both at 19:54 UTC — exec paths `deploy/macos/`, crons preserved, single skip line each, `restart=0` stable after 2min. **Root copies still in place until the first real 05:00 run is verified next morning** (then `mv` to archive). | Verification at activation: `bash -n`, boundary matrix (04:49/05:11 skip, 04:50–05:10 pass; dom≠01 skip, 01:03:50–04:10 pass), live skip test exit 0. Residual risk noted: Mac sleep prevented only by the LaunchAgent `com.noder.keepawake` caffeinate (pid 96837, since 09-29) — if it dies, a late pm2 fire outside the window skips the night; watchdogs staleness-alert on overnight >26h is the proposed backstop. |
| 2026-10-09 | `eabceca` | (a) `keep-awake` pm2 app — `/usr/bin/caffeinate -is` with `interpreter: none`, `autorestart: true`, `restart_delay` 30s: a pm2-managed second PreventSystemSleep assertion (pid 71986) so sleep-prevention survives both a caffeinate kill (pm2 autorestart) and a reboot (`pm2 resurrect`). The pre-existing caffeinate pid 96837 was re-attributed: it is **not hand-started** — it is the LaunchAgent `com.noder.keepawake` (ppid=1, born 1s after the plist was created 2026-09-29; `RunAtLoad`+`KeepAlive`). Overlap is harmless. **Resurrect path verified**: `~/Library/LaunchAgents/pm2.noder.plist` (label `com.PM2`, `RunAtLoad` -> `pm2 resurrect` -> `~/.pm2/dump.pm2`) loads at GUI login and `autoLoginUser=noder` is set — proven by the 10:51 resurrect run in `/tmp/com.PM2.out`. (b) `check_overnight_stale` in the watchdogs supervisor: alerts when `NIGHTLY_STATUS.json` `generated_ms` > 26h — written by every real run, unreachable by the guard's skip path, immune to artifact-dir touches. Edge-triggered both ways; a missed 05:00 pages by the 12:45 watchdogs run. State migrates automatically via `fresh_state()`. | Ops-only: ecosystem add + supervisor check + tests (27/27). `pm2 start --only keep-awake` + `pm2 save`; `pmset -g assertions` confirms two `PreventSystemSleep` (96837 + 71986). Supervisor needs no restart — next watchdogs run picks the code up. Also recorded: pm2's cron double-fire observed again 2026-10-09 (overnight started 04:59:57, SIGINT'd + refired at 05:00:00) — the ±10min guard admits the early fire and the job's singleton lock absorbs the second. |




## Operational deviations

| Date | Event | Impact | Resolution |
|------|-------|--------|------------|
| 2026-10-09 13:11 UTC | Unplanned `hyperliquid` restart (pid 89341→92633, ↺6): pm2.log shows `Stopping app:hyperliquid` at 14:10:56 local with no attributable caller — not `pm2 resurrect` (LaunchAgent ran once at 10:51 and unloaded), not the watchdogs scripts (no pm2 calls in repo), no other logged-in session or agent process found. jev-judge/outcome-eval were delete+recreated in the same ~90s window (ids 38→40, 39→41; outcome-eval recreate = the sanctioned offset-fix redeploy; jev-judge's was not). | Jev edge-trigger held: boot logged `JevJudge _last_signaled restored: {BTC,HYPE,SOL}` — the write-through map persisted by the 12:53 process survived. Zero Jev trades 13:11–14:41 UTC, `jev_eval --mark-dupes` = 0. Shadow warm-up restored again (VWAP evaluating 8s post-boot, zero WARM-UP lines). | Attributed (closed 10-09): the restart was the Devin agent ssh command chain at 14:10:56 local — /tmp/bot_l0.txt mtime correlates with pm2.log `Stopping app:hyperliquid`; the jev-judge/outcome-eval deletes were part of the same chain. The defence stack behaved as designed; no action beyond this record. |
| 2026-10-09 17:28 UTC | First `pm2 start` of the wrapper migration ran the stale ecosystem — `412c6e2` was pushed to origin but not yet fetched on the Mac (credential-less HTTPS remote; updates arrive via the Windows-side fetch+push). `pm2 jlist` showed exec paths still at root. | One extra `hyperliquid` restart (~4min; paper state preserved on both boots, Jev dedup map intact on both). | Detected by the exec-path check inside the same window; `412c6e2` pushed from the Windows clone into a temp ref on the Mac repo over SSH, ff-merged, delete/start redone 17:32:27 UTC. Rule added: verify `git rev-parse HEAD` on the target host before a config-path restart. |
| 2026-10-07 08:10 → 10-09 10:31 UTC | `jev-judge` pm2 cron dropped: pm2's cron scheduler logged a double Deregister/Register pair at 08:10 and the re-registration was lost after the in-flight run was SIGINT'd (same double-fire race SIGKILLed an `outcome-eval` run at 10-09 00:20, orphaning its PID lock — and `_lock_acquired` reported the dead holder as "still running", silently skipping 105 consecutive fires until 10-09 ~10:50). No reboot, no OOM, no manual stop. | `jev_verdicts` silent 50.7h. **Contamination audit: zero** — the strategy's 90min consumption TTL refused every stale verdict; 0 JevJudge decisions/trades opened in the window, kill count unaffected (12/100 stands). | `jev-judge` re-armed 10-09 10:31; `pm2 save` + launchd `com.PM2` agent bootstrapped (plist existed since 09-29 but was never loaded — resurrections were unarmed). Rule fixed regardless of zero occurrences: trades opened on a >2h verdict are flagged `stale_verdict=1` (rows kept, never deleted) and excluded from the kill count (`e05fe2e`); stale refusals now log WARNING. outcome-eval lock hardened (same commit); `/ops` sys-strip now shows jev_verdicts + outcome-eval + watchdogs heartbeats in red with the corrective action. |
