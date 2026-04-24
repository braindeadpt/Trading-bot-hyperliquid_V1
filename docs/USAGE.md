# 📖 USAGE.md — Guia de Utilização
## Hyperliquid Momentum Bot v0.1.0

---

## 🖥️ Visão Geral do Dashboard

O dashboard abre automaticamente no teu navegador quando inicias o bot. Apresenta um estilo **cypherpunk / terminal vintage** com três colunas:

```
┌─────────────┬──────────────────────────────┬─────────────┐
│  ESQUERDA   │           CENTRO             │   DIREITA   │
│ Configuração│   Dados + Posição + Equity   │    Logs     │
│             │        + Trades              │   Estado    │
└─────────────┴──────────────────────────────┴─────────────┘
```

### Painéis Principais

| Painel | O que mostra | Localização |
|--------|-------------|-------------|
| **📊 Mercado em Tempo Real** | Preço, Mark Price, Oracle, OI, Funding, Volume, Variação 24h | Centro — topo |
| **🎯 Posição Atual** | Direção, preço de entrada, tamanho, PnL, trailing stop | Centro |
| **📈 Equity Curve** | Gráfico da evolução do capital + estatísticas | Centro |
| **🎮 Controlo** | Botões Iniciar/Parar/Reset | Centro |
| **📋 Trades Recentes** | Histórico completo de todas as operações | Centro — fundo |
| **🖥️ Logs** | Mensagens do bot em tempo real | Direita — topo |
| **⚡ Estado** | Monitor, updates, latência, última actualização | Direita |
| **ℹ️ Ajuda** | Dicas rápidas de utilização | Direita — fundo |

---

## 🌐 Testnet vs Mainnet

O bot tem **dois modos de rede**, seleccionáveis no painel esquerdo:

### 🟡 Testnet (Paper Trading) — Modo por Defeito

- **Sem carteira necessária** — usa capital virtual ($10,000)
- **Dados reais** — preços e OI vêm das APIs reais da Hyperliquid, Binance, Bybit, OKX
- **Zero risco** — não perdes dinheiro real
- **Ideal para:** aprender, testar estratégias, validar sinais

### 🟢 Mainnet (Dinheiro Real)

- **Requer carteira EVM** (MetaMask, Rabby, etc.)
- **Precisa de USDC** na Hyperliquid
- **Risco real** — podes ganhar ou perder dinheiro
- **Ideal para:** quando o bot estiver validado em testnet (ver [MAINNET_PREP.md](MAINNET_PREP.md))

### Como Mudar de Modo

1. No painel esquerdo, encontra o campo **"🌐 Network"**
2. Selecciona **Testnet** ou **Mainnet** no dropdown
3. Se escolheres Mainnet, o painel de ligação desbloqueia-se:
   - Preenche o endereço da wallet
   - Preenche a chave privada da API
   - Clica em **"▶ TESTAR LIGAÇÃO"**
4. Guarda a configuração com **"💾 GUARDAR"**

> ⚠️ **IMPORTANTE:** Quando estás em Testnet, o painel de ligação fica **cinzento e desactivado**. Isto é normal — não precisas de credenciais para simulação.

---

## 📡 Como Ler os Sinais

O bot gera sinais baseados em **três pilares**:

### 1. Volume Spike (Spike de Volume)
- O bot detecta quando o volume atual é **4x superior** à média dos últimos 100 períodos
- Representa interesse súbito do mercado
- No dashboard: aparece como spike verde no gráfico de volume

### 2. Open Interest (OI)
- Monitoriza o aumento de OI em **≥1%** em 15 minutos
- OI crescente = novas posições a entrar no mercado (confirmação de força)
- Aparece no painel "Mercado em Tempo Real" como "OI" e "OI ($)"

### 3. Funding Rate
- Se o funding estiver extremo (>1% ou <-1%), o bot pode evitar entradas
- Funding muito positivo = muitos longs (pode indicar topo)
- Funding muito negativo = muitos shorts (pode indicar fundo)

### O Que Faz o Bot Quando Detecta um Sinal

```
DETECTA Spike de Volume (4x)  →  VERIFICA OI (↑1%)  →  CONFIRMA direção
      ↓                               ↓                      ↓
   Volume alto                    Novo dinheiro a         Candles bullish
   = momentum                     entrar no mercado       (long) ou bearish
                                                           (short)
```

### Estados do Bot (Barra de Estado Superior)

| Indicador | Cor | Significado |
|-----------|-----|-------------|
| **API** | 🟢 Verde | Ligação às APIs OK |
| **API** | 🔴 Vermelho | APIs offline ou com erro |
| **Bot** | 🟢 Verde | Motor a correr |
| **Bot** | 🔴 Vermelho | Motor parado |
| **Pos** | 🟢 Verde | Em posição long com lucro |
| **Pos** | 🟡 Laranja | Em posição long com prejuízo |
| **Pos** | 🔴 Vermelho | Em posição short com prejuízo |
| **Pos** | ⚫ Cinzento | Sem posição aberta (FLAT) |

---

## 💰 Como Interpretar PnL e Equity Curve

### Painel "📈 Equity Curve"

| Campo | O que significa |
|-------|----------------|
| **Capital Inicial** | O capital com que começaste (por defeito: $10,000) |
| **Capital Atual** | Capital inicial + PnL acumulado de todas as trades |
| **Total PnL** | Lucro ou prejuízo total desde o início |
| **Trades** | Número total de operações fechadas |
| **Win Rate** | Percentagem de trades ganhadores |
| **Profit Factor** | Razão entre lucros e prejuízos (>1.5 é bom, >2 é excelente) |

### Como Ler o Gráfico

- **Linha subindo** → o bot está lucrativo
- **Linha a descer** → período de drawdown (prejuízo temporário)
- **Volatilidade suave** → estratégia estável
- **Quedas abruptas** → possível problema (verificar logs)

### Painel "🎯 Posição Atual"

| Campo | Significado |
|-------|-------------|
| **Direção** | LONG (aposta que sobe) ou SHORT (aposta que desce) |
| **Entry Price** | Preço a que entraste na posição |
| **Size** | Tamanho da posição em USD |
| **PnL (%)** | Lucro/prejuízo percentual desde a entrada |
| **PnL (USDC)** | Lucro/prejuízo absoluto |
| **Trailing Stop** | Preço de saída automática que sobe com o lucro |
| **Stop Loss** | Preço de saída fixo (2% abaixo da entrada) |
| **Tempo Aberto** | Há quanto tempo a posição está aberta |

> 💡 O **Trailing Stop** só activa depois de a posição ganhar 1.5%. A partir daí, acompanha o preço máximo com 1.5% de margem. Se o preço cair 1.5% desde o máximo, sai automaticamente.

---

## ⬆️⬇️ Como Usar Force Long / Force Short

São botões manuais para **forçar uma entrada** independentemente dos sinais:

### Quando Usar

| Botão | Quando usar |
|-------|-------------|
| **⬆ FORÇAR LONG** | Acreditas que o preço vai subir e queres entrar imediatamente |
| **⬇ FORÇAR SHORT** | Acreditas que o preço vai descer e queres entrar imediatamente |

### Como Funciona

1. O bot **ignora os sinais automáticos** e entra na direcção escolhida
2. Usa os mesmos parâmetros de risco (tamanho de posição, stop loss, trailing stop)
3. Regista-se nos logs como entrada manual

> ⚠️ **Atenção:** Forçar entradas manualmente desactiva a lógica de confirmação do bot. Usa com cautela — é uma ferramenta avançada, não um botão de "acho que sobe".

---

## 🚨 Emergency Close

O botão **"🚨 EMERGENCY CLOSE"** fecha a posição aberta **imediatamente**.

### Quando Usar

- Mercado a mover-se rapidamente contra a tua posição
- Queres sair agora, independentemente das regras do bot
- Notícias inesperadas no mercado
- Qualquer situação de pânico ou incerteza

### Como Funciona

1. Clica no botão vermelho "EMERGENCY CLOSE"
2. O bot envia ordem de mercado para fechar a posição
3. A posição fecha no preço actual do mercado
4. Regista-se como "EMERGENCY_CLOSE" no histórico

> 🔴 **NOTA:** Em paper trading, fecha a simulação. Em mainnet real, envia uma ordem real de mercado para a Hyperliquid. Usa apenas em emergências reais — ordens de mercado podem ter slippage (derrapagem de preço).

---

## ⚙️ Configurações Avançadas

### Painel Esquerdo — Configuração

| Campo | Valor por Defeito | O que Faz |
|-------|-------------------|-----------|
| **Asset** | BTC-PERP | Par de trading (BTC ou ETH) |
| **Timeframe** | 15m | Intervalo de análise (15m recomendado, 5m disponível) |
| **Capital (USDC)** | 10000 | Capital virtual ou real disponível |
| **Position Size (USD)** | 100 | Tamanho máximo de cada posição |
| **Leverage (x)** | 2 | Alavancagem (máx: 50, recomendado: 2) |
| **Vol Threshold (x)** | 4.0 | Quantos x acima da média o volume tem de estar |
| **OI Threshold (%)** | 1.0 | Aumento mínimo de OI para confirmar sinal |
| **Stop Loss (%)** | 2.0 | Perda máxima aceitável por posição |
| **Trailing Stop (%)** | 1.5 | Distância do trailing stop ao máximo de preço |

### Como Alterar Configurações

1. Edita os campos no painel esquerdo
2. Clica em **"💾 GUARDAR"** — guarda no `localStorage` do navegador
3. Clica em **"📥 EXPORTAR"** — descarrega um ficheiro JSON de backup

> 🔄 Para carregar configuração anterior: o bot faz isso automaticamente ao abrir.

---

## 🎯 Fluxo Típico de Uso (Paper Trading)

```
1. Duplo clique em start.bat
      ↓
2. Dashboard abre no browser
      ↓
3. Confirma que "Network" = Testnet
      ↓
4. Clica "▶ INICIAR BOT"
      ↓
5. Aguarda sinais (pode demorar minutos a horas)
      ↓
6. Quando detectar sinal → entra automaticamente
      ↓
7. Monitoriza posição no painel "Posição Atual"
      ↓
8. Sai sozinho por stop-loss, trailing stop, ou sinal contrário
      ↓
9. Consulta trades no histórico e no equity curve
      ↓
10. Para o bot quando quiseres (system tray ou Ctrl+C)
```

---

## 📱 Atalhos e Dicas

| Dica | Descrição |
|------|-----------|
| **Ícone na system tray** | Clica no ícone verde no canto inferior direito para abrir dashboard, iniciar/parar, ou sair |
| **Resize da janela** | O dashboard adapta-se a ecrãs pequenos (esconde painéis laterais) |
| **Logs em tempo real** | Painel direito actualiza a cada 3 segundos quando o bot está a correr |
| **Gráfico de equity** | Actualiza em tempo real à medida que as trades fecham |
| **Ficheiro de config** | Podes editar `config/settings.yaml` directamente com um editor de texto |

---

*Última actualização: 2026-04-24 | Versão do bot: v0.1.0*
