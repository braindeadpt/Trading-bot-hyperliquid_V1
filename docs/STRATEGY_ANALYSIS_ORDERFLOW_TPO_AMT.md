# 📊 Análise de Estratégia: Orderflow + TPO + AMT

> **Pedro, isto é a análise que pediste — honesta, sem filtros, mas construtiva.**  
> Não vou destruir o teu entusiasmo. Mas também não vou deixar que te atires de cabeça para uma piscina sem água.

---

## 1. Resumo Executivo — GO ou NO-GO?

### Veredicto: 🛑 **NO-GO (para já)**

Pedro, a estratégia que descreveste é **teoricamente sólida**. Não estás louco. Orderflow + TPO + AMT é uma combinação que profissionais usam (ou tentam usar) em bancos de investimento e prop firms. O problema não é a estratégia — **o problema é o timing e o contexto**.

**Resumo em três linhas:**
- Tens 100€ de capital e estás a aprender Python do zero.
- Esta estratégia exige infraestrutura que custa milhares em dados e meses de desenvolvimento.
- A tua estratégia atual (momentum) já funciona, já tem edge validado, e ainda nem sequer a otimizaste ao máximo.

**Não é "não". É "não agora".**

---

## 2. Análise por Componente

### 2.1 Orderflow — O Monstro

#### O que é?

Orderflow analisa o **fluxo de ordens em tempo real** — quem está a comprar, quem está a vender, a que preços, e com que intensidade. O "DOM" (Depth of Market) mostra-te o livro de ordens nível por nível. O "Delta de Volume" subtrai volume vendedor do comprador em cada tick. Os "Imbalances" detectam quando há 3x mais compras que vendas num nível de preço.

#### O que precisas para implementar?

1. **WebSocket de trades em tempo real** — cada trade com preço, size e side (buy/sell).
2. **Reconstrução do orderbook L2** — Hyperliquid envia snapshots + deltas. Tens de manter o estado completo do livro na tua memória.
3. **Cálculo de Delta em tempo real** — agregar buys vs sells por nível de preço, janela deslizante.
4. **Deteção de Imbalances** — ratio 3:1 exige granularidade que a Hyperliquid **não dá de graça**.

#### O problema real:

| Aspeto | O que o Pedro precisa | O que a Hyperliquid dá |
|--------|----------------------|------------------------|
| Dados de trades tick-by-tick | Sim, em tempo real | ✅ WebSocket `trades` — SIM |
| Orderbook L2 completo | Sim, para DOM | ⚠️ Apenas 20 níveis por lado via API pública |
| Dados históricos de tick | Para backtestar o orderflow | ❌ **NÃO** — não há acesso fácil |
| Imbalance ratio 3:1 | Detetar concentração de ordens | ⚠️ Limitado — 20 níveis é pouco para DOM "real" |

**A Hyperliquid limita WebSocket a 1000 subscriptions por IP e 100 conexões.** Para uma só moeda (BTC), isso chega. Mas se quiseres expandir, torna-se problemático.

**O deal-breaker:** Não há dados históricos de tick-by-tick facilmente acessíveis para backtestar. Terias de correr em live durante meses para acumular dados — ou pagar por dados de terceiros (0xarchive, CoinAPI, etc.), o que com 100€ de capital não faz sentido.

#### Dificuldade: 🔴 **ALTA — Provavelmente a parte mais difícil de todo o projeto**

---

### 2.2 TPO (Time Price Opportunity / Market Profile)

#### O que é?

Criado por J. Peter Steidlmayer na Chicago Board of Trade (CBOT) nos anos 80. O TPO divide o dia em blocos de tempo ("TPOs") e conta quanto tempo o preço passou em cada nível. O resultado é um histograma que revela:

- **Value Area (VA)** — os preços onde 70% do tempo foi negociado.
- **Point of Control (POC)** — o preço com mais tempo negociado (o "preço justo").
- **Initial Balance (IB)** — o range dos primeiros períodos da sessão.

#### O problema para Crypto 24/7:

O TPO foi concebido para **mercados com sessões definidas** — a Bolsa de Chicago abria às 9h30 e fechava às 16h00. Crypto nunca fecha. Portanto, tens de inventar "sessões sintéticas".

**Sessões sintéticas comuns em crypto:**
- Sessão de 24h (UTC 00:00 a 23:59)
- Sessão de 8h (estilo forex: Ásia, Europa, América)
- Sessão volátil vs sessão calma

**Mas aqui está o problema:** o TPO funciona porque as sessões têm estrutura — abertura com volume, meio do dia calmo, fecho com decisões. Crypto 24/7 não tem essa estrutura. Um "P-shaped profile" em crypto pode ser apenas aleatoriedade, não acumulação institucional.

#### Dificuldade: 🟡 **MÉDIA-ALTA — Conceitos simples, adaptação para crypto é que é complicada**

---

### 2.3 AMT (Auction Market Theory)

#### O que é?

AMT é uma teoria de que o mercado alterna entre dois estados:
- **Balance** — o preço oscila dentro de um range (consolidação). O mercado está a "descobrir" o preço justo.
- **Imbalance** — o preço move-se direcionalmente. O mercado aceitou um novo preço e move-se para lá.

A tua state machine (BALANCE → TESTING_BREAKOUT → CONFIRMING_IMBALANCE) é uma tentativa de quantificar isto.

#### Os conceitos de Aceitação vs Rejeição:

- **Aceitação** — o preço testa um nível e fica lá. O mercado "aceita" o novo preço → continuação.
- **Rejeição** — o preço testa um nível e volta rapidamente. O mercado "rejeita" → reversão.

#### O problema:

AMT é **descritiva**, não prescritiva. É ótima para explicar o que aconteceu, mas terrível para prever o que vai acontecer. A linha entre "teste" e "breakout confirmado" só é óbvia **depois** de acontecer.

Transformar isto numa state machine quantitativa significa:
1. Definir thresholds numéricos para "balance" vs "imbalance"
2. Definir o que é "teste" vs "confirmação"
3. Lidar com falsos breakouts (o preço sai da VA e volta 30 segundos depois)

**Tudo isto requer calibração extensiva — e sem dados históricos de tick, não podes calibrar.**

#### Dificuldade: 🟠 **ALTA — A teoria é elegante, a quantificação é um pesadelo**

---

## 3. Complexidade de Implementação

### Comparativo: Estratégia Atual vs. Nova

| Aspeto | Estratégia Atual (Momentum) | Nova Estratégia (Orderflow+TPO+AMT) |
|--------|---------------------------|-----------------------------------|
| **Dados necessários** | OHLCV + OI + Funding (REST API) | WebSocket L2 + trades tick-by-tick |
| **Infraestrutura** | Python básico + requests | Python async + WebSocket + state management |
| **Backtesting** | Fácil — dados OHLCV históricos abundantes | Difícil — requer dados de tick, que não tens |
| **Debugging** | Fácil — os números são claros | Difícil — race conditions, WebSocket drops, estado do orderbook |
| **Tempo estimado** | 2-4 semanas para versão funcional | **3-6 meses** para versão mínima viável |
| **Manutenção** | Baixa — thresholds estáveis | Alta — constante ajuste de parâmetros |
| **Risco de bugs** | Baixo | Alto — orderflow bugs são silent killers |

### Skills que o Pedro precisaria desenvolver:

1. **Python async/await** — WebSockets requerem programação assíncrona. O teu bot atual é provavelmente síncrono.
2. **Gerenciamento de estado em tempo real** — o orderbook é um estado mutável que tem de ser consistente. Um packet perdido = dados corrompidos.
3. **Cálculo estatístico em tempo real** — Delta, VA, POC, desvios padrão, tudo em streaming.
4. **Debugging de sistemas distribuídos** — WebSocket drops, reconexões, sincronização de clocks.

### Riscos Técnicos Específicos:

| Risco | Probabilidade | Impacto |
|-------|--------------|---------|
| Packet loss no WebSocket corrompe o orderbook | Média | 🔴 Alto — sinais falsos |
| Latência na deteção de imbalance → entra tarde | Alta | 🟡 Médio — slippage |
| Overfitting nos thresholds de AMT | Muito Alta | 🔴 Alto — estratégia parece boa no backtest, morre em live |
| WebSocket rate limit (2000 msg/min) | Baixa | 🟡 Médio — dados atrasados |
| Dados históricos insuficientes para calibração | Garantido | 🔴 Alto — não podes validar |

---

## 4. Viabilidade em Hyperliquid

### O que a Hyperliquid API fornece:

| Dado | Disponível | Limitação |
|------|-----------|-----------|
| WebSocket trades (`trades`) | ✅ Sim | Tick-by-tick, mas sem histórico fácil |
| WebSocket L2 orderbook (`l2Book`) | ✅ Sim | Apenas 20 níveis por lado |
| WebSocket L4 orderbook (`l4Book`) | ✅ Sim | Requer autenticação, mas ainda limitado |
| Dados históricos de tick | ❌ Não | Não há endpoint para descarregar |
| Dados históricos OHLCV | ✅ Sim | Via REST, suficiente para TPO básico |
| OI + Funding | ✅ Sim | Já usas isto na estratégia atual |

### A realidade da latência:

A Hyperliquid é uma DEX rápida, mas não és o único a receber estes dados. HFTs e market makers profissionais têm:
- Colocation próximo dos servidores da Hyperliquid
- Infraestrutura dedicada (ver Dwellir, 0xarchive)
- Algoritmos otimizados em C++/Rust, não Python

**O teu edge de orderflow, se existir, está a competir com quem tem vantagem tecnológica de 10x.** Não é impossível, mas é como tentar ganhar uma corrida de F1 com um carro de rally.

### Dados históricos — o problema do elefante na sala:

Para backtestar orderflow, precisas de:
- Cada trade individual (price, size, side)
- Snapshots do orderbook a cada X milissegundos
- Histórico de pelo menos 6-12 meses

**Isso não existe de graça para a Hyperliquid.** Soluções pagas:
- 0xarchive: dados desde Abril 2023, mas é pago
- CoinAPI: tick data, mas caro para uso profissional
- Lighter.xyz: tem dados de tick, mas tier Enterprise

Com 100€ de capital, investir 50-100€/mês em dados históricos não faz sentido económico.

---

## 5. Lucratividade — Expectativas Realistas

### O que a literatura diz:

| Componente | Edge Real? | Tendência |
|-----------|-----------|-----------|
| **Orderflow** | Sim, mas a diminuir | 🔻 Mais HFTs = edge comprimido |
| **TPO** | Sim, em futuros tradicionais | ⚠️ Adaptação para crypto é não validada |
| **AMT** | Conceitualmente sim | ❌ Difícil de quantificar com rigor |
| **Combinado (confluência)** | Potencialmente sim | ⚠️ Mas overfitting é risco enorme |

### Comparativo com a estratégia atual:

| Métrica | Estratégia Atual (Momentum) | Nova Estratégia (Estimativa) |
|---------|---------------------------|------------------------------|
| **Tempo até primeiro backtest** | 1-2 semanas | 3-6 meses (sem dados históricos) |
| **Profit Factor esperado** | 2.50 (validado) | 1.5-2.0? (especulação) |
| **Win Rate esperado** | 72% (validado) | 55-65%? (especulação) |
| **Drawdown esperado** | <1% (backtest) | 5-15%? (não testado) |
| **Confiança estatística** | Alta — 36 trades, dados reais | **Zero** — não podes backtestar |

### A verdade honesta sobre o scoring system:

O teu sistema de scoring (+1/+1/+1 = entra) parece simples, mas:

1. **Quem define o que é "imbalance > 2σ"?** — Precisas de baseline histórico. Não tens.
2. **Quem define "nível de valor extremo"?** — A VA muda constantemente. Qual timeframe?
3. **Threshold ≥ +2** — Porque não +1.5? Ou +3? Isso é arbitrário e precisa de calibração.

**Sem backtest, isto é adivinhação com equações.**

---

## 6. Roadmap Alternativo — Como Aproveitar Isto Sem Afundar

Pedro, não quero que desistas da ideia. Quero que a **desconstruas** e aproveites as partes que fazem sentido para o teu nível atual.

### Fase A: Otimizar o que já tens (Próximos 2-3 meses)

A tua estratégia de momentum já funciona. Mas ainda há margem:
- **Ajustar thresholds por regime de mercado** (tendência vs range)
- **Adicionar análise de multi-timeframe** (confirmar em 1h o que se vê em 15m)
- **Implementar o backtesting com base de dados** — já falámos nisto
- **Dashboard web com métricas em tempo real** — já estamos a construir

### Fase B: Introduzir conceitos TPO simplificados (Meses 3-6)

Em vez de TPO completo, começa com:
- **Volume Profile** — histograma de volume por preço (mais fácil que TPO de tempo)
- **Níveis de POC e VA** — usar como zonas de suporte/resistência dinâmicas
- **Identificar range days vs trend days** — simples: se o range do dia for < 1.5x ATR, é "balance"

Isto dá-te 80% do valor do TPO com 20% do esforço.

### Fase C: Orderflow light (Meses 6-12)

Se ainda quiseres orderflow:
- Começa com **delta de volume em candles** (não tick-by-tick) — calcula buys vs sells dentro de cada candle de 15m
- Usa **imbalance do orderbook** — compara bid volume vs ask volume nos primeiros 5 níveis
- **NÃO uses WebSocket em tempo real** ainda — faz em batches a cada candle

### Fase D: AMT como filtro (Meses 9-12)

- Define "balance day" simples: range do dia < 1.5x ATR20
- Define "trend day": range do dia > 2.5x ATR20
- Usa isto como **filtro de regime** para a tua estratégia de momentum:
  - Em "balance day": não trades (evitar falsos breakouts)
  - Em "trend day": trades normais
  - Em "trend day" + volume spike: trades com size maior

### Fase E: Estratégia completa (Ano 2+)

Só quando:
- Já dominas Python async
- Tens base de dados com meses de dados de tick
- Já validaste Fase B, C e D em live com resultados positivos
- Tens capital suficiente para justificar a infraestrutura

---

## 7. Recomendação Final

### Para o Pedro, hoje:

**🛑 NÃO implementes esta estratégia agora.**

Não é porque é má. É porque:
1. **É demasiado complexa para o teu nível atual de Python.**
2. **Não tens dados históricos para backtestar.** Correr em live sem backtest é suicídio de capital.
3. **A tua estratégia atual ainda tem muito potencial por explorar.** Profit Factor 2.50 com 72% win rate — isto é ouro. A maioria dos traders profissionais não tem estes números.
4. **100€ de capital não justifica infraestrutura de orderflow.** Mesmo que a estratégia funcione, os custos de dados + tempo de desenvolvimento não se pagam.

### O que fazer em vez disso:

1. **Foca-te em fazer a estratégia atual ganhar dinheiro de verdade.** Paper trading → testnet → live com 100€. Provavelmente já estás perto.
2. **Usa o roadmap alternativo (Fase B) para introduzir TPO simplificado.** Volume Profile é mais acessível e dá valor real.
3. **Quando tiveres 6 meses de experiência em Python e capital de 500-1000€, reavaliamos.**
4. **Se quiseres mesmo explorar orderflow agora, usa ferramentas prontas:**
   - TradingView tem indicadores de Volume Profile e Delta (pago)
   - TensorCharts, Bookmap — plataformas de visualização de orderflow
   - Observa manualmente, aprende os padrões, e só depois automatiza

### A analogia:

Imagina que estás a aprender a conduzir. A tua estratégia atual é um carro de estrada — fiável, previsível, já funciona. A estratégia orderflow+TPO+AMT é um avião de caça. É mais rápido? Sim. Mas se ainda estás a aprender a fazer mudanças de velocidade, não te metas num cockpit.

**Domina o carro primeiro. O caça pode esperar.**

---

## Referências e Fontes

1. **Hyperliquid WebSocket API Docs** — Limites de 1000 subscriptions/IP, 100 conexões/IP, 2000 msg/min. Sem batch subscriptions.
2. **Dwellir Blog (2026)** — "Hyperliquid WebSocket Subscription Limits" — detalha os constraints técnicos.
3. **0xarchive / oxarchive Python SDK** — Dados históricos Hyperliquid desde Abril 2023, mas limitado a 20 níveis de profundidade na tier gratuita.
4. **Chainstack Docs** — Hyperliquid L2 book: máximo 20 níveis por lado via API pública.
5. **Evans & Lyons (2002)** — "Order Flow and Exchange Rate Dynamics" — modelo microestrutura FX que inspirou orderflow em crypto. R² de ~40% para USD, mas baixo poder explicativo para mercados finos (Yen-Bitcoin: 4-5%).
6. **Steidlmayer, J.P. (1985)** — "Markets and Market Logic" — fundação teórica do TPO/Market Profile.
7. **Dalton, J.** — "Mind Over Markets" — referência padrão para AMT e Market Profile em futuros tradicionais.
8. **Tickerly / Thrive.fi** — Confirma que overfitting e curve fitting são as armadilhas #1 em bots de crypto. Backtesting com dados de qualidade é essencial.

---

> **Nota final, Pedro:**
>
> A tua vontade de ir mais fundo é EXACTAMENTE o que separa traders que ganham de traders que perdem. Mas canaliza essa energia para o próximo passo lógico, não para um salto de fé de 10 metros.
>
> A estratégia que descreveste é **institutional-grade**. Quando a implementares — e um dia vais — vais sentir-te como um profissional. Mas hoje, **o melhor trade que podes fazer é não fazer este trade.**
>
> Bora construir sobre o que já funciona. 🔥

---

*Análise produzida a 25 de Abril de 2026.*  
*Para uso interno do projeto trading-bot-hyperliquid.*
