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

# Pre-commit / pre-push gate (CI battery + security audit in one command)
python scripts/run_pre_push_gate.py
python scripts/run_pre_push_gate.py --fail-on-high   # audit fails on HIGH too
python scripts/run_pre_push_gate.py --skip-audit     # CI battery only

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
commit/push** — it runs the full CI battery (pytest `unit` +
`integration_offline`, same as `scripts/run_ci_tests.py`) and then the static
security audit (`security.audit`), stopping early with a non-zero exit code
if either stage fails. Wire it into your hook:

```bash
# .git/hooks/pre-push
python scripts/run_pre_push_gate.py || exit 1
```

Flags:

| flag | effect |
|------|--------|
| `--network` / `--testnet-live` | also run those opt-in pytest suites (real endpoints) |
| `--fail-on-high` | audit fails on HIGH findings too (default: CRITICAL only, matching `main.py --audit`) |
| `--skip-audit` | run only the CI battery |

Exit codes: `0` all stages passed · `1` CI or audit failed · `2` audit
unreachable. Note the audit default mirrors `main.py --audit`: it fails on
CRITICAL findings; the 2 pre-existing HIGH (`AUDIT-005` subprocess) and
1 MEDIUM (`AUDIT-004` file write) are baseline and do not block unless you
pass `--fail-on-high`.

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
# Default CI battery (unit + integration_offline)
python scripts/run_ci_tests.py

# Everything including network-dependent tests
python scripts/run_ci_tests.py --network --testnet-live

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
