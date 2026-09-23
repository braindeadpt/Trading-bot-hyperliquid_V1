"""CLI: evaluate shadow decisions into a hypothetical outcome scoreboard.

Research / observability only. Zero network calls — pure local DB work.

Default is ``--dry-run`` (print scoreboard, do not persist). Pass ``--persist``
to write a snapshot into research DB table ``shadow_outcome_scoreboards``.

Examples:
  python scripts/research/evaluate_shadow_outcomes.py
  python scripts/research/evaluate_shadow_outcomes.py --strategy OrderBookScalper --since-days 7
  python scripts/research/evaluate_shadow_outcomes.py --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase
from src.research.shadow_outcome_evaluator import (
    IDEALIZED_FILL_DISCLAIMER,
    LIVE_DB_DEFAULT,
    run_evaluation,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# PID lockfile (same pattern as scripts/research/overnight_nightly.py) — the
# hourly scheduled task must never overlap: two concurrent evaluations would
# duplicate exactly the memory pressure this decoupling removes.
LOCK_PATH = ROOT / "data" / "research" / ".shadow_eval.lock"


def _lock_acquired() -> bool:
    """Best-effort PID lockfile — a missed stale lock only risks overlap."""
    try:
        if LOCK_PATH.exists():
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
            if pid and pid != os.getpid():
                os.kill(pid, 0)  # raises if the pid is gone
                return False
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def _lock_release() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(
            encoding="utf-8"
        ).strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shadow outcome evaluator (research-only, no network)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print scoreboard without persisting (default)",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        default=False,
        help="Write scoreboard snapshot to research DB",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="Filter to one strategy name (e.g. OrderBookScalper)",
    )
    parser.add_argument(
        "--variant",
        default=None,
        help=(
            "Filter by variant: phase08_shadow | router_blocked "
            "(default: all, labeled separately)"
        ),
    )
    parser.add_argument(
        "--since-days",
        type=float,
        default=None,
        help="Only decisions newer than N days",
    )
    parser.add_argument(
        "--research-db",
        default=None,
        help="Research DB path (default: research.database.path from config)",
    )
    parser.add_argument(
        "--live-db",
        default=str(LIVE_DB_DEFAULT),
        help="Live bot.db for read-only candle fallback (never written)",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        default=False,
        help="Print JSON summary only (no human table)",
    )
    args = parser.parse_args()
    persist = bool(args.persist)
    dry_run = not persist

    # Overlap guard only for --persist (the scheduled path). Dry-runs are
    # ad-hoc research invocations and may run alongside the hourly job.
    if persist and not _lock_acquired():
        logger.info("Another evaluation is still running — exiting without work")
        return 0

    plan = {
        "mode": "persist" if persist else "dry-run",
        "strategy": args.strategy,
        "variant": args.variant,
        "since_days": args.since_days,
        "research_db": args.research_db,
        "live_db": args.live_db,
        "disclaimer": IDEALIZED_FILL_DISCLAIMER,
        "network": False,
    }
    print(json.dumps(plan, indent=2))

    try:
        summary = run_evaluation(
            strategy=args.strategy,
            variant=args.variant,
            since_days=args.since_days,
            research_db_path=Path(args.research_db) if args.research_db else None,
            live_db_path=Path(args.live_db) if args.live_db else None,
            persist=persist,
        )
    finally:
        if persist:
            _lock_release()

    if not args.json_only:
        print()
        print(summary.get("table", ""))
        print()
    # Drop the large table string from JSON dump
    json_out = {k: v for k, v in summary.items() if k != "table"}
    print(json.dumps(json_out, indent=2, sort_keys=True))
    if dry_run:
        logger.info("Dry-run complete — nothing persisted")
    else:
        logger.info("Scoreboard snapshot persisted to research DB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
