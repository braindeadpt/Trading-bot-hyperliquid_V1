# AGENTS.md — Hyperliquid Premium Trading Bot v3.1

> This file is intended for AI coding agents. It assumes zero prior knowledge of the project.

---

## 1. Project Overview

This is **Hyperliquid Premium Trading Bot v3.1** — an async Python trading bot for the Hyperliquid perpetuals exchange. It supports **paper trading** (default), **testnet**, and **mainnet** execution modes.

The bot is built around a **WebSocket-first event architecture**: real-time market data from Hyperliquid (and optional Binance feeds) flows through an async pub/sub `DataBus`, gets aggregated into multi-timeframe candles, and is consumed by six strategy modules (including the OrderBook Imbalance Scalper for tick-level orderbook micro-patterns). A central `TradingEngine` orchestrates signal generation, risk gating, position sizing, and execution. All state is persisted to a local SQLite database, and a Flask + Socket.IO dashboard provides real-time monitoring.

**Key characteristics:**
- Fully async (`asyncio`) with auto-reconnecting WebSocket clients.
- Modular strategy system with an abstract base class.
- Deterministic risk management shared between backtest and live runs.
- Encrypted credential vault + static security auditor.
- No `eval`, `exec`, `pickle.loads`, or dynamic imports anywhere in core code.

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
│   ├── strategies/            # 6 strategies + base ABC + indicators + ensemble
│   ├── exchanges/             # Hyperliquid WS/REST, Binance API, funding aggregator
│   ├── data/                  # SQLite DB, candle builder, orderbook metrics, historical fetcher
│   ├── dashboard/             # Flask + Socket.IO server + embedded HTML UI
│   ├── security/              # Encrypted vault + static security audit engine
│   ├── alerts/                # Telegram / Discord notifier
│   ├── backtest/              # Backtest engine + performance metrics
│   └── utils/                 # Config loader, logger, helpers, crash recovery, log monitor
├── tests/                     # Test suite (hybrid: unittest + manual assertion scripts)
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
- `strategy`: parameters for all 5 strategies + Kelly Criterion + cooldown governance
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
The project uses a **hybrid approach**:
- **Manual assertion-based tests** — most files (`test_task_*.py`, `test_fase_4.py`, etc.) are standalone scripts using `assert` + `print()`. Run them individually:
  ```bash
  python tests/test_task_2_3.py
  python tests/test_fase_4.py
  ```
- **unittest** — only `tests/test_basic.py` uses Python's built-in `unittest` framework. Run it with:
  ```bash
  python -m unittest tests.test_basic
  # or
  python tests/test_basic.py
  ```

### Running the Full Test Battery
```bash
python tests/test_tasks_1_4_2_1_2_2.py   # ADX, regime weights, slippage, SmartMoneyFlow
python tests/test_task_2_3.py            # Dynamic thresholds, cross-exchange, OI filter
python tests/test_task_2_4.py            # Cooldown state machine, doubling, auto-reset
python tests/test_task_3_1.py            # FundingArbitrage pair selection, spread, exit
python tests/test_task_3_2.py            # VWAPDeviation Z-score, entry, ADX filter, exit
python tests/test_task_3_3.py            # LiquidationCatcher entry, filters, 2R exit, max hold
python tests/test_fase_4.py              # Portfolio governance: drawdown, exposure, Kelly
python tests/test_basic.py               # unittest smoke tests for core components
```

### Notes
- `test_funding.py` and `test_ws*.py` are **live integration tests** — they require network access and API keys.
- There is **no centralized test runner** (no `pytest.ini`, `tox.ini`, or CI pipeline).
- `pytest` and `pytest-asyncio` are listed as commented-out optional dependencies in `requirements.txt`.
- **`tests/test_critical_fixes.py`** — tests for the v3.1.1 patch (drawdown circuit, portfolio restore, FundingArbitrage lifecycle, execution exit price fix). Always run this after modifying core engine, portfolio, risk, or execution.

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

---

## 10. Important Files for Agents

| File | Why it matters |
|------|----------------|
| `main.py` | Bootstraps everything; understand the initialization order before modifying startup logic. |
| `src/utils/config.py` | Configuration loader with defaults, deep-merge, env overrides, and type coercion. |
| `src/core/engine.py` | `TradingEngine` — the main event loop. Any change to signal flow or timing goes here. |
| `src/core/risk_manager.py` | Central risk gate. Changing position limits or adding new risk rules happens here. |
| `src/core/execution.py` | `ExecutionEngine` — paper simulation and live order submission. |
| `src/strategies/base.py` | `Strategy` ABC, `MarketEvent`, `Signal`, `Position`, `ExitSignal`. |
| `src/exchanges/hyperliquid_ws.py` | WebSocket client and `DataBus` pub/sub implementation. |
| `src/strategies/orderbook_scalper.py` | OrderBookScalper — scalps bid_ask_ratio micro-imbalances with tight TP/SL. |
| `src/strategies/ensemble.py` | StrategyEnsemble — weighted consensus across all 6 sub-strategies. |
| `src/data/database.py` | SQLite schema and all persistence queries. |
| `scripts/backfill_candles.py` | Binance historical candle backfill to populate candle tables before bot start. |
| `src/security/audit.py` | Static security scanner. If you add new file-I/O or HTTP patterns, update the auditor. |
| `config/settings.yaml` | All tunable parameters. Add new strategy params here and in `DEFAULT_CONFIG`. |

---

## 11. Development Checklist for Agents

Before submitting any code change:

1. **Run the security audit** and ensure zero CRITICAL / HIGH findings:
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
   python tests/test_<relevant_task>.py
   ```
4. **Ensure type hints** are present on new public functions.
5. **Use safe helpers** (`safe_float`, `safe_json_loads`, `validate_safe_path`) instead of raw conversions.
6. **Do not add** `eval`, `exec`, `pickle.loads`, `subprocess`, or hardcoded secrets.
7. **Update `DEFAULT_CONFIG`** in `src/utils/config.py` if you introduce new required config keys.
8. **Update this `AGENTS.md`** if you change build steps, testing procedures, or security rules.

---

*Last updated: 2026-05-11 (v3.1.1 patch applied)*
