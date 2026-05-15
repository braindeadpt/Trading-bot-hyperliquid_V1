# Hyperliquid Premium Trading Bot v3.1

Professional automated trading bot for Hyperliquid perp exchange with modular architecture, real-time dashboard, 5 confluence strategies, advanced risk management, auto-recovery, and paper/testnet/live execution.

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run in Paper Trading mode
python main.py --mode paper

# Or use the launcher
./quickstart.bat
```

Dashboard: http://localhost:5000

---

## Features

| Feature | Status |
|---------|--------|
| Paper Trading | ✅ |
| Real-time Dashboard | ✅ |
| WebSocket Data (price, funding, OI, orderbook) | ✅ |
| Cross-exchange Funding Aggregation | ✅ |
| 5 Trading Strategies | ✅ |
| Kelly Criterion Sizing | ✅ |
| Drawdown Circuit Breaker | ✅ |
| Auto-recovery on Crash | ✅ |
| Security Audit | ✅ |

---

## Strategies

### 1. TrendFollow (SmartMoneyFlow)
- Trend following with 9 confluence conditions
- Filters: OIR, RSI, orderbook walls, funding
- Confidence: 6/9 → 0.50, 8/9 → 1.00

### 2. FundingExtreme (MeanReversion)
- Contrarian on extreme funding rates
- Dynamic percentile thresholds (p90/p70)
- Cross-exchange confirmation + OI decreasing

### 3. FundingArbitrage
- Market-neutral funding spread
- Long negative funding, short positive funding
- Exit on funding reversion (< 0.2%)

### 4. VWAPDeviation
- Mean reversion to VWAP(1h)
- Z-score threshold: 1.8σ
- Filters: ADX < 35, OIR, funding

### 5. LiquidationCatcher
- Fade liquidation cascades ($10M+ in 5min)
- Opposite direction entry
- Stop: 1% ATR, Take Profit: 2R

---

## Configuration

Key parameters in `config/settings.yaml`:

```yaml
mode: "paper"              # paper | testnet | mainnet

assets:
  - "BTC"
  - "ETH"
  - "SOL"

risk:
  initial_capital: 10_000
  max_positions: 5
  circuit_breaker_drawdown_pct: 10.0

strategy:
  mean_reversion:
    extreme_threshold: 0.003   # 0.3% funding
    strong_threshold: 0.002    # 0.2% funding

  funding_arbitrage:
    min_funding_spread: 0.004  # 0.4% spread

  vwap_deviation:
    z_threshold: 1.8           # 1.8 sigma
    max_adx: 35.0

  liquidation_catcher:
    min_notional_usd: 10_000_000  # $10M

  ensemble:
    threshold: 0.40
    min_agreeing: 1
```

---

## Project Structure

```
trading-bot-hyperliquid/
├── main.py                    # Entry point
├── run_with_recovery.py       # Auto-restart wrapper
├── config/
│   └── settings.yaml          # Configuration
├── src/
│   ├── core/                  # Engine, risk, execution, portfolio
│   ├── strategies/            # 5 strategy modules
│   ├── exchanges/             # Hyperliquid WS/REST, Binance, funding aggregator
│   ├── data/                  # Candle builder, database, orderbook metrics
│   ├── dashboard/             # Flask + Socket.IO dashboard
│   └── utils/                 # Config, logger, helpers
├── tests/                     # Test suite
└── logs/                     # Runtime logs
```

---

## Commands

```bash
# Paper trading
python main.py --mode paper

# Testnet
python main.py --mode testnet

# Mainnet (requires API keys)
python main.py --mode mainnet

# Backtest
python main.py --backtest --from-date 2024-01-01 --to-date 2024-03-01

# Security audit
python main.py --audit
```

---

## Security

- **Paper mode is default** — no real money at risk
- Live trading requires API keys in `config/.env`
- `.env` is gitignored — never committed
- Run `python main.py --audit` before live trading

---

## Requirements

- Python 3.11+
- Windows or Linux/macOS
- All dependencies in `requirements.txt`

---

## License

MIT