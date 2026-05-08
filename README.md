# Hyperliquid Premium Trading Bot v3.1

Bot de trading automatizado para Hyperliquid com arquitetura modular, dashboard real-time, 5 estratégias de confluência, gestão de risco avançada, auto-recovery, e execução em paper/testnet/live.

---

## Estado Actual

| Componente | Estado | Detalhe |
|---|---|---|
| Bot paper trading | ✅ Funcional | `python main.py --mode paper` |
| Dashboard web v2 | ✅ Funcional | http://localhost:5000 |
| WebSocket Hyperliquid | ✅ Ligado | tickers + allMids + assetCtxs + l2Book + trades |
| CandleBuilder | ✅ 1m / 5m / 15m / 1h | Com OI, funding, buy/sell volume |
| Orderbook L2 + métricas | ✅ | OIR, wall detection, depth quality, slippage, fill ratio |
| Funding cross-exchange | ✅ | Agregado Binance + Bybit + OKX + Coinalyze |
| **ADX Regime Filter** | ✅ **FASE 2.1** | Trend/Range/Neutral — ajusta confiança por estratégia |
| **SmartMoneyFlow v2** | ✅ **FASE 2.2** | OIR >0.6, wall detection 0.5%, RSI 40-70, confluência 6/9 |
| **FundingExtreme v2** | ✅ **FASE 2.3** | Percentis dinâmicos, cross-exchange confirm, OI decreasing, predicted primary |
| **Cooldown Inteligente** | ✅ **FASE 2.4** | Per-(strategy,symbol), doubling after loss, auto-reset on funding/ADX |
| **FundingArbitrage** | ✅ **FASE 3.1** | Long funding-negative, short funding-positive, hedge 1:1 |
| **VWAPDeviation** | ✅ **FASE 3.2** | Z-score >2.5σ do VWAP(1h) + volume surge → mean reversion |
| **LiquidationCatcher** | ✅ **FASE 3.3** | Fade $50M+ liquidation cascades, stop 1% ATR, TP 2R |
| **Portfolio correlation limit** | ✅ **FASE 4.1** | Max 60% do book na mesma direção |
| **Sector exposure cap** | ✅ **FASE 4.2** | Max 30% do capital em crypto |
| **Daily drawdown circuit** | ✅ **FASE 4.3** | >5% drawdown diário → para entradas, fecha posições |
| **Kelly Criterion sizing** | ✅ **FASE 4.4** | Win rate + R/R histórico → ajusta size (Half-Kelly) |
| **Auto-log monitoring** | ✅ **FASE 5.1** | Heartbeat 15min — alerta em erros novos |
| **Auto-restart on crash** | ✅ **FASE 5.2** | Crash → restart em paper mode + notificação |
| **Dashboard drill-down** | ✅ **FASE 5.3** | Click em estratégia → stats, win rate, PnL, signals |
| Crash recovery + graceful shutdown | ✅ | SQLite persistence + signal handlers |
| Paper trader | ✅ | Simula execução com slippage realista |

---

## Estrutura do Repositório

```
trading-bot-hyperliquid/
├── main.py                    # Entry point — parse args, bootstrap all modules
├── start.bat                  # Launcher interactivo (Paper/Testnet/Mainnet/Backtest/Audit)
├── quickstart.bat             # Paper mode directo + abre browser
├── config/
│   ├── settings.yaml          # Configurações completas (assets, risk, strategies, dashboard)
│   └── .env.example           # Template para chaves API
├── src/
│   ├── core/
│   │   ├── engine.py          # TradingEngine — orquestra eventos, estratégias, execução
│   │   ├── portfolio.py       # Estado do portfolio (cash, positions, PnL unrealized)
│   │   ├── risk_manager.py    # Gestão de risco (max positions, size limits, correlation, drawdown circuit)
│   │   ├── execution.py       # ExecutionEngine (paper/live) + simulação de fills + slippage
│   │   └── kelly_sizer.py     # Kelly Criterion position sizing (Task 4.4)
│   ├── strategies/
│   │   ├── base.py            # MarketEvent, Signal, ExitSignal, Position, Strategy ABC
│   │   ├── indicators.py      # EMA, RSI, MACD, ATR, VWAP, ADX, Volume Profile (pure Python)
│   │   ├── trend_follow.py    # SmartMoneyFlow — trend following com microestrutura
│   │   ├── mean_reversion.py  # FundingExtreme — contrarian com funding + OI dinâmico
│   │   ├── funding_arbitrage.py   # FundingArbitrage — market-neutral funding spread
│   │   ├── vwap_deviation.py      # VWAPDeviation — mean reversion ao VWAP(1h)
│   │   └── liquidation_catcher.py # LiquidationCatcher — fade liquidation cascades
│   ├── exchanges/
│   │   ├── hyperliquid_ws.py    # WebSocket client (allMids, assetCtx, l2Book, trades)
│   │   ├── hyperliquid_rest.py  # REST API (orders, positions, fills, l2Book fallback)
│   │   ├── hyperliquid_base.py  # Modelos: HlPriceTick, HlTrade, HlAssetCtx, HlOrderbook
│   │   ├── binance_api.py       # Dados de mercado Binance (fallback + funding agregado)
│   │   └── funding_aggregator.py # Agrega funding + OI de múltiplas exchanges
│   ├── data/
│   │   ├── candle_builder.py     # Constrói candles OHLCV + OI + funding + buy/sell volume
│   │   ├── data_bus.py           # Pub/sub assíncrono entre módulos
│   │   ├── orderbook_metrics.py  # OIR, wall detection, depth quality, slippage estimation
│   │   └── database.py           # SQLite (signals, trades, portfolio snapshots, funding history)
│   ├── dashboard/
│   │   ├── web.py                # Flask + Socket.IO server (drill-down endpoint Task 5.3)
│   │   └── index.html            # UI real-time (cypherpunk dark theme)
│   └── utils/
│       ├── config.py             # YAML + overrides + dot-notation
│       ├── logger.py             # Pretty-logs com rotação
│       ├── helpers.py            # safe_float, safe_divide, utc_now, utc_timestamp_ms
│       ├── log_monitor.py        # Auto-log monitoring (Task 5.1)
│       └── crash_recovery.py     # Auto-restart on crash (Task 5.2)
├── tests/
│   ├── test_tasks_1_4_2_1_2_2.py   # ADX, regime weights, slippage, SmartMoneyFlow filters
│   ├── test_task_2_3.py            # Dynamic thresholds, cross-exchange, OI filter
│   ├── test_task_2_4.py            # Cooldown state machine, doubling, auto-reset
│   ├── test_task_3_1.py            # FundingArbitrage pair selection, spread, exit
│   ├── test_task_3_2.py            # VWAPDeviation Z-score, entry, ADX filter, exit
│   ├── test_task_3_3.py            # LiquidationCatcher entry, filters, 2R exit, max hold
│   ├── test_fase_4.py              # Portfolio governance: drawdown, exposure, Kelly
│   ├── test_funding.py             # Funding aggregator unit tests
│   ├── test_sio_client.py          # Socket.IO client test
│   ├── test_socketio.py            # Socket.IO connection test
│   ├── test_ws.py                  # WebSocket connection test
│   ├── test_ws_coin.py             # WebSocket coin data test
│   └── test_ws_ctx.py              # WebSocket context data test
├── data/live/
│   └── bot.db                    # SQLite runtime (auto-criado)
├── logs/
│   └── bot.log                   # Logs com rotação (auto-criado)
│   └── crashes.log               # Crash history (Task 5.2)
├── run_with_recovery.py          # Entry point with crash recovery wrapper (Task 5.2)
└── README.md
```

---

## Requisitos

- **Python 3.11+** (testado em 3.14)
- **Windows** (bat files incluídos) ou **Linux/macOS** (comandos equivalentes abaixo)

```bash
pip install -r requirements.txt
```

Copia `config/.env.example` para `config/.env` e configura as chaves da Hyperliquid (só necessário para modo live).

---

## Setup no macOS

### 1. Clonar o repositório

```bash
git clone https://github.com/braindeadpt/Trading-bot-hyperliquid_V1.git
cd Trading-bot-hyperliquid_V1
```

### 2. Criar ambiente virtual (recomendado)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Instalar dependências

```bash
pip install -r requirements.txt
```

### 4. Configurar chaves API (modo live/testnet)

```bash
cp config/.env.example config/.env
# Edita config/.env com as tuas chaves da Hyperliquid
```

### 5. Criar diretórios de runtime

```bash
mkdir -p data/live logs
```

### 6. Correr em Paper Trading

```bash
python main.py --mode paper
```

Dashboard: http://localhost:5000  
Para parar: `Ctrl+C` no terminal (graceful shutdown).

### 7. Correr com auto-recovery (recomendado para 24/7)

```bash
python run_with_recovery.py --mode paper
```

---

## Como Correr (Windows / Geral)

### Modo Rápido (Paper Trading)

Duplo-clique em **`quickstart.bat`** — abre o browser automaticamente e inicia o bot em paper mode.

Ou manualmente:
```bash
cd trading-bot-hyperliquid
python main.py --mode paper
```

Dashboard: http://localhost:5000  
Para parar: `Ctrl+C` na janela do terminal (graceful shutdown).

### Outros Modos

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

## Features Premium

### 1. Dashboard Real-Time v2
- **Live Data Stream** — preços, funding, OI, volume, imbalance, spread, OIR, depth quality
- **Candle Watch** — OHLCV + buy% + VWAP por timeframe (1m, 5m, 15m, 1h)
- **Engine Monitor** — ticks/sec, total ticks, memória, último erro, eventos recentes
- **Strategies Detail** — parâmetros, último sinal, confiança, status, sinais hoje (5 estratégias)
- **Signal Stream** — últimos 20 sinais com side colorido + status (pending/approved/rejected/executed)
- **Decision Log** — risk approvals/rejections + executions + exits + cooldown resets
- **Portfolio** — capital, PnL diário, drawdown, trades, positions abertas
- **Open Positions** — posições com PnL%, entry, current, estratégia, stop, TP
- **Live Logs** — últimas 50 linhas do ficheiro de log

### 2. Orderbook L2 Hyperliquid
Métricas de microestrutura em tempo real:
- **OIR** (Orderbook Imbalance Ratio) — (bid_vol − ask_vol) / total_vol nas primeiras 10 camadas
- **Spread analysis** — spread absoluto + percentual + weighted mid price
- **Depth quality** — bid_depth / total_depth (0-1), bid_ask_ratio
- **Wall detection** — maior parede de bids e asks (suporte/resistência)
- **Slippage estimation** — estimativa de slippage para ordens de mercado
- **Fill ratio gate** — rejeita se book não cobre size mínimo (0.8 default)

### 3. Funding Cross-Exchange
Agregador de funding + OI de múltiplas exchanges (Binance, Bybit, OKX, Coinalyze):
- `funding_avg` — média simples
- `funding_weighted` — média ponderada por OI
- `predicted_funding_avg` — predicted funding médio
- `oi_total_aggregated` — OI total cross-exchange
- `oi_exchange_count` — número de exchanges com dados

### 4. ADX Regime Filter (FASE 2.1)
Calcula ADX(14) a partir de candles 15m e classifica o regime:
- **ADX > 25** → Trend → aumenta confiança da TrendFollow (1.3x), diminui MeanReversion (0.7x)
- **ADX < 20** → Range → aumenta confiança da MeanReversion (1.3x), diminui TrendFollow (0.7x)
- **ADX 20-25** → Neutral → sem ajuste

Aplicado no engine antes da resolução de conflitos entre sinais.

### 5. Cooldown Inteligente (FASE 2.4)
Per-(strategy, symbol) state machine:
- **Base**: 60 min após entrada
- **After loss**: duplica (2x), max 240 min, capped
- **After win**: reseta para base
- **Auto-reset**: funding normaliza (<0.2%) ou ADX regime muda

---

## Estratégias (5)

### 1. SmartMoneyFlow (TrendFollow) — FASE 2.2

Sinal de entrada quando **≥6 de 9** condições alinhadas:

| # | Condição LONG | Condição SHORT |
|---|---|---|
| 1 | Preço > EMA 20 | Preço < EMA 20 |
| 2 | EMA 20 > EMA 50 | EMA 20 < EMA 50 |
| 3 | RSI 14 > 50 | RSI 14 < 50 |
| 4 | MACD histograma > 0 | MACD histograma < 0 |
| 5 | Preço > VWAP | Preço < VWAP |
| 6 | Orderflow imbalance > 0.15 | Orderflow imbalance < -0.15 |
| 7 | **OIR > 0.6** (book bids dominam) | **OIR < -0.6** (book asks dominam) |
| 8 | **RSI 40-70** (não overbought) | **RSI 30-60** (não oversold) |
| 9 | **Sem ask wall a 0.5%** (resistência) | **Sem bid wall a 0.5%** (suporte) |

Confidence: 6/9 → 0.50, 7/9 → 0.75, 8/9 → 1.00

### 2. FundingExtreme (MeanReversion) — FASE 2.3

Contrarian em funding extremo com filtros dinâmicos:
- **Thresholds dinâmicos**: rolling p90/p70 por asset (lookback 90 períodos), sanity caps 0.1%-2.0%
- **Cross-exchange confirmation**: rejeita se HL funding desvia >0.3% da média ou sinais opostos
- **OI decreasing**: só entra se OI_delta < 0 (crowd já está a sair)
- **Predicted funding**: sinal primário, fallback para current funding

### 3. FundingArbitrage (FASE 3.1)

Market-neutral funding spread:
- **Long**: ativo com funding mais negativo (shorts pagam longs)
- **Short**: ativo com funding mais positivo (longs pagam shorts)
- **Entry**: spread > 1.2%, cada leg |funding| > 0.5%, OI estável
- **Exit**: funding reverte para < 0.2% ou max hold 8h

### 4. VWAPDeviation (FASE 3.2)

Mean reversion ao VWAP(1h):
- **Entry**: |Z-score| > 2.5σ do VWAP(1h) + volume > 150% média 24h
- **Z > 2.5** → SHORT (preço muito acima, reverte para baixo)
- **Z < -2.5** → LONG (preço muito abaixo, reverte para cima)
- **Filtros**: ADX < 25 (só não-trending), OIR confirma, funding não extremo oposto
- **Exit**: cruza VWAP (|Z| < 0.5) ou max hold 4h

### 5. LiquidationCatcher (FASE 3.3)

Fade liquidation cascades:
- **Entry**: $50M+ liquidado numa direção em <5min
- **Longs liquidados** (price drop) → **go LONG**
- **Shorts liquidados** (price pump) → **go SHORT**
- **Filtros**: count > 10, OI decreasing, ADX < 40
- **Stop**: 1% ATR (tight)
- **Take Profit**: 2R (2x risk)
- **Exit**: 2R hit, VWAP reversion, ou max hold 30min
- **Size**: 0.5-1% capital (small — catching a knife)
- **Throttle**: 2h cooldown entre catches

---

## Arquitetura de Dados

```
Hyperliquid WS ──▶ DataBus ──▶ CandleBuilder ──▶ DataBus ──▶ TradingEngine
                        │                              │
                        │                              ├──▶ SmartMoneyFlow
                        │                              ├──▶ FundingExtreme
                        │                              ├──▶ FundingArbitrage
                        │                              ├──▶ VWAPDeviation
                        │                              ├──▶ LiquidationCatcher
                        │                              └──▶ Portfolio / Risk / Executor
                        └──▶ Dashboard (Socket.IO)
```

- **DataBus**: pub/sub assíncrono — `price:BTC`, `ctx:ETH`, `candle_complete:60:BTC`, `orderbook:BTC`, etc.
- **CandleBuilder**: acumula ticks em candles OHLCV com OI, funding, buy/sell volume
- **TradingEngine**: recebe `candle_complete`, constrói `MarketEvent` (com ADX, funding agregado, orderbook metrics, liquidation stats), alimenta estratégias, aplica regime weights, gating via RiskManager
- **RiskManager**: valida size, max positions, correlation, drawdown limits
- **ExecutionEngine**: executa approved signals (paper simula slippage realista)

---

## Testes

### Unit Tests (TODOS PASSING)

```bash
# Bateria completa (FASE 0–5)
python tests/test_tasks_1_4_2_1_2_2.py   # ADX, regime weights, slippage, SmartMoneyFlow
python tests/test_task_2_3.py            # Dynamic thresholds, cross-exchange, OI filter
python tests/test_task_2_4.py            # Cooldown state machine, doubling, auto-reset
python tests/test_task_3_1.py            # FundingArbitrage pair selection, spread, exit
python tests/test_task_3_2.py            # VWAPDeviation Z-score, entry, ADX filter, exit
python tests/test_task_3_3.py            # LiquidationCatcher entry, filters, 2R exit, max hold
python tests/test_fase_4.py              # Portfolio governance: drawdown, exposure, Kelly
```

Resultados: **37/37 testes passing** (100% pass rate)

### Integration Test

```bash
# Cria engine com todas as 5 estratégias e verifica inicialização
python -c "from main import main; print('OK')"
```

---

## Notas de Segurança

- O bot corre em **paper trading por defeito** — sem dinheiro real
- Modo live requer `HYPERLIQUID_API_KEY` e `HYPERLIQUID_API_SECRET` no `.env`
- `.env` está no `.gitignore` — nunca é commitado
- `start.bat` pede confirmação escrita `MAINNET` antes de arrancar em live
- **Nunca** executa prune automático de Docker em cron
- **Nunca** desactiva auth services ou firewall

---

## Changelog

### v3.1.0 — FASE 5 Completa (Observabilidade & Auto-recovery)
- **Auto-log monitoring** — heartbeat 15min, alerta em ERROR/CRITICAL/Traceback
- **Auto-restart on crash** — CrashRecovery wrap, restart em paper mode, crash log
- **Dashboard drill-down** — click em estratégia → stats, win rate, PnL, signal history
- **Strategy stats tracking** — per-strategy: signals, trades, win rate, PnL, avg PnL

### v3.0.0 — FASE 4 Completa (Portfolio Heat & Governance)
- **Portfolio correlation limit** — max 60% do book na mesma direção
- **Sector exposure cap** — max 30% do capital em crypto
- **Daily drawdown circuit** — >5% drawdown diário → para entradas, fecha posições
- **Kelly Criterion sizing** — Half-Kelly: win rate + R/R histórico → ajusta size

### v2.1.0 — FASE 3 Completa (Novas Estratégias)
- **FundingArbitrage** — market-neutral funding spread (long negative, short positive)
- **VWAPDeviation** — mean reversion ao VWAP(1h) com Z-score e volume surge
- **LiquidationCatcher** — fade $50M+ liquidation cascades, stop 1% ATR, TP 2R

### v2.0.0 — FASE 2 Completa (Sinais Inteligentes)
- **ADX Regime Filter** — trend/range/neutral, ajusta confiança por estratégia
- **SmartMoneyFlow v2** — OIR >0.6, wall detection 0.5%, RSI 40-70, confluência 6/9
- **FundingExtreme v2** — percentis dinâmicos rolling, cross-exchange confirm, OI decreasing
- **Cooldown Inteligente** — per-(strategy,symbol), doubling after loss, auto-reset

---

## Roadmap

| FASE | Tarefas | Estado |
|---|---|---|
| FASE 0 | Dashboard, fundações | ✅ |
| FASE 1 | Risk, Execution, ATR stops, Slippage | ✅ |
| FASE 2 | ADX, SmartMoneyFlow, FundingExtreme, Cooldown | ✅ |
| FASE 3 | FundingArbitrage, VWAPDeviation, LiquidationCatcher | ✅ |
| **FASE 4** | **Portfolio Heat & Governance** | **✅** |
| **FASE 5** | **Observability & Auto-recovery** | **✅** |

**TODAS AS FASES COMPLETAS! 🎉**

---

## Testes

### Unit Tests (TODOS PASSING — 37/37)

```bash
# Bateria completa (FASE 0–5)
python test_tasks_1_4_2_1_2_2.py   # ADX, regime weights, slippage, SmartMoneyFlow
python test_task_2_3.py            # Dynamic thresholds, cross-exchange, OI filter
python test_task_2_4.py            # Cooldown state machine, doubling, auto-reset
python test_task_3_1.py            # FundingArbitrage pair selection, spread, exit
python test_task_3_2.py            # VWAPDeviation Z-score, entry, ADX filter, exit
python test_task_3_3.py            # LiquidationCatcher entry, filters, 2R exit, max hold
python test_fase_4.py              # Portfolio governance: drawdown, exposure, Kelly
```

Resultados: **37/37 testes passing** (100% pass rate)

### Integration Test

```bash
# Cria engine com todas as 5 estratégias e verifica inicialização
python -c "from main import main; print('OK')"
```

### Audit

```bash
python main.py --audit
```
