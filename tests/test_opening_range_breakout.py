"""Unit tests for OpeningRangeBreakout (research-only strategy)."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent, Position
from src.strategies.indicators import Candle
from src.strategies.opening_range_breakout import (
    OpeningRangeBreakout,
    SessionSpec,
    session_open_utc_ms,
)

pytestmark = pytest.mark.unit

_NY = ZoneInfo("America/New_York")
_INTERVAL_5M = 300_000


def _ny_open_ms(d: date) -> int:
    return session_open_utc_ms(
        SessionSpec(name="NY", tz_name="America/New_York", local_open="09:30"),
        d,
    )


def _c5(
    open_ms: int,
    *,
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 1000.0,
) -> Candle:
    return Candle(
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        timestamp_ms=open_ms + _INTERVAL_5M - 1,
    )


def _feed_history(strat: OpeningRangeBreakout, symbol: str, candles: list[Candle]) -> None:
    state = strat._get_state(symbol)
    for c in candles:
        state.candles_5m.append(c)


def _event(symbol: str, bar: Candle, price: float | None = None) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=price if price is not None else bar.close,
        timestamp_ms=bar.timestamp_ms,
        candle_5m=bar,
    )


def _warmup_before_session(open_ms: int, n: int = 25, base: float = 100.0, vol: float = 1000.0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n, 0, -1):
        oms = open_ms - i * _INTERVAL_5M
        out.append(_c5(oms, o=base, h=base + 0.2, l=base - 0.2, c=base, v=vol))
    return out


def _range_bars(open_ms: int, high: float, low: float, base: float = 100.0) -> list[Candle]:
    """Three 5m bars covering the first 15 minutes after session open."""
    mid = (high + low) / 2.0
    return [
        _c5(open_ms + 0 * _INTERVAL_5M, o=base, h=high, l=mid, c=mid, v=1000.0),
        _c5(open_ms + 1 * _INTERVAL_5M, o=mid, h=mid + 0.1, l=low, c=mid, v=1000.0),
        _c5(open_ms + 2 * _INTERVAL_5M, o=mid, h=high, l=low, c=base, v=1000.0),
    ]


def test_ny_open_dst_january_vs_july() -> None:
    jan = date(2025, 1, 15)
    jul = date(2025, 7, 15)
    jan_ms = _ny_open_ms(jan)
    jul_ms = _ny_open_ms(jul)

    jan_utc = datetime.fromtimestamp(jan_ms / 1000.0, tz=_NY)
    jul_utc_as_ny = datetime.fromtimestamp(jul_ms / 1000.0, tz=_NY)
    assert jan_utc.hour == 9 and jan_utc.minute == 30
    assert jul_utc_as_ny.hour == 9 and jul_utc_as_ny.minute == 30

    # EST (UTC-5) vs EDT (UTC-4) → UTC anchors differ by 1 hour
    jan_utc_h = datetime.fromtimestamp(jan_ms / 1000.0, tz=ZoneInfo("UTC")).hour
    jul_utc_h = datetime.fromtimestamp(jul_ms / 1000.0, tz=ZoneInfo("UTC")).hour
    assert abs(jan_utc_h - jul_utc_h) == 1


def test_range_forms_from_first_range_minutes() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.0)
    hist = _warmup_before_session(open_ms)
    # ~1.0% range — inside max_range_pct 1.5%
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    _feed_history(strat, "BTC", hist + rb)

    # Just after range closes — no breakout yet
    after_range = _c5(open_ms + 3 * _INTERVAL_5M, o=100.0, h=100.3, l=99.7, c=100.1, v=1000.0)
    _feed_history(strat, "BTC", [after_range])
    sig = strat.on_data(_event("BTC", after_range))
    assert sig is None

    day_state = strat._get_state("BTC").sessions[("NY", date(2025, 7, 15))]
    assert day_state.range_formed
    assert day_state.range_high == pytest.approx(100.5)
    assert day_state.range_low == pytest.approx(99.5)


def test_breakout_long_with_volume() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.5, min_stop_pct=0.0015)
    hist = _warmup_before_session(open_ms, vol=1000.0)
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    breakout = _c5(open_ms + 3 * _INTERVAL_5M, o=100.4, h=101.2, l=100.3, c=100.8, v=2000.0)
    _feed_history(strat, "BTC", hist + rb + [breakout])

    sig = strat.on_data(_event("BTC", breakout))
    assert sig is not None
    assert sig.side == "long"
    assert sig.strategy == "OpeningRangeBreakout"
    assert sig.entry_price == pytest.approx(100.8)
    # Stop at range-low → pct from entry
    expected_sl = max(0.0015, (100.8 - 99.5) / 100.8)
    assert sig.stop_loss_pct == pytest.approx(expected_sl)
    assert sig.take_profit_pct == pytest.approx(expected_sl * 2.0)
    assert sig.metadata["session"] == "NY"
    assert sig.metadata["range_high"] == pytest.approx(100.5)
    assert sig.metadata["range_low"] == pytest.approx(99.5)
    assert sig.metadata["volume_ratio"] >= 1.5


def test_breakout_short() -> None:
    open_ms = _ny_open_ms(date(2025, 1, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.5)
    hist = _warmup_before_session(open_ms)
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    breakout = _c5(open_ms + 4 * _INTERVAL_5M, o=99.6, h=99.7, l=98.8, c=99.2, v=2500.0)
    _feed_history(strat, "ETH", hist + rb + [breakout])

    sig = strat.on_data(_event("ETH", breakout))
    assert sig is not None
    assert sig.side == "short"
    expected_sl = max(0.0015, (100.5 - 99.2) / 99.2)
    assert sig.stop_loss_pct == pytest.approx(expected_sl)


def test_close_inside_range_no_signal() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.0)
    hist = _warmup_before_session(open_ms)
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    inside = _c5(open_ms + 3 * _INTERVAL_5M, o=100.0, h=100.4, l=99.6, c=100.1, v=5000.0)
    _feed_history(strat, "BTC", hist + rb + [inside])
    assert strat.on_data(_event("BTC", inside)) is None


def test_volume_below_threshold_no_signal() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.5)
    hist = _warmup_before_session(open_ms, vol=1000.0)
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    weak = _c5(open_ms + 3 * _INTERVAL_5M, o=100.4, h=101.2, l=100.3, c=100.8, v=1100.0)
    _feed_history(strat, "BTC", hist + rb + [weak])
    assert strat.on_data(_event("BTC", weak)) is None


def test_one_trade_per_session() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.5)
    hist = _warmup_before_session(open_ms)
    rb = _range_bars(open_ms, high=100.5, low=99.5)
    b1 = _c5(open_ms + 3 * _INTERVAL_5M, o=100.4, h=101.2, l=100.3, c=100.8, v=2000.0)
    _feed_history(strat, "BTC", hist + rb + [b1])
    assert strat.on_data(_event("BTC", b1)) is not None

    b2 = _c5(open_ms + 5 * _INTERVAL_5M, o=100.8, h=101.5, l=100.7, c=101.2, v=3000.0)
    _feed_history(strat, "BTC", [b2])
    assert strat.on_data(_event("BTC", b2)) is None


def test_new_session_next_day_trades_again() -> None:
    d1 = date(2025, 7, 15)
    d2 = date(2025, 7, 16)
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.5)

    for d in (d1, d2):
        open_ms = _ny_open_ms(d)
        hist = _warmup_before_session(open_ms)
        rb = _range_bars(open_ms, high=100.5, low=99.5)
        bo = _c5(open_ms + 3 * _INTERVAL_5M, o=100.4, h=101.2, l=100.3, c=100.8, v=2000.0)
        # Reset candle buffer per day for clean fixture (strategy keeps session state)
        state = strat._get_state("BTC")
        state.candles_5m.clear()
        _feed_history(strat, "BTC", hist + rb + [bo])
        sig = strat.on_data(_event("BTC", bo))
        assert sig is not None, f"expected signal on {d}"
        assert sig.metadata["session_date"] == d.isoformat()


def test_max_range_pct_skips() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(signal_throttle_ms=0, volume_mult=1.0, max_range_pct=0.015)
    hist = _warmup_before_session(open_ms)
    # ~3% range around 100 — exceeds 1.5%
    rb = _range_bars(open_ms, high=102.0, low=99.0)
    bo = _c5(open_ms + 3 * _INTERVAL_5M, o=101.0, h=103.0, l=100.9, c=102.5, v=5000.0)
    _feed_history(strat, "BTC", hist + rb + [bo])
    assert strat.on_data(_event("BTC", bo)) is None


def test_on_position_max_hold() -> None:
    strat = OpeningRangeBreakout(max_hold_hours=4.0)
    open_ms = _ny_open_ms(date(2025, 7, 15))
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=101.5,
        size=0.01,
        entry_time_ms=open_ms + 20 * 60_000,
        metadata={"session_open_ms": open_ms},
    )
    event = MarketEvent(
        symbol="BTC",
        price=102.0,
        timestamp_ms=pos.entry_time_ms + int(4.0 * 3_600_000),
    )
    exit_sig = strat.on_position(pos, event)
    assert exit_sig is not None
    assert exit_sig.reason == "orb_max_hold"


def test_on_position_session_flat() -> None:
    strat = OpeningRangeBreakout(session_flat_minutes=360, max_hold_hours=24.0)
    open_ms = _ny_open_ms(date(2025, 7, 15))
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=101.5,
        size=0.01,
        entry_time_ms=open_ms + 30 * 60_000,
        metadata={"session_open_ms": open_ms},
    )
    event = MarketEvent(
        symbol="BTC",
        price=102.0,
        timestamp_ms=open_ms + 360 * 60_000,
    )
    exit_sig = strat.on_position(pos, event)
    assert exit_sig is not None
    assert exit_sig.reason == "orb_session_flat"


def test_outside_entry_window_no_signal() -> None:
    open_ms = _ny_open_ms(date(2025, 7, 15))
    strat = OpeningRangeBreakout(
        signal_throttle_ms=0,
        volume_mult=1.0,
        entry_window_minutes=120,
    )
    hist = _warmup_before_session(open_ms)
    rb = _range_bars(open_ms, high=101.0, low=99.0)
    # 125 minutes after open → outside 120m entry window
    late_open = open_ms + 125 * 60_000
    late = _c5(late_open, o=100.5, h=102.0, l=100.4, c=101.5, v=5000.0)
    _feed_history(strat, "BTC", hist + rb + [late])
    assert strat.on_data(_event("BTC", late)) is None


def test_asia_session_disabled_by_default() -> None:
    strat = OpeningRangeBreakout()
    names = [s.name for s in strat._sessions]
    assert names == ["NY"]
