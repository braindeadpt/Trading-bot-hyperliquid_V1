"""Tiny TCP forwarder: LISTEN:LISTEN_PORT -> 127.0.0.1:TARGET_PORT.

The dashboard binds 127.0.0.1 inside the container (frozen settings.yaml);
docker's port mapping reaches the container's *external* IP, so a loopback
bind is invisible outside. This relay listens on all interfaces and proxies
to the loopback dashboard — packaging shim only, no app code touched.
"""
import socket
import sys
import threading

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
TARGET_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000


def relay(src: socket.socket) -> None:
    try:
        dst = socket.create_connection(("127.0.0.1", TARGET_PORT), timeout=5)
    except OSError:
        src.close()
        return

    def pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            a.close()
            b.close()

    threading.Thread(target=pump, args=(src, dst), daemon=True).start()
    threading.Thread(target=pump, args=(dst, src), daemon=True).start()


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(64)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=relay, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
