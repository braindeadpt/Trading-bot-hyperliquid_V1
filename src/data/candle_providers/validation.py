"""Timestamp validation for HL-wire candle rows."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from src.data.candle_providers.base import INTERVAL_MS
from src.utils.helpers import safe_float


class CandleValidationError(ValueError):
    """Invalid candle row or series ordering."""


def validate_candle_row(row: Dict[str, Any], *, interval: str) -> None:
    """Validate a single HL candleSnapshot row."""
    if "T" not in row or "t" not in row:
        raise CandleValidationError("missing open/close timestamps (t/T)")
    close_ms = int(row["T"])
    open_ms = int(row["t"])
    if open_ms >= close_ms:
        raise CandleValidationError(f"open_time >= close_time: {open_ms} >= {close_ms}")
    gap = INTERVAL_MS.get(interval, 60_000)
    if close_ms - open_ms > gap * 2:
        raise CandleValidationError(
            f"candle span {close_ms - open_ms}ms exceeds 2× interval {gap}ms",
        )
    for key in ("o", "h", "l", "c"):
        if key not in row:
            raise CandleValidationError(f"missing price field {key}")
        px = safe_float(row[key], -1.0)
        if px < 0:
            raise CandleValidationError(f"invalid price {key}={row[key]!r}")
    hi = safe_float(row["h"])
    lo = safe_float(row["l"])
    if hi < lo:
        raise CandleValidationError(f"high < low: {hi} < {lo}")


def validate_page_order(rows: List[Dict[str, Any]], *, interval: str) -> None:
    """Validate rows are strictly increasing by close time."""
    prev_close: int | None = None
    for row in rows:
        validate_candle_row(row, interval=interval)
        close_ms = int(row["T"])
        if prev_close is not None and close_ms <= prev_close:
            raise CandleValidationError(
                f"non-monotonic close times: {prev_close} then {close_ms}",
            )
        prev_close = close_ms


def sort_and_dedupe_rows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Sort by close time and drop duplicate ``T`` keys."""
    by_close: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        by_close[int(row["T"])] = row
    deduped = [by_close[k] for k in sorted(by_close)]
    skipped = len(rows) - len(deduped)
    return deduped, skipped
