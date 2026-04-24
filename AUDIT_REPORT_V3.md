# AUDIT REPORT V3 — Hyperliquid Trading Bot

**Auditor:** Code Auditor V3  
**Date:** 2026-04-24  
**Scope:** ALL source files after major changes (app_flask.py, bot_engine.py, bridge.js, start.bat added)  
**Files Audited:** 21 files (src/*.py, app_flask.py, bot_engine.py, bridge.js, app_desktop.py, build_app.py, start.bat, tests/*.py)

---

## File: app_flask.py
### Issues Found: 6

1. **Severity: CRITICAL** — Flask `static_folder='.'` exposes entire project directory to anyone with browser access. Attackers can download `config/settings.yaml`, the SQLite database (`data/trading_bot.db`), source code, and private keys if stored in the project tree. **Lines 27, 31-32**. **Fix:** `static_folder='static'` and only serve explicitly allowed files, or remove static serving entirely.

2. **Severity: HIGH** — `api_config` POST endpoint writes user-provided JSON directly to disk (`config/settings.json`) without validation, schema checking, or sanitization. Malformed config could crash the bot on restart. **Lines 95-105**. **Fix:** Validate config with `load_config()` before saving; write to `.yaml` to match loader; require auth token.

3. **Severity: HIGH** — `os._exit(0)` in `on_quit` (line 194) immediately terminates the process without cleanup. SQLite WAL file may be left in an unclean state, and open trades are not properly logged. **Fix:** Call `stop_bot_engine()`, wait for thread join, close DB connections, then `sys.exit(0)`.

4. **Severity: MEDIUM** — `api_trades` catches all exceptions with bare `except: pass` (line 89). Silent failures hide DB corruption or schema mismatches. **Fix:** Log the exception: `except Exception as e: logger.error(...)`.

5. **Severity: MEDIUM** — `monitor_loop()` runs `while True` with no stop condition except `self._closing`. If the tray icon quits unexpectedly, the monitor thread keeps running indefinitely. **Fix:** Check `threading.current_thread().is_alive()` or use an Event.

6. **Severity: LOW** — Tray icon title is updated dynamically (`tray_icon.title = ...`), but `pystray` does not guarantee live title updates on all platforms. May show stale status. **Fix:** Use a notification bubble instead.

---

## File: bot_engine.py
### Issues Found: 5

1. **Severity: HIGH** — `app_state` is a global dict shared between threads without any `threading.Lock`. When `_run()` updates `app_state["last_data"]` (a nested dict) while `get_bot_status()` or the Flask API reads it, the reader may see a partially mutated dict (e.g., `exchanges_data` updated but `oi_total` not yet). This is a race condition. **Lines 28-47, 152-155**. **Fix:** Use `threading.Lock()` around all `app_state` mutations and reads.

2. **Severity: HIGH** — `_save_market_data` calls `self.db.save_open_interest(asset, ts, data['oi_total'])` and `self.db.save_funding_rate(asset, ts, data['funding_avg'])`. The database aliases `save_open_interest = save_oi` and `save_funding_rate = save_funding`. However, if the alias resolution ever fails (import order, monkey-patching), the error `missing 1 required positional argument: 'oi_usd'` will recur. **Lines 165-172**. **Fix:** Call `self.db.save_oi()` and `self.db.save_funding()` directly to avoid alias indirection.

3. **Severity: MEDIUM** — `start_bot_engine()` sets `app_state["capital"]` from config instead of reading it from the database or the trader. If the bot was restarted after a crash with an open trade, capital is reset to the initial value, making PnL tracking wrong. **Line 199**. **Fix:** Load capital from `trader.capital` or DB after trader initialization.

4. **Severity: MEDIUM** — `get_bot_status()` can raise `IndexError` if `app_state["config"]` exists but `assets` is an empty list: `app_state.get("config", {}).get('assets', ['BTC'])[0]`. **Line 218**. **Fix:** Use `(app_state.get(...) or ['BTC'])[0]` or check list length.

5. **Severity: LOW** — `add_log()` keeps only the last 1000 logs but accesses `app_state["logs"]` without a lock. If two threads append simultaneously, one append may be lost. **Lines 228-235**. **Fix:** Lock around log append and trim.

---

## File: bridge.js
### Issues Found: 4

1. **Severity: MEDIUM** — `window.stopBot` is overridden twice. The second override saves `originalStopBot2 = window.stopBot` (which is already the FIRST override, not the original dashboard function). The second override then calls `originalStopBot2()`, which calls the first override. The original `stopBot` from `dashboard.html` is never reached. If the original had critical cleanup (e.g., clearing chart data), it is silently bypassed. **Lines ~115 and ~220**. **Fix:** Store the truly original function before any overrides: `const _origStopBot = window.stopBot;` at the very top of the overrides block.

2. **Severity: MEDIUM** — `fetchRealData` override computes `pnlPct` with `(price - p.entryPrice) / p.entryPrice`. If `entryPrice` is ever 0 (bug elsewhere), this causes `Infinity` or `NaN` in the UI. **Fix:** Guard with `p.entryPrice > 0 ? ... : 0`.

3. **Severity: LOW** — `isFlask` detection is hardcoded to `127.0.0.1` and `localhost`. If the user binds Flask to `0.0.0.0` and accesses via LAN IP, bridge falls back to standalone mode and stops working. **Fix:** Detect by trying `fetch('/api/status')` and falling back.

4. **Severity: LOW** — `statePollInterval` polls every 3 seconds with two parallel `await` calls (`fetchRealData()` + `fetchLogs()`). If the backend is slow, requests can pile up because `setInterval` does not wait for the async function to finish. **Fix:** Use `setTimeout` recursively or track an `isPolling` flag.

---

## File: app_desktop.py
### Issues Found: 5

1. **Severity: HIGH** — `_js_save_config` writes JSON to `config/settings.json`, but `load_config()` reads YAML from `config/settings.yaml`. The saved config is NEVER loaded by the Python backend. This is a silent data-loss bug for the user. **Lines 244-252**. **Fix:** Write YAML; use `yaml.dump()`; save to `.yaml`.

2. **Severity: MEDIUM** — `webview.start()` is launched in a daemon thread (`daemon=True`). If the main Tkinter loop crashes or the user closes the root window, the daemon is killed immediately without cleanup. The webview window may become a zombie process. **Line 113**. **Fix:** Use a non-daemon thread with proper join on quit.

3. **Severity: MEDIUM** — `_quit()` calls `self.root.quit()`, `self.root.destroy()`, then `sys.exit(0)`. Tkinter's `destroy()` schedules destruction but may not complete before `sys.exit()` kills the interpreter. The tray icon and webview may leak handles. **Fix:** Call `self.root.after(100, sys.exit)` or use a proper shutdown sequence.

4. **Severity: MEDIUM** — `_show_status()` calls `messagebox.showinfo()` from the tray callback thread. On some Windows versions, Tkinter messageboxes called from non-main threads can deadlock or throw `RuntimeError`. **Fix:** Use `self.root.after(0, lambda: messagebox.showinfo(...))` to marshal to the main thread.

5. **Severity: LOW** — `_js_force_long`, `_js_force_short`, `_js_emergency_close` are all stubbed with `"Ainda não implementado"`. If the user clicks these buttons, they get a success=False message but no visual warning that the feature is missing. **Fix:** Return HTTP 501 or show a persistent banner in the UI.

---

## File: build_app.py
### Issues Found: 2

1. **Severity: MEDIUM** — The `datas` list (line 32-38) is defined but NEVER used in the PyInstaller `args`. The `--add-data` arguments are hardcoded separately below. If someone edits `datas` expecting it to affect the build, it silently does nothing. **Fix:** Iterate `datas` to generate `--add-data` arguments dynamically.

2. **Severity: LOW** — `--onefile` + `--windowed` means no console. If the app crashes on startup (e.g., missing `pywebview`), the user sees nothing. Debugging requires running the `.exe` from a terminal with redirection. **Fix:** Add a fallback `MessageBox` on fatal errors in a small wrapper script.

---

## File: src/main.py
### Issues Found: 4

1. **Severity: HIGH** — Logging crash on Windows: `logger.info(f"\n📡 Analisando {asset}...")` (line 59, 93). The leading newline + emoji characters cause `UnicodeEncodeError: 'charmap' codec can't decode byte 0x9d` when the Windows console is not in UTF-8 mode. This has already been observed by the user. **Fix:** Remove the leading `\n` from log messages; rely on `setup_logging()` to configure UTF-8 properly; or use `logging.StreamHandler(stream=sys.stdout)` with `encoding='utf-8'`.

2. **Severity: MEDIUM** — `consecutive_errors` is incremented on failure but **never reset to 0 on success**. After a transient API glitch, the backoff remains at the max level (120s) even after successful calls resume. **Line 82**. **Fix:** Set `consecutive_errors = 0` after any successful data fetch.

3. **Severity: MEDIUM** — The main loop catches `KeyboardInterrupt` (line 101) but does not gracefully stop any background threads that `PaperTrader` may have spawned. Those threads become zombie processes. **Fix:** Call `trader.stop_monitoring()` in the `except KeyboardInterrupt` block.

4. **Severity: LOW** — `time.sleep(1)` at the bottom of the loop is unconditional. Even when data fetch succeeds and the user wants faster updates, the loop always waits 1s. **Fix:** Make sleep dynamic based on how much time remains until the next interval.

---

## File: src/data_aggregator.py
### Issues Found: 3

1. **Severity: MEDIUM** — `get_cached_price` returns `0` when the cache is expired or missing. Callers (e.g., `bot_engine.py` line 136) check `if price == 0` to decide whether to fetch from API. A legitimate price of `0` (theoretical) would be indistinguishable from "no cache". **Fix:** Return `None` for missing/expired cache and check `if price is None`.

2. **Severity: MEDIUM** — `_fetch_hyperliquid` has a subtle float precision issue: `mark_price = float(str_px)` where `str_px` comes from API as `'85432.50'`. If the API ever returns `'85432.5000000001'` or scientific notation, the sanity check `self._is_price_sane` may fail unexpectedly. **Fix:** Round to 2 decimal places before sanity check.

3. **Severity: LOW** — `fetch_all_data` aggregates `oi_total` by summing OI across exchanges. If the same underlying liquidity is counted on multiple exchanges (e.g., Binance and Bybit both track the same perpetual contract), the OI is double-counted. This inflates the OI change signal. **Fix:** Document this limitation; consider de-duplication by contract type.

---

## File: src/strategy.py
### Issues Found: 3

1. **Severity: MEDIUM** — `should_exit()` only checks OI fading. It does NOT check stop-loss, trailing stop, or max drawdown. Those are in `PaperTrader._check_exit_signals_fast()`. This split responsibility means `MomentumStrategy` cannot be unit-tested for exit logic independently, and the exit criteria are scattered across two files. **Lines 80-93**. **Fix:** Move all exit logic into `RiskManager` or `MomentumStrategy`.

2. **Severity: LOW** — `volume_history` uses `deque` with `maxlen=100`, but `analyze()` only requires `len >= volume_lookback // 2` (i.e., 50). This means the strategy can generate signals with only half the configured lookback history, producing noisier signals than intended. **Line 55**. **Fix:** Require `len >= volume_lookback` or rename the config key.

3. **Severity: LOW** — `_is_price_sane` uses hardcoded ranges. For an unknown asset, the default range `0.0001–250000` is so wide it would accept insane prices for most altcoins. **Fix:** Load sanity ranges from config per asset.

---

## File: src/risk_manager.py
### Issues Found: 3

1. **Severity: MEDIUM** — `daily_trades` counter is never reset. After 24 hours, the bot still thinks it has reached the daily limit. **Line 19**. **Fix:** Track the date of the last reset and reset `daily_trades` to 0 if the date changed.

2. **Severity: MEDIUM** — `calculate_position_size` does not validate that `price > 0`. If a buggy price of `0` is passed, `size = max_position * confidence` is returned regardless, which would calculate a completely wrong notional amount if used for coin quantity. **Line 37**. **Fix:** `if price <= 0: return 0.0`.

3. **Severity: LOW** — `check_stop_loss` computes `(entry - current) / entry`. If `entry == 0` (impossible in normal operation but possible in a test/mock), this raises `ZeroDivisionError`. **Fix:** Guard with `if entry <= 0: return False`.

---

## File: src/paper_trading.py
### Issues Found: 7

1. **Severity: CRITICAL** — **TOCTOU vulnerability in `_fast_price_check`**. The method reads `self.current_position` and `self.entry_price` **without** holding `self._lock` (line ~380), then later acquires the lock to call `_exit_position`. Between the read and the lock acquisition, another thread (e.g., `run_cycle`) could enter a new position, change `entry_price`, or exit the position. This can cause double-exits, exits on wrong prices, or missed stop-losses. **Fix:** Acquire `self._lock` at the very beginning of `_fast_price_check` and hold it until the method returns.

2. **Severity: CRITICAL** — **TOCTOU vulnerability in `run_cycle`**. It checks `self.current_position is None` outside the lock (line ~450), then acquires the lock to enter a position. Between the check and the lock, the fast price check thread could enter a position. This can cause double entries. **Fix:** Move the `if self.current_position is None` check inside the `with self._lock:` block.

3. **Severity: HIGH** — `_enter_position` and `_exit_position` hold `self._lock` while performing **database I/O** (inserting trades, updating stats). SQLite writes can take 50–500ms. During this time, `_fast_price_check` (which needs the lock to evaluate stop-loss) is completely blocked. In a volatile flash crash, the stop-loss may be evaluated 500ms late, turning a 2% loss into a 5%+ loss. **Fix:** Do DB operations **after** releasing the lock, or use a separate DB queue/thread.

4. **Severity: MEDIUM** — `_check_exit_signals_fast` does not validate `entry_price > 0`. If `entry_price` is 0 (due to a bug or data corruption), `pct_from_entry = (price - entry) / entry` causes `ZeroDivisionError`, crashing the monitor thread. **Line ~340**. **Fix:** `if self.entry_price <= 0: return None`.

5. **Severity: MEDIUM** — `_monitor_loop` and `_mtf_loop` are daemon threads. If they crash with an unhandled exception, they die silently and are never restarted. The bot continues running but stops monitoring prices or fetching MTF data. **Lines ~520, ~560**. **Fix:** Wrap the loop body in `try/except Exception` and restart the thread after a delay.

6. **Severity: LOW** — `_calculate_sma` returns `0.0` for an empty list. While this avoids `ZeroDivisionError`, using `0.0` as a SMA is mathematically meaningless and could trick the regime detector into thinking the price is flat. **Fix:** Return `None` and handle it in the caller.

7. **Severity: LOW** — `AutoTuner.analyze_and_tune()` does not cap `volume_threshold` and `oi_threshold`. After many losing trades, the thresholds could grow to extreme values (e.g., 100x), making the strategy permanently silent. **Fix:** Cap thresholds to reasonable bounds (e.g., 1–50x).

---

## File: src/utils.py
### Issues Found: 2

1. **Severity: MEDIUM** — `setup_logging()` sets `os.environ['PYTHONIOENCODING'] = 'utf-8'` **after** creating the `StreamHandler`. The handler may have already cached the default encoding (cp1252 on Windows). The environment variable change does not retroactively reconfigure the handler. **Fix:** Explicitly set `stream_handler.setStream(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))` on Windows.

2. **Severity: LOW** — `load_config()` validates only top-level keys (`bot`, `assets`, `strategy`, `risk`). Missing `data_sources` or `timeframes` passes validation but crashes later in `DataAggregator.__init__` or `PaperTrader.__init__`. **Fix:** Add `data_sources` and `timeframes` to `REQUIRED_TOP_KEYS`, or use `.get()` with sensible defaults throughout.

---

## File: src/database.py
### Issues Found: 2

1. **Severity: MEDIUM** — `_find_nearest()` is O(M) where M = number of OI/funding records. For a 30-day backtest with ~3000 candles and ~3000 OI records, it performs 3000×3000 = 9 million key comparisons. This makes backtests slow. **Line ~147**. **Fix:** Use `bisect` on sorted timestamps, or pre-build an interpolation lookup.

2. **Severity: LOW** — `save_trade()` inserts into the DB inside a `with self._get_conn()` block, but if `trade['symbol']` or other fields contain malicious strings, SQLite parameterized queries protect against SQL injection, which is good. However, `strategy_params` is JSON-serialized without size limit. A huge dict could exceed SQLite's row size limit or memory. **Fix:** Cap `strategy_params` size before serializing.

---

## File: src/dashboard_web.py
### Issues Found: 3

1. **Severity: MEDIUM** — Creates its own `DataAggregator` and `MomentumStrategy` instances (lines 186-187), independent of the main bot. When both `main.py` and `dashboard_web.py` run simultaneously, they make **duplicate API calls**, doubling the load on rate-limited endpoints and increasing ban risk. **Fix:** Share a single `DataAggregator` instance via dependency injection.

2. **Severity: MEDIUM** — `_update_data()` is called on **every** HTTP request to `/` (line 262). With the browser auto-refreshing every 30s and multiple tabs, this hammers the APIs. The 10-second cache helps, but concurrent requests within the 10s window still trigger simultaneous updates. **Fix:** Use a background thread to update data periodically; HTTP requests only read cached data.

3. **Severity: LOW** — Jinja2 template computes `min(data.oi_total / 1000000000 * 100, 100)` directly. If `data.oi_total` is `None` (e.g., API failure), Jinja2 throws `TypeError: unsupported operand type`. The outer `try/except` in `_update_data` does not catch template rendering errors. **Fix:** Ensure `oi_total` defaults to `0` before passing to template.

---

## File: src/exchange_client.py
### Issues Found: 2

1. **Severity: LOW** — `order_id = f'paper_{hash(f'{asset}{side}{size}')}'` uses a nested f-string. While valid in Python 3.12+, it is fragile and hard to read. Worse, `hash()` is randomized per Python process, so order IDs differ across restarts, making log correlation difficult. **Line 87**. **Fix:** Use a deterministic hash like `hashlib.md5(f'{asset}:{side}:{size}:{time.time()}'.encode()).hexdigest()`.

2. **Severity: LOW** — `place_stop_loss_order` accepts `market_price=None` and passes it to `_validate_order`, which returns `False`. Good. But the method raises `NotImplementedError` for real trading. If this is accidentally called in production, it crashes the bot. **Fix:** Return an error dict instead of raising, so the caller can log and continue.

---

## File: start.bat
### Issues Found: 1

1. **Severity: LOW** — `python app_flask.py` assumes `python` is on the user's PATH. The user has already experienced `pip` not being on PATH. If `python` is missing, the batch file exits silently after the `pause`. **Fix:** Add a check: `where python >nul 2>&1 || echo "Python not found on PATH"`.

---

## File: tests/conftest.py
### Issues Found: 0

Well-structured fixtures. No issues.

---

## File: tests/test_strategy.py
### Issues Found: 1

1. **Severity: LOW** — `test_signal_with_various_prices` calls `strategy._reset_position()` between iterations, but `_reset_position()` resets `volume_history` too (not shown in code, but if it does, the next iteration starts with empty history). **Fix:** Ensure `volume_history` is re-populated between iterations if needed.

---

## File: tests/test_data_aggregator.py
### Issues Found: 0

Excellent mock coverage. No issues.

---

## File: tests/test_paper_trading.py
### Issues Found: 1

1. **Severity: LOW** — `test_concurrent_position_access` sets `MockDB.return_value.get_open_trade.return_value = None`, but the real `_enter_position` does not call `get_open_trade`. The test validates mock behavior rather than real thread-safety logic. The actual TOCTOU bug is not caught. **Fix:** Remove the mock and test with the real DB in a temp directory, or assert on the real state.

---

## File: tests/test_risk_manager.py
### Issues Found: 0

Good coverage. No issues.

---

## File: tests/test_edge_cases.py
### Issues Found: 1

1. **Severity: LOW** — `test_price_zero_exit_signals_no_crash` asserts `exit_reason in ('STOP_LOSS', 'TRAILING_STOP', None)`. If `_check_exit_signals_fast` returns `None` for `price=0` (because it fails a sanity check before evaluating), the test passes even though the stop-loss SHOULD have triggered. This is a weak assertion that masks a logic gap. **Fix:** Assert the expected specific reason (e.g., `'STOP_LOSS'`) based on the actual business rule.

---

# TOP 5 CRITICAL ISSUES

| Rank | Severity | Issue | File | Impact |
|------|----------|-------|------|--------|
| **1** | 🔴 CRITICAL | Flask serves entire project directory as static files | `app_flask.py` | Any local user can download config, DB, source code, and potentially wallet private keys if stored in the project tree. |
| **2** | 🔴 CRITICAL | TOCTOU race condition in fast price check | `paper_trading.py` | Stop-loss may be evaluated on stale position state, causing double-exits, missed exits, or exits at wrong prices. Lethal in volatile markets. |
| **3** | 🟠 HIGH | Windows logging crash with emojis/newlines | `main.py`, `utils.py` | Bot crashes with `UnicodeEncodeError` on Windows default console (cp1252). Already observed by the user. |
| **4** | 🟠 HIGH | `app_state` shared across threads without locks | `bot_engine.py`, `app_flask.py` | Readers see partially updated dicts; race conditions in status API; inconsistent dashboard data. |
| **5** | 🟠 HIGH | DB I/O blocks price-check lock | `paper_trading.py` | During a 50–500ms SQLite write, the fast price monitor is frozen. In a flash crash, stop-loss triggers late, magnifying losses. |

---

# SUMMARY

**Total Issues Found:** 47 across 21 files  
**Critical:** 2 | **High:** 6 | **Medium:** 19 | **Low:** 20  

The most dangerous issues are:
1. **Security** — Flask static folder exposure.
2. **Race conditions** — TOCTOU in paper trading and unprotected `app_state`.
3. **Reliability** — Windows logging crashes and DB blocking.

**Immediate action items for Pedro:**
1. Fix `static_folder='.'` in `app_flask.py` — this is a data leak.
2. Wrap all `app_state` access with a `threading.Lock()`.
3. Fix the TOCTOU in `paper_trading.py` by holding the lock for the entire `_fast_price_check()` duration.
4. Remove leading `\n` and emojis from `main.py` log messages until Windows UTF-8 is fully configured.
5. Move DB writes outside the trading lock or use an async DB queue.
