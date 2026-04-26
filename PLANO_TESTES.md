# Plano de Testes e Revisão de Qualidade
# Hyperliquid Trading Bot — Versão Final Unificada
# Autor: Revisor | Data: 2026-04-26

---

## Sumário Executivo

Este documento define o plano completo de testes para a versão final unificada do bot de trading Hyperliquid, resultante da fusão das 3 versões existentes (`src/`, `refactored/`, `clean/`). O objetivo é garantir que todos os bugs conhecidos do legado estejam corrigidos, que a arquitetura Clean esteja funcional, e que o sistema seja robusto para paper trading e futura migração para mainnet.

**Versões do projeto:**
- `src/` — Legado (v1): código monolítico, difícil de testar
- `refactored/` — v2: modular com DI, EventBus, containers
- `clean/` — v3: Clean Architecture completa (Domain → Application → Interface Adapters → Infrastructure)
- **Final Unificada** — merge otimizado das 3 versões

---

## 1. Bugs Conhecidos do Legado — Estado e Como Testar

### 1.1 `_is_price_sane` Attribute Missing

**Descrição:** O método `_is_price_sane()` da classe `DataAggregator` era chamado em `_fetch_hyperliquid()`, mas em certas condições (import circular, ordem de definição no ficheiro, ou problema de indentação) o método não estava acessível, resultando em `'DataAggregator' object has no attribute '_is_price_sane'`.

**Impacto:** O bot falhava a validar preços da API, podendo aceitar preços corruptos ou falhar completamente o fetch.

**Como foi corrigido na v2/v3:**
- Na `refactored/`, o `DataAggregator` foi reescrito com o método claramente definido antes de ser chamado
- Na `clean/`, a validação de preço foi movida para o `HyperliquidAPIGateway` como função pura `_validate_price()`

**Testes para verificar correção:**
```python
# tests/test_price_validation.py

class TestPriceSanityMethodExists:
    def test_is_price_sane_method_exists(self, mock_config):
        """Garantir que _is_price_sane está acessível como método de instância."""
        from data_aggregator import DataAggregator
        agg = DataAggregator(mock_config)
        # NÃO deve levantar AttributeError
        assert hasattr(agg, '_is_price_sane')
        assert callable(getattr(agg, '_is_price_sane'))
    
    def test_is_price_sane_called_from_fetch_hyperliquid(self, mock_config):
        """Simular fetch_hyperliquid e verificar que _is_price_sane é chamado."""
        from unittest.mock import patch, MagicMock
        agg = DataAggregator(mock_config)
        
        with patch.object(agg, '_is_price_sane', wraps=agg._is_price_sane) as mock_sane:
            with patch.object(agg.session, 'post') as mock_post:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {'BTC': 85000.0}
                mock_post.return_value = resp
                agg._fetch_hyperliquid('BTC')
                mock_sane.assert_called()
```

### 1.2 Valores BTC Errados

**Descrição:** A API da Hyperliquid retornava valores completamente aleatórios ou errados para BTC (ex: $0.15, $999999, ou strings não-numéricas). O parse dos diferentes endpoints (`allMids`, `metaAndAssetCtxs`) tinha falhas de type coercion e fallback inadequado.

**Impacto:** Dashboard mostrava preços absurdos; cálculos de PnL, stop-loss e position size ficavam completamente errados.

**Como foi corrigido na v2/v3:**
- Multi-layer fallback: `allMids` → `metaAndAssetCtxs` (midPx → markPx → oraclePx) → cache → 0
- Validação `_is_price_sane()` com ranges por asset (BTC: $10K-$200K)
- Logging detalhado em cada etapa do fallback

**Testes para verificar correção:**
```python
# tests/test_hyperliquid_fetching.py

class TestBTCErroneousValues:
    def test_btc_price_zero_rejected(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {'BTC': 0}
            mock_post.return_value = resp
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 0 or result['mark_price'] > 10000
    
    def test_btc_price_string_parsed(self, mock_config):
        """API pode retornar string — garantir conversão float correta."""
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {'BTC': '85000.5'}
            mock_post.return_value = resp
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85000.5
    
    def test_btc_price_insane_fallback_to_meta_ctxs(self, mock_config):
        """Se allMids retorna preço insano, deve fallback para metaAndAssetCtxs."""
        def side_effect(*args, **kwargs):
            resp = MagicMock()
            payload = kwargs.get('json', {})
            if payload.get('type') == 'allMids':
                resp.status_code = 200
                resp.json.return_value = {'BTC': 999999.0}  # Insane
            elif payload.get('type') == 'metaAndAssetCtxs':
                resp.status_code = 200
                resp.json.return_value = [
                    {'universe': [{'name': 'BTC'}]},
                    [{'midPx': '85000.0'}]
                ]
            return resp
        
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post', side_effect=side_effect):
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85000.0
    
    def test_btc_all_methods_fail_returns_zero(self, mock_config):
        """Se todas as APIs falham e não há cache, retornar mark_price=0."""
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            mock_post.return_value.status_code = 503
            result = agg._fetch_hyperliquid('BTC')
            assert result is not None
            assert result['mark_price'] == 0
```

### 1.3 Dashboard Não Interativa

**Descrição:** A dashboard web (Flask + HTML/JS) não respondia a cliques — botões "Start/Stop Bot", "Force Long/Short", "Emergency Close" não tinham handlers JavaScript ligados ao backend. O `app_flask.py` tinha endpoints API mas o frontend não os chamava corretamente.

**Impacto:** Utilizador não conseguia controlar o bot pela interface gráfica.

**Como foi corrigido na v2/v3:**
- `app_flask.py`: endpoints REST claros (`/api/start`, `/api/stop`, `/api/force_long`, etc.)
- `bridge.js`: camada de bridge entre dashboard standalone e Flask API
- WebSocket / Server-Sent Events para updates em tempo real (na v3 clean)

**Testes para verificar correção:**
```python
# tests/test_dashboard_interactivity.py

class TestDashboardInteractivity:
    def test_api_start_endpoint_returns_ok(self, client):
        """POST /api/start deve iniciar o bot e retornar status."""
        resp = client.post('/api/start')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('success') is True or data.get('running') is True
    
    def test_api_stop_endpoint_returns_ok(self, client):
        """POST /api/stop deve parar o bot."""
        resp = client.post('/api/stop')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get('success') is True or data.get('running') is False
    
    def test_api_status_returns_current_state(self, client):
        """GET /api/status deve retornar estado completo do bot."""
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'running' in data
        assert 'price' in data
        assert 'position' in data
    
    def test_dashboard_html_has_click_handlers(self):
        """Verificar que o HTML/JS tem event listeners nos botões."""
        from pathlib import Path
        dashboard_path = Path('src/dashboard_web.html')  # ou similar
        if dashboard_path.exists():
            html = dashboard_path.read_text()
            assert 'addEventListener' in html or 'onclick' in html
            assert '/api/start' in html or 'startBot' in html
```

### 1.4 Erros de Encoding (charmap)

**Descrição:** No Windows com console em `cp1252`, mensagens de log com emojis (📡, ⚠️, ✅) e newlines (`\n` no início da string de log) causavam `UnicodeEncodeError: 'charmap' codec can't decode byte 0x9d in position 4035`.

**Impacto:** O bot crashava no arranque ou durante o loop principal, impossibilitando execução em Windows.

**Como foi corrigido na v2/v3:**
- `utils.py`: `setup_logging()` configura `PYTHONIOENCODING=utf-8` **antes** de criar handlers
- `main.py`: remove leading `\n` das mensagens de log
- Uso de `sys.stdout.reconfigure(encoding='utf-8')` no Python 3.7+

**Testes para verificar correção:**
```python
# tests/test_encoding.py

class TestWindowsEncoding:
    def test_log_with_emoji_does_not_crash(self, mock_config, caplog):
        """Garantir que logs com emoji não levantam UnicodeEncodeError."""
        import logging
        logger = logging.getLogger('test_encoding')
        try:
            logger.info("📡 Analisando BTC...")
            logger.warning("⚠️ Preço não disponível")
            logger.info("✅ API OK")
        except UnicodeEncodeError:
            pytest.fail("UnicodeEncodeError ao logar com emoji — Windows crash!")
    
    def test_setup_logging_configures_utf8(self, tmp_path):
        """Verificar que setup_logging() configura UTF-8 corretamente."""
        from utils import setup_logging
        import sys, os
        log_file = tmp_path / 'test.log'
        setup_logging(level='INFO', log_file=str(log_file))
        # Em Windows, PYTHONIOENCODING deve ser 'utf-8'
        if sys.platform == 'win32':
            assert os.environ.get('PYTHONIOENCODING') == 'utf-8'
    
    def test_no_leading_newline_in_log_messages(self):
        """Verificar que nenhuma mensagem de log começa com \\n."""
        import ast
        from pathlib import Path
        src_files = list(Path('src').rglob('*.py'))
        violations = []
        for f in src_files:
            try:
                tree = ast.parse(f.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        for kw in node.keywords:
                            if kw.arg == 'msg' or kw.arg is None:
                                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                    if kw.value.value.startswith('\n'):
                                        violations.append(f"{f}:{kw.lineno}")
            except SyntaxError:
                continue
        assert len(violations) == 0, f"Log messages with leading newline found: {violations}"
```

---

## 2. Testes Unitários por Camada

### 2.1 Camada Domain (`clean/domain/`)

A camada Domain contém entities, events, repositories (interfaces) e services (interfaces). Não deve ter dependências externas.

```
tests/unit/domain/
├── test_entities.py
├── test_events.py
└── test_services.py
```

#### 2.1.1 Entities (`domain/entities/`)

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_candle_creation` | Criar Candle com OHLCV | `candle.close == 85000.0` |
| `test_candle_invalid_price` | Preço negativo deve rejeitar | `raises ValueError` |
| `test_signal_creation` | Criar Signal (LONG/SHORT/HOLD) | `signal.direction == 'LONG'` |
| `test_signal_timestamp_auto` | Timestamp auto-gerado | `signal.timestamp > 0` |
| `test_position_creation` | Criar Position com entry_price | `position.entry_price == 80000.0` |
| `test_position_pnl_long` | PnL long positivo | `position.unrealized_pnl(88000) > 0` |
| `test_position_pnl_short` | PnL short positivo | `position.unrealized_pnl(72000) > 0` |
| `test_trade_creation` | Criar Trade completo | `trade.fee > 0` |
| `test_market_snapshot_aggregation` | Agregar múltiplos dados | `snapshot.is_valid()` |

#### 2.1.2 Events (`domain/events/`)

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_signal_generated_event` | Evento SignalGenerated | `event.type == 'SIGNAL_GENERATED'` |
| `test_trade_executed_event` | Evento TradeExecuted | `event.trade_id is not None` |
| `test_position_opened_event` | Evento PositionOpened | `event.position.direction == 'LONG'` |
| `test_position_closed_event` | Evento PositionClosed | `event.pnl is not None` |
| `test_event_timestamp` | Todos os eventos têm timestamp | `event.timestamp > 0` |

#### 2.1.3 Domain Services (interfaces)

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_market_data_provider_interface` | MarketDataProvider é abstract | `hasattr(provider, 'fetch_price')` |
| `test_exchange_gateway_interface` | ExchangeGateway é abstract | `hasattr(gateway, 'place_order')` |

### 2.2 Camada Application (`clean/application/`)

A camada Application contém use cases, DTOs e interfaces (ports). Depende apenas de Domain.

```
tests/unit/application/
├── test_use_cases.py
├── test_dtos.py
└── test_interfaces.py
```

#### 2.2.1 Use Cases

| Teste | Use Case | Descrição | Mock |
|-------|----------|-----------|------|
| `test_fetch_market_data_success` | FetchMarketDataUseCase | Busca dados e publica evento | `MockMarketDataProvider` |
| `test_fetch_market_data_failure` | FetchMarketDataUseCase | Falha na API → evento de erro | `MockMarketDataProvider(raise_error=True)` |
| `test_generate_signal_long` | GenerateSignalUseCase | Condições LONG → SignalGenerated | `MockStrategyPort(return_value='LONG')` |
| `test_generate_signal_hold` | GenerateSignalUseCase | Condições neutras → HOLD | `MockStrategyPort(return_value=None)` |
| `test_generate_signal_persists` | GenerateSignalUseCase | Signal é persistido no repo | `MockSignalRepository` |
| `test_execute_trade_validates_risk` | ExecuteTradeUseCase | Valida risco antes de executar | `MockRiskManager` |
| `test_execute_trade_blocks_oversize` | ExecuteTradeUseCase | Position size > max → rejeita | `MockRiskManager(block=True)` |
| `test_execute_trade_publishes_event` | ExecuteTradeUseCase | Trade executado → evento | `MockEventPublisher` |
| `test_get_portfolio_status` | GetPortfolioStatusUseCase | Retorna capital, PnL, trades | `MockTradeRepository` |

#### 2.2.2 DTOs

| Teste | DTO | Descrição |
|-------|-----|-----------|
| `test_market_data_dto_from_dict` | MarketDataDTO | Criar DTO a partir de dict |
| `test_signal_dto_serialization` | SignalDTO | Serializar/deserializar |
| `test_trade_dto_fee_calculation` | TradeDTO | Fee incluído no DTO |
| `test_portfolio_status_dto_equity` | PortfolioStatusDTO | Equity = capital + unrealized_pnl |

### 2.3 Camada Infrastructure (`clean/infrastructure/`)

A camada Infrastructure contém frameworks (Flask, EventBus, logging). Depende de todas as camadas internas.

```
tests/unit/infrastructure/
├── test_flask_app.py
├── test_event_bus.py
├── test_strategy_adapter.py
└── test_main_composition.py
```

#### 2.3.1 Flask Web App

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_flask_routes_registered` | Todas as rotas existem | `'/api/signal/<asset>' in routes` |
| `test_api_signal_post` | POST /api/signal/BTC retorna sinal | `status_code == 200` |
| `test_api_trade_post` | POST /api/trade executa trade | `status_code == 200` |
| `test_api_status_get` | GET /api/status retorna estado | `'running' in response.json` |
| `test_static_folder_not_exposed` | `static_folder` não é '.' | `static_folder != '.'` |
| `test_cors_headers` | CORS configurado se necessário | `'Access-Control-Allow-Origin' in headers` |

#### 2.3.2 Event Bus

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_subscribe_and_publish` | Subscrever e publicar evento | `subscriber.called == True` |
| `test_multiple_subscribers` | Múltiplos subscribers | `all(s.called for s in subscribers)` |
| `test_unsubscribe` | Remover subscriber | `subscriber not in bus._subscribers` |
| `test_async_publish` | Publicação async não bloqueia | `elapsed < 0.1s` |
| `test_event_order_preserved` | Ordem de eventos preservada | `events[0].timestamp < events[1].timestamp` |

#### 2.3.3 Strategy Adapter

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_adapter_converts_signal` | GhostMethodStrategy → Signal entity | `signal.direction == 'LONG'` |
| `test_adapter_handles_none` | Strategy retorna None → HOLD | `signal is None` |
| `test_adapter_preserves_confidence` | Confidence passada pelo adapter | `signal.confidence == 0.85` |

#### 2.3.4 Composition Root

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_create_app_returns_all_components` | `create_app()` retorna dict completo | `len(components) == 12` |
| `test_create_app_wires_dependencies` | Todas as dependências ligadas | `app['fetch_uc'].gateway is app['gateway']` |
| `test_create_app_with_custom_config` | Config customizada aplicada | `app['web_app'].port == 8080` |

---

## 3. Testes de Integração

### 3.1 Integração API Hyperliquid

```
tests/integration/test_hyperliquid_api.py
```

| Teste | Descrição | Tipo |
|-------|-----------|------|
| `test_allmids_live_response` | Chamar API real `/info` com `type: allMids` | **LIVE** — requer internet |
| `test_metaandassetctxs_live_response` | Chamar API real `/info` com `type: metaAndAssetCtxs` | **LIVE** |
| `test_live_btc_price_sane` | Preço BTC da API real está dentro do range | **LIVE** |
| `test_live_eth_price_sane` | Preço ETH da API real está dentro do range | **LIVE** |
| `test_api_timeout_handling` | Simular timeout da API | Mock |
| `test_api_rate_limit_handling` | Simular 429 Too Many Requests | Mock |
| `test_api_html_response_handling` | Simular resposta HTML (Cloudflare block) | Mock |
| `test_api_malformed_json_handling` | Simular JSON inválido | Mock |

**Configuração LIVE:**
```bash
pytest tests/integration/test_hyperliquid_api.py -v -m live
```

**Configuração MOCK (CI):**
```bash
pytest tests/integration/test_hyperliquid_api.py -v -m "not live"
```

### 3.2 Integração SQLite

```
tests/integration/test_sqlite_repositories.py
```

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_candle_repository_crud` | Create, Read, Update, Delete de candles | `saved == retrieved` |
| `test_trade_repository_persistence` | Trade persistido e recuperado | `trade.fee == retrieved.fee` |
| `test_signal_repository_query_by_date` | Query signals por intervalo de datas | `len(signals) == expected` |
| `test_concurrent_writes` | Múltiplas threads a escrever | `no sqlite3.OperationalError` |
| `test_connection_pool_reuse` | Conexões reutilizadas | `conn1 is conn2` (mesma thread) |
| `test_wal_mode_enabled` | WAL mode para melhor concorrência | `journal_mode == 'wal'` |

### 3.3 Integração EventBus

```
tests/integration/test_eventbus_integration.py
```

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_end_to_end_signal_flow` | Fetch → Signal → Trade → DB | `trade in db` |
| `test_event_persistence` | Eventos persistidos para audit | `event_log.count > 0` |
| `test_cross_module_event_communication` | Module A publica, Module B recebe | `module_b.received == True` |
| `test_event_bus_with_real_threads` | EventBus com threads reais | `no race conditions` |
| `test_event_order_under_load` | 1000 eventos em burst | `order preserved` |

---

## 4. Testes de Sistema (End-to-End)

### 4.1 Paper Trading E2E

```
tests/system/test_paper_trading_e2e.py
```

| Teste | Descrição | Duração |
|-------|-----------|---------|
| `test_e2e_long_trade_lifecycle` | Entrar LONG → subir preço → trailing stop → sair | ~5s |
| `test_e2e_short_trade_lifecycle` | Entrar SHORT → descer preço → trailing stop → sair | ~5s |
| `test_e2e_stop_loss_triggered` | Entrar LONG → descer preço → stop-loss → sair | ~5s |
| `test_e2e_daily_limit_enforced` | 5 trades/dia → 6º trade rejeitado | ~2s |
| `test_e2e_bot_loop_5_cycles` | Loop principal 5 ciclos completos | ~25s |
| `test_e2e_capital_never_negative` | 100 trades perdedores → capital >= 0 | ~10s |
| `test_e2e_equity_curve_monotonic` | Equity history nunca desce abruptamente | ~5s |
| `test_e2e_all_exchanges_down_graceful` | Todas as APIs down → bot continua | ~5s |
| `test_e2e_config_reload_mid_run` | Alterar config durante execução | ~10s |

### 4.2 Sistema Completo (Bot + Dashboard)

```
tests/system/test_full_system.py
```

| Teste | Descrição | Assert |
|-------|-----------|--------|
| `test_start_bot_via_api` | POST /api/start → bot corre | `bot.running == True` |
| `test_stop_bot_via_api` | POST /api/stop → bot para | `bot.running == False` |
| `test_dashboard_receives_updates` | Dashboard mostra preços atualizados | `price > 0` |
| `test_dashboard_shows_position` | Dashboard mostra posição aberta | `position is not None` |
| `test_log_stream_to_dashboard` | Logs aparecem no dashboard | `len(logs) > 0` |
| `test_bot_restart_clean` | Parar e recomeçar sem erros | `no exceptions` |

---

## 5. Checklist de Edge Cases

### 5.1 Dados de Mercado

- [ ] Preço = 0
- [ ] Preço = -1
- [ ] Preço = NaN
- [ ] Preço = Infinity
- [ ] Preço = string vazia `""`
- [ ] Preço = string `"null"`
- [ ] Volume = 0
- [ ] Volume = -1
- [ ] Volume = 10^18 (overflow check)
- [ ] OI change = +1000%
- [ ] OI change = -1000%
- [ ] OI change = NaN
- [ ] Funding rate = 0.5 (extremo)
- [ ] Funding rate = -0.5 (extremo negativo)
- [ ] Todas as exchanges down simultaneamente
- [ ] Apenas 1 exchange funcional
- [ ] API retorna HTML em vez de JSON
- [ ] API retorna 503 Service Unavailable
- [ ] API retorna 429 Rate Limited
- [ ] Timeout de 30 segundos

### 5.2 Trading

- [ ] Entrar posição com capital = 0
- [ ] Entrar posição com price = 0
- [ ] Stop-loss com entry_price = 0 (ZeroDivisionError)
- [ ] Trailing stop com max_price = 0
- [ ] Daily trades = max_daily_trades + 1
- [ ] Daily trades counter não resetado após 24h
- [ ] Position size > max_position_size_usd
- [ ] Leverage > max_leverage
- [ ] Dois sinais simultâneos (LONG + SHORT)
- [ ] Sinal de entrada enquanto já em posição
- [ ] Sinal de saída sem posição aberta
- [ ] Fee calculation com position_size = 0

### 5.3 Configuração

- [ ] Ficheiro de config inexistente
- [ ] Config com YAML inválido
- [ ] Config com chave `bot` em falta
- [ ] Config com chave `risk` em falta
- [ ] Config com chave `strategy` em falta
- [ ] Config com `assets` = lista vazia
- [ ] Config com `assets` = `None`
- [ ] Config com `max_position_size_usd` = 0
- [ ] Config com `stop_loss_pct` = 0
- [ ] Config com `stop_loss_pct` = 1.0 (100%)
- [ ] Config com `max_leverage` = 0
- [ ] Config com `data_sources` = dict vazio

### 5.4 Concorrência & Threads

- [ ] `app_state` lido e escrito por múltiplas threads
- [ ] `app_state["logs"]` append simultâneo
- [ ] `_fast_price_check` e `run_cycle` simultâneos
- [ ] `_enter_position` e `_exit_position` simultâneos
- [ ] DB write durante price check (lock block)
- [ ] Monitor thread crash → bot continua?
- [ ] Flask request durante `app_state` update

### 5.5 Windows Específico

- [ ] Encoding cp1252 → logs com emoji
- [ ] Encoding cp1252 → prints com unicode
- [ ] Path com espaços (`C:\Users\User Name\...`)
- [ ] Path com caracteres especiais (acentos, ç)
- [ ] `python` não está no PATH
- [ ] `pip` não está no PATH
- [ ] Ficheiro `.bat` arranca o bot corretamente
- [ ] Tray icon funciona no Windows 10/11
- [ ] Webview (dashboard desktop) abre corretamente

---

## 6. Checklist de Bugs Conhecidos (Verificação Manual)

### 6.1 Data Aggregator

| # | Bug | Como verificar | Status |
|---|-----|----------------|--------|
| 1 | `_is_price_sane` attribute missing | Rodar `test_is_price_sane_method_exists` | ⬜ |
| 2 | Valores BTC completamente errados | Comparar preço do bot com CoinGecko/CoinMarketCap | ⬜ |
| 3 | `get_cached_price` retorna 0 para cache missing | Chamar com asset não existente → deve retornar `None` (não 0) | ⬜ |
| 4 | Fallback de API não funciona | Desligar internet → bot deve usar cache ou retornar 0 graciosamente | ⬜ |
| 5 | `fetch_all_data` não valida OI/volume negativo | Passar OI=-1 → deve ignorar ou logar warning | ⬜ |

### 6.2 Paper Trading

| # | Bug | Como verificar | Status |
|---|-----|----------------|--------|
| 6 | TOCTOU em `_fast_price_check` | Simular thread concurrente → verificar com `pytest-race` | ⬜ |
| 7 | TOCTOU em `run_cycle` | Simular entrada dupla → apenas 1 posição permitida | ⬜ |
| 8 | DB I/O bloqueia lock durante flash crash | Medir tempo de `_exit_position` com DB mock lento | ⬜ |
| 9 | `daily_trades` nunca reseta | Esperar 24h simuladas → counter deve resetar | ⬜ |
| 10 | `_check_exit_signals_fast` não valida `entry_price > 0` | Passar entry_price=0 → não deve crashar | ⬜ |
| 11 | AutoTuner thresholds crescem sem limite | Simular 100 trades perdedores → thresholds devem ser capped | ⬜ |

### 6.3 Bot Engine & Dashboard

| # | Bug | Como verificar | Status |
|---|-----|----------------|--------|
| 12 | `app_state` sem lock → race condition | 10 threads a ler/escrever → verificar consistência | ⬜ |
| 13 | `add_log` sem lock → logs perdidos | 100 threads a adicionar logs → contar total | ⬜ |
| 14 | Flask `static_folder='.'` expõe projeto inteiro | Aceder `http://localhost:5000/config/settings.yaml` → deve 404 | ⬜ |
| 15 | Dashboard não interativa (botões sem ação) | Clicar "Start Bot" → bot deve iniciar | ⬜ |
| 16 | `api_config` POST sem validação | Enviar JSON malformado → deve rejeitar com 400 | ⬜ |
| 17 | `os._exit(0)` sem cleanup | Parar bot → verificar se DB WAL está limpo | ⬜ |

### 6.4 Encoding & Windows

| # | Bug | Como verificar | Status |
|---|-----|----------------|--------|
| 18 | `UnicodeEncodeError` com emoji no Windows | Rodar no CMD do Windows → não deve crashar | ⬜ |
| 19 | Leading `\n` em mensagens de log | Fazer grep por `\\n` no início de strings em `src/` | ⬜ |
| 20 | `setup_logging` configura UTF-8 depois de criar handler | Verificar ordem de execução em `utils.py` | ⬜ |
| 21 | `config/settings.json` vs `.yaml` mismatch | App desktop grava JSON, backend lê YAML → deve ser consistente | ⬜ |

---

## 7. Cobertura de Código (Coverage)

**Meta:** 80%+ cobertura em `clean/`, 70%+ em `refactored/`, 60%+ em `src/`

```bash
# Gerar relatório de cobertura
coverage run -m pytest tests/
coverage html
coverage report --fail-under=70
```

**Arquivos críticos (requerem 90%+):**
- `clean/domain/entities/*.py`
- `clean/application/use_cases/*.py`
- `refactored/core/event_bus.py`
- `src/data_aggregator.py`
- `src/paper_trading.py`

---

## 8. Performance & Stress Tests

```
tests/stress/
├── test_eventbus_throughput.py
├── test_api_rate_limits.py
└── test_db_concurrent_load.py
```

| Teste | Descrição | Threshold |
|-------|-----------|-----------|
| `test_eventbus_10k_events` | 10.000 eventos em burst | < 1s |
| `test_db_1000_writes` | 1000 writes concorrentes | < 5s, 0 erros |
| `test_api_100_requests` | 100 requests HTTP ao Flask | < 10s, 0 timeouts |
| `test_memory_leak` | Rodar 1000 ciclos → memória estável | delta < 10MB |
| `test_bot_loop_1hour` | Simular 1 hora de execução | < 60s simulado |

---

## 9. Execução dos Testes

### 9.1 Windows (run_tests.bat)

Ver `scripts/run_tests.bat` — script completo com:
- Verificação de Python no PATH
- Instalação automática de dependências
- Execução por categoria
- Relatório HTML de cobertura
- Geração de badge de status

### 9.2 Linux/macOS

```bash
# Todos os testes
pytest tests/ -v

# Apenas unitários (rápido)
pytest tests/unit/ -v

# Apenas integração (requer internet para tests LIVE)
pytest tests/integration/ -v -m "not live"

# Apenas sistema (lento)
pytest tests/system/ -v

# Com cobertura
pytest tests/ --cov=clean --cov=refactored --cov=src --cov-report=html

# Stress tests
pytest tests/stress/ -v

# Apenas bugs conhecidos
pytest tests/ -v -k "price_sane or btc or encoding or dashboard or charmapp"
```

### 9.3 CI/CD (GitHub Actions / GitLab CI)

```yaml
# .github/workflows/tests.yml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ['3.11', '3.12', '3.14']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - run: pip install -r requirements.txt
      - run: pip install pytest pytest-cov pytest-race
      - run: pytest tests/unit tests/integration -v --cov --cov-fail-under=70
```

---

## 10. Sign-off Checklist (Antes de Mainnet)

- [ ] Todos os testes unitários passam (`pytest tests/unit -v` → 100% pass)
- [ ] Todos os testes de integração passam (MOCK)
- [ ] Testes LIVE da Hyperliquid passam (preço BTC/ETH realista)
- [ ] Todos os 21 bugs conhecidos verificados e marcados ✅
- [ ] Cobertura de código >= 70% global, >= 90% em arquivos críticos
- [ ] Stress test: 1000 ciclos sem crash, sem memory leak
- [ ] Teste de encoding no Windows: logs com emoji funcionam
- [ ] Dashboard interativo: Start/Stop/Force Long/Force Short/Emergency Close funcionam
- [ ] Paper trading: 30 dias simulados, capital nunca negativo
- [ ] Configuração: testnet/mainnet switch por ficheiro (sem botões desnecessários)
- [ ] Backup e restore da base de dados testado
- [ ] Documentação de troubleshooting atualizada (`docs/TROUBLESHOOTING.md`)

---

## 11. Estrutura Final de Testes (Tree)

```
trading-bot-hyperliquid/
├── tests/
│   ├── conftest.py                 # Fixtures compartilhadas
│   ├── __init__.py
│   │
│   ├── unit/                       # Testes unitários por camada
│   │   ├── domain/
│   │   │   ├── test_entities.py
│   │   │   ├── test_events.py
│   │   │   └── test_services.py
│   │   ├── application/
│   │   │   ├── test_use_cases.py
│   │   │   ├── test_dtos.py
│   │   │   └── test_interfaces.py
│   │   ├── infrastructure/
│   │   │   ├── test_flask_app.py
│   │   │   ├── test_event_bus.py
│   │   │   ├── test_strategy_adapter.py
│   │   │   └── test_main_composition.py
│   │   ├── refactored/
│   │   │   ├── test_event_bus_v2.py
│   │   │   ├── test_aggregator_v2.py
│   │   │   └── test_cache_v2.py
│   │   └── src/
│   │       ├── test_strategy.py       # (existente)
│   │       ├── test_data_aggregator.py # (existente)
│   │       ├── test_paper_trading.py   # (existente)
│   │       ├── test_risk_manager.py    # (existente)
│   │       └── test_edge_cases.py      # (existente)
│   │
│   ├── integration/                # Testes de integração
│   │   ├── test_hyperliquid_api.py
│   │   ├── test_sqlite_repositories.py
│   │   └── test_eventbus_integration.py
│   │
│   ├── system/                     # Testes end-to-end
│   │   ├── test_paper_trading_e2e.py
│   │   └── test_full_system.py
│   │
│   ├── stress/                     # Testes de performance
│   │   ├── test_eventbus_throughput.py
│   │   ├── test_api_rate_limits.py
│   │   └── test_db_concurrent_load.py
│   │
│   └── regression/                 # Testes de regressão (bugs conhecidos)
│       ├── test_price_sane_regression.py
│       ├── test_btc_values_regression.py
│       ├── test_dashboard_interactive_regression.py
│       └── test_encoding_regression.py
│
├── scripts/
│   └── run_tests.bat               # Script Windows completo
│
└── PLANO_TESTES.md                 # Este documento
```

---

*Documento gerado pelo REVISOR — Hyperliquid Trading Bot.*
*Última atualização: 2026-04-26*
