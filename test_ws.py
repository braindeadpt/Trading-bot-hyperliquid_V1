import asyncio
import json
import websockets

async def test():
    url = "wss://api.hyperliquid.xyz/ws"
    print(f"Connecting to {url}...")
    async with websockets.connect(url) as ws:
        print("Connected!")
        # Subscribe to allMids
        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        print("Subscribed to allMids")
        # Wait for messages
        for i in range(10):
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                print(f"Msg #{i+1}: {msg[:200]}")
            except asyncio.TimeoutError:
                print(f"Timeout waiting for msg #{i+1}")
                break

asyncio.run(test())
