# Hyperliquid Premium Trading Bot v3.1.15

Professional automated trading bot for Hyperliquid perpetuals exchange.
Modular async architecture, real-time WebSocket data, 8 pluggable strategies,
deterministic risk management, paper / testnet / mainnet execution, and a
Flask + Socket.IO dashboard.

Current score: **9.5/10** (Phases A, B, C hardening + QW observability + v3.1.14
CVDOrderFlow volume unit fix + v3.1.15 volume observability panel:
OBV slope, MFI, rolling VWAP multi-TF).

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
| 7 trading strategies                | [OK]  | 5 active in current regime, 2 disabled by governor |
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

The 7 active strategies are governed by `StrategyGovernor` which auto-disables
any strategy with negative Sharpe over the last 30 days. After v3.1.14 cleanup,
the dead reference to `FundingExtreme` was removed from the factory table
(`SmartMoneyFlow` is kept because it's the display name returned by the
`TrendFollow` class — see `src/strategies/trend_follow.py:name`).

| Strategy             | Type           | Status (typical) | Notes |
|----------------------|----------------|------------------|-------|
| FundingArbitrage     | market-neutral | Active           | Best performer; uses HL predicted + cross-venue CEX |
| LiquidationCatcher   | event-driven   | Active           | Fades $10M+ Binance liquidations |
| VWAPDeviation        | mean-reversion | Active (low-vol) | Z-score vs VWAP(1h), ADX filter |
| VolatilityBreakout   | trend          | Active           | ATR-scaled breakout, regime-weighted |
| OrderBookScalper     | microstructure | Active           | Fades OIR micro-imbalances, tight TP/SL |
| CVDOrderFlow         | order-flow     | Active           | Multi-TF CVD divergence (5m/15m/1h) |
| SmartMoneyFlow       | trend          | Disabled         | Sharpe negative in current regime |
| DonchianBreakout     | trend          | Disabled         | Sharpe negative (small sample) |
| FundingExtreme       | mean-reversion | Disabled         | Sharpe -37, kept off permanently |

Ensemble logic combines signals via weighted consensus (configurable threshold).

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
2. Volatility circuit breaker (soft; blocks entries when ATR>3x baseline)
3. Funding-reset blackout (soft; blocks +/-5min around funding reset)
4. Correlation monitor (rejects correlated adds)
5. `RiskManager.can_enter` (hard; daily trades / loss / DD / exposure / leverage)
6. TCA check (slippage + fill ratio from L2)
7. Order routing (post-only vs market vs limit)

Soft gates (1-4) only block new entries. Hard gates (5) own flatten behavior.

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
No central pytest runner; each test is invoked directly.

```bash
python -m unittest tests.test_basic               # 11/11 unittest smoke
python tests/test_critical_fixes.py               # v3.1.1 regression
python tests/test_cascade_simulation.py           # Phase C stress (7 tests)
python tests/test_cvd_orderflow.py                # CVDOrderFlow 21/21 (v3.1.14 + volume unit test)
python tests/test_volume_indicators.py            # v3.1.15 OBV + MFI + VWAP-multi-TF (16 tests)
python tests/test_qw_observability.py             # v3.1.12 QW1+QW2 (11 tests)
python tests/test_log_rotation.py                 # v3.1.12 QW3 (9 tests)
python tests/test_databus_per_topic.py            # v3.1.13 QW4 (7 tests)
python scripts/lookahead_audit.py --ci            # Phase B future-data
python audit_all.py                               # Component health
python -m src.security.audit                      # Static security
```

---

## Requirements

- Python 3.11+ (tested on 3.14)
- Windows or Linux/macOS
- All dependencies in `requirements.txt` (fully pinned)

---

## License

MIT
