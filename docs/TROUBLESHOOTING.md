# 🔧 Resolução de Problemas (Troubleshooting)

> _Quando algo corre mal, não entres em pânico. Tens um manual._

---

## Problema 1: "pip não é reconhecido como nome de cmdlet"

### O que vês:
```powershell
pip : The term 'pip' is not recognized as the name of a cmdlet...
```

### Porque acontece:
O Windows não sabe onde o `pip` está instalado. O Python está no computador, mas o Windows não o encontrou porque o **PATH** não está configurado.

### Solução — Passo a Passo:

**Método 1 — Usa `python -m pip` (mais fiável):**
```powershell
python -m pip install -r requirements.txt
```

**Método 2 — Se `python` também não for reconhecido, usa `py`:**
```powershell
py -m pip install -r requirements.txt
```

**Método 3 — Reinstalar Python com PATH:**
1. Vai a "Adicionar ou Remover Programas"
2. Desinstala Python
3. Descarrega de novo em [python.org](https://www.python.org/downloads/)
4. **NO INSTALADOR, MARCA A CAIXINHA:** "Add Python to PATH" (está no fundo da primeira janela)
5. Instala
6. Fecha e abre o PowerShell de novo
7. Testa: `python --version`

> 💡 **Dica:** Nunca uses `pip` sozinho no Windows. `python -m pip` é sempre mais seguro.

---

## Problema 2: "O preço mostra valor errado ou completamente absurdo"

### O que vês:
No dashboard ou terminal, o preço do BTC aparece como $0, $999999, ou um valor que sabes que está errado.

### Porque acontece:
1. A API da Hyperliquid ou Binance teve um problema temporário
2. Cloudflare bloqueou o pedido (retornou HTML em vez de JSON)
3. A resposta da API veio vazia ou mal formatada
4. O "sanity check" do bot detetou algo estranho e recusou o preço

### Solução — Passo a Passo:

**Passo 1 — Verifica se é um problema da API:**
```powershell
python src/data_aggregator.py
```
Isto testa todas as APIs. Se aparecerem ❌ em todas, o problema é a tua internet ou as APIs estão em manutenção.

**Passo 2 — Testa a Hyperliquid especificamente:**
```powershell
python test_price_now.py
```

**Passo 3 — Se só a Hyperliquid falha:**
- Espera 5 minutos e tenta de novo. A Hyperliquid faz manutenções breves.
- Verifica [status.hyperliquid.xyz](https://status.hyperliquid.xyz) (se existir) ou o Twitter/X da equipa.

**Passo 4 — Se o dashboard mostra valor errado:**
- Recarrega a página (F5)
- Abre a consola do browser (F12 → separador "Console") — vês erros vermelhos?
- Se houver erros CORS, confirma que abriste o `dashboard.html` como ficheiro local (file://) e não via servidor.

> 🧠 **Nota:** O bot tem um "cache de preço" que guarda o último valor válido por 2 minutos. Se vir valor 0, significa que NENHUMA API respondeu com um preço que o bot considerasse "são".

---

## Problema 3: "O bot não inicia — dá erro de rede ou de ficheiro"

### O que vês:
```
Erro fatal: [algo sobre ficheiro não encontrado ou rede]
```

### Porque acontece:
1. O ficheiro `config/settings.yaml` não existe ou está vazio
2. O ficheiro tem erros de formatação (YAML é sensível a espaços!)
3. A pasta `data/` não existe e o bot tentou criar algo lá
4. Estás a correr o bot de uma pasta errada

### Solução — Passo a Passo:

**Passo 1 — Confirma que estás na pasta certa:**
```powershell
cd C:\Users\O_Teu_Nome\Documents\trading-bot-hyperliquid
```

**Passo 2 — Verifica se o settings.yaml existe:**
```powershell
ls config\settings.yaml
```

**Passo 3 — Abre o settings.yaml e verifica:**
- Não pode ter tabs (espaços só, nunca o botão Tab)
- A indentação tem de estar correta (ex: `bot:` tem de estar alinhado com `assets:`)
- Não pode haver linhas vazias estranhas no meio

**Passo 4 — Se alteraste o ficheiro e agora dá erro:**
- Copia o `settings.yaml` original do repositório GitHub
- Ou corre: `git checkout config/settings.yaml` (se tens git)

**Passo 5 — Cria as pastas que faltam:**
```powershell
mkdir data
mkdir logs
mkdir backtest_results
```

---

## Problema 4: "Não acontecem trades — o bot corre mas fica calado"

### O que vês:
O bot está a correr, os logs aparecem a cada 30 segundos, mas nunca diz "SINAL LONG!" ou abre posições.

### Porque acontece:
1. O mercado está calmo — não há spikes de volume suficientes
2. Os thresholds estão demasiado apertados
3. O funding rate está extremo e o bot está a filtrar
4. O bot está em "coleta de dados" — precisa de 50 amostras de volume antes de calcular média

### Solução — Passo a Passo:

**Passo 1 — Espera um pouco:**
O bot precisa de recolher ~50 amostras de volume (cerca de 25 minutos em 15m) antes de poder calcular a média. Nos primeiros minutos, isto é normal.

**Passo 2 — Verifica se os thresholds não estão demasiado altos:**
Abre `config/settings.yaml` e confirma:
```yaml
strategy:
  volume_spike_threshold: 4.0   # Se isto for 10.0, nunca vai disparar
  oi_change_threshold: 0.01     # 1.0% — se for 5.0%, raro de acontecer
```

**Passo 3 — Testa em horário de maior volume:**
O volume de crypto é maior durante:
- Abertura de Nova York (14:00 UTC / 15:00 Portugal)
- Sessão asiática (meia-noite a 08:00 UTC)

**Passo 4 — Força um trade no dashboard para testar:**
- Abre o `dashboard.html`
- Clica em "▶ INICIAR BOT"
- Clica em "⬆ FORÇAR LONG"
- Se abrir posição → tudo funciona, só não há sinais naturais agora

**Passo 5 — Verifica se o funding rate está a bloquear:**
No dashboard, olha para o cartão "Funding". Se estiver > 1.0% ou < -1.0%, o bot recusa entrar como proteção.

---

## Problema 5: "O dashboard não atualiza — números congelados"

### O que vês:
Os números no dashboard ficam iguais, a "Última atualização" não muda, ou os logs param.

### Porque acontece:
1. O browser perdeu a ligação à internet
2. A aba do browser foi "dormida" pelo Chrome (economia de energia)
3. Erro JavaScript travou o polling
4. O bot foi parado mas a página não atualizou o estado visual

### Solução — Passo a Passo:

**Passo 1 — Recarrega a página:**
Pressiona **F5** (ou Ctrl + R). O dashboard vai recarregar e os números devem atualizar.

> 💡 **As tuas definições ficam guardadas** no browser (localStorage), por isso não perdes nada ao recarregar.

**Passo 2 — Abre a consola do browser para ver erros:**
1. Pressiona **F12**
2. Clica no separador **"Console"**
3. Vês algo a vermelho? Copia o erro e pesquisa ou pergunta.

**Passo 3 — Desativa o "sleep" de abas no Chrome:**
O Chrome às vezes "dorme" abas que não estás a ver. Para evitar:
- Chrome → `chrome://flags/#enable-throttle-display-none-and-visibility-hidden-cross-origin-iframes`
- Ou simplesmente: **mantém a aba do dashboard visível** (não minimizes o browser)

**Passo 4 — Se o bot estava a correr e parou:**
- No dashboard, clica em **"⏹ PARAR BOT"** e depois **"▶ INICIAR BOT"** de novo
- Ou recarrega a página e reinicia

**Passo 5 — Se nada funciona:**
- Fecha o browser completamente
- Abre de novo
- Abre o `dashboard.html`
- Clica "Iniciar Bot"

---

## Problemas Extra (Bónus)

### "O bot dá erro quando tento mainnet mas eu tenho uma carteira na Hyperliquid"

O bot precisa de uma **API Wallet** específica, não da tua carteira principal.

1. Vai a [app.hyperliquid.xyz](https://app.hyperliquid.xyz)
2. Clica no teu nome → "API Wallets"
3. Cria uma API Wallet nova (ex: "BotTrading")
4. Usa o **Wallet Address** e **Private Key** dessa API Wallet — não da tua main wallet

### "Backtest diz 'Dados não encontrados'"

Precisas de descarregar dados primeiro:
```powershell
python src/data_downloader.py BTCUSDT 30
```
Isto cria os ficheiros CSV na pasta `data/`. Só depois podes correr o backtest.

### "O terminal fecha logo que abro o bot"

O bot está a dar um erro fatal que fecha o terminal.

**Solução:**
1. Abre o PowerShell MANUALMENTE (não com duplo-clique)
2. Navega para a pasta do bot
3. Corre `python src/main.py`
4. Se der erro, o terminal fica aberto e vês a mensagem

---

## Onde Obter Mais Ajuda

1. **Verifica primeiro:** Este ficheiro e o `README.md`
2. **Corre o diagnóstico:** `python diagnose.py`
3. **Testa as APIs:** `python src/data_aggregator.py`
4. **Abre uma issue no GitHub** com:
   - O que estavas a fazer
   - O erro exato (copia e cola)
   - O sistema operativo (Windows 10/11)
   - Versão do Python (`python --version`)

---

> 🛠️ **A maior parte dos problemas tem solução simples. Respira, lê o erro com atenção, e segue os passos. Não inventes — o manual existe por uma razão.**
