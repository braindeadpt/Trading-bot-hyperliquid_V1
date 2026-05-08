import asyncio
import json
import websockets

async def test():
    url = "wss://api.hyperliquid.xyz/ws"
    print(f"Connecting to {url}...")
    async with websockets.connect(url) as ws:
        print("Connected!")
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "activeAssetCtxs"}}))
        print("Subscribed to activeAssetCtxs")
        for i in range(3):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"Msg #{i+1}: {msg[:500]}")
            except asyncio.TimeoutError:
                print(f"Timeout")
                break

asyncio.run(test())
