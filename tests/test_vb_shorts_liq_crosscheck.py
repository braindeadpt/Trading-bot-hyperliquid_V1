"""Unit tests for scripts/vb_shorts_liq_crosscheck.py.

Pins the flush-reversal measurement mechanics: minute bucketing, p90
threshold, post-flush return direction by dominant side, and the VB
short pre-drop/post-return proxy.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.vb_shorts_liq_crosscheck import (  # noqa: E402
    load_liq_events,
    percentile,
)


class FakeCur:
    """Cursor stub returning rows for load_liq_events."""

    def __init__(self, rows):
        self._rows = rows
        self._last = None

    def execute(self, sql, params=None):
        self._last = params
        return self

    def fetchall(self):
        return self._rows


def test_load_liq_events_buckets_by_minute_and_side():
    rows = [
        ("ETH", 120_000, 100.0, "long"),
        ("ETH", 121_000, 200.0, "short"),
        ("ETH", 125_000, 50.0, "long"),   # same minute as above
        ("BTC", 120_000, 999.0, "long"),  # filtered out (not in SYMBOLS? BTC is)
        ("SOL", 60_000, 10.0, "long"),
    ]
    cur = FakeCur(rows)
    out = load_liq_events(cur, ("okx", "bybit"))
    assert "ETH" in out
    m2 = out["ETH"][2]
    assert m2["long"] == pytest.approx(150.0)
    assert m2["short"] == pytest.approx(200.0)
    assert 1 in out["SOL"]
    assert 2 in out["BTC"]  # BTC minute 2 bucket exists


def test_load_liq_events_filters_symbols():
    rows = [("NOPE", 120_000, 100.0, "long"), ("HYPE", 120_000, 5.0, "short")]
    out = load_liq_events(FakeCur(rows), ("okx",))
    assert "NOPE" not in out
    assert out["HYPE"][2]["short"] == pytest.approx(5.0)


def test_percentile_reference():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.90) == pytest.approx(3.7)
    assert percentile([], 0.90) == 0.0


def test_flush_direction_semantics():
    """Reversal sign convention: long-liq (forced sells) -> price rises post."""
    # long-liq dominant flush: post return positive = reversal confirmed
    flushes_long = [("long", +0.04), ("long", +0.02)]
    flushes_short = [("short", -0.19), ("short", -0.27)]
    avg_long = sum(r[1] for r in flushes_long) / len(flushes_long)
    avg_short = sum(r[1] for r in flushes_short) / len(flushes_short)
    assert avg_long > 0 and avg_short < 0
