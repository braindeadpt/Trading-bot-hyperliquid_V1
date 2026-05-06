# Hyperliquid Bot Premium — Architecture Blueprint

## Philosophy
Clean architecture. No patches. No eval. No god classes. Test-driven. WebSocket-first.

## Stack
- Python 3.11+
- WebSocket (websockets library) for real-time data
- SQLite for local state + historical data
- Flask + Socket.IO for dashboard push updates
- Pandas + NumPy for calculations

## Directory Structure
```
hyperliquid-bot-premium/
├── src/
│   ├── core/
│   │   ├── engine.py          # Main orchestrator — runs both strategies
│   │   ├── risk_manager.py    # Position sizing, daily limits, circuit breaker
│   │   ├── execution.py       # Paper trade executor + testnet bridge
│   │   └── portfolio.py       # Portfolio state, capital tracking
│   ├── strategies/
│   │   ├── base.py            # Abstract strategy interface
│   │   ├── trend_follow.py    # Strategy 1: Smart Money Flow (breakout + volume + OI)
│   │   └── mean_reversion.py  # Strategy 2: Funding extreme (contrarian)
│   ├── exchanges/
│   │   ├── hyperliquid_ws.py  # WebSocket client for HL (price, OI, funding, trades)
│   │   ├── hyperliquid_rest.py # REST client for order placement
│   │   └── binance_api.py     # Free Binance API (volume, VWAP, orderflow proxy)
│   ├── data/
│   │   ├── database.py        # SQLite: trades, signals, candles, OI, funding, prices
│   │   ├── candle_builder.py  # Aggregates 1m WebSocket ticks into 5m/15m/1h candles
│   │   └── historical_fetcher.py # Downloads historical data from APIs
│   ├── backtest/
│   │   ├── engine.py          # Backtest runner using DB data
│   │   └── metrics.py         # Sharpe, Sortino, max drawdown, win rate
│   ├── dashboard/
│   │   ├── web.py             # Flask + Socket.IO server
│   │   └── index.html         # Cypherpunk dashboard (real-time push)
│   ├── security/
│   │   ├── vault.py           # Encrypted credential storage
│   │   └── audit.py           # Security audit runner
│   └── utils/
│       ├── config.py          # YAML config loader
│       ├── logger.py          # Structured logging
│       └── helpers.py         # Math helpers, time utils
├── config/
│   └── settings.yaml
├── data/
│   ├── historical/            # CSV/DB historical data
│   └── live/                  # Runtime SQLite DB
├── tests/
│   ├── test_strategies.py
│   ├── test_risk.py
│   ├── test_backtest.py
│   └── test_api.py
└── main.py                    # Entry point
```

## Key Design Decisions

### 1. WebSocket-First Architecture
- **Hyperliquid WS**: Subscribe to `allMids` (prices), `activeAssetCtxs` (OI, funding), `trades` (orderflow proxy)
- **Binance WS**: Subscribe to aggregate trades for volume/flow
- All data flows through a single `DataBus` that distributes to strategies + DB + dashboard

### 2. Dual Strategy Architecture
```
DataBus (WebSocket ticks)
    ├── CandleBuilder (1m → 5m → 15m → 1h)
    ├── Strategy 1: TrendFollow
    │   ├── Input: 15m candles (OHLCV + OI delta + funding + VWAP + volume profile)
    │   ├── Signal: Breakout + volume surge + OI growing + not overcrowded
    │   └── Action: Enter LONG or SHORT, ride the trend
    ├── Strategy 2: MeanReversion
    │   ├── Input: Funding rate + predicted funding + OI concentration
    │   ├── Signal: Extreme funding (>±0.8%) + OI overcrowded alert
    │   └── Action: Enter contrarian position, exit on reversion
    └── RiskManager (both strategies feed into same risk engine)
```

### 3. Overcrowded Detection
- OI ratio long/short from Hyperliquid
- Funding + OI combined: funding > 1% + OI 70%+ on one side = extreme overcrowding
- Not a hard reject — feeds into confidence score reduction

### 4. Paper Trading Layers
- **Layer 1**: Internal simulation (no latency, for backtest)
- **Layer 2**: Testnet real (submits to Hyperliquid testnet, real matching)
- **Layer 3**: Mainnet (real money, manual activation only)

### 5. Backtest Engine
- Pulls historical candles + funding + OI from DB
- Simulates both strategies with exact same logic as live
- Outputs: equity curve, trades list, metrics (Sharpe, win rate, max DD)
- Results feed into dashboard

### 6. Dashboard (Cypherpunk)
- Real-time via Socket.IO (not polling)
- Panels: Live positions, PnL, funding rates, OI chart, strategy signals, equity curve
- Color scheme: Dark neon / Matrix
- Mobile responsive

## Interfaces

### Strategy Interface (all strategies implement)
```python
class Strategy(ABC):
    @abstractmethod
    def on_data(self, event: MarketEvent) -> Optional[Signal]
    @abstractmethod  
    def on_position(self, position: Position) -> Optional[ExitSignal]
    @property
    @abstractmethod
    def name(self) -> str
```

### MarketEvent
```python
@dataclass
class MarketEvent:
    symbol: str
    price: float
    timestamp_ms: int
    candle_1m: Optional[Candle]
    candle_5m: Optional[Candle]
    candle_15m: Optional[Candle]
    funding: Optional[float]
    predicted_funding: Optional[float]
    oi_total: Optional[float]
    oi_delta: Optional[float]      # change since last period
    volume_1m: Optional[float]
    bid_ask_imbalance: Optional[float]
    vwap_15m: Optional[float]
```

### Signal
```python
@dataclass
class Signal:
    strategy: str
    symbol: str
    side: str  # 'long' | 'short'
    confidence: float  # 0.0 - 1.0
    size_pct: float    # % of capital to use
    entry_price: Optional[float]  # market if None
    stop_loss_pct: float
    take_profit_pct: Optional[float]
    reason: str
    metadata: Dict
```

## Data Flow
```
Hyperliquid WS ──┐
                 ├─→ DataBus ──→ CandleBuilder ──→ MarketEvent ──→ Strategies
Binance WS ──────┘                              │
                                                └→ Database (SQLite)
                                                └→ Dashboard (Socket.IO push)

Strategy ──Signal──→ RiskManager ──→ Execution (paper/testnet/mainnet)
                         │
                         └→ Database (trades, PnL)
```

## Security
- API keys stored encrypted (AES-256 via keyring or env)
- No eval/exec anywhere
- All user input validated
- Audit script checks for data exfiltration, hardcoded keys, etc.

## Next Steps
1. Implement DataBus + WebSocket clients
2. Implement Database + CandleBuilder
3. Implement both strategies
4. Implement RiskManager + Execution
5. Implement Dashboard
6. Implement Backtest engine
7. Integration test + audit
