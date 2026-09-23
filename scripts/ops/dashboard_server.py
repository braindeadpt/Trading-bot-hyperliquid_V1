#!/usr/bin/env python3
"""Standalone read-only dashboard server — Flask app without a trading engine.

On the VPS the dashboard normally runs embedded inside ``main.py``. This
runner serves the same app with ``_engine`` unset, so every engine-dependent
push emitter no-ops while all DB-backed REST endpoints keep working
(``/api/shadow_panel``, feed-silence sparklines, watchdog panels, IV joins)
by opening the research DB themselves via ``ResearchDatabase.open()``.

Use it to inspect scoreboards and feed health while the bot service is
stopped (migration windows, maintenance) — or permanently alongside the
bot, in which case run the bot with ``--no-dashboard`` so only one server
binds the port.

Binds ``dashboard.host`` from config (``127.0.0.1`` by default — remote
access goes through an SSH tunnel or Tailscale, never a public bind).

Usage:
    python scripts/ops/dashboard_server.py
    python scripts/ops/dashboard_server.py --config config/settings.yaml --port 5000
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
# main.py's bare-import convention: dashboard.web does "from src...." AND
# some panels import "utils.xxx" — both roots must be importable.
sys.path.insert(0, str(PROJECT_ROOT / "src"))

logger = logging.getLogger("dashboard_server")


def build_dashboard_config(cfg: object) -> dict:
    """Same key set main.py passes to create_app (single source of truth)."""
    get = cfg.get  # type: ignore[attr-defined]
    return {
        "mode": str(get("mode", "paper")).upper(),
        "version": get("version", "1.0.0"),
        "host": get("dashboard.host", "127.0.0.1"),
        "port": get("dashboard.port", 5000),
        "secret_key": get("dashboard.secret_key"),
        "password": get("dashboard.password"),
        "token": get("dashboard.token"),
        "auth_enabled": get("dashboard.auth_enabled"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config" / "settings.yaml"))
    ap.add_argument("--host", default=None, help="override dashboard.host (default: config)")
    ap.add_argument("--port", type=int, default=None, help="override dashboard.port")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from src.utils.config import load_config

    cfg = load_config(args.config)
    dashboard_cfg = build_dashboard_config(cfg)
    if args.host:
        dashboard_cfg["host"] = args.host
    if args.port:
        dashboard_cfg["port"] = args.port

    host = str(dashboard_cfg["host"])
    if host not in ("127.0.0.1", "::1", "localhost"):
        # The dashboard exposes account/position data; a non-loopback bind
        # is only ever a mistake on the VPS (SSH tunnel / Tailscale are the
        # sanctioned remote paths). Fail closed instead of serving.
        logger.error("Refusing non-loopback bind %s — use SSH tunnel or Tailscale", host)
        return 2

    from src.dashboard.web import create_app

    app, socketio, _emit_fn = create_app(config=dashboard_cfg)
    logger.info(
        "Dashboard (standalone, no engine) at http://%s:%s",
        host,
        dashboard_cfg["port"],
    )
    socketio.run(
        app,
        host=host,
        port=int(dashboard_cfg["port"]),
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,  # same flag as the embedded thread in main.py
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
