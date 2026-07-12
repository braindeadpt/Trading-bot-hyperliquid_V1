"""CLI: GoldRush vs HL official raw parity diagnostic (no OOS / no backtest)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_providers.goldrush_hypercore import GoldrushHypercoreCandleProvider
from src.data.candle_providers.hyperliquid_public import HyperliquidPublicCandleProvider
from src.data.candle_providers.parity import compare_candle_overlap
from src.data.candle_providers.parity_diagnostic import (
    filter_closed_rows,
    last_closed_end_ms,
    run_full_diagnostic,
)
from src.data.candle_providers.tick_meta import load_meta_cache_from_meta_response
from src.data.research_parity_ledger import ResearchParityLedger
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.utils.config import load_config
from src.utils.helpers import safe_write_file, validate_safe_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
DEFAULT_INTERVALS = ["1h", "15m", "5m", "1m"]


async def _fetch_meta() -> Dict[str, Dict[str, int]]:
    async with HyperliquidRESTClient() as client:
        raw = await client.meta_and_asset_ctxs()
    return load_meta_cache_from_meta_response(raw)


async def _fetch_overlap_rows(
    symbol: str,
    interval: str,
    sample_bars: int,
    end_ms: int,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from src.data.candle_providers.base import INTERVAL_MS

    gap = INTERVAL_MS[interval]
    start_ms = end_ms - sample_bars * gap
    async with GoldrushHypercoreCandleProvider(max_requests_per_second=4.0) as gr:
        async with HyperliquidPublicCandleProvider() as hl:
            gr_page = await gr.fetch_page(symbol, interval, start_ms, end_ms)
            hl_page = await hl.fetch_page(symbol, interval, start_ms, end_ms)
    gr_rows = filter_closed_rows(gr_page.rows, interval, end_ms=end_ms)
    hl_rows = filter_closed_rows(hl_page.rows, interval, end_ms=end_ms)
    return hl_rows, gr_rows


def _write_report(payload: Dict[str, Any], out_dir: Path) -> Path:
    rel = out_dir.relative_to(ROOT) if out_dir.is_absolute() else out_dir
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise RuntimeError(f"Unsafe report directory: {out_dir}")
    safe.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = safe / f"goldrush_parity_diagnostic_{ts}.json"
    latest = safe / "goldrush_parity_diagnostic_latest.json"
    body = json.dumps(payload, indent=2, sort_keys=True)
    if not safe_write_file(path, body):
        raise RuntimeError(f"Failed to write {path}")
    if not safe_write_file(latest, body):
        raise RuntimeError(f"Failed to write {latest}")
    return path


async def run_diagnostic(
    *,
    symbols: List[str],
    intervals: List[str],
    sample_bars: int,
) -> Dict[str, Any]:
    end_ms = last_closed_end_ms()
    meta_cache = await _fetch_meta()
    series_reports: List[Dict[str, Any]] = []
    all_passed = True

    for sym in symbols:
        for interval in intervals:
            logger.info("Diagnosing %s %s (%d bars max)...", sym, interval, sample_bars)
            try:
                official_rows, goldrush_rows = await _fetch_overlap_rows(
                    sym, interval, sample_bars, end_ms,
                )
            except Exception as exc:
                logger.warning("Fetch failed %s %s: %s", sym, interval, exc)
                all_passed = False
                series_reports.append({
                    "symbol": sym,
                    "interval": interval,
                    "error": str(exc),
                    "parity_passed": False,
                })
                continue

            diag = run_full_diagnostic(
                official_rows,
                goldrush_rows,
                symbol=sym,
                interval=interval,
                meta_cache=meta_cache,
            )
            parity = compare_candle_overlap(
                official_rows,
                goldrush_rows,
                symbol=sym,
                interval=interval,
                meta_cache=meta_cache,
            )
            passed = bool(diag.get("parity_passed")) and parity.passed
            if not passed:
                all_passed = False
            series_reports.append({
                **diag,
                "tick_parity_report": parity.to_dict(),
                "sample_bars_requested": sample_bars,
                "window_end_ms": end_ms,
                "parity_passed": passed,
            })
            logger.info(
                "%s %s: match_key=%s parity_passed=%s matched=%d",
                sym,
                interval,
                diag.get("recommended_match_key"),
                passed,
                parity.matched_bars,
            )

    return {
        "generated_at_ms": int(time.time() * 1000),
        "sample_bars": sample_bars,
        "window_end_ms": end_ms,
        "all_passed": all_passed,
        "oos_dataset_ready": all_passed,
        "series": series_reports,
        "summary": {
            "total": len(series_reports),
            "passed": sum(1 for s in series_reports if s.get("parity_passed")),
            "failed": sum(1 for s in series_reports if not s.get("parity_passed")),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GoldRush parity diagnostic")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--intervals", default=",".join(DEFAULT_INTERVALS))
    parser.add_argument("--sample-bars", type=int, default=300)
    parser.add_argument("--report-dir", default="data/research")
    args = parser.parse_args()

    load_config(ROOT / args.config)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    if not 100 <= args.sample_bars <= 500:
        logger.error("--sample-bars must be between 100 and 500")
        return 2

    result = asyncio.run(
        run_diagnostic(symbols=symbols, intervals=intervals, sample_bars=args.sample_bars),
    )
    report_path = _write_report(result, ROOT / args.report_dir)
    result["report_path"] = str(report_path)
    ledger = ResearchParityLedger()
    ledger.save_validation(result)
    print(json.dumps(result, indent=2))
    print(f"\nParity diagnostic: {report_path}")
    if not result.get("all_passed"):
        logger.warning("Parity NOT validated — dataset blocked for OOS")
        return 2
    logger.info("Parity validated — ledger updated (still no OOS execution here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
