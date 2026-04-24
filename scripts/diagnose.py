"""
DIAGNÓSTICO RÁPIDO — corre isto no teu PC para verificar tudo
"""
import requests
import json
import sys
from pathlib import Path

print("=" * 70)
print("🔍 DIAGNÓSTICO DO BOT — HYPERLIQUID")
print("=" * 70)

# 1. Verificar preço real na API
print("\n📡 1. A BUSCAR PREÇO REAL NA API...")
try:
    resp = requests.post(
        'https://api.hyperliquid.xyz/info',
        json={'type': 'allMids'},
        headers={'Content-Type': 'application/json'},
        timeout=15
    )
    data = resp.json()
    real_btc = float(data.get('BTC', 0))
    print(f"   ✅ API Hyperliquid: BTC = ${real_btc:,.2f}")
except Exception as e:
    print(f"   ❌ ERRO na API: {e}")
    real_btc = None

# 2. Verificar preço na Binance (referência)
print("\n📡 2. A COMPARAR COM BINANCE...")
try:
    resp = requests.get('https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT', timeout=10)
    binance_btc = float(resp.json().get('price', 0))
    print(f"   ✅ Binance: BTC = ${binance_btc:,.2f}")
    if real_btc:
        diff = abs(real_btc - binance_btc)
        print(f"   📊 Diferença: ${diff:,.2f} ({diff/real_btc*100:.2f}%)")
except Exception as e:
    print(f"   ❌ Erro Binance: {e}")

# 3. Verificar o que o teu data_aggregator.py está a fazer
print("\n🔎 3. A VERIFICAR CÓDIGO LOCAL...")
src_path = Path(__file__).parent / 'src' / 'data_aggregator.py'
if src_path.exists():
    code = src_path.read_text()
    
    # Verificar se tem validação de sanidade
    if '_is_price_sane' in code:
        print("   ✅ Código tem validação de preço (VERSÃO NOVA)")
    else:
        print("   ⚠️ Código NÃO tem validação de preço (VERSÃO VELHA)")
    
    # Verificar se tem metaAndAssetCtxs fallback
    if 'metaAndAssetCtxs' in code:
        print("   ✅ Código tem fallback metaAndAssetCtxs (VERSÃO NOVA)")
    else:
        print("   ⚠️ Código NÃO tem fallback (VERSÃO VELHA)")
    
    # Verificar se tem logs detalhados
    if 'raw:' in code:
        print("   ✅ Código tem logs de debug (VERSÃO NOVA)")
    else:
        print("   ⚠️ Código NÃO tem logs de debug (VERSÃO VELHA)")
else:
    print(f"   ❌ Ficheiro não encontrado: {src_path}")

print("\n" + "=" * 70)
print("📋 RESULTADO:")
print("=" * 70)

if real_btc and 50000 < real_btc < 200000:
    print(f"\n   ✅ API está OK — BTC a ${real_btc:,.2f}")
    print(f"\n   ⚠️ Se o teu dashboard mostra outro valor,")
    print(f"      o problema é o CÓDIGO DESACTUALIZADO no teu PC!")
    print(f"\n   🛠️ SOLUÇÃO:")
    print(f"      1. Para o bot (Ctrl+C)")
    print(f"      2. Substitui o ficheiro src/data_aggregator.py")
    print(f"      3. Reinicia o bot")
else:
    print(f"\n   ❌ API está a devolver valor estranho: {real_btc}")
    print(f"\n   Pode ser:")
    print(f"      - Problema temporário na API")
    print(f"      - Rate limiting")
    print(f"      - Conexão bloqueada (VPN/Firewall)")

print("\n" + "=" * 70)
