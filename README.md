# Hyperliquid Premium Trading Bot

Bot de trading automatizado para Hyperliquid com arquitetura modular, dashboard real-time, e duas estratégias de confluência (TrendFollow + MeanReversion).

---

## Estado actual

| Componente | Estado |
|---|---|
| Bot paper trading | ✅ Funcional — `python main.py --mode paper` |
| Dashboard web | ✅ Funcional — http://localhost:5000 |
| WebSocket Hyperliquid | ✅ Ligado (tickers + allMids + assetCtxs) |
| CandleBuilder | ✅ 1m / 5m / 15m / 1h |
| Orderflow (bid/ask imbalance) | ✅ Integrado na TrendFollow |
| Crash recovery + graceful shutdown | ✅ Implementado |
| Paper trader | ✅ Simula execução com slippage |

---

## Estrutura do repositório

```
trading-bot-hyperliquid/
├── main.py                 # Entry point — parse args, bootstrap all modules
├── start.bat               # Launcher interactivo (Paper / Testnet / Mainnet / Backtest / Audit)
├── quickstart.bat          # Paper mode directo + abre browser
├── config/
│   ├── settings.yaml       # Configurações (assets, risk, timeframes, dashboard)
│   └── .env.example        # Template para chaves API
├── src/
│   ├── core/
│   │   ├── engine.py        # TradingEngine — orquestra eventos, estratégias, execução
│   │   ├── portfolio.py     # Estado do portfolio (cash, positions, PnL)
│   │   ├── risk_manager.py  # Gestão de risco (circuit breaker, daily limits)
│   │   └── execution.py     # ExecutionEngine (paper/live) + simulação de fills
│   ├── strategies/
│   │   ├── trend_follow.py  # Estratégia de tendência (6 critérios de confluência)
│   │   ├── mean_reversion.py # Estratégia de mean reversion (funding + OI + VP)
│   │   ├── base.py          # MarketEvent, Signal, ExitSignal, Position, Strategy
│   │   └── indicators.py    # EMA, RSI, MACD, ATR, VWAP
│   ├── exchanges/
│   │   ├── hyperliquid_ws.py   # WebSocket client Hyperliquid
│   │   ├── hyperliquid_api.py  # REST API (orders, positions, fills)
│   │   ├── hyperliquid_base.py # Modelos de dados (HlPriceTick, HlTrade, HlAssetCtx)
│   │   └── binance_api.py      # Dados de mercado Binance (fallback)
│   ├── data/
│   │   ├── candle_builder.py   # Constrói candles OHLCV + OI + funding + buy/sell volume
│   │   ├── data_bus.py         # Pub/sub assíncrono entre módulos
│   │   └── hyperliquid_candles.py # REST candles REST
│   ├── dashboard/
│   │   ├── web.py           # Flask + Socket.IO server
│   │   └── index.html       # UI real-time (cypherpunk dark)
│   ├── database/
│   │   └── database.py      # SQLite (signals, trades, portfolio snapshots, funding)
│   └── utils/
│       ├── config_manager.py   # YAML + overrides + dot-notation
│       ├── logging_config.py   # Pretty-logs com rotação
│       ├── logger.py           # Logger customizado
│       └── helpers.py          # safe_float, safe_divide, utc_now
├── tests/
│   ├── test_security.py     # Security audit scanner
│   ├── test_strategies.py   # Unit tests para estratégias
│   └── conftest.py          # Fixtures pytest
├── data/
│   └── live/
│       └── bot.db            # SQLite runtime (auto-criado)
├── logs/
│   └── bot.log              # Logs com rotação (auto-criado)
└── README.md
```

---

## Requisitos

- Python 3.11+
- Windows (bat files) ou Linux/Mac (comandos equivalentes)

```bash
pip install -r requirements.txt
```

Copia `.env.example` para `.env` e configura as chaves da Hyperliquid (só necessário para modo live).

---

## Como correr

### Modo rápido (Paper Trading)

Duplo-clique em **`quickstart.bat`** — abre o browser automaticamente e inicia o bot em paper mode.

Ou manualmente:
```bash
cd trading-bot-hyperliquid
python main.py --mode paper
```

Dashboard: http://localhost:5000

Para parar: `Ctrl+C` na janela do terminal (graceful shutdown).

### Outros modos

```bash
# Paper trading (simulação)
python main.py --mode paper

# Live trading (REAL MONEY — requer .env configurado)
python main.py --mode live

# Backtest histórico
python main.py --backtest --from-date 2024-01-01 --to-date 2024-03-01

# Security audit
python main.py --audit
```

Ou usa **`start.bat`** para menu interactivo.

---

## Estratégias

### TrendFollow (Confluência 4/6)

Sinal de entrada quando **≥4 de 6** condições alinhadas:

| # | Condição LONG | Condição SHORT |
|---|---|---|
| 1 | Preço > EMA 20 | Preço < EMA 20 |
| 2 | EMA 20 > EMA 50 | EMA 20 < EMA 50 |
| 3 | RSI 14 > 50 | RSI 14 < 50 |
| 4 | MACD histograma > 0 | MACD histograma < 0 |
| 5 | Preço > VWAP | Preço < VWAP |
| 6 | Orderflow imbalance > 0.15 | Orderflow imbalance < -0.15 |

Orderflow: calculado a partir do **buy_volume − sell_volume** da candle de 15m.

### MeanReversion (Funding + OI + VP)

Sinal quando funding rate atinge extremos (>0.01% ou <-0.01%) com:
- OI elevado (crowded trade)
- Preço em suporte/resistência do Volume Profile
- Confiança ajustada pela força do sinal

---

## Arquitetura de dados

```
Hyperliquid WS ──▶ DataBus ──▶ CandleBuilder ──▶ DataBus ──▶ TradingEngine
                        │                              │
                        │                              ├──▶ TrendFollow
                        │                              ├──▶ MeanReversion
                        │                              └──▶ Portfolio / Risk / Executor
                        └──▶ Dashboard (Socket.IO)
```

- **DataBus**: pub/sub assíncrono — `price:BTC`, `ctx:ETH`, `candle_complete:60:BTC`, etc.
- **CandleBuilder**: acumula ticks em candles OHLCV com OI, funding, buy/sell volume
- **TradingEngine**: recebe `candle_complete`, constrói `MarketEvent`, alimenta estratégias

---

## Notas de segurança

- O bot corre em **paper trading por defeito** — sem dinheiro real
- Modo live requer `HYPERLIQUID_API_KEY` e `HYPERLIQUID_API_SECRET` no `.env`
- `.env` está no `.gitignore` — nunca é commitado
- `start.bat` pede confirmação escrita `MAINNET` antes de arrancar em live

---

## Changelog v2.0.0

- Arquitetura modular com DataBus pub/sub
- WebSocket Hyperliquid com auto-reconnect
- CandleBuilder multi-timeframe (1m/5m/15m/1h) com OI + funding
- Duas estratégias de confluência com orderflow
- Dashboard real-time (Flask + Socket.IO)
- SQLite persistence + crash recovery
- Paper trader com simulação realista de fills
- Security audit integrado (0 findings)

