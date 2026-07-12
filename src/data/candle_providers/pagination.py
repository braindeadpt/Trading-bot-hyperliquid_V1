"""Forward pagination for HL-wire candle providers (<=5000 bars/page)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src.data.candle_providers.base import (
    INTERVAL_MS,
    MAX_CANDLES_PER_PAGE,
    CandleProvider,
    PaginatedCandleResult,
)
from src.data.candle_providers.checkpoint import (
    BackfillCheckpoint,
    clear_checkpoint,
    load_checkpoint,
    save_checkpoint,
)
from src.data.candle_providers.validation import sort_and_dedupe_rows, validate_page_order

logger = logging.getLogger(__name__)

PAGE_SLEEP_SEC = 0.1


async def paginate_candles_chronological(
    provider: CandleProvider,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    checkpoint_path: Optional[Any] = None,
    page_sleep_sec: float = PAGE_SLEEP_SEC,
    on_page: Optional[Any] = None,
) -> PaginatedCandleResult:
    """Download candles with forward ``startTime`` pagination.

    GoldRush HyperCore pages by advancing ``startTime`` past the last close ``T``.
    Checkpoints are persisted after each page for resume.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"Unsupported interval: {interval}")

    gap = INTERVAL_MS[interval]
    key = f"{symbol.upper()}:{interval}"
    checkpoints = load_checkpoint(checkpoint_path)
    cursor = int(start_ms)
    rows_fetched = 0
    resumed = False

    existing = checkpoints.get(key)
    if (
        existing is not None
        and existing.provider == provider.name
        and existing.window_start_ms == int(start_ms)
        and existing.window_end_ms == int(end_ms)
        and existing.cursor_ms > cursor
    ):
        cursor = existing.cursor_ms
        rows_fetched = existing.rows_fetched
        resumed = True
        logger.info(
            "Resuming %s %s %s from cursor %d (%d rows prior)",
            provider.name,
            symbol,
            interval,
            cursor,
            rows_fetched,
        )

    all_rows: List[Dict[str, Any]] = []
    seen_close: set[int] = set()
    pages = 0
    duplicates_skipped = 0
    # GoldRush (and HL wire) return the *latest* candles inside [start, end]
    # when the window spans more than one page. Cap each request to <=5000 bars.
    max_chunk_ms = MAX_CANDLES_PER_PAGE * gap

    while cursor < end_ms:
        chunk_end = min(cursor + max_chunk_ms - 1, end_ms)
        page = await provider.fetch_page(symbol, interval, cursor, chunk_end)
        pages += 1
        if not page.rows:
            cursor = chunk_end + 1
            await asyncio.sleep(page_sleep_sec)
            continue

        validate_page_order(page.rows, interval=interval)
        fresh = [r for r in page.rows if int(r["T"]) not in seen_close]
        duplicates_skipped += len(page.rows) - len(fresh)
        for r in fresh:
            seen_close.add(int(r["T"]))
        all_rows.extend(fresh)
        rows_fetched += len(fresh)

        if on_page is not None:
            on_page(page, len(fresh))

        last_close = int(page.rows[-1]["T"])
        adv_cursor = last_close + 1
        if adv_cursor <= cursor:
            adv_cursor = last_close + gap
        if adv_cursor <= cursor:
            adv_cursor = chunk_end + 1
        cursor = adv_cursor

        save_checkpoint(
            BackfillCheckpoint(
                provider=provider.name,
                symbol=symbol.upper(),
                interval=interval,
                window_start_ms=int(start_ms),
                window_end_ms=int(end_ms),
                cursor_ms=cursor,
                rows_fetched=rows_fetched,
            ),
            checkpoint_path,
        )

        await asyncio.sleep(page_sleep_sec)

    deduped, extra_dup = sort_and_dedupe_rows(all_rows)
    duplicates_skipped += extra_dup
    clear_checkpoint(symbol, interval, checkpoint_path)

    return PaginatedCandleResult(
        rows=deduped,
        pages_fetched=pages,
        duplicates_skipped=duplicates_skipped,
        resumed_from_checkpoint=resumed,
    )
