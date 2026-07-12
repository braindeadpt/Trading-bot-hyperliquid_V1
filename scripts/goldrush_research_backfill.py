"""CLI: GoldRush HyperCore research candle backfill (180d, parity audit)."""



from __future__ import annotations



import argparse

import json

import logging

import sys

import time

from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))



from src.data.goldrush_backfill import backfill_goldrush_research

from src.data.research_database import ResearchDatabase

from src.utils.config import load_config

from src.utils.helpers import safe_write_file, validate_safe_path



logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

logger = logging.getLogger(__name__)





def _write_coverage_report(result: dict, out_dir: Path) -> Path:

    project_root = ROOT

    rel = out_dir.relative_to(project_root) if out_dir.is_absolute() else out_dir

    safe = validate_safe_path(rel.as_posix())

    if safe is None:

        raise RuntimeError(f"Unsafe report directory: {out_dir}")

    safe.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")

    path = safe / f"goldrush_coverage_report_{ts}.json"

    coverage = result.get("coverage", {})

    payload = {

        "generated_at_ms": int(time.time() * 1000),

        "provider": result.get("provider"),

        "window_start_ms": result.get("window_start_ms"),

        "window_end_ms": result.get("window_end_ms"),

        "candles_inserted": result.get("candles_inserted"),

        "candles_skipped_protected": result.get("candles_skipped_protected"),

        "pages_fetched": result.get("pages_fetched"),

        "min_coverage_pct": result.get("min_coverage_pct"),

        "provider_limits": {

            "endpoint": "https://hypercore.goldrushdata.com/info",

            "auth": "GOLDRUSH_API_KEY (env only, never logged)",

            "page_cap": 5000,

            "rate_limit": "no public HL IP cap; client-side token bucket",

            "pagination": "forward chunked windows (<=5000 bars/request); advance startTime after last close T",

        },

        "summary": coverage,

        "by_symbol_feed": _group_by_symbol_feed(coverage),

        "parity_reports": result.get("parity_reports", []),

    }

    latest = safe / "goldrush_coverage_report_latest.json"

    body = json.dumps(payload, indent=2)

    if not safe_write_file(path, body):

        raise RuntimeError(f"Failed to write {path}")

    if not safe_write_file(latest, body):

        raise RuntimeError(f"Failed to write {latest}")

    return path





def _group_by_symbol_feed(coverage: dict) -> dict:

    grouped: dict = {}

    for report in coverage.get("reports", []):

        sym = report.get("symbol", "?")

        feed = report.get("feed", "?")

        grouped.setdefault(sym, {})[feed] = {

            "coverage_pct": report.get("coverage_pct"),

            "bar_count": report.get("bar_count"),

            "expected_bars": report.get("expected_bars"),

            "passed": report.get("passed"),

            "failures": report.get("failures"),

            "venue": report.get("venue"),

            "source": report.get("source"),

            "volume_unit": report.get("volume_unit"),

        }

    return grouped





def _coverage_threshold_met(coverage: dict, min_cov: float) -> bool:
    """Pass when every series meets coverage % (gaps may still be flagged separately)."""
    reports = coverage.get("reports") or []
    if not reports:
        return False
    return all(float(r.get("coverage_pct", 0)) >= min_cov for r in reports)


def main() -> int:

    parser = argparse.ArgumentParser(description="GoldRush HyperCore research backfill")

    parser.add_argument("--config", default="config/settings.yaml")

    parser.add_argument("--days", type=int, default=None)

    parser.add_argument("--symbols", default=None, help="Comma-separated, e.g. BTC,ETH,SOL,HYPE")

    parser.add_argument("--no-parity", action="store_true")

    parser.add_argument(

        "--report-dir",

        default="data/research",

        help="Directory for goldrush coverage JSON output",

    )

    args = parser.parse_args()



    cfg = load_config(args.config)

    research = cfg.get("research", {}) or {}

    goldrush_cfg = research.get("goldrush", {}) or {}

    symbols = (

        [s.strip().upper() for s in args.symbols.split(",")]

        if args.symbols

        else list(research.get("backfill_symbols", ["BTC", "ETH", "SOL", "HYPE"]))

    )

    days = args.days if args.days is not None else int(

        goldrush_cfg.get("backfill_days", research.get("backfill_days", 180)),

    )

    min_cov = float(

        goldrush_cfg.get("min_coverage_pct", research.get("goldrush_min_coverage_pct", 99.0)),

    ) / 100.0



    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))

    logger.info("Research DB: %s", db.db_path)

    result = backfill_goldrush_research(

        db,

        symbols=symbols,

        days=days,

        timeframes=tuple(research.get("backfill_timeframes", ["1m", "5m", "15m", "1h"])),

        min_coverage_pct=min_cov,

        run_parity=not args.no_parity,

    )

    report_path = _write_coverage_report(result, ROOT / args.report_dir)

    print(json.dumps(result, indent=2))

    print(f"\nCoverage report: {report_path}")

    coverage = result.get("coverage", {})
    if int(result.get("candles_inserted", 0)) <= 0:
        logger.error("No GoldRush candles inserted — check API credits and GOLDRUSH_API_KEY")
        return 2
    if not _coverage_threshold_met(coverage, min_cov):

        logger.warning("Coverage below %.1f%% threshold — see %s", min_cov * 100, report_path)

        return 2

    return 0





if __name__ == "__main__":

    raise SystemExit(main())

