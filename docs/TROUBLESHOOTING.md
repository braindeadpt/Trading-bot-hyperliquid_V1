# 🔧 TROUBLESHOOTING.md — Resolução de Problemas
## Hyperliquid Momentum Bot v0.1.0

---

## 🚨 Problemas Mais Comuns

### 1. "Bot não arranca" — A janela abre e fecha logo

**Causas prováveis:**
- Python não instalado ou não no PATH
- Dependências em falta (flask, pystray, pillow)
- Erro no ficheiro de configuração

**Como resolver:**

1. Abre o PowerShell na pasta do bot:
   ```powershell
   cd C:\Users\%USERNAME%\Documents\hyperliquid-momentum-bot
   python app_flask.py
   ```

2. Lê a mensagem de erro. Se aparecer:
   - `"python" não é reconhecido` → [Ver INSTALL.md](INSTALL.md#python-não-é-reconhecido-como-comando)
   - `ModuleNotFoundError: No module named 'flask'` → Instala dependências:
     ```powershell
     python -m pip install flask pystray pillow
     ```
   - `YAML parse error` → Verifica que `config/settings.yaml` está bem formatado (não edits com Word!)

---

### 2. "Dashboard não abre" — O bot diz que está a correr mas não vês nada

**Causas prováveis:**
- O navegador não abriu automaticamente
- Porta 5000 ocupada por outro programa
- Firewall a bloquear ligações locais

**Como resolver:**

1. Abre manualmente no navegador:
   ```
   http://127.0.0.1:5000
   ```

2. Se não funcionar, testa a porta:
   ```powershell
   python -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',5000)); print('Porta livre')"
   ```
   Se der erro, a porta 5000 está ocupada. Fecha outras aplicações ou muda a porta em `app_flask.py`.

3. Verifica firewall/antivírus — a ligação é local (`127.0.0.1`), mas alguns antivírus bloqueiam lo.

---

### 3. "Preço não aparece" — O dashboard mostra "--" em vez do preço

**Causas prováveis:**
- Sem ligação à Internet
- APIs da Hyperliquid/Binance/Bybit/OKX em baixo
- Erro de parsing nos dados

**Como resolver:**

1. Verifica a tua ligação à Internet:
   ```powershell
   ping google.com
   ```

2. Testa as APIs individualmente:
   ```powershell
   python scripts/test_price_now.py
   ```

3. Verifica os logs no painel direito do dashboard — procura mensagens como:
   - `Erro a buscar dados` → problema de rede
   - `Expecting value: line 1 column 1` → API devolveu HTML em vez de JSON (Cloudflare/proxy)

4. Se as APIs estiverem em baixo, o bot continua a tentar automaticamente (retry com backoff). Aguarda alguns minutos.

---

### 4. "Erro de encoding" — Mensagens com caracteres estranhos ou crash

**Causa:** Windows usa codificação CP1252 por defeito. O bot precisa de UTF-8.

**Como resolver:**

O `start.bat` já define UTF-8 automaticamente:
```batch
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
```

Se usares `python app_flask.py` manualmente, define antes:
```powershell
$env:PYTHONIOENCODING = "utf-8"
python app_flask.py
```

---

### 5. "pystray não instalado" — Mensagem de aviso ao iniciar

**Mensagem típica:**
```
⚠️ pystray/pillow não instalado — system tray desativado
```

**Como resolver:**

```powershell
python -m pip install pystray pillow
```

O bot funciona mesmo sem pystray — só não terás o ícone verde na barra de tarefas. Para parar, usa `Ctrl + C`.

---

### 6. "Nenhum trade" — O bot está a correr mas não entra em posições

**Isto é NORMAL na maior parte do tempo!** O bot é **selectivo** por design.

**Porquê:**
- O bot espera por **spikes de volume 4x acima da média** + **aumento de OI ≥1%** + **confirmação de direção**
- Em mercados laterais (range) com baixa volatilidade, estes sinais são raros
- O bot foi optimizado para **qualidade sobre quantidade** (Profit Factor 2.50)

**Como verificar se está tudo OK:**

1. Confirma que o bot está a correr (painel direito → "Monitor: ON")
2. Verifica o painel "Mercado em Tempo Real" — o preço actualiza a cada 5 segundos?
3. Abre os logs — vês mensagens como `[HL] BTC | $78,304.50 | OI: ...`?
4. Se sim, o bot está a trabalhar corretamente. Aguarda volatilidade.

**Quanto tempo esperar?**
- Em BTC 15m: tipicamente 1-3 sinais por dia em dias voláteis
- Em fins de semana ou mercados calmos: pode não haver nenhum sinal
- O backtest mostrou 36 trades em 30 dias (~1.2 trades/dia em média)

---

## 🔍 Problemas Menos Comuns

### "Erro SQL / Base de dados"

```
Erro a guardar dados: BotDatabase.save_oi() missing 1 required positional argument
```

**Causa:** Bug conhecido na versão actual — a função `save_oi` foi alterada mas algumas chamadas não actualizadas.

**Resolução:** O bot recupera automaticamente. O erro não impede o funcionamento — apenas não grava OI na base de dados. Será corrigido numa versão futura.

---

### "O bot entrou e saiu logo da mesma posição"

**Causas:**
1. Stop-loss muito apertado — aumenta de 2% para 3% se o mercado estiver volátil
2. Trailing stop activou-se muito cedo — verifica `trailing_activation_pct` em `config/settings.yaml`
3. Sinal contrário apareceu logo após a entrada — o bot reverte posição se o sinal inverter

**Resolução:**
- Revisa `config/settings.yaml` — ajusta `stop_loss_pct` e `trailing_stop_pct`
- Verifica os logs para entender o motivo da saída (STOP_LOSS, TRAILING_STOP, SIGNAL_REVERSE)

---

### "Force Long/Short não funciona"

**Causa:** Na versão actual (v0.1.0), os botões Force Long/Short e Emergency Close ainda **não estão totalmente implementados** no backend. O frontend mostra os botões mas o servidor Flask responde com "Ainda não implementado".

**Resolução:** Será implementado numa versão futura. Por agora, o bot opera apenas em modo automático.

---

## 📋 Checklist de Diagnóstico Rápido

Quando algo corre mal, percorre esta lista:

```
□ O Python está instalado? (python --version)
□ As dependências estão instaladas? (pip list | findstr flask)
□ O start.bat está na pasta correcta?
□ A Internet está ligada?
□ A porta 5000 está livre?
□ O config/settings.yaml está intacto?
□ Os logs mostram erros específicos?
□ O preço actualiza no dashboard?
□ Há volatilidade no mercado? (Bitcoin está a mover-se?)
```

---

## 🛠️ Ferramentas de Diagnóstico Incluídas

O projeto inclui scripts de diagnóstico na pasta `scripts/`:

| Script | Como usar | O que faz |
|--------|-----------|-----------|
| `scripts/test_price_now.py` | `python scripts/test_price_now.py` | Testa se consegue buscar preço da API |
| `scripts/diagnose.py` | `python scripts/diagnose.py` | Diagnóstico completo do ambiente |
| `scripts/debug_api.py` | `python scripts/debug_api.py` | Testa todas as APIs individualmente |

---

## 📞 Onde Pedir Ajuda

1. **Lê os logs** — painel direito do dashboard mostra tudo em tempo real
2. **Verifica ficheiro de log** — `logs/bot.log` tem histórico completo
3. **Corre diagnóstico** — `python scripts/diagnose.py`

---

*Última actualização: 2026-04-24 | Versão do bot: v0.1.0*
