# 🛡️ MAINNET CHECKLIST V3 — Hyperliquid Trading Bot

**Data:** 2026-04-24  
**Scope:** Pós-major code changes (app_flask.py, bot_engine.py, mainnet_guardian.py)  
**Auditor:** MAINNET GUARDIAN V3

---

## RESUMO EXECUTIVO

| # | Item | Status | Risk |
|---|------|--------|------|
| 1 | Circuit breaker perda diária (5% soft / 10% hard) | ⚠️ NEEDS WORK | **CRITICAL** |
| 2 | Graceful shutdown (fecha posições) | ⚠️ NEEDS WORK | **HIGH** |
| 3 | Stop-loss na exchange (ordens reais) | ❌ FAIL | **CRITICAL** |
| 4 | Validação de ordens (tamanho, slippage, margem) | ⚠️ NEEDS WORK | **HIGH** |
| 5 | Switch testnet/mainnet com confirmação | ⚠️ NEEDS WORK | **CRITICAL** |
| 6 | Nunca enviar ordens reais em testnet | ⚠️ NEEDS WORK | **CRITICAL** |
| 7 | Wallet private keys NUNCA hardcoded | ✅ PASS | LOW |
| 8 | Rate limiting nas APIs | ⚠️ NEEDS WORK | MEDIUM |
| 9 | Handling de erro da exchange | ⚠️ NEEDS WORK | **HIGH** |
| 10 | Audit trail completo (logs de ordens) | ⚠️ NEEDS WORK | MEDIUM |

**Final Verdict: ❌ NO-GO**

> O bot **nunca foi testado com dinheiro real** e falha em 7 dos 10 critérios críticos. O mainnet_guardian.py existe como módulo standalone mas **não está integrado** no pipeline de execução. O exchange_client.py é um stub sem execução real. Paper trading funciona, mas mainnet requer trabalho substancial.

---

## 1. ✅ Circuit Breaker de Perda Diária

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **CRITICAL**  
**Evidence:**
- `src/mainnet_guardian.py` linhas 431-530 — `CircuitBreaker` com soft stop 5% (`DAILY_LOSS_SOFT_PCT = 0.05`) e hard stop 10% (`DAILY_LOSS_HARD_PCT = 0.10`).
- Reset diário automático em `check()`.
- **PROBLEMA:** `CircuitBreaker` **não está instanciado nem usado** em `bot_engine.py`, `paper_trading.py`, ou `app_flask.py`. É um módulo morto.
- `config/settings.yaml` linha 38-39: `daily_loss_limit_pct: 0.05`, `daily_loss_hard_stop_pct: 0.10` — config existe mas não é consumida.

**Action Needed:**
- [ ] Instanciar `CircuitBreaker` dentro de `BotEngine.__init__()`
- [ ] Chamar `circuit_breaker.check()` no loop principal (`_run()`) antes de cada ciclo de trading
- [ ] Integrar com `PaperTrader` para que pare de abrir posições quando ativado
- [ ] Exportar estado do circuit breaker no dashboard (soft/hard stop, PnL diário)

---

## 2. ✅ Graceful Shutdown

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **HIGH**  
**Evidence:**
- `src/paper_trading.py` linhas 335-369 — `_setup_signal_handlers()` captura `SIGINT` e `SIGTERM`, define `_shutdown_requested = True`, e tenta fechar posição aberta antes de sair.
- **PROBLEMA 1:** `bot_engine.py` **não usa** `GracefulShutdown` do `mainnet_guardian.py`. Quando `stop()` é chamado, apenas seta `_stop_event` e chama `trader.stop_monitoring()` — **não força fecho de posição**.
- **PROBLEMA 2:** `app_flask.py` linha 175 (`on_quit`) chama `api_stop()` → `stop_bot_engine()` → `trader.stop()` — mas se houver posição aberta, **não é fechada automaticamente**. O `os._exit(0)` mata o processo sem garantir cleanup.
- **PROBLEMA 3:** `GracefulShutdown` do `mainnet_guardian.py` (linhas 533-620) é sofisticado (timeout 30s, callbacks, atexit) mas **nunca é usado**.

**Action Needed:**
- [ ] Em `BotEngine.stop()`, verificar se `trader.current_position` está aberta e forçar `close_position()` antes de parar
- [ ] Em `app_flask.py`, `on_quit()` deve aguardar confirmação de fecho de posição com timeout
- [ ] Integrar `GracefulShutdown` do mainnet_guardian no `BotEngine` e expor callback de fecho de posição

---

## 3. ✅ Stop-loss na Exchange

**Status:** ❌ FAIL  
**Risk Level:** **CRITICAL**  
**Evidence:**
- `src/exchange_client.py` linha 129-153 — `place_stop_loss_order()` existe mas é **TODO**:
  ```python
  # TODO: Enviar stop-loss order real à Hyperliquid
  raise NotImplementedError("Stop-loss real ainda não implementado")
  ```
- O `PaperTrader` (`paper_trading.py` linhas 616-631, 859-910) tem **stop-loss interno** (trailing stop, stop loss fixo a 2%) mas isto é **só simulação**. Se o bot crashar ou o PC perder internet, a posição real na Hyperliquid **fica sem proteção**.
- Não existe código que envie ordem `stop-market` ou `stop-limit` à Hyperliquid API.

**Action Needed:**
- [ ] Implementar `place_stop_loss_order()` em `exchange_client.py` com assinatura correta para Hyperliquid
- [ ] Após abrir posição real, **obrigatório** colocar stop-loss na exchange dentro de 1 segundo
- [ ] Guardar ID da ordem de stop-loss para cancelar/atualizar se a posição for aumentada
- [ ] Validar que o stop-loss foi aceite pela exchange antes de considerar posição "protegida"

---

## 4. ✅ Validação de Ordens

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **HIGH**  
**Evidence:**
- `src/mainnet_guardian.py` linhas 654-820 — `OrderValidator` com checks completos:
  - Tamanho mínimo $10 (`MIN_ORDER_SIZE_USD`)
  - Tamanho máximo $100k (`MAX_ORDER_SIZE_USD`)
  - Slippage máximo 0.5% (`MAX_SLIPPAGE_PCT`)
  - Desvio de preço máximo 2% (`MAX_PRICE_DEVIATION_PCT`)
  - Margem suficiente
  - Leverage dentro do limite
- **PROBLEMA:** `OrderValidator` **não está usado** em `paper_trading.py`, `bot_engine.py`, ou `exchange_client.py`.
- `paper_trading.py` linha 163-170 — `_enter_position()` calcula `position_size` diretamente sem validar contra limites:
  ```python
  position_size = min(max_position_size, capital * 0.9)
  ```
  Não verifica `MIN_ORDER_SIZE_USD`, não valida margem, não verifica slippage.

**Action Needed:**
- [ ] Integrar `OrderValidator.validate()` em `PaperTrader._enter_position()`
- [ ] Rejeitar ordem se `is_valid = False` e logar motivo no audit trail
- [ ] No real trading, validar **antes** de assinar e enviar à exchange
- [ ] Adicionar check de margem disponível via `exchange_client.get_balance()`

---

## 5. ✅ Switch Testnet/Mainnet com Confirmação Explícita

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **CRITICAL**  
**Evidence:**
- `src/mainnet_guardian.py` linhas 284-395 — `NetworkGate` implementa **dois passos**:
  1. `mainnet_enabled: true` no config
  2. Ficheiro `.mainnet_approved` na root do projeto
  3. Ficheiro `.mainnet_blocked` pode bloquear emergencialmente
- `config/settings.yaml` linha 46: `mainnet_confirm_required: true`
- **PROBLEMA:** `NetworkGate` **não é usado** em `bot_engine.py`, `paper_trading.py`, ou `app_flask.py`.
- `bot_engine.py` inicia o bot sem verificar `can_trade_real()`. Se `paper_trading: false` for setado no config, o bot tentaria enviar ordens reais sem passar pela gate.
- `app_flask.py` endpoint `/api/config` permite alterar config via POST **sem validação de rede**.

**Action Needed:**
- [ ] Instanciar `NetworkGate` em `BotEngine.__init__()`
- [ ] Chamar `network_gate.can_trade_real()` no startup — bloquear se não passar
- [ ] No endpoint `/api/config` do Flask, **recusar** mudança para `paper_trading: false` sem confirmação explícita (ex: modal no dashboard)
- [ ] Dashboard deve mostrar banner vermelho quando em modo que pode executar reais
- [ ] Criar comando CLI: `python -m mainnet_guardian approve_mainnet`

---

## 6. ✅ Nunca Enviar Ordens Reais em Testnet

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **CRITICAL**  
**Evidence:**
- `exchange_client.py` é um **stub** — nunca envia ordens reais (levanta `NotImplementedError`).
- **PROBLEMA:** Quando `exchange_client.py` for implementado para real trading, **não existe proteção** que impeça enviar ordens com dinheiro real quando a URL é testnet, ou vice-versa.
- `NetworkGate` tem `get_network()` que retorna 'paper', 'testnet', ou 'mainnet', mas não está integrado.
- Não existe verificação de URL da API (testnet vs mainnet) antes de enviar ordens.

**Action Needed:**
- [ ] No `exchange_client.py`, verificar `network_gate.get_network()` antes de cada ordem:
  - Se 'testnet' → usar `https://api.hyperliquid-testnet.xyz`
  - Se 'mainnet' → usar `https://api.hyperliquid.xyz`
  - Se 'paper' → **nunca** chamar API real, redirecionar para `PaperTrader`
- [ ] Implementar `assert_network_consistency()` que verifica se a URL da API corresponde à rede declarada
- [ ] Log CRITICAL se houver mismatch (ex: mainnet config + testnet URL)

---

## 7. ✅ Wallet Private Keys NUNCA Hardcoded

**Status:** ✅ PASS  
**Risk Level:** LOW  
**Evidence:**
- Não existem private keys, seeds, ou mnemonics hardcoded em nenhum ficheiro `.py`.
- `src/mainnet_prep.py` linha 59-60 tem **placeholders** (`wallet_address="0x..."`, `private_key="0x..."`) como documentação de como usar — não são valores reais.
- `exchange_client.py` linha 105-107: TODO para implementar com wallet + assinatura, mas não hardcoded.

**Action Needed:**
- None. Manter regra: private keys via ficheiro `.env` ou wallet externa (MetaMask / hardware wallet).

---

## 8. ✅ Rate Limiting nas APIs

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** MEDIUM  
**Evidence:**
- `src/mainnet_guardian.py` linhas 430-500 — `RateLimiter` com token bucket por exchange:
  - Binance: 20 req/s
  - Bybit: 50 req/s
  - OKX: 20 req/s
  - Hyperliquid: 10 req/s (conservador)
- **PROBLEMA:** `RateLimiter` **não é usado** em `data_aggregator.py` ou `exchange_client.py`.
- `data_aggregator.py` tem `retry_on_failure` decorator (linhas 14-31) com backoff exponencial (2s → 4s → 8s), mas isto é **retry**, não **rate limiting preventivo**.
- `data_aggregator.py` usa `requests.Session()` mas não integra com `RateLimiter.wait_if_needed()`.
- `bot_engine.py` busca preço a cada 5 segundos (`price_interval = 5`) — sem rate limit, pode exceder limites da Hyperliquid.

**Action Needed:**
- [ ] Integrar `RateLimiter` em `DataAggregator` — chamar `wait_if_needed('hyperliquid')` antes de cada request
- [ ] Integrar `RateLimiter` em `exchange_client.py` para requests de trading
- [ ] No dashboard, mostrar estatísticas de rate limit (requests/min, erros 429)
- [ ] Considerar cache mais agressivo para preço (ex: 10s em vez de 5s)

---

## 9. ✅ Handling de Erro da Exchange

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** **HIGH**  
**Evidence:**
- `data_aggregator.py` tem bom handling para dados de mercado:
  - `retry_on_failure` com 3 tentativas e backoff exponencial
  - `_safe_json()` valida HTML vs JSON, respostas vazias, status codes
  - `_is_price_sane()` valida preços fora de range (ex: BTC < $10k ou > $200k)
  - Fallback para cache de 2 minutos se APIs falharem
- **PROBLEMA 1:** `exchange_client.py` é stub — não existe handling de erros de **execução de ordens** (rejection, insufficient margin, rate limit, invalid signature, etc.).
- **PROBLEMA 2:** Não existe lógica para:
  - `insufficient margin` → reduzir tamanho da ordem ou parar
  - `order rejected` → logar e notificar
  - `position liquidated` → parar bot imediatamente
  - `API downtime` → modo degradado (só monitorizar, não tradear)
- **PROBLEMA 3:** `bot_engine.py` linha 153-158 — `try/except` genérico no loop principal mas sem distinção de erros críticos vs transientes.

**Action Needed:**
- [ ] Implementar `ExchangeError` hierarchy em `exchange_client.py`:
  - `InsufficientMarginError` → parar trading, notificar
  - `OrderRejectedError` → logar, esperar 1 ciclo, tentar novamente
  - `RateLimitError` → backoff exponencial, respeitar `Retry-After`
  - `LiquidationError` → **hard stop imediato**, requer restart manual
- [ ] No `BotEngine._run()`, distinguir `Exception` críticos (parar bot) de transientes (continuar)
- [ ] Testar com Hyperliquid testnet antes de mainnet

---

## 10. ✅ Audit Trail Completo (logs de todas as ordens)

**Status:** ⚠️ NEEDS WORK  
**Risk Level:** MEDIUM  
**Evidence:**
- `src/mainnet_guardian.py` linhas 730-810 — `AuditLogger` guarda eventos em JSONL (`logs/audit.jsonl`):
  - Timestamp ISO, event name, level (INFO/WARNING/CRITICAL), details dict
- `src/database.py` — SQLite com tabela `trades` que guarda:
  - entry_price, exit_price, entry_time, exit_time, pnl_usd, exit_reason, strategy_params
- **PROBLEMA 1:** `AuditLogger` **não é usado** em `bot_engine.py` ou `paper_trading.py`.
- **PROBLEMA 2:** `paper_trading.py` linha 163 — `_enter_position()` guarda trade na DB mas **não loga evento de auditoria estruturado**.
- **PROBLEMA 3:** Não existe log de:
  - Tentativas de ordem rejeitadas
  - Mudanças de config (especialmente `paper_trading` → `false`)
  - Ativação de circuit breaker
  - Shutdowns (graceful ou forçado)
  - Mudanças de rede (testnet → mainnet)
- **PROBLEMA 4:** `app_state["logs"]` em `bot_engine.py` mantém só últimos 1000 logs em memória — se o processo crashar, perde-se histórico recente.

**Action Needed:**
- [ ] Integrar `AuditLogger` em `BotEngine`:
  - Logar cada tentativa de ordem (mesmo que simulada)
  - Logar cada fill (preço real, slippage)
  - Logar cada mudança de config
  - Logar ativação de circuit breaker ou emergency stop
- [ ] Garantir que `audit.jsonl` é flushed para disco a cada evento (append + fsync)
- [ ] Dashboard deve ter página de "Audit Trail" com filtros por data/evento
- [ ] Backup diário do `audit.jsonl` para diretório `logs/archive/`

---

## 🎯 FINAL VERDICT: ❌ NO-GO

### Justificação

O bot **funciona bem em paper trading** (coleta dados, gera sinais, simula trades, mostra dashboard), mas **não está pronto para dinheiro real** pelos seguintes motivos:

1. **Mainnet Guardian é um módulo órfão** — Todo o sistema de segurança (circuit breaker, order validation, network gate, rate limiter, audit logger) foi implementado em `mainnet_guardian.py` mas **nunca foi ligado** ao motor de execução. É como ter um airbag desligado.

2. **Exchange Client é stub** — Não existe execução real de ordens, não existe stop-loss na exchange, não existe handling de erros de trading. O bot crasha com `NotImplementedError` se tentar enviar ordens reais.

3. **Graceful shutdown incompleto** — O `paper_trading.py` tem signal handlers, mas o `bot_engine.py` e `app_flask.py` não garantem fecho de posições no shutdown via system tray ou API.

4. **Nenhuma proteção contra deploy acidental** — Mudar `paper_trading: true` → `false` no config faria o bot tentar tradear sem passar por confirmações ou verificação de rede.

### O que precisa de ser feito antes de GO:

| Prioridade | Tarefa | Ficheiro(s) |
|-----------|--------|-------------|
| P0 | Integrar `MainnetGuardian` no `BotEngine` | `bot_engine.py`, `paper_trading.py` |
| P0 | Implementar `exchange_client.py` para real trading | `exchange_client.py` |
| P0 | Implementar stop-loss real na Hyperliquid | `exchange_client.py` |
| P1 | Graceful shutdown completo no `app_flask.py` | `app_flask.py`, `bot_engine.py` |
| P1 | Proteção de config contra mudança acidental | `app_flask.py`, dashboard |
| P2 | Rate limiter preventivo | `data_aggregator.py` |
| P2 | Audit logger integrado | `bot_engine.py`, `paper_trading.py` |
| P2 | Testes end-to-end na testnet | `tests/` |

> **Recomendação:** Correr na **testnet** por pelo menos 1 semana com todas as integrações acima antes de considerar mainnet. Paper trading não valida execução real, latência, slippage, ou comportamento da API sob stress.

---

*Relatório gerado automaticamente pelo MAINNET GUARDIAN V3.*
