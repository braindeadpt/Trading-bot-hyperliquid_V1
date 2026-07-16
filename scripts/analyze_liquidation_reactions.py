"""CLI: Phase-2 liquidation reaction analysis (research-only).

Default is dry-run (no S3). Pass ``--execute`` to scan local fills archives
and/or correlate Phase-1 snapshots with subsequent candles.

Examples:
  python scripts/analyze_liquidation_reactions.py --dry-run
  python scripts/analyze_liquidation_reactions.py --execute \\
      --from-fills data/research/fills/schema_peek_20260715_14.lz4
  python scripts/analyze_liquidation_reactions.py --execute --forward-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.liquidation_reaction_analysis import (
    estimate_sample_need,
    extract_liquidation_events,
    run_forward_track_analysis,
    run_retrospective_analysis,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_COINS = ("BTC", "ETH", "SOL", "HYPE")


def _parse_coins(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_COINS)
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-2 liquidation reaction analysis")
    parser.add_argument("--from-fills", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--execute", action="store_true", default=False)
    parser.add_argument("--forward-only", action="store_true", default=False)
    parser.add_argument("--retrospective-only", action="store_true", default=False)
    parser.add_argument("--coins", default=",".join(DEFAULT_COINS))
    parser.add_argument("--flush-minutes", type=int, default=5)
    parser.add_argument("--reverse-minutes", type=int, default=30)
    parser.add_argument("--flush-threshold-pct", type=float, default=0.05)
    parser.add_argument("--reverse-threshold-pct", type=float, default=0.05)
    parser.add_argument("--approach-pct", type=float, default=0.25)
    parser.add_argument("--forward-minutes", type=int, default=60)
    parser.add_argument(
        "--research-db",
        default=str(ROOT / "data" / "research" / "hyperliquid.db"),
    )
    parser.add_argument(
        "--candle-db",
        default=str(ROOT / "data" / "research" / "hyperliquid.db"),
        help="Candle source DB (research preferred; live bot.db allowed read-only)",
    )
    args = parser.parse_args()
    dry_run = not bool(args.execute)
    coins = _parse_coins(args.coins)

    plan = {
        "mode": "dry-run" if dry_run else "execute",
        "coins": coins,
        "from_fills": args.from_fills,
        "run_retrospective": not args.forward_only,
        "run_forward": not args.retrospective_only,
        "sample_need_estimate": estimate_sample_need(),
    }
    print(json.dumps(plan, indent=2))

    if dry_run:
        if args.from_fills:
            # Offline count only — no candle join
            n = 0
            for p in args.from_fills:
                path = Path(p)
                if path.exists():
                    n += len(extract_liquidation_events(path, coins=coins))
            print(json.dumps({"dry_run_liquidation_events": n}, indent=2))
        logger.info(
            "DRY-RUN: no candle analysis. Pass --execute with --from-fills "
            "and/or omit --retrospective-only for forward track."
        )
        logger.info(
            "Snapshot cron (manual — do not auto-install): "
            "python scripts/build_liquidation_map.py --from-fills <paths...> --execute "
            "--max-distance-pct 50"
        )
        return 0

    out: dict = {}
    research_db = Path(args.research_db)
    candle_db = Path(args.candle_db)

    if not args.forward_only:
        if not args.from_fills:
            logger.error("--execute retrospective requires --from-fills PATH...")
            return 2
        paths = [Path(p) for p in args.from_fills]
        report = run_retrospective_analysis(
            paths,
            research_db,
            coins=coins,
            flush_minutes=args.flush_minutes,
            reverse_minutes=args.reverse_minutes,
            flush_threshold_pct=args.flush_threshold_pct,
            reverse_threshold_pct=args.reverse_threshold_pct,
        )
        out["retrospective"] = report.to_dict()
        logger.info(
            "Approach A: events=%d with_candles=%d flushed=%d reversed=%d",
            report.n_events,
            report.n_with_candles,
            report.n_flushed,
            report.n_reversed,
        )

    if not args.retrospective_only:
        fwd = run_forward_track_analysis(
            research_db,
            candle_db,
            coins=coins,
            approach_pct=args.approach_pct,
            forward_minutes=args.forward_minutes,
        )
        out["forward"] = fwd.to_dict()
        logger.info(
            "Approach B: snapshots=%d approaches=%d reactions=%s",
            fwd.n_snapshots,
            fwd.n_approaches,
            fwd.reactions,
        )

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
