#!/usr/bin/env python3
"""Launcher for the unlock-shadow daemon (docs/PREREGISTER_UNLOCK_SHADOW.md).

Standalone process: daily point-in-time unlock-calendar snapshots
(DefiLlama + CryptoRank/Tokenomist), deterministic eligibility, and
hypothetical beta-hedged short windows in data/research/unlock_shadow.db.
Places zero orders; touches zero engine state. Intended to run under a
process supervisor (pm2) — the supervisor owns restart policy.

Usage:
    python -m scripts.research.run_unlock_shadow [--db PATH]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research.unlock_shadow.daemon import UnlockShadowDaemon  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None,
                    help="override ledger path (default: spec.DB_PATH)")
    args = ap.parse_args()
    from src.research.unlock_shadow import spec
    db = args.db or spec.DB_PATH
    import asyncio
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    d = UnlockShadowDaemon(db_path=db)
    try:
        asyncio.run(d.run())
    finally:
        d.shutdown()


if __name__ == "__main__":
    main()
