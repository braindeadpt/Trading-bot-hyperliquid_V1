# 🏗️ ANÁLISE DE ARQUITETURA — Hyperliquid Trading Bot
## Relatório de Refatoração v1.0

---

## 📋 RESUMO DA ARQUITETURA ATUAL

### Padrão Arquitetural
O projeto segue uma arquitetura **procedural com classes**, não uma arquitetura em camadas verdadeira. Há separação conceitual de responsabilidades, mas com acoplamento forte e dependências diretas entre módulos.

### Componentes Principais

```
┌────────────────────────────────────────────────────────────┐
│  ENTRY POINTS (3 alternativas conflitantes)                │
│  ├── src/main.py           (CLI + Dashboard Web)           │
│  ├── app_flask.py          (Flask + System Tray)           │
│  └── app_desktop.py        (Tkinter + WebView + Tray)      │
├────────────────────────────────────────────────────────────┤
│  ORQUESTRAÇÃO                                              │
│  ├── bot_engine.py         (Motor principal, estado global)│
│  ├── src/paper_trading.py  (Execução + Monitorização)      │
├────────────────────────────────────────────────────────────┤
│  DOMÍNIO                                                   │
│  ├── src/strategy.py       (Regras de entrada/saída)       │
│  ├── src/risk_manager.py   (Gestão de risco)               │
│  ├── src/volume_profile.py (Filtro de qualidade)           │
│  ├── src/exchange_client.py (Validação + Simulação)        │
├────────────────────────────────────────────────────────────┤
│  DADOS                                                     │
│  ├── src/data_aggregator.py (Fetch APIs + Cache)           │
│  ├── src/database.py       (SQLite + WAL)                  │
│  ├── src/backtest.py       (Backtest CSV)                  │
│  └── src/backtest_db.py    (Backtest SQLite)               │
├────────────────────────────────────────────────────────────┤
│  APRESENTAÇÃO                                              │
│  ├── src/dashboard_web.py  (Flask + HTML inline)           │
│  ├── dashboard.html        (Dashboard Cypherpunk)          │
│  └── src/performance_tracker.py (Relatórios)               │
└────────────────────────────────────────────────────────────┘
```

### Fluxo de Dados Atual

```
APIs Exchanges → DataAggregator → PaperTrader → BotDatabase
                                      ↓
                                WebDashboard (polling direto)
                                      ↓
                                dashboard.html (JS fetch)
```

**Problema crítico**: O `DataAggregator` é instanciado **3 vezes** (main, dashboard, bot_engine), cada um com o seu cache separado, causando:
- Rate limit desnecessários
- Dados inconsistentes entre componentes
- Consumo excessivo de memória

---

## 🚨 PROBLEMAS ESTRUTURAIS

### 1. ESTADO GLOBAL (CRÍTICO)

**Ficheiro**: `bot_engine.py`

```python
app_state = {
    "bot_running": False,
    "trader": None,
    "aggregator": None,
    # ... 15+ chaves
}
app_state_lock = threading.Lock()
```

**Impacto**:
- Qualquer módulo pode mutar o estado de qualquer outro
- Race conditions difíceis de debugar
- Testes unitários impossíveis de isolar
- O `app_state` é importado diretamente por `app_flask.py` e `app_desktop.py`

**Ocorrências**: ~50 referências diretas a `app_state` em 3 ficheiros

---

### 2. TRÊS ENTRY POINTS CONFLITANTES

| Entry Point | Tecnologia | Estado |
|-------------|-----------|--------|
| `src/main.py` | CLI + Flask | Funcional mas obsoleto |
| `app_flask.py` | Flask + Tray | **Recomendado atualmente** |
| `app_desktop.py` | Tkinter + WebView | Quebrado (webview pesado) |

**Problema**: Cada um inicializa o bot de forma diferente:
- `main.py` → cria `PaperTrader` diretamente
- `app_flask.py` → chama `start_bot_engine(config)`
- `app_desktop.py` → também chama `start_bot_engine(config)`

**Resultado**: Comportamento inconsistente, bugs de inicialização, config carregada múltiplas vezes.

---

### 3. ACOPLOMENTO CIRCULAR / DEPENDÊNCIAS DIRETAS

```
dashboard_web.py → imports → data_aggregator.py
    ↓                                ↓
render_template_string ←────── strategy.py
    ↓
BotDatabase ←──────────────────────┘
```

O `dashboard_web.py` instancia `DataAggregator` e `MomentumStrategy` diretamente, violando o princípio da inversão de dependências.

---

### 4. DUPLICAÇÃO MASSIVA DE CÓDIGO

#### A. Monitorização (3 implementações idênticas)
- `bot_engine.py::_run()` — loop de monitorização
- `app_flask.py::monitor_loop()` — atualização de estado global
- `app_desktop.py::_monitor_loop()` — cópia literal do de cima

Diferença entre `app_flask.py` e `app_desktop.py` monitor loops: **apenas 2 linhas**.

#### B. System Tray (2 implementações idênticas)
- `app_flask.py::setup_tray()` — cria ícone, menu, callbacks
- `app_desktop.py::_setup_tray()` — **código 95% igual**

#### C. Carregamento de Config
- `src/utils.py::load_config()` — função útil
- Mas é chamada em **7 ficheiros diferentes**, muitas vezes sem cache

#### D. Formatação de Dados
```python
# Em data_aggregator.py
oi_billions = oi_usd / 1_000_000_000

# Em dashboard_web.py  
def _format_oi(value):
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    # ...

# Em dashboard.html
// JavaScript faz a mesma coisa de novo
```

#### E. Lógica de Trading (2 motores)
- `src/backtest.py` — motor de backtest com CSV
- `src/backtest_db.py` — motor de backtest com SQLite
- Ambos implementam a **mesma lógica de sinais** copiada da `MomentumStrategy`

---

### 5. GARGALOS DE PERFORMANCE

#### A. Polling Ineficiente (CRÍTICO)

```python
# bot_engine.py::_run()
while self.running:
    # Buscar preço a cada 5 segundos
    if current_time - last_price_time >= 5:
        for asset in self.assets:  # HTTP request!
            price = aggregator.get_cached_price(asset)
    
    # Buscar dados completos a cada 30 segundos  
    if current_time - last_oi_time >= 30:
        for asset in self.assets:  # 3+ HTTP requests!
            data = aggregator.fetch_all_data(asset)
```

**Problema**: Sem batching, sem backoff exponencial, sem circuit breaker. Se a API falhar, continua a tentar a cada 5 segundos.

#### B. N+1 Queries Resolvidas — Mas Ainda Há Problemas

```python
# database.py::get_candles_for_backtest() — OTIMIZADO ✅
# Faz 2 queries para OI + funding em vez de N+2

# MAS em get_candles():
candles = self.get_candles(symbol, interval)  # Query 1
# Depois para CADA candle, busca OI e funding separadamente
# Ainda pode ser otimizado com JOIN
```

#### C. Locks de Threading Ineficientes

```python
# app_flask.py::monitor_loop()
while True:
    with app_state_lock:  # Lock a cada 2 segundos para TUDO
        app_state["capital"] = trader.capital
        app_state["position"] = {...}  # 10+ atribuições
    time.sleep(2)
```

**Problema**: Lock contínuo bloqueia todas as outras threads. Deveria usar `queue` ou `asyncio`.

#### D. Dashboard Web — Dual Instanciação

```python
# dashboard_web.py
self.aggregator = DataAggregator(config)  # Instância 2
self.strategy = MomentumStrategy(config)  # Instância 2

# app_flask.py
engine = BotEngine(config)  # Tem o seu DataAggregator (Instância 3)
```

**Resultado**: 3x mais requests HTTP do que necessário.

---

### 6. RISCOS DE MANUTENIBILIDADE

#### A. 30+ TODOs Escondidos

```python
# exchange_client.py
raise NotImplementedError("Execução real requer wallet e assinatura")

# app_flask.py
return jsonify({"success": False, "message": "Ainda não implementado"})

# paper_trading.py
# TODO: Implementar trailing stop real
# TODO: Implementar partial exits
# TODO: Implementar re-entry logic
```

**Problema**: 12 endpoints da API retornam "Ainda não implementado".

#### B. HTML/CSS/JS Inline — Impossível de Manter

```python
# dashboard_web.py
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt">
<head>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { background: #0a0e27; ... }
    /* 500+ linhas de CSS inline */
</style>
</head>
<body>
    <!-- 300+ linhas de HTML com Jinja2 inline -->
    <script>
    // JavaScript inline sem sintax highlighting
    </script>
</body>
</html>
"""
```

**Problema**: Sem linting, sem formatação automática, sem reutilização de componentes.

#### C. Configuração YAML + JSON Conflitantes

```python
# utils.py carrega settings.yaml
config = load_config()  # YAML

# app_flask.py grava settings.json
config_path = Path(__file__).parent / "config" / "settings.json"
with open(config_path, 'w') as f:
    json.dump(cfg, f)  # JSON!
```

**Problema**: Duas fontes de verdade. Se o user editar YAML, o JSON fica desatualizado.

#### D. Tratamento de Erros Inconsistente

```python
# data_aggregator.py — suprime erros
except Exception as e:
    logger.warning(f"Erro: {e}")  # WARNING para erro de API
    return {}

# paper_trading.py — suprime erros  
except Exception as e:
    logger.error(f"Erro: {e}")    # ERROR
    # Não propaga — trade falha silenciosamente

# dashboard_web.py — suprime erros
except Exception as e:
    logger.warning(f"Erro: {e}")  # WARNING de novo
```

**Problema**: Erros de API são tratados como warnings. O bot pode estar cego e o user não sabe.

#### E. Ausência de Interfaces / Contratos

Não há classes base ou protocols. Se quiseres trocar `SQLite` por `PostgreSQL`, tens de editar 15 métodos em `database.py`. Se quiseres trocar `MomentumStrategy` por outra, tens de editar `PaperTrader`, `BacktestEngine`, `WebDashboard`, etc.

#### F. Testes Fracos / Desatualizados

```python
# tests/test_strategy.py
# ⚠️ NÃO MODIFICAR — testes frozen para garantir que a estratégia
# NÃO muda acidentalmente. Se mudares a estratégia, estes testes vão falhar.
```

**Problema**: Testes "frozen" que nunca falham porque testam mocks estáticos. Zero testes de integração para as APIs reais.

---

## 🔧 ESTRATÉGIAS DE REFATORAÇÃO

### FASE 1: Consolidação de Entry Points (URGENTE)

**Objetivo**: Um único entry point.

```
NOVA ESTRUTURA:
run.py                    ← Único entry point
├── modo: cli            → Terminal Rich (original main.py)
├── modo: web            → Flask + Tray (app_flask.py)
├── modo: desktop        → WebView (app_desktop.py) — DESATIVADO por defeito
└── modo: headless       → Só bot, sem UI
```

### FASE 2: Injeção de Dependências (IMPORTANTE)

**Objetivo**: Componentes recebem dependências, não as criam.

```python
# ANTES (acoplado)
class PaperTrader:
    def __init__(self, config):
        self.db = BotDatabase()           # Cria diretamente!
        self.risk_mgr = RiskManager(config) # Cria diretamente!
        self.strategy = MomentumStrategy(config) # Cria diretamente!

# DEPOIS (injetado)
class PaperTrader:
    def __init__(self, config, db, risk_mgr, strategy, exchange_client):
        self.db = db                      # Recebido
        self.risk_mgr = risk_mgr          # Recebido
        self.strategy = strategy          # Recebido
        self.exchange = exchange_client   # Recebido
```

### FASE 3: Container de Serviços (SINGLETONS)

**Objetivo**: Uma única instância de cada serviço partilhada.

```python
class ServiceContainer:
    """Container DI — garante singletons"""
    
    def __init__(self, config):
        self.config = config
        self._db = None
        self._aggregator = None
        self._lock = threading.Lock()
    
    @property
    def database(self):
        if self._db is None:
            with self._lock:
                if self._db is None:
                    self._db = BotDatabase(config['database_path'])
        return self._db
    
    @property
    def aggregator(self):
        if self._aggregator is None:
            with self._lock:
                if self._aggregator is None:
                    self._aggregator = DataAggregator(self.config)
        return self._aggregator
```

### FASE 4: Event Bus (Substituir Estado Global)

**Objetivo**: Comunicação desacoplada via eventos.

```python
class EventBus:
    """Pub/Sub interno — substitui app_state"""
    
    def publish(self, event_type, payload):
        """Dispara evento para todos os subscribers"""
        
    def subscribe(self, event_type, callback):
        """Regista callback para evento"""

# Uso:
event_bus.publish("price.update", {"asset": "BTC", "price": 50000})
event_bus.publish("trade.executed", {"trade": trade_dict})
event_bus.publish("signal.generated", {"signal": signal_dict})
```

### FASE 5: Async / asyncio (PERFORMANCE)

**Objetivo**: Substituir threads por async, eliminar locks.

```python
# ANTES (threading + locks)
def fetch_all_data(self, asset):
    with self._lock:
        # ... fetch síncrono

# DEPOIS (async)
async def fetch_all_data(self, asset):
    async with aiohttp.ClientSession() as session:
        # Fetch paralelo das 3 exchanges
        results = await asyncio.gather(
            self._fetch_binance(session, asset),
            self._fetch_bybit(session, asset),
            self._fetch_hyperliquid(session, asset),
        )
```

### FASE 6: Templates Externos (MANUTENIBILIDADE)

**Objetivo**: Separar HTML/CSS/JS do Python.

```
web/
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   └── components/
│       ├── stats_card.html
│       ├── trade_table.html
│       └── price_chart.html
├── static/
│   ├── css/
│   │   └── cyberpunk.css
│   └── js/
│       ├── dashboard.js
│       └── charts.js
```

### FASE 7: Pipeline de Dados (BACKPRESSURE)

**Objetivo**: Evitar polling, usar produtor/consumidor.

```python
class DataPipeline:
    """
    Produtor: Busca dados das APIs a cada N segundos
    Consumidores: Strategy, Dashboard, Database (cada um no seu ritmo)
    """
    
    def __init__(self):
        self._queue = asyncio.Queue(maxsize=100)
    
    async def producer(self):
        """Busca dados e coloca na queue"""
        while self.running:
            data = await self.aggregator.fetch_all_data("BTC")
            await self._queue.put(data)
            await asyncio.sleep(self.interval)
    
    async def consumer_strategy(self):
        """Consome da queue e gera sinais"""
        while self.running:
            data = await self._queue.get()
            signal = self.strategy.analyze(data)
            if signal:
                await self.execute_signal(signal)
```

---

## 📊 PRIORIZAÇÃO DE IMPACTO

| Problema | Impacto | Esforço | Prioridade |
|----------|---------|---------|------------|
| Estado Global (`app_state`) | 🔴 CRÍTICO | Médio | **P0 — Agora** |
| 3 Entry Points | 🔴 CRÍTICO | Baixo | **P0 — Agora** |
| Duplicação Monitor Loop | 🟠 ALTO | Baixo | **P1 — Esta semana** |
| Dashboard instancia Aggregator | 🟠 ALTO | Médio | **P1 — Esta semana** |
| HTML Inline | 🟡 MÉDIO | Médio | P2 — Próxima sprint |
| Async/await | 🟡 MÉDIO | Alto | P2 — Próxima sprint |
| YAML vs JSON | 🟡 MÉDIO | Baixo | P2 — Próxima sprint |
| Testes fracos | 🟢 BAIXO | Alto | P3 — Depois |

---

## ✅ CÓDIGO MELHORADO (Exemplos)

### Exemplo 1: ServiceContainer (resolve duplicação + estado global)

```python
# src/container.py
import threading
from typing import Optional

class ServiceContainer:
    """
    Container de Injeção de Dependências.
    Garante singletons thread-safe para todos os serviços.
    """
    
    def __init__(self, config: dict):
        self._config = config
        self._services = {}
        self._lock = threading.RLock()
    
    def get(self, name: str, factory) -> object:
        """Lazy initialization thread-safe"""
        if name not in self._services:
            with self._lock:
                if name not in self._services:  # Double-check
                    self._services[name] = factory(self._config)
        return self._services[name]
    
    @property
    def database(self):
        from database import BotDatabase
        return self.get('db', lambda c: BotDatabase(c.get('database', {}).get('path', 'data/trading_bot.db')))
    
    @property
    def aggregator(self):
        from data_aggregator import DataAggregator
        return self.get('aggregator', lambda c: DataAggregator(c))
    
    @property
    def strategy(self):
        from strategy import MomentumStrategy
        return self.get('strategy', lambda c: MomentumStrategy(c))
    
    @property
    def risk_manager(self):
        from risk_manager import RiskManager
        return self.get('risk', lambda c: RiskManager(c))
    
    @property
    def exchange_client(self):
        from exchange_client import HyperliquidClient
        return self.get('exchange', lambda c: HyperliquidClient(c, paper_trading=c['bot'].get('paper_trading', True)))

# Uso em QUALQUER ficheiro:
from container import ServiceContainer

container = ServiceContainer(config)
db = container.database  # Sempre a mesma instância
agg = container.aggregator  # Sempre a mesma instância
```

### Exemplo 2: EventBus (substitui app_state)

```python
# src/events.py
import asyncio
from typing import Callable, Dict, List
from dataclasses import dataclass
from datetime import datetime

@dataclass
class Event:
    type: str
    payload: dict
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

class EventBus:
    """Pub/Sub simples — substitui estado global mutable"""
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._history: List[Event] = []
        self._max_history = 1000
    
    def subscribe(self, event_type: str, callback: Callable):
        """Regista callback para evento"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
    
    def publish(self, event_type: str, payload: dict):
        """Dispara evento para todos os subscribers"""
        event = Event(type=event_type, payload=payload)
        self._history.append(event)
        
        # Trim history
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        
        # Notificar subscribers
        for callback in self._subscribers.get(event_type, []):
            try:
                callback(event)
            except Exception as e:
                # Não deixar um subscriber crashar os outros
                import logging
                logging.getLogger(__name__).error(f"Erro no subscriber: {e}")
    
    def get_history(self, event_type: str = None, limit: int = 100) -> List[Event]:
        """Busca histórico de eventos"""
        events = self._history
        if event_type:
            events = [e for e in events if e.type == event_type]
        return events[-limit:]

# Uso:
bus = EventBus()

# Dashboard subscreve a updates de preço
bus.subscribe("price.update", lambda e: dashboard.update_price(e.payload))

# Database subscreve a trades
bus.subscribe("trade.executed", lambda e: db.save_trade(e.payload))

# PaperTrader publica
def on_price_change(self, asset, price):
    bus.publish("price.update", {"asset": asset, "price": price})

def on_trade(self, trade):
    bus.publish("trade.executed", trade.to_dict())
```

### Exemplo 3: Async DataPipeline (resolve polling + performance)

```python
# src/pipeline.py
import asyncio
from typing import Optional
import logging

logger = logging.getLogger(__name__)

class DataPipeline:
    """
    Pipeline produtor-consumidor para dados de mercado.
    Resolve problemas de polling, locks e duplicação de requests.
    """
    
    def __init__(self, config, container, event_bus):
        self.config = config
        self.container = container
        self.bus = event_bus
        self._running = False
        self._queue = asyncio.Queue(maxsize=50)
        self._tasks = []
    
    async def start(self):
        """Inicia pipeline"""
        self._running = True
        
        # Produtor único de dados
        self._tasks.append(asyncio.create_task(self._producer()))
        
        # Consumidores (cada um no seu ritmo)
        self._tasks.append(asyncio.create_task(self._consumer_strategy()))
        self._tasks.append(asyncio.create_task(self._consumer_database()))
        self._tasks.append(asyncio.create_task(self._consumer_dashboard()))
        
        logger.info("Pipeline iniciada")
    
    async def stop(self):
        """Para pipeline graciosamente"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
    
    async def _producer(self):
        """Busca dados das APIs e coloca na queue"""
        aggregator = self.container.aggregator
        interval = self.config.get('polling', {}).get('oi_interval', 30)
        
        while self._running:
            try:
                for asset in self.config.get('assets', ['BTC']):
                    data = await aggregator.fetch_all_data_async(asset)
                    if data:
                        await self._queue.put({
                            'type': 'market_data',
                            'asset': asset,
                            'data': data,
                            'timestamp': asyncio.get_event_loop().time()
                        })
                
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Erro no producer: {e}")
                await asyncio.sleep(5)  # Backoff rápido
    
    async def _consumer_strategy(self):
        """Consome dados e gera sinais de trading"""
        strategy = self.container.strategy
        trader = self.container.trader
        
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=1)
                
                if item['type'] == 'market_data':
                    data = item['data']
                    price = data.get('price', 0)
                    
                    signal = strategy.analyze(data, price)
                    if signal:
                        self.bus.publish("signal.generated", {
                            'asset': item['asset'],
                            'signal': signal,
                            'price': price
                        })
                        
                        # Executar trade
                        await trader.execute_signal_async(signal, item['asset'], price)
                
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Erro no consumer strategy: {e}")
```

### Exemplo 4: Entry Point Unificado

```python
# run.py — ÚNICO entry point
#!/usr/bin/env python3
"""
Hyperliquid Bot — Entry Point Unificado
Modos: cli | web | desktop | headless
"""
import argparse
import sys
import asyncio
from pathlib import Path

# Adicionar src ao path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from utils import load_config, setup_logging
from container import ServiceContainer
from events import EventBus

MODES = {
    'cli': 'Terminal Rich com logs',
    'web': 'Flask server + System Tray',
    'desktop': 'WebView nativo (experimental)',
    'headless': 'Só o bot, sem UI'
}

def main():
    parser = argparse.ArgumentParser(description='Hyperliquid Trading Bot')
    parser.add_argument('mode', choices=MODES.keys(), default='web', nargs='?',
                       help='Modo de execução')
    parser.add_argument('--config', '-c', default='config/settings.yaml',
                       help='Caminho para ficheiro de config')
    parser.add_argument('--no-tray', action='store_true',
                       help='Desativar system tray (modo web)')
    parser.add_argument('--port', '-p', type=int, default=5000,
                       help='Porta do servidor web')
    
    args = parser.parse_args()
    
    # Setup
    config = load_config(args.config)
    setup_logging(config.get('logging', {}))
    
    # Container DI (singletons)
    container = ServiceContainer(config)
    event_bus = EventBus()
    
    # Iniciar modo
    if args.mode == 'cli':
        from modes.cli_mode import run_cli
        run_cli(config, container, event_bus)
    
    elif args.mode == 'web':
        from modes.web_mode import run_web
        run_web(config, container, event_bus, port=args.port, tray=not args.no_tray)
    
    elif args.mode == 'headless':
        from modes.headless_mode import run_headless
        asyncio.run(run_headless(config, container, event_bus))
    
    elif args.mode == 'desktop':
        print("Modo desktop desativado. Use 'web' em vez.")
        sys.exit(1)

if __name__ == '__main__':
    main()
```

---

## 📈 ESTIMATIVAS DE REFATORAÇÃO

| Fase | Tempo Est. | Impacto |
|------|-----------|---------|
| Fase 1: Entry Point Unificado | 2-3h | 🟢 Elimina 3 ficheiros, comportamento consistente |
| Fase 2: ServiceContainer | 3-4h | 🟢 Elimina duplicação, melhora testes |
| Fase 3: EventBus (básico) | 4-5h | 🟢 Elimina app_state, desacopla componentes |
| Fase 4: Async Pipeline | 6-8h | 🟡 Melhora performance, requer testes extensivos |
| Fase 5: Templates Externos | 3-4h | 🟡 Melhora manutenibilidade da dashboard |
| **TOTAL** | **18-24h** | 🚀 Bot pronto para produção |

---

## 🎯 RECOMENDAÇÃO FINAL

**NÃO refactors tudo de uma vez.** O bot funciona. A estratégia tem edge. O risco é quebrar o que já funciona.

**Ordem recomendada:**
1. **Hoje**: Entry point unificado + ServiceContainer (baixo risco, alto impacto)
2. **Esta semana**: EventBus básico (elimina app_state gradualmente)
3. **Próxima sprint**: Async pipeline (só se houver problemas de performance reais)
4. **Depois**: Templates externos, testes, CI/CD

**O que NÃO mudar agora:**
- A lógica da estratégia (está validada, PF 2.50)
- A estrutura da base de dados (funciona, WAL mode resolveu locks)
- O sistema de paper trading (core está sólido)

---

*Relatório gerado em: 2026-04-26*
*Ficheiros analisados: 18 Python, 1 HTML, 1 YAML*
*Linhas de código analisadas: ~4,500*
