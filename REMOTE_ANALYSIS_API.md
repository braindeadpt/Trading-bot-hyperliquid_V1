# 🔍 Remote Analysis API — Análise Remota Completa do Bot

Adiciona endpoints ao dashboard Flask para eu (ou qualquer pessoa autorizada) analisar o bot remotamente via ngrok.

---

## ⚡ Instalação Rápida

```powershell
cd C:\Users\Braindead\Documents\trading-bot-hyperliquid
git pull origin main
```

Os ficheiros `src/api_extensions.py` e o patch no `src/dashboard_web.py` vêm automaticamente.

**Reiniciar o bot:**
```powershell
Ctrl + C
python src/main.py
```

Ao arrancar, deves ver:
```
✅ Remote Analysis API ativada — endpoints disponíveis via /api/*
```

---

## 📡 Endpoints Disponíveis

### 1. Logs do Bot
```
GET /api/logs?lines=200&file=bot.log
```
Retorna as últimas N linhas do ficheiro de log.

**Exemplo de resposta:**
```json
{
  "log_file": "bot.log",
  "total_lines": 15420,
  "returned_lines": 200,
  "lines": [
    "2026-04-28 22:17:01 - INFO - Bot operacional!",
    "2026-04-28 22:17:02 - INFO - 📊 Status | Capital: $10,000 | Trades: 0"
  ]
}
```

---

### 2. Listar Ficheiros
```
GET /api/files?dir=src&depth=2
```
Lista ficheiros num diretório do projeto.

**Diretórios válidos:** `src`, `config`, `data`, `logs`, `root`

**Exemplo:**
```json
{
  "directory": "src",
  "total_files": 15,
  "files": [
    {"name": "main.py", "path": "src/main.py", "size": 2450, "modified": "2026-04-28T20:00:00"},
    {"name": "paper_trading.py", "path": "src/paper_trading.py", "size": 45120, "modified": "2026-04-28T20:00:00"}
  ]
}
```

---

### 3. Ler Ficheiro
```
GET /api/file?path=src/paper_trading.py
```
Retorna o conteúdo completo de qualquer ficheiro do projeto.

**Segurança:** Só permite ler ficheiros dentro do diretório do projeto.

**Exemplo:**
```json
{
  "path": "src/paper_trading.py",
  "size": 45120,
  "truncated": false,
  "content": "import time..."
}
```

---

### 4. Query à Base de Dados
```
GET /api/db?sql=SELECT+*+FROM+signals+ORDER+BY+id+DESC+LIMIT+20
```
Executa queries SELECT na base de dados SQLite.

**Segurança:** Apenas SELECT é permitido. INSERT/UPDATE/DELETE são bloqueados.

**Exemplo:**
```json
{
  "database": "trading_bot.db",
  "query": "SELECT * FROM signals ORDER BY id DESC LIMIT 20",
  "rows_returned": 20,
  "rows": [
    {"id": 29, "asset": "BTC", "signal_type": "SHORT", "executed": 0, "reason": "FILTER: vol_low(0.9x<2.8)"}
  ]
}
```

---

### 5. Configuração (settings.yaml)
```
GET /api/config?file=settings.yaml
```
Retorna o conteúdo do ficheiro de configuração.

**Exemplo:**
```json
{
  "file": "settings.yaml",
  "content": "bot:\n  name: Hyperliquid Bot\n...",
  "parsed": {"bot": {"name": "Hyperliquid Bot"}}
}
```

---

### 6. Sinais Rejeitados (Detalhado)
```
GET /api/rejections?days=7&limit=100
```
Retorna todos os sinais rejeitados com estatísticas agrupadas por motivo.

**Exemplo:**
```json
{
  "days": 7,
  "total_rejections": 26,
  "by_reason": {
    "FILTER: vol_low(0.9x<2.8)": 12,
    "FILTER: oi_insufficient": 8,
    "FILTER: vol_low(2.1x<2.8)": 6
  },
  "rejections": [
    {"id": 29, "volume_ratio": 0.9, "reason": "FILTER: vol_low(0.9x<2.8)"}
  ]
}
```

---

### 7. Todos os Sinais
```
GET /api/all_signals?days=7&limit=200&executed=1
```
Retorna TODOS os sinais (aceites e rejeitados) com filtros opcionais.

**Parâmetros:**
- `days=7` — quantos dias para trás
- `limit=200` — máximo de sinais
- `executed=1` — só sinais aceites (opcional)

---

### 8. Resumo do Projeto
```
GET /api/analysis/summary
```
Retorna um resumo da estrutura do projeto — ficheiros existentes, logs, base de dados.

**Exemplo:**
```json
{
  "timestamp": "2026-04-28T22:00:00",
  "project_dir": "C:\\Users\\...\\trading-bot-hyperliquid",
  "src": {"exists": true, "file_count": 12},
  "config": {"exists": true, "file_count": 3},
  "data": {"exists": true, "file_count": 2},
  "logs": {"exists": true, "file_count": 5, "latest_log": {"file": "bot.log", "size": 15420}},
  "databases": ["trading_bot.db"]
}
```

---

## 🔗 Como Usar (Exemplos PowerShell)

```powershell
# Ver logs
Invoke-RestMethod "https://remedial-deception-contact.ngrok-free.dev/api/logs?lines=50" | ConvertTo-Json -Depth 2

# Ver último sinal rejeitado
Invoke-RestMethod "https://remedial-deception-contact.ngrok-free.dev/api/rejections?days=1&limit=1" | ConvertTo-Json -Depth 3

# Ver código do paper_trading.py
Invoke-RestMethod "https://remedial-deception-contact.ngrok-free.dev/api/file?path=src/paper_trading.py" | Select-Object -ExpandProperty content | Set-Content paper_trading_backup.py

# Query à base de dados
Invoke-RestMethod "https://remedial-deception-contact.ngrok-free.dev/api/db?sql=SELECT+*+FROM+signals+WHERE+executed=0+ORDER+BY+id+DESC+LIMIT+10" | ConvertTo-Json -Depth 3
```

---

## 🛡️ Segurança

- **Apenas SELECT** na base de dados — queries de escrita são bloqueadas
- **Path traversal protegido** — só pode ler ficheiros dentro do diretório do projeto
- **Máximo 500KB** por ficheiro — ficheiros maiores são truncados
- **100 ficheiros** por listagem — para evitar overload

---

## 🎯 Uso Principal

Com esta API, posso:
1. **Diagnosticar problemas** sem pedir logs ao Pedro
2. **Ver código fonte** em tempo real
3. **Analisar base de dados** de sinais e trades
4. **Ver configuração** atual do bot
5. **Identificar padrões** de rejeição

**O Pedro não precisa de fazer nada** — basta o bot estar a correr com ngrok ativo.

---

*Criado em 2026-04-28 para diagnóstico remoto completo.*
