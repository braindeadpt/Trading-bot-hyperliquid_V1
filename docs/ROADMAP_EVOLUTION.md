# Roadmap de Evolução Gradual — Bot Hyperliquid

> **Princípio:** Não largar o momentum que funciona. Adicionar 1 componente de cada vez, validar, e só depois avançar.

---

## Fase A: Volume Profile Simples (AGORA — 1-2 semanas)

### Objetivo
Adicionar POC + Value Area como filtro de qualidade para entradas momentum.

### Conceito
- **POC** (Point of Control): Preço onde mais volume foi negociado
- **Value Area (VA)**: Faixa onde 70% do volume foi negociado
- **VAH/VAL**: Topo e base da Value Area

### Como funciona no bot
1. Calcular POC/VAH/VAL das últimas 24-48h de candles
2. **Filtro de entrada:**
   - LONG: Só entra se preço está **abaixo de VAL** (barato) ou **acima de VAH com breakout**
   - SHORT: Só entra se preço está **acima de VAH** (caro) ou **abaixo de VAL com breakdown**
3. **Evitar:** Entrar no "meio" da Value Area (preço justo = sem edge)

### Dados necessários
- Candles OHLCV que JÁ TEMOS ✅
- Zero dados adicionais necessários

### Implementação
- Novo módulo: `src/volume_profile.py`
- ~100-150 linhas
- Integração em `paper_trading.py`: 20 linhas

### Métricas de sucesso
- Win rate mantém-se ou aumenta (> 70%)
- Profit Factor mantém-se ou aumenta (> 2.0)
- Trades reduzem (mais seletivo = menos trades, melhor qualidade)

---

## Fase B: Delta de Volume em Candles (Mês 2)

### Objetivo
Adicionar "Orderflow Light" usando buy volume vs sell volume em candles.

### Conceito
- Cada candle tem volume total. Mas QUEM foi agressivo?
- Se candle fechou no topo → compradores agressivos dominaram
- Se candle fechou na base → vendedores agressivos dominaram
- **Delta estimado:** `(close - open) / (high - low) × volume`

### Como funciona no bot
1. Calcular delta estimado para cada candle de 15m
2. **Confirmação de entrada:**
   - LONG: Só entra se delta > 0 (compradores dominam)
   - SHORT: Só entra se delta < 0 (vendedores dominam)
3. **Filtro adicional:** Se oi_change confirmar a direção do delta → score +1

### Dados necessários
- Candles OHLCV que JÁ TEMOS ✅
- Não precisamos de tick data!

### Implementação
- Novo módulo: `src/delta_volume.py` (~80 linhas)
- Integração em `paper_trading.py`: +15 linhas

---

## Fase C: Regime AMT Simplificado (Mês 3)

### Objetivo
Detetar "balance" vs "imbalance" do mercado para ajustar thresholds.

### Conceito
- **Balance:** Mercado em range, POC estável, VA estreita
- **Imbalance:** Mercado em tendência, POC migrando, VA alargando
- **Transição:** Quando range de 4h rompe → imbalance começa

### Como funciona no bot
1. Calcular range das últimas 4h (ou 8 candles de 30m)
2. **Classificação:**
   - Range < 2% → BALANCE → thresholds MAIS STRICT (evitar whipsaws)
   - Range > 5% → IMBALANCE → thresholds MAIS LAXOS (aproveitar momentum)
3. **Ajuste dinâmico:**
   - Balance: volume_threshold +20%, oi_threshold +20%
   - Imbalance: volume_threshold -10%, oi_threshold -10%

### Implementação
- Extensão de `src/strategy.py`: +40 linhas
- Zero novos dados necessários

---

## Fase D: Orderbook Imbalance Ratio (Mês 4-5)

### Objetivo
Adicionar microestrutura REAL via L2 orderbook da Hyperliquid.

### Conceito
- Pedir os primeiros 5 níveis do orderbook
- Calcular: `imbalance_ratio = sum(bids_volume) / sum(asks_volume)`
- Ratio > 2:0 → pressão compradora forte
- Ratio < 0:5 → pressão vendedora forte

### Como funciona no bot
1. A cada 30s, buscar L2 snapshot (5 níveis)
2. **Confirmação final antes de entrar:**
   - LONG: imbalance_ratio > 1.5
   - SHORT: imbalance_ratio < 0.67
3. **Se L2 não disponível:** Fallback para delta de volume (Fase B)

### Dados necessários
- Hyperliquid REST API: `l2Book` endpoint ✅ (já existe)
- Custo: 1 pedido a cada 30s = trivial

### Implementação
- Novo módulo: `src/orderbook_imbalance.py` (~120 linhas)
- Integração: +20 linhas

---

## Fase E: WebSocket Real-Time (Mês 6+, opcional)

### Objetivo
Substituir REST polling por WebSocket para dados mais frescos.

### O que muda
- Preços: a cada 1-2s em vez de a cada 10-60s
- Orderbook: atualizações incrementais em vez de snapshots
- Latência: de 500ms para < 100ms

### COMPLEXIDADE: ALTA
- Reconexão automática
- Heartbeats
- Gestão de estado
- Debugging difícil

### Veredicto
- Só quando Pedro tiver confiança em Python async
- Só quando estratégia estiver lucrativa em testnet/mainnet
- Não é prioritário para 15m momentum

---

## Critérios de Transição entre Fases

| Fase | Critério para avançar |
|------|----------------------|
| A → B | 50+ trades com PF > 2.0 |
| B → C | 100+ trades, win rate > 65% |
| C → D | 200+ trades, Sharpe > 1.0 |
| D → E | Lucro consistente em testnet por 1 mês |

---

## Regras de Ouro

1. **Nunca remover o que funciona.** Só ADICIONAR filtros/confirmações.
2. **Cada fase deve melhorar ou manter métricas.** Se piorar, reverte.
3. **Paper trading entre cada fase.** Nunca testar 2 mudanças ao mesmo tempo.
4. **Documentar tudo.** Métricas antes/depois, o que mudou, porquê.

---

## Resumo

| Fase | Componente | Complexidade | Dados Novos | Tempo Est. |
|------|-----------|:------------:|:-----------:|:----------:|
| A | Volume Profile | 🟢 Baixa | ❌ Nenhum | 1-2 semanas |
| B | Delta Volume | 🟢 Baixa | ❌ Nenhum | 2-3 semanas |
| C | AMT Regime | 🟡 Média | ❌ Nenhum | 3-4 semanas |
| D | Orderbook L2 | 🟡 Média | ✅ l2Book | 1-2 meses |
| E | WebSocket | 🔴 Alta | ✅ WS | 2-3 meses |

**Total para "Orderflow Light":** ~4-6 meses, usando dados que já existem.
**Total para institutional-grade:** 1-2 anos, com dados L4 e hardware dedicado.
