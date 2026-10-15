#!/usr/bin/env python3
"""Launcher for the boros-shadow daemon (docs/PREREGISTER_BOROS_SPREAD.md).

Standalone process: hourly Boros markets + perp-L2 snapshots at :05 UTC
and hypothetical locked-spread entries in data/research/boros_shadow.db.
Places zero orders; touches zero engine state. Intended to run under a
process supervisor (pm2) — the supervisor owns restart policy.

Usage:
    python -m scripts.research.run_boros_shadow [--db PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.boros_shadow.daemon import BorosShadowDaemon  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="override ledger path (default: spec.DB_PATH)")
    args = ap.parse_args()
    from src.boros_shadow import spec
    db = args.db or spec.DB_PATH
    import asyncio
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    d = BorosShadowDaemon(db_path=db)
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
