# AGENTS.md — Hyperliquid Premium Trading Bot v3.1.47

> This file is intended for AI coding agents. It assumes zero prior knowledge of the project.

---

## 1. Project Overview

This is the **Hyperliquid Premium Trading Bot** — an async Python trading bot for the Hyperliquid perpetuals exchange. It supports **paper trading** (default), **testnet**, and **mainnet** execution modes.

The bot is built around a **WebSocket-first event architecture**: real-time market data from Hyperliquid (and optional Binance feeds) flows through an async pub/sub `DataBus`, gets aggregated into multi-timeframe candles, and is consumed by twelve strategy modules (including the OrderBook Imbalance Scalper for tick-level orderbook micro-patterns, the CVD OrderFlow strategy for volume-tape divergence, the SpotPerpCarry delta-neutral funding arb, the RangeGrid maker grid, the TrendPyramid EMA pullback trend follower, and the FundingMomentum funding-flip strategy). A central `TradingEngine` orchestrates signal generation, risk gating, position sizing, and execution. All state is persisted to a local SQLite database, and a Flask + Socket.IO dashboard provides real-time monitoring.

**Key characteristics:**
- Fully async (`asyncio`) with auto-reconnecting WebSocket clients.
- Modular strategy system with an abstract base class.
- Deterministic risk management shared between backtest and live runs.
- Encrypted credential vault + static security auditor.
- No `eval`, `exec`, `pickle.loads`, or dynamic imports anywhere in core code.

**Current operational status (check before suggesting live/OOS work):**
- Live-executing strategies (`strategy.phase08.execution_strategies` in
  `config/settings.yaml`): VWAPDeviation only — currently
  **paper-only** (`strategy.phase08.paper_only: true`) pending OOS
  (out-of-sample / walk-forward) validation.
- Shadow-mode strategies (`strategy.phase08.shadow_strategies`, signal-tracked
  but never executed): ChecklistMeta, VolatilityBreakout, CVDOrderFlow, OrderBookScalper,
  FundingArbitrage, FundingMomentum, SpotPerpCarry (plus others listed in YAML).
- **GoldRush candle-data readiness is not yet validated.** Do not run OOS,
  parameter tuning, holdout, or performance backtests against GoldRush-sourced
  data until this is resolved.
- **Mainnet execution is blocked** pending the OOS validation and data
  readiness items above.
- **Baseline-signal gate (required for new execution promotions):** no strategy
  may be *added* to `strategy.phase08.execution_strategies` without a
  `baseline_signal_gate: PASS` on the preregister manifest. See §12.

---

## 2. Technology Stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.11+ (tested on 3.14) |
| Async IO | `asyncio`, `websockets==14.2`, `aiohttp==3.11.16` |
| Web framework | `flask==3.1.0`, `flask-socketio==5.5.1`, `python-socketio==5.13.0` |
| Data / Calculation | `pandas==2.2.3`, `numpy==2.2.5` |
| Persistence | SQLite (via `sqlite3`, WAL mode enabled) |
| Configuration | YAML (`pyyaml==6.0.2`) + environment variable overrides |
| Security | `cryptography==44.0.2` (Fernet vault) |
| OS | Primary target is Windows (`.bat` launchers), but runs on Linux/macOS |

**No formal build system** — this is a pure-Python project managed with `requirements.txt`. There is no `pyproject.toml`, `setup.py`, `Makefile`, or `tox.ini`.

---

## 3. Project Structure

```
trading-bot-hyperliquid/
├── main.py                    # Entry point — arg parsing, bootstraps all modules
├── run_with_recovery.py       # Crash-recovery wrapper (restarts main.py on crash)
├── requirements.txt           # Fully pinned dependencies
├── config/
│   ├── settings.yaml          # Main YAML configuration
│   └── .env.example           # Template for API secrets
├── src/                       # All application code
│   ├── core/                  # Engine, portfolio, risk, execution, Kelly sizer, correlation monitor
│   ├── strategies/            # 10 sub-strategies + base ABC + indicators + ensemble
│   ├── exchanges/             # Hyperliquid WS/REST, Binance API, funding aggregator
│   ├── data/                  # SQLite DB, candle builder, orderbook metrics, historical fetcher
│   ├── dashboard/             # Flask + Socket.IO server + embedded HTML UI
│   ├── security/              # Encrypted vault + static security audit engine
│   ├── alerts/                # Telegram / Discord notifier
│   ├── backtest/              # Backtest engine + performance metrics
│   └── utils/                 # Config loader, logger, helpers, crash recovery, log monitor
├── tests/                     # pytest suite (unit / integration_offline / network / testnet_live markers)
├── data/
│   ├── historical/            # Backfill / historical CSV or DB data
│   └── live/                  # Runtime SQLite DB (auto-created)
├── logs/                      # Rotating logs (auto-created)
├── docs/
│   └── SECURITY.md            # Threat model, deployment checklist, incident response
├── start.bat                  # Interactive Windows launcher menu
├── quickstart.bat             # One-click paper trading launcher
├── stop.bat                   # Kill running python.exe processes
├── service.bat                # Background service wrapper with recovery
├── scripts/
│   └── backfill_candles.py    # Binance historical candle backfill (run before bot start)
└── audit_all.py               # Component health-check script (imports every module)
```

---

## 4. How to Run

### Install dependencies
```bash
pip install -r requirements.txt
```

### Run modes
```bash
# Paper trading (default, no real money)
python main.py --mode paper

# Testnet (real order matching, fake money)
python main.py --mode testnet

# Mainnet (REAL MONEY — requires API keys and explicit confirmation)
python main.py --mode mainnet

# Backtest
python main.py --backtest --from-date 2024-01-01 --to-date 2024-03-01

# Security audit (standalone, then exits)
python main.py --audit

# Disable dashboard
python main.py --mode paper --no-dashboard
```

### Backfill historical candles (recommended before first run)
```bash
python scripts/backfill_candles.py
# or with custom parameters
python scripts/backfill_candles.py --symbols BTC,ETH,SOL --days 7
```

### Override config path
```bash
python main.py --config config/settings.yaml --mode paper
```

### With crash recovery (recommended for 24/7)
```bash
python run_with_recovery.py --mode paper --max-restarts 3 --cooldown 30
```

### Windows shortcuts
- `quickstart.bat` — paper mode + auto-opens browser at `http://localhost:5000`
- `start.bat` — interactive menu (Paper / Testnet / Mainnet / Backtest / Audit / Git pull / Recovery)
- `stop.bat` — kills `python.exe` processes running `main.py`
- `service.bat` — background wrapper with recovery

---

## 5. Configuration System

**Primary config file:** `config/settings.yaml`

**Hierarchy (later wins):**
1. Hard-coded `DEFAULT_CONFIG` in `src/utils/config.py`
2. User YAML (`config/settings.yaml`)
3. Environment variables prefixed with `BOT_` (e.g., `BOT_RISK_MAX_POSITIONS=7`)

**Key top-level sections in `settings.yaml`:**
- `mode`: `paper` | `testnet` | `mainnet`
- `assets`: e.g. `["BTC", "ETH", "SOL"]`
- `timeframes`: `["1m", "5m", "15m", "1h"]`
- `exchange`: Hyperliquid and Binance WS/REST URLs
- `risk`: capital limits, position sizing, leverage, slippage, fees, drawdown circuit breaker
- `strategy`: parameters for all 12 strategies + Kelly Criterion + cooldown governance
- `backtest`: initial capital, commission, slippage
- `database`: SQLite path and prune retention
- `dashboard`: Flask host, port, push interval
- `logging`: level, JSON/plain format, rotation (10 MB, 5 backups)

**Secrets** are **NOT** stored in the YAML file. Use either:
- The encrypted vault (`data/vault.enc`) via `src/security/vault.py`
- Environment variables (see `.env.example`): `HYPERLIQUID_API_KEY`, `HYPERLIQUID_API_SECRET`, `COINALYZE_API_KEY`, `TELEGRAM_BOT_TOKEN`, etc.

---

## 6. Code Style Guidelines

### Language
- **Code, comments, and docstrings:** English
- **User-facing docs:** Mixed (README and roadmap are Portuguese; architecture and security docs are English)

### Type Hints
- Use `from __future__ import annotations` at the top of modules.
- Return-type annotations are expected on public functions (~69% coverage in the current codebase).
- Use `Optional[X]`, `Dict[str, Any]`, `List[X]` for compatibility; modern syntax (`str | None`) is acceptable where supported.

### Code Organization
- One class per file is the dominant pattern (e.g., `TradingEngine` lives in `src/core/engine.py`).
- Constants are UPPER_CASE with type annotations at module level.
- Dataclasses are preferred for data models; many are `@dataclass(frozen=True)`.
- Abstract base classes define interfaces (e.g., `Strategy(ABC)` with `@abstractmethod`).

### Naming Conventions
- Classes: `PascalCase`
- Functions / methods / variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private methods / internals: leading underscore `_private_method`
- Hyperliquid-specific types: `Hl` prefix (e.g., `HlPriceTick`, `HlL2Book`)

### Error Handling
- No bare `except:` clauses. Catch specific exceptions.
- Use safe helpers from `src/utils/helpers.py`: `safe_float`, `safe_divide`, `safe_json_loads`.
- Log errors via the standard `logging` module, not `print()`.

### Async Patterns
- Core engine, portfolio, execution, and DataBus all use `asyncio.Lock` for mutable state.
- WebSocket clients run as `asyncio.create_task()` background tasks.
- Graceful shutdown is handled via signal handlers (`SIGINT`, `SIGTERM`) that await component `stop()` coroutines with a 10-second timeout.

### What NOT to do
- **Never** introduce `eval`, `exec`, `compile`, or `pickle.loads` — the security auditor (`AUDIT-001`, `AUDIT-006`) will reject them.
- **Never** use `os.system` or `subprocess` in core logic — also rejected by audit (`AUDIT-005`).
- **Never** hardcode secrets — use the vault or env vars (`AUDIT-002`).
- **Never** write files outside the project directory without using `validate_safe_path()` (`AUDIT-004`).

---

## 7. Testing Instructions

### Test Framework
The project uses **pytest** (`pytest.ini` at repo root, `asyncio_mode = auto`).
Tests are split into four markers (see `tests/conftest.py` and `pytest.ini`):

| Marker                | Meaning                                                                          | Run by default CI? |
|------------------------|-----------------------------------------------------------------------------------|---------------------|
| `unit`                 | Fast, no network, no cross-module wiring                                          | Yes |
| `integration_offline`  | Multi-component (OMS, reconciliation, engine boot/shutdown, walk-forward), mocks only, no network | Yes |
| `network`              | Real HTTP/WebSocket calls to external APIs (GoldRush, Hyperliquid, Coinalyze)      | No (opt-in) |
| `testnet_live`         | Live Hyperliquid testnet connection / real order placement                        | No (opt-in) |

Most files still carry `if __name__ == "__main__":` blocks for direct
invocation, but the canonical entry point is pytest.

### Running the Full Test Battery
```bash
python scripts/run_ci_tests.py                       # unit + integration_offline (default CI)
python scripts/run_ci_tests.py --network              # + network suite
python scripts/run_ci_tests.py --network --testnet-live

python -m pytest -m unit                              # ad-hoc: unit only
python -m pytest tests/test_cvd_orderflow.py -v       # ad-hoc: single file
```

### Notes
- `tests/test_ws.py`, `tests/test_ws_coin.py`, `tests/test_ws_ctx.py`, and
  `tests/test_funding.py` are marked `network` — they make real calls and are
  excluded from default CI.
- `scripts/manual/manual_socketio_client_check.py` and
  `manual_socketio_ws_check.py` (moved out of `tests/` — they were plain
  connect-and-print scripts with no assertions) require a locally running
  Socket.IO server and are not part of any CI suite.
- `tests/test_monte_carlo.py` is excluded from collection via
  `tests/conftest.py` — it targets a `MCResult`/`PercentileCI`/`run_monte_carlo`
  API removed from `src/backtest/monte_carlo.py` (current API: `MCMetrics`,
  `bootstrap_metrics`, `block_bootstrap_metrics`). Needs a rewrite before
  re-enabling.
- **`tests/test_critical_fixes.py`** — drawdown circuit, portfolio restore, FundingArbitrage lifecycle, execution exit price fix. Always run after modifying core engine, portfolio, risk, or execution.
- **`tests/test_cascade_simulation.py`** — VolatilityCircuitBreaker trip/extend/per-symbol isolation/snapshot, FundingBlackoutFilter boundary cases, DD CB regression, cold-start guard.
- **`tests/test_engine_boot_integration.py`** — full boot/shutdown cycle against a fully-stubbed `TradingEngine`: feed subscription, OMS poller start, startup reconciliation, then graceful shutdown (background task cancellation, unsubscribe, OMS stop). Companion to `tests/test_mainnet_readiness_5_6.py`, which covers `start()`/`stop()` in isolation.
- **`scripts/lookahead_audit.py --ci`** — static scan for future-data access (LOOKAHEAD-001..006). Fails CI on any non-LOW finding.

### Component Health Check
```bash
python audit_all.py
```
This imports and instantiates every major module to verify they load without import errors. It is **not** the security audit.

---

## 8. Security Considerations

### Static Security Auditor
Run before any deployment or after code changes:
```bash
python main.py --audit
# or
python -m src.security.audit --verbose --src-dir src
```

Rules checked:
- `AUDIT-001`: `eval` / `exec` / `compile` → CRITICAL
- `AUDIT-002`: Hardcoded secrets → CRITICAL
- `AUDIT-003`: HTTP to unverified domains → HIGH (allowlist gates known exchange APIs)
- `AUDIT-004`: File writes outside project → HIGH / MEDIUM
- `AUDIT-005`: `os.system` / `subprocess` → HIGH
- `AUDIT-006`: `pickle.loads` → HIGH
- `AUDIT-007`: Dynamic `__import__` → HIGH
- `AUDIT-008`: Suspicious keywords in comments → LOW
- `AUDIT-009`: HTTP client inventory → INFO

### Vault (`src/security/vault.py`)
- Encrypts secrets with Fernet (AES-128-CBC + HMAC-SHA256).
- PBKDF2-HMAC-SHA256 key derivation with **480,000 iterations** and a 32-byte random salt.
- Supports password-derived mode, OS keyring mode, and auto-generated keyring fallback.
- Atomic writes via temp file + `shutil.move()`.
- Falls back to environment variables if vault file is missing.

### Safe Path Validation
Always use `validate_safe_path()` from `src/utils/helpers.py` before file operations that accept user input.

### Live Trading Safety
- Paper mode is the **default**.
- Mainnet requires both a config flag and an environment variable (`HYPERLIQUID_MAINNET_ENABLED`).
- `start.bat` requires typing `MAINNET` as explicit confirmation.

---

## 9. Key Architectural Patterns

### Data Flow
```
Hyperliquid WSClient ──┐
                       ├─→ DataBus ──→ CandleBuilder ──→ DataBus ──→ TradingEngine
Binance WSClient ──────┘                          │              │
                                                  └→ Database    └→ Strategies
                                                                  │
                                                                  ├→ RiskManager
                                                                  ├→ ExecutionEngine
                                                                  └→ PortfolioState
```

### Event Topics on DataBus
- `price:{symbol}` — latest mid price
- `ctx:{symbol}` — asset context (OI, funding)
- `orderbook:{symbol}` — L2 orderbook metrics
- `candle_complete:{tf}:{symbol}` — completed OHLCV candle
- `trade:{symbol}` — individual trade tick

### Strategy Interface
All strategies extend `Strategy(ABC)` in `src/strategies/base.py` and implement:
```python
@property
@abstractmethod
def name(self) -> str: ...

@abstractmethod
def on_data(self, event: MarketEvent) -> Optional[Signal]: ...

@abstractmethod
def on_position(self, position: Position) -> Optional[ExitSignal]: ...
```

### Risk Gate
`TradingEngine` feeds every strategy signal through `RiskManager.check(...)` before execution. The risk manager enforces:
- Max 5 open positions
- Max 5 trades per day
- Max 3% daily loss
- 10% drawdown circuit breaker
- 60% directional exposure cap
- 30% sector exposure cap
- Correlation rejection (via `CorrelationMonitor`)
- ATR-based position sizing

### Determinism
Backtest and live modes share the **exact same** strategy and risk logic. The only difference is the data source (historical DB candles vs. live WebSocket ticks) and the execution layer (simulated fills vs. REST API orders).

### Risk Gate Order (Phase C, v3.1.10)
Every entry signal flows through these gates in order — fail any one → reject:
1. **Per-symbol lock** (`_get_symbol_lock` in `engine.py:983`) — serializes same-symbol processing.
2. **Volatility circuit breaker** (`VolatilityCircuitBreaker.is_blocked`) — soft gate; blocks entries when ATR(1h) > 3x 7d baseline for 30min. Config: `risk.volatility_circuit_breaker`.
3. **Funding-reset blackout** (`FundingBlackoutFilter.is_blocked`) — global time-of-day filter; blocks entries 5min before/after 00:00/08:00/16:00 UTC. Config: `risk.funding_blackout`.
4. **Correlation monitor** — rejects highly-correlated position adds (governed by `strategy.portfolio_governance.max_correlation`).
5. **`RiskManager.can_enter`** — daily trade count, daily loss, drawdown circuit, exposure caps, position-size cap, ATR-based sizing, leverage cap, max-positions.
6. **TCA check** (`passes_tca_check`) — slippage + fill ratio from L2 book.
7. **Order routing** (`resolve_order_routing`) — `post_only` vs `market` vs `limit`.

Soft gates (vol CB, funding blackout, correlation) only block new entries — never force exits. Hard gates (drawdown CB, daily loss) own flatten behavior.

### Per-Mode Overrides (Phase C, v3.1.10)
`mode_overrides.<mode>` in `settings.yaml` is shallow-merged on top of the active section by `_apply_mode_overrides` in `src/utils/config.py`. Used to ship safer mainnet defaults:
- `mainnet`: leverage 5x (was 10), max_daily_loss 2% (was 3), max_daily_trades 20 (was unlimited), max_pos 3% (was 5).
- `testnet`: max_daily_trades 50.

Effective settings are logged once at engine start (`Effective risk: leverage=...`).

---

## 10. Important Files for Agents

| File | Why it matters |
|------|----------------|
| `main.py` | Bootstraps everything; understand the initialization order before modifying startup logic. |
| `src/utils/config.py` | Configuration loader with defaults, deep-merge, env overrides, and type coercion. |
| `src/core/engine.py` | `TradingEngine` — the main event loop. Any change to signal flow or timing goes here. |
| `src/core/risk_manager.py` | Central risk gate. Changing position limits or adding new risk rules happens here. |
| `src/core/execution.py` | `ExecutionEngine` — paper simulation and live order submission. |
| `src/core/volatility_circuit.py` | `VolatilityCircuitBreaker` — soft per-symbol gate (Phase C, v3.1.10). |
| `src/core/funding_blackout.py` | `FundingBlackoutFilter` — time-of-day entry filter (Phase C, v3.1.10). |
| `src/strategies/base.py` | `Strategy` ABC, `MarketEvent`, `Signal`, `Position`, `ExitSignal`. |
| `src/exchanges/hyperliquid_ws.py` | WebSocket client and `DataBus` pub/sub implementation. |
| `src/strategies/orderbook_scalper.py` | OrderBookScalper — scalps bid_ask_ratio micro-imbalances with tight TP/SL. |
| `src/strategies/cvd_orderflow.py` | CVDOrderFlow — multi-timeframe (5m/15m/1h) cumulative volume delta divergence. Uses `buy_volume`/`sell_volume` from candle_builder. |
| `src/strategies/ensemble.py` | StrategyEnsemble — weighted consensus across all enabled sub-strategies. |
| `src/strategies/lead_lag.py` | LeadLag — Binance USD-M perp mark vs HL mid lag arb (short hold). |
| `src/exchanges/binance_price_bridge.py` | Spot `@aggTrade` → DataBus `binance_price:{symbol}`. |
| `src/exchanges/binance_perp_price_bridge.py` | USD-M `@markPrice@1s` → DataBus `binance_perp_price:{symbol}` (LeadLag). |
| `src/data/database.py` | SQLite schema and all persistence queries. |
| `scripts/backfill_candles.py` | Binance historical candle backfill to populate candle tables before bot start. |
| `scripts/lookahead_audit.py` | `LOOKAHEAD-001..006` static scanner (Phase B, v3.1.9). |
| `src/security/audit.py` | Static security scanner. If you add new file-I/O or HTTP patterns, update the auditor. |
| `config/settings.yaml` | All tunable parameters. Add new strategy params here and in `DEFAULT_CONFIG`. |
| `src/utils/config.py` | Configuration loader: defaults, deep-merge, env overrides, **`_apply_mode_overrides`**. |

---

## 11. Development Checklist for Agents

Before submitting any code change:

1. **Run the security audit** and ensure zero CRITICAL / MED / LOW findings (HIGH is acceptable only for pre-existing `AUDIT-005` in `crash_recovery.py`):
   ```bash
   python main.py --audit
   ```
2. **Run `audit_all.py`** to verify no import / instantiation regressions:
   ```bash
   python audit_all.py
   ```
3. **Run the relevant test files** for the subsystem you changed:
   ```bash
   python tests/test_basic.py
   python tests/test_critical_fixes.py
   python tests/test_cascade_simulation.py
   python tests/test_qw_observability.py   # if you touched DB schema or trade entry
   python tests/test_log_rotation.py       # if you touched setup_logger
   python tests/test_<relevant_task>.py
   ```
4. **Run the look-ahead audit** to catch future-data access regressions:
   ```bash
   python scripts/lookahead_audit.py --ci
   ```
5. **Ensure type hints** are present on new public functions.
6. **Use safe helpers** (`safe_float`, `safe_json_loads`, `validate_safe_path`) instead of raw conversions.
7. **Do not add** `eval`, `exec`, `pickle.loads`, `subprocess`, or hardcoded secrets.
8. **Update `DEFAULT_CONFIG`** in `src/utils/config.py` if you introduce new required config keys.
9. **Update this `AGENTS.md`** if you change build steps, testing procedures, or security rules.
10. **Do not add a strategy to `execution_strategies`** without a baseline-signal
    gate PASS (three conditions) — see §12. Legacy paper names
    (ChecklistMeta, VWAPDeviation) are soft-exempt until deliberately re-gated.

---

## 12. Baseline-signal gate (research → execution)

### Feature screening first (required before new strategies)

Do **not** invent strategy #N from a pattern that “worked elsewhere.” Measure
whether raw features predict forward returns **before** building entries,
stops, or sizing.

```
feature with predictive power
  → simple strategy around it
  → baseline-signal gate (this section)
  → shadow
  → execution (PASS required)
```

Never the inverse. Screening CLI: `python scripts/feature_screening.py`.
Report: `docs/FEATURE_SCREENING_REPORT.md`. Only TOP survivors (FDR +
monotonicity + temporal stability + cross-symbol consistency) justify a
strategy attempt. Pre-FDR hits alone do **not**.

No strategy may be **added** to `strategy.phase08.execution_strategies` without
passing the baseline-signal gate on at least one validation fold (W2/W3).

### Three cumulative conditions

```
PASS  ⇔  B1 ≥ p95   AND   n_trades ≥ 30   AND   expectancy > 0 (PF > 1)
```

| Condition | Meaning |
|-----------|---------|
| **B1 ≥ p95** | At real signal times, randomizing only direction: the strategy’s profit factor must beat ≥95% of random-direction seeds. Isolates *directional* information while holding market conditions fixed. |
| **n_trades ≥ 30** | Statistical power floor. Below this → **INCONCLUSIVE** (not tested), never “no edge”. |
| **expectancy > 0 (PF > 1)** | Must be profitable in absolute terms. Beating noise while losing money is not edge. |

Any miss other than underpowered sample → **FAIL**, and the CLI must report
**which** condition failed.

**B2** (random timing, real direction) and **B3** (both random) are complementary
diagnostics; **B1 is the primary gate**.

### Why percentile alone is insufficient (SmartMoneyFlow W3)

SmartMoneyFlow on W3 reached **B1 PF percentile 96** with engine **PF ≈ 0.27**
(large absolute loss). Under a percentile-only rule it would have “passed” while
reliably losing money — it only lost *less* than chance in an adverse regime.
That case is why profitability is a hard third condition: **FAIL
(`not_profitable`)**, not PASS.

### INCONCLUSIVE vs FAIL vs frequency

- **INCONCLUSIVE** (`n_trades < 30`, or no runnable replay): strategy was **not
  tested**. Do **not** kill or demote solely on INCONCLUSIVE.
- **FAIL**: powered sample and at least one of (B1 / profitability) missed.
- **INCONCLUSIVE (frequency insufficient):** if a strategy cannot produce
  `n_trades ≥ 30` in a **90-day / quarter** observation window (live shadow or
  replay), it is **not validatable and not usable** in practice. This is a
  legitimate standing verdict beside PASS/FAIL — never call it FAIL.

### First real demotion (precedent, 2026-08-09)

**ChecklistMeta** failed the three-condition gate with power on both W2
(B1=48, n=146) and W3 (B1=43, n=215). Keeping it in `execution_strategies`
would make the gate decorative. Precedent: **demote FAIL → shadow**; do not
re-promote without a fresh PASS. Script:
`scripts/demote_checklist_meta_for_baseline_fail.py`.

**VWAPDeviation** stays in execution only because it is **INCONCLUSIVE**
(n&lt;30) — grandfathered while sample accumulates — not because it passed.

### Fidelity / Tier B

When missing feeds (L2, tape, funding, binance_perp, liquidations) hurt the
strategy but not the random baselines, the test is **conservative against the
strategy**. Declare that on the result. Zero-trade Tier-B names are **not
validatable** in candle-only replay and therefore **not promotable** until a
powered run exists.

### CLI

```bash
python scripts/baseline_signal_gate.py --strategy NAME --folds W2,W3 --seeds 200 --gate
# exit 0=PASS, 1=FAIL, 2=INCONCLUSIVE
python scripts/baseline_signal_gate.py --portfolio --seeds 200
python scripts/baseline_signal_gate.py --validate-harness --seeds 40
```

Artifact board: `data/backtests/parity_diag/BASELINE_PORTFOLIO_GATE_REPORT.md`.
Longer write-up: `docs/BASELINE_SIGNAL_GATE.md`.

**Feed silence (2026-08-09):** contracted feeds
(`liquidation_okx`, `liquidation_bybit`, `liquidation_binance`,
`liquidation_coinalyze_check`, `binance_perp`, funding venues, `taker_split`)
must alert if quiet beyond configured thresholds — see
`FeedSilenceMonitor` / `docs/FEED_CONTAMINATION_AUDIT.md` /
`docs/LIQUIDATION_AGGREGATOR.md`. A dead venue must never again go unnoticed
for weeks. Hyperliquid has **no** public market-wide liquidation channel;
do not contract `liquidation_hl`.

`market_data.liquidation_source`: `real` | `binance` | `proxy` | `auto`
(see § liquidation aggregator).

### Preregister asymmetry (hard at entry, soft for legacy)

| Path | Behaviour |
|------|-----------|
| **Promote / add** a non-legacy strategy to `execution_strategies` | **Hard:** `baseline_signal_gate` with `verdict: PASS` required (`assert_can_promote_to_execution` / persist overwrite). |
| **Startup** with ChecklistMeta / VWAPDeviation already executing | **Soft:** missing gate field does **not** brick the paper bot (`LEGACY_EXECUTION_WITHOUT_BASELINE_GATE`). |
| Gate record present but not PASS | Always fails, including for legacy names. |
| `BOT_REQUIRE_BASELINE_GATE=1` | Removes grandfathering — every execution name needs PASS. |

This is intentional, not inconsistent: measurement post-dates the current paper
set; blocking boot would add no edge, while blocking *new* promotions prevents
repeating the CM mistake.

Hooks: `src/research/phase08_preregister.py`, `phase10_preregister.py`.

---

*Last updated: 2026-08-09 (feature-screening-first rule + baseline-signal gate).*
