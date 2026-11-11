"""Read-only CLI status report for MultiLegCoordinator episodes (multileg.db)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.multileg.ledger import MultiLegLedger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/live/multileg.db",
        help="Path to multileg SQLite ledger (default: data/live/multileg.db)",
    )
    parser.add_argument(
        "--status",
        default=None,
        help="Optional status filter (e.g. open, pending_entry, aborted_unwound)",
    )
    args = parser.parse_args()

    ledger = MultiLegLedger(args.db)
    try:
        episodes = ledger.list_episodes(status=args.status)
        summary = {
            "db": args.db,
            "count": len(episodes),
            "by_status": {},
            "episodes": episodes[:25],
        }
        for ep in episodes:
            st = str(ep.get("status", "unknown"))
            summary["by_status"][st] = summary["by_status"].get(st, 0) + 1
        print(json.dumps(summary, indent=2, default=str))
        return 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
