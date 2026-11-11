# Full Forensic Audit Report — Hyperliquid Trading Bot v3.1.48

**Audit Date:** 2026-11-11  
**Base Branch / Commit Audited:** `feat/post-cutover` (`c25e2e3`)  
**Production Environment:** Linux VPS `bots-01` (Oracle A1 arm64, Docker Compose `userns-remap` UID `101000`, frozen `config_hash = 4077927fec6a880c`)  
**Overall Mainnet Verdict:** **BLOCKED** — 7 P0 execution/accounting blockers + 9 P1 risk/security/ops defects + 0 OOS-validated strategies (pending preregistered shadow readouts).

---

## 1. Executive Summary

While the repository enforces strong research hygiene (frozen pre-registration hashes, point-in-time snapshots, look-ahead scanner `LOOKAHEAD-001..006`, and Hyperliquid tier-0 cost modeling at `4.5 bps` taker / `1.5 bps` maker), **the live execution, OMS, native protection, portfolio accounting, configuration merge, and Linux operational layers contain critical defects that prevent safe Mainnet deployment.**

Additionally, **2026-11-11 is the exact preregistered expiry date** for `JevJudge` geometry-v2 (`docs/ops/PREREGISTER_JEV_OOS_KILL_2026-10-08.md:43`), requiring an immediate one-shot readout on `bots-01`.

---

## 2. Automated Gate Baseline (`c25e2e3`)

| Gate | Command | Status at `c25e2e3` | Notes |
|---|---|---|---|
| Static Security Audit | `python main.py --audit` | **PASS** (0 CRIT, 0 HIGH, 4 LOW `AUDIT-008`, 7 accepted) | Blind spots: auditor only scans `src/` and skips `aiohttp`/`websockets` in `AUDIT-003`. |
| Look-Ahead Audit | `python scripts/ops/lookahead_audit.py --ci` | **PASS** (0 HIGH/MED; 1 informational comment) | Clean across 117 scanned files. |
| Component Health Check | `python audit_all.py` | **FAIL (4 broken imports)** | Attempts to import `MeanReversion`, `FundingArbitrage`, `DonchianBreakout`, `RangeGrid` from `src.strategies.*` after their 2026-10-28 move to `archive/strategies_closed/`. |
| Frozen Config Hash | `compute_config_hash(load_config())` | **PASS (`4077927fec6a880c`)** | Matches `data/research/phase10/phase10_preregister.json`. |

---

## 3. P0 Mainnet Blockers (Code-Verified Proofs)

### P0-1: `normalize_price()` Rounds Every Asset Price to an Integer (`0` Decimals)
- **Locations:** `src/exchanges/hyperliquid_live.py:107-113`, `src/exchanges/hyperliquid_live.py:131-139`
- **Root Cause:** `build_meta_cache()` populates `"px_decimals": int(entry.get("pxDecimals", 0))`. Hyperliquid's `/info` `meta` endpoint returns `{"universe": [{"name": ..., "szDecimals": ..., "maxLeverage": ...}]}` — **there is no `pxDecimals` field in Hyperliquid's API** (unit tests in `tests/test_hyperliquid_sdk.py:45-49` fabricated `pxDecimals` in `MOCK_META`). Consequently, `px_decimals == 0` for every coin in production, and `normalize_price()` computes `round(price * 1) / 1`.
- **Why Testnet Missed It:** `tests/test_testnet_e2e.py:80` tested exclusively `BTC` (~`$60k–$95k`), where integer dollar rounding coincidentally satisfies `<= 5` sig figs (or integer rule) and `6 - 5 = 1` decimal place.
- **Mainnet Impact:** On `HYPE` (~`$25`, `szDecimals=2`), a `0.5%` stop at `$24.875` rounds to `$25.0` (`0%` stop distance -> instant stop-out or trigger rejection), and `Alo` maker limits cross the spread and reject. On sub-`$0.50` tokens, `normalize_price()` returns `0.0` (`ValueError`). Furthermore, `normalize_size()` (`math.floor(size * factor) / factor` at line `128`) suffers IEEE-754 float drift (`math.floor(0.003 * 10**5) / 10**5 == 0.00299`), and no `$10` minimum notional check is enforced before order submission.

### P0-2: CLI `--mode mainnet` Silently Bypasses All `mode_overrides.mainnet` Safeguards
- **Locations:** `main.py:409-416`, `src/utils/config.py:406`, `src/utils/config.py:580-619`
- **Root Cause:** `load_config()` invokes `_apply_mode_overrides(merged)` while `merged["mode"]` is still `"paper"` (from `config/settings.yaml:10`). `main.py:415-416` then sets `cfg.set("mode", mode)` **after** `load_config()` returns, never re-applying `_apply_mode_overrides`. Furthermore, `_apply_mode_overrides()` uses shallow `target.update(values)` on every non-`strategy` section (`config.py:618`), so setting `mode: mainnet` in YAML wipes `execution.maker_orders.strategies`, `maker_fee_pct`, and `timeout_ms` when merging `maker_orders: {enabled: true}`.
- **Mainnet Impact:** Starting via `python main.py --mode mainnet` leaves `leverage_max = 10.0` (instead of `5.0`), `max_daily_trades = 0` (**unlimited**, instead of `20`), `max_daily_loss_pct = 3.0%` (instead of `2.0%`), `orphan_exchange_policy = ADOPT_AND_PROTECT` (instead of `HALT`), and `close_positions_on_shutdown = false`.

### P0-3: `mode_overrides.mainnet` Enables Unhedged Synthetic `SpotPerpCarry` and Archived `RangeGrid`
- **Locations:** `config/settings.yaml:1157-1160`, `src/strategies/spot_perp_carry.py:16-18, 53, 214-233`
- **Root Cause:** `mode_overrides.mainnet.strategy` sets `spot_perp_carry.enabled: true` (`# true arbitrage, safe`) and `range_grid.enabled: true`. However, `SpotPerpCarry` sets `SYNTHETIC_SPOT_ONLY = True` and only emits a single perp short signal — **zero spot order execution exists in `ExecutionEngine`**.
- **Mainnet Impact:** If Phase 08 paper-only isolation is turned off for Mainnet, `SpotPerpCarry` places **naked directional perpetual shorts** with no spot hedge.

### P0-4: Resting `limit_maker` Orders (`status="pending"`) Allow Duplicate Live Entries Every 5s
- **Locations:** `src/core/execution.py:140, 457, 473, 790-795, 812-815`, `src/core/engine.py:3972-3991, 4079`, `src/data/database.py:799-810`
- **Root Cause:** When a live `Alo` order rests on the book (`ORDER_RESTING`), `enter_position()` stores `status="resting"` in SQLite and returns `result.status = "pending"`. Because `result.status != "open"`, `self._open_trades[symbol]` is not populated (`execution.py:813`), `_portfolio` has no position (`engine.py:4022`), `record_trade_opened()` is skipped (`engine.py:3991`), and `db.get_open_trades()` queries `WHERE status = 'open'`, ignoring `status IN ('resting', 'partial', 'pending_submission', 'submission_unknown')`. Also, `self._maker_timeout_ms` (`30,000ms`) is parsed at `execution.py:140` and never used anywhere (`_poll_one_order` uses `live_order_timeout_s = 60s`).
- **Mainnet Impact:** Five seconds after a maker entry rests on Hyperliquid (once `_last_entry_ms` debounce expires), the next signal on that symbol passes every duplicate check and submits stacked duplicate orders.

### P0-5: Live `close_position()` Marks Partial IoC Fills & Resting Closes as `ORDER_FILLED` and Races Native SL/TP
- **Locations:** `src/core/order_lifecycle.py:226-238`, `src/core/execution.py:864-953`
- **Root Cause:**
  1. `parse_hyperliquid_close_response()` returns `ORDER_FILLED` whenever `"resting" in entry` or `"filled" in entry` without checking `filled["totalSz"] >= position.size`, and `close_position()` cancels native SL/TP *before* submitting the close (`execution.py:908`). A partial IoC close marks the trade closed in DB/portfolio while leaving the unfilled remainder **completely unprotected on Hyperliquid**.
  2. `close_position()` computes `pnl_usd` and `exit_fee` from WS `raw_exit` and config `taker_fee_pct` (`execution.py:864-895`) rather than the exchange's actual fill `avgPx` and `fee`.
  3. With `native_protection.enabled: true` and `software_stop_redundancy: true`, if the exchange's native trigger fills milliseconds before `_check_hard_stops()`, `market_close()` sees `szi == 0` and returns `None` -> `ORDER_REJECTED` -> `LiveExecutionError`, leaving a phantom open trade in memory and attempting to re-place triggers on a flat account.

### P0-6: Native SL/TP Trigger Cancel/Replace Storm & 60-Second Infinite Loop
- **Locations:** `src/core/engine.py:4235-4266`, `src/core/native_protection.py:72, 102-168`
- **Root Cause:**
  1. Once unrealized profit exceeds `1%`, `_maybe_update_trailing_stop()` runs on **every `allMids` tick** without time or step-size throttling, cancelling *both* SL and TP (`native_protection.py:119-123`) before placing two new triggers (4 signed L1 actions/tick). If replacement hits HTTP 429 or transient error, the position is left with **zero exchange-side stop-loss**.
  2. `ensure_protection()` stores unrounded `st.stop_price = float(stop_price)` (`line 137`), whereas `place_trigger_order()` submits `normalize_price(symbol, stop_price)`. Every 60s, `ExchangeReconciler` runs `sync_from_open_orders()` (`line 72`), overwriting `st.stop_price` with the rounded exchange price. On the next WS tick, `abs(st.stop_price - new_stop) > 1e-6` triggers again, causing an infinite 60s cancel/replace loop even on flat prices.

### P0-7: `PortfolioState` Deducts `entry_fee` Twice on Every Closed Trade (Paper & Live)
- **Locations:** `src/core/engine.py:1305, 4023-4024`, `src/core/execution.py:894`, `src/core/portfolio.py:533, 571-576, 617`
- **Root Cause:** On entry, `add_position()` (paper: `cost = notional + entry_fee`) and `apply_entry_fill()` (live: `cost_delta = avg_px * delta + fee_delta`) subtract `notional + entry_fee` from `self._cash`. On exit, `ExecutionEngine.close_position()` sets `pnl_usd = gross_pnl - exit_fee - entry_fee`, and `PortfolioState.remove_position()` credits `self._cash += notional + pnl_usd`. Total cash delta per closed round-trip:
  `-(notional + entry_fee) + notional + (gross_pnl - exit_fee - entry_fee) = gross_pnl - exit_fee - 2 * entry_fee`.
- **Impact:** Every closed trade leaks `1 × entry_fee` permanently from `_cash`, depressing equity and tripping the 10% drawdown circuit breaker prematurely. Meanwhile, `cancel_position()` (`portfolio.py:673`) refunds only `notional`, and `trigger_reconcile.py:225` omits `entry_fee` when `closed_pnl_exchange != 0`.

---

## 4. P1 Risk, Exchange, Security & VPS Operational Defects

| ID | Subsystem | Defect Summary | File:Line |
|---|---|---|---|
| **P1-1** | Capital & Funding Sync | `PortfolioState` initializes to fixed `$10,000` (`settings.yaml:63`), never reading `marginSummary.accountValue` from HL (would 20× over-size a `$500` canary wallet), and settles funding at `entry_time_ms + 3600s * k` instead of exact UTC hours (`HH:00:00 UTC`). | `src/core/portfolio.py:114, 481`<br>`src/core/reconciliation.py:141` |
| **P1-2** | Emergency Kill Switch | Zero external operator trigger exists for `TradingEngine.kill_switch()`: no CLI flatten script, no Telegram `/kill` command, and no dashboard endpoint despite `INCIDENT_RUNBOOK.md:21` instructing operators to invoke it. | `src/alerts/telegram_bot.py:192`<br>`docs/ops/INCIDENT_RUNBOOK.md:21` |
| **P1-3** | Exchange SDK Hardening | Missing `update_leverage` at boot, missing HTTP `timeout` on `Exchange`/`Info`, unlocked concurrent `Exchange` nonces across per-symbol locks, non-hex local `client_order_id` (`Cloid` never sent to HL), unused rate limiter, and no `schedule_cancel` dead-man's switch for resting entries. | `src/exchanges/hyperliquid_live.py:203-290`<br>`src/core/execution.py:300` |
| **P1-4** | Immediate Fill Callback & SL Metadata | `parse_hyperliquid_entry_response` with `oid=None` (`order_lifecycle.py:181`) skips `_fire_order_callbacks` (`execution.py:749`), leaving live fills out of `_portfolio` and without SL/TP; `_on_oms_status_change` uses unreferenced `asyncio.create_task` (`engine.py:1216`); `enrich_trade_stop_metadata` (`engine.py:4048`) only runs in paper mode. | `src/core/engine.py:1216, 4048`<br>`src/core/execution.py:749`<br>`src/core/order_lifecycle.py:181` |
| **P1-5** | Dashboard Circuit Breaker Badge | `web.py` reads `getattr(risk, "circuit_breaker_tripped", False)` in 4 endpoints, but `RiskManager` only defines `_circuit_breaker_tripped` and `is_circuit_breaker_tripped()`. Dashboard permanently shows `CB: OFF`. | `src/dashboard/web.py:1530, 1687, 2200, 2270` |
| **P1-6** | Docker Auth Bypass & S3 Secret Backup | `deploy/linux/dashboard_proxy.py` relays raw TCP to `127.0.0.1:5000` without `X-Forwarded-For`, triggering `_is_direct_local_request()` auth bypass for any Docker network peer; `deploy/linux/backup.sh:75` backs up `/etc/hyperliquid/bot.env` secrets to S3. | `deploy/linux/dashboard_proxy.py:48`<br>`src/dashboard/web.py:1249`<br>`deploy/linux/backup.sh:75` |
| **P1-7** | Root SQLite `VACUUM` on `userns-remap` | `hl-db-vacuum.service` runs `sqlite3 ... VACUUM` as `root` without `User=101000` or `.timeout`, risking `SQLITE_BUSY` or `root:root` ownership on `-wal`/`-shm` files (`readonly database` inside container UID `101000`). | `deploy/linux/systemd/hl-db-vacuum.service:5`<br>`deploy/linux/db_vacuum.sh:21` |
| **P1-8** | Crash Recovery & Env Var Drift | `crash_recovery.py` rejects `--mode mainnet`, passes invalid `--mode live`, and rewrites crashed live runs to `--mode paper` on the live DB (`line 231`); `.env.example` and `bot.env.example` document `HYPERLIQUID_API_KEY` instead of `HYPERLIQUID_PRIVATE_KEY`; headless Linux `Vault()` writes undecryptable `vault.enc`. | `src/utils/crash_recovery.py:97, 181, 231`<br>`deploy/linux/bot.env.example:54`<br>`src/security/vault.py:217` |
| **P1-9** | Restart Cold-Start & Shutdown Flatten | `_restore_candles_from_db()` does not seed `_vol_circuit` (24h blind), `_correlation_monitor` (45m blind), or `_latest_adx`; shutdown/CB flatten skips symbols when `_latest_price` is `None` before first WS tick. | `src/core/engine.py:1804, 4466, 4661` |

---

## 5. Quantitative Strategy & Alpha Audit

### 5.1 Definitively Refuted Families (Do Not Deploy to Mainnet)
Extensive 24-month feature screenings (`731` UTC dates, cluster bootstrap + FDR) and live paper evidence (`STRATEGY_AUDIT.md §0`, `RESEARCH_BACKLOG.md`) conclusively closed:
- **Candle & Classical TA Directional (`15m`/`1h`):** `ChecklistMeta` (`-$905.27` / 184 live paper trades, B1 FAIL), `VolatilityBreakout` (`-$454.30` / 26 trades), `VWAPDeviation` unfiltered (`-$220.84` / 44 trades), `TrendFollow`, `DonchianBreakout`, `SFPReversion`, `VARejection`, `RangeGrid`, `TrendPyramid`, `VWAPTrend`. Gross breakeven (`< 6.8 bps`) is structurally below Hyperliquid tier-0 round-trip taker cost (`9–11 bps`), and maker penetration models (`M2/M3`) turn negative after adverse selection.
- **Retail Microstructure / L2 Scalping / Retail MM:** `OrderBookScalper` (net PF `0.21` over `25,000` shadow evals), `TopTraderFlow` (net PF `0.31` over `3,800` evals), `CVDOrderFlow` (B1 FAIL), and retail Market Making across all 177 HL perps (touch half-spread `0.07–0.27 bps` << `1.5 bps` maker fee + `0.8–2.5 bps` 10s adverse selection).

### 5.2 Viable Structural / Event-Driven Mainnet Candidates

| Rank | Candidate | Edge Mechanism | Current Status | Mainnet Engineering Prerequisite |
|---|---|---|---|---|
| **1** | **CarryA1 (`Spot–Perp Carry`)** | Captures persistent positive funding (`24h mean >= +15% APR`) delta-neutral (long HL Spot + short HL Perp) with proportional deleveraging (`M=0.45 / M0=0.60`). | Active shadow (`carry_shadow.db`), hurdle `>= 8%/yr` net on committed capital. | **Isolated `MultiLegCoordinator`** with real HL Spot execution (`@index` / `BASE/USDC`), Unified Account (`'u'`) / PM (`'p'`) support, unlegged unwind, and proportional deleveraging. |
| **2** | **UnlockShort (`Cliff >= 2%`)** | Calendar supply shock (`T-5` to `T+2`) on liquid alts (`ADV >= $10M`) beta-hedged (`[0.5, 2.0]`) with long BTC perp; `3x` placebo control. | Active shadow (`unlock_shadow.db`), interim read at `n = 8`, full read at `n = 15`. | **Perp–Perp Pair Executor** inside `MultiLegCoordinator` (atomic two-leg entry/exit + unlegged rollback). |
| **3** | **ResMom (`Residual Momentum`)** | Weekly cross-sectional OLS residual momentum vs BTC on top-25 HL perps (beta-neutral basket, ultra-low weekly turnover). | Forward shadow started `2026-11-09` (26 weekly rebalances). | Multi-leg basket rebalance execution via `MultiLegCoordinator` once forward gate passes. |
| **4** | **BorosLockedSpread** | Locks fixed vs. floating funding yield (`>= 8%/yr` net) to maturity (`14–90d`) between Boros YU and HL/CEX perps. | Active shadow (`boros_shadow.db`), read at `n >= 10` clusters. | Deferred to v2 execution (requires cross-venue / Boros smart-contract execution outside HL-only v1 scope). |
| **5** | **VWAPDeviation `iv_gate_shadow`** | Range fade conditioned on Deribit DVOL regime (`discovery PF 1.337, n=30`). | Sealed confirmation sample (`indep_n >= 60`, expiry `2027-02-25`). | Single-leg engine (once P0/P1 fixes land AND confirmation + baseline-signal gate PASS). |
| **—** | **JevJudge (`jev-1.13.0`)** | Hourly LLM/model directional call (`4h` hold, `1R` symmetric bracket). | **EXPIRES TODAY (`2026-11-11`)** (`PREREGISTER_JEV_OOS_KILL_2026-10-08.md`). | Run `scripts/research/jev_eval.py` on `bots-01` immediately; turn off if `net PF <= 1.0`. |
