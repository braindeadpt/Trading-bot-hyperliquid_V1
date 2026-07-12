"""Coverage audit for research / replay data contracts (Phase 07).

Checks continuity, duplicates, close-time alignment, stale auxiliary feeds,
feed span, and volume unit labelling.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.utils.helpers import safe_float

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}


@dataclass
class FeedCoverageReport:
    """Audit result for one symbol × feed over a window."""

    symbol: str
    feed: str
    venue: str
    start_ms: int
    end_ms: int
    bar_count: int
    expected_bars: int
    coverage_pct: float
    max_gap_ms: int
    duplicate_count: int
    close_time_violations: int
    stale_pct: float
    feed_span_ms: int
    volume_unit: str
    source: str = ""
    quality_flags: Dict[str, Any] = field(default_factory=dict)
    passed: bool = True
    failures: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _interval_ms_for_feed(feed: str) -> int:
    if feed.startswith("candles_"):
        tf = feed.split("_", 1)[1]
        return INTERVAL_MS.get(tf, 60_000)
    return 60_000


def audit_candle_series(
    symbol: str,
    candles: Sequence[Any],
    *,
    feed: str = "candles_1m",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    expected_interval_ms: Optional[int] = None,
    venue: str = "",
    source: str = "",
    volume_unit: str = "base",
    min_coverage_pct: float = 0.95,
    max_gap_multiplier: float = 2.0,
) -> FeedCoverageReport:
    """Audit OHLCV continuity and close-time semantics."""
    interval = expected_interval_ms or _interval_ms_for_feed(feed)
    if not candles:
        return FeedCoverageReport(
            symbol=symbol,
            feed=feed,
            venue=venue,
            start_ms=start_ms or 0,
            end_ms=end_ms or 0,
            bar_count=0,
            expected_bars=0,
            coverage_pct=0.0,
            max_gap_ms=0,
            duplicate_count=0,
            close_time_violations=0,
            stale_pct=100.0,
            feed_span_ms=0,
            volume_unit=volume_unit,
            source=source,
            passed=False,
            failures=["no_bars"],
        )

    def _ts(c: Any) -> int:
        if isinstance(c, dict):
            return int(c["timestamp_ms"])
        return int(getattr(c, "timestamp_ms"))

    ts_list = sorted(_ts(c) for c in candles)
    lo = start_ms if start_ms is not None else ts_list[0]
    hi = end_ms if end_ms is not None else ts_list[-1]
    expected = max(1, int((hi - lo) / interval) + 1)
    in_window = [t for t in ts_list if lo <= t <= hi]

    duplicate_count = len(in_window) - len(set(in_window))
    max_gap = 0
    for i in range(1, len(in_window)):
        gap = in_window[i] - in_window[i - 1]
        if gap > max_gap:
            max_gap = gap

    close_violations = 0
    for c in candles:
        ts = _ts(c)
        if ts < lo or ts > hi:
            continue
        open_ts = None
        if isinstance(c, dict):
            open_ts = c.get("open_time_ms")
        else:
            open_ts = getattr(c, "open_time_ms", None)
        if open_ts is not None and int(open_ts) >= ts:
            close_violations += 1

    coverage = len(in_window) / expected if expected > 0 else 0.0
    span = (max(in_window) - min(in_window)) if in_window else 0
    stale_pct = max(0.0, 100.0 * (1.0 - coverage))

    failures: List[str] = []
    if coverage < min_coverage_pct:
        failures.append(f"coverage_low:{coverage * 100:.1f}%<{min_coverage_pct * 100:.1f}%")
    if max_gap > interval * max_gap_multiplier:
        failures.append(f"gap_exceeds:{max_gap}ms>{int(interval * max_gap_multiplier)}ms")
    if duplicate_count > 0:
        failures.append(f"duplicates:{duplicate_count}")
    if close_violations > 0:
        failures.append(f"close_time_violations:{close_violations}")

    return FeedCoverageReport(
        symbol=symbol,
        feed=feed,
        venue=venue,
        start_ms=lo,
        end_ms=hi,
        bar_count=len(in_window),
        expected_bars=expected,
        coverage_pct=round(coverage, 6),
        max_gap_ms=max_gap,
        duplicate_count=duplicate_count,
        close_time_violations=close_violations,
        stale_pct=round(stale_pct, 4),
        feed_span_ms=span,
        volume_unit=volume_unit,
        source=source,
        passed=len(failures) == 0,
        failures=failures,
    )


def audit_auxiliary_feed(
    symbol: str,
    feed: str,
    points: Sequence[Tuple[int, Any, ...]],
    *,
    window_start_ms: int,
    window_end_ms: int,
    max_stale_ms: int = 300_000,
    venue: str = "",
    source: str = "",
    min_points: int = 1,
) -> FeedCoverageReport:
    """Audit funding/OI (or similar) freshness over the replay window."""
    if not points:
        return FeedCoverageReport(
            symbol=symbol,
            feed=feed,
            venue=venue,
            start_ms=window_start_ms,
            end_ms=window_end_ms,
            bar_count=0,
            expected_bars=0,
            coverage_pct=0.0,
            max_gap_ms=0,
            duplicate_count=0,
            close_time_violations=0,
            stale_pct=100.0,
            feed_span_ms=0,
            volume_unit="n/a",
            source=source,
            passed=False,
            failures=["feed_missing"],
        )

    ts_sorted = sorted(int(p[0]) for p in points)
    in_window = [t for t in ts_sorted if window_start_ms <= t <= window_end_ms]
    span = (max(in_window) - min(in_window)) if in_window else 0
    window_len = max(1, window_end_ms - window_start_ms)
    stale_slices = 0
    check_points = max(1, min(100, len(in_window)))
    step = max(1, len(in_window) // check_points)
    for i in range(0, len(in_window), step):
        t = in_window[i]
        prior = [x for x in in_window if x <= t]
        if not prior:
            stale_slices += 1
            continue
        last_ts = prior[-1]
        if t - last_ts > max_stale_ms:
            stale_slices += 1
    stale_pct = 100.0 * stale_slices / check_points if check_points else 100.0

    failures: List[str] = []
    if len(in_window) < min_points:
        failures.append(f"insufficient_points:{len(in_window)}<{min_points}")

    return FeedCoverageReport(
        symbol=symbol,
        feed=feed,
        venue=venue,
        start_ms=window_start_ms,
        end_ms=window_end_ms,
        bar_count=len(in_window),
        expected_bars=max(1, int(window_len / max_stale_ms)),
        coverage_pct=round(len(in_window) / max(1, int(window_len / max_stale_ms)), 6),
        max_gap_ms=0,
        duplicate_count=len(in_window) - len(set(in_window)),
        close_time_violations=0,
        stale_pct=round(stale_pct, 4),
        feed_span_ms=span,
        volume_unit="n/a",
        source=source,
        passed=len(failures) == 0,
        failures=failures,
    )


def summarize_coverage_reports(reports: Sequence[FeedCoverageReport]) -> Dict[str, Any]:
    """Aggregate per-symbol coverage for manifest attachment."""
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    all_passed = True
    for r in reports:
        by_symbol.setdefault(r.symbol, []).append(r.to_dict())
        if not r.passed:
            all_passed = False
    return {
        "all_passed": all_passed,
        "reports": [r.to_dict() for r in reports],
        "by_symbol": by_symbol,
    }


def reports_to_json(reports: Sequence[FeedCoverageReport]) -> str:
    return json.dumps(summarize_coverage_reports(reports), sort_keys=True)
