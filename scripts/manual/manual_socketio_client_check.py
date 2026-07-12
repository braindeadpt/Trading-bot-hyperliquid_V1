import socketio

sio = socketio.Client()

@sio.event
def connect():
    print('Connected to server')

@sio.event
def disconnect():
    print('Disconnected from server')

@sio.on('status_update')
def on_status(data):
    print(f'Status: {data}')

@sio.on('price_update')
def on_price(data):
    print(f'Price: {data}')

try:
    sio.connect('http://127.0.0.1:5000')
    sio.wait()
except Exception as e:
    print(f'Error: {e}')
