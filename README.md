# Hyperliquid Premium Trading Bot v3.1.48

Professional automated trading bot for Hyperliquid perpetuals.
Modular async architecture, real-time WebSocket data, pluggable strategies,
deterministic risk management, paper / testnet / mainnet execution modes,
and a Flask + Socket.IO dashboard.

## Project Status

- **Execution roster:** `VWAPDeviation` only
  (`strategy.phase08.execution_strategies`), **paper-only**
  (`strategy.phase08.paper_only: true`). Mainnet stays gated pending OOS
  (walk-forward / Phase06) validation.
- **Shadow roster** (signals tracked, never executed): VolatilityBreakout,
  CVDOrderFlow, OrderBookScalper, FundingArbitrage, FundingMomentum,
  SpotPerpCarry, LeadLag, LiquidationCatcher, ChecklistMeta.
- **GoldRush candle-data readiness is not yet validated.** Do not run OOS,
  parameter tuning, holdout, or performance backtests on GoldRush-sourced
  candles until parity is closed. Tooling:
  `scripts/goldrush_parity_diagnostic.py`,
  `scripts/goldrush_secondary_validation.py`.
- **Mainnet execution is blocked** until OOS validation and data readiness
  above are closed.
- **Baseline-signal gate** is required to promote any new name into
  `execution_strategies` — see `docs/BASELINE_SIGNAL_GATE.md`.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional but recommended) Backfill historical candles
python scripts/backfill_candles.py --days 7

# 3. Run in Paper Trading mode
python main.py --mode paper

# Or use the Windows launcher
quickstart.bat
```

Dashboard: <http://localhost:5000>

For a guided menu (paper / testnet / mainnet / audit / cascade test / update):
```bash
start.bat
```

---

## Features

| Feature | Status | Notes |
|---------|--------|-------|
| Paper trading (default)             | OK | No real money |
| Testnet execution                   | OK | Real matching, fake funds |
| Mainnet execution                   | Gated | Blocked until OOS + data readiness |
| Real-time WebSocket dashboard       | OK | Flask + Socket.IO |
| HL WS feeds (mids, OI, trades, L2)  | OK | Plus optional L2 book recorder |
| Multi-venue liquidations            | OK | Aggregator + feed-silence monitors |
| Cross-venue funding                 | OK | HL + Binance/Bybit/OKX (+ Coinalyze optional) |
| Phase08 execution / shadow split    | OK | VWAPDeviation paper-only; others shadow |
| Baseline-signal gate                | OK | Required for new execution promotions |
| Strategy governor                   | OK | Negative Sharpe over 30d => off |
| Drawdown circuit breaker (10%)      | OK | Hard gate; auto-reset 00:00 UTC |
| Intraday volatility circuit         | OK | Soft gate when ATR > 3× baseline |
| Funding-reset time blackout         | OK | ±5 min around 00:00/08:00/16:00 UTC |
| Kelly Criterion sizing              | OK | Per-strategy, bounded |
| Correlation monitor                 | OK | Rejects correlated adds |
| Look-ahead / future-data audit      | OK | Static scanner (Phase B) |
| Static security audit               | OK | 9 rules (eval / subprocess / secrets / …) |
| Encrypted credential vault          | OK | Fernet + PBKDF2 480k iterations |
| Crash-recovery wrapper              | OK | 3 restarts, 30s cooldown |
| Research / feature-screening tooling| OK | Scripts + docs under `scripts/` / `docs/` |

---

## Strategies

Authoritative Phase08 roster lives in `config/settings.yaml`
(`execution_strategies` / `shadow_strategies`). The table below is an
inventory of modules and their **current operating role**, not a claim that
every “available” module is trading live.

`StrategyGovernor` can still auto-disable strategies with negative Sharpe over
the last 30 days. Ensemble consensus remains available but is **disabled** in
the current paper config (direct Phase08 routing).

| Strategy           | Type            | Role now | Notes |
|--------------------|-----------------|----------|-------|
| VWAPDeviation      | mean-reversion  | Execution (paper) | Only name allowed to place paper orders |
| VolatilityBreakout | trend           | Shadow   | Signal-tracked; not executed |
| CVDOrderFlow       | order-flow      | Shadow   | Multi-TF CVD divergence |
| OrderBookScalper   | microstructure  | Shadow   | L2 imbalance scalper |
| FundingArbitrage   | market-neutral  | Shadow   | Previously killed as live arb; shadow only |
| FundingMomentum    | carry           | Shadow   | Funding-flip follower |
| SpotPerpCarry      | carry           | Shadow   | Delta-neutral carry |
| LeadLag            | microstructure  | Shadow   | Perp-vs-perp lag / basis mode |
| LiquidationCatcher | event-driven    | Shadow   | Liquidation + OI confirm |
| ChecklistMeta      | meta checklist  | Shadow   | Demoted after baseline-signal FAIL |
| TrendPyramid       | trend           | Available | Not in Phase08 execution/shadow lists |
| SmartMoneyFlow     | trend           | Available | Research / legacy |
| DonchianBreakout   | trend           | Available | Research / legacy |
| RangeGrid          | revert          | Available | Maker grid in low-ADX ranges |
| FundingExtreme     | mean-reversion  | Disabled | Governor Sharpe failure — kept off |

---

## Project Structure

```
trading-bot-hyperliquid/
  main.py                      # Entry point + arg parsing
  run_with_recovery.py         # Crash-recovery wrapper
  audit_all.py                 # Component health check (imports every module)
  requirements.txt             # Fully pinned deps
  config/
    settings.yaml              # Main configuration
    .env.example               # Template for API secrets
  src/
    core/                      # engine, risk, execution, portfolio, vol circuit, funding blackout
    strategies/                # 8 strategies + base ABC + indicators + ensemble + factory + governor
    exchanges/                 # Hyperliquid WS/REST, Binance API, funding aggregator, HL predicted
    data/                      # SQLite, candle builder, orderbook metrics, backfill, decision audit
    dashboard/                 # Flask + Socket.IO server + embedded UI
    security/                  # Fernet vault + static security audit
    alerts/                    # Telegram / Discord notifier
    backtest/                  # Backtest engine + performance metrics
    utils/                     # config loader, logger, helpers, crash recovery
  tests/                       # pytest suite (unit / integration_offline / network / testnet_live markers)
  scripts/                     # backfill, lookahead audit, CI runner, manual/ (non-pytest network scripts)
  data/                        # Runtime SQLite DB (auto-created, gitignored)
  logs/                        # Rotating logs (auto-created, gitignored)
  docs/
    SECURITY.md                # Threat model + deployment checklist
  quickstart.bat / start.bat / stop.bat / service.bat   # Windows launchers
```

---

## Risk Gates (per entry, in order)

1. Per-symbol lock (serializes same-symbol processing)
2. Cooldown (per-strategy, doubling on consecutive losses)
3. Kelly sizing (confidence-weighted, half-Kelly, capped 2x)
4. Correlation monitor (rejects correlated adds)
5. Volatility circuit breaker (soft; blocks entries when ATR>3x baseline)
6. Funding-reset blackout (soft; blocks +/-5min around funding reset)
7. `RiskManager.can_enter` (hard; daily trades / loss / DD / exposure / leverage / max position size)
8. TCA check (slippage + fill ratio from L2 book)
9. Order routing (post-only vs market vs limit, maker-first when viable)

Soft gates (1-6) only block new entries. Hard gates (7) own flatten behavior.
The same `RiskManager` is shared between backtest and live (v3.1.19).

---

## Commands

```bash
# Modes
python main.py --mode paper          # default
python main.py --mode testnet
python main.py --mode mainnet        # requires API keys + explicit env var
python main.py --backtest --from-date 2024-01-01 --to-date 2024-03-01

# Audits
python main.py --audit               # security audit
python scripts/lookahead_audit.py --ci   # future-data leakage scanner
python tests/test_cascade_simulation.py  # vol circuit stress test

# Pre-commit / pre-push gate (CI + security audit + config_hash in one command)
python scripts/run_pre_push_gate.py
python scripts/run_pre_push_gate.py --fail-on-high   # audit fails on HIGH too
python scripts/run_pre_push_gate.py --skip-audit     # CI battery only
python scripts/run_pre_push_gate.py --skip-hash      # skip the config_hash check
python scripts/run_pre_push_gate.py --preflight      # also validate deployment feeds (stage 0)

# CI battery — also runs the same security audit + config_hash (the full trio)
python scripts/run_ci_tests.py

# Git hooks: fast pre-commit (staged files only) + full pre-push gate
python scripts/install_git_hooks.py

# Pre-start feed delivery check (fails early instead of waiting for silence)
python scripts/preflight_feed_check.py

# Runs automatically at boot (main.py step 4b) before the engine starts and
# blocks if a contracted feed is not delivering. First deployment / fresh DB?
python main.py --skip-preflight

# Maintenance
python scripts/backfill_candles.py --symbols BTC,ETH,SOL --days 7
python tests/test_basic.py
python tests/test_critical_fixes.py
python audit_all.py
```

---

## Configuration

YAML in `config/settings.yaml`. Hierarchy (later wins):
1. Hard-coded `DEFAULT_CONFIG` in `src/utils/config.py`
2. User YAML
3. Environment variables prefixed with `BOT_` (e.g. `BOT_RISK_MAX_POSITIONS=7`)
4. Per-mode overrides in `mode_overrides.<mode>` (Phase C)

Mainnet defaults (auto-applied via `mode_overrides.mainnet`):
- `leverage_max`: 5x (was 10x)
- `max_daily_loss_pct`: 2% (was 3%)
- `max_daily_trades`: 20 (was unlimited)
- `max_position_size_pct`: 3% (was 5%)

Secrets live in `.env` (gitignored) or in the encrypted vault at
`data/vault.enc`. Required env vars: `HYPERLIQUID_API_KEY`,
`HYPERLIQUID_API_SECRET`, optional `COINALYZE_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`.

### Frozen config window (Fase 08 + Fase 10)

`tests/test_config_hash_frozen.py` is the **guard of the frozen window**: it
pins the effective `config/settings.yaml` hash to the frozen Fase 10 hash
(`9456c6eb877b2391`) and re-runs the `assert_config_matches_preregister`
checks — the same assert `main.py` runs at startup. **Changing any
hash-affecting parameter in `settings.yaml` mid-window turns the CI red and
would make the bot refuse to start.**

Mid-window changes therefore require an **explicit re-freeze**: persist a new
Fase 10 pre-registration manifest with a `reregistration_reason` (see
`src/research/phase10_preregister.py` — `persist_preregister_manifest(
overwrite=True, reregistration_reason=...)`), which archives the superseded
manifest and re-freezes a new hash, then update `test_config_hash_frozen.py`
and `docs/SECURITY.md`/`docs/GATES_REFERENCE.md` accordingly. Operational
knobs that must NOT trip the window (e.g. `FEED_SILENCE_WARN_FRACTION`) are
deliberately env-only and excluded from the hash.

---

## Feed Contracts (operation)

The feed-silence watchdog (`FeedSilenceMonitor` +
`feed_silence_contracts()` in `src/core/engine.py`) raises a `degraded`
flag when a feed stops delivering for longer than its threshold. The
operating rule is strict: **only feeds this deployment actually contracts
can light up `degraded`** — a feed that is disabled, blocked or absent here
must never force a false alarm. This is the direct lesson of the
2026-06-29 Binance fstream outage, which ran silent for six weeks and
contaminated research because nobody was told the pipe was empty.

| Feed | Contracted when | Default threshold |
|------|------------------|-------------------|
| `liquidation_okx` / `liquidation_bybit` | always | 6h |
| `funding_cex` / `funding_hl` / `taker_split` | always | 1h |
| `liquidation_coinalyze_check` | always (verify-only) | 12h |
| `l2_book_recording` | `market_data.l2_recording.enabled` | 2m |
| `binance_perp` | `strategy.lead_lag.enabled` / `auto_enable` | 1h |
| `liquidation_binance` | operator opt-in (below) | 6h |

### Pre-start delivery check

Run **before** starting the bot (e.g. in the start script or after a
restart) to confirm every contracted feed has recent delivery evidence —
failing early instead of waiting for the silence threshold to trip:

```bash
python scripts/preflight_feed_check.py            # exit 0 = all fresh
python scripts/preflight_feed_check.py --json     # machine-readable report
```

It reads the persisted artifacts (liquidation/funding/candles tables in
`data/live/bot.db`, newest file mtime under `data/research/l2_books/`),
uses the **same** `feed_silence_contracts()` decision as the engine, and
reports per feed: age, threshold, % of threshold, status. Exit codes:
`0` all fresh · `2` a feed past 50% of its threshold (delivery slowing) ·
`1` a feed stale or missing (blocked path — check before starting).
Coinalyze (verify-only, never persisted) is reported but not gated unless
`--gate-coinalyze`. `liquidation_binance`/`binance_perp` are skipped when
not contracted, exactly like the watchdog.

It also validates **per-symbol candle freshness** (1m/15m for every trading
symbol): a 1m candle older than `--candle-1m-max-age-sec` (default 5 min,
15m: 30 min) means the collector fell behind — a data backlog. Two modes:

* **freshness** (default, used at boot): candles must be within the max age
  of now;
* **coverage** (`--min-latest-ms`, used before backtests): the latest candle
  must REACH the requested end-of-window timestamp — catches a backlog at
  the end of the window without blocking historical windows.

The check runs **automatically at boot** (`main.py` step 4b) before the
engine starts and blocks on exit 1; the **backtest path** runs the candle
coverage check against the target DB before reading the data. Use
`--skip-preflight` (first deployment / fresh DB) or `--candles-only`
(backtest-only) as needed.

It is also available as an **optional stage of the pre-push gate**:
`python scripts/run_pre_push_gate.py --preflight` validates the deployment
feeds before the CI battery — a stale contracted feed blocks the push, a
feed past the warn fraction (exit 2) warns and continues.

### Enabling `liquidation_binance` where fstream is accessible

On this network Binance **fstream `@forceOrder` delivers 0 messages**, so
`liquidation_binance` is **not** contracted by default — contracting it
would make `degraded` permanently true. In a deployment where the channel
is reachable, opt the watchdog back in **before** starting the bot:

```bash
# .env (gitignored) — re-contract liquidation_binance for THIS deployment
LIQUIDATION_BINANCE_CONTRACTED=true
```

Why `.env` and not `config/settings.yaml`:

- `.env` is **gitignored** — the contract decision stays deployment-local
  and never leaks into the repository.
- The variable is deliberately **not** `BOT_`-prefixed, so the Fase 10
  `config_hash` (frozen window) stays intact — the hash pins
  `settings.yaml` only, and this opt-in is an operator-side switch, not a
  strategy change.
- Accepted truthy values: `1`, `true`, `yes` (case-insensitive).

`binance_perp` needs no opt-in: it is contracted automatically whenever
the LeadLag perp-price bridge runs (`strategy.lead_lag.enabled` /
`auto_enable`; the testnet mode override turns it on).

### Tuning the early-warning threshold

The watchdog fires a `FEED QUIET (early)` warning when a feed's silence
crosses **50%** of its threshold (fire-once per episode, reset on beat),
then `FEED QUIET (imminent)` at **90%** before degrading at 100%. The
early-warning level is operator-configurable via env — e.g. to alert
earlier (30%) on a critical deployment:

```bash
# .env (gitignored) — early-warning at 30% of the silence threshold
FEED_SILENCE_WARN_FRACTION=0.3
```

- Accepted: any float in `(0, 1)`; values below `0.05` clamp to `0.05` and
  above `0.95` clamp to `0.95` (so it can never sit at/above the 90%
  imminent level). Unparseable values fall back to `0.5` with a warning.
- Like `LIQUIDATION_BINANCE_CONTRACTED`, the variable is deliberately
  **not** `BOT_`-prefixed — it is an operator-side switch read at startup,
  never enters `settings.yaml`, and therefore never moves the Fase 10
  `config_hash`. It is a monitoring sensitivity knob, not a strategy
  parameter.

  The imminent level follows the same pattern, defaulting to **90%** and
  clamped to `(0.5, 1.0)` so it always stays above the early level:

```bash
# .env (gitignored) — imminent at 80% of the silence threshold
FEED_SILENCE_IMMINENT_FRACTION=0.8
```

- Accepted: any float in `(0, 1]`; values below `0.5` clamp to `0.5`.
  Unparseable or out-of-range values fall back to `0.9` with a warning.
  Both fractions appear per-feed in the snapshot as `warn_fraction` /
  `imminent_fraction` and render in the panel's Alerted column
  (`early @ N%` / `imminent @ N%`). `FEED_SILENCE_WARN_FRACTION` also
  sets the preflight warn level (`scripts/preflight_feed_check.py` —
  `past N%` in its exit-2 report) unless `--warn-fraction` is passed.

### The cadence signal and the exp-gap indicator

Beyond age vs threshold, the monitor tracks each feed's **cadence** — the
rolling distribution of its inter-event gaps. `FEED CADENCE`, the
earliest signal in the escalation ladder (before `early`), fires when a
feed's current gap exceeds its **own historical p99**:

- **p99** — nearest-rank percentile of the last
  `FEED_CADENCE_GAP_HISTORY` gaps, computed only after
  `FEED_CADENCE_MIN_SAMPLES` gaps (the `learning…` state). Scale is
  per-feed: a feed that normally prints every 5s and one that prints
  every 2m each get a p99 of *their own* delivery — a thinning feed is
  flagged long before any absolute silence threshold.
- **Fire** — once per episode (reset on beat), like the early/imminent
  levels. **Log-only by design**: a p99 threshold means ~1% of gaps
  naturally exceed it, so paging on every breach would be constant
  noise — the operator sees it in the dashboard instead, and the
  imminent/degraded levels own the Telegram paging.
- **Lead** — a gap that keeps growing past p99 and reaches the silence
  threshold (6h for liquidation feeds) is the real outage; the cadence
  fire preceded it by `max_silence − p99`. That lead time is measured
  against the real `liquidation_events` history by
  `scripts/validate_feed_cadence_leadtime.py` (report:
  `docs/FEED_CADENCE_LEADTIME_VALIDATION.md`).

The Feed Silence panel's **Age/exp gap** column renders the same signal:

- The small line under the age shows `exp ≤ p99` (the feed's expected
  quiet ceiling), or `gap 45.2m > p99 12.3m` in red when the p99 is
  breached.
- A red **`cadence`** pill next to the feed name flags the episode
  (tooltip: the exact comparison, the sample count behind the p99, and
  whether the fire-once alert already emitted).
- The cell tooltip carries the full shape of the distribution —
  `p50 / p95 / p99` and where the current gap sits (`gap actual no
  percentil 63 do histórico`, read from the snapshot as
  `cadence_pct_current`).
- The cell color is a **continuous 0-100 ramp** of that percentile
  (green → amber → red), not a hard p95/p99 cutoff — gated on the
  learned p99 so a cold-start feed never shows a false alarm color.

### Tuning the cadence detector sensitivity

The cadence watchdog (`FEED CADENCE`, log-only) fires when a feed's
current gap exceeds its **historical p99** inter-event gap. Two knobs
adjust how quickly that p99 is trusted, per deployment:

```bash
# .env (gitignored) — trust the p99 after 50 gaps (default 100);
# keep 8000 gaps of history (default 4000)
FEED_CADENCE_MIN_SAMPLES=50
FEED_CADENCE_GAP_HISTORY=8000
```

- `FEED_CADENCE_MIN_SAMPLES`: minimum recorded gaps before the p99 is
  computed — the `learning…` state in the panel's Age column. Lower =
  more sensitive, noisier on cold start; higher = more robust but blind
  longer after a restart. Positive integer; non-integers, zero and
  negatives fall back to `100` with a warning.
- `FEED_CADENCE_GAP_HISTORY`: rolling inter-event gap history kept per
  feed (the deque the percentile is computed over). Positive integer;
  rejected values fall back to `4000`. The monitor clamps it up to
  `FEED_CADENCE_MIN_SAMPLES` so the percentile can always be computed.
- Same hash-neutral contract as the fractions: not `BOT_`-prefixed,
  read at startup, never in `settings.yaml`, never in the Fase 10
  `config_hash`. The effective `cadence_min_samples` appears per-feed
  in the snapshot (`cadence_min_samples`) and in the Age-column
  tooltip, so the panel reflects the deployment's actual learning gate.

**Verify after start:** `GET /api/market_data_health` returns
`feed_silence` (per-feed age + `degraded`) and `feed_silence_degraded`.
An uncontracted feed must never appear in the snapshot, and
`feed_silence_degraded` must reflect only real contracts. Full detail:
`docs/FEED_CONTAMINATION_AUDIT.md` §0.1 and `docs/SECURITY.md` §3.6.

---

## Security

- Paper mode is the default. Mainnet requires both the config flag and
  `HYPERLIQUID_MAINNET_ENABLED=true`.
- **Dashboard auth is OFF by default** (localhost-only bind). Enable it in
  `.env` (gitignored, hash-neutral — does not touch the Fase 10
  `config_hash`):
  ```bash
  DASHBOARD_AUTH_ENABLED=true
  BOT_DASHBOARD_TOKEN=<a-long-random-token>
  ```
  This protects every REST endpoint and the Socket.IO stream (login gate in
  the UI; `X-Dashboard-Token` header / `?token=` for programmatic access).
  Do **not** flip `dashboard.auth_enabled` in `config/settings.yaml`
  mid-window — that key IS part of the frozen hash and would trip the
  Fase 10 drift assert (the token/password keys are excluded).
- **Per-IP rate limiting is ON by default** for REST endpoints (100
  requests/min per client IP, sliding window — bounds brute-force attempts
  against the dashboard token). Tune with `DASHBOARD_RATE_LIMIT_PER_MIN` in
  `.env` (hash-neutral). Socket.IO transport and static assets are exempt.
- `.env` and `data/vault.enc` are gitignored.
- `python main.py --audit` runs the static security scanner (9 rules:
  eval/exec, hardcoded secrets, HTTP to unknown hosts, file writes outside
  project, os.system/subprocess, pickle.loads, dynamic __import__,
  suspicious comments, HTTP inventory).
- `scripts/lookahead_audit.py --ci` runs the future-data leakage scanner
  (6 rules) and fails CI on any non-LOW finding.
- See `docs/SECURITY.md` for the full threat model and deployment checklist.

---

## Pre-commit / pre-push gate

`scripts/run_pre_push_gate.py` is the **single command to run before
commit/push** — it runs its validations in order, stopping early with a
non-zero exit code if any fails:

0. **Preflight (optional, `--preflight`)** — `scripts/preflight_feed_check.py`
   against the **deployment state** (contracted feeds + per-symbol candle
   freshness). A deployment concern, not code: a stale contracted feed blocks
   the gate here, before the CI battery spends minutes. Exit 1 blocks; exit 2
   (past the warn fraction but still delivering) warns and continues — the
   same semantics as the boot-time wiring in `main.py` step 4b.
1. **CI battery** — pytest `unit` + `integration_offline` (via
   `scripts/run_ci_tests.py`). Since `run_ci_tests.py` itself runs the full
   trio (CI + audit + hash), the gate passes `--skip-audit --skip-hash` so
   each check runs **exactly once** — the trio lives in `run_ci_tests.py`;
   the gate adds the opt-in suites and the `--fail-on-high` knob.
2. **Security audit** — `security.audit` (static scanner).
3. **config_hash** — the effective `config/settings.yaml` hash must equal the
   Fase 10 frozen manifest hash — the same assert `main.py` runs at startup
   (`assert_phase10_preregister`). A drift here means the bot would **refuse
   to start**, so the gate catches it before commit/push.

Wire it into your hook:

```bash
# .git/hooks/pre-push
python scripts/run_pre_push_gate.py || exit 1
```

Flags:

| flag | effect |
|------|--------|
| `--preflight` | also run the deployment feed/candle check (stage 0, before CI); exit 1 blocks, exit 2 warns-and-continues |
| `--network` / `--testnet-live` | also run those opt-in pytest suites (real endpoints) |
| `--fail-on-high` | audit fails on HIGH findings too (default: CRITICAL only, matching `main.py --audit`) |
| `--skip-audit` | run only the CI battery (audit + hash run separately) |
| `--skip-hash` | skip the config_hash-vs-frozen check (runs separately) |

**Baseline tracking**: the audit stage always passes `--enforce-baseline` — a
**new HIGH finding** whose `(rule, file)` is not in the documented accepted
baseline (`ACCEPTED_HIGH_BASELINE`, see `docs/SECURITY.md` §2.4) fails the
gate even without `--fail-on-high`. The single accepted HIGH today is
`AUDIT-005 @ utils/crash_recovery.py` (subprocess, hardened with
`_validate_cmd`). Adding a new HIGH requires remediating it or extending the
baseline with justification; an `# audit-ok` marker without a baseline entry
is also blocked (acceptance must be documented in both places).

Exit codes: `0` all stages passed · `1` any stage failed · `2` a stage was
unreachable (e.g. missing Fase 10 manifest). The audit is **closed at 0 HIGH
+ 0 MEDIUM** (2026-08-13, see `docs/SECURITY.md` §2.4): AUDIT-004 was
remediated (`safe_write_file` in `top_trader_tracker`), and the single
accepted AUDIT-005 (`crash_recovery` subprocess — hardened with
`_validate_cmd`) carries an inline `# audit-ok: AUDIT-005` marker and is
reported in the audit's `[ACCEPTED]` section instead of the blocking counts.
`tests/test_security_audit_suppression.py` fails CI if a new HIGH/MEDIUM
appears without a decision.

---

## Git hooks (fast pre-commit + full pre-push)

Install both hooks once (idempotent — re-running only refreshes its own
hooks; pre-existing foreign hooks are left alone unless `--force`, which
backs them up to `.bak`):

```bash
python scripts/install_git_hooks.py              # install both hooks
python scripts/install_git_hooks.py --force      # replace foreign hooks (backup .bak)
python scripts/install_git_hooks.py --list       # show current state
python scripts/install_git_hooks.py --uninstall  # remove managed hooks only
```

- **pre-commit — fast path** (`scripts/run_git_hooks.py --hook pre-commit`):
  runs in seconds, only over the **staged** files — a syntax check of every
  staged `.py`, a **scoped security audit** of the staged files under `src/`
  (CRITICAL blocks; `--fail-on-high` also blocks HIGH), and the
  config_hash-vs-frozen check (always — catches drift even from a
  `DEFAULT_CONFIG` change). No full pytest, no full-tree audit: those stay
  in the pre-push hook.
- **pre-push — full gate**: delegates to `scripts/run_pre_push_gate.py`
  (pytest battery + full security audit + config_hash); its exit code passes
  through.

The scoped audit is the same `security.audit` rules restricted to the diff
(`SecurityAuditor.run(targets=...)`), so new code with an `eval`/hardcoded
secret/subprocess is caught at commit time instead of at push.

## Testing

The suite runs on **pytest** (`pytest.ini` at repo root), split into four
markers so CI can choose what to run:

| Marker                  | Meaning                                                                 | Run in default CI? |
|--------------------------|--------------------------------------------------------------------------|---------------------|
| `unit`                   | Fast, no network, no cross-module wiring                                 | Yes |
| `integration_offline`    | OMS, reconciliation, engine boot/shutdown, walk-forward — mocks only, no network | Yes |
| `network`                | Real HTTP/WebSocket calls (GoldRush, Hyperliquid, Coinalyze)              | No (opt-in) |
| `testnet_live`           | Live Hyperliquid testnet connection / real order placement               | No (opt-in) |

### Parity contract: minimal test config vs production config

The parity tests deliberately run against **two** configs, because each one
catches a different class of regression.

**Minimal config** — built inline by `_cfg()` in
`tests/test_backtest_live_parity.py`, with loose thresholds and every
optional gate disabled. Its job is to exercise the gate *machinery* in
isolation: deterministic, threshold-independent behaviour that fails fast
and loudly if a gate mis-reads a config key or the ordering changes.

**Production config** — the real `config/settings.yaml` (loaded by
`TestParityAgainstProductionConfig` in `test_backtest_live_parity.py`, and
`test_production_gate_parity.py` for feed-health / TCA strict-proxy /
reconciliation). Its job is to verify the same chain still holds under the
*calibration* the bot actually runs with.

| Key | Minimal (unit) | Production (`config/settings.yaml`) |
|-----|----------------|-------------------------------------|
| `risk.max_positions` | 5 | 3 |
| `risk.max_position_size_pct` | 5.0 | 2.0 |
| `risk.taker_fee_pct` | 0.04 (4 bp) | 0.045 (4.5 bp) |
| `risk.symbol_risk_multiplier.SOL` | 1.0 | 0.5 |
| `risk.chase_filter.exempt_strategies` | `[]` | VolatilityBreakout, DonchianBreakout |
| `strategy.portfolio_governance.max_directional_exposure_pct` | 60 | 50 |
| Volatility circuit | off | on (3×, 30 min block, 24 bars warm-up) |
| Funding blackout | off | on (±5 min around 00/08/16 UTC) |
| TCA | off | strict (live) / proxy (backtest) |
| Reconciliation / feed-health gates | not exercised | exercised (live-only + replay substitute) |

**Why the contract must run against both:**

- A test that only runs the **loose minimal** config proves the machinery
  but says nothing about production. A regression that only bites at real
  thresholds — the 3rd-position reject vs the 5th, the 2% size cap vs 5%,
  SOL 0.5× scaling, the vol-circuit 24-bar warm-up, the funding-blackout
  resets, or `tca_mode: strict` needing an L2 book — would pass unnoticed.
- A test that only runs the **production** config is fragile and opaque:
  if it fails, you cannot tell whether the *logic* broke or a *threshold*
  drifted. The minimal config isolates the two, so a production failure is
  immediately attributable to calibration, not code.

The two layers together pin the full contract: **minimal proves the
machinery, production proves the calibration.**

```bash
# Default CI battery (unit + integration_offline) — after the tests pass this
# also runs the security audit and the config_hash-vs-frozen check, i.e. the
# same three stages as scripts/run_pre_push_gate.py in a single command
python scripts/run_ci_tests.py

# Everything including network-dependent tests
python scripts/run_ci_tests.py --network --testnet-live
python scripts/run_ci_tests.py --skip-audit     # CI battery only (audit + hash separately)
python scripts/run_ci_tests.py --skip-hash      # skip the config_hash check

# Ad-hoc: run a single suite directly with pytest
python -m pytest -m unit
python -m pytest -m integration_offline
python -m pytest -m network              # requires network access
python -m pytest tests/test_execution_oms.py -v
```

`tests/test_monte_carlo.py` is excluded from collection (see
`tests/conftest.py`) — it imports a `MCResult`/`PercentileCI`/`run_monte_carlo`
API that no longer exists in `src/backtest/monte_carlo.py` (current API:
`MCMetrics`, `bootstrap_metrics`, `block_bootstrap_metrics`). It needs a
rewrite against the current module before it can be re-enabled.

Manual (non-pytest) network smoke scripts that connect to a local Socket.IO
server live in `scripts/manual/` — they are not part of any CI suite.

```bash
python audit_all.py                               # Component health check
python -m security.audit --src-dir src             # Static security audit
python scripts/lookahead_audit.py --ci             # Future-data leakage scanner
```

---

## Requirements

- Python 3.11+ (tested on 3.14)
- Windows or Linux/macOS
- All dependencies in `requirements.txt` (fully pinned)

---

## License

MIT
