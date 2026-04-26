# 🏗️ HYPERLIQUID BOT v2.0 — Arquitetura Completa
## Sistema Escalável — Design + Implementação

---

## 📐 ARQUITETURA

### Visão Geral

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HYPERLIQUID BOT v2.0                         │
│                    (Arquitetura em Camadas Clean)                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐            │
│  │   CLI/Rich   │  │  Flask API   │  │ System Tray  │  CAMADA UI │
│  │  Terminal    │  │   + HTML     │  │   (opt)      │            │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘            │
│         └───────────────────┼───────────────────┘                  │
│                             │ Event Bus (Pub/Sub)                  │
│  ┌──────────────────────────┴──────────────────────────┐           │
│  │              ORQUESTRAÇÃO (BotEngine)               │           │
│  │  StateMachine: IDLE → SCANNING → ANALYZING → ...    │           │
│  └──────────────────────────┬──────────────────────────┘           │
│                             │                                      │
│  ┌──────────────────────────┴──────────────────────────┐           │
│  │           SERVIÇOS (Single Instance — Container)      │           │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────────┐ │           │
│  │  │Strategy │ │  Risk   │ │  Order  │ │  Position  │ │           │
│  │  │ Engine  │ │ Manager │ │Executor │ │  Tracker   │ │           │
│  │  └────┬────┘ └────┬────┘ └────┬────┘ └─────┬──────┘ │           │
│  │       └───────────┴───────────┴────────────┘         │           │
│  └──────────────────────────┬──────────────────────────┘           │
│                             │                                      │
│  ┌──────────────────────────┴──────────────────────────┐           │
│  │              DATA (Abstração + Cache)                 │           │
│  │  ┌─────────┐ ┌─────────┐ ┌──────────────────────────┐ │           │
│  │  │Hyperliquid│ │  Cache  │ │      SQLite DB         │ │           │
│  │  │  Client │ │  (TTL)  │ │  (WAL mode, batch)     │ │           │
│  │  └─────────┘ └─────────┘ └──────────────────────────┘ │           │
│  └──────────────────────────────────────────────────────┘           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Princípios Arquiteturais

| Princípio | Implementação | Problema do Legado Resolvido |
|-----------|--------------|------------------------------|
| **Inversão de Dependências** | Container DI injeta serviços | Aggregator instanciado 3x |
| **Pub/Sub Desacoplado** | EventBus comunicação assíncrona | `app_state` global mutable |
| **Estado Finito** | StateMachine com transições validadas | Bot sem estado definido |
| **Resiliência** | Circuit breaker + retries + rate limit | APIs falham silenciosamente |
| **Cache Partilhado** | DataCache TTL único para todos | Requests HTTP duplicados |
| **Observabilidade** | Eventos tipados + logs estruturados | Debugging impossível |

---

## 📁 ESTRUTURA DE COMPONENTES

```
trading-bot-hyperliquid/
│
├── run.py                          ← 🔥 ENTRY POINT UNIFICADO
│   ├── modo web    → Flask + Tray
│   ├── modo cli    → Terminal Rich
│   └── modo headless → Só o bot
│
├── refactored/                     ← NOVA ARQUITETURA v2.0
│   ├── __init__.py
│   │
│   ├── core/                       ← 🧠 NÚCLEO
│   │   ├── event_bus.py            → Pub/Sub desacoplado
│   │   ├── container.py            → DI Container (singletons)
│   │   └── state_machine.py        → Máquina de estados
│   │
│   ├── api/                        ← 🌐 API EXTERNA
│   │   └── hyperliquid_client.py   → Cliente robusto (circuit breaker)
│   │
│   ├── data/                       ← 📊 DADOS
│   │   ├── cache.py                → Cache em memória com TTL
│   │   ├── aggregator.py           → Agregador único (shared cache)
│   │   └── database.py             → SQLite com WAL + batch inserts
│   │
│   ├── strategy/                   → 🎯 ESTRATÉGIA
│   │   ├── base.py                 → Classe base abstrata (Signal)
│   │   └── ghost.py                → Ghost Method (PF 2.50 validado)
│   │
│   ├── execution/                  → ⚡ EXECUÇÃO
│   │   ├── trader.py               → PaperTrader + real trading
│   │   └── risk.py                 → Risk manager adaptativo
│   │
│   ├── web/                        → 🖥️ WEB
│   │   └── app.py                  → Flask API (dados via EventBus)
│   │
│   ├── cli/                        → 💻 TERMINAL
│   │   └── terminal.py             → Dashboard Rich com emojis
│   │
│   └── utils/                      → 🛠️ UTILITÁRIOS
│       └── config.py               → Loader YAML/JSON + defaults
│
├── verify_refactored.py            ← ✅ Testes de integridade
│
└── config/settings.yaml            ← Configuração do bot
```

---

## 🌊 FLUXO DE DADOS

### Ciclo Principal (BotEngine)

```
┌─────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────┐
│  APIs   │────▶│ DataAggregator│────▶│   Strategy  │────▶│ Trader  │
│Hyperliquid│    │  (com Cache)  │     │  (analyze)  │     │(execute)│
└─────────┘     └─────────────┘     └──────┬──────┘     └───┬─────┘
                                            │                  │
                                            ▼                  ▼
                                    ┌─────────────┐     ┌───────────┐
                                    │   Signal    │     │  Database │
                                    │ (LONG/SHORT)│     │ (persiste)│
                                    └─────────────┘     └───────────┘
                                              │
                                              ▼
                                    ┌───────────────────┐
                                    │    EventBus       │
                                    │ (market.data)     │
                                    │ (trade.entered)   │
                                    │ (trade.exited)    │
                                    └─────────┬─────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    │                         │                         │
                    ▼                         ▼                         ▼
            ┌───────────┐           ┌───────────┐           ┌───────────┐
            │ Dashboard │           │   CLI     │           │   Logs    │
            │  (Web)    │           │ (Rich)    │           │  (File)   │
            └───────────┘           └───────────┘           └───────────┘
```

### Vantagens do Novo Fluxo

| Aspecto | Legado v1.0 | Novo v2.0 |
|---------|-------------|-----------|
| Instâncias Aggregator | 3 (uma por componente) | 1 (partilhada) |
| Comunicação | `app_state` global mutable | EventBus pub/sub |
| Estado do bot | Booleano (`running`) | StateMachine FSM |
| Fetch de dados | Polling com sleep | Loop controlado + cache |
| Updates dashboard | Polling direto a APIs | Recebe eventos do bus |

---

## 🔌 DESIGN DE API (Flask)

### Endpoints REST

| Método | Endpoint | Descrição | Auth |
|--------|----------|-----------|------|
| GET | `/api/status` | Estado do bot + mercado | Não |
| GET | `/api/market/<asset>` | Dados de mercado para asset | Não |
| GET | `/api/trades` | Trades recentes (eventos) | Não |
| GET | `/api/db/trades` | Trades da base de dados | Não |
| GET | `/api/db/stats` | Estatísticas gerais | Não |
| POST | `/api/bot/start` | Iniciar bot | Não |
| POST | `/api/bot/stop` | Parar bot | Não |
| POST | `/api/bot/emergency` | Fechar posição emergência | Não |

### Exemplo de Resposta

```json
GET /api/status
{
  "running": true,
  "state": "POSITION",
  "last_update": "2026-04-26T14:30:00",
  "assets": {
    "BTC": {
      "price": 77320.50,
      "oi": 15000000000,
      "funding": 0.0001,
      "volume": 500000000
    }
  }
}
```

---

## 🗄️ ESQUEMA DE BASE DE DADOS

### Tabelas

```sql
-- Candles OHLCV (com índice composto)
CREATE TABLE candles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    UNIQUE(symbol, interval, timestamp)
);
CREATE INDEX idx_candles_sym_int_ts ON candles(symbol, interval, timestamp);

-- Open Interest histórico
CREATE TABLE open_interest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    oi_usd REAL NOT NULL,
    UNIQUE(symbol, timestamp)
);

-- Funding rates
CREATE TABLE funding_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    funding_rate REAL NOT NULL,
    UNIQUE(symbol, timestamp)
);

-- Trades (paper + real)
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,           -- long / short
    entry_price REAL NOT NULL,
    exit_price REAL,
    entry_time INTEGER NOT NULL,
    exit_time INTEGER,
    size_usd REAL NOT NULL,
    leverage REAL DEFAULT 1,
    pnl_usd REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    is_backtest INTEGER DEFAULT 0,
    strategy_params TEXT             -- JSON
);

-- Sinais (todos, executados ou filtrados)
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    asset TEXT NOT NULL,
    signal_type TEXT NOT NULL,         -- LONG / SHORT / EXIT
    confidence REAL DEFAULT 1.0,
    executed BOOLEAN DEFAULT FALSE,
    execution_time INTEGER,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    reason TEXT,
    market_regime TEXT
);

-- Performance diária (para relatórios)
CREATE TABLE performance_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    asset TEXT NOT NULL,
    total_trades INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    win_rate REAL,
    profit_factor REAL,
    total_pnl REAL,
    max_drawdown REAL,
    UNIQUE(date, asset)
);
```

### Configuração SQLite

```python
PRAGMA journal_mode=WAL      # Escrita paralela (não em :memory:)
PRAGMA busy_timeout=5000     # Espera por locks
PRAGMA synchronous=NORMAL    # Balance performance/durability
```

---

## 💾 ESTRATÉGIA DE CACHE

### DataCache (Memória)

```
┌────────────────────────────────────────┐
│           DataCache (Singleton)        │
│  ┌─────────┐  ┌─────────┐  ┌──────┐  │
│  │ market  │  │ candles │  │ meta │  │
│  │ :BTC    │  │ :15m    │  │ info │  │
│  │ TTL: 10s│  │ TTL: 60s│  │TTL:5m│  │
│  └─────────┘  └─────────┘  └──────┘  │
│                                        │
│  Stats: hits=45, misses=3, rate=93%   │
└────────────────────────────────────────┘
```

### Política de TTL

| Tipo de Dado | TTL | Razão |
|--------------|-----|-------|
| Preço de mercado | 10s | Muda rapidamente |
| Candles | 60s | Dados históricos estáveis |
| Meta info (assets) | 300s | Muda raramente |
| OI / Funding | 30s | Intervalo de polling |

### Cache Key Convention

```python
f"market:{asset}:{timestamp_bucket_10s}"
f"candles:{asset}:{interval}:{limit}"
f"meta:universe"
```

---

## 🔧 CÓDIGO DE IMPLEMENTAÇÃO

### Entry Point (run.py)

```python
python3 run.py web      # Flask + System Tray
python3 run.py cli      # Terminal Rich
python3 run.py headless # Só o bot
```

### Uso Programático

```python
from refactored.utils.config import load_config
from refactored.core.event_bus import EventBus
from refactored.core.container import ServiceContainer

# 1. Carregar config
config = load_config('config/settings.yaml')

# 2. Criar event bus + container
event_bus = EventBus()
container = ServiceContainer(config, event_bus=event_bus).boot()

# 3. Aceder serviços
db = container.database
agg = container.aggregator
strategy = container.strategy
trader = container.trader

# 4. Subscrever a eventos
event_bus.subscribe('trade.exited', lambda e: print(f"Trade fechado: {e.payload}"))

# 5. Buscar dados
data = agg.fetch_market_data("BTC")
print(f"BTC: ${data.mark_price:,.2f}")

# 6. Analisar
signal = strategy.analyze({'price': 50000, 'oi_total': 1e9, ...}, 50000)
if signal:
    print(f"SINAL: {signal.type} @ ${signal.entry_price}")
```

### Extender a Estratégia

```python
from refactored.strategy.base import BaseStrategy, Signal

class MinhaEstrategia(BaseStrategy):
    def analyze(self, market_data, price):
        # ... minha lógica ...
        return Signal("LONG", confidence=0.9, entry_price=price)

# Registar no container
container.register('strategy', lambda cfg, ctn: MinhaEstrategia(cfg, ctn.event_bus))
```

---

## ✅ VERIFICAÇÃO

```bash
cd trading-bot-hyperliquid
python3 verify_refactored.py
```

**Resultado esperado:** 15/15 testes PASS

---

## 📊 COMPARAÇÃO: Legado vs Refatorado

| Métrica | v1.0 (Legado) | v2.0 (Refatorado) | Impacto |
|---------|---------------|-------------------|---------|
| Linhas de código | ~4,500 | ~3,200 (-29%) | Menos complexidade |
| Entry points | 3 conflitantes | 1 unificado | Menos confusão |
| Instâncias Aggregator | 3 | 1 | -67% requests HTTP |
| Estado global | `app_state` dict | EventBus pub/sub | Testável |
| Cache | 3 caches separados | 1 cache partilhado | Consistente |
| Tratamento de erros | Inconsistente | Circuit breaker | Resiliente |
| Testabilidade | Fraca (acoplado) | Alta (DI) | Manutenível |

---

## 🚀 PRÓXIMOS PASSOS

1. **Testar com dados reais**: `python3 run.py headless` por 1 hora
2. **Validar preços**: Confirmar que BTC/ETH têm valores corretos
3. **Conectar dashboard**: `python3 run.py web` + abrir browser
4. **Paper trading**: Deixar correr 24h, verificar trades na DB
5. **Migrar config**: Copiar `config/settings.yaml` para a nova estrutura

---

*Documento gerado em: 2026-04-26*
*Ficheiros criados: 15 Python modules + 1 entry point + 1 test suite*
