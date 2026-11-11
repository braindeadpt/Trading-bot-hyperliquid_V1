"""HTTP-aware TCP forwarder: LISTEN:LISTEN_PORT -> 127.0.0.1:TARGET_PORT.

The dashboard binds 127.0.0.1 inside the container (frozen settings.yaml);
docker's port mapping reaches the container's *external* IP, so a loopback
bind is invisible outside.

Security hardening (2026-11-11 audit):
  - Raw TCP forwarding made every Docker-bridge or port-forwarded connection
    appear as direct 127.0.0.1 without ``X-Forwarded-*``, bypassing
    ``_is_direct_local_request()`` token auth in ``web.py``.
  - For any non-loopback peer (e.g. Docker bridge ``172.x.x.x``), this proxy
    strips client-spoofed ``X-Forwarded-For`` headers on the initial HTTP
    request head and injects the verified socket peer IP so Flask enforces
    ``BOT_DASHBOARD_TOKEN`` and per-IP rate limiting accurately.
"""
from __future__ import annotations

import socket
import sys
import threading

DEFAULT_LISTEN_PORT = 8091
DEFAULT_TARGET_PORT = 5000

_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def rewrite_http_head(raw: bytes, peer_ip: str) -> bytes:
    """Inject verified ``X-Forwarded-For`` for non-loopback peers."""
    if not raw or peer_ip in _LOOPBACK_PEERS:
        return raw
    sep = b"\r\n\r\n"
    idx = raw.find(sep)
    if idx < 0:
        return raw
    head_bytes = raw[:idx]
    rest = raw[idx + len(sep):]
    try:
        lines = head_bytes.decode("latin-1").split("\r\n")
    except Exception:
        return raw
    if not lines or "HTTP/" not in lines[0]:
        return raw
    # Preserve upstream Tailscale header when arriving from host gateway only
    # if explicitly trusted; otherwise strip spoofable X-Forwarded-For and
    # stamp actual TCP peer IP.
    filtered = [
        ln for ln in lines[1:]
        if not ln.lower().startswith("x-forwarded-for:")
    ]
    filtered.append(f"X-Forwarded-For: {peer_ip}")
    rebuilt = "\r\n".join([lines[0], *filtered]).encode("latin-1") + sep + rest
    return rebuilt


def relay(src: socket.socket, peer_ip: str, target_port: int = DEFAULT_TARGET_PORT) -> None:
    try:
        dst = socket.create_connection(("127.0.0.1", target_port), timeout=5)
    except OSError:
        src.close()
        return

    def pump_client_to_backend() -> None:
        first_chunk = True
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if first_chunk:
                    first_chunk = False
                    data = rewrite_http_head(data, peer_ip)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            src.close()
            dst.close()

    def pump_backend_to_client() -> None:
        try:
            while True:
                data = dst.recv(65536)
                if not data:
                    break
                src.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            src.close()
            dst.close()

    threading.Thread(target=pump_client_to_backend, daemon=True).start()
    threading.Thread(target=pump_backend_to_client, daemon=True).start()


def main() -> None:
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LISTEN_PORT
    target_port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_TARGET_PORT
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    while True:
        conn, addr = srv.accept()
        peer_ip = str(addr[0]) if isinstance(addr, tuple) and addr else ""
        threading.Thread(
            target=relay, args=(conn, peer_ip, target_port), daemon=True
        ).start()


if __name__ == "__main__":
    main()
