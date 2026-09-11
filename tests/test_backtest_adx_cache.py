"""Regression test: ADX computed once per NEW 15m candle, not per 1m event.

Profile 2026-09-11 (14d backtest, 62s wall): ``calculate_adx`` was 17.6s
tottime / 26.3s cumtime — 89,693 calls, ~14/15 redundant because
``hist_15m`` only changes when a 15m candle closes. The per-symbol cache
keyed on ``(len, last timestamp_ms)`` removes the redundant recomputation.
"""
from __future__ import annotations

import pytest

import src.backtest.engine as bt_engine
from src.backtest.engine import BacktestEngine
from src.strategies.indicators import Candle

pytestmark = pytest.mark.unit


def _candle(ts: int) -> Candle:
    return Candle(open=100.0, high=101.0, low=99.0, close=100.5,
                  volume=10.0, timestamp_ms=ts)


def _engine() -> BacktestEngine:
    eng = object.__new__(BacktestEngine)
    eng._adx_cache = {}
    return eng


def test_adx_recomputed_only_when_15m_history_changes(monkeypatch):
    eng = _engine()
    calls: list[int] = []

    def fake_adx(candles, period=14):
        calls.append(len(candles))
        return 42.0

    monkeypatch.setattr(bt_engine, "calculate_adx", fake_adx)

    hist = [_candle(1_000_000 + i * 900_000) for i in range(30)]

    # 15 consecutive 1m events between 15m closes -> single computation.
    for _ in range(15):
        assert eng._adx_for("BTC", hist) == 42.0
    assert calls == [30]

    # A new 15m candle lands -> exactly one recompute.
    hist.append(_candle(1_000_000 + 30 * 900_000))
    assert eng._adx_for("BTC", hist) == 42.0
    assert calls == [30, 31]

    # 1m events again -> no recompute until next 15m close.
    for _ in range(14):
        eng._adx_for("BTC", hist)
    assert len(calls) == 2


def test_adx_cache_is_per_symbol(monkeypatch):
    eng = _engine()
    calls: list[int] = []
    monkeypatch.setattr(
        bt_engine, "calculate_adx",
        lambda candles, period=14: calls.append(len(candles)) or 7.0,
    )
    hist = [_candle(2_000_000 + i * 900_000) for i in range(29)]
    eng._adx_for("BTC", hist)
    eng._adx_for("ETH", list(hist))  # same content, different symbol
    assert calls == [29, 29]


def test_adx_for_below_min_history_returns_none():
    eng = _engine()
    short = [_candle(i * 900_000) for i in range(28)]  # needs >= 29
    assert eng._adx_for("BTC", short) is None


def test_ind_candle_converted_once_per_timestamp():
    """5m/15m/1h candle objects are rebuilt once per close, not per event."""
    from types import SimpleNamespace
    eng = object.__new__(BacktestEngine)
    data: dict = {}
    db_candle = SimpleNamespace(
        open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0,
        timestamp_ms=1234, oi_total=None, buy_volume=None,
        sell_volume=None, trade_count=0,
    )
    first = eng._ind_candle(data, "15m", db_candle)
    second = eng._ind_candle(data, "15m", db_candle)
    assert first is second  # same object — no reconstruction
    db_candle2 = SimpleNamespace(**{**vars(db_candle), "timestamp_ms": 5678})
    third = eng._ind_candle(data, "15m", db_candle2)
    assert third is not first
    assert third.timestamp_ms == 5678


def test_adx_cache_matches_batch_value(monkeypatch):
    """Cache returns exactly what a fresh batch call would return."""
    import math
    eng = _engine()
    real = bt_engine.calculate_adx
    hist = [_candle(500_000 + i * 900_000) for i in range(40)]
    expected = real(hist, 14)
    assert eng._adx_for("BTC", hist) == expected
    eng._adx_for("BTC", hist)
    hist.append(_candle(500_000 + 40 * 900_000))
    again = eng._adx_for("BTC", hist)
    assert again == real(hist, 14) or (again is None and expected is None) \
        or math.isclose(again or -1, real(hist, 14) or -1)
