import asyncio
import json

import pytest
import websockets


async def _run() -> None:
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


@pytest.mark.network
def test_hyperliquid_ws_all_mids() -> None:
    """Manual/network smoke — connects to the real Hyperliquid WS feed."""
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
