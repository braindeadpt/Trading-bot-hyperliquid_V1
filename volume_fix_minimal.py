"""
volume_fix_minimal.py - Versão Simplificada e Direta
====================================================

Como correr:
    pip install ccxt websockets
    python volume_fix_minimal.py

Config: edita as variáveis em CONFIGURAÇÕES abaixo
"""

import asyncio
import json
import time
from collections import deque
from datetime import datetime

# ========================= CONFIGURAÇÕES =========================
SYMBOL = "BTC"              # BTC, ETH, SOL, etc.
TIMEFRAME = "1m"            # 1m, 5m, 15m
MA_PERIOD = 20              # Média móvel (20 candles)
SPIKE_THRESHOLD = 2.5       # 2.5x acima da média = spike
DELTA_THRESHOLD = 0.60      # 60% delta (buy ou sell agressivo)

EXCHANGES = [
    "hyperliquid",
    "binance", 
    "bybit",
    "okx"
]

# Filas para histórico
volume_history = deque(maxlen=MA_PERIOD)
delta_history = deque(maxlen=MA_PERIOD)

print(f"🚀 Volume Fix Minimal | {SYMBOL} | {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"📊 Config: MA{MA_PERIOD} | Spike {SPIKE_THRESHOLD}x | Delta {DELTA_THRESHOLD}")
print("=" * 60)

# ====================== VOLUME VIA CCXT ======================
async def get_volume_ccxt():
    """Busca volume dos últimos candles via CCXT."""
    try:
        import ccxt
        
        total_vol = 0.0
        for ex_name in EXCHANGES:
            try:
                exchange = getattr(ccxt, ex_name)({'options': {'defaultType': 'swap'}})
                ohlcv = exchange.fetch_ohlcv(f"{SYMBOL}/USDT", TIMEFRAME, limit=MA_PERIOD + 1)
                
                if ohlcv and len(ohlcv) > 0:
                    # Soma volumes dos candles
                    vol_sum = sum(c[5] for c in ohlcv[:-1])  # Exceto último (incompleto)
                    total_vol += vol_sum / len(ohlcv[:-1])  # Média por exchange
                    
            except Exception as e:
                continue  # Ignora erros de exchange
        
        return total_vol
        
    except ImportError:
        print("❌ CCXT não instalado: pip install ccxt")
        return None

# ====================== WEBSOCKET HYPERLIQUID ======================
async def hyperliquid_ws():
    """WebSocket Hyperliquid para Volume Delta em tempo real."""
    import websockets
    
    uri = "wss://api.hyperliquid.xyz/ws"
    
    buy_vol = 0.0
    sell_vol = 0.0
    window_start = time.time()
    
    reconnect_delay = 5
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                # Subscreve a trades
                sub = {
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": SYMBOL}
                }
                await ws.send(json.dumps(sub))
                print(f"📡 WS Hyperliquid: subscrito a {SYMBOL}")
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    if data.get("channel") == "trades":
                        trades = data.get("data", [])
                        
                        for trade in trades:
                            px = float(trade["px"])
                            sz = float(trade["sz"])
                            notional = px * sz
                            side = trade["side"]  # "B" = Taker Buy, "A" = Taker Sell
                            
                            if side == "B":
                                buy_vol += notional
                            else:
                                sell_vol += notional
                    
                    # A cada 60 segundos, processa janela
                    if time.time() - window_start >= 60:
                        total = buy_vol + sell_vol
                        
                        if total > 0:
                            delta = (buy_vol - sell_vol) / total  # -1.0 a +1.0
                            
                            # Atualiza histórico
                            volume_history.append(total)
                            delta_history.append(delta)
                            
                            # Cálculos
                            if len(volume_history) >= 5:  # Mínimo 5 amostras
                                vol_ma = sum(volume_history) / len(volume_history)
                                vol_ratio = total / vol_ma if vol_ma > 0 else 1.0
                                avg_delta = sum(delta_history) / len(delta_history)
                                
                                # Direção
                                if delta > DELTA_THRESHOLD:
                                    direction = "🟢 BUY AGRESSIVO"
                                elif delta < -DELTA_THRESHOLD:
                                    direction = "🔴 SELL AGRESSIVO"
                                else:
                                    direction = "⚪ NEUTRO"
                                
                                # Log
                                timestamp = datetime.now().strftime('%H:%M:%S')
                                print(f"\n[{timestamp}] Vol: ${total:,.0f} | MA: ${vol_ma:,.0f} | "
                                      f"Ratio: {vol_ratio:.2f}x | Delta: {delta:+.1%} | {direction}")
                                
                                # SPIKE DETECTION
                                if vol_ratio >= SPIKE_THRESHOLD and abs(delta) >= DELTA_THRESHOLD:
                                    spike_dir = "📈 PUMP" if delta > 0 else "📉 DUMP"
                                    print(f"\n{'='*50}")
                                    print(f"⚡ SPIKE DETETADO! {spike_dir}")
                                    print(f"   Volume: {vol_ratio:.1f}x acima da média")
                                    print(f"   Delta: {delta:+.1%} {'compra' if delta > 0 else 'venda'} agressiva")
                                    print(f"{'='*50}\n")
                                    
                                    # AQUI PODES DISPARAR ORDEM NO HYPERLIQUID
                                    # await execute_trade(direction="LONG" if delta > 0 else "SHORT")
                            
                            # Reset janela
                            buy_vol = sell_vol = 0.0
                            window_start = time.time()
                            
        except asyncio.TimeoutError:
            print(f"⚠️ Timeout WS, reconectando em {reconnect_delay}s...")
            await asyncio.sleep(reconnect_delay)
        except Exception as e:
            print(f"❌ Erro WS: {e}")
            await asyncio.sleep(reconnect_delay)

# ====================== FUNÇÃO PARA ORDENS (placeholder) ======================
async def execute_trade(direction, size=0.01):
    """
    Placeholder para execução de ordem.
    Integra aqui a tua lógica de trading.
    """
    print(f"🎯 ORDEM: {direction} {size} BTC")
    # TODO: Integrar com Hyperliquid SDK
    pass

# ====================== LOOP PRINCIPAL ======================
async def main():
    """Inicia WebSocket e polling de backup."""
    
    # Verifica dependências
    try:
        import ccxt
        import websockets
    except ImportError:
        print("❌ Instala dependências primeiro:")
        print("   pip install ccxt websockets")
        return
    
    # Inicia WebSocket em background
    ws_task = asyncio.create_task(hyperliquid_ws())
    
    # Polling CCXT (backup)
    while True:
        try:
            vol = await get_volume_ccxt()
            if vol:
                print(f"📊 Volume médio CCXT: ${vol:,.0f}")
        except:
            pass
        await asyncio.sleep(300)  # A cada 5 min

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot parado pelo utilizador")
