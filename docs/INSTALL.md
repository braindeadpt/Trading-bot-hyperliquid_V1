# 📦 INSTALL.md — Guia de Instalação
## Hyperliquid Momentum Bot v0.1.0

---

## 🖥️ Requisitos de Sistema

| Componente | Versão Mínima | Notas |
|------------|---------------|-------|
| **Windows** | 10 ou 11 | Funciona em qualquer versão actualizada |
| **Python** | 3.10+ | Recomendado: Python 3.12 (instalar do [python.org](https://python.org)) |
| **Git** | 2.30+ | Para clonar o repositório (instalar do [git-scm.com](https://git-scm.com)) |
| **Navegador** | Chrome, Edge, Firefox | Qualquer navegador moderno serve |
| **Ligação à Internet** | Estável | Necessária para dados de mercado em tempo real |

---

## 🚀 Instalação Passo a Passo

### Passo 1 — Instalar Python

1. Vai a [python.org/downloads](https://python.org/downloads)
2. Descarrega o instalador para Windows (64-bit)
3. **IMPORTANTE:** Durante a instalação, marca a opção **"Add Python to PATH"**
4. Clica em "Install Now"
5. Verifica a instalação abrindo o PowerShell e escrevendo:
   ```powershell
   python --version
   ```
   Deve aparecer algo como `Python 3.12.x`

### Passo 2 — Instalar Git

1. Vai a [git-scm.com/download/win](https://git-scm.com/download/win)
2. Descarrega e executa o instalador
3. Aceita as opções por defeito
4. Verifica no PowerShell:
   ```powershell
   git --version
   ```

### Passo 3 — Clonar o Repositório

Abre o PowerShell (ou CMD) na pasta onde queres instalar:

```powershell
cd C:\Users\%USERNAME%\Documents
git clone https://github.com/braindead/hyperliquid-momentum-bot.git
cd hyperliquid-momentum-bot
```

> 💡 Se não usares Git, podes descarregar o ZIP do projeto e extrair para a pasta desejada.

### Passo 4 — Instalar Dependências

Ainda na pasta do projeto, executa:

```powershell
python -m pip install flask pystray pillow
```

Ou, se tiveres um ficheiro `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

Dependências instaladas:
- **Flask** — servidor web do dashboard
- **pystray** — ícone na barra de tarefas (system tray)
- **Pillow** — manipulação de imagens para o ícone

---

## ▶️ Como Iniciar o Bot

### Método 1 — Duplo clique (Recomendado)

1. Abre a pasta do bot no Explorador de Ficheiros
2. Encontra o ficheiro **`start.bat`**
3. **Duplo clique** em `start.bat`
4. O bot arranca automaticamente:
   - Abre uma janela do terminal
   - Inicia o motor do bot
   - Abre o dashboard no teu navegador
   - Coloca um ícone verde na barra de tarefas (system tray)

### Método 2 — Linha de comandos

```powershell
cd C:\Users\%USERNAME%\Documents\hyperliquid-momentum-bot
python app_flask.py
```

---

## 🛑 Como Parar o Bot

Tens **três formas** de parar o bot:

| Método | Como fazer |
|--------|-----------|
| **System Tray** | Clica no ícone verde (canto inferior direito) → "Sair" |
| **Fechar janela** | Clica no X da janela do terminal |
| **Teclado** | Na janela do terminal, pressiona `Ctrl + C` |

> ⚠️ Quando paras o bot, qualquer posição aberta em **paper trading** é simulada e ficará registada na base de dados. Em modo real, a posição continua aberta na exchange — o bot não a fecha automaticamente (ver [MAINNET_PREP.md](MAINNET_PREP.md)).

---

## 🔧 Resolução de Problemas na Instalação

### "python" não é reconhecido como comando
- Python não está no PATH. Reinstala Python e marca "Add Python to PATH".

### "pip" não é reconhecido
- Usa `python -m pip install ...` em vez de `pip install ...`

### Erro "No module named 'flask'"
- As dependências não estão instaladas. Corre `python -m pip install flask pystray pillow`.

### O `start.bat` abre e fecha logo
- Abre o PowerShell na pasta do bot e corre `python app_flask.py` manualmente para ver a mensagem de erro.

---

## ✅ Verificação Rápida

Depois de instalar, confirma que tudo funciona:

1. ✅ Duplo clique em `start.bat` → janela abre
2. ✅ Aparece "Bot a correr em http://127.0.0.1:5000"
3. ✅ O navegador abre com o dashboard cypherpunk
4. ✅ O preço do BTC aparece no painel "Mercado em Tempo Real"
5. ✅ O ícone verde aparece na system tray

Se todos estes passos funcionarem, a instalação está completa! 🎉

---

## 📂 Estrutura de Pastas Após Instalação

```
hyperliquid-momentum-bot/
├── app_flask.py          ← Aplicação principal (Flask + Tray)
├── bot_engine.py         ← Motor do bot (thread separada)
├── bridge.js             ← Ponte entre dashboard e Python
├── dashboard.html        ← Interface web (cypherpunk terminal)
├── start.bat             ← Script de arranque (duplo clique!)
├── config/
│   └── settings.yaml     ← Configurações do bot
├── src/                  ← Código-fonte dos módulos
│   ├── data_aggregator.py
│   ├── paper_trading.py
│   ├── strategy.py
│   └── ...
├── tests/                ← Testes automatizados
├── docs/                 ← Documentação (estás aqui!)
└── logs/                 ← Logs de execução
```

---

*Última actualização: 2026-04-24 | Versão do bot: v0.1.0*
