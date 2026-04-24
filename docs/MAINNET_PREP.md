# 🛡️ MAINNET_PREP.md — Preparação para Mainnet
## Hyperliquid Momentum Bot v0.1.0

---

## ⚠️ AVISO LEGAL E RISCOS

> **TRADING COM DINHEIRO REAL ENVOLVE RISCO SIGNIFICATIVO DE PERDA.**
> 
> O bot pode perder dinheiro. Pode perder **TODO** o dinheiro que depositares na exchange. Não deposites mais do que estás disposto a perder. Nunca deixes mais de $500 na exchange dedicada ao bot.
>
> Este bot é software experimental em desenvolvimento activo. Não há garantias de lucro. Resultados passados (backtests) não garantem resultados futuros.

---

## 🔴 ESTADO ATUAL: NÃO ESTÁ PRONTO PARA MAINNET

**Veredicto do Guardian Report V2:** 🔴 **NO-GO FOR MAINNET**

O bot **NÃO DEVE** ser usado com dinheiro real nesta versão (v0.1.0). Razões:

| # | Bloqueador | Estado |
|---|-----------|--------|
| 1 | **Execução real não implementada** | `exchange_client.py` está vazio — levanta `NotImplementedError` |
| 2 | **Sem circuit breaker de perda diária** | O bot pode perder até 50% do capital num dia sem parar |
| 3 | **Sem graceful shutdown com fecho de posição** | Se o bot crashar, a posição fica aberta sem protecção |
| 4 | **Sem stop-loss ao nível da exchange** | Todos os stops são client-side — se o processo morrer, não há protecção |
| 5 | **Sem validação de ordens** | Não verifica tamanho mínimo, slippage, margem, ou desvio de preço |
| 6 | **Sem rate limiting** | Podes ser banido pela Hyperliquid por excesso de requests |
| 7 | **Sem autenticação no dashboard** | Qualquer pessoa na rede local pode aceder ao dashboard |

---

## ✅ Checklist de Segurança (Guardian Report)

Antes de considerar mainnet, **TODAS** estas condições têm de estar verificadas:

### Fase 0 — Infraestrutura de Segurança (OBRIGATÓRIO)

| Item | Requisito | Status Actual |
|------|-----------|---------------|
| Circuit breaker diário | Parar se perder 5% (soft) ou 10% (hard) num dia | ❌ Não implementado |
| Graceful shutdown | Fechar posição ao parar o bot | ❌ Não implementado |
| Stop-loss na exchange | Colocar stop na Hyperliquid, não só no bot | ❌ Não implementado |
| Validação de ordens | Verificar tamanho, margem, slippage, preço | ❌ Não implementado |
| Rate limiting | Máximo 120 requests/minuto na Hyperliquid | ❌ Não implementado |
| HTTPS forçado | Verificar que todas as APIs usam HTTPS | ⚠️ Parcial |
| Testnet/mainnet switch | Configuração explícita com confirmação em dois passos | ❌ Não implementado |
| Auth no dashboard | Login obrigatório no dashboard | ❌ Não implementado |
| 50+ testes de risco | Testes unitários para todos os controlos de risco | ❌ Não implementado |

### Fase 1 — Validação em Paper Trading

| Item | Requisito | Duração |
|------|-----------|---------|
| Paper trading contínuo | Correr 24/7 com logging completo | 30 dias |
| Simular circuit breaker | Forçar perda de 5% e verificar se para | 1 dia |
| Verificar graceful shutdown | Parar com posição aberta, verificar se fecha | Testes repetidos |
| Verificar MTF | Confirmar que thread de 5m não usa dados desactualizados | Contínuo |

### Fase 2 — Validação em Testnet

| Item | Requisito | Duração |
|------|-----------|---------|
| Primeiro trade | $1 em testnet para validar execução | 1 dia |
| Execução contínua | Correr com trades reais em testnet | 2 semanas |
| Testar circuit breaker | Simular perda em testnet | 1 dia |
| Testar shutdown | Parar durante posição aberta em testnet | Testes repetidos |
| Testar stop-loss na exchange | Verificar se stop executa na exchange | 1 dia |

### Fase 3 — Graduação para Mainnet

| Item | Requisito |
|------|-----------|
| Primeiro trade real | $10 (1% do capital inicial) |
| Semana 1-2 | $10 por posição, monitorização intensiva |
| Semana 3-4 | Aumentar para $25 se tudo correr bem |
| Mês 2+ | Aumentar gradualmente, nunca mais de $100 sem backtest |
| **NUNCA** | Desactivar o circuit breaker |

---

## 📋 Como Configurar a Carteira (Quando Estiver Pronto)

### Passo 1 — Criar Wallet Dedicada

1. Instala **MetaMask** ou **Rabby** (não uses a wallet pessoal!)
2. Cria uma **wallet NOVA** exclusivamente para o bot
3. Anota a seed phrase em papel (nunca digitalmente)
4. Guarda a wallet address (começa com `0x...`)

### Passo 2 — Gerar API Key na Hyperliquid

1. Vai ao painel da Hyperliquid → Settings → API Keys
2. Cria uma API Key **dedicada ao bot**
3. Copia a **Private Key** (64 caracteres hexadecimais)
4. **NUNCA** partilhes esta chave com ninguém

### Passo 3 — Configurar no Bot

1. Abre o dashboard
2. Muda Network para **Mainnet**
3. Preenche:
   - **Account Wallet Address:** o teu endereço público `0x...`
   - **API Wallet Private Key:** a chave privada da API
   - **API Wallet Name:** um nome (ex: "BotTrading")
4. Clica em **"▶ TESTAR LIGAÇÃO"**
5. Se aparecer "Ligação OK", clica **"💾 GUARDAR"**

### Passo 4 — Segurança da Chave

- **NUNCA** commites a chave no GitHub
- **NUNCA** a envies por email ou mensagem
- **NUNCA** a guardes num ficheiro de texto sem encriptação
- Usa variáveis de ambiente ou ficheiro `.env` (adicionado ao `.gitignore`)

---

## 🧪 Como Testar a Ligação

O dashboard tem um botão **"▶ TESTAR LIGAÇÃO"** que verifica:

1. Se o endereço da wallet é válido
2. Se a chave privada está correcta
3. Se consegue comunicar com a API da Hyperliquid
4. Se a conta tem saldo suficiente

Resultados possíveis:

| Resultado | Significado |
|-----------|-------------|
| ✅ Ligação OK | Tudo pronto para mainnet |
| ⚠️ Sem saldo | Carteira correcta mas sem USDC — deposita fundos |
| ❌ Chave inválida | A private key está errada — verifica caracteres |
| ❌ Sem resposta | Hyperliquid API offline ou bloqueada |

---

## 💰 Estratégia de Capital Recomendada

### Regra de Ouro: Start Small, Scale Up

```
Semana 1-2:   $10 por posição  ($50-100 total na exchange)
Semana 3-4:   $25 por posição  (se PnL positivo)
Mês 2:        $50 por posição  (se continuar lucrativo)
Mês 3+:       $100 por posição (se backtest validar)
NUNCA:        Mais de 10% do capital total por posição
NUNCA:        Mais de $500 total na exchange
```

### Limites Configurados no Bot

O `config/settings.yaml` já tem limites de segurança:

```yaml
risk:
  mainnet_max_position_usd: 50    # Tamanho máximo em mainnet
  mainnet_first_trade_size: 10     # Primeiro trade: $10
  daily_loss_limit_pct: 0.05       # Soft stop: 5% perda diária
  daily_loss_hard_stop_pct: 0.10   # Hard stop: 10% perda diária
```

> ⚠️ **NOTA:** Estes valores são configurações. O código que os impõe ainda não está implementado. Não confies neles até serem activos.

---

## 🎯 Roadmap para Mainnet

```
AGORA (v0.1.0)
    ↓
[Paper Trading 30 dias] → Validar sinais e PnL
    ↓
[Implementar segurança] → Circuit breaker, graceful shutdown, exchange stops
    ↓
[Testnet 2 semanas] → $1 trades reais, validar execução
    ↓
[Mainnet $10] → Primeiro trade com dinheiro real
    ↓
[Mainnet $25-50] → Escalar gradualmente
    ↓
[Mainnet $100] → Após 1 mês de PnL positivo
```

---

## 📚 Recursos Adicionais

- [Hyperliquid Docs](https://hyperliquid.gitbook.io/hyperliquid-docs) — Documentação oficial
- [Hyperliquid Python SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) — SDK para integração
- [Risk Management Guide](https://www.investopedia.com/articles/trading/09/risk-management.asp) — Gestão de risco

---

*Última actualização: 2026-04-24 | Versão do bot: v0.1.0 | Estado: NÃO PRONTO PARA MAINNET*
