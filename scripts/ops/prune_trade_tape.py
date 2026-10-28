#!/usr/bin/env python3
"""Purge old ``trade_tape`` rows from the research DB — opt-in only.

Post-cutover cleanup (2026-10-28): the tape writer is already off
(``research.ws_microstructure_enabled: false`` + ``rest_sampling_enabled:
false``), but the accumulated ~115 MB/day history stays until the owner
picks a retention. Default here is OFF — with no ``--days`` and no
policy file the script deletes nothing and exits 0.

Retention is resolved in this order:
  1. ``--days N`` CLI argument
  2. ``data/research/trade_tape_retention.json`` — host-managed, never
     committed: ``{"retention_days": 45}``
Absent both -> no purge (the 30-60 day decision is the owner's).

Usage:
    python scripts/ops/prune_trade_tape.py            # no-op unless policy file
    python scripts/ops/prune_trade_tape.py --days 45  # one-shot explicit purge
    python scripts/ops/prune_trade_tape.py --dry-run --days 45
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

POLICY_FILE = ROOT / "data" / "research" / "trade_tape_retention.json"


def _policy_days() -> float:
    if not POLICY_FILE.exists():
        return 0.0
    try:
        d = json.loads(POLICY_FILE.read_text())
        return float(d.get("retention_days") or 0)
    except (ValueError, TypeError, OSError) as exc:
        print(f"prune_trade_tape: bad policy file {POLICY_FILE}: {exc}",
              file=sys.stderr)
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None,
                    help="retention in days; omitted = policy file only")
    ap.add_argument("--dry-run", action="store_true",
                    help="count rows that WOULD be deleted, delete none")
    args = ap.parse_args()

    days = args.days if args.days is not None else _policy_days()
    if days <= 0:
        print("prune_trade_tape: retention disabled (no --days, no "
              f"policy in {POLICY_FILE.name}) — nothing deleted")
        return 0

    from src.data.research_database import ResearchDatabase
    from src.utils.config import load_config
    import sqlite3
    import time

    cfg = load_config(ROOT / "config" / "settings.yaml")
    db_path = ResearchDatabase.resolve_path(cfg)
    db = ResearchDatabase(db_path)
    cutoff = int(time.time() * 1000 - days * 86_400_000)
    n = db._conn().execute(
        "SELECT COUNT(*) FROM trade_tape WHERE timestamp_ms < ?",
        (cutoff,)).fetchone()[0]
    if args.dry_run:
        print(f"prune_trade_tape: DRY RUN — {n} rows older than "
              f"{days}d would be deleted from {db_path}")
        db.close()
        return 0
    deleted = db.prune_trade_tape(days)
    db.close()
    print(f"prune_trade_tape: deleted {deleted} rows older than "
          f"{days}d from {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
