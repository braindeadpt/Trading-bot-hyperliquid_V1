#!/usr/bin/env python3
"""Launcher for the latency-probe daemon (prereg 2026-10-28).

Passive collector: trade.xyz vs Binance latency during US-close windows
(America/New_York). Writes data/research/latency_probe.db. Zero orders,
zero paper trades — ever.

Usage:
    python -m scripts.research.run_latency_probe [--db PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="override ledger path (default: spec.DB_PATH)")
    args = ap.parse_args()

    from src.latency_probe.daemon import LatencyProbeDaemon
    from src.latency_probe import spec
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    d = LatencyProbeDaemon(db_path=args.db or spec.DB_PATH)
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
