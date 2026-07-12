import asyncio
import json

import pytest
import websockets


async def _run() -> None:
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


@pytest.mark.network
def test_hyperliquid_ws_active_asset_ctx_coin() -> None:
    """Manual/network smoke — connects to the real Hyperliquid WS feed."""
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
