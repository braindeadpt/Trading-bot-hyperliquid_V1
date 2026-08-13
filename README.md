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

## Testing

The suite runs on **pytest** (`pytest.ini` at repo root), split into four
markers so CI can choose what to run:

| Marker                  | Meaning                                                                 | Run in default CI? |
|--------------------------|--------------------------------------------------------------------------|---------------------|
| `unit`                   | Fast, no network, no cross-module wiring                                 | Yes |
| `integration_offline`    | OMS, reconciliation, engine boot/shutdown, walk-forward — mocks only, no network | Yes |
| `network`                | Real HTTP/WebSocket calls (GoldRush, Hyperliquid, Coinalyze)              | No (opt-in) |
| `testnet_live`           | Live Hyperliquid testnet connection / real order placement               | No (opt-in) |

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
