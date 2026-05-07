import asyncio
import websockets

async def test_socketio():
    # Try to connect via websocket to the Socket.IO server
    url = "ws://127.0.0.1:5000/socket.io/?EIO=4&transport=websocket"
    try:
        async with websockets.connect(url) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"Received: {msg}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test_socketio())
