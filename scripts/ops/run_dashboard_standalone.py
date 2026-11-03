#!/usr/bin/env python3
"""Run the monitoring dashboard standalone — no trading engine attached.

Useful for read-only preview/scratch instances that serve real data:
all engine-bound panels degrade to empty fragments while the DB-driven
panels (topbar, paper strategies, feed, ledgers) render normally.

Example:
    python scripts/ops/run_dashboard_standalone.py \
        --host 0.0.0.0 --port 5000 --prefix /dash
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.dashboard.web import create_app


class _PrefixStrip:
    """WSGI middleware: drop a mount prefix before routing.

    Lets the app sit behind a path-based reverse proxy (e.g. tailscale
    serve `/dash` -> :8095) without touching route definitions.
    """

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix:
            environ["PATH_INFO"] = "/"
        elif path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):]
        return self.app(environ, start_response)


def main() -> None:
    p = argparse.ArgumentParser(description="Standalone dashboard (no engine)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--prefix", default="", help="mount prefix to strip, e.g. /dash")
    p.add_argument("--secret-key", default="standalone")
    args = p.parse_args()

    app, socketio, _emit = create_app(
        config={
            "host": args.host,
            "port": args.port,
            "auth_enabled": os.environ.get("DASHBOARD_AUTH_ENABLED", "false").lower() == "true",
            "secret_key": args.secret_key,
        }
    )
    if args.prefix:
        app.wsgi_app = _PrefixStrip(app.wsgi_app, args.prefix)
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
