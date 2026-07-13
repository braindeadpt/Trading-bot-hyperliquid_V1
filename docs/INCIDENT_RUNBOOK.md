# Incident Runbook (Operator Guide)

*For a human operator responding to a live incident once the bot is running
on testnet or mainnet. This is not a developer guide — it assumes you can run
the commands below but does not require reading source code first.*

As of 2026-07-13, the bot has **never been run against real testnet or
mainnet money** (see `docs/MAINNET_READINESS.md`). This runbook is written
ahead of that so the procedures exist before they're needed under pressure.

---

## 1. Kill switch — stop everything immediately

**When to use it:** the bot is doing something wrong and you need to stop
new risk-taking and close existing exposure right now, without waiting to
diagnose the root cause first.

**How to invoke it:**

- If the process is reachable and you have a way to call into it (dashboard
  action, an operator script, or a Python one-liner against a running
  engine instance), call `TradingEngine.kill_switch()`. This is the code
  path exercised by `tests/test_testnet_e2e.py::test_kill_switch_flattens_all_positions`.
- If the process is unresponsive or you have no live hook into it, the
  blunt fallback is:
  1. Stop the bot process (`stop.bat`, or kill the `python.exe main.py`
     process directly).
  2. Log into Hyperliquid directly (web UI or your own script using the
     same API key) and manually cancel all open orders and close all open
     positions. The bot process being down means nothing further will
     protect or manage those positions automatically.

**What the kill switch guarantees** (from `ExecutionEngine.kill_switch()` in
`src/core/execution.py:1108`):
- Cancels all open orders on the exchange (`cancel_all_orders()`).
- Flattens (market-closes) every open position (`flatten_all_positions()`).
- Confirms the exchange reports flat (`confirm_flat()`) before returning.
- Cancels any resting native SL/TP trigger orders for closed positions.
- Clears the bot's local view of open positions and trades so it does not
  try to manage them further.
- `TradingEngine.kill_switch()` additionally forces one
  `ExchangeReconciler.reconcile_once()` pass immediately after, so local
  state and exchange state are re-synced in the same operation.

**What it does NOT guarantee:**
- **Network partition risk**: if the bot's connection to Hyperliquid is down
  when you invoke this, the cancel/flatten calls themselves cannot reach the
  exchange. The kill switch reports errors per failed step (`result.errors`)
  rather than silently pretending success — check that list. If it's
  non-empty, you must fall back to the manual exchange-UI path above.
- **Exchange-side latency / partial fills**: `flatten_all_positions()`
  issues market orders; on a fast-moving or illiquid market these can slip
  or partially fill. `confirm_flat()` is what actually verifies the end
  state — trust that field, not just "the call returned."
- Anything that already happened before you invoked it (a bad fill that
  already landed is not undone).
- It does not prevent a *new* bot process from being started again — if the
  underlying bug that caused the incident is still present in config/code,
  restarting the bot will hit it again. Kill switch stops the bleeding; it
  does not fix the cause.

---

## 2. What to check first when something looks wrong

In order:

1. **Dashboard** (`http://localhost:5000` by default, or wherever
   `dashboard.host`/`dashboard.port` in `config/settings.yaml` point) — open
   positions, daily PnL, feed health status (green/yellow/red), active
   strategies.
2. **`logs/` directory** — the most recent rotating log file. Look for
   `ERROR`, `CRITICAL`, `KILL SWITCH`, `RECONCILE`, or repeated stack traces.
   Do not delete or move anything in `logs/` while investigating.
3. **Gate/health check**: `python scripts/phase10_check_gate.py` — read-only,
   safe to run at any time, shows current trade count and gate criteria
   against the live DB.
4. **Reconciliation-related tests**, if you suspect a position-tracking bug
   specifically (not for normal incident response — this runs against
   mocks, not your live account):
   ```
   python -m pytest tests/test_reconcile.py -v
   ```
5. If you have testnet credentials configured and want to validate the
   exchange-facing code paths themselves are healthy (rare — only do this
   if instructed by someone who understands it's placing real testnet
   orders):
   ```
   python -m pytest -m testnet_live -v
   ```

---

## 3. Orphan position detected

**What it means:** the reconciliation loop (`src/core/reconciliation.py`,
runs every `reconciliation.interval_sec` = 60s per `config/settings.yaml`)
found a position on the exchange that the bot's local database does not know
about. This can happen after a crash, a manual trade placed outside the bot,
or a bug.

**Current configured behavior** (`config/settings.yaml` → `reconciliation:`
section, read directly — do not assume these values without checking
yourself, they can change):

```yaml
reconciliation:
  enabled: true
  interval_sec: 60
  stale_threshold_sec: 120
  orphan_exchange_policy: ADOPT_AND_PROTECT   # FLATTEN | HALT
  mismatch_policy: HALT
  block_entries_when_stale: true
```

- `orphan_exchange_policy: ADOPT_AND_PROTECT` (the default for both `paper`
  and `testnet`) means the bot will **adopt** the orphan position into its
  local portfolio and attempt to attach SL/TP protection to it (native
  triggers if any are already on the exchange, otherwise it computes and
  places its own). It does **not** close the position.
  - **Mainnet override**: `mode_overrides.mainnet.reconciliation.orphan_exchange_policy: HALT` —
    on mainnet the bot instead halts (blocks new entries) and raises an
    alert rather than silently adopting an unknown position. Confirm which
    mode you're running before assuming which behavior applies.
- `mismatch_policy: HALT` — if a position exists on *both* sides but with a
  different side or size than the bot expects, the bot halts entries and
  alerts. It does not automatically try to reconcile the discrepancy by
  trading.

**What you should do:**
1. Check the alert/log message — it names the symbol and whether it was
   treated as `orphan_exchange` (adopted) or `mismatch` (halted).
2. If adopted: verify the SL/TP the bot attached actually makes sense for
   that position (right side, sane distance from entry). If it looks wrong,
   use the kill switch or manually manage that one position via the
   exchange UI.
3. If halted (mismatch): new entries are blocked until you clear it. Do not
   just restart the bot to "fix" this — investigate why local and exchange
   state disagree first (partial fill missed by the bot? two processes
   running against the same account? a manual trade?).

---

## 4. WS / feed disconnect

**What the bot does automatically:**
- `_ws_health_loop()` (`src/core/engine.py:1058`, delegating to
  `src/core/background_tasks.py`) checks WebSocket health on a fixed
  interval and marks the client unhealthy if it detects a stale/dropped
  connection (the client itself auto-reconnects per `AGENTS.md`'s
  "auto-reconnecting WebSocket clients" characteristic).
- `_entry_feed_block_reason()` (`src/core/engine.py:3741`) gates **new
  entries only** — it will block new positions with a specific reason
  (`ws_unhealthy`, `feed_health_pending`, `feed_health_not_ready`,
  `feed_red:{symbol}`, `feed_red:overall`, or a reconciliation-staleness
  reason) if the feed looks bad. It does **not** touch existing open
  positions or force an exit — a feed problem alone will not flatten you.

**What a human should manually verify:**
- That the dashboard's feed-health indicator (green/yellow/red) actually
  recovers after a disconnect — don't assume reconnection succeeded just
  because entries resumed.
- Whether any open position needed protective action during the outage that
  the bot couldn't take (e.g., a native SL/TP trigger is exchange-side and
  still functions even if the bot's WS is down — but the bot's own logic for
  detecting a triggered exit and updating local state will be delayed until
  the feed recovers). Check `data/live/bot.db` open trades against the
  exchange directly if the outage was long.
- Whether the outage happened during a funding-reset blackout window or
  volatility-circuit-breaker block already in effect (those are normal soft
  gates, not the feed issue) — check logs to avoid conflating the two.

---

## 5. Crash / restart

**What recovery does automatically** (`_recover_state()`,
`src/core/engine.py:3525`):
- Reloads open trades from the DB into the executor
  (`load_open_trades()`, `load_pending_orders()` if supported).
- Restores the last portfolio snapshot (capital, peak capital, daily peak,
  daily PnL, cash, day-start equity, daily trade count) from
  `get_portfolio_history(limit=1)`.
- Re-syncs open trades to the in-memory portfolio and reconciles daily PnL
  against the DB.
- Increments an internal restore-invocation counter (expected to be 1 per
  process — used by tests to catch accidental double-recovery).

**What a human should double-check after any crash/restart:**
1. **Position count matches the exchange** — compare `data/live/bot.db` open
   trades against `get_user_state()` / the exchange UI directly. The
   reconciliation loop will catch a mismatch within
   `reconciliation.interval_sec` (60s) automatically, but don't wait
   passively on your first restart after an incident — check it yourself.
2. **No duplicate orders** — a crash mid-order-submission can in principle
   leave a resting order the bot no longer tracks. Check open orders on the
   exchange against what the bot's dashboard shows.
3. **Daily PnL / daily trade count look sane** — a bad restore (e.g., from a
   stale or corrupt snapshot) could under- or over-count daily loss, which
   feeds directly into the `max_daily_loss_pct` circuit breaker. If the
   dashboard's daily PnL looks obviously wrong right after a restart, treat
   it as a P0 until confirmed.

---

## 6. Emergency contacts / escalation

*Template — fill in before running on testnet or mainnet with real
operational risk. Left blank deliberately; no placeholder contacts are
invented here.*

| Role | Name | Contact | When to page |
|---|---|---|---|
| Primary operator | | | Any P0 (kill switch invoked, mismatch halt, unexplained loss) |
| Secondary / backup operator | | | Primary unreachable within __ minutes |
| Exchange account owner (Hyperliquid API key holder) | | | Vault/key compromise, need to rotate credentials |
| Escalation path (e.g., Telegram/Discord alert channel already configured in `alerts:` section of `config/settings.yaml`) | | | |

---

## 7. Do NOT do this

- **Never manually edit `data/live/bot.db` while the bot is running.** The
  bot holds this SQLite file open (WAL mode); a concurrent external write
  can corrupt state or silently be overwritten by the bot's next write.
  Stop the bot first if you must edit the DB directly, and know exactly
  which table/row you're touching.
- **Never change `config/settings.yaml` mid-incident without stopping the
  bot first.** The config is loaded once at startup (with `mode_overrides`
  applied); editing the file while the process is running has no effect
  until restart, so an operator can be misled into thinking a live change
  took effect when it didn't — and a restart at the wrong moment can compound
  an ongoing incident.
- **Never use a mainnet private key for testnet testing**, and never do the
  reverse. `docs/TESTNET_E2E_GUIDE.md` calls this out explicitly: the
  testnet e2e suite deliberately places real orders and flattens whatever
  it finds — using it against a mainnet-keyed account risks real financial
  exposure and unintended liquidation of unrelated positions.
- **Never bypass the kill switch's `confirm_flat()` result.** If it reports
  not-flat or reports errors, do not assume "close enough" — verify directly
  against the exchange before considering the incident resolved.
- **Never restart the bot repeatedly in a loop hoping the problem goes
  away** without checking logs first. If the incident was caused by a code
  or config bug, an unmanaged restart loop can re-trigger it repeatedly
  (e.g., duplicate entries, repeated mismatch halts) faster than a human can
  intervene. Use `run_with_recovery.py`'s `--max-restarts` only when you
  understand why the process is crashing, not as a substitute for
  diagnosis.
- **Never relax the GoldRush data-parity gate or Fase 10 gate thresholds to
  "get past" an incident.** Per project policy (see `AGENTS.md` and
  `docs/NODE_TRADES_REBUILD.md`), these gates exist specifically so nobody
  — including under incident pressure — loosens tolerance to make a bad
  signal look acceptable.
