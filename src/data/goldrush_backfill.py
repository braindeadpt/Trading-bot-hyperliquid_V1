"""Research candle backfill with parity audit (GoldRush / Coinalyze / auto)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from src.data.candle_providers.coinalyze_hl import (
    CoinalyzeCandleProvider,
    CoinalyzeConfigError,
)
from src.data.candle_providers.goldrush_hypercore import (
    GoldrushConfigError,
    GoldrushHypercoreCandleProvider,
)
from src.data.candle_providers.tick_meta import load_meta_cache_from_meta_response
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.data.candle_providers.hyperliquid_public import HyperliquidPublicCandleProvider
from src.data.candle_providers.pagination import paginate_candles_chronological
from src.data.candle_providers.parity import compare_candle_overlap
from src.data.coverage_audit import (
    FeedCoverageReport,
    audit_candle_series,
    reports_to_json,
    summarize_coverage_reports,
)
from src.data.database import Candle
from src.data.hl_research_backfill import DEFAULT_SYMBOLS, DEFAULT_TIMEFRAMES, INTERVAL_MS, hl_snapshot_to_candle
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import SeriesMetadata

logger = logging.getLogger(__name__)


def _rows_to_candles(rows: List[Dict[str, Any]], symbol: str) -> List[Candle]:
    return [hl_snapshot_to_candle(r, symbol) for r in rows]


def _make_provider(name: str) -> Any:
    """Instantiate a research candle provider by name."""
    if name == "goldrush":
        return GoldrushHypercoreCandleProvider(max_requests_per_second=4.0)
    if name == "coinalyze":
        return CoinalyzeCandleProvider(max_requests_per_second=4.0)
    raise ValueError(f"unknown research candle provider {name!r}")


def _meta_for(provider_name: str) -> SeriesMetadata:
    if provider_name == "coinalyze_hl":
        return SeriesMetadata.coinalyze_hl_candles()
    return SeriesMetadata.goldrush_candles()


async def run_goldrush_research_backfill(
    db: ResearchDatabase,
    *,
    symbols: Sequence[str] = DEFAULT_SYMBOLS,
    days: int = 180,
    timeframes: Sequence[str] = DEFAULT_TIMEFRAMES,
    min_coverage_pct: float = 0.99,
    run_parity: bool = True,
    provider: str = "auto",
) -> Dict[str, Any]:
    """Backfill HL candles via a research provider with official overlap audit.

    ``provider``: "goldrush" | "coinalyze" | "auto". In auto mode the
    provider chain is tried in order and the first one that can be
    constructed (key present, no billing failure) is used — GoldRush is
    kept first for continuity but its account ran out of credits on
    2026-09-10 (402 insufficient_credits on every request), so auto mode
    currently resolves to Coinalyze (HL-native ``*_PERP.A`` markets,
    ~60-90d depth, real taker-buy volume).
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days) * 86_400_000
    total_inserted = 0
    total_skipped = 0
    reports: List[FeedCoverageReport] = []
    parity_reports: List[Dict[str, Any]] = []
    pages_total = 0

    chain = ["goldrush", "coinalyze"] if provider == "auto" else [provider]
    source_prov = None
    last_err: Optional[Exception] = None
    for pname in chain:
        try:
            candidate = _make_provider(pname)
            source_prov = candidate
            break
        except (GoldrushConfigError, CoinalyzeConfigError) as exc:
            logger.warning("provider %s unavailable: %s", pname, exc)
            last_err = exc
    if source_prov is None:
        raise RuntimeError(
            f"no research candle provider available (tried {chain}): {last_err}"
        )
    meta = _meta_for(source_prov.name)
    logger.info("research backfill provider: %s", source_prov.name)

    async with HyperliquidRESTClient() as rest_client:
        meta_cache = load_meta_cache_from_meta_response(
            await rest_client.meta_and_asset_ctxs(),
        )
    async with source_prov as src:
        async with HyperliquidPublicCandleProvider() as official:
            for sym in symbols:
                sym_u = sym.upper()
                for tf in timeframes:
                    if tf not in INTERVAL_MS:
                        continue
                    try:
                        result = await paginate_candles_chronological(
                            src,
                            sym_u,
                            tf,
                            start_ms,
                            end_ms,
                        )
                        pages_total += result.pages_fetched
                        candles = _rows_to_candles(result.rows, sym_u)

                        parity: Optional[Dict[str, Any]] = None
                        if run_parity and result.rows:
                            off_result = await paginate_candles_chronological(
                                official,
                                sym_u,
                                tf,
                                start_ms,
                                end_ms,
                            )
                            pr = compare_candle_overlap(
                                off_result.rows,
                                result.rows,
                                symbol=sym_u,
                                interval=tf,
                                meta_cache=meta_cache,
                            )
                            parity = pr.to_dict()
                            parity_reports.append(parity)
                            if not pr.passed and pr.matched_bars > 0:
                                logger.warning(
                                    "Parity divergence %s %s — "
                                    "official rows protected (%d mismatches)",
                                    sym_u,
                                    tf,
                                    len(pr.mismatches),
                                )

                        inserted, skipped = db.save_research_candles_non_destructive(
                            candles, tf, meta,
                        )
                        total_inserted += inserted
                        total_skipped += skipped
                        logger.info(
                            "%s backfill %s %s: fetched=%d inserted=%d "
                            "skipped_protected=%d pages=%d",
                            source_prov.name,
                            sym_u,
                            tf,
                            len(candles),
                            inserted,
                            skipped,
                            result.pages_fetched,
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
                            f"{source_prov.name}_candles_{tf}",
                            start_ms,
                            end_ms,
                            reports_to_json([report]),
                            int(time.time() * 1000),
                        )
                    except Exception as exc:
                        logger.warning(
                            "%s backfill failed %s %s: %s",
                            source_prov.name, sym_u, tf, exc,
                        )

    summary = summarize_coverage_reports(reports)
    return {
        "provider": source_prov.name,
        "candles_inserted": total_inserted,
        "candles_skipped_protected": total_skipped,
        "pages_fetched": pages_total,
        "coverage": summary,
        "parity_reports": parity_reports,
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "min_coverage_pct": min_coverage_pct,
    }


def backfill_goldrush_research(db: ResearchDatabase, **kwargs: Any) -> Dict[str, Any]:
    """Sync facade for scripts."""
    return asyncio.run(run_goldrush_research_backfill(db, **kwargs))
