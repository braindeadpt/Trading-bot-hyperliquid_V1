# Hyperliquid Trading Bot

Bot de trading automatizado para Hyperliquid com dados agregados de OI (Open Interest) de múltiplas exchanges.

## Estratégia

- **Sinais de entrada:** Spikes de volume + OI global a subir + confirmação de preço
- **Dados agregados:** Binance, Bybit, OKX, Hyperliquid
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
│   └── utils.py              # Utilitários
├── tests/
│   └── test_strategy.py      # Testes
├── requirements.txt          # Dependências
├── .env.example              # Variáveis de ambiente
└── README.md                 # Este ficheiro
```

## Setup Windows

### 1. Instalar Python
- Download: https://www.python.org/downloads/
- **Importante:** Na instalação, marca a caixa **"Add Python to PATH"**
- Verifica: abre o terminal (cmd) e escreve `python --version`

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

### 5. Testar
```bash
python tests/test_strategy.py
```

### 6. Correr o bot
```bash
python src/main.py
```

Para parar: `Ctrl + C`

## Setup Linux/Mac

```bash
git clone https://github.com/braindeadpt/trading-bot-hyperliquid.git
cd trading-bot-hyperliquid
pip install -r requirements.txt
python tests/test_strategy.py
python src/main.py
```

## Estado

🚧 Em desenvolvimento - V1 base

## Notas

- O bot está em **PAPER TRADING** por defeito (não gasta dinheiro real)
- Verifica os logs na pasta `logs/` para ver o que o bot está a fazer
- Para ajustar a estratégia, edita `config/settings.yaml`
