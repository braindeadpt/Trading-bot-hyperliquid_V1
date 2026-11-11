# Mainnet Readiness & Multi-Leg Execution Technical Plan

**Date:** 2026-11-11  
**Base Branch:** `feat/post-cutover` (`c25e2e3`)  
**Companion Forensic Audit:** [`docs/ops/FULL_AUDIT_2026-11-11.md`](../ops/FULL_AUDIT_2026-11-11.md)

---

## 1. Governing Architectural Decisions (Settled)

| # | Decision | Chosen Direction | Alternatives Rejected & Rationale |
|---|---|---|---|
| **D1** | **Multi-Leg Execution Topology** | **Isolated `MultiLegCoordinator` (`src/multileg/`)** sharing the hardened `HyperliquidLiveClient`, persisting to its own SQLite tables (`multileg_episodes`, `multileg_legs`, `multileg_events`), never mutating single-leg `PortfolioState`. | *Rejected: Refactoring `TradingEngine` (4,933 lines) & `trades` table.* `TradingEngine`, `ExecutionEngine._open_trades[symbol]`, and `PortfolioState._positions[symbol]` assume 1 perp leg per symbol. Forcing multi-leg Spot+Perp and beta-hedged baskets into `engine.py` carries extreme regression risk and violates Phase 10 frozen-window isolation. |
| **D2** | **Mainnet v1 Strategy Scope** | **100% Hyperliquid-native:** (a) **Spot+Perp Carry (`CarryA1`)** and (b) **Perp–Perp Pairs / Baskets (`UnlockShort` Alt+BTC beta hedge & `ResMom`)**, plus hardened single-leg perp engine. | *Rejected: Including Boros cross-exchange in v1.* Boros requires external venue APIs (Binance/Bybit/Gate/CrossEx) + Boros YU contract signing. Keeping v1 strictly on Hyperliquid L1 eliminates cross-exchange margin/bridge failure modes. |
| **D3** | **Capital & Margin Architecture (`<$10k` to `>$10k`)** | **Hyperliquid Unified Account (`AgentAbstraction = 'u'`) as primary `<$10k` mode + `usd_class_transfer` fallback (master-key only) + optional Portfolio Margin (`'p'`, `>$10k`)** with `M=0.45 / M0=0.60` proportional deleveraging. | *Rejected: Requiring Portfolio Margin (`>$10k`) from Day 1, or relying solely on `usd_class_transfer`.* Inspection of `hyperliquid-python-sdk==0.23.0` proved `Exchange.usd_class_transfer` uses `sign_usd_class_transfer_action` (EIP-712 user-signed, **rejected for Agent Wallets**), whereas `Exchange.agent_set_abstraction('u')` uses `sign_l1_action` and shares a single USDC balance across Spot and Perp at **any** account size (`$500` canary included) while keeping withdrawal-free Agent Wallet security on the VPS. |
| **D4** | **Frozen `config_hash` Transition** | **Preserve `config_hash = 4077927fec6a880c` in default `paper` mode** across all PRs until `JevJudge`'s `2026-11-11` readout on `bots-01`, shipping code-level Mainnet guards now + an explicit post-readout re-registration script. | *Rejected: Mutating `config/settings.yaml` immediately.* Mutating hashed YAML keys breaks CI (`run_ci_tests.py` stage 3) and trips `assert_phase10_preregister` on `bots-01` before the `2026-11-11` JevJudge readout is recorded. |

---

## 2. PR Stack Structure (Designed for Independent Agent Review)

1. **PR #1 — `docs/mainnet-audit-and-tech-plan` → `feat/post-cutover`**
   - Pure documentation & specification (`docs/ops/FULL_AUDIT_2026-11-11.md` + `docs/plans/mainnet-readiness-and-multileg-plan.md`). Zero runtime diff.
2. **PR #2 — `fix/mainnet-p0-p1-core-ops` → `feat/post-cutover`**
   - All P0 & P1 fixes across `HyperliquidLiveClient` (perp/spot tick & sig-fig rounding, `$10` min notional, SDK timeouts, nonce lock, `Cloid`, `update_leverage`, `schedule_cancel`), `config.py` & `main.py` (`mode_override` ordering + `_deep_merge` + `SpotPerpCarry` live block), `ExecutionEngine` / `order_lifecycle.py` / `NativeProtectionManager` (OMS pending duplicate lock, `maker_timeout_ms`, verified close size & real `avgPx`/`fee`, safe replace-before-cancel & rounded trigger price), `PortfolioState` (fix double `entry_fee` deduction, HL `accountValue` sync, UTC-hour funding alignment), `emergency_flatten.py` + Telegram `/kill`, Dashboard CB badge & proxy `X-Forwarded-For` hardening, `db_vacuum.sh` / `backup.sh` / `crash_recovery.py` / `vault.py` / `audit_all.py`, plus `tests/test_mainnet_audit_fixes.py`.
3. **PR #3 — `feat/multileg-coordinator-hl` → `fix/mainnet-p0-p1-core-ops`**
   - Hyperliquid Spot order/book/balance extensions on `HyperliquidLiveClient` + `src/multileg/` (`MultiLegCoordinator`, `CarryExecutionAdapter`, `PerpPairExecutionAdapter`, SQLite `MultiLegLedger`, proportional deleverager, unlegged unwind engine) + `scripts/ops/run_multileg_executor.py` + `tests/test_multileg_coordinator.py` (including failure-injection scenarios).

---

## 3. Detailed Mechanism Design — Core P0/P1 Fixes (PR #2)

### 3.1 Hyperliquid Price & Size Precision Contract (`src/exchanges/hyperliquid_live.py`)
Hyperliquid's wire protocol (`hyperliquid.utils.signing.float_to_wire`) and matching engine enforce:
- **Size (`normalize_size`):** Floor to `szDecimals` decimal places using exact `decimal.Decimal(..., ROUND_DOWN)` arithmetic (eliminating IEEE-754 `0.00299` truncation drift).
  - Perp `szDecimals` comes from `meta()["universe"][i]["szDecimals"]`.
  - Spot `szDecimals` comes from `spot_meta()["tokens"][base_token_idx]["szDecimals"]`, indexed by both canonical pair name (`"HYPE/USDC"`, `"PURR/USDC"`) and wire coin (`"@107"`, `"PURR/USDC"`).
- **Price (`normalize_price`):**
  - Maximum decimal places: `max_decimals = (8 if is_spot else 6) - sz_decimals` (unless an explicit override `pxDecimals` was injected by a legacy unit test fixture).
  - Significant figures: When `enforce_sig_figs=True` (all real HL API entries), any non-integer price is rounded to **at most 5 significant figures** AND at most `max_decimals` decimal places via `Decimal`. Integer prices (`price >= 100_000` rounded to integer when `max_decimals >= 0`) are preserved per Hyperliquid's integer-price rule for BTC `>= $100k`.
- **Minimum Notional Guard:** Reject entry orders below `MIN_ORDER_NOTIONAL_USD = 10.0` (reduce-only exits and triggers remain exempt so dust residuals can always be flattened).
- **SDK Concurrency, Timeouts & Idempotency:**
  - Single shared `Info` instance (`self._info = self._exchange.info`) initialized with `timeout=10.0` (eliminating 3× redundant metadata HTTP calls and indefinite socket hangs).
  - `self._action_lock = asyncio.Lock()` serializes signed `/exchange` actions so millisecond nonces stay strictly monotonic across concurrent per-symbol tasks.
  - Deterministic 16-byte hex `Cloid` (`0x` + 32 hex chars derived via `blake2b(digest_size=16)` from `(symbol, side, timestamp_ms, salt)`) attached to live orders when supported by `Exchange.order`.
  - Optional `schedule_cancel(time_ms)` heartbeat method (dead-man's switch, >= `now + 15_000ms` per HL rules) + `set_leverage(symbol, leverage, is_cross=True)` + `ensure_account_abstraction("u")`.

### 3.2 Configuration & CLI Mode Ordering (`main.py` & `src/utils/config.py`)
- `load_config(config_path=..., *, mode_override: Optional[str] = None)` applies `mode_override` **before** `_apply_mode_overrides(merged)` when explicitly provided (e.g. `python main.py --mode mainnet`).
- Calling `load_config()` with no `mode_override` (paper default and CI hash check) produces the **exact `4077927fec6a880c` hash**.
- `_apply_mode_overrides()` uses `_deep_merge(target, values)` for **all** dict sections (not just `strategy`), preserving `execution.maker_orders.strategies`, `maker_fee_pct`, and `timeout_ms` on Mainnet.
- Hard code-level safety block: `SpotPerpCarry` (`SYNTHETIC_SPOT_ONLY = True`) refuses to initialize as an active single-leg strategy when `mode in ("testnet", "mainnet")`, even if `mode_overrides.mainnet.strategy.spot_perp_carry.enabled` is `True` in the frozen YAML.

### 3.3 OMS Duplicate Entry Lock, Verified Close & Trigger Stability
1. **Pending Entry Lock (`src/data/database.py`, `src/core/execution.py`):**
   - Add `Database.get_active_trades()` (`status IN ('open', 'resting', 'partial', 'pending_submission', 'submission_unknown', 'close_pending')`).
   - Track `_pending_entries: Dict[str, int]` in `ExecutionEngine` so any symbol with an active/resting live order rejects duplicate `enter_position()` calls immediately until OMS reaches a terminal non-fill state (`cancelled` / `rejected`).
   - Enforce `self._maker_timeout_ms / 1000.0` for `limit_maker` orders in `_poll_one_order`.
   - Ensure immediate live fills (`ORDER_FILLED`) **always** fire OMS fill accounting even when `oid is None` (synthesizing a deterministic fallback key and extracting `avgPx`/`totalSz` from `entry["filled"]`), and persist `enrich_trade_stop_metadata` in both paper and live modes.
2. **Close Lifecycle & Native Trigger Race (`src/core/order_lifecycle.py`, `src/core/execution.py`):**
   - `parse_hyperliquid_close_response(hl_response, expected_size=...)` extracts `filled_size`, `avg_fill_price`, and ` resting_oid`:
     - `"resting"` on a close is `ORDER_CLOSE_PENDING` (never `ORDER_FILLED`).
     - Partial IoC fill (`0 < filled_size < expected_size * 0.999`) reduces open position size by `filled_size`, re-protects the residual size with native SL/TP, and raises `LiveExecutionError("partial_close_residual_protected")` instead of dropping residual exposure.
   - Before raising on `market_close -> None`, query exchange position (`get_user_state`): if exchange `szi == 0`, reconcile the close cleanly via `trigger_reconcile` (native SL/TP won the race!) instead of throwing `LiveExecutionError`.
3. **Native Protection Replace-Before-Cancel & Rounding Parity (`src/core/native_protection.py`, `src/core/engine.py`):**
   - Normalize `stop_price` and `take_profit_price` via `normalize_price(symbol, px)` **before** comparing `abs(st.stop_price - norm_stop)` and store the normalized trigger price in `ProtectionState` — permanently eliminating the 60s reconciliation replace loop.
   - Only replace the changed leg (e.g., trailing stop updates SL **only**, leaving TP untouched), **place the new SL trigger first, and cancel the old SL trigger only after the new `oid` succeeds** (zero unprotected gap on HTTP 429!).
   - Throttle live `_sync_native_stop` updates to require `>= 10s` elapsed AND `>= 0.10%` stop price ratcheting.

### 3.4 Portfolio Fee Invariant, Live Equity Sync & UTC-Hour Funding (`src/core/portfolio.py`)
- **Single-Deduction Fee Invariant:**
  - `PortfolioState._PositionSnapshot` stores `entry_fee: float = 0.0` (recorded from `cost - notional` on `add_position` and `fee_delta` on `apply_entry_fill`).
  - On `remove_position()`, since `pnl_usd` (`gross_pnl - exit_fee - entry_fee`) already deducts `entry_fee`, cash is credited back with `notional + entry_fee + pnl_usd = notional + gross_pnl - exit_fee`. Net round-trip cash change is **exact**:
    `-(notional + entry_fee) + notional + entry_fee + (gross_pnl - exit_fee - entry_fee) = gross_pnl - entry_fee - exit_fee`!
  - `cancel_position()` likewise refunds `notional + pos.entry_fee`.
  - In `trigger_reconcile.py:225`, subtract `entry_fee` consistently whether `closed_pnl_exchange` is provided or computed locally (since HL `closedPnl` on a closing fill excludes the entry fill's fee).
- **Live Account Equity & UTC Funding:**
  - Add `PortfolioState.sync_live_account_equity(account_value_usd)` called on live boot and reconciliation so sizing uses real wallet equity (preventing `$10k` over-sizing on a `$500` canary).
  - Align `next_funding_settle_ms` to the next UTC top-of-hour (`((ts // 3_600_000) + 1) * 3_600_000`).

### 3.5 Operator Kill-Switch, Dashboard, SQLite & VPS Hardening
- **Emergency Kill Switch:**
  - New standalone CLI `scripts/ops/emergency_flatten.py` (`--mode testnet|mainnet --confirm FLATTEN`) that connects directly via `HyperliquidLiveClient`, cancels all open/trigger orders (`schedule_cancel` + `cancel_all`), flattens every open perp position, verifies `confirm_flat()`, and writes a local `HALT` sentinel.
  - Authenticated `/kill CONFIRM` command in `TelegramCommandBot` restricted to configured `telegram_chat_id` invoking `engine.kill_switch()`.
- **Dashboard & Linux Ops:**
  - Fix all 4 `circuit_breaker_tripped` lookups in `src/dashboard/web.py` to call `risk.is_circuit_breaker_tripped()`.
  - Upgrade `deploy/linux/dashboard_proxy.py` to strip client-supplied `X-Forwarded-*` headers on incoming HTTP requests and inject `X-Forwarded-For: <peer_ip>` whenever `<peer_ip>` is not host-loopback (`127.0.0.1`), closing both the Docker bridge auth bypass and rate-limiter IP spoofing.
  - Set `User=101000` / `Group=101000` in `deploy/linux/systemd/hl-db-vacuum.service` and `PRAGMA busy_timeout=30000;` in `deploy/linux/db_vacuum.sh`.
  - Remove plaintext `/etc/hyperliquid/bot.env` from `deploy/linux/backup.sh` S3 upload list.
  - Fix `Database.prune_old_data()` (`_write_lock` + run `VACUUM` outside transaction), fix `Vault` headless key handling, fix `crash_recovery.py` modes (never rewrite `mainnet`/`testnet` to `paper`), fix `.env.example` / `bot.env.example` (`HYPERLIQUID_PRIVATE_KEY`), seed `_vol_circuit` + `_correlation_monitor` + `_latest_adx` on boot, and update `audit_all.py`.

---

## 4. Detailed Mechanism Design — Isolated `MultiLegCoordinator` (PR #3)

### 4.1 Module Boundary (`src/multileg/`)

```
src/multileg/
├── __init__.py
├── types.py          # LegSpec, EpisodeSpec, LegFillState, EpisodeState, MarginMode
├── ledger.py         # Dedicated SQLite WAL ledger (multileg_episodes, multileg_legs, multileg_events)
├── coordinator.py    # Atomic two-leg / N-leg entry, 15m maker window, unlegged unwind, exit & reconciliation
├── carry_executor.py # CarryA1 live/paper adapter: Spot BUY + Perp SELL + M/M0 proportional deleverage
└── pair_executor.py  # Perp-Perp beta-hedged pair adapter (UnlockShort Alt SHORT + BTC LONG, ResMom baskets)
```

### 4.2 Hyperliquid Spot + Perp Unified Execution Flow (`CarryA1`)

```
[Signal: f_ann >= 15% & PIT Liquidity OK]
       │
       ▼
1. Pre-Flight Margin & Abstraction Check
   • Verify HL account abstraction ('u' Unified or 'p' Portfolio Margin; if 'i' Standard & master wallet, optional usd_class_transfer)
   • Compute committed capital: 1.0x Spot + M0 (0.60x) Perp Margin (or 1.1x under PM for eligible HYPE)
   • Check min_notional >= $10 on BOTH Spot coin ('@idx' / 'BASE/USDC') and Perp coin ('BASE')
       │
       ▼
2. Simultaneous Maker-at-Touch Placement (Alo, Deterministic Cloids)
   • Leg A (Spot BUY @ best_bid, 8 - szDecimals, 5 sig figs)
   • Leg B (Perp SELL @ best_ask, 6 - szDecimals, 5 sig figs)
   • Arm dead-man's switch schedule_cancel(now + 120s) refreshed every 30s while orders rest
       │
       ├──► Both legs fill within 900s (15m) ──► STATE = OPEN
       │       • Persist s0, p0, q=1.0, m_trigger, m0, maint
       │
       └──► Timeout (900s) OR unlegged adverse excursion > max_unlegged_adverse_bps (e.g. 35 bps)
               • Cancel unfilled maker leg (re-query fills post-cancel RTT!)
               • If 0 legs filled -> STATE = ABORTED (0 exposure)
               • If 1 leg filled (or partially filled) -> Taker IoC unwind of filled delta -> STATE = ABORTED_UNWOUND
       │
       ▼
3. Open Episode Risk & Deleverage Loop (Every 60s + L2 Tick Guard)
   • Compute live margin fraction: mf = equity(perp_mid) / (q * perp_mid)
   • If mf <= maint: EMERGENCY TAKER CLOSE BOTH LEGS -> STATE = LIQUIDATED_GUARD
   • If mf < m_trigger: PROPORTIONAL DELEVERAGE
       k = 1 - equity / (m0 * N_t)
       If (1 - k) <= 0.05 -> Close 100% both legs -> STATE = CLOSED_DELEV_FLOOR
       Else -> Taker reduce fraction k on BOTH Spot (SELL) and Perp (BUY reduce_only); q *= (1 - k)
   • If 24h mean funding < 0 for 48h consecutive -> Maker/Taker Exit Both Legs -> STATE = CLOSED
```

### 4.3 Failure-Injection Scenarios Covered by Tests (`tests/test_multileg_coordinator.py`)
1. **Unlegged Maker Timeout:** Perp short fills, Spot buy rests 15m without fill → Coordinator cancels Spot order, re-checks post-cancel fills, and unwinds Perp short via `reduce_only` IoC, leaving `0.0` net delta.
2. **Post-Cancel Fill Race:** Spot order fills during the cancel RTT → Coordinator sees `filled_size > 0` on post-cancel snapshot and transitions to `OPEN` (matching hedge) instead of unwinding into a reverse naked leg.
3. **Adverse Excursion Circuit During Unlegged Window:** Spot fills first, Perp mid drops `> max_unlegged_adverse_bps` within 20s → Immediate cancel of Perp maker + emergency Spot sell unwind.
4. **Proportional Deleverage (`mf < M`):** Perp rallies `+30%` on a `10x` maxLev asset (`maint=5%, M=45%, M0=60%`) → Coordinator sells exact fraction `k` of Spot and buys `k` of Perp (`reduce_only`), restoring `mf = 0.60` while preserving delta-neutrality (`spot_qty == perp_qty`).
5. **Perp–Perp Beta-Hedged Pair (`UnlockShort`):** Short `$1,000` Alt Perp + Long `$1,350` BTC Perp (`beta=1.35`), including rejection/rollback if Leg 2 fails margin or liquidity check.
