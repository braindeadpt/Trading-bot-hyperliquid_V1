# Hyperliquid Premium Trading Bot

Bot de trading automatizado para Hyperliquid com arquitetura modular, dashboard real-time, e duas estratégias de confluência (TrendFollow + MeanReversion).

---

## Estado actual

| Componente | Estado |
|---|---|
| Bot paper trading | ✅ Funcional — `python main.py --mode paper` |
| Dashboard web v2 | ✅ Funcional — http://localhost:5000 |
| WebSocket Hyperliquid | ✅ Ligado (tickers + allMids + assetCtxs + **l2Book**) |
| CandleBuilder | ✅ 1m / 5m / 15m / 1h |
| Orderbook L2 + métricas | ✅ OIR, wall detection, depth quality, spread analysis |
| Funding cross-exchange | ✅ Agregado Binance + Bybit + OKX + Coinalyze |
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
│   │   ├── orderbook_metrics.py # OIR, wall detection, depth quality, slippage estimation
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

## Features Premium

### 1. Dashboard Real-Time v2
- **Live Data Stream** — preços, funding, OI, volume, imbalance, spread, OIR (Orderbook Imbalance Ratio), depth quality
- **Candle Watch** — OHLCV + buy% + VWAP por timeframe (1m, 5m, 15m, 1h)
- **Engine Monitor** — ticks/sec, total ticks, memória, último erro, eventos recentes
- **Strategies Detail** — parâmetros, último sinal, confiança, status, sinais hoje
- **Signal Stream** — últimos 20 sinais com side colorido + status (pending/approved/rejected/executed)
- **Decision Log** — risk approvals/rejections + executions + exits
- **Portfolio** — capital, PnL diário, drawdown, trades
- **Open Positions** — posições com PnL%, entry, current, estratégia
- **Live Logs** — últimas 50 linhas do ficheiro de log

### 2. Funding Cross-Exchange
Agregador de funding + OI de múltiplas exchanges (Binance, Bybit, OKX, Coinalyze):
- `funding_avg` — média simples
- `funding_weighted` — média ponderada por OI
- `predicted_funding_avg` — predicted funding médio
- `oi_total_aggregated` — OI total cross-exchange
- `oi_exchange_count` — número de exchanges com dados

### 3. Orderbook L2 Hyperliquid
Métricas de microestrutura em tempo real:
- **OIR** (Orderbook Imbalance Ratio) — (bid_vol - ask_vol) / total_vol nas primeiras 10 camadas
- **Spread analysis** — spread absoluto + percentual + weighted mid price
- **Depth quality** — bid_depth / total_depth (0-1), bid_ask_ratio
- **Wall detection** — maior parede de bids e asks
- **Slippage estimation** — estimativa de slippage para ordens de mercado

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

