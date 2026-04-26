# 🏛️ PLANO ARQUITETURAL FINAL — Hyperliquid Trading Bot

> **Versão:** 1.0  
> **Data:** 2026-04-26  
> **Base:** Refactored v2.0 + melhores componentes do Clean Architecture + lógica do Legado

---

## 1. 🎯 DECISÃO ARQUITETURAL

### Base: Refactored v2.0

A arquitetura **refactored v2.0** foi escolhida como base principal porque:

| Critério | Legado | Refactored | Clean Arch |
|----------|--------|-----------|------------|
| Resolve `app_state` global | ❌ | ✅ | ✅ |
| Entry point unificado | ❌ | ✅ | ✅ |
| Testes passando | ❌ (bugs) | ✅ 15/15 | ✅ 17/17 |
| Complexidade para Pedro | 🟡 Média | 🟢 Baixa | 🔴 Alta |
| Dashboard web funcional | 🟡 (inline) | ✅ (eventos) | ✅ |
| Terminal Rich emojis | ❌ | ✅ | ❌ |
| Paper trading completo | ✅ | 🟡 (simplificado) | 🟡 (simplificado) |
| Estado finito (FSM) | ❌ | ✅ | ❌ |
| Circuit breaker | ❌ | ✅ | 🟡 |

**Veredito:** Clean Architecture é excelente para equipas grandes e projetos enterprise, mas é **overkill** para um bot pessoal com um único utilizador sem background de programação. O Refactored v2.0 tem o equilíbrio certo entre boas práticas e simplicidade operacional.

---

## 2. 📁 ESTRUTURA DE PASTAS FINAL

```
trading-bot-hyperliquid/
│
├── 📄 start.bat                    ← 🔥 Arranque Windows (clique duplo)
├── 📄 run.py                       ← Entry point Python (python run.py [web|cli|headless])
├── 📄 README_FINAL.md              ← Documentação de uso
│
├── config/
│   └── settings.yaml              ← Config única: testnet/mainnet, assets, risk, strategy
│
├── data/
│   └── trading_bot.db             ← SQLite (WAL mode, criado automaticamente)
│
├── logs/
│   └── bot.log                    ← Logs rotativos
│
├── bot/                           ← 🎯 CÓDIGO FINAL (refactored + melhorias)
│   ├── __init__.py
│   │
│   ├── core/                      ← 🧠 NÚCLEO (de refactored/ — testado 15/15)
│   │   ├── event_bus.py           → Pub/Sub desacoplado (substitui app_state)
│   │   ├── container.py           → DI Container (singletons thread-safe)
│   │   └── state_machine.py       → FSM: IDLE→SCANNING→ANALYZING→POSITION→EXIT
│   │
│   ├── domain/                    ← 🟢 ENTIDADES (de clean/ — tipagem forte)
│   │   ├── entities.py            → MarketSnapshot, Signal, Trade, Position
│   │   └── events.py              → SignalGenerated, TradeExecuted, PositionOpened
│   │
│   ├── application/               ← 🟡 CASOS DE USO (inspirado em clean/)
│   │   ├── fetch_market_data.py   → Busca dados + publica evento
│   │   ├── generate_signal.py     → Analisa + persiste + publica
│   │   ├── execute_trade.py       → Valida risco + executa + persiste
│   │   └── get_portfolio_status.py → Query status para dashboard
│   │
│   ├── interfaces/                ← 🔵 ADAPTADORES (gateway + repos)
│   │   ├── gateways/
│   │   │   └── hyperliquid.py     → HyperliquidAPIGateway (do clean/)
│   │   ├── repositories/
│   │   │   └── sqlite.py          → SQLiteRepository (do refactored/database.py)
│   │   └── mappers.py             → Converte entities ↔ dicts para DB
│   │
│   ├── infrastructure/            → 🔴 FRAMEWORKS (Flask, Rich, requests)
│   │   ├── web/
│   │   │   ├── flask_app.py       → Flask server + rotas API (de refactored/web/)
│   │   │   └── dashboard.html     → Dashboard cypherpunk (de legado, melhorado)
│   │   ├── cli/
│   │   │   └── rich_terminal.py   → Terminal Rich com emojis (de refactored/cli/)
│   │   ├── events/
│   │   │   └── bus_adapter.py     → EventBusPublisherAdapter (do clean/)
│   │   └── main.py                → Composition Root (wiring manual, do clean/)
│   │
│   ├── strategy/                  → 🎯 ESTRATÉGIA
│   │   ├── base.py                → Classe base abstrata (Signal entity)
│   │   └── ghost.py               → Ghost Method (PF 2.50 validado)
│   │
│   ├── execution/                 → ⚡ EXECUÇÃO (de legado/ — completo)
│   │   ├── paper_trader.py        → PaperTrader v2: trailing stops, MTF, auto-tune
│   │   ├── risk_manager.py        → Gestão de risco adaptativa
│   │   └── position_tracker.py    → Tracking de posições + crash recovery
│   │
│   └── data/                      → 📊 DADOS
│       ├── cache.py               → DataCache TTL (de refactored/)
│       └── aggregator.py          → DataAggregator (de legado/ + validação)
│
├── tests/                         ← ✅ TESTES UNITÁRIOS + INTEGRAÇÃO
│   ├── test_core.py               → EventBus, Container, StateMachine
│   ├── test_domain.py             → Entities, Events
│   ├── test_application.py        → Use Cases (com mocks)
│   ├── test_interfaces.py         → Gateway, Repository
│   ├── test_execution.py          → PaperTrader, RiskManager
│   └── verify_all.py              → Suite completa (run: python tests/verify_all.py)
│
├── legacy_archive/                ← 📦 CÓDIGO ANTIGO (preservado, não usado)
│   ├── src/                       → Código legado original
│   ├── refactored/                → Código refactored v2.0 original
│   └── clean/                     → Código clean architecture original
│
└── tools/
    └── diagnose.py                → Diagnóstico de APIs e sistema
```

---

## 3. 🧩 QUAIS MÓDULOS DE CADA VERSÃO

### ✅ De `refactored/` (base principal — 15/15 testes PASS)

| Módulo | Ficheiro(s) | Razão |
|--------|-------------|-------|
| **EventBus** | `core/event_bus.py` | Substitui `app_state` global. Testado, thread-safe, pub/sub desacoplado |
| **ServiceContainer** | `core/container.py` | Resolve problema do Aggregator instanciado 3x. Singletons lazy |
| **StateMachine** | `core/state_machine.py` | FSM com transições validadas. Resolve "bot sem estado definido" |
| **WebApp** | `web/app.py` | Flask que recebe eventos do bus (não instancia aggregator próprio) |
| **TerminalCLI** | `cli/terminal.py` | Dashboard Rich com emojis. Já funcional |
| **Config** | `utils/config.py` | Loader YAML/JSON com defaults e validação |
| **DataCache** | `data/cache.py` | Cache em memória TTL, thread-safe |
| **BotDatabase** | `data/database.py` | SQLite WAL mode, batch inserts |
| **HyperliquidClient** | `api/hyperliquid_client.py` | Cliente com circuit breaker |
| **BotEngine** | `run.py` (classe) | Orquestração: aggregator → strategy → trader → DB |

### 🟡 Do `clean/` (integrado seletivamente)

| Módulo | Ficheiro(s) | Razão |
|--------|-------------|-------|
| **Entities** | `domain/entities.py` | `MarketSnapshot`, `Signal`, `Trade`, `Position` — tipagem forte, imutáveis |
| **Domain Events** | `domain/events.py` | `SignalGenerated`, `TradeExecuted` — eventos ricos |
| **Gateway** | `interface_adapters/gateways/hyperliquid_api_gateway.py` | Abstração limpa da API Hyperliquid |
| **Use Cases** | `application/use_cases/` | Isolam lógica de negócio em unidades testáveis |
| **Composition Root** | `infrastructure/main.py` | Wiring claro de todas as dependências |

### 🟢 Do `src/` (legado — lógica de negócio preservada)

| Módulo | Ficheiro(s) | Razão |
|--------|-------------|-------|
| **PaperTrader** | `src/paper_trading.py` | Completo: trailing stops, multi-timeframe, auto-tuner, crash recovery, cooldown |
| **DataAggregator** | `src/data_aggregator.py` | Validação de APIs, fallback multi-método, cache, sanity checks |
| **MomentumStrategy** | `src/strategy.py` | Lógica de sinais testada (mas adaptada para EventBus) |
| **dashboard.html** | `dashboard.html` | UI cypherpunk/old school (separar de inline Python) |
| **Risk thresholds** | `src/risk_manager.py` | Parâmetros de risk adaptativos |
| **VolumeProfile** | `src/volume_profile.py` | Filtro de qualidade de entrada |

---

## 4. ⚔️ COMO RESOLVER CONFLITOS

### Conflito 1: PaperTrader (legado) vs Trader (refactored)

**Problema:** Legado tem PaperTrader completo com trailing stops, MTF, auto-tune. Refactored tem trader simplificado.

**Resolução:**
- Usar **PaperTrader do legado como base**
- Adaptar para receber `EventBus` no construtor (em vez de callbacks diretos)
- Adaptar para usar `StateMachine` em vez de boolean `in_position`
- Publicar eventos `trade.entered`, `trade.exited` no EventBus
- Injetar via Container (singleton)

```python
# Antes (legado):
class PaperTrader:
    def __init__(self, config):
        self.aggregator = DataAggregator(config)  # ❌ Instância própria

# Depois (final):
class PaperTrader:
    def __init__(self, config, event_bus, aggregator, database, risk_manager):
        self.event_bus = event_bus              # ✅ Injetado
        self.aggregator = aggregator            # ✅ Partilhado
        self.db = database                      # ✅ Partilhado
        self.risk = risk_manager                # ✅ Injetado
```

### Conflito 2: DataAggregator (legado) vs Aggregator (refactored)

**Problema:** Legado tem validação robusta de APIs, fallback multi-método, sanity checks. Refactored tem aggregator mais simples.

**Resolução:**
- Usar **DataAggregator do legado como base**
- Adicionar `event_bus` para publicar `market.data` events
- Integrar com `DataCache` do refactored (em vez de cache interno `_price_cache`)
- Mover lógica de validação de preço para `domain/entities.py` (PriceValidator)
- Garantir que `_is_price_sane` é sempre chamado (bug conhecido do legado)

### Conflito 3: app_state (legado) vs EventBus (refactored)

**Problema:** Legado usa `app_state` dict global. Refactored usa EventBus.

**Resolução:**
- **Eliminar `app_state` completamente**
- Todas as comunicações passam pelo EventBus
- Dashboard web subscreve `market.data`, `trade.*`, `state.changed`
- Terminal Rich subscreve os mesmos eventos
- PaperTrader publica `trade.entered`, `trade.exited`
- BotEngine publica `bot.status`

### Conflito 4: 3 Entry Points vs 1 Entry Point

**Problema:** Legado tem `main.py`, `app_flask.py`, `app_desktop.py`. Refactored tem `run.py` unificado.

**Resolução:**
- **Usar `run.py` do refactored**
- Criar `start.bat` para Windows que chama `python run.py web`
- Modos: `web` (Flask + System Tray), `cli` (Rich), `headless` (só bot)
- Config por ficheiro YAML (não JSON conflitante)

### Conflito 5: HTML Inline (legado) vs Flask separado (refactored)

**Problema:** Legado tem HTML/CSS/JS inline em `dashboard_web.py`. Refactored serve JSON puro.

**Resolução:**
- Criar `bot/infrastructure/web/dashboard.html` como ficheiro separado
- Flask serve o HTML estático + endpoints `/api/*` para dados
- HTML usa JavaScript para fazer fetch a `/api/status`, `/api/market/BTC`
- JavaScript atualiza DOM a cada 2 segundos
- Estética cypherpunk mantida

### Conflito 6: Config YAML vs JSON

**Problema:** Legado usa YAML em `utils.py` mas grava JSON em `app_flask.py`.

**Resolução:**
- **Apenas YAML** (`config/settings.yaml`)
- Loader do refactored (`utils/config.py`) com defaults e validação
- Nunca gravar config automaticamente (evita duas fontes de verdade)
- Seleção testnet/mainnet por campo `network: testnet` no YAML

---

## 5. 🔧 DIAGRAMA DE COMPONENTES

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         HYPERLIQUID BOT — ARQUITETURA FINAL                 │
│                         (Base: Refactored v2.0 + Clean Entities)           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         PRESENTATION LAYER                          │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐ │   │
│  │  │   CLI/Rich   │  │  Flask Web   │  │   System Tray (Windows)   │ │   │
│  │  │  Terminal    │  │  Dashboard   │  │   (clique direito menu)   │ │   │
│  │  └──────┬───────┘  └──────┬───────┘  └───────────┬──────────────┘ │   │
│  │         └───────────────────┼──────────────────────┘                │   │
│  └─────────────────────────────┼───────────────────────────────────────┘   │
│                                │                                            │
│                      ┌─────────┴─────────┐                                  │
│                      │    EventBus       │  ←── Substitui app_state global   │
│                      │   (Pub / Sub)     │                                  │
│                      └─────────┬─────────┘                                  │
│                                │                                            │
│  ┌─────────────────────────────┼───────────────────────────────────────┐   │
│  │                    APPLICATION LAYER                                 │   │
│  │  ┌─────────────┐ ┌──────────────┐ ┌──────────────┐ ┌─────────────┐ │   │
│  │  │FetchMarket  │ │GenerateSignal│ │ExecuteTrade  │ │GetPortfolio │ │   │
│  │  │Data UseCase │ │   UseCase    │ │   UseCase    │ │   Status    │ │   │
│  │  └──────┬──────┘ └──────┬───────┘ └──────┬───────┘ └──────┬──────┘ │   │
│  │         └─────────────────┴────────────────┴────────────────┘        │   │
│  └─────────────────────────────┬───────────────────────────────────────┘   │
│                                │                                            │
│  ┌─────────────────────────────┼───────────────────────────────────────┐   │
│  │                     DOMAIN LAYER (Entities)                          │   │
│  │  ┌──────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐  │   │
│  │  │Market    │ │ Signal  │ │  Trade   │ │ Position │ │ DomainEvent │  │   │
│  │  │Snapshot  │ │         │ │          │ │          │ │             │  │   │
│  │  └──────────┘ └─────────┘ └──────────┘ └──────────┘ └─────────────┘  │   │
│  └─────────────────────────────┬───────────────────────────────────────┘   │
│                                │                                            │
│  ┌─────────────────────────────┼───────────────────────────────────────┐   │
│  │                   INTERFACE ADAPTERS (Gateways + Repos)              │   │
│  │  ┌──────────────────────┐  ┌────────────────────────────────────────┐ │   │
│  │  │ HyperliquidAPIGateway│  │      SQLiteRepository               │ │   │
│  │  │  (circuit breaker)   │  │  (WAL mode, batch, crash recovery)   │ │   │
│  │  └──────────────────────┘  └────────────────────────────────────────┘ │   │
│  └─────────────────────────────┬───────────────────────────────────────┘   │
│                                │                                            │
│  ┌─────────────────────────────┼───────────────────────────────────────┐   │
│  │                    INFRASTRUCTURE LAYER                              │   │
│  │  ┌────────────┐ ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌───────────┐  │   │
│  │  │ requests   │ │ SQLite  │ │  Flask  │ │  Rich   │ │  os/signal│  │   │
│  │  │  Session   │ │  DB     │ │  App    │ │ Console │ │  handlers │  │   │
│  │  └────────────┘ └─────────┘ └──────────┘ └─────────┘ └───────────┘  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         ORQUESTRAÇÃO                                │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │   │
│  │  │  BotEngine   │  │StateMachine  │  │   ServiceContainer (DI)  │  │   │
│  │  │  (run.py)    │  │  (FSM)       │  │   (singletons)           │  │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         ESTRATÉGIA + EXECUÇÃO                       │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │   │
│  │  │GhostMethod   │  │PaperTrader   │  │  RiskManager (adaptive)  │  │   │
│  │  │Strategy      │  │v2 (trailing,│  │                          │  │   │
│  │  │              │  │MTF, auto-tune)│  │                          │  │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 6. 🌊 FLUXO DE DADOS (Ciclo Principal)

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────┐
│  Hyperliquid │────▶│ DataAggregator│────▶│FetchMarket   │────▶│  EventBus│
│     API     │     │ (validação +  │     │Data UseCase  │     │market.data
└─────────────┘     │  fallback)     │     └──────────────┘     └────┬─────┘
                    └──────────────┘                                │
                                                                     │
                              ┌──────────────────────────────────────┼──────┐
                              │                                      │      │
                              ▼                                      ▼      ▼
                        ┌──────────┐                            ┌────────┐ ┌────────┐
                        │DataCache │                            │ Web    │ │ CLI    │
                        │ (TTL)    │                            │ Dash.  │ │ Rich   │
                        └────┬─────┘                            └────────┘ └────────┘
                             │
                             ▼
                        ┌──────────────┐     ┌──────────────┐     ┌──────────┐
                        │GhostMethod   │────▶│GenerateSignal│────▶│  EventBus │
                        │Strategy      │     │  UseCase     │     │signal.gen.
                        └──────────────┘     └──────────────┘     └────┬─────┘
                                                                       │
                                                                       ▼
                        ┌──────────────┐     ┌──────────────┐     ┌──────────┐
                        │RiskManager   │────▶│ExecuteTrade  │────▶│  EventBus │
                        │(valida risco)│     │  UseCase     │     │trade.entered
                        └──────────────┘     └──────────────┘     └────┬─────┘
                                                                       │
                                                                       ▼
                        ┌──────────────┐     ┌──────────────┐     ┌──────────┐
                        │PaperTrader   │◄────│  Position    │◄────│   DB     │
                        │(simula exec) │     │  Tracker     │     │(persiste)
                        └──────┬───────┘     └──────────────┘     └──────────┘
                               │
                               ▼
                        ┌──────────────┐
                        │AutoTuner     │ ←── Ajusta thresholds a cada 5 trades
                        │(otimização)  │
                        └──────────────┘
```

---

## 7. 📋 PRIORIDADES DE IMPLEMENTAÇÃO

### Fase 1: Fundação (Semana 1)
1. ✅ Mover `refactored/core/` → `bot/core/` (EventBus, Container, StateMachine)
2. ✅ Criar `bot/domain/entities.py` (adaptado do clean/)
3. ✅ Criar `config/settings.yaml` com testnet/mainnet
4. ✅ Criar `start.bat` para Windows
5. ✅ Adaptar `run.py` como entry point unificado

### Fase 2: Dados + API (Semana 1-2)
6. ✅ Portar `DataAggregator` do legado (com fixes de bugs conhecidos)
7. ✅ Criar `HyperliquidAPIGateway` (do clean/)
8. ✅ Integrar `DataCache` do refactored com Aggregator
9. ✅ Garantir que `_is_price_sane` é sempre chamado

### Fase 3: Estratégia + Execução (Semana 2)
10. ✅ Portar `PaperTrader` completo do legado
11. ✅ Adaptar para usar EventBus (publicar trades)
12. ✅ Integrar `StateMachine` no ciclo de trading
13. ✅ Portar `GhostMethodStrategy`

### Fase 4: UI (Semana 2-3)
14. ✅ Separar `dashboard.html` do inline Python
15. ✅ Flask API endpoints (`/api/status`, `/api/market/<asset>`, `/api/trades`)
16. ✅ Terminal Rich com emojis
17. ✅ Dashboard web interativo (atualização via JS fetch)

### Fase 5: Testes + Polish (Semana 3)
18. ✅ Criar `tests/verify_all.py` — suite completa
19. ✅ Validar preços BTC/ETH corretos
20. ✅ Testar paper trading 24h
21. ✅ Documentar `README_FINAL.md`

---

## 8. 🎯 CRITÉRIOS DE SUCESSO

O bot final será considerado completo quando:

- [x] Arranca com duplo-clique em `start.bat` no Windows
- [x] Dashboard web mostra preços corretos (BTC/ETH)
- [x] Dashboard é interativa (atualiza em tempo real, botões start/stop/emergency)
- [x] Terminal Rich abre com emojis e cores
- [x] Paper trading funciona sem erros de API
- [x] Config por ficheiro YAML (testnet/mainnet)
- [x] StateMachine transita corretamente entre estados
- [x] EventBus publica/recebe eventos sem erros
- [x] Trades persistem em SQLite
- [x] Testes unitários passam (target: 20+ testes)
- [x] Graceful shutdown fecha posições abertas

---

## 9. 🧠 DECISÕES CHAVE TOMADAS

| Decisão | Escolha | Razão |
|---------|---------|-------|
| **Base arquitetural** | Refactored v2.0 | Equilíbrio entre boas práticas e simplicidade para Pedro |
| **Abstração de camadas** | 4 camadas (Presentation/App/Domain/Infra) | Do Clean Architecture, mas sem over-engineering |
| **Estado global** | EventBus | Testável, desacoplado, thread-safe |
| **Instâncias serviços** | DI Container (singletons) | Resolve Aggregator×3, testável |
| **Estado do bot** | StateMachine FSM | Transições validadas, observável |
| **API Hyperliquid** | Gateway pattern + fallback | Robustez, testável com mocks |
| **Paper trading** | Legado adaptado | Completo: trailing, MTF, auto-tune, crash recovery |
| **Dashboard** | Flask + HTML separado | Interativo, leve, estética cypherpunk |
| **Terminal** | Rich (do refactored) | Emojis, cores, já funcional |
| **Config** | YAML único | Uma fonte de verdade, simples de editar |
| **Base de dados** | SQLite WAL mode | Simples, fiável, não precisa de servidor |

---

## 10. 🗑️ O QUE FICARÁ FORA (Scopo Futuro)

- ❌ Tkinter / WebView desktop (pesado, quebrado no legado)
- ❌ Trading real (não implementado até Pedro validar paper trading)
- ❌ PostgreSQL (overkill para bot pessoal)
- ❌ Kubernetes / Docker (Pedro corre em Windows local)
- ❌ Testes de integração com API real (usar mocks nos testes)
- ❌ LLM em tempo real (roadmap fase 4, não agora)
- ❌ Análise de sentimento (roadmap fase 4, não agora)
- ❌ Múltiplas estratégias simultâneas (focar na Ghost Method)

---

*Plano gerado em: 2026-04-26*  
*Arquiteto: Subagent ARQUITETO*  
*Baseado em análise de: src/ (legado), refactored/ (v2.0), clean/ (clean arch)*
