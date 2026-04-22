# Hyperliquid Trading Bot

Bot de trading automatizado para Hyperliquid com dados agregados de OI (Open Interest) de múltiplas exchanges.

## Estratégia

- **Sinais de entrada:** Spikes de volume + OI global a subir + confirmação de preço
- **Dados agregados:** Binance, Bybit, OKX, Hyperliquid (preço)
- **Polling:** 30-60 segundos
- **Execução:** Hyperliquid (paper trading primeiro)

## Estrutura

```
trading-bot-hyperliquid/
├── config/
│   └── settings.yaml         # Configurações do bot
├── src/
│   ├── main.py               # Loop principal
│   ├── data_aggregator.py    # Agregação OI/volume/funding
│   ├── strategy.py            # Lógica de entrada/saída
│   ├── risk_manager.py        # Gestão de risco
│   ├── exchange_client.py    # Cliente Hyperliquid
│   ├── data_downloader.py    # Descarrega dados históricos
│   ├── backtest.py            # Motor de backtest
│   ├── dashboard.py           # Dashboard Rich no terminal
│   └── utils.py              # Utilitários
├── tests/
│   └── test_strategy.py      # Testes
├── data/                      # Dados históricos (auto-criado)
├── backtest_results/          # Resultados de backtests
├── requirements.txt          # Dependências
├── .env.example              # Variáveis de ambiente
└── README.md                 # Este ficheiro
```

## Setup Windows

### 1. Instalar Python
- Download: https://www.python.org/downloads/
- **Importante:** Na instalação, marca a caixa **"Add Python to PATH"**
- Verifica: abre o terminal (cmd/PowerShell) e escreve `python --version`

### 2. Clonar o repositório
```bash
git clone https://github.com/braindeadpt/trading-bot-hyperliquid.git
cd trading-bot-hyperliquid
```

Se não tens git instalado, podes descarregar o ZIP diretamente do GitHub (botão verde "Code" → "Download ZIP") e extrair.

### 3. Instalar dependências
```bash
pip install -r requirements.txt
```

Se der erro, tenta:
```bash
python -m pip install -r requirements.txt
```

### 4. Configurar
Copia `.env.example` para `.env` e ajusta se necessário (por agora, as defaults servem para paper trading).

## Como Usar

### Teste Rápido
```bash
python tests/test_strategy.py
```

### Bot em Tempo Real
```bash
python src/main.py
```

Para parar: `Ctrl + C`

### Backtest (TESTAR ANTES DE TUDO!)

**Passo 1 - Descarregar dados históricos:**
```bash
python src/data_downloader.py BTCUSDT 30
```
Isto descarrega 30 dias de candles, OI e funding rate da Binance (grátis).

**Passo 2 - Correr backtest:**
```bash
python src/backtest.py BTCUSDT 30
```

Isto simula a tua estratégia sobre 30 dias de dados reais e mostra:
- PnL total
- Win rate
- Profit factor
- Max drawdown
- Veredito: VALIDO ou NÃO usar

### Dashboard Rich (Terminal Colorido)

Abre uma janela nova com tabelas, cores e emojis:

```bash
# PowerShell ou Windows Terminal (recomendado):
python src/dashboard.py
```

**Nota:** Usa PowerShell ou Windows Terminal em vez do cmd antigo. O cmd não suporta emojis bem.

## Setup Linux/Mac

```bash
git clone https://github.com/braindeadpt/trading-bot-hyperliquid.git
cd trading-bot-hyperliquid
pip install -r requirements.txt
python tests/test_strategy.py
python src/main.py
```

## Estado

Em desenvolvimento ativo:
- ✅ Agregador de dados (Binance, Bybit, OKX, Hyperliquid)
- ✅ Estratégia base (volume + OI + funding)
- ✅ Gestão de risco
- ✅ Paper trading
- ✅ Data downloader (dados históricos Binance)
- ✅ Backtest engine
- ✅ Dashboard Rich (terminal)
- 🚧 WebSocket preço em tempo real
- 🚧 Trailing stop com ATR
- 🚧 Notificações Telegram

## Notas Importantes

- O bot está em **PAPER TRADING** por defeito (não gasta dinheiro real)
- A Hyperliquid API pública não fornece OI nem funding rate — estes vêm de Binance/Bybit/OKX
- Os dados históricos da Binance são **gratuitos** — usa-os para backtest antes de por dinheiro real
- Para ajustar a estratégia, edita `config/settings.yaml`
