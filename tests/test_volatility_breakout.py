"""Tests for VolatilityBreakout strategy."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent
from src.strategies.indicators import Candle
from src.strategies.volatility_breakout import VolatilityBreakout


def _mixed_candles(ts_start: int) -> list:
    """Wide range history, tight squeeze, then breakout bar."""
    out: list = []
    base = 100_000.0
    # 25 bars: moderate volatility
    for i in range(25):
        swing = 50.0 if i % 3 else 20.0
        out.append(
            Candle(
                open=base,
                high=base + swing,
                low=base - swing,
                close=base + (swing * 0.2),
                volume=1200.0,
                timestamp_ms=ts_start + i * 900_000,
            )
        )
    # 19 bars: tight squeeze
    for i in range(19):
        out.append(
            Candle(
                open=base,
                high=base + 1.0,
                low=base - 1.0,
                close=base + (0.05 if i % 2 else -0.05),
                volume=900.0,
                timestamp_ms=ts_start + (25 + i) * 900_000,
            )
        )
    # Breakout bar
    out.append(
        Candle(
            open=base + 2.0,
            high=base + 450.0,
            low=base,
            close=base + 420.0,
            volume=9000.0,
            timestamp_ms=ts_start + 44 * 900_000,
        )
    )
    return out


def test_no_signal_during_warmup() -> None:
    strat = VolatilityBreakout({"min_confidence": 0.50})
    event = MarketEvent(symbol="BTC", price=100_000.0, timestamp_ms=int(time.time() * 1000))
    assert strat.on_data(event) is None


def test_squeeze_breakout_long() -> None:
    strat = VolatilityBreakout({
        "squeeze_percentile": 30.0,
        "min_squeeze_bars": 2,
        "volume_surge": 1.1,
        "min_confidence": 0.50,
        "signal_throttle_ms": 0,
        "min_adx": 0,
        "max_adx": 100,
    })
    ts = int(time.time() * 1000) - 50 * 900_000
    candles = _mixed_candles(ts)

    state = strat._get_state("BTC")
    state.candles_15m.extend(candles)

    prior = list(candles[:-1])
    squeeze_ok, _, _, _, upper = strat._detect_squeeze(prior)
    assert squeeze_ok, "test fixture should produce squeeze on prior bars"
    assert upper is not None and candles[-1].close > upper

    event = MarketEvent(
        symbol="BTC",
        price=100_430.0,
        timestamp_ms=candles[-1].timestamp_ms + 60_000,
        adx_14=22.0,
        oi_delta=100.0,
        candle_15m=candles[-1],
    )
    sig = strat.on_data(event)
    assert sig is not None, "expected breakout signal after squeeze"
    assert sig.side == "long"
    assert sig.strategy == "VolatilityBreakout"
    assert sig.confidence >= 0.50


def test_factory_includes_volatility_breakout() -> None:
    from src.utils.config import Config
    from src.strategies.factory import build_sub_strategies

    cfg = Config({"strategy": {"volatility_breakout": {"enabled": True}}})
    names = [s.name for s in build_sub_strategies(cfg)]
    assert "VolatilityBreakout" in names


if __name__ == "__main__":
    test_no_signal_during_warmup()
    test_squeeze_breakout_long()
    test_factory_includes_volatility_breakout()
    print("VolatilityBreakout tests passed.")
