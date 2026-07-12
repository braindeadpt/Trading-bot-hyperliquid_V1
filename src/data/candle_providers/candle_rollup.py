"""Roll up 1m HL-wire candles into higher timeframes (gap-aware)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.backtest.continuous_segments import resolve_gap_ms, segment_candles
from src.data.candle_providers.base import INTERVAL_MS
from src.utils.helpers import safe_float

ROLLUP_TARGETS = ("5m", "15m", "1h")


def bucket_open_ms(open_ms: int, target_interval: str) -> int:
    gap = INTERVAL_MS[target_interval]
    return (int(open_ms) // gap) * gap


def _wire_close_ms(row: Dict[str, Any]) -> int:
    return int(row["T"])


def _is_contiguous_1m_chunk(chunk: List[Dict[str, Any]], *, max_gap_ms: int) -> bool:
    if len(chunk) < 2:
        return True
    ordered = sorted(chunk, key=_wire_close_ms)
    for i in range(1, len(ordered)):
        if _wire_close_ms(ordered[i]) - _wire_close_ms(ordered[i - 1]) > max_gap_ms:
            return False
    return True


def _aggregate_bucket(
    chunk: List[Dict[str, Any]],
    target_interval: str,
    *,
    symbol: str,
) -> Dict[str, Any]:
    ordered = sorted(chunk, key=lambda r: int(r["t"]))
    gap = INTERVAL_MS[target_interval]
    open_ms = bucket_open_ms(int(ordered[0]["t"]), target_interval)
    highs = [safe_float(r["h"]) for r in ordered]
    lows = [safe_float(r["l"]) for r in ordered]
    vol = sum(safe_float(r.get("v")) for r in ordered)
    trades = sum(int(r.get("n", 0)) for r in ordered)
    return {
        "s": symbol,
        "i": target_interval,
        "t": open_ms,
        "T": open_ms + gap - 1,
        "o": ordered[0]["o"],
        "h": str(max(highs)),
        "l": str(min(lows)),
        "c": ordered[-1]["c"],
        "v": str(vol),
        "n": trades,
        "_rollup_from": "1m",
        "_rollup_bar_count": len(ordered),
    }


def rollup_1m_to_interval(
    rows_1m: List[Dict[str, Any]],
    target_interval: str,
    *,
    symbol: str,
    gap_intervals: int = 2,
    gap_intervals_by_tf: Optional[Dict[str, int]] = None,
    max_gap_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Aggregate closed 1m wire rows into *target_interval* candles."""
    if target_interval == "1m":
        return list(rows_1m)
    if target_interval not in ROLLUP_TARGETS:
        raise ValueError(f"Unsupported rollup target: {target_interval}")

    gap_1m = resolve_gap_ms(
        "1m",
        gap_intervals=gap_intervals,
        gap_intervals_by_tf=gap_intervals_by_tf,
        max_gap_ms=max_gap_ms,
    )
    target_gap = INTERVAL_MS[target_interval]
    bars_per_bucket = target_gap // INTERVAL_MS["1m"]

    segments = segment_candles(rows_1m, max_gap_ms=gap_1m)
    out: List[Dict[str, Any]] = []

    for segment in segments:
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for row in segment:
            bo = bucket_open_ms(int(row["t"]), target_interval)
            buckets.setdefault(bo, []).append(row)

        for bo in sorted(buckets):
            chunk = sorted(buckets[bo], key=lambda r: int(r["t"]))
            if len(chunk) != bars_per_bucket:
                continue
            if not _is_contiguous_1m_chunk(chunk, max_gap_ms=gap_1m):
                continue
            expected_opens = [bo + i * INTERVAL_MS["1m"] for i in range(bars_per_bucket)]
            actual_opens = [int(r["t"]) for r in chunk]
            if actual_opens != expected_opens:
                continue
            out.append(_aggregate_bucket(chunk, target_interval, symbol=symbol))

    return sorted(out, key=_wire_close_ms)


def rollup_1m_all_targets(
    rows_1m: List[Dict[str, Any]],
    *,
    symbol: str,
    targets: Optional[List[str]] = None,
    gap_intervals: int = 2,
    gap_intervals_by_tf: Optional[Dict[str, int]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Roll 1m rows into each requested higher timeframe."""
    tfs = targets or list(ROLLUP_TARGETS)
    return {
        tf: rollup_1m_to_interval(
            rows_1m,
            tf,
            symbol=symbol,
            gap_intervals=gap_intervals,
            gap_intervals_by_tf=gap_intervals_by_tf,
        )
        for tf in tfs
    }
