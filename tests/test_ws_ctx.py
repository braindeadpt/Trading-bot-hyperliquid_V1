import asyncio
import json

import pytest
import websockets


async def _run() -> None:
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


@pytest.mark.network
def test_hyperliquid_ws_active_asset_ctxs() -> None:
    """Manual/network smoke — connects to the real Hyperliquid WS feed."""
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
