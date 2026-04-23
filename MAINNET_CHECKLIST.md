# 🛡️ MAINNET SECURITY CHECKLIST
# Hyperliquid Trading Bot — Security Audit for Real Money Deployment
# Auditor: Mainnet Guardian Subagent
# Date: 2026-04-24
# Bot Version: 0.1.0

---

## 🚨 EXECUTIVE SUMMARY

| Category | Score | Status |
|----------|-------|--------|
| Paper Trading Isolation | ✅ PASS | SAFE — Bot literally cannot send real orders |
| API Security | ⚠️ NEEDS WORK | No private key exists yet, but real trading is unimplemented |
| Order Safety | ❌ FAIL | No real order execution safeguards exist |
| Risk Controls | ⚠️ NEEDS WORK | Partial — position/leverage limits OK, missing circuit breaker |
| Operational Safety | ⚠️ NEEDS WORK | Config-driven OK, missing graceful position close on exit |

### **FINAL VERDICT: 🔴 NO-GO FOR MAINNET**

> The bot CANNOT and MUST NOT be deployed to mainnet with real money. While the paper trading isolation is solid (the code literally raises `NotImplementedError` for real execution), there are zero safeguards for when real trading IS eventually implemented. A user could accidentally wire up a wallet, and there would be no circuit breakers, no slippage protection, no margin checks, and no emergency stop. **DO NOT DEPLOY.**

---

## 1. Paper Trading Isolation

### 1.1 — Paper trading mode CANNOT send real orders
- **Status:** ✅ **PASS**
- **Evidence:** `exchange_client.py` lines 22–54. The `HyperliquidClient.place_order()` method checks `if self.paper_trading:` and returns a simulated response. The `else` branch raises `NotImplementedError("Execução real requer wallet e assinatura criptográfica")`. The `close_position()` method does the same. It is physically impossible for this code to execute a real order — the real path throws an exception.
- **Risk Level:** LOW (while unimplemented)
- **Action:** ✅ None needed for safety, but implement a more robust gate when real trading is added.

### 1.2 — Testnet/mainnet switch is clear and foolproof
- **Status:** ❌ **FAIL**
- **Evidence:** There is NO testnet/mainnet switch in the codebase. `config/settings.yaml` has `paper_trading: true` but no `network` field. The `exchange_client.py` only connects to data APIs (`https://api.hyperliquid.xyz/info`), not the trading API. There is no concept of testnet vs mainnet — the bot is purely data-feed driven right now. When real trading is added, there is no mechanism to force testnet verification before mainnet.
- **Risk Level:** **CRITICAL**
- **Action:** Add explicit `network: testnet | mainnet` config with a **two-step approval** gate. Real trading must require:
  1. A config flag `mainnet_enabled: true`
  2. A runtime confirmation prompt or file-based confirmation
  3. A startup banner that screams "MAINNET — REAL MONEY" when active

### 1.3 — No accidental mainnet calls in test mode
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** The `debug_api.py` script makes direct `requests.post()` to `https://api.hyperliquid.xyz/info`. This is read-only (info endpoint) and safe. However, there is no audit trail that distinguishes test-mode calls from what would be mainnet calls. When real trading is implemented, every request must be tagged with environment context.
- **Risk Level:** MEDIUM
- **Action:** Add request header logging with environment tag (`X-Env: paper|testnet|mainnet`). Ensure all API calls in paper mode go through the `paper_trading` gate.

---

## 2. API Security

### 2.1 — Private key is NEVER logged or printed
- **Status:** ✅ **PASS** (by absence)
- **Evidence:** There is no private key anywhere in the codebase. No wallet integration exists. `exchange_client.py` has a TODO comment: `# TODO: Implementar execução real com wallet + assinatura`. No logging of secrets exists because there are no secrets yet.
- **Risk Level:** LOW
- **Action:** When adding wallet support:
  - NEVER log the private key or mnemonic
  - Use environment variables or encrypted key files only
  - Add a pre-commit hook or CI check that scans for private key patterns
  - Log a WARNING if the key is detected in environment but never print it

### 2.2 — Private key is stored securely (not hardcoded in source)
- **Status:** ✅ **PASS** (by absence)
- **Evidence:** No private key exists in any file. No `.env`, no hardcoded strings, no wallet files. The only "secret-like" data is the `wallet_address` placeholder which is empty/commented.
- **Risk Level:** LOW
- **Action:** When adding wallet support, mandate `.env` file or OS keychain. Add `.env` and `*.pem` to `.gitignore` with a comment explaining why.

### 2.3 — API requests use HTTPS only
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** All URLs in `config/settings.yaml` use `https://`. `data_aggregator.py` uses `requests.Session()` with HTTPS. However, there is **no enforcement** — no code checks that the URL starts with `https://`. A malicious or misconfigured `settings.yaml` could point to `http://`. There is no `verify=True` explicitly set on the session (it defaults to True, but relying on defaults is risky).
- **Risk Level:** HIGH
- **Action:** Add a startup check in `DataAggregator.__init__()` that asserts every base_url starts with `https://`. Explicitly set `self.session.verify = True`. Log FATAL and exit if HTTP is detected.

### 2.4 — Rate limiting is implemented
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** `data_aggregator.py` has a `@retry_on_failure` decorator with exponential backoff (max 3 retries, 2s base). This is for failures, not rate limiting. There is **no explicit rate limit tracking** per exchange (Binance has weight limits, Bybit has rate limits). The bot could hit limits during fast monitor loops (10s interval) or MTF loops (60s). The `data_downloader.py` uses `time.sleep(0.05)` between API calls but no weight tracking.
- **Risk Level:** HIGH
- **Action:** Add a `RateLimiter` class that tracks requests per endpoint and enforces minimum intervals. Binance: max 1200 req/min. Bybit: max 50 req/s. Add `429 Too Many Requests` handling with `Retry-After` header parsing.

### 2.5 — Timeout/retry logic exists
- **Status:** ✅ **PASS**
- **Evidence:** `data_aggregator.py` sets `timeout=5` for validation, `timeout=10` for most data calls, `timeout=15` for Hyperliquid. The `@retry_on_failure` decorator provides 3 retries with exponential backoff (1s, 2s, 4s). `paper_trading.py` monitor threads catch exceptions and sleep. `dashboard_web.py` has no timeout on data updates though.
- **Risk Level:** LOW
- **Action:** Add a global request timeout config in `settings.yaml`. Ensure the dashboard doesn't hang on slow API calls.

---

## 3. Order Safety

### 3.1 — Minimum order size checks
- **Status:** ❌ **FAIL**
- **Evidence:** `exchange_client.py` `place_order()` does not validate `size` against minimums. Hyperliquid minimum is typically ~$10–$20. The `paper_trading.py` sets `position_size = min(self.max_position_usd, self.capital * 0.1)` which could be below minimum if capital is low. The `backtest_db.py` also uses `min(self.max_position_usd, self.current_capital * 0.1)`.
- **Risk Level:** **CRITICAL**
- **Action:** Add `min_order_size_usd: 20` to config. In `exchange_client.py`, reject orders below minimum with a clear error.

### 3.2 — Slippage protection
- **Status:** ❌ **FAIL**
- **Evidence:** No slippage protection exists anywhere. The strategy and paper trading assume market orders execute at the fetched price. In reality, market orders on Hyperliquid can slip significantly during volatility. The `strategy.py` returns `'LONG'` signals but does not suggest order type or max slippage.
- **Risk Level:** **CRITICAL**
- **Action:** Add `max_slippage_pct: 0.005` (0.5%) to config. For market orders, compare expected vs. actual fill price. Use limit orders with `post_only` when possible. Log a WARNING if slippage exceeds threshold.

### 3.3 — Orders have max price deviation limits
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** `data_aggregator.py` has `_is_price_sane()` which validates prices against hardcoded ranges (BTC: $10k–$200k, ETH: $500–$20k). This is good for data validation but is **NOT applied before order placement**. There is no check that the order price is within acceptable deviation from market.
- **Risk Level:** HIGH
- **Action:** Reuse `_is_price_sane()` in `exchange_client.py` before placing any order. Add a `max_price_deviation_pct: 0.02` (2%) check: if the order price is >2% away from the last known price, reject it.

### 3.4 — Emergency close works even if API is slow
- **Status:** ❌ **FAIL**
- **Evidence:** There is no emergency close mechanism. The `paper_trading.py` monitor thread runs every 10 seconds and handles exceptions, but in real trading, if the Hyperliquid API is down or slow, positions remain open with no stop-loss at the exchange level. The `exchange_client.py` `close_position()` has no timeout override or circuit breaker for emergency situations.
- **Risk Level:** **CRITICAL**
- **Action:** Implement an **exchange-level stop-loss** on Hyperliquid for every position. The bot's trailing stop is client-side only — if the bot crashes, the exchange must have a hard stop. Also implement a "panic button" method that uses a shorter timeout (3s) and retries aggressively.

### 3.5 — Orders are rejected if account has insufficient margin
- **Status:** ❌ **FAIL**
- **Evidence:** The `exchange_client.py` `get_balance()` returns a hardcoded `{'USDC': 10000.0}` in paper mode. In real mode it returns `{}` (unimplemented). There is no margin check before placing orders. The `risk_manager.py` has no margin awareness.
- **Risk Level:** **CRITICAL**
- **Action:** Before every order, query account balance via Hyperliquid API. Calculate required margin = `size / leverage`. Reject order if `available_margin < required_margin * 1.1` (10% buffer).

---

## 4. Risk Controls

### 4.1 — Max position size limits
- **Status:** ✅ **PASS**
- **Evidence:** `config/settings.yaml` has `max_position_size_usd: 100`. This is enforced in `paper_trading.py` (`position_size = min(self.max_position_usd, self.capital * 0.1)`), `backtest_db.py`, `backtest.py`, and `optimizer.py`. The `risk_manager.py` also tracks `max_position`.
- **Risk Level:** LOW
- **Action:** Ensure the limit is also enforced in `exchange_client.py` when real trading is implemented.

### 4.2 — Max leverage limits
- **Status:** ✅ **PASS**
- **Evidence:** `config/settings.yaml` has `max_leverage: 2`. The `paper_trading.py` and backtest engines respect this conceptually (position size is calculated in USD terms, notional). However, there is no explicit check that `size / margin <= max_leverage`.
- **Risk Level:** LOW
- **Action:** Add explicit leverage check: `if size / account_balance > max_leverage: reject_order()`. Log the calculated leverage for every trade.

### 4.3 — Daily loss limits (circuit breaker)
- **Status:** ❌ **FAIL**
- **Evidence:** `config/settings.yaml` has `max_daily_trades: 5` but **NO daily loss limit**. The bot could theoretically lose $100 per trade × 5 trades = $500/day (50% of $1000 capital). There is no circuit breaker that stops trading after a cumulative loss threshold. The `paper_trading.py` resets `daily_trades` each day but never checks cumulative PnL.
- **Risk Level:** **CRITICAL**
- **Action:** Add `max_daily_loss_pct: 0.05` (5% of capital) to config. Track running daily PnL. If cumulative loss exceeds threshold, set a flag `circuit_breaker_active = True` and refuse all new entries until manual reset or next day. Log ALERT-level message.

### 4.4 — Bot can be stopped instantly
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** `paper_trading.py` catches `KeyboardInterrupt` and stops threads after 2-second timeout. `main.py` catches `KeyboardInterrupt` and exits. However, there is no external control mechanism (API endpoint, signal file, or IPC) to stop the bot remotely. The dashboard has no "STOP" button. In a headless server scenario, you'd need to SSH in and Ctrl+C.
- **Risk Level:** HIGH
- **Action:** Implement a `stop_bot()` method that:
  1. Checks for a `STOP` file in the project root
  2. Or listens on a local socket / HTTP endpoint for a stop command
  3. On stop, cancels all open orders, closes positions (configurable), and exits cleanly

### 4.5 — No infinite loops that could drain account
- **Status:** ⚠️ **NEEDS WORK**
- **Evidence:** `paper_trading.py` `run_continuous()` has `while True:` with `time.sleep(interval_seconds)`. The monitor threads also have `while self._monitor_running:` loops. These are bounded by sleep so they won't CPU-spin, but there is **no maximum iteration counter** or health check. If `time.sleep()` is bypassed (e.g., due to threading issue), the loop could run unbounded. The `dashboard_web.py` Flask `run()` also loops indefinitely.
- **Risk Level:** MEDIUM
- **Action:** Add a `max_runtime_hours: 24` config. After this duration, the bot enters "cooldown" and requires restart. Add a watchdog thread that monitors loop health and kills the process if iterations exceed a safe threshold.

---

## 5. Operational Safety

### 5.1 — Bot logs are clear and auditable
- **Status:** ✅ **PASS**
- **Evidence:** Logging is well-structured with format `'%(asctime)s - %(name)s - %(levelname)s - %(message)s'`. `utils.py` `setup_logging()` supports both console and file output. `logs/bot.log` is configured. Trade entries/exits are logged with timestamps, prices, sizes, and PnL. `paper_trading.py` logs every cycle with position state.
- **Risk Level:** LOW
- **Action:** Add a structured JSON log option for programmatic parsing. Ensure every order attempt (even rejected) is logged.

### 5.2 — Error messages don't expose sensitive data
- **Status:** ✅ **PASS**
- **Evidence:** No sensitive data exists in the codebase to expose. Error messages in `data_aggregator.py` log API response snippets (first 100 chars) but these are public data (prices, volumes). `debug_api.py` prints raw API responses but these are also public market data. No wallet addresses, keys, or account IDs are logged.
- **Risk Level:** LOW
- **Action:** When adding wallet support, create a `sanitize_for_logs()` function that strips any private key, seed phrase, or account identifier from exception messages before logging.

### 5.3 — Config can be changed without code changes
- **Status:** ✅ **PASS**
- **Evidence:** `config/settings.yaml` is a comprehensive YAML file controlling all parameters: assets, timeframes, data sources, strategy thresholds, risk limits, polling intervals, and logging. `utils.py` `load_config()` reads this file. No hardcoded trading parameters exist in the source code (except defaults in constructors).
- **Risk Level:** LOW
- **Action:** Add a `config/settings.yaml.example` file for version control and document each parameter. Add config validation at startup that prints errors for invalid values.

### 5.4 — Graceful shutdown (close positions on exit)
- **Status:** ❌ **FAIL**
- **Evidence:** `paper_trading.py` `run_continuous()` catches `KeyboardInterrupt` and stops monitor threads, but does **NOT close the open position** before exiting. The position remains open in the database but the monitoring stops. `main.py` has no shutdown hook. `dashboard_web.py` has no shutdown handler. If the bot crashes while in a long position, there is no automatic close.
- **Risk Level:** **CRITICAL**
- **Action:** Implement a `shutdown()` method that:
  1. Stops all new entries immediately
  2. Attempts to close the current position via `exchange_client.close_position()`
  3. Waits up to 30 seconds for confirmation
  4. Logs the result
  5. Exits
  Register this as `atexit` handler and signal handler for SIGTERM/SIGINT.

---

## 📊 DETAILED FILE-BY-FILE ANALYSIS

### exchange_client.py
| Check | Status |
|-------|--------|
| Paper gate | ✅ PASS — raises NotImplementedError for real orders |
| HTTPS enforcement | ❌ FAIL — no explicit HTTPS check |
| Rate limiting | ❌ FAIL — none |
| Timeout | ⚠️ NEEDS WORK — uses default from config, no per-call override |
| Key storage | ✅ PASS — no keys exist |

### paper_trading.py
| Check | Status |
|-------|--------|
| Paper isolation | ✅ PASS — entirely virtual |
| Position limits | ✅ PASS — enforces max_position_usd |
| Daily limits | ⚠️ NEEDS WORK — max trades only, no loss circuit breaker |
| Graceful exit | ❌ FAIL — does not close position on KeyboardInterrupt |
| Thread safety | ⚠️ NEEDS WORK — uses `threading.Lock()` but no deadlock detection |
| MTF signal | ⚠️ NEEDS WORK — enters on low-TF spike without confirmation delay |

### data_aggregator.py
| Check | Status |
|-------|--------|
| HTTPS | ⚠️ NEEDS WORK — uses HTTPS but no enforcement |
| Retry logic | ✅ PASS — exponential backoff with max retries |
| Timeout | ✅ PASS — explicit timeouts on all calls |
| Rate limiting | ❌ FAIL — no per-API weight tracking |
| Price validation | ✅ PASS — `_is_price_sane()` checks realistic ranges |
| HTML detection | ✅ PASS — detects Cloudflare/block pages |
| Cache | ✅ PASS — 2-minute price cache with fallback |

### strategy.py / optimizer.py / backtest_*.py
| Check | Status |
|-------|--------|
| Parameter bounds | ⚠️ NEEDS WORK — grid search allows arbitrary values |
| Overfitting risk | ⚠️ NEEDS WORK — optimized on 30 days only |
| Walk-forward test | ❌ FAIL — no out-of-sample testing |
| Slippage model | ❌ FAIL — assumes zero slippage in backtest |

### dashboard_web.py
| Check | Status |
|-------|--------|
| CORS | ⚠️ NEEDS WORK — `CORS(self.app)` allows all origins |
| Auth | ❌ FAIL — no authentication on dashboard |
| HTTPS | ❌ FAIL — Flask runs HTTP only |
| Data exposure | ⚠️ NEEDS WORK — `/api/stats` exposes DB contents |

---

## 🔴 TOP 5 CRITICAL BLOCKERS FOR MAINNET

### #1 — Real Trading Is Unimplemented (CRITICAL)
The bot cannot execute real orders. When this is eventually added, ALL the following blockers become active simultaneously. **Do NOT enable real trading until ALL blockers are resolved.**

### #2 — No Daily Loss Circuit Breaker (CRITICAL)
The bot can lose up to 50% of capital in a single day (5 trades × $100 = $500 on $1000 effective capital). Add `max_daily_loss_pct: 0.05` with hard stop.

### #3 — No Graceful Shutdown with Position Close (CRITICAL)
If the bot crashes, Ctrl+C'd, or the server reboots while in a position, that position stays open unmonitored. Implement `atexit` + `SIGTERM` handlers that close positions.

### #4 — Client-Side Stops Only (CRITICAL)
All stop-loss and trailing-stop logic is in Python. If the bot process dies, there is NO protection. Hyperliquid exchange stops must be set for every position.

### #5 — No Exchange-Level Order Validation (CRITICAL)
No checks for: minimum order size, margin sufficiency, slippage, price deviation, or leverage caps at the exchange layer. All validation is client-side only.

---

## 🟡 HIGH PRIORITY FIXES (Before Any Real Money Test)

1. **Add `network` config** with testnet-first mandate
2. **Enforce HTTPS** on all API URLs at startup
3. **Add rate limiter** per exchange with 429 handling
4. **Add `max_daily_loss_pct`** circuit breaker
5. **Implement graceful shutdown** with position close
6. **Add exchange-level stop-loss** for every position
7. **Add slippage protection** and max price deviation checks
8. **Add minimum order size** validation
9. **Add margin check** before order placement
10. **Secure dashboard** with auth and local-only binding

---

## 🟢 GOOD SECURITY PRACTICES ALREADY IN PLACE

- ✅ No hardcoded secrets or API keys
- ✅ Paper trading is genuinely isolated (throws exception for real orders)
- ✅ Structured logging to file
- ✅ Config-driven parameters (no code changes needed)
- ✅ Price sanity checks on data feeds
- ✅ Retry logic with exponential backoff
- ✅ HTML/Cloudflare detection on API responses
- ✅ Price cache with age validation
- ✅ Position size limits enforced
- ✅ Leverage config exists (needs explicit check)

---

## 📋 RECOMMENDED MAINNET DEPLOYMENT ROADMAP

### Phase 1: Safety Infrastructure (DO THIS FIRST)
1. Implement all CRITICAL and HIGH fixes above
2. Add comprehensive unit tests for risk controls
3. Add integration tests with Hyperliquid testnet
4. Run paper trading for 30 days with full logging

### Phase 2: Testnet Validation
1. Enable testnet trading with $1 positions
2. Verify order placement, fill, and cancellation
3. Test circuit breaker under simulated loss
4. Test graceful shutdown during active position
5. Run for 2 weeks minimum

### Phase 3: Mainnet Graduation
1. Increase position size to $10 (1% of capital)
2. Run for 2 weeks with full monitoring
3. Only then consider increasing size
4. NEVER increase size without backtest validation

---

## ✅ CHECKLIST SUMMARY TABLE

| # | Item | Status | Risk | File |
|---|------|--------|------|------|
| 1.1 | Paper trading CANNOT send real orders | ✅ PASS | LOW | `exchange_client.py:22` |
| 1.2 | Testnet/mainnet switch is clear | ❌ FAIL | **CRITICAL** | `config/settings.yaml` |
| 1.3 | No accidental mainnet in test mode | ⚠️ NEEDS WORK | MEDIUM | `data_aggregator.py` |
| 2.1 | Private key NEVER logged | ✅ PASS | LOW | N/A (no key) |
| 2.2 | Private key not hardcoded | ✅ PASS | LOW | N/A (no key) |
| 2.3 | HTTPS only enforced | ⚠️ NEEDS WORK | HIGH | `data_aggregator.py` |
| 2.4 | Rate limiting implemented | ❌ FAIL | HIGH | `data_aggregator.py` |
| 2.5 | Timeout/retry logic | ✅ PASS | LOW | `data_aggregator.py` |
| 3.1 | Minimum order size checks | ❌ FAIL | **CRITICAL** | `exchange_client.py` |
| 3.2 | Slippage protection | ❌ FAIL | **CRITICAL** | `exchange_client.py` |
| 3.3 | Max price deviation | ⚠️ NEEDS WORK | HIGH | `data_aggregator.py` |
| 3.4 | Emergency close (slow API) | ❌ FAIL | **CRITICAL** | `exchange_client.py` |
| 3.5 | Insufficient margin rejection | ❌ FAIL | **CRITICAL** | `exchange_client.py` |
| 4.1 | Max position size limits | ✅ PASS | LOW | `config/settings.yaml` |
| 4.2 | Max leverage limits | ✅ PASS | LOW | `config/settings.yaml` |
| 4.3 | Daily loss circuit breaker | ❌ FAIL | **CRITICAL** | `paper_trading.py` |
| 4.4 | Instant stop mechanism | ⚠️ NEEDS WORK | HIGH | `paper_trading.py` |
| 4.5 | No infinite loops | ⚠️ NEEDS WORK | MEDIUM | `paper_trading.py` |
| 5.1 | Clear auditable logs | ✅ PASS | LOW | `utils.py` |
| 5.2 | No sensitive data in errors | ✅ PASS | LOW | All files |
| 5.3 | Config-driven changes | ✅ PASS | LOW | `config/settings.yaml` |
| 5.4 | Graceful shutdown (close positions) | ❌ FAIL | **CRITICAL** | `paper_trading.py` |

---

*Report generated by Mainnet Guardian subagent for Hyperliquid Trading Bot v0.1.0*
*Date: 2026-04-24 | Auditor: Automated Code Review*
