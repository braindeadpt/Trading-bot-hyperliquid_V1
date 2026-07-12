"""CLI: Hyperliquid research DB backfill (candleSnapshot + L2/tape samples)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.coverage_audit import summarize_coverage_reports
from src.data.hl_research_backfill import backfill_hl_research
from src.data.research_database import ResearchDatabase
from src.utils.config import load_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def _write_coverage_report(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"coverage_report_{ts}.json"
    coverage = result.get("coverage", {})
    payload = {
        "generated_at_ms": int(time.time() * 1000),
        "window_start_ms": result.get("window_start_ms"),
        "window_end_ms": result.get("window_end_ms"),
        "candles_saved": result.get("candles_saved"),
        "l2_samples": result.get("l2_samples"),
        "trade_tape_rows": result.get("trade_tape_rows"),
        "hl_api_retention_note": (
            "HL candleSnapshot returns at most ~5000 bars per request. "
            "Observed venue spans: 1h≈180d, 15m≈52d, 5m≈17d, 1m≈3.5d. "
            "Coverage % is vs the requested 180d window."
        ),
        "summary": coverage,
        "by_symbol_feed": _group_by_symbol_feed(coverage),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest = out_dir / "coverage_report_latest.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
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
            "volume_unit": report.get("volume_unit"),
        }
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description="HL research DB backfill")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--symbols", default=None, help="Comma-separated, e.g. BTC,ETH,SOL,HYPE")
    parser.add_argument("--no-microstructure", action="store_true")
    parser.add_argument(
        "--report-dir",
        default="data/research",
        help="Directory for coverage_report JSON output",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    research = cfg.get("research", {}) or {}
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else list(research.get("backfill_symbols", ["BTC", "ETH", "SOL", "HYPE"]))
    )
    days = args.days if args.days is not None else int(research.get("backfill_days", 7))
    min_cov = float(research.get("min_coverage_pct", 95.0)) / 100.0

    db = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    logger.info("Research DB: %s", db.db_path)
    result = backfill_hl_research(
        db,
        symbols=symbols,
        days=days,
        timeframes=tuple(research.get("backfill_timeframes", ["1m", "5m", "15m", "1h"])),
        sample_microstructure=not args.no_microstructure,
        min_coverage_pct=min_cov,
    )
    report_path = _write_coverage_report(result, ROOT / args.report_dir)
    print(json.dumps(result, indent=2))
    print(f"\nCoverage report: {report_path}")
    coverage = result.get("coverage", {})
    if not coverage.get("all_passed", False):
        logger.warning("Coverage audit reported failures — see %s", report_path)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
