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

## Setup

1. Copiar `.env.example` para `.env` e preencher
2. `pip install -r requirements.txt`
3. `python src/main.py`

## Estado

🚧 Em desenvolvimento - V1 base
