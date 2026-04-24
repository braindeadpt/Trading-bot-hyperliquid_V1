"""
Teste rápido para verificar o preço da Hyperliquid API
Corre isto no teu PC para ver o que a API está a devolver
"""
import requests
import json

print("=" * 60)
print("TESTE DE PREÇO - HYPERLIQUID API")
print("=" * 60)

# Test 1: allMids
resp = requests.post(
    'https://api.hyperliquid.xyz/info',
    json={'type': 'allMids'},
    headers={'Content-Type': 'application/json'},
    timeout=15
)

print(f"\n1. Status HTTP: {resp.status_code}")

if resp.status_code == 200:
    try:
        data = resp.json()
        if isinstance(data, dict) and 'BTC' in data:
            btc_price = float(data['BTC'])
            print(f"2. Preço BTC raw: {data['BTC']} (tipo: {type(data['BTC']).__name__})")
            print(f"3. Preço BTC convertido: ${btc_price:,.2f}")
            
            # Verificar se é um valor realista
            if 50000 < btc_price < 200000:
                print("✅ Preço parece REALISTA")
            else:
                print("⚠️ Preço parece FORA DO NORMAL")
        else:
            print(f"⚠️ Resposta inesperada: {type(data)}")
            print(f"   Primeiras keys: {list(data.keys())[:5] if isinstance(data, dict) else 'N/A'}")
    except Exception as e:
        print(f"❌ Erro a processar JSON: {e}")
        print(f"   Texto raw: {resp.text[:200]}")
else:
    print(f"❌ API retornou erro: {resp.status_code}")
    print(f"   Texto: {resp.text[:200]}")

print("=" * 60)
