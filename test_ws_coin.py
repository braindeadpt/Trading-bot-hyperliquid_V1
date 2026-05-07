import asyncio
import json
import websockets

async def test():
    url = "wss://api.hyperliquid.xyz/ws"
    async with websockets.connect(url) as ws:
        # Test subscribing to specific coin
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}}))
        msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
        print(f"Sub response: {msg[:200]}")
        try:
            msg2 = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"Data: {msg2[:500]}")
        except asyncio.TimeoutError:
            print("No data received")

asyncio.run(test())
