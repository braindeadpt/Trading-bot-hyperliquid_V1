"""Memoization neutrality + effectiveness — Task 4 (memory).

The per-tick shadow path recomputes candle-derived indicators on every
price tick for every shadow strategy. Task 4 memoizes them on the candle
set identity ``(len, last_timestamp_ms)`` — recomputation over the same
candles is bit-identical, so emitted signals must be unchanged.

Each ``test_*_signal_stream_unchanged`` replays a fixed event sequence and
compares against a golden stream captured on the PRE-memoization code
(``_mem_capture.py``, same builders). Each ``test_*_memo_*`` proves the
memo actually engages (indicator math runs once per candle set, not once
per tick).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent  # noqa: E402
from src.strategies.indicators import Candle  # noqa: E402
from src.strategies.checklist_meta import ChecklistMeta  # noqa: E402
from src.strategies.volatility_breakout import VolatilityBreakout  # noqa: E402
from src.strategies.liquidation_catcher import LiquidationCatcher  # noqa: E402
from src.strategies.funding_momentum import FundingMomentum  # noqa: E402

pytestmark = pytest.mark.unit

MS_15M = 900_000
MS_1H = 3_600_000
T0 = 1_700_000_000_000


def _sig_tuple(sig):
    if sig is None:
        return None
    return (
        sig.side,
        round(sig.confidence, 10),
        round(sig.size_pct or 0.0, 10),
        round(sig.stop_loss_pct or 0.0, 10),
        round(sig.take_profit_pct, 10) if sig.take_profit_pct is not None else None,
        sig.reason,
    )


def _candle(ts, o, h, l, c, v):
    return Candle(open=o, high=h, low=l, close=c, volume=v, timestamp_ms=ts)


def _uptrend(n, ts0, step, base=100.0, drift=0.4):
    out = []
    for i in range(n):
        b = base + drift * i
        out.append(_candle(ts0 + i * step, b, b + 1.0, b - 1.0, b + 0.3, 1000 + i))
    return out


def _stream_digest(stream):
    return hashlib.sha256(json.dumps(stream).encode()).hexdigest()[:16]


def _assert_stream(stream, digest, n_events, n_signals):
    n_sig = sum(1 for x in stream if x is not None)
    assert n_sig == n_signals, f"signal count {n_sig} != golden {n_signals}"
    got = _stream_digest(stream)
    assert got == digest, (
        f"signal stream diverged: digest {got} != golden {digest} "
        f"(first diffs: "
        f"{[i for i, s in enumerate(stream) if s is not None][:10]}...)"
    )


# --------------------------------------------------------------------------
# Event-sequence builders — MUST stay identical to _mem_capture.py
# --------------------------------------------------------------------------

def _checklist_events():
    candles = _uptrend(120, T0, MS_15M, base=100.0, drift=0.4)
    events = []
    for c in candles:
        base_ts = c.timestamp_ms + MS_15M
        for j, dp in enumerate((0.0, -2.0, +3.0, -0.5)):
            events.append(
                MarketEvent(
                    symbol="BTC",
                    price=c.close + dp,
                    timestamp_ms=base_ts + j * 30_000,
                    candle_15m=c,
                    adx_14=25.0,
                    rsi_14=50.0 + (j * 7) % 30,
                    oi_delta=10.0,
                    orderbook_oir=0.4,
                    liquidation_data_source="real",
                    liquidation_side_5m="short",
                )
            )
    return events


CHECKLIST_CFG = {
    "signal_throttle_ms": 60_000,
    "min_adx_gate": 0.0,
    "adx_trend_min": 20.0,
    "score_threshold": 1.5,
    "dominance_margin": 0.5,
    "require_oir_alignment": False,
    "sfp_lookback": 30,
    "sfp_max_age_bars": 20,
}
CHECKLIST_GOLDEN_SHA = "2be16c045c5cdf02"


def test_checklist_signal_stream_unchanged() -> None:
    s = ChecklistMeta(CHECKLIST_CFG)
    stream = [_sig_tuple(s.on_data(e)) for e in _checklist_events()]
    _assert_stream(stream, CHECKLIST_GOLDEN_SHA, n_events=480, n_signals=136)


def test_checklist_memo_reuses_candle_metrics() -> None:
    """Same 15m candle set across ticks -> indicator math runs once."""
    s = ChecklistMeta({**CHECKLIST_CFG, "signal_throttle_ms": 0})
    events = _checklist_events()
    for e in events[:208]:  # warm up through candle 51 (unpatched)
        s.on_data(e)
    warm = events[208:212]  # 4 ticks sharing candle_15m #52
    with patch.object(s, "_detect_sfp_side", wraps=s._detect_sfp_side) as spy:
        for e in warm:
            s.on_data(e)
    assert spy.call_count == 1, f"SFP ran {spy.call_count}x on unchanged candle set"
    with patch.object(s, "_detect_sfp_side", wraps=s._detect_sfp_side) as spy2:
        s.on_data(events[212])  # first tick of candle #53 -> key changes
    assert spy2.call_count == 1, "SFP must recompute on new candle"


# ---------------------------------------------------------- volatility

def _volbreakout_events():
    base = 100_000.0
    candles = []
    for i in range(25):
        swing = 50.0 if i % 3 else 20.0
        candles.append(
            _candle(T0 + i * MS_15M, base, base + swing, base - swing, base + swing * 0.2, 1200.0)
        )
    for i in range(19):
        candles.append(
            _candle(
                T0 + (25 + i) * MS_15M, base, base + 1.0, base - 1.0,
                base + (0.05 if i % 2 else -0.05), 900.0,
            )
        )
    candles.append(_candle(T0 + 44 * MS_15M, base + 2.0, base + 450.0, base, base + 420.0, 9000.0))
    candles.append(_candle(T0 + 45 * MS_15M, base + 420.0, base + 460.0, base + 380.0, base + 440.0, 8000.0))
    events = []
    for c in candles:
        for j, dp in enumerate((0.0, +30.0, +470.0, -10.0)):
            events.append(
                MarketEvent(
                    symbol="BTC",
                    price=c.close + dp,
                    timestamp_ms=c.timestamp_ms + MS_15M + j * 30_000,
                    candle_15m=c,
                    adx_14=20.0,
                )
            )
    return events


VB_CFG = {
    "signal_throttle_ms": 0,
    "require_trend_alignment": False,
    "min_adx": 0.0,
    "max_adx": 100.0,
    "volume_surge": 1.1,
    "squeeze_lookback": 20,
    "squeeze_percentile": 30.0,
    "min_squeeze_bars": 2,
    "min_confidence": 0.50,
}
VB_GOLDEN_SHA = "7003c531f0df558d"
VB_GOLDEN = {
    176: ("long", 0.95, 0.0145, 0.0007705222, 0.0015410445, "squeeze_breakout_long_bbw_p5"),
    177: ("long", 0.95, 0.0145, 0.0007702921, 0.0015405842, "squeeze_breakout_long_bbw_p5"),
    178: ("long", 0.95, 0.0145, 0.0007669327, 0.0015338654, "squeeze_breakout_long_bbw_p5"),
    179: ("long", 0.95, 0.0145, 0.000770599, 0.0015411979, "squeeze_breakout_long_bbw_p5"),
}


def test_volatility_breakout_signal_stream_unchanged() -> None:
    s = VolatilityBreakout(VB_CFG)
    stream = [_sig_tuple(s.on_data(e)) for e in _volbreakout_events()]
    for idx, expected in VB_GOLDEN.items():
        assert stream[idx] == expected, f"idx {idx}: {stream[idx]!r} != {expected!r}"
    _assert_stream(stream, VB_GOLDEN_SHA, n_events=184, n_signals=4)


def test_volatility_breakout_memo_reuses_squeeze() -> None:
    """_detect_squeeze is the O(n*BB) hot spot — once per candle set."""
    s = VolatilityBreakout(VB_CFG)
    events = _volbreakout_events()
    for e in events[:-8]:  # warm up through candle 43 (unpatched)
        s.on_data(e)
    with patch.object(s, "_detect_squeeze", wraps=s._detect_squeeze) as spy:
        for e in events[-8:]:  # last 2 candles x 4 ticks each
            s.on_data(e)
    # 4 ticks share the same candle -> squeeze runs once per candle set
    assert spy.call_count == 2, f"squeeze ran {spy.call_count}x over 2 candle sets"


# --------------------------------------------------------- liquidation

def _liqcatcher_events_and_strategy():
    cfg = {
        "signal_throttle_ms": 60_000,
        "min_notional_usd": 1_000_000,
        "min_liquidation_count": 3,
        "require_oi_decreasing": True,
        "oi_delta_max": 0.0,
        "max_adx": 80.0,
        "enabled": True,
        "require_real_liquidation_data": True,
        "confirmation_delay_ms": 0,
    }
    s = LiquidationCatcher(cfg)
    for c in _uptrend(40, T0, MS_1H, base=100.0, drift=0.2):
        s.on_candle(c, "BTC")
    events = []
    t = T0 + 40 * MS_1H
    for k in range(3):
        events.append(MarketEvent(
            symbol="BTC", price=100.0 - k, timestamp_ms=t + k * 90_000,
            liquidation_notional_5m=2_000_000 + k * 500_000,
            liquidation_side_5m="long", liquidation_count_5m=12,
            liquidation_data_source="real", oi_delta=-50.0, adx_14=20.0,
        ))
    for k in range(2):
        events.append(MarketEvent(
            symbol="BTC", price=99.0, timestamp_ms=t + 300_000 + k * 30_000,
            liquidation_notional_5m=100.0, liquidation_side_5m="long",
            liquidation_count_5m=1, liquidation_data_source="real",
            oi_delta=-5.0, adx_14=20.0,
        ))
    newc = _candle(t + MS_1H, 99.0, 99.5, 98.0, 98.5, 1100.0)
    s.on_candle(newc, "BTC")
    events.append(MarketEvent(
        symbol="BTC", price=98.0, timestamp_ms=t + MS_1H + 90_000,
        liquidation_notional_5m=3_000_000, liquidation_side_5m="short",
        liquidation_count_5m=20, liquidation_data_source="real",
        oi_delta=-80.0, adx_14=25.0,
    ))
    return s, events


LIQ_GOLDEN_SHA = "900d3faded32cb52"
LIQ_GOLDEN = {
    0: ("long", 0.85, 0.01, 0.0257857143, 0.0515714286, "liq_catch_long_$2M"),
    1: ("long", 0.85, 0.01, 0.026046176, 0.0520923521, "liq_catch_long_$2M"),
    2: ("long", 0.85, 0.01, 0.0263119534, 0.0526239067, "liq_catch_long_$3M"),
    5: ("short", 0.85, 0.01, 0.0263119534, 0.0526239067, "liq_catch_short_$3M"),
}


def test_liquidation_catcher_signal_stream_unchanged() -> None:
    s, events = _liqcatcher_events_and_strategy()
    stream = [_sig_tuple(s.on_data(e)) for e in events]
    for idx, expected in LIQ_GOLDEN.items():
        assert stream[idx] == expected, f"idx {idx}: {stream[idx]!r} != {expected!r}"
    _assert_stream(stream, LIQ_GOLDEN_SHA, n_events=6, n_signals=4)


def test_liquidation_catcher_memo_reuses_atr() -> None:
    """ATR(1h) recomputed only when the 1h candle set changes."""
    import src.strategies.indicators as ind

    cfg = {
        "signal_throttle_ms": 0,
        "min_notional_usd": 1_000_000,
        "min_liquidation_count": 3,
        "require_oi_decreasing": True,
        "oi_delta_max": 0.0,
        "max_adx": 80.0,
        "enabled": True,
        "require_real_liquidation_data": True,
        "confirmation_delay_ms": 0,
    }
    s = LiquidationCatcher(cfg)
    candles = _uptrend(40, T0, MS_1H, base=100.0, drift=0.2)
    for c in candles:
        s.on_candle(c, "BTC")
    t = T0 + 40 * MS_1H
    cascades = [
        MarketEvent(
            symbol="BTC", price=100.0 - k, timestamp_ms=t + k * 30_000,
            liquidation_notional_5m=2_000_000, liquidation_side_5m="long",
            liquidation_count_5m=12, liquidation_data_source="real",
            oi_delta=-50.0, adx_14=20.0,
        )
        for k in range(3)
    ]
    with patch(
        "src.strategies.liquidation_catcher.calculate_atr",
        wraps=ind.calculate_atr,
    ) as spy:
        for e in cascades:  # 3 cascade ticks, same 1h candle set
            s.on_data(e)
    assert spy.call_count == 1, f"ATR recomputed {spy.call_count}x on same candle set"
    s.on_candle(_candle(t + MS_1H, 99.0, 99.5, 98.0, 98.5, 1100.0), "BTC")
    post_candle_tick = MarketEvent(
        symbol="BTC", price=99.0, timestamp_ms=t + 300_000,
        liquidation_notional_5m=2_000_000, liquidation_side_5m="long",
        liquidation_count_5m=12, liquidation_data_source="real",
        oi_delta=-50.0, adx_14=20.0,
    )
    with patch(
        "src.strategies.liquidation_catcher.calculate_atr",
        wraps=ind.calculate_atr,
    ) as spy2:
        s.on_data(post_candle_tick)  # same shape, new 1h candle set
    assert spy2.call_count == 1, "ATR must recompute on new 1h candle"


# ------------------------------------------------------------ funding

def _fundingmom_events():
    candles = _uptrend(60, T0, MS_1H, base=100.0, drift=0.5)
    events = []
    for i, c in enumerate(candles):
        funding = -0.001 if i < 55 else 0.001
        events.append(MarketEvent(
            symbol="BTC", price=c.close, timestamp_ms=c.timestamp_ms + 60_000,
            candle_1h=c, predicted_funding=funding, oi_delta=25.0, adx_14=25.0,
        ))
        events.append(MarketEvent(
            symbol="BTC", price=c.close + 0.4, timestamp_ms=c.timestamp_ms + 90_000,
            candle_1h=c, predicted_funding=funding, oi_delta=25.0, adx_14=25.0,
        ))
    return events


FM_CFG = {
    "signal_throttle_ms": 60_000,
    "ema_slow": 50,
    "min_adx": 10.0,
    "funding_flip_threshold": 0.0001,
    "oi_lookback_hours": 4,
    "enabled": True,
}
FM_GOLDEN_SHA = "f9cb19f1c021ddb0"
FM_GOLDEN = {
    110: ("long", 0.7625, 0.015, 0.0312989045, 0.0469483568, "funding_momentum_long_from_short_f0.00100"),
}


def test_funding_momentum_signal_stream_unchanged() -> None:
    s = FundingMomentum(FM_CFG)
    stream = [_sig_tuple(s.on_data(e)) for e in _fundingmom_events()]
    for idx, expected in FM_GOLDEN.items():
        assert stream[idx] == expected, f"idx {idx}: {stream[idx]!r} != {expected!r}"
    _assert_stream(stream, FM_GOLDEN_SHA, n_events=120, n_signals=1)


def test_funding_momentum_memo_reuses_ema_atr() -> None:
    """EMA50/ATR on 1h candles computed once per candle set, not per tick.

    Funding alternates sign every tick so each tick reaches the indicator
    block; the candle set stays fixed, so the memo must serve ticks 2+.
    """
    import src.strategies.funding_momentum as fm_mod
    import src.strategies.indicators as ind

    s = FundingMomentum({**FM_CFG, "signal_throttle_ms": 0})
    candles = _uptrend(60, T0, MS_1H, base=100.0, drift=0.5)

    def ev(candle, k):
        return MarketEvent(
            symbol="BTC", price=candle.close,
            timestamp_ms=candle.timestamp_ms + 60_000 + k * 10_000,
            candle_1h=candle,
            predicted_funding=(-0.001 if k % 2 == 0 else 0.001),
            oi_delta=25.0, adx_14=25.0,
        )

    for i, c in enumerate(candles[:-1]):  # warm: one flip event per candle
        s.on_data(ev(c, i))
    last = candles[-1]
    events = [ev(last, 59 + k) for k in range(4)]  # 4 ticks, same 1h set, alternating flips
    with patch.object(fm_mod, "calculate_ema", wraps=ind.calculate_ema) as ema_spy, \
         patch.object(fm_mod, "calculate_atr", wraps=ind.calculate_atr) as atr_spy:
        for e in events:
            s.on_data(e)
    assert ema_spy.call_count <= 1, f"EMA recomputed {ema_spy.call_count}x on same set"
    assert atr_spy.call_count <= 1, f"ATR recomputed {atr_spy.call_count}x on same set"


if __name__ == "__main__":
    test_checklist_signal_stream_unchanged()
    test_volatility_breakout_signal_stream_unchanged()
    test_liquidation_catcher_signal_stream_unchanged()
    test_funding_momentum_signal_stream_unchanged()
    print("golden streams OK")
