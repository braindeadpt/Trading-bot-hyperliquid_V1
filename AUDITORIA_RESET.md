# RELATÓRIO DE AUDITORIA — RESET ESTRATÉGICO

## 🚨 Situação Atual: O Que Aconteceu

Temos **dois bots** em paralelo, nenhum funcional:

### 1. Código LEGADO (o que funcionava antes)
- `bot_engine.py` — entry point original
- `src/paper_trading.py` — paper trader com trailing stops, MTF, auto-tuner
- `src/data_aggregator.py` — busca OI de múltiplas exchanges (Binance, Bybit, OKX, Hyperliquid)
- `src/strategy.py` — MomentumStrategy (Ghost Method)
- `app_flask.py` — dashboard original

**Estado:** Funcionava em paper trading, tinha 5 bugs críticos que corrigimos numa sessão anterior.

### 2. Código REFATORADO v3 (o que quebrou tudo)
- `src/v3/main.py` — entry point "unificado" que integra 3 arquiteturas
- `refactored/` — EventBus, DI Container, State Machine, etc.
- `clean/` — Clean Architecture com Domain, Application, Use Cases
- `refactored/optimized/` — "otimizações" que não foram testadas no Windows

**Estado:** Quebrado. Imports que falham, paths errados, templates em falta, OI hardcoded a 0.

---

## ❌ Decisões que Pioraram Tudo

| Decisão | Porquê Foi Mau |
|---------|---------------|
| Criar `src/v3/main.py` | Tentou integrar 3 sistemas num só, criou mais problemas |
| Substituir imports relativos por absolutos | Funciona no Linux, quebra no Windows |
| Hardcoded `oi_change_pct = 0.0` | Estratégia nunca gera sinais |
| `refactored/` architecture | Overkill para bot pessoal de 1 utilizador |
| Múltiplos dashboards | `app_flask.py`, `webapp_v2.py`, templates HTML complexos |
| `.gitignore` ignorou `data/` | `refactored/data/` nunca foi enviado para o GitHub |

---

## ✅ Plano de RESET — Voltar ao Funcional

### Fase 1: Simplificar Entry Point (AGORA)
1. **Eliminar `src/v3/main.py`** — não funciona
2. **Voltar `start.bat` a apontar para `bot_engine.py`**
3. **Garantir que `bot_engine.py` funciona com paper trading**

### Fase 2: Corrigir APENAS o Legado (Hoje)
Aplicar no código LEGADO apenas estas correções:
1. ✅ `run_cycle()` no PaperTrader (já feito em `src/paper_trading.py`)
2. ✅ Circuit breaker diário (soft 5%, hard 10%)
3. ✅ Volume intraday real (Binance klines, não 24h/288)
4. ✅ OI agregado de múltiplas exchanges (já existia no legado!)
5. ✅ Strict OI check (não permitir OI=0)

**NÃO aplicar:** EventBus, DI Container, Clean Architecture, State Machine — tudo overkill.

### Fase 3: Dashboard Funcional (Hoje)
- Usar `app_flask.py` (original) ou criar dashboard minimalista
- Mostrar: preço, posição, capital, trades, status
- Sem Web Components complexos, sem CSS externo

### Fase 4: Testnet/Mainnet Config (Amanhã)
- Config por ficheiro YAML: `network: testnet` ou `network: mainnet`
- Se mainnet: pedir wallet address (não guardar secret keys no código!)
- Se testnet: paper trading com dados reais

### Fase 5: Testar (3 dias)
- Correr em paper trading por 72h
- Verificar se gera sinais e trades
- Ajustar thresholds se necessário

---

## 📋 Ficheiros a Manter vs. Eliminar

### MANTER (funcionam ou são essenciais)
```
bot_engine.py                    ← entry point
src/paper_trading.py             ← trader (com run_cycle fix)
src/data_aggregator.py           ← agregador (com OI de multiplas exchanges)
src/strategy.py                  ← Ghost Method
src/database.py                  ← SQLite
core/config/settings.yaml        ← config
app_flask.py                     ← dashboard (ou simplificar)
start.bat                        ← arranque Windows
```

### ELIMINAR (overhead/complexidade)
```
src/v3/                          ← arquitetura quebrada
refactored/                      ← overkill
clean/                           ← overkill
PLANO_*.md                       ← documentacao que nao ajuda a correr
verify_*.py                      ← testes de arquiteturas que nao usamos
```

---

## ⏱️ Estimativa de Tempo

| Tarefa | Tempo |
|--------|-------|
| Reset entry point (bot_engine.py) | 30 min |
| Verificar que paper trading funciona | 1h |
| Dashboard minimalista | 2h |
| Config testnet/mainnet | 1h |
| Testar 72h paper trading | 3 dias |
| **Total para bot funcional** | **~4h + 3 dias teste** |

---

## 💡 Lição Aprendida

**"Refatoração prematura é a raiz de todo o mal."**

Tentámos construir uma arquitetura enterprise (EventBus, DI, Clean Arch) para um bot pessoal de 1 utilizador. O resultado: um sistema que funciona em teoria (Linux, testes unitários) mas quebra na prática (Windows, deploy real).

**A regra é:** Faz o simples funcionar primeiro. Otimiza depois.

---

*Relatório honesto. Sem desculpas. Vamos ao trabalho.*
