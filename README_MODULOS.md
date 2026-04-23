# 🚀 Hyperliquid Trading Bot - Novos Módulos

## O que foi construído

### 1. ✅ Base de Dados SQLite (`src/database.py`)
- **Guarda dados históricos** (candles, OI, funding) para backtest rápido
- **Guarda trades** (backtest + live)
- **Não depende de APIs** durante o backtest
- Ficheiro: `data/trading_bot.db`

### 2. ✅ Dashboard Web (`src/dashboard_web.py`)
- **Abre no BROWSER** = janela separada do PowerShell!
- Interface visual com estatísticas em tempo real
- URL: `http://127.0.0.1:5000`
- Auto-refresh a cada 30 segundos

### 3. ✅ Data Aggregator v2 (`src/data_aggregator.py`)
- **Retry logic** com backoff exponencial
- **Validação de APIs** antes de usar (testa todas primeiro!)
- **Cache de preços** quando API falha
- **Logging detalhado** de erros (HTML vs JSON, etc.)

### 4. ✅ Backtest Engine v2 (`src/backtest_db.py`)
- Usa **SQLite em vez de CSVs**
- **Sharpe ratio** incluído nas métricas
- **Guarda trades** na base de dados
- Muito mais rápido!

---

## 🎯 Como Usar

### Testar tudo (verifica APIs + módulos)
```bash
python test_all.py
```

### Descarregar dados históricos (para backtest)
```bash
# BTC, 30 dias, timeframe 15m
python src/data_downloader.py BTCUSDT 30 15m

# ETH também
python src/data_downloader.py ETHUSDT 30 15m
```

### Correr backtest rápido
```bash
# BTC, 15m, últimos 30 dias
python src/backtest_db.py BTC 15m 30
```

### Iniciar Dashboard Web (janela separada!)
```bash
# Método 1: Script Python
python src/dashboard_web.py

# Método 2: Batch file (Windows)
.
```
O dashboard abre automaticamente no teu browser!

### Testar APIs individualmente
```bash
python src/data_aggregator.py
```

---

## 📊 Estrutura dos Ficheiros Novos

```
trading-bot-hyperliquid/
├── src/
│   ├── database.py          ⭐ NOVO - SQLite para dados
│   ├── backtest_db.py       ⭐ NOVO - Backtest com SQLite
│   ├── dashboard_web.py     ⭐ NOVO - Dashboard no browser
│   └── data_aggregator.py   🔧 ATUALIZADO - Retry + validação
├── test_all.py             ⭐ NOVO - Test suite
├── launch_dashboard.bat    ⭐ NOVO - Launcher Windows
├── data/
│   └── trading_bot.db      ⭐ Base de dados SQLite
└── requirements.txt        🔧 Atualizado (Flask)
```

---

## ⚡ Instalação no Windows

1. **Instala Flask** (para o dashboard web):
```bash
pip install flask
```

2. **Verifica instalação**:
```bash
python test_all.py
```

---

## 🔥 Próximos Passos Recomendados

1. **Descarrega 30 dias de dados**:
   ```bash
   python src/data_downloader.py BTCUSDT 30 15m
   ```

2. **Corre backtest**:
   ```bash
   python src/backtest_db.py BTC 15m 30
   ```

3. **Abre dashboard**:
   ```bash
   python src/dashboard_web.py
   ```

4. **Ajusta parâmetros da estratégia** se necessário!

---

## 🛡️ Segurança

- **Paper trading** por padrão (não arrisca dinheiro real)
- **Validação de APIs** antes de cada sessão
- **Cache local** para continuar funcionando mesmo com APIs instáveis

---

## 📝 Notas

- Dashboard usa **Flask** (servidor web leve)
- Base de dados usa **SQLite** (zero configuração)
- Backtest usa **dados locais** (não precisa de internet)
- Todas as APIs são **testadas automaticamente** antes de usar
