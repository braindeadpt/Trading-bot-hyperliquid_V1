#!/usr/bin/env python3
"""Launcher for the resmom-shadow daemon (notes/resmom-forward-prereg-2026-10-28.md).

Standalone process: weekly paper rebalance Mon 00:05 UTC (first
2026-11-02), daily NAV marks 00:10 UTC, hourly funding accrual :10 UTC.
Writes data/research/resmom_shadow.db. Places zero orders; touches zero
engine state. Intended to run under a supervisor (compose/pm2).

Usage:
    python -m scripts.research.run_resmom_shadow [--db PATH] [--once]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.resmom_shadow.daemon import ResmomShadowDaemon  # noqa: E402
from src.resmom_shadow import spec  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="override ledger path (default: spec.DB_PATH)")
    ap.add_argument("--once", action="store_true",
                    help="run a single tick then exit")
    args = ap.parse_args()
    db = args.db or spec.DB_PATH
    import asyncio
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    d = ResmomShadowDaemon(db_path=db)
    try:
        if args.once:
            import asyncio as _a
            _a.run(d.boot())
            d.tick()
            d._heartbeat()
        else:
            asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
