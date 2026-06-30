# Hyperliquid Premium Trading Bot v3.1.23

Professional automated trading bot for Hyperliquid perpetuals exchange.
Modular async architecture, real-time WebSocket data, 12 pluggable
strategies, deterministic risk management, paper / testnet / mainnet
execution, and a Flask + Socket.IO dashboard with 12 real-time panels.

Current score: **9.5/10** (Phases A, B, C hardening + QW observability
+ v3.1.14 CVDOrderFlow volume unit fix + v3.1.15 volume observability
panel + v3.1.16-v3.1.22 critical bug fixes, 4 new strategies, HMM regime
detection, Monte Carlo bootstrap, walk-forward optimization, OMS order
tracking, leverage-aware sizing, L2 slippage, position reconciliation,
funding payment accounting + v3.1.23 dashboard parity redesign).

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
| Paper Trading (default)             | [OK]  | No real money |
| Testnet + Mainnet execution         | [OK]  | Mainnet requires explicit confirmation |
| Real-time WebSocket dashboard       | [OK]  | Flask + Socket.IO |
| HL WS feeds (mids, OI, trades, L2)  | [OK]  | 4 channels |
| Binance WS (trades, liquidations)   | [OK]  | forceOrder + aggTrade |
| Cross-venue funding (4 venues)      | [OK]  | HL predicted + Bin/Bybit/OKX REST + Coinalyze (optional) |
| 12 trading strategies               | [OK]  | 10 active in current regime, 2 disabled by governor |
| Strategy governor (auto-disable)    | [OK]  | Negative Sharpe over 30d => off |
| Drawdown circuit breaker (10%)      | [OK]  | Hard gate, auto-reset at 00:00 UTC |
| Intraday volatility circuit         | [OK]  | Soft gate, blocks entries when ATR>3x baseline |
| Funding-reset time blackout         | [OK]  | +/-5min around 00:00/08:00/16:00 UTC |
| Kelly Criterion sizing              | [OK]  | Per-strategy, bounded |
| Correlation monitor                 | [OK]  | Rejects correlated adds |
| Look-ahead / future-data audit      | [OK]  | Static scanner (Phase B) |
| Static security audit               | [OK]  | 9 rules (eval/subprocess/secret/etc.) |
| Encrypted credential vault          | [OK]  | Fernet + PBKDF2 480k iterations |
| Crash-recovery wrapper              | [OK]  | 3 restarts, 30s cooldown |

---

## Strategies

**Estado actual (v3.1.38):** apenas **3 estratégias activas** em direct mode (ensemble OFF).
Todas as restantes falharam audit ou walk-forward — ver `docs/STRATEGY_AUDIT.md`.

`StrategyGovernor` auto-desactiva qualquer sub-estratégia com Sharpe rolling negativo (30 dias).

| Strategy             | Type           | Status (v3.1.38) | Notes |
|----------------------|----------------|------------------|-------|
| VolatilityBreakout   | trend          | **Active**       | Bollinger squeeze + trailing EMA9; W1/W3 trending |
| VWAPDeviation        | mean-reversion | **Active**       | Z-score vs VWAP; session filter 07–22 UTC |
| ChecklistMeta        | meta-scoring   | **Active**       | Weighted checklist; única PF>1 em regime choppy W2 |
| SFP Reversion        | mean-reversion | OFF              | Regime-dependent; componente do ChecklistMeta |
| VA Rejection         | mean-reversion | OFF              | Regime-dependent; sweep v3.1.38 não passou |
| TrendPyramid         | trend          | OFF              | Outlier distorce PF |
| SmartMoneyFlow       | trend          | OFF              | PF 0.27 — KILL |
| DonchianBreakout     | trend          | OFF              | Sharpe -7.4 — KILL |
| CVDOrderFlow         | order-flow     | OFF              | Marginal; WATCH only |
| LeadLag              | microstructure | OFF              | 0 backtest trades |
| LiquidationCatcher   | event-driven   | OFF              | 0 backtest trades |
| OrderBookScalper     | microstructure | OFF              | KILLED v3.1.18 |
| RangeGrid            | revert         | OFF              | Sharpe -4.3 — KILL |
| SpotPerpCarry        | carry          | OFF              | Sem dados spot |
| FundingMomentum      | carry          | OFF              | 0 backtest trades |
| FundingArbitrage     | market-neutral | OFF              | KILLED v3.1.18 |
| FundingExtreme       | mean-reversion | OFF              | Inconsistente / bug histórico |

Ensemble (`strategy.ensemble.enabled: false`) está **desligado**. Cada estratégia activa gera sinais
de forma independente; não há consenso ponderado nem high-conviction bypass.

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
  tests/                       # Hybrid suite: unittest + assertion scripts
  scripts/                     # backfill, lookahead audit, CI runner
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

The suite is hybrid (unittest + standalone assertion scripts).
271+ tests across 22 files. No central pytest runner; each test is invoked directly.

```bash
# Core smoke + regression
python -m unittest tests.test_basic               # 11 unittest smoke
python tests/test_critical_fixes.py               # v3.1.1 regression (5 tests)
python tests/test_cascade_simulation.py           # Phase C stress (7 tests)

# Strategy tests
python tests/test_cvd_orderflow.py                # CVDOrderFlow (23 tests, v3.1.16 USD fix)
python tests/test_volume_indicators.py            # OBV + MFI + VWAP-multi-TF (16 tests, v3.1.15)
python tests/test_spot_perp_carry.py              # SpotPerpCarry (10 tests, v3.1.20)
python tests/test_range_grid.py                   # RangeGrid (10 tests, v3.1.20)
python tests/test_trend_pyramid.py                # TrendPyramid (10 tests, v3.1.20)
python tests/test_funding_momentum.py             # FundingMomentum (11 tests, v3.1.20)

# Risk + execution
python tests/test_leverage_sizing.py              # Leverage-aware sizing (16 tests, v3.1.22)
python tests/test_execution_oms.py                # OMS order tracking (19 tests, v3.1.22)
python tests/test_reconcile.py                    # Position reconciliation (3 tests, v3.1.17)
python tests/test_dashboard_v3123.py              # Dashboard v3.1.23 features (13 tests)

# Quant models + data
python tests/test_hmm_regime.py                   # HMM regime detection (17 tests, v3.1.21)
python tests/test_monte_carlo.py                  # Monte Carlo bootstrap (20 tests, v3.1.21)
python tests/test_walk_forward.py                 # Walk-forward optimization (19 tests, v3.1.21)
python tests/test_funding_normalize.py            # Per-symbol funding intervals (23 tests, v3.1.21)
python tests/test_funding_stale_detection.py      # Stale funding detection (11 tests, v3.1.21)

# Observability + infra
python tests/test_qw_observability.py             # decision_audit + trade journal (11 tests, v3.1.12)
python tests/test_log_rotation.py                 # TimedRotatingFileHandler (9 tests, v3.1.12)
python tests/test_databus_per_topic.py            # DataBus per-topic rate limit (10 tests, v3.1.13)
python tests/test_mainnet_readiness_5_6.py        # Mainnet safe shutdown + WS health (7 tests, v3.1.22)

# Audits
python scripts/lookahead_audit.py --ci            # Future-data leakage scanner (CI mode)
python audit_all.py                               # Component health (14 strategies + ensemble)
python -m src.security.audit                      # Static security (9 AUDIT rules)
```

---

## Requirements

- Python 3.11+ (tested on 3.14)
- Windows or Linux/macOS
- All dependencies in `requirements.txt` (fully pinned)

---

## License

MIT
