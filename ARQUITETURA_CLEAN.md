# 🏛️ Clean Architecture — Hyperliquid Trading Bot

## Princípios Aplicados

- **Dependency Rule**: Inner layers don't know about outer layers.
- **Separation of Concerns**: Cada camada tem responsabilidade única.
- **Dependency Inversion**: Domain define interfaces (ports), infrastructure implementa (adapters).
- **Testability**: Cada camada pode ser testada isoladamente com mocks.

---

## Estrutura de Pastas

```
clean/
├── domain/                          # 🟢 Innermost — no external deps
│   ├── entities/
│   │   ├── candle.py               # Candle, Signal, Position, Trade, MarketSnapshot
│   │   └── ...
│   ├── events/
│   │   └── domain_event.py         # SignalGenerated, TradeExecuted, PositionOpened...
│   ├── repositories/
│   │   └── interfaces.py           # CandleRepository, TradeRepository, SignalRepository (ports)
│   └── services/
│       └── interfaces.py           # MarketDataProvider, ExchangeGateway (ports)
│
├── application/                     # 🟡 Orchestration layer
│   ├── use_cases/
│   │   ├── fetch_market_data.py    # Busca dados + publica evento
│   │   ├── generate_signal.py      # Analisa + persiste + publica
│   │   ├── execute_trade.py        # Valida risco + executa + persiste
│   │   └── get_portfolio_status.py # Query status
│   ├── dto/
│   │   └── dtos.py                 # MarketDataDTO, SignalDTO, TradeDTO, PortfolioStatusDTO
│   └── interfaces/
│       └── ports.py                # EventPublisher, Logger, StrategyPort (ports)
│
├── interface_adapters/              # 🔵 Adapters — concretions
│   ├── repositories/
│   │   ├── sqlite_candle_repository.py   # Implements CandleRepository
│   │   ├── sqlite_trade_repository.py    # Implements TradeRepository
│   │   └── sqlite_signal_repository.py   # Implements SignalRepository
│   ├── gateways/
│   │   └── hyperliquid_api_gateway.py    # Implements MarketDataProvider + ExchangeGateway
│   ├── controllers/
│   │   └── web_api_controller.py         # Chama use cases, retorna JSON
│   ├── mappers/
│   │   └── mappers.py                    # CandleMapper, TradeMapper, SignalMapper
│   └── database/
│       └── sqlite_connection.py          # Persistent connection per thread
│
└── infrastructure/                  # 🔴 Outermost — frameworks
    ├── web/
    │   └── flask_app.py            # Flask routes + server
    ├── events/
    │   └── event_bus_publisher.py  # Adapts EventBus to EventPublisher port
    ├── strategy_adapter.py         # Adapts GhostMethodStrategy to StrategyPort
    └── main.py                     # 🎯 Composition Root — wires everything
```

---

## Fluxo de Dados

```
HTTP Request → FlaskApp → WebAPIController → UseCase → DomainEntity → Repository
                                     ↓
                                EventPublisher → EventBus → Subscribers
```

### Exemplo: Gerar Sinal

1. **Infrastructure**: Flask recebe POST `/api/signal/BTC`
2. **Interface Adapter**: Controller chama `GenerateSignalUseCase.execute("BTC")`
3. **Application**: UseCase busca `MarketDataDTO` via `FetchMarketDataUseCase`
4. **Application**: UseCase chama `StrategyPort.analyze()` (adaptador)
5. **Domain**: Strategy decide LONG/SHORT/HOLD
6. **Application**: UseCase cria `Signal` entity, persiste via `SignalRepository`
7. **Application**: UseCase publica `SignalGenerated` event via `EventPublisher`
8. **Infrastructure**: EventBus notifica subscribers (dashboard, logger)

---

## Vantagens vs. Legado

| Aspecto | Legado (v1) | Refactored (v2) | Clean Architecture (v3) |
|---------|------------|-----------------|------------------------|
| **Acoplamento** | Tudo acoplado | Melhor com DI | Zero acoplamento entre camadas |
| **Testabilidade** | Difícil (API real) | Melhor | **Cada camada testável com mocks** |
| **Trocar DB** | Editar 15 métodos | 1 ficheiro | **Alterar adapter só** |
| **Trocar Exchange** | Editar cliente | 1 ficheiro | **Alterar gateway só** |
| **Trocar Strategy** | Herança frágil | BaseStrategy | **Alterar adapter só** |
| **Trocar Web** | Inline HTML | Flask separado | **Alterar infrastructure só** |
| **Domain Logic** | Espalhada | Centralizada | **100% isolada em domain + application** |

---

## Ports & Adapters

| Port (Interface) | Location | Adapter (Implementation) |
|-------------------|----------|-------------------------|
| `CandleRepository` | `domain/repositories/` | `SQLiteCandleRepository` |
| `TradeRepository` | `domain/repositories/` | `SQLiteTradeRepository` |
| `SignalRepository` | `domain/repositories/` | `SQLiteSignalRepository` |
| `MarketDataProvider` | `domain/services/` | `HyperliquidAPIGateway` |
| `ExchangeGateway` | `domain/services/` | `HyperliquidAPIGateway` |
| `EventPublisher` | `application/interfaces/` | `EventBusPublisherAdapter` |
| `Logger` | `application/interfaces/` | `StandardLogger` |
| `StrategyPort` | `application/interfaces/` | `StrategyAdapter` |

---

## Como Executar

```bash
# A partir da raiz do projeto
python3 clean/infrastructure/main.py

# Testar
python3 clean/tests/verify_clean.py
```

---

## Próximos Passos

1. ✅ **Merge com optimized/** — Usar módulos otimizados (deque, RLock, persistent connections)
2. **Testes unitários** — Mockar todas as ports para testar use cases isoladamente
3. **CLI Adapter** — Criar `cli_controller.py` + `rich_terminal.py`
4. **Real Trading** — Implementar `ExchangeGateway` real com chaves API
