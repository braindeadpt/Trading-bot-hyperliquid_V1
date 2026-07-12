"""GoldRush parity tick formatting, rollup, and gap segmentation tests."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.continuous_segments import (
    DEFAULT_GAP_INTERVALS,
    DEFAULT_RESEARCH_GAP_MS,
    gap_threshold_ms,
    is_cross_gap,
    resolve_gap_ms,
    segment_timeline,
)
from src.data.candle_providers.candle_rollup import rollup_1m_to_interval
from src.data.candle_providers.parity import compare_candle_overlap
from src.data.candle_providers.parity_secondary import safe_relative_delta
from src.data.candle_providers.tick_meta import (
    dynamic_quantum_for_price,
    format_hl_price,
    price_match_ticks,
    tick_size_for,
)
import pytest

pytestmark = pytest.mark.unit


def _row(symbol: str, interval: str, open_ms: int, **prices: str) -> dict:
    gap = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}[interval]
    base = {
        "s": symbol,
        "i": interval,
        "t": open_ms,
        "T": open_ms + gap - 1,
        "o": "100.0",
        "h": "101.0",
        "l": "99.0",
        "c": "100.5",
        "v": "1.0",
        "n": 1,
    }
    base.update(prices)
    return base


def _row_1m(open_ms: int, **kw: str) -> dict:
    return _row("BTC", "1m", open_ms, **kw)


def test_format_hl_price_btc_one_decimal() -> None:
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    assert format_hl_price("64023.0", "BTC", meta) == 64023.0
    assert format_hl_price("64023.55", "BTC", meta) == 64024.0


def test_dynamic_quantum_btc_60k() -> None:
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    assert dynamic_quantum_for_price(64023.0, "BTC", meta) == 1.0
    assert tick_size_for("BTC", meta, reference_price=64023.0) == 1.0


def test_price_match_within_one_tick() -> None:
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    identical, within, _ = price_match_ticks("BTC", "100.0", "100.1", meta)
    assert not identical
    assert within


def test_price_match_btc_one_dollar_is_one_tick() -> None:
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    identical, within, delta_ticks = price_match_ticks(
        "BTC", "60105.0", "60106.0", meta,
    )
    assert not identical
    assert within
    assert abs(delta_ticks - 1.0) < 1e-6


def test_parity_passes_with_tick_tolerance() -> None:
    ts = 1_700_000_000_000
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    official = [_row("BTC", "1h", ts, c="100.0")]
    goldrush = [_row("BTC", "1h", ts, c="100.1")]
    report = compare_candle_overlap(
        official, goldrush, symbol="BTC", interval="1h", meta_cache=meta,
    )
    assert report.passed


def test_parity_fails_large_tick_delta() -> None:
    ts = 1_700_000_000_000
    meta = {"BTC": {"sz_decimals": 5, "px_decimals": 1}}
    official = [_row("BTC", "1h", ts, c="60105.0")]
    goldrush = [_row("BTC", "1h", ts, c="60108.0")]
    report = compare_candle_overlap(
        official, goldrush, symbol="BTC", interval="1h", meta_cache=meta,
    )
    assert not report.passed


def test_safe_relative_delta_near_zero() -> None:
    assert safe_relative_delta(0.0, 0.0) == 0.0
    assert safe_relative_delta(0.0, 1e-9) == 1e-9 / 1e-8


def test_rollup_1m_to_5m() -> None:
    base = (1_700_000_000_000 // 300_000) * 300_000
    rows = [
        _row_1m(base + i * 60_000, o=str(100 + i), h=str(101 + i), l="99", c=str(100.5 + i), v="1", n="1")
        for i in range(5)
    ]
    rolled = rollup_1m_to_interval(rows, "5m", symbol="BTC")
    assert len(rolled) == 1
    assert rolled[0]["t"] == base
    assert rolled[0]["T"] == base + 300_000 - 1
    assert rolled[0]["n"] == 5


def test_segment_timeline_splits_two_interval_gap() -> None:
    gap = gap_threshold_ms("1m", DEFAULT_GAP_INTERVALS)
    assert gap == DEFAULT_RESEARCH_GAP_MS
    ts = [0, 60_000, 120_000, 120_000 + gap + 60_000]
    segments = segment_timeline(ts, max_gap_ms=gap)
    assert len(segments) == 2
    assert segments[0].bar_count == 3


def test_resolve_gap_ms_per_tf() -> None:
    assert resolve_gap_ms("5m", gap_intervals=2) == 600_000
    assert resolve_gap_ms("1m", gap_intervals_by_tf={"1m": 3}) == 180_000


def test_is_cross_gap() -> None:
    gap = DEFAULT_RESEARCH_GAP_MS
    assert is_cross_gap(0, 60_000, max_gap_ms=gap) is False
    assert is_cross_gap(0, gap + 1, max_gap_ms=gap)


if __name__ == "__main__":
    test_format_hl_price_btc_one_decimal()
    print("  tick format OK")
    test_dynamic_quantum_btc_60k()
    print("  dynamic quantum OK")
    test_price_match_within_one_tick()
    print("  tick match OK")
    test_price_match_btc_one_dollar_is_one_tick()
    print("  btc 1-dollar tick OK")
    test_parity_passes_with_tick_tolerance()
    print("  parity tick pass OK")
    test_parity_fails_large_tick_delta()
    print("  parity tick fail OK")
    test_safe_relative_delta_near_zero()
    print("  rel delta OK")
    test_rollup_1m_to_5m()
    print("  rollup OK")
    test_segment_timeline_splits_two_interval_gap()
    print("  gap segments OK")
    test_resolve_gap_ms_per_tf()
    print("  resolve gap OK")
    test_is_cross_gap()
    print("  cross gap OK")
    print("test_goldrush_parity_tick: all passed")
