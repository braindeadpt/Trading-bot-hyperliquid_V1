# 🤖 Hyperliquid Momentum Bot

**Bot de trading automatizado para a Hyperliquid**  
_Versão: 0.1.0 | Idioma: Português (Portugal)_

---

## O que é isto? (Em português simples)

Imagina que tens um amigo que está sempre de olho no Bitcoin, 24 horas por dia, 7 dias por semana. Ele não dorme, não come, não se distrai. Só observa.

Quando detecta que **muito dinheiro novo está a entrar no mercado** (Open Interest a subir) **e o volume de trading explodiu** (spike de volume), ele dá-te um sinal: "Epá, isto pode subir."

Esse amigo é este bot.

Ele não adivinha o futuro. Ele **lê o presente** com números — e age com disciplina.

### O que o bot faz, resumido:

1. **Busca dados** de 4 exchanges (Binance, Bybit, OKX, Hyperliquid)
2. **Calcula** se há volume anormal e dinheiro novo a entrar
3. **Decide** se entra num trade (LONG ou SHORT)
4. **Gerencia o risco** — stop loss, trailing stop, limites diários
5. **Executa** em paper trading (simulação) ou mainnet (dinheiro real)

---

## 🛠️ Instalação — Passo a Passo

### Requisitos

- **Windows 10/11** (ou Linux/Mac, mas o guia foca em Windows porque é o que o Pedro usa)
- **Python 3.10 ou superior**
- **Git** (opcional, mas recomendado)

### Passo 1 — Instalar Python

1. Vai a [python.org/downloads](https://www.python.org/downloads/)
2. Clica no botão amarelo para descarregar o instalador para Windows
3. Corre o ficheiro `.exe`
4. **IMPORTANTE:** Na primeira janela do instalador, marca a caixinha que diz **"Add Python to PATH"**
   - Se não marcares, o teu computador não vai reconhecer o comando `python` depois
5. Clica "Install Now"
6. Para confirmar que funcionou, abre o PowerShell e escreve:
   ```powershell
   python --version
   ```
   Deve aparecer algo como `Python 3.12.2`.

> 💡 **Dica:** Se o comando `python` não for reconhecido, tenta `py --version` em vez disso. Algumas instalações do Windows usam `py` em vez de `python`.

### Passo 2 — Descarregar o Bot

**Opção A — Com Git (recomendado, mais fácil de atualizar depois):**

```powershell
git clone https://github.com/braindeadpt/trading-bot-hyperliquid.git
cd trading-bot-hyperliquid
```

**Opção B — Sem Git (ZIP):**

1. Vai ao repositório no GitHub
2. Clica no botão verde **"<> Code"**
3. Seleciona **"Download ZIP"**
4. Extrai o ZIP para uma pasta (ex: `C:\Users\O_Teu_Nome\Documents\trading-bot-hyperliquid`)
5. Abre o PowerShell dentro dessa pasta

### Passo 3 — Instalar Dependências

Ainda no PowerShell, dentro da pasta do bot:

```powershell
python -m pip install -r requirements.txt
```

> ⚠️ **Nota:** Se o comando `pip` não funcionar sozinho, usa sempre `python -m pip` em vez disso. O `python -m pip` é mais fiável no Windows.

Isto instala as bibliotecas que o bot precisa para funcionar (como `requests`, `flask`, etc.).

### Passo 4 — Verificar que Tudo Funciona

```powershell
python tests/test_strategy.py
```

Se correr sem erros, estás pronto para o próximo passo! 🎉

---

## ⚙️ Configuração

### Onde estão as definições?

O bot usa dois ficheiros principais para configuração:

#### 1. `config/settings.yaml` — O Cérebro do Bot

Abre este ficheiro com o Notepad (ou qualquer editor de texto). Vês algo assim:

```yaml
bot:
  name: "Hyperliquid Momentum Bot"
  version: "0.1.0"
  paper_trading: true   # ⬅️ Isto é IMPORTANTE

assets:
  - "BTC"              # Só Bitcoin por agora

timeframes:
  primary: "15m"      # Timeframe principal: 15 minutos

strategy:
  volume_spike_threshold: 4.0    # Spike de volume: 4x a média
  oi_change_threshold: 0.01     # OI a subir 1%
  stop_loss_pct: 0.02           # Stop loss: 2%
  trailing_stop_pct: 0.015      # Trailing stop: 1.5%

risk:
  max_position_size_usd: 100     # Máximo $100 por posição
  max_leverage: 2               # Alavancagem máxima: 2x
  max_daily_trades: 5           # Máximo 5 trades por dia
```

> 📝 **Não tenhas medo de editar:** Estes são números, não código. Podes alterá-los com o Notepad, guardar, e pronto.

#### 2. `.env` — Credenciais (só para Mainnet)

Copia o ficheiro `.env.example` para `.env`:

```powershell
copy .env.example .env
```

Depois abre o `.env` e preenche os dados da tua carteira Hyperliquid **SÓ quando fores para mainnet**.

---

## 🧪 Correr em Testnet (Paper Trading — Simulação)

**Isto é o primeiro passo OBRIGATÓRIO.** Nunca ponhas dinheiro real sem testares primeiro.

### Método 1 — Bot no Terminal (Simples)

```powershell
python src/main.py
```

Vais ver mensagens no terminal a dizer coisas como:
```
[SAT] Analisando BTC...
OI: $45,000,000,000 | OI Δ: +1.20% | Vol: 4.5x média | Funding: 0.0050% | Preço: $67,432.50
```

O bot está a trabalhar! Para parar, pressiona **Ctrl + C**.

### Método 2 — Dashboard Web (Recomendado)

O dashboard é uma página HTML que abre no teu browser — visual, cypherpunk, e muito mais fácil de ler.

1. Abre o ficheiro `dashboard.html` no teu browser:
   - Clica duas vezes no ficheiro, ou
   - Arrasta-o para uma janela do browser
2. No painel da esquerda, confirma que **Network = Testnet**
3. Clica no botão **"▶ INICIAR BOT"**
4. Observa os painéis a atualizarem-se em tempo real

> 📖 **Ver o guia completo do dashboard:** `DASHBOARD_GUIDE.md`

---

## 💰 Correr em Mainnet (Dinheiro REAL)

### ⚠️⚠️⚠️ AVISOS GRANDES ⚠️⚠️⚠️

- **NUNCA** deposites mais dinheiro do que podes perder.
- O bot pode perder dinheiro. Nenhuma estratégia é infalível.
- Começa com valores muito pequenos ($10-$20).
- Testa em testnet pelo menos **1 semana** antes de pensar em mainnet.
- O mercado de crypto é volátil. Podes perder tudo numa noite.

### Passos para Mainnet

1. **Configura a carteira:**
   - Vai a [app.hyperliquid.xyz](https://app.hyperliquid.xyz)
   - Cria uma API Wallet dentro da plataforma
   - Copia o **Wallet Address** (começa com `0x`, 42 caracteres)
   - Copia a **Private Key** da API Wallet (começa com `0x`, 66 caracteres)

2. **Preenche os dados no dashboard ou no `.env`:**
   - Wallet Address
   - Private Key
   - Nome da API Wallet (ex: "BotTrading")

3. **Testa a ligação:**
   - No dashboard, clica em **"▶ TESTAR LIGAÇÃO"**
   - Só prossegues se disser **"Ligação OK!"**

4. **Muda a Network para Mainnet:**
   - No dashboard, muda o dropdown de "Testnet" para "Mainnet"
   - O painel fica a laranja/aviso — isto é normal, é para te lembrar que é dinheiro real

5. **Ajusta os valores:**
   - Capital: quanto tens na conta (ex: $500)
   - Position Size: $20 (muito pequeno para começar!)
   - Leverage: 1x ou 2x (nunca mais que isso no início)

6. **Inicia:**
   - Clica **"▶ INICIAR BOT"**
   - Não minimizes a janela — observa os primeiros trades

---

## 🛑 Como Parar o Bot em Segurança

### Se estás no terminal (PowerShell):

- Pressiona **Ctrl + C** — o bot para graciosamente e fecha posições se necessário.

### Se estás no Dashboard:

- Clica no botão **"⏹ PARAR BOT"**
- Se tiveres uma posição aberta, clica **"🚨 EMERGENCY CLOSE"** para fechar imediatamente

### Se o computador travar ou o bot "congelar":

1. Fecha a janela do PowerShell/browser
2. Abre o site da Hyperliquid diretamente
3. Fecha as posições manualmente lá

> 🔥 **Regra de ouro:** Se algo corre mal, pára tudo. Melhor perder uma oportunidade do que perder dinheiro por pânico.

---

## 🔧 Resolução de Problemas (Troubleshooting)

### 1. "pip não é reconhecido como comando"

**Solução:**
```powershell
python -m pip install -r requirements.txt
```
Se ainda não funcionar, o Python não está no PATH. Reinstala o Python e marca a opção "Add Python to PATH".

### 2. "Preço mostra valor errado ou zero"

**Solução:**
- Verifica se tens internet
- Corre `python src/data_aggregator.py` para testar as APIs
- Às vezes a Hyperliquid ou a Binance têm manutenção — espera 5 minutos e tenta de novo

### 3. "Bot não inicia — dá erro de rede"

**Solução:**
- Confirma que `config/settings.yaml` existe e não está vazio
- Verifica se o ficheiro tem indentação correta (os espaços no início das linhas importam em YAML!)

### 4. "Não acontecem trades"

**Solução:**
- O bot só trade quando há um spike de volume + OI a subir. Nem todos os dias há sinais.
- Diminui o `volume_spike_threshold` para `3.0` em `settings.yaml` para ser mais sensível.
- Confirma que estás a usar `paper_trading: true` para não precisares de carteira.

### 5. "Dashboard não atualiza"

**Solução:**
- Recarrega a página (F5)
- Abre a consola do browser (F12 → separador "Console") e vê se há erros vermelhos
- Confirma que o browser tem acesso à internet (a dashboard busca dados das APIs diretamente)

---

## 📚 Documentação Adicional

| Ficheiro | O que encontras lá |
|----------|---------------------|
| `STRATEGY.md` | Explicação detalhada da estratégia de trading |
| `DASHBOARD_GUIDE.md` | Guia completo para usar o dashboard.html |
| `TROUBLESHOOTING.md` | Problemas comuns e soluções detalhadas |
| `CHANGELOG.md` | Histórico de versões e funcionalidades |

---

## 💬 Precisas de Ajuda?

- Abre uma _issue_ no GitHub se encontrares um bug
- Verifica o `TROUBLESHOOTING.md` antes de perguntar — a resposta pode já estar lá
- Lembra-te: **todos começam do zero.** Não há perguntas estúpidas, só respostas que ainda não encontraste.

---

_Bom trading, e lembra-te: o bot é uma ferramenta. Tu és o piloto. Mantém o controlo._ 🎯
