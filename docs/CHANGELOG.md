# 📋 Changelog — Histórico de Versões

> _Tudo o que foi construído, quando, e porquê._

---

## v0.1.0 — Lançamento Inicial (2026-04-24)

> _"De zero a bot funcional em 3 dias. Nada mau, Pedro."_

### 🎯 Funcionalidades Principais

#### Bot de Trading Automatizado
- **Loop principal** (`src/main.py`) que corre indefinidamente
- **Polling configurável** — busca dados a cada 30s (OI) e 5s (preço)
- **Execução em modo testnet** (paper trading) por defeito
- **Suporte a múltiplos assets** — BTC e ETH (configurável via `settings.yaml`)

#### Agregador de Dados (`src/data_aggregator.py`)
- Busca dados de **4 exchanges simultâneas**:
  - Binance (futures)
  - Bybit (linear perpetuals)
  - OKX (SWAP)
  - Hyperliquid (preço mark/oracle)
- **Validação de APIs** antes de usar — testa se cada exchange está online
- **Retry automático** com backoff exponencial (até 3 tentativas)
- **Cache de preços** — guarda o último preço válido durante 2 minutos
- **Deteção de preços insanos** — valida se o preço está dentro de ranges realistas (ex: BTC $10k-$200k)
- **Pesos configuráveis** por exchange (Binance 40%, Bybit 30%, OKX 20%, Hyperliquid 10%)

#### Estratégia de Momentum (`src/strategy.py`)
- **Sinal de entrada LONG** baseado em:
  - Spike de volume (threshold configurável, ótimo: 4.0x)
  - OI global a subir (threshold: 1.0%)
  - Funding rate dentro de limites aceitáveis (±1%)
- **Sinal de saída** por exaustão de OI (OI a descer > 0.5%)
- **Suporte a SHORT** — testado e funcional com OI real (PF 3.61 em backtest)
- **Confirmação por candles** — 1 candle bullish para LONG, 2 candles bearish para SHORT
- **Regime de mercado** — deteção automática de tendência

#### Gestão de Risco (`src/risk_manager.py`)
- **Limite de posição** máxima em USD (por defeito: $100)
- **Alavancagem máxima** (por defeito: 2x)
- **Stop loss fixo** (por defeito: 2%)
- **Trailing stop** com ativação configurável (1.5% de lucro) e distância (1.5%)
- **Limite diário de trades** (máximo 5 por dia)
- **Verificação de stop loss** a cada tick

#### Cliente Hyperliquid (`src/exchange_client.py`)
- **Paper trading completo** — simula execução com logs detalhados
- **Suporte a ordens de mercado** (market orders)
- **Fecho de posições** simulado
- **Saldo virtual** de $10,000 USDC para testes
- Estrutura pronta para execução real (requer wallet + assinatura criptográfica)

#### Motor de Backtest (`src/backtest.py`)
- Simula a estratégia sobre dados históricos reais
- **Alinhamento de dados** — junta candles, OI e funding rate por timestamp
- **Métricas de performance**:
  - Profit Factor (PF)
  - Win Rate (WR)
  - Max Drawdown (DD)
  - Total PnL
  - Avg Win / Avg Loss
- **Veredito automático:** [PASS] / [WARNING] / [FAIL]
- **Exportação de resultados** para JSON com timestamp

#### Descarregador de Dados (`src/data_downloader.py`)
- Busca dados históricos da Binance (grátis)
- Candles (OHLCV) em múltiplos timeframes
- Open Interest histórico
- Funding rate histórico
- Guarda tudo em CSV na pasta `data/`

#### Dashboard Web (`dashboard.html`)
- **Interface cypherpunk/terminal** — estilo retro com scanlines e flicker
- **Layout em 3 colunas:** Configuração | Mercado+Posição+Equity | Logs+Estado
- **Dados em tempo real** da Hyperliquid API (allMids, metaAndAssetCtxs)
- **Paper trading integrado** — simula posições, PnL, equity curve
- **Gráfico equity** em canvas (HTML5)
- **Tabela de trades** com histórico completo
- **Métricas ao vivo:** Win Rate, Profit Factor, Total PnL
- **Botões de controlo:** Iniciar, Parar, Reset, Emergency Close
- **Forçar trades** (LONG/SHORT) para testes
- **Guardar/Exportar configuração** para JSON
- **Alternância Testnet/Mainnet** com proteção visual
- **Teste de ligação** à Hyperliquid antes de correr

#### Dashboard Rich no Terminal (`src/dashboard.py`)
- Interface visual no terminal com a biblioteca **Rich**
- Tabelas coloridas com emojis
- Painel de assets em monitorização
- Atualizações em tempo real (Live)
- Indicadores de sinais LONG

#### Utilitários (`src/utils.py`)
- Carregamento de configuração YAML
- Setup de logging com ficheiro (`logs/bot.log`)
- Helpers diversos

#### Configuração (`config/settings.yaml`)
- Todas as definições em YAML legível
- Comentários explicativos em português
- Parâmetros ótimos documentados (Grid Search v4)
- Timeframe vencedor: 15m (PF 2.50, WR 72.2%)

#### Scripts de Lançamento
- `launch_dashboard.bat` — Abre dashboard numa janela separada
- `launch_paper_trading.bat` — Corre o bot em paper trading com um duplo-clique

#### Testes
- `tests/test_strategy.py` — Testes unitários da estratégia
- `test_all.py` — Suite de testes completa
- `test_mtf.py` — Testes multi-timeframe
- `test_price_now.py` — Teste de preço em tempo real
- `diagnose.py` — Ferramenta de diagnóstico do bot
- `debug_api.py` — Debug de APIs individuais

#### Base de Dados
- SQLite (`data/trading_bot.db`) para registo de trades e estatísticas
- Dados históricos em CSV para backtest rápido sem depender da internet

---

## Próximas Versões (Roadmap)

### v0.2.0 (Planeado)
- [ ] Execução real em Mainnet (assinatura criptográfica)
- [ ] WebSocket para preço em tempo real (em vez de polling HTTP)
- [ ] Notificações Telegram / Discord
- [ ] Suporte a múltiplos assets simultâneos com threads
- [ ] Otimização automática de parâmetros (walk-forward analysis)

### v0.3.0 (Futuro)
- [ ] Estratégias adicionais (mean reversion, breakout)
- [ ] Gestão de portfólio (allocação entre assets)
- [ ] Machine learning para scoring de sinais
- [ ] Relatórios diários/semanais automáticos

---

> 📝 **Este changelog é atualizado manualmente. Se quiseres saber o estado exato do código, vê o `README.md` principal ou corre `git log` na pasta do projeto.**
