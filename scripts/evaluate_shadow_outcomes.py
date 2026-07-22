"""CLI: evaluate shadow decisions into a hypothetical outcome scoreboard.

Research / observability only. Zero network calls — pure local DB work.

Default is ``--dry-run`` (print scoreboard, do not persist). Pass ``--persist``
to write a snapshot into research DB table ``shadow_outcome_scoreboards``.

Examples:
  python scripts/evaluate_shadow_outcomes.py
  python scripts/evaluate_shadow_outcomes.py --strategy OrderBookScalper --since-days 7
  python scripts/evaluate_shadow_outcomes.py --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.research_database import DEFAULT_RESEARCH_DB_PATH
from src.research.shadow_outcome_evaluator import (
    IDEALIZED_FILL_DISCLAIMER,
    LIVE_DB_DEFAULT,
    run_evaluation,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


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
        default=str(DEFAULT_RESEARCH_DB_PATH),
        help="Research DB path (shadow_decisions + optional scoreboard persist)",
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

    summary = run_evaluation(
        strategy=args.strategy,
        variant=args.variant,
        since_days=args.since_days,
        research_db_path=Path(args.research_db),
        live_db_path=Path(args.live_db) if args.live_db else None,
        persist=persist,
    )

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
