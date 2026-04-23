# 🖥️ Guia do Dashboard (dashboard.html)

> _O teu cockpit. Tudo o que precisas de saber para ler, controlar e dominar o dashboard._

---

## O que é o Dashboard?

O `dashboard.html` é uma página web que corre **no teu browser** (Chrome, Edge, Firefox). Não precisa de instalação — basta abrir o ficheiro.

É o teu **painel de controlo visual** do bot. Em vez de olhares para linhas de texto num terminal, tens:
- Números grandes e coloridos
- Gráficos
- Botões
- Logs organizados

E tem um estilo **cypherpunk / old-school terminal** — verde sobre preto, linhas de scan, e aquele "feel" de hacker dos anos 80. É funcional _e_ porreiro.

---

## Como Abrir

1. Abre o Explorador de Ficheiros
2. Vai à pasta do bot (`trading-bot-hyperliquid`)
3. Clica duas vezes em `dashboard.html`
4. Abre no teu browser predefinido

> 💡 **Dica:** Podes também arrastar o ficheiro `dashboard.html` para uma janela do browser já aberta.

---

## Layout — O que Cada Painel Mostra

O dashboard tem **3 colunas**:

### 🟩 Coluna da Esquerda — CONFIGURAÇÃO

Aqui defines **tudo** antes de correr.

#### Network (🌐)
O dropdown mais importante:
- **Testnet (Paper Trading)** 🟡 → Simulação. Capital virtual. Zero risco. Ideal para aprender.
- **Mainnet (Dinheiro Real)** 🟢 → Dinheiro REAL. Só para quando tiveres confiança total.

> 🧠 **Regra:** Quando mudas para Mainnet, os campos de ligação (carteira + chave privada) ficam ativos. Em Testnet, estão escondidos — não precisas de carteira.

#### Trading (📈)
- **Asset:** BTC ou ETH
- **Timeframe:** 15m (recomendado) ou 5m
- **Capital (USDC):** Quanto dinheiro virtual/tens na conta
- **Position Size (USD):** Quanto arriscas por trade
- **Leverage (x):** Alavancagem (1x = sem alavancagem, 2x = dobro)

#### Estratégia (🧠)
- **Vol Threshold:** Quão grande o spike de volume precisa de ser (4.0 = 4x a média)
- **OI Threshold:** Quanto o OI precisa de subir (1.0%)
- **Stop Loss (%):** Quanto perdes antes de sair automaticamente (2.0%)
- **Trailing Stop (%):** Quanto abaixo do máximo o stop segue (1.5%)

> 💾 **Guardar / Exportar:** Os botões no fundo guardam as tuas definições no browser (localStorage) ou exportam para um ficheiro JSON.

---

### 🟨 Coluna do Centro — MERCADO, POSIÇÃO, EQUITY, CONTROLO

#### 📊 Mercado em Tempo Real

8 cartões com dados ao vivo da Hyperliquid:

| Cartão | O que significa |
|--------|-----------------|
| **Preço** | Preço atual (mid price) |
| **Mark Price** | Preço de marcação usado para PnL |
| **Oracle** | Preço do oracle (referência externa) |
| **OI** | Open Interest em contratos |
| **OI ($)** | Open Interest em dólares |
| **Funding** | Taxa de funding atual |
| **Volume 24h** | Volume negociado nas últimas 24h |
| **24h Change** | Variação percentual nas últimas 24h |

> 🟢 Valores a verde = positivo. 🔴 Valores a vermelho = negativo.

#### 🎯 Posição Atual

Só aparece quando tens uma posição aberta:

| Cartão | O que significa |
|--------|-----------------|
| **Direção** | LONG (aposta a subir) ou SHORT (aposta a descer) |
| **Entry Price** | Preço onde entraste |
| **Size (USD)** | Tamanho da posição |
| **PnL (%)** | Lucro/perda percentual |
| **PnL (USDC)** | Lucro/perda em dólares |
| **Trailing Stop** | Preço onde o trailing stop está colocado |
| **Stop Loss** | Preço onde o stop fixo está |
| **Tempo Aberto** | Há quanto tempo a posição está aberta |

#### 📈 Equity Curve

Um gráfico de linha que mostra a evolução do teu capital ao longo do tempo.

- Começa sempre no capital inicial (ex: $10,000)
- Cada trade fecha → o capital sobe ou desce → nova linha no gráfico
- Linha verde = lucro
- Linha vermelha = prejuízo

> 📊 **Interpretação:** Se a linha sobe de forma consistente com pequenas oscilações, estás no bom caminho. Se desce continuamente, algo está errado com a estratégia ou as tuas definições.

#### 🎮 Controlo

- **▶ INICIAR BOT** — Começa a buscar dados e a monitorizar
- **⏹ PARAR BOT** — Para tudo. Fecha polling.
- **🔄 RESET** — Limpa estatísticas (equity, trades, PnL). Útil para recomeçar de novo.

---

### 🟦 Coluna da Direita — LOGS E ESTADO

#### 🖥️ Logs

Janela de texto com tudo o que o bot está a fazer, em tempo real:

- `[--:--:--]` → Hora do evento
- Texto branco → Informação normal
- Texto amarelo → Aviso (atenção, mas não crítico)
- Texto vermelho → Erro (algo falhou)
- Texto verde brilhante → Sinal de trade!

> 💡 **Como ler os logs:**
> ```
> 📡 BTC | Preço: $67,432.50 | OI: 125,000 | Funding: 0.0050%
> ```
> Isto significa: o bot buscou dados do BTC. Preço atual $67k. OI = 125k contratos. Funding = 0.005%.

#### ⚡ Estado

- **Monitor:** ON/OFF — se o bot está a correr
- **Updates:** Quantas vezes os dados foram atualizados
- **Latência:** Tempo que demora a buscar dados (em ms)
- **Último:** Hora da última atualização

---

## Como Mudar de Testnet para Mainnet

1. No painel da esquerda, encontra o dropdown **"🌐 Network"**
2. Clica nele
3. Seleciona **"🟢 Mainnet (Dinheiro Real)"**
4. O painel de ligação (carteira + chave) fica ativo automaticamente
5. Preenche os 3 campos de ligação
6. Clica em **"▶ TESTAR LIGAÇÃO"**
7. Só avança se disser **"✅ Ligação OK!"**
8. Clica em **"▶ INICIAR BOT"**

> ⚠️ **Aviso visual:** Em Mainnet, o banner no topo fica a laranja e diz "Dinheiro REAL!". Isto é propositado — para te manter alerta.

---

## Como Ler a Informação da Posição

Quando tens uma posição aberta, olha para estes 3 números primeiro:

1. **PnL (%)** — O mais importante. Verde = estás a ganhar. Vermelho = estás a perder.
2. **Tempo Aberto** — Se já passou muito tempo e o PnL está negativo, considera fechar manualmente.
3. **Trailing Stop / Stop Loss** — Sabes exatamente onde sairás automaticamente.

---

## Como Interpretar a Equity Curve

| Padrão | O que significa |
|--------|-----------------|
| Linha a subir suavemente | ✅ Bot está a funcionar bem |
| Linha plana | Nenhum trade ainda — normal no início |
| Subidas grandes seguidas de quedas pequenas | ✅ Ótimo! Ganhos superiores a perdas |
| Quedas contínuas | ❌ Revisa a estratégia ou definições |
| Linha a "serrar" para cima e para baixo | ⚠️ Volatilidade normal, mas observa o Profit Factor |

> 🧠 **Não paniques com quedas pequenas.** Mesmo a melhor estratégia tem trades perdedores. O que importa é a **tendência geral** ao longo de 20+ trades.

---

## Como Forçar um Trade

Às vezes queres testar se tudo funciona sem esperar por um sinal real.

- Clica em **"⬆ FORÇAR LONG"** → abre uma posição comprada imediatamente
- Clica em **"⬇ FORÇAR SHORT"** → abre uma posição vendida imediatamente

> ⚠️ **Isto é só para teste!** Não abuses disto em mainnet. Em testnet, serve para veres como a posição aparece no painel e como o PnL se comporta.

---

## Botões de Emergência

### 🚨 EMERGENCY CLOSE

- **Quando usar:** Quando tens uma posição aberta e queres sair **IMEDIATAMENTE**, sem esperar pelo stop.
- **O que faz:** Fecha a posição ao preço de mercado atual.
- **Quando precisas:** Se o mercado está a desabar e o teu stop ainda não ativou, ou se perdeste confiança no trade.

> 🔥 **Isto existe para uma razão:** em momentos de pânico, clicar num botão grande e vermelho é mais rápido do que raciocinar.

---

## Dicas e Truques

| Truque | Como fazer |
|--------|------------|
| **Gravar definições** | Clica em "💾 GUARDAR" — fica guardado no browser |
| **Mudar de asset** | Dropdown "Asset" → escolhe BTC ou ETH |
| **Ver histórico de trades** | Painel "Trades Recentes" no centro, em baixo |
| **Ver mais logs** | A coluna da direita mostra os últimos 1000 logs |
| **Atualizar manualmente** | Recarrega a página (F5) — não perdes definições |

---

## Resolução de Problemas do Dashboard

| Problema | Solução |
|----------|---------|
| Dashboard abre em branco | Confirma que abriste o ficheiro `dashboard.html`, não um atalho quebrado |
| Números não atualizam | Verifica internet. O dashboard busca dados das APIs diretamente. |
| "Network" não muda para Mainnet | É normal — só muda se preencheres os campos de ligação |
| Gráfico equity não aparece | Precisas de pelo menos 2 trades fechados para haver linha |
| Logs cheios de erros vermelhos | Copia o erro e vê o `TROUBLESHOOTING.md` |

---

> 🎯 **O dashboard é a tua janela para o bot. Mantém-no aberto, observa, aprende. Com o tempo, vais começar a "ler" o mercado através destes números.**
