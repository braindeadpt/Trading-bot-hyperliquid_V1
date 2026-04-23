# 🔍 AUDIT REPORT — Hyperliquid Trading Bot
**Date:** 2026-04-24  
**Scope:** All `.py` files in `src/`  
**Auditor:** Subagent Code Auditor  

---

## File: data_aggregator.py
### Issues Found: 7

1. **Severity: HIGH** — `_fetch_hyperliquid()` silently swallows ALL exceptions with nested bare `except Exception` blocks (lines ~296–345). Multiple `try/except Exception` wrappers around each API method make it impossible to distinguish real API failures from parsing bugs. If Hyperliquid changes their response format, the code will silently fall through to cache without any actionable error.
   - *Suggested fix:* Log the specific exception type and raw response at ERROR level before falling back. Consider breaking out the fallback logic into a separate retry policy.

2. **Severity: MEDIUM** — `_get_sanity_range()` has hardcoded price ranges for BTC/ETH/SOL (lines ~275–279). Adding a new asset requires editing source code.
   - *Suggested fix:* Move sanity ranges to `config.yaml` under a `price_validation` section.

3. **Severity: MEDIUM** — `_safe_json()` HTML detection only checks lowercase prefixes (lines ~227–229). A Cloudflare response starting with `<!DOCTYPE html>` (uppercase) or BOM/whitespace-prefixed HTML will pass through and trigger a confusing JSON decode error instead of a clear "blocked" message.
   - *Suggested fix:* Strip whitespace and make the check case-insensitive: `response.text.strip()[:20].lower().startswith(('<!doctype', '<html'))`.

4. **Severity: LOW** — `validate_api()` for Hyperliquid uses `len(resp.text) > 10` as a success check (line ~87). A 500-error page with >10 chars would pass validation.
   - *Suggested fix:* Parse the response as JSON and verify it contains expected token metadata.

5. **Severity: LOW** — `test_apis_command()` imports `utils` at function scope (line ~367). While not currently a circular import, it creates an implicit dependency that is hard to trace.
   - *Suggested fix:* Move the import to module top-level.

6. **Severity: LOW** — `_fetch_binance()`, `_fetch_bybit()`, `_fetch_okx()` all make 3–4 sequential HTTP requests per call. No connection-level rate limiting or request batching.
   - *Suggested fix:* Add a small `time.sleep(0.05)` between calls, or use async/await for parallel fetches.

7. **Severity: LOW** — `_fetch_bybit()` accesses `data['result']['list'][0]` (line ~317) without checking if `'result'` or `'list'` exist. A malformed API response will raise an unhandled `KeyError` or `IndexError` that bypasses the `@retry_on_failure` decorator because the decorator only retries on generic `Exception`, which does catch it — but it will waste retries on a structurally bad response.
   - *Suggested fix:* Add structural validation before indexing into nested dicts.

---

## File: paper_trading.py
### Issues Found: 10

1. **Severity: HIGH** — **Uninitialized attributes `self.bullish_count` and `self.bearish_count`** (used in `_check_entry_signals()`, ~line 575). `PaperTrader.__init__()` never initializes these counters, yet `_check_entry_signals()` does `self.bullish_count += 1`. Calling `run_cycle()` will raise `AttributeError` immediately.
   - *Suggested fix:* Add `self.bullish_count = 0` and `self.bearish_count = 0` in `__init__`.

2. **Severity: HIGH** — **Undefined variable `price` in `run_cycle()`** (line ~740). The code sets `self._htf_price = price` but `price` is never assigned in that function scope. The nearest `price` variable is local to `fetch_and_process_candle()` and is not in scope here.
   - *Suggested fix:* Use `candle['close']` or `prices[-1]` instead.

3. **Severity: HIGH** — **Race condition on shared state between threads** (lines ~355–360). `run_cycle()` writes to `self._htf_direction`, `self._htf_sma`, and `self._htf_price` without any lock, while the MTF thread (`_mtf_loop`) reads them. CPython's GIL makes simple attribute assignment atomic, but compound updates (e.g., multiple assignments) are not guaranteed consistent across threads. More critically, `_process_low_tf_candle()` calls `_enter_position()` **inside** `with self._lock`, but `run_cycle()` calls it **without** the lock. Two threads could simultaneously enter positions.
   - *Suggested fix:* Always acquire `self._lock` before calling `_enter_position()` or `_exit_position()` from any thread.

4. **Severity: MEDIUM** — **`_fetch_low_tf_candle()` reads `data.get('price', 0)`** (line ~400), but `fetch_all_data()` returns a dict with keys `oi_total`, `volume_total`, `exchanges_data`, etc. — **there is no `'price'` key**. `current_price` will always be 0, rendering the multi-timeframe spike detection completely non-functional.
   - *Suggested fix:* Extract price from `exchanges_data['hyperliquid']['mark_price']` or use `get_cached_price()`.

5. **Severity: MEDIUM** — **`_monitor_loop` and `_mtf_loop` catch all `Exception` and continue** (lines ~285, ~335). If a persistent error occurs (e.g., API key revoked, network down), the threads will spin in a tight error loop instead of backing off exponentially.
   - *Suggested fix:* Add an error counter; after N consecutive errors, increase sleep time or signal the main thread to shut down.

6. **Severity: MEDIUM** — **Flaky time-based logging in `_check_exit_signals_fast()`** (line ~445): `self.last_check_time % 60 < self._monitor_interval`. `last_check_time` is `time.time()` (epoch seconds, a huge float). `% 60` gives seconds-of-current-minute. The condition is essentially random and can cause log spam or silence unpredictably.
   - *Suggested fix:* Track a separate `last_trail_log_time` and use `time.time() - last_trail_log_time >= 60`.

7. **Severity: MEDIUM** — **`AutoTuner` hardcodes min/max thresholds** (lines ~55–58): `min_volume=2.0`, `max_volume=5.0`, etc. These should be configurable.
   - *Suggested fix:* Read from `config['strategy']['auto_tuner_limits']` with these as defaults.

8. **Severity: LOW** — **Inconsistent fee calculation** (line ~485). `_enter_position()` uses `position_size * self.fee_pct * 2` (entry+exit estimated), but `_exit_position()` also applies `position_size * self.fee_pct * 2`. Fees are applied twice: once on entry and once on exit. The entry fee is actually charged immediately, and then on exit another double fee is charged. Total fees = 4× instead of 2×.
   - *Suggested fix:* Charge `fee_pct` on entry and `fee_pct` on exit separately, not `2×` at each stage.

9. **Severity: LOW** — **`fetch_and_process_candle()` has an unclosed `conn.close()` pattern** (line ~525). If an exception occurs between `cursor.fetchone()` and `conn.close()`, the connection leaks until GC.
   - *Suggested fix:* Use `with self.db._get_conn() as conn:` context manager.

10. **Severity: LOW** — **`run_continuous()` attempts `thread.join()` on threads that may never have been started** (line ~785). If `run_cycle()` is called directly (e.g., `--test` mode), `self._monitor_thread` and `self._mtf_thread` are `None`, but the `KeyboardInterrupt` handler still tries to join them.
    - *Suggested fix:* Check `if self._monitor_thread is not None:` before joining.

---

## File: strategy.py
### Issues Found: 4

1. **Severity: HIGH** — **Direct dict access on config without `.get()`** (line ~23): `self.volume_threshold = config['strategy']['volume_spike_threshold']`. If any key is missing, the bot crashes on startup.
   - *Suggested fix:* Use `.get()` with sensible defaults, or validate config at boot and exit gracefully with a clear message.

2. **Severity: MEDIUM** — **`should_exit()` only handles LONG positions** (line ~76). If a SHORT position is open and OI starts dropping, the method returns `None`, missing a valid exhaustion signal.
   - *Suggested fix:* Add symmetrical logic for short exits (e.g., OI rising while price drops = short exhaustion).

3. **Severity: LOW** — **Hardcoded OI exhaustion threshold** (line ~79): `oi_change < -0.005`. Should be configurable.
   - *Suggested fix:* Move to config as `oi_exhaustion_threshold`.

4. **Severity: LOW** — **`analyze()` modifies state even on no-signal candles** (line ~52). `self.volume_history.append(volume_total)` grows on every call regardless of data quality. If `volume_total` is 0 (API failure), it poisons the moving average.
   - *Suggested fix:* Skip appending if `volume_total == 0` or data is stale.

---

## File: risk_manager.py
### Issues Found: 2

1. **Severity: MEDIUM** — **`calculate_position_size()` accepts `confidence` without bounds checking** (line ~39). Passing `confidence=10.0` would create a position 10× the max, bypassing the risk limit.
   - *Suggested fix:* Clamp `confidence` to `[0.0, 1.0]` or raise `ValueError`.

2. **Severity: LOW** — **`check_stop_loss()` has potential division by zero** (line ~48). `entry_price` of 0 would crash. Currently protected by caller logic, but the method itself is not defensive.
   - *Suggested fix:* Add `if entry_price <= 0: return False` at the top.

---

## File: utils.py
### Issues Found: 2

1. **Severity: LOW** — **`load_config()` uses a relative path default** (line ~11): `path: str = "config/settings.yaml"`. If the working directory is not the project root, the file is not found.
   - *Suggested fix:* Resolve relative to the script: `Path(__file__).parent.parent / "config" / "settings.yaml"`.

2. **Severity: LOW** — **No YAML validation or schema checking** (line ~15). A malformed config will raise a cryptic `yaml.YAMLError` instead of a user-friendly message.
   - *Suggested fix:* Wrap `yaml.safe_load()` in a try/except and print a helpful error.

---

## File: dashboard_web.py
### Issues Found: 4

1. **Severity: MEDIUM** — **Unbounded `total_signals` counter** (line ~275). Every dashboard page load increments `total_signals` if a signal fires. Over long uptimes this will grow without bound (not a memory leak per se, but a logic bug if displayed as "sinais hoje").
   - *Suggested fix:* Reset `total_signals` daily or rename the label.

2. **Severity: MEDIUM** — **CORS allows all origins** (line ~48): `CORS(self.app)`. If the dashboard is ever exposed beyond `localhost`, any website can call the API endpoints.
   - *Suggested fix:* Restrict to `origins=["http://127.0.0.1:5000", "http://localhost:5000"]`.

3. **Severity: LOW** — **Hardcoded `$10,000` initial capital in HTML template** (line ~87). Doesn't reflect `config['risk']['initial_capital']`.
   - *Suggested fix:* Pass `initial_capital` as a template variable.

4. **Severity: LOW** — **No API response caching in `_update_data()`** (line ~237). Every HTTP request to `/` triggers `fetch_all_data()` for every asset. At the default browser auto-refresh of 30s, this can hit API rate limits quickly.
   - *Suggested fix:* Cache results for 10–15 seconds and serve stale data.

---

## File: main.py
### Issues Found: 3

1. **Severity: MEDIUM** — **Multiple direct config key accesses without `.get()`** (lines ~28–30): `config['bot']['paper_trading']`, `config['assets']`, `config['polling']['oi_interval']`. Missing any key crashes the bot on startup.
   - *Suggested fix:* Validate required config keys at boot with a `REQUIRED_KEYS` list and fail gracefully.

2. **Severity: LOW** — **`oi_interval` vs `price_interval` logic is confusing** (line ~56). The main loop sleeps `price_interval` seconds, but OI is fetched every `oi_interval`. If `price_interval` > `oi_interval`, the OI condition is effectively delayed.
   - *Suggested fix:* Use a single interval variable, or use `min(oi_interval, price_interval)` for the sleep.

3. **Severity: LOW** — **No graceful shutdown on exceptions other than `KeyboardInterrupt`** (line ~62). A network blip will crash the entire process.
   - *Suggested fix:* Wrap the loop body in a try/except that logs and continues after a backoff.

---

## File: database.py
### Issues Found: 3

1. **Severity: HIGH** — **SQL Injection in `get_candles()` via `limit` parameter** (line ~98):
   ```python
   if limit:
       query += f" LIMIT {limit}"
   ```
   `limit` is passed directly into the SQL string without validation. If an attacker passes `"1; DROP TABLE candles;"`, the query will execute.
   - *Suggested fix:* Use parameterized queries: `query += " LIMIT ?"` and add `limit` to `params`. Or validate that `limit` is an integer `<= 10000`.

2. **Severity: MEDIUM** — **N+2 query problem in `get_candles_for_backtest()`** (line ~112). For every candle returned, two additional queries are executed (one for OI, one for funding). With 10,000 candles this is 20,001 queries.
   - *Suggested fix:* Load all OI and funding data into dicts keyed by timestamp once, then do dict lookups per candle.

3. **Severity: LOW** — **`save_candles()` uses individual `INSERT` per candle** (line ~78). Inefficient for large batches.
   - *Suggested fix:* Use `executemany()` for batch insertion.

---

## File: exchange_client.py
### Issues Found: 2

1. **Severity: MEDIUM** — **Suspicious/invalid f-string format specifier** (line ~42):
   ```python
   f"Preço: ${price:,.2f if price else 'MARKET'}"
   ```
   This ternary expression inside a format specifier is **not valid Python** and will raise `ValueError: Invalid format specifier` at runtime whenever `place_order()` is called with a non-None price. If `price` is `None`, it will also raise `TypeError` before reaching the format step.
   - *Suggested fix:* Split into two f-strings or use a helper variable:
   ```python
   price_str = f"${price:,.2f}" if price else "MARKET"
   ```

2. **Severity: LOW** — **`get_balance()` hardcodes `$10,000`** (line ~77). Should read from config.
   - *Suggested fix:* Accept `initial_capital` parameter or read from `config['risk']['initial_capital']`.

---

## File: backtest.py (CSV engine — legacy)
### Issues Found: 2

1. **Severity: MEDIUM** — **Fragile position-size retrieval in `_exit_position()`** (line ~152): `position_size = self.trades[-1]['size']`. If the last trade in the list is an exit (due to a bug or edge case), it retrieves the wrong size.
   - *Suggested fix:* Search backwards for the most recent entry trade, similar to how `backtest_db.py` does it.

2. **Severity: LOW** — **No validation of CSV columns in `load_data()`** (line ~56). Missing columns will raise `KeyError` on `row['timestamp']` etc.
   - *Suggested fix:* Check `reader.fieldnames` against a required set before iterating.

---

## File: backtest_db.py
### Issues Found: 1

1. **Severity: LOW** — **`_exit_position()` calculates PnL using `self.entry_price`** which was set by `_enter_long/short()`. However, if multiple overlapping entries were somehow allowed (not currently possible due to `current_position` check), the exit would use the most recent entry price, not the one matching the trade being exited. With current logic this is safe, but the method is not robust.
   - *Suggested fix:* Store entry price inside the entry trade dict and retrieve it from there, as already done for `position_size`.

---

## File: data_downloader.py
### Issues Found: 2

1. **Severity: LOW** — **No User-Agent header on requests** (lines ~45, ~118). Binance and other exchanges may rate-limit or block requests without a proper UA.
   - *Suggested fix:* Add a session with `headers={'User-Agent': '...'}`.

2. **Severity: LOW** — **`download_open_interest_history()` silently changes interval** (line ~95). If the caller passes an unsupported OI interval, it falls back to `'1h'` without any warning, which could surprise the user.
   - *Suggested fix:* Log a warning when falling back.

---

## File: oi_downloader.py
### Issues Found: 2

1. **Severity: MEDIUM** — **Infinite loop risk in pagination** (line ~38). The `while True` loop has no maximum iteration cap. If `first_ts` stops changing (e.g., exchange returns the same page repeatedly), the loop runs forever.
   - *Suggested fix:* Add a `max_pages` counter (e.g., 100) and break if exceeded.

2. **Severity: LOW** — **`fetchOpenInterestHistory` pagination parameter may be incorrect** (line ~48). The code uses `params={'endTime': last_end_time - 1}`, but CCXT's Binance implementation may expect `since` (startTime) for forward pagination rather than `endTime` for backward pagination. This depends on the CCXT version.
   - *Suggested fix:* Test pagination manually and verify the `since` parameter works correctly with CCXT.

---

## File: optimizer.py
### Issues Found: 3

1. **Severity: MEDIUM** — **Division by zero in OI change calculation** (line ~178):
   ```python
   oi_change = (oi - oi_history[-2]) / oi_history[-2]
   ```
   If `oi_history[-2] == 0`, this crashes. No guard exists.
   - *Suggested fix:* `oi_change = (oi - oi_history[-2]) / oi_history[-2] if oi_history[-2] > 0 else 0`.

2. **Severity: MEDIUM** — **`profit_factor = float('inf')` when no losses** (line ~240). In grid search, strategies with very few trades (e.g., 1 win, 0 losses) get `PF=inf` and sort to the top, falsely appearing optimal.
   - *Suggested fix:* Require `total_trades >= self.min_trades` AND `total_losses > 0` for a valid profit factor, or penalize low trade counts.

3. **Severity: LOW** — **Code duplication**: `_run_backtest()` re-implements almost the same backtest logic found in `backtest_db.py::BacktestEngineDB.run()`. Any strategy change must be updated in two places.
   - *Suggested fix:* Refactor `BacktestEngineDB.run()` to accept an optional `params` dict override, then call it from the optimizer.

---

## File: dashboard.py
### Issues Found: 1

1. **Severity: LOW** — **`RICH_AVAILABLE = False` fallback just prints a message and returns** (line ~20). If Rich is not installed, the dashboard silently exits with no error code.
   - *Suggested fix:* Return exit code 1 or raise `RuntimeError` so the caller knows the dashboard failed to start.

---

## File: __init__.py
### Issues Found: 0

---

# 🚨 TOP 5 CRITICAL ISSUES

| Rank | Issue | File | Severity | Why It Matters |
|------|-------|------|----------|----------------|
| **1** | `bullish_count` / `bearish_count` never initialized | `paper_trading.py` | **HIGH** | `run_cycle()` crashes with `AttributeError` on first signal check. The paper trader is completely broken. |
| **2** | Undefined variable `price` in `run_cycle()` | `paper_trading.py` | **HIGH** | `self._htf_price = price` will raise `NameError`. Multi-timeframe direction logic fails. |
| **3** | Race condition on position entry between main thread and MTF thread | `paper_trading.py` | **HIGH** | Two threads can call `_enter_position()` simultaneously without locks, leading to double positions, capital desync, and DB corruption. |
| **4** | `_fetch_low_tf_candle()` reads non-existent `'price'` key | `paper_trading.py` | **HIGH** | The multi-timeframe "spike detector" always gets price=0, making it entirely non-functional. |
| **5** | SQL injection via `limit` parameter in `get_candles()` | `database.py` | **HIGH** | If `limit` is ever user-controlled (or from a config file), an attacker can execute arbitrary SQL. Even without an attacker, passing a string by mistake will corrupt the database. |

### Immediate Action Items
1. **Fix the 4 paper_trading.py bugs before any live or paper test.** These are showstoppers.
2. **Sanitize the `limit` parameter in `database.py`.** This is a security issue.
3. **Audit all shared-state reads/writes** between the monitor thread, MTF thread, and main thread. Add `threading.Lock()` consistently around every state mutation.
4. **Run the bot in `--test` mode after fixes** and verify `AttributeError` / `NameError` no longer occur.
5. **Add a startup config validator** that checks all required keys exist with correct types, so missing config produces a clear error instead of a `KeyError` traceback.
