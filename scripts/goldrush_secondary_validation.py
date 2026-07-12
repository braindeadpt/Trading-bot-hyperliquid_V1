"""CLI: GoldRush secondary validation (1m rollup, dynamic quantum, support package)."""

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

from src.backtest.continuous_segments import resolve_gap_ms
from src.data.candle_providers.base import INTERVAL_MS
from src.data.candle_providers.goldrush_hypercore import GoldrushHypercoreCandleProvider
from src.data.candle_providers.hyperliquid_public import HyperliquidPublicCandleProvider
from src.data.candle_providers.parity_diagnostic import filter_closed_rows, last_closed_end_ms
from src.data.candle_providers.parity_secondary import (
    build_secondary_report,
    run_secondary_validation,
)
from src.data.candle_providers.support_package import write_support_package
from src.data.candle_providers.tick_meta import load_meta_cache_from_meta_response
from src.data.research_parity_ledger import ResearchParityLedger
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.utils.config import load_config
from src.utils.helpers import safe_write_file, validate_safe_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]


async def _fetch_meta() -> Dict[str, Dict[str, int]]:
    async with HyperliquidRESTClient() as client:
        raw = await client.meta_and_asset_ctxs()
    return load_meta_cache_from_meta_response(raw)


async def _fetch_symbol_bundle(
    symbol: str,
    sample_bars: int,
    end_ms: int,
) -> Dict[str, Any]:
    """Fetch 1m + higher TF rows for secondary validation."""
    gap_1m = INTERVAL_MS["1m"]
    # Need enough 1m bars to rollup highest TF
    start_ms = end_ms - sample_bars * INTERVAL_MS["1h"]

    async with GoldrushHypercoreCandleProvider(max_requests_per_second=4.0) as gr:
        async with HyperliquidPublicCandleProvider() as hl:
            gr_1m_page = await gr.fetch_page(symbol, "1m", start_ms, end_ms)
            hl_1m_page = await hl.fetch_page(symbol, "1m", start_ms, end_ms)
            gr_direct: Dict[str, List[Dict[str, Any]]] = {}
            hl_official: Dict[str, List[Dict[str, Any]]] = {}
            for interval in ("5m", "15m", "1h"):
                tf_start = end_ms - sample_bars * INTERVAL_MS[interval]
                gr_page = await gr.fetch_page(symbol, interval, tf_start, end_ms)
                hl_page = await hl.fetch_page(symbol, interval, tf_start, end_ms)
                gr_direct[interval] = filter_closed_rows(gr_page.rows, interval, end_ms=end_ms)
                hl_official[interval] = filter_closed_rows(hl_page.rows, interval, end_ms=end_ms)

    gr_1m = filter_closed_rows(gr_1m_page.rows, "1m", end_ms=end_ms)
    hl_1m = filter_closed_rows(hl_1m_page.rows, "1m", end_ms=end_ms)
    # Trim to requested sample window on 1m
    trim_start = end_ms - sample_bars * gap_1m
    gr_1m = [r for r in gr_1m if int(r["T"]) >= trim_start]
    hl_1m = [r for r in hl_1m if int(r["T"]) >= trim_start]

    return {
        "gr_1m": gr_1m,
        "hl_1m": hl_1m,
        "gr_direct": gr_direct,
        "hl_official": hl_official,
        "window_start_ms": trim_start,
    }


def _write_report(payload: Dict[str, Any], out_dir: Path, prefix: str) -> Path:
    rel = out_dir.relative_to(ROOT) if out_dir.is_absolute() else out_dir
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise RuntimeError(f"Unsafe report directory: {out_dir}")
    safe.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = safe / f"{prefix}_{ts}.json"
    latest = safe / f"{prefix}_latest.json"
    body = json.dumps(payload, indent=2, sort_keys=True)
    if not safe_write_file(path, body):
        raise RuntimeError(f"Failed to write {path}")
    if not safe_write_file(latest, body):
        raise RuntimeError(f"Failed to write {latest}")
    return path


async def run_secondary(
    *,
    symbols: List[str],
    sample_bars: int,
    gap_intervals: int,
    gap_intervals_by_tf: Dict[str, int],
) -> Dict[str, Any]:
    end_ms = last_closed_end_ms()
    meta_cache = await _fetch_meta()
    series_reports: List[Dict[str, Any]] = []
    window_start_ms: int | None = None

    for sym in symbols:
        logger.info("Secondary validation %s (%d 1m bars)...", sym, sample_bars)
        try:
            bundle = await _fetch_symbol_bundle(sym, sample_bars, end_ms)
        except Exception as exc:
            logger.warning("Fetch failed %s: %s", sym, exc)
            series_reports.append({
                "symbol": sym,
                "error": str(exc),
                "all_passed": False,
            })
            continue

        if window_start_ms is None:
            window_start_ms = bundle["window_start_ms"]

        result = run_secondary_validation(
            symbol=sym,
            gr_1m=bundle["gr_1m"],
            hl_1m=bundle["hl_1m"],
            gr_direct=bundle["gr_direct"],
            hl_official=bundle["hl_official"],
            meta_cache=meta_cache,
            gap_intervals=gap_intervals,
            gap_intervals_by_tf=gap_intervals_by_tf or None,
        )
        series_reports.append(result)
        logger.info(
            "%s: all_passed=%s comparisons=%d",
            sym,
            result.get("all_passed"),
            len(result.get("comparisons", [])),
        )

    report = build_secondary_report(
        series_reports,
        sample_bars=sample_bars,
        window_end_ms=end_ms,
        gap_intervals=gap_intervals,
    )
    report["meta_cache"] = meta_cache
    report["window_start_ms"] = window_start_ms
    report["gap_intervals_by_tf"] = gap_intervals_by_tf
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="GoldRush secondary parity validation")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--sample-bars", type=int, default=300)
    parser.add_argument("--report-dir", default="data/research")
    parser.add_argument("--gap-intervals", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config(ROOT / args.config)
    research = cfg.get("research", {}) or {}
    gap_intervals = int(
        args.gap_intervals
        if args.gap_intervals is not None
        else research.get("gap_intervals", 2)
    )
    gap_by_tf = research.get("gap_intervals_by_tf") or {}
    if not isinstance(gap_by_tf, dict):
        gap_by_tf = {}

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not 100 <= args.sample_bars <= 500:
        logger.error("--sample-bars must be between 100 and 500")
        return 2

    result = asyncio.run(
        run_secondary(
            symbols=symbols,
            sample_bars=args.sample_bars,
            gap_intervals=gap_intervals,
            gap_intervals_by_tf={str(k): int(v) for k, v in gap_by_tf.items()},
        ),
    )

    report_dir = ROOT / args.report_dir
    report_path = _write_report(result, report_dir, "goldrush_secondary_validation")
    result["report_path"] = str(report_path)

    support_path = write_support_package(
        result,
        report_dir,
        window_start_ms=result.get("window_start_ms"),
        meta_cache=result.get("meta_cache"),
        project_root=ROOT,
    )
    result["support_package_path"] = str(support_path)
    _write_report(result, report_dir, "goldrush_secondary_validation")

    ledger = ResearchParityLedger()
    ledger.save_validation(result)

    print(json.dumps({
        "all_passed": result.get("all_passed"),
        "summary": result.get("summary"),
        "report_path": result.get("report_path"),
        "support_package_path": result.get("support_package_path"),
        "node_trades_proposed": result.get("node_trades_reconstruction") is not None,
    }, indent=2))

    if not result.get("all_passed"):
        logger.warning("Secondary validation FAILED — OOS blocked; support package written")
        return 2
    logger.info("Secondary validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
