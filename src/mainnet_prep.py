"""
Exchange Client — Preparação para execução real (mainnet)
Stub com requisitos e pseudo-código para integração com Hyperliquid SDK
"""

# =============================================================================
# REQUISITOS PARA MAINNET
# =============================================================================

MAINNET_CHECKLIST = {
    "wallet": {
        "desc": "Wallet EVM (MetaMask / Rabby / etc.)",
        "status": "PENDENTE",
        "action": "Criar wallet dedicada ao bot (não usar wallet pessoal!)"
    },
    "funding": {
        "desc": "Transferir USDC para Hyperliquid",
        "status": "PENDENTE",
        "action": "Depositar $50-100 USDC na Hyperliquid (começar pequeno!)"
    },
    "api_key": {
        "desc": "API key da Hyperliquid",
        "status": "PENDENTE",
        "action": "Gerar API key no painel da Hyperliquid"
    },
    "sdk": {
        "desc": "Hyperliquid Python SDK",
        "status": "PENDENTE",
        "action": "pip install hyperliquid-python-sdk"
    },
    "testnet_validation": {
        "desc": "2 semanas em testnet com $1",
        "status": "PENDENTE",
        "action": "Correr bot em testnet, validar sinais e PnL"
    },
    "mainnet_first_trade": {
        "desc": "Primeiro trade em mainnet ($10)",
        "status": "PENDENTE",
        "action": "Trade mínimo para validar execução real"
    },
}


# =============================================================================
# PSEUDO-CÓDIGO: Execução Real
# =============================================================================

"""
Quando for implementar execução real, seguir este padrão:

1. INSTALAR SDK:
   pip install hyperliquid-python-sdk

2. INICIALIZAR CLIENTE REAL:
   from hyperliquid.exchange import Exchange
   from hyperliquid.utils import constants
   
   exchange = Exchange(
       wallet_address="0x...",  # Wallet do bot
       private_key="0x...",      # Chave privada (GUARDAR EM SEGURANÇA!)
       base_url=constants.MAINNET_API_URL  # ou TESTNET
   )

3. COLOCAR ORDEM:
   # Converter side do bot ('BUY'/'SELL') para formato Hyperliquid
   is_buy = side == 'BUY'
   
   # Calcular tamanho em tokens (não USD)
   size_tokens = size_usd / current_price
   
   # Arredondar para lot size da Hyperliquid
   # BTC: 0.001, ETH: 0.01, etc.
   
   # Colocar ordem a mercado
   result = exchange.market_open(
       name=asset,           # "BTC"
       is_buy=is_buy,        # True/False
       sz=size_tokens,       # Tamanho em tokens
       slippage=0.005        # 0.5% slippage max
   )

4. COLOCAR STOP-LOSS:
   exchange.order(
       name=asset,
       is_buy=not is_buy,    # Lado oposto para fechar
       sz=size_tokens,
       limit_px=stop_price,  # Preço do stop
       order_type="Stop Market"
   )

5. FECHAR POSIÇÃO:
   exchange.market_close(
       name=asset,
       sz=size_tokens
   )

6. VALIDAR EXECUÇÃO:
   - Verificar status da ordem
   - Confirmar preenchimento
   - Logar tx hash para auditoria
   - Atualizar estado interno do bot
"""

# =============================================================================
# SEGURANÇA — LEIA ANTES DE IMPLEMENTAR
# =============================================================================

SECURITY_NOTES = """
⚠️ IMPORTANTE:

1. NUNCA committar chaves privadas no repo!
   - Usar variáveis de ambiente: os.environ['HYPERLIQUID_PK']
   - Ou ficheiro .env (adicionado ao .gitignore!)

2. Wallet dedicada ao bot:
   - Não usar wallet pessoal
   - Fundo máximo inicial: $50-100
   - Nunca deixar mais de $500 na exchange

3. Rate limits:
   - Hyperliquid: 120 requests/minuto
   - Implementar rate limiting no cliente

4. Idempotência:
   - Gerar client_order_id único por ordem
   - Verificar ordens pendentes antes de enviar nova

5. Fallback:
   - Se execução real falhar, voltar para paper trading
   - Alertar utilizador (email/discord/sms)
"""


if __name__ == "__main__":
    print("=" * 60)
    print("  HYPERLIQUID — PREPARAÇÃO PARA MAINNET")
    print("=" * 60)
    print()
    for key, item in MAINNET_CHECKLIST.items():
        status_icon = "✅" if item['status'] == 'DONE' else "⬜"
        print(f"  {status_icon} {item['desc']}")
        print(f"     → {item['action']}")
        print()
    print("=" * 60)
    print(SECURITY_NOTES)
