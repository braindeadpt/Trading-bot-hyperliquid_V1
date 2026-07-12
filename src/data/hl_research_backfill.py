"""Hyperliquid venue candle backfill into research DB (Phase 07)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data.coverage_audit import (
    FeedCoverageReport,
    audit_candle_series,
    reports_to_json,
    summarize_coverage_reports,
)
from src.data.database import Candle
from src.data.research_database import ResearchDatabase, TradeTapeRecord
from src.data.series_metadata import (
    SOURCE_HL_L2_SAMPLE,
    SOURCE_HL_TRADE_TAPE,
    VENUE_HYPERLIQUID,
    HL_API_VERSION,
    SeriesMetadata,
)
from src.data.hl_l2_parse import l2_snapshot_from_book
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
DEFAULT_TIMEFRAMES = ("1m", "5m", "15m", "1h")
INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}
MAX_CANDLES_PER_REQUEST = 5000
PAGE_SLEEP_SEC = 0.35


def hl_snapshot_to_candle(row: Dict[str, Any], symbol: str) -> Candle:
    """Convert Hyperliquid candleSnapshot row to :class:`Candle`.

    HL does not expose taker-buy split — buy_volume/sell_volume are NULL
    (never zero) so CVD cannot misread neutral flow as real data.
    """
    close_time = int(row["T"])
    open_time = int(row["t"])
    volume = safe_float(row.get("v"), 0.0)
    trade_count = int(row.get("n", 0))
    c = Candle(
        symbol=symbol.upper(),
        timestamp_ms=close_time,
        open=safe_float(row.get("o"), 0.0),
        high=safe_float(row.get("h"), 0.0),
        low=safe_float(row.get("l"), 0.0),
        close=safe_float(row.get("c"), 0.0),
        volume=volume,
        funding_rate=None,
        oi_total=None,
        oi_delta=None,
        buy_volume=None,
        sell_volume=None,
        trade_count=trade_count,
    )
    object.__setattr__(c, "open_time_ms", open_time)  # type: ignore[misc]
    return c


def split_taker_volume_from_kline(
    kline_row: list,
    volume: float,
) -> Tuple[Optional[float], Optional[float], bool]:
    """Derive buy/sell from Binance kline taker-buy field when present.

    Returns (buy_volume, sell_volume, taker_split_available).
    When unavailable, returns (None, None, False) — never fake zeros.
    """
    if len(kline_row) <= 9:
        return None, None, False
    buy = safe_float(kline_row[9], 0.0)
    if buy <= 0.0 and volume <= 0.0:
        return None, None, False
    sell = max(volume - buy, 0.0)
    return buy, sell, True


async def _download_hl_candles(
    client: HyperliquidRESTClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> List[Candle]:
    """Download candles via candleSnapshot with backward pagination.

    HL returns up to 5000 *most recent* candles inside [start_ms, end_ms].
    To reach older history we shrink ``end_ms`` after each page.
    """
    seen_close: set[int] = set()
    candles: List[Candle] = []
    page_end = end_ms
    while page_end > start_ms:
        page = await client.candle_snapshot(symbol, interval, start_ms, page_end)
        if not page:
            break
        page.sort(key=lambda r: int(r["T"]))
        earliest_open = min(int(r.get("t", r["T"])) for r in page)
        fresh = [r for r in page if int(r["T"]) not in seen_close]
        for r in fresh:
            seen_close.add(int(r["T"]))
        if fresh:
            candles.extend(hl_snapshot_to_candle(r, symbol) for r in fresh)
        if earliest_open <= start_ms:
            break
        prior_end = earliest_open - 1
        if prior_end >= page_end:
            break
        page_end = prior_end
        if len(page) < MAX_CANDLES_PER_REQUEST and not fresh:
            break
        await asyncio.sleep(PAGE_SLEEP_SEC)
    candles.sort(key=lambda c: c.timestamp_ms)
    return candles


def _l2_from_book(symbol: str, book: Dict[str, Any], ts_ms: int) -> Optional[Any]:
    return l2_snapshot_from_book(symbol, book, ts_ms, int(time.time() * 1000))


async def _sample_l2_and_tape(
    client: HyperliquidRESTClient,
    db: ResearchDatabase,
    symbols: Sequence[str],
) -> Tuple[int, int]:
    """Sample current L2 book and recent trades for Tier-A replay prep."""
    l2_count = 0
    tape_count = 0
    ts = int(time.time() * 1000)
    ingested = ts
    for sym in symbols:
        sym_u = sym.upper()
        try:
            book = await client.l2_book(sym_u)
            snap = _l2_from_book(sym_u, book, ts)
            if snap is not None:
                db.save_l2_snapshots([snap])
                l2_count += 1
        except Exception as exc:
            logger.warning("L2 sample failed %s: %s", sym_u, exc)

        try:
            trades = await client.recent_trades(sym_u)
            tape_rows: List[TradeTapeRecord] = []
            for t in trades:
                tape_rows.append(
                    TradeTapeRecord(
                        symbol=sym_u,
                        timestamp_ms=int(t.get("time", t.get("T", ts))),
                        price=safe_float(t.get("px", t.get("p")), 0.0),
                        size=safe_float(t.get("sz", t.get("s")), 0.0),
                        side=str(t.get("side", t.get("S", "unknown"))),
                        trade_id=str(t.get("tid", t.get("id", ""))) or None,
                        source=SOURCE_HL_TRADE_TAPE,
                        venue=VENUE_HYPERLIQUID,
                        api_version=HL_API_VERSION,
                        ingested_at_ms=ingested,
                    )
                )
            if tape_rows:
                db.save_trade_tape(tape_rows)
                tape_count += len(tape_rows)
        except Exception as exc:
            logger.warning("Trade tape sample failed %s: %s", sym_u, exc)
    return l2_count, tape_count


async def run_hl_research_backfill(
    db: ResearchDatabase,
    *,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    days: int = 7,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    sample_microstructure: bool = True,
    min_coverage_pct: float = 0.95,
) -> Dict[str, Any]:
    """Backfill HL candleSnapshot + optional L2/trade tape samples."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    meta = SeriesMetadata.hl_candles(taker_split=False)
    total_candles = 0
    reports: List[FeedCoverageReport] = []

    async with HyperliquidRESTClient() as client:
        for sym in symbols:
            sym_u = sym.upper()
            for tf in timeframes:
                if tf not in INTERVAL_MS:
                    continue
                try:
                    candles = await _download_hl_candles(
                        client, sym_u, tf, start_ms, end_ms,
                    )
                    if candles:
                        db.save_research_candles(candles, tf, meta)
                        total_candles += len(candles)
                        logger.info(
                            "HL research backfill %s %s: %d candles",
                            sym_u, tf, len(candles),
                        )
                    report = audit_candle_series(
                        sym_u,
                        candles,
                        feed=f"candles_{tf}",
                        start_ms=start_ms,
                        end_ms=end_ms,
                        venue=meta.venue,
                        source=meta.source,
                        volume_unit=meta.volume_unit,
                        min_coverage_pct=min_coverage_pct,
                    )
                    reports.append(report)
                    db.save_coverage_report(
                        sym_u,
                        f"candles_{tf}",
                        start_ms,
                        end_ms,
                        reports_to_json([report]),
                        int(time.time() * 1000),
                    )
                except Exception as exc:
                    logger.warning("HL backfill failed %s %s: %s", sym_u, tf, exc)

        l2_n, tape_n = (0, 0)
        if sample_microstructure:
            l2_n, tape_n = await _sample_l2_and_tape(client, db, symbols)

    summary = summarize_coverage_reports(reports)
    return {
        "candles_saved": total_candles,
        "coverage": summary,
        "l2_samples": l2_n,
        "trade_tape_rows": tape_n,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
    }


def backfill_hl_research(
    db: ResearchDatabase,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Sync facade for scripts and tests."""
    return asyncio.run(run_hl_research_backfill(db, **kwargs))
