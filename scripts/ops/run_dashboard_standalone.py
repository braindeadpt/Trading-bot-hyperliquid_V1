#!/usr/bin/env python3
"""Run the monitoring dashboard standalone — no trading engine attached.

Useful for read-only preview/scratch instances that serve real data:
all engine-bound panels degrade to empty fragments while the DB-driven
panels (topbar, paper strategies, feed, ledgers) render normally.

Exposing the dashboard beyond loopback requires authentication: the
script refuses to start on a non-loopback --host (or with --prefix,
which implies a reverse proxy) unless DASHBOARD_AUTH_ENABLED=true and a
token (BOT_DASHBOARD_TOKEN or DASHBOARD_TOKEN) are set in the
environment. On plain loopback, token presence still enables auth.

Example (loopback only):
    python scripts/ops/run_dashboard_standalone.py --port 5000

Example (exposed — auth mandatory):
    DASHBOARD_AUTH_ENABLED=true BOT_DASHBOARD_TOKEN=<token> \
    python scripts/ops/run_dashboard_standalone.py \
        --host 0.0.0.0 --port 5000 --prefix /dash
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.dashboard.web import create_app

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_AUTH_TRUE_VALUES = frozenset({"1", "true", "yes"})


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


def _auth_requested() -> bool:
    return os.environ.get("DASHBOARD_AUTH_ENABLED", "").strip().lower() in _AUTH_TRUE_VALUES


def _dashboard_token_configured() -> bool:
    return bool(
        os.environ.get("BOT_DASHBOARD_TOKEN", "").strip()
        or os.environ.get("DASHBOARD_TOKEN", "").strip()
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Standalone dashboard (no engine)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--prefix", default="", help="mount prefix to strip, e.g. /dash")
    p.add_argument(
        "--secret-key",
        default=None,
        help="Flask SECRET_KEY (default: random per start, via create_app)",
    )
    args = p.parse_args()

    # Fail closed: a non-loopback bind or a mount prefix (reverse proxy)
    # exposes every read endpoint — positions, PnL, logs — to whatever
    # reaches the port, so an explicit token gate is mandatory there.
    exposed = args.host not in _LOOPBACK_HOSTS or bool(args.prefix)
    if exposed and not (_auth_requested() and _dashboard_token_configured()):
        sys.stderr.write(
            "Refusing to start: serving the dashboard beyond loopback "
            "(--host != loopback or --prefix behind a proxy) without "
            "authentication exposes positions, PnL and bot.log to anyone "
            "who reaches the port.\n"
            "Set DASHBOARD_AUTH_ENABLED=true and BOT_DASHBOARD_TOKEN (or "
            "DASHBOARD_TOKEN) in the environment, or bind to 127.0.0.1 "
            "without --prefix.\n"
        )
        raise SystemExit(2)

    app, socketio, _emit = create_app(
        config={
            "host": args.host,
            "port": args.port,
            # None → resolve_dashboard_auth: DASHBOARD_AUTH_ENABLED env wins,
            # else token presence decides (never forced off here).
            "auth_enabled": None,
            "secret_key": args.secret_key,
        }
    )
    if args.prefix:
        app.wsgi_app = _PrefixStrip(app.wsgi_app, args.prefix)
    socketio.run(app, host=args.host, port=args.port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
