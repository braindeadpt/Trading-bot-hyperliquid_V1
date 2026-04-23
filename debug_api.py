"""
DEBUG COMPLETO - Hyperliquid API Price Fetcher
Corre isto no teu PC para ver EXACTAMENTE o que a API devolve
"""
import requests
import json
import time

print("=" * 70)
print("🔍 DEBUG COMPLETO - HYPERLIQUID API")
print("=" * 70)

# Teste 1: allMids
print("\n📡 TESTE 1: allMids")
print("-" * 50)
resp = requests.post(
    'https://api.hyperliquid.xyz/info',
    json={'type': 'allMids'},
    headers={'Content-Type': 'application/json'},
    timeout=15
)

print(f"Status HTTP: {resp.status_code}")
print(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")

try:
    data = resp.json()
    print(f"Tipo de resposta: {type(data).__name__}")
    
    if isinstance(data, dict):
        print(f"Total de keys: {len(data)}")
        
        # Procurar BTC em todas as formas possíveis
        btc_keys = [k for k in data.keys() if 'BTC' in k.upper()]
        print(f"\nKeys com 'BTC': {btc_keys}")
        
        for key in btc_keys[:5]:  # Mostrar primeiros 5
            val = data[key]
            print(f"  {key}: {val} (tipo: {type(val).__name__})")
        
        # Verificar se BTC existe exactamente
        if 'BTC' in data:
            raw = data['BTC']
            print(f"\n✅ Key 'BTC' encontrada!")
            print(f"  Valor raw: '{raw}'")
            print(f"  Tipo: {type(raw).__name__}")
            try:
                price = float(raw)
                print(f"  Convertido: ${price:,.2f}")
                # Verificar se é realista
                if 10000 < price < 200000:
                    print(f"  ✅ Preço parece REALISTA")
                else:
                    print(f"  ⚠️⚠️⚠️ PREÇO FORA DO ESPERADO!")
            except:
                print(f"  ❌ Não é convertível para float!")
        else:
            print(f"\n❌ Key 'BTC' NÃO encontrada!")
            print(f"   Primeiras 20 keys: {list(data.keys())[:20]}")
    
    elif isinstance(data, list):
        print(f"Resposta é lista com {len(data)} itens")
        if data:
            print(f"Primeiro item: {data[0]}")
    
    else:
        print(f"Tipo inesperado: {type(data)}")
        print(f"Valor: {str(data)[:200]}")
        
except Exception as e:
    print(f"❌ Erro a processar: {e}")
    print(f"Texto raw (primeiros 300 chars): {resp.text[:300]}")

# Teste 2: metaAndAssetCtxs (alternativa mais rica)
print("\n" + "=" * 70)
print("📡 TESTE 2: metaAndAssetCtxs")
print("-" * 50)
resp2 = requests.post(
    'https://api.hyperliquid.xyz/info',
    json={'type': 'metaAndAssetCtxs'},
    headers={'Content-Type': 'application/json'},
    timeout=15
)

try:
    data2 = resp2.json()
    if isinstance(data2, list) and len(data2) >= 2:
        meta = data2[0]
        ctxs = data2[1]
        print(f"meta type: {type(meta).__name__}, ctxs type: {type(ctxs).__name__}")
        
        # Procurar BTC nos contexts
        if isinstance(ctxs, list):
            for ctx in ctxs[:3]:
                if isinstance(ctx, dict):
                    coin = ctx.get('coin', 'N/A')
                    mid = ctx.get('midPx', 'N/A')
                    mark = ctx.get('markPx', 'N/A')
                    print(f"  Coin: {coin} | midPx: {mid} | markPx: {mark}")
    else:
        print(f"Resposta inesperada: {type(data2)}")
        print(f"Preview: {str(data2)[:200]}")
except Exception as e:
    print(f"Erro: {e}")
    print(f"Raw: {resp2.text[:200]}")

# Teste 3: Verificar preço real para comparação
print("\n" + "=" * 70)
print("📡 TESTE 3: Comparação com Binance (referência)")
print("-" * 50)
try:
    binance_resp = requests.get(
        'https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT',
        timeout=10
    )
    binance_data = binance_resp.json()
    binance_price = float(binance_data.get('price', 0))
    print(f"Binance BTC: ${binance_price:,.2f}")
except Exception as e:
    print(f"Erro Binance: {e}")

print("\n" + "=" * 70)
