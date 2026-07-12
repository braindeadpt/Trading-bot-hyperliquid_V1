"""Split candle timelines at research gaps — never treat holes as continuous."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from src.data.candle_providers.base import INTERVAL_MS

DEFAULT_GAP_INTERVALS = 2
DEFAULT_RESEARCH_GAP_MS = INTERVAL_MS["1m"] * DEFAULT_GAP_INTERVALS  # 2 × 1m


@dataclass(frozen=True)
class ContinuousSegment:
    start_ms: int
    end_ms: int
    bar_count: int


def gap_threshold_ms(
    interval: str,
    gap_intervals: int = DEFAULT_GAP_INTERVALS,
) -> int:
    """Split when step exceeds *gap_intervals* expected bar spans for *interval*."""
    gap_ms = INTERVAL_MS.get(interval, INTERVAL_MS["1m"])
    return int(gap_ms * max(1, gap_intervals))


def resolve_gap_ms(
    interval: str = "1m",
    *,
    gap_intervals: Optional[int] = None,
    gap_intervals_by_tf: Optional[dict[str, int]] = None,
    max_gap_ms: Optional[int] = None,
) -> int:
    """Resolve gap threshold: explicit *max_gap_ms* wins, else per-TF intervals."""
    if max_gap_ms is not None:
        return int(max_gap_ms)
    n = DEFAULT_GAP_INTERVALS
    if gap_intervals_by_tf and interval in gap_intervals_by_tf:
        n = int(gap_intervals_by_tf[interval])
    elif gap_intervals is not None:
        n = int(gap_intervals)
    return gap_threshold_ms(interval, n)


def find_gap_boundaries(
    timestamps_ms: Sequence[int],
    *,
    max_gap_ms: int = DEFAULT_RESEARCH_GAP_MS,
) -> List[int]:
    """Return close timestamps immediately before a gap larger than *max_gap_ms*."""
    if len(timestamps_ms) < 2:
        return []
    ts = sorted(int(t) for t in timestamps_ms)
    boundaries: List[int] = []
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] > max_gap_ms:
            boundaries.append(ts[i - 1])
    return boundaries


def segment_timeline(
    timestamps_ms: Sequence[int],
    *,
    max_gap_ms: int = DEFAULT_RESEARCH_GAP_MS,
) -> List[ContinuousSegment]:
    """Partition sorted timestamps into continuous segments."""
    if not timestamps_ms:
        return []
    ts = sorted(int(t) for t in timestamps_ms)
    segments: List[ContinuousSegment] = []
    seg_start = ts[0]
    seg_count = 1
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] > max_gap_ms:
            segments.append(
                ContinuousSegment(start_ms=seg_start, end_ms=ts[i - 1], bar_count=seg_count),
            )
            seg_start = ts[i]
            seg_count = 1
        else:
            seg_count += 1
    segments.append(ContinuousSegment(start_ms=seg_start, end_ms=ts[-1], bar_count=seg_count))
    return segments


def is_cross_gap(
    prev_ts: Optional[int],
    current_ts: int,
    *,
    max_gap_ms: int = DEFAULT_RESEARCH_GAP_MS,
) -> bool:
    """True when advancing from *prev_ts* to *current_ts* crosses a research gap."""
    if prev_ts is None:
        return False
    return int(current_ts) - int(prev_ts) > max_gap_ms


def segment_candles(
    candles: Sequence[Any],
    *,
    max_gap_ms: int = DEFAULT_RESEARCH_GAP_MS,
) -> List[List[Any]]:
    """Split candle objects by timestamp_ms gaps."""
    if not candles:
        return []

    def _ts(c: Any) -> int:
        if isinstance(c, dict):
            if "timestamp_ms" in c:
                return int(c["timestamp_ms"])
            return int(c["T"])
        return int(getattr(c, "timestamp_ms"))

    ordered = sorted(candles, key=_ts)
    chunks: List[List[Any]] = [[ordered[0]]]
    for c in ordered[1:]:
        if _ts(c) - _ts(chunks[-1][-1]) > max_gap_ms:
            chunks.append([c])
        else:
            chunks[-1].append(c)
    return chunks
