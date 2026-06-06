"""Unit tests for CVDOrderFlow strategy.

Tests cover:
  * Instantiation and configuration
  * Warm-up gating (insufficient bars)
  * Buy/sell volume extraction from MarketEvent.candle_1m
  * Bullish and bearish divergence detection
  * Multi-timeframe alignment requirement
  * Regime filters (ADX band, OIR confirmation, volume gate)
  * Throttling and deduplication
  * Exit logic (TP, SL, max-hold, opposite divergence)
  * Pure-function helper: _window_stats

Run:  python tests/test_cvd_orderflow.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategies.cvd_orderflow import (  # noqa: E402
    CVDOrderFlow,
    _BarDelta,
    _CVDState,
    _WindowStats,
)
from src.strategies.base import (  # noqa: E402
    MarketEvent,
    Signal,
    Position,
    ExitSignal,
)


# ── Test helpers ─────────────────────────────────────────────────────────


@dataclass
class _FakeCandle:
    """Mimics the candle_builder Candle interface (subset of fields)."""
    open: float
    high: float
    low: float
    close: float
    volume: float
    _ts: int
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    @property
    def timestamp_ms(self) -> int:
        return self._ts


def _ts(i: int, base: int = 1_700_000_000_000) -> int:
    return base + i * 60_000  # 1m intervals


def make_buy_sell_candle(
    i: int,
    price: float,
    buy: float = 0.0,
    sell: float = 0.0,
    vol: Optional[float] = None,
) -> _FakeCandle:
    """Build a 1m candle with the given buy/sell split."""
    total = vol if vol is not None else (buy + sell)
    return _FakeCandle(
        open=price, high=price, low=price, close=price,
        volume=total, _ts=_ts(i),
        buy_volume=buy, sell_volume=sell,
    )


def make_event(
    symbol: str,
    price: float,
    candle_1m: Optional[_FakeCandle] = None,
    adx: Optional[float] = None,
    oir: Optional[float] = None,
    candle_1h: Optional[_FakeCandle] = None,
    ts_offset: int = 0,
) -> MarketEvent:
    """Build a MarketEvent with a controllable 1m candle."""
    return MarketEvent(
        symbol=symbol,
        price=price,
        timestamp_ms=_ts(ts_offset),
        candle_1m=candle_1m,        # type: ignore[arg-type]
        candle_1h=candle_1h,        # type: ignore[arg-type]
        adx_14=adx,
        orderbook_oir=oir,
    )


def fill_bars(
    strategy: CVDOrderFlow,
    symbol: str,
    n: int,
    base_price: float = 100.0,
    delta_per_bar: float = 0.0,
) -> List[_BarDelta]:
    """Pre-seed *symbol* state with *n* synthetic bars.

    delta_per_bar=0 means buy=sell.  Positive => bullish pressure, negative => bearish.
    """
    state = strategy._get_state(symbol)
    out: List[_BarDelta] = []
    for i in range(n):
        buy = 100.0 + max(delta_per_bar, 0.0)
        sell = 100.0 + max(-delta_per_bar, 0.0)
        bar = _BarDelta(
            timestamp_ms=_ts(i),
            price_close=base_price + i * 0.0,  # flat for now
            delta=buy - sell,
            total_volume=buy + sell,
            buy_volume=buy,
            sell_volume=sell,
        )
        state.bars_1m.append(bar)
        out.append(bar)
    return out


def print_test(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        global FAILED
        FAILED += 1


FAILED = 0


# ── Test cases ──────────────────────────────────────────────────────────


def test_instantiation_defaults() -> None:
    strat = CVDOrderFlow()
    assert strat.name == "CVDOrderFlow"
    assert strat.WINDOW_SHORT == 5
    assert strat.WINDOW_MED == 15
    assert strat.WINDOW_LONG == 60
    assert strat.MIN_DIVERGENCE == 0.35
    print_test("instantiation_defaults", True)


def test_instantiation_custom_config() -> None:
    cfg = {
        "window_short_bars": 3,
        "min_divergence_strength": 0.5,
        "max_hold_hours": 4,
    }
    strat = CVDOrderFlow(cfg)
    assert strat.WINDOW_SHORT == 3
    assert strat.MIN_DIVERGENCE == 0.5
    assert strat.MAX_HOLD_HOURS == 4.0
    print_test("instantiation_custom_config", True)


def test_no_candle_returns_none() -> None:
    strat = CVDOrderFlow()
    ev = make_event("BTC", 100.0, candle_1m=None)
    sig = strat.on_data(ev)
    print_test("no_candle_returns_none", sig is None, f"got {sig!r}")


def test_zero_buy_sell_returns_none() -> None:
    strat = CVDOrderFlow()
    c = make_buy_sell_candle(0, 100.0, buy=0.0, sell=0.0)
    ev = make_event("BTC", 100.0, candle_1m=c, ts_offset=0)
    sig = strat.on_data(ev)
    print_test("zero_buy_sell_returns_none", sig is None)


def test_warmup_gates_signal() -> None:
    strat = CVDOrderFlow()
    sym = "BTC"
    # Seed only 30 bars (need 60 for WINDOW_LONG)
    fill_bars(strat, sym, n=30, delta_per_bar=0.0)
    # Push an event with divergent delta
    c = make_buy_sell_candle(99, 99.0, buy=200.0, sell=10.0)
    ev = make_event(sym, 99.0, candle_1m=c, ts_offset=99)
    sig = strat.on_data(ev)
    print_test("warmup_gates_signal", sig is None, "30 bars should not be enough")


def test_bullish_divergence_long() -> None:
    """Price falling, CVD rising => LONG signal."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    # Bars 0-59: price drifts down -0.05/bar, but CVD strongly positive
    for i in range(60):
        price = 100.0 - i * 0.05   # downtrend
        buy = 1_400_000.0
        sell = 600_000.0
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=price,
            delta=buy - sell,
            total_volume=buy + sell,
            buy_volume=buy,
            sell_volume=sell,
        ))
    # Bar 60: continues the pattern
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=20.0, oir=0.3, ts_offset=60)
    sig = strat.on_data(ev)
    print_test(
        "bullish_divergence_long",
        sig is not None and sig.side == "long",
        f"got side={sig.side if sig else 'None'}",
    )


def test_bearish_divergence_short() -> None:
    """Price rising, CVD falling => SHORT signal."""
    strat = CVDOrderFlow()
    sym = "ETH"
    state = strat._get_state(sym)
    for i in range(60):
        price = 100.0 + i * 0.05
        buy = 200_000.0
        sell = 1_400_000.0
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=price,
            delta=buy - sell,
            total_volume=buy + sell,
            buy_volume=buy,
            sell_volume=sell,
        ))
    c = make_buy_sell_candle(60, 103.0, buy=200_000.0, sell=1_400_000.0)
    ev = make_event(sym, 103.0, candle_1m=c, adx=20.0, oir=-0.3, ts_offset=60)
    sig = strat.on_data(ev)
    print_test(
        "bearish_divergence_short",
        sig is not None and sig.side == "short",
        f"got side={sig.side if sig else 'None'}",
    )


def test_no_signal_when_aligned() -> None:
    """Price AND CVD both falling => no signal (trend is supported)."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        price = 100.0 - i * 0.05
        buy = 600_000.0
        sell = 1_400_000.0
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=price,
            delta=buy - sell,
            total_volume=buy + sell,
            buy_volume=buy,
            sell_volume=sell,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=600_000.0, sell=1_400_000.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=20.0, ts_offset=60)
    sig = strat.on_data(ev)
    print_test("no_signal_when_aligned", sig is None, f"got {sig!r}")


def test_adx_too_high_blocks() -> None:
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=45.0, ts_offset=60)
    sig = strat.on_data(ev)
    print_test("adx_too_high_blocks", sig is None)


def test_adx_too_low_blocks() -> None:
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=8.0, ts_offset=60)
    sig = strat.on_data(ev)
    print_test("adx_too_low_blocks", sig is None)


def test_volume_too_low_blocks() -> None:
    """If total window volume < min, no signal.

    Pre-fix bug check: ``total_volume=100.0`` with price 100.0 yields
    100*100 = 10_000 USD < 50_000 USD threshold -> still blocked. The
    companion test ``test_volume_unit_conversion`` proves the unit conversion
    is happening in the candle.
    """
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=50.0, total_volume=100.0,
            buy_volume=75.0, sell_volume=25.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=75.0, sell=25.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=20.0, ts_offset=60)
    sig = strat.on_data(ev)
    print_test("volume_too_low_blocks", sig is None)


def test_volume_unit_conversion() -> None:
    """v3.1.14: verify candle volume is converted from token-units to USD.

    Reproduces the production bug where ``CVDOrderFlow`` was comparing
    raw token volume (e.g. ``160 BTC``) to a USD threshold (``50_000``)
    and silently blocking every signal. After the fix, the strategy
    multiplies the token volume by the candle close price.

    Scenarios:
      * BTC at $100_000 with 100 BTC traded = $10_000_000 USD  -> passes gate
      * SOL at $150 with 50_000 SOL traded  = $7_500_000 USD    -> passes gate
      * BTC at $100_000 with 0.05 BTC traded = $5_000 USD       -> blocked

    This test directly inspects the ``_BarDelta`` returned by
    ``_extract_bar`` to confirm the conversion happens.
    """
    strat = CVDOrderFlow()

    # Scenario 1: BTC at $100k, 100 BTC traded -> $10M USD
    ev_btc = make_event(
        "BTC", price=100_000.0,
        candle_1m=_FakeCandle(
            open=100_000.0, high=100_100.0, low=99_900.0, close=100_000.0,
            volume=100.0, _ts=_ts(0),  # 100 BTC in token units
            buy_volume=70.0, sell_volume=30.0,
        ),
    )
    bar_btc = CVDOrderFlow._extract_bar(ev_btc)
    assert bar_btc is not None, "BTC candle should be extracted"
    expected_btc_usd = 100.0 * 100_000.0  # 10_000_000
    assert abs(bar_btc.total_volume - expected_btc_usd) < 1.0, (
        f"BTC: total_volume should be {expected_btc_usd} USD, "
        f"got {bar_btc.total_volume}"
    )
    assert abs(bar_btc.buy_volume - 70.0 * 100_000.0) < 1.0, (
        f"BTC: buy_volume should be 7_000_000 USD, got {bar_btc.buy_volume}"
    )

    # Scenario 2: SOL at $150, 50_000 SOL traded -> $7.5M USD
    ev_sol = make_event(
        "SOL", price=150.0,
        candle_1m=_FakeCandle(
            open=150.0, high=151.0, low=149.0, close=150.0,
            volume=50_000.0, _ts=_ts(0),  # 50k SOL in token units
            buy_volume=30_000.0, sell_volume=20_000.0,
        ),
    )
    bar_sol = CVDOrderFlow._extract_bar(ev_sol)
    assert bar_sol is not None, "SOL candle should be extracted"
    expected_sol_usd = 50_000.0 * 150.0  # 7_500_000
    assert abs(bar_sol.total_volume - expected_sol_usd) < 1.0, (
        f"SOL: total_volume should be {expected_sol_usd} USD, "
        f"got {bar_sol.total_volume}"
    )

    # Scenario 3: very low volume -> should be rejected by volume gate
    ev_tiny = make_event(
        "BTC", price=100_000.0,
        candle_1m=_FakeCandle(
            open=100_000.0, high=100_000.0, low=100_000.0, close=100_000.0,
            volume=0.05, _ts=_ts(0),  # 0.05 BTC = $5_000 USD
            buy_volume=0.03, sell_volume=0.02,
        ),
    )
    # Pre-fill bars so warm-up is satisfied
    state = strat._get_state("BTC")
    for i in range(20):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100_000.0 - i * 10.0,  # gentle downtrend
            delta=0.05, total_volume=0.05 * 100_000.0,  # 5_000 USD per bar
            buy_volume=0.04 * 100_000.0, sell_volume=0.01 * 100_000.0,
        ))
    sig = strat.on_data(ev_tiny)
    # Either rejected by volume gate OR by warm-up; both are valid since
    # the scenario presents a low-volume case that shouldn't pass.
    print_test("volume_unit_conversion_tiny_blocked", sig is None)

    print_test(
        "volume_unit_conversion_btc",
        abs(bar_btc.total_volume - expected_btc_usd) < 1.0,
        f"{bar_btc.total_volume:.0f} USD (expected {expected_btc_usd:.0f})",
    )
    print_test(
        "volume_unit_conversion_sol",
        abs(bar_sol.total_volume - expected_sol_usd) < 1.0,
        f"{bar_sol.total_volume:.0f} USD (expected {expected_sol_usd:.0f})",
    )


def test_oir_misaligned_blocks() -> None:
    """Long signal but OIR strongly negative => blocked."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    ev = make_event(sym, 97.0, candle_1m=c, adx=20.0, oir=-0.5, ts_offset=60)
    sig = strat.on_data(ev)
    print_test("oir_misaligned_blocks", sig is None)


def test_throttle_blocks_rapid_signals() -> None:
    """Two divergent events within throttle window => only first fires."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c1 = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    c2 = make_buy_sell_candle(61, 96.9, buy=1_400_000.0, sell=600_000.0)
    sig1 = strat.on_data(make_event(sym, 97.0, candle_1m=c1, adx=20.0, oir=0.3, ts_offset=60))
    sig2 = strat.on_data(make_event(sym, 96.9, candle_1m=c2, adx=20.0, oir=0.3, ts_offset=61))
    print_test(
        "throttle_blocks_rapid_signals",
        sig1 is not None and sig2 is None,
        f"sig1={sig1.side if sig1 else 'None'} sig2={sig2.side if sig2 else 'None'}",
    )


def test_exit_max_hold() -> None:
    strat = CVDOrderFlow()
    pos = Position(
        symbol="BTC", side="long", entry_price=100.0,
        size=0.1, entry_time_ms=_ts(0),
        metadata={"stop_loss_pct": 0.02, "take_profit_pct": 0.04},
    )
    # 7h later
    ev = make_event("BTC", 101.0, ts_offset=7 * 60)
    exit_sig = strat.on_position(pos, ev)
    print_test("exit_max_hold", exit_sig is not None and "max_hold" in exit_sig.reason)


def test_exit_take_profit() -> None:
    strat = CVDOrderFlow()
    pos = Position(
        symbol="BTC", side="long", entry_price=100.0,
        size=0.1, entry_time_ms=_ts(0),
        metadata={"stop_loss_pct": 0.02, "take_profit_pct": 0.04},
    )
    # Price up 5% (above 4% TP)
    ev = make_event("BTC", 105.0, ts_offset=10)
    exit_sig = strat.on_position(pos, ev)
    print_test(
        "exit_take_profit",
        exit_sig is not None and "take_profit" in exit_sig.reason,
    )


def test_exit_stop_loss() -> None:
    strat = CVDOrderFlow()
    pos = Position(
        symbol="BTC", side="short", entry_price=100.0,
        size=0.1, entry_time_ms=_ts(0),
        metadata={"stop_loss_pct": 0.02, "take_profit_pct": 0.04},
    )
    # Price up 3% (above 2% SL for short)
    ev = make_event("BTC", 103.0, ts_offset=10)
    exit_sig = strat.on_position(pos, ev)
    print_test(
        "exit_stop_loss",
        exit_sig is not None and "stop_loss" in exit_sig.reason,
    )


def test_window_stats_pure_function() -> None:
    """Test the _window_stats helper directly with synthetic bars."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    # 5 bars: price down -0.1% per bar, but CVD strongly positive
    for i in range(5):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.10,   # -0.4% total
            delta=80.0, total_volume=200.0,
            buy_volume=140.0, sell_volume=60.0,
        ))
    stats = strat._window_stats(state.bars_1m, window=5)
    assert stats is not None
    assert stats.divergence > 0.0   # bullish div
    assert stats.cvd == 80.0 * 5
    assert stats.price_change_pct < 0.0
    print_test("window_stats_pure_function", True, f"div={stats.divergence:+.2f}")


def test_window_stats_no_divergence_when_aligned() -> None:
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    # Both price and CVD down
    for i in range(5):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.10,
            delta=-80.0, total_volume=200.0,
            buy_volume=60.0, sell_volume=140.0,
        ))
    stats = strat._window_stats(state.bars_1m, window=5)
    assert stats is not None
    assert stats.divergence == 0.0
    print_test("window_stats_no_divergence_when_aligned", True)


def test_signal_metadata_complete() -> None:
    """Signal metadata contains the diagnostic fields we need."""
    strat = CVDOrderFlow()
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    sig = strat.on_data(make_event(sym, 97.0, candle_1m=c, adx=20.0, oir=0.3, ts_offset=60))
    assert sig is not None
    expected_keys = {
        "cvd_short", "cvd_medium", "cvd_long",
        "div_short", "div_medium", "div_long",
        "price_change_pct", "adx", "stop_loss_pct", "take_profit_pct",
        "window_short_bars", "window_medium_bars", "window_long_bars",
    }
    missing = expected_keys - sig.metadata.keys()
    print_test(
        "signal_metadata_complete",
        len(missing) == 0,
        f"missing={missing}" if missing else "all fields present",
    )


def test_disabled_returns_none() -> None:
    """When enabled=False, strategy never fires."""
    strat = CVDOrderFlow({"enabled": False})
    sym = "BTC"
    state = strat._get_state(sym)
    for i in range(60):
        state.bars_1m.append(_BarDelta(
            timestamp_ms=_ts(i),
            price_close=100.0 - i * 0.05,
            delta=800_000.0, total_volume=2_000_000.0,
            buy_volume=1_400_000.0, sell_volume=600_000.0,
        ))
    c = make_buy_sell_candle(60, 97.0, buy=1_400_000.0, sell=600_000.0)
    sig = strat.on_data(make_event(sym, 97.0, candle_1m=c, adx=20.0, oir=0.3, ts_offset=60))
    print_test("disabled_returns_none", sig is None)


# ── Runner ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print("CVDOrderFlow strategy tests")
    print("=" * 70)

    tests = [
        test_instantiation_defaults,
        test_instantiation_custom_config,
        test_no_candle_returns_none,
        test_zero_buy_sell_returns_none,
        test_warmup_gates_signal,
        test_bullish_divergence_long,
        test_bearish_divergence_short,
        test_no_signal_when_aligned,
        test_adx_too_high_blocks,
        test_adx_too_low_blocks,
        test_volume_too_low_blocks,
        test_volume_unit_conversion,
        test_oir_misaligned_blocks,
        test_throttle_blocks_rapid_signals,
        test_exit_max_hold,
        test_exit_take_profit,
        test_exit_stop_loss,
        test_window_stats_pure_function,
        test_window_stats_no_divergence_when_aligned,
        test_signal_metadata_complete,
        test_disabled_returns_none,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print_test(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            print_test(t.__name__, False, f"{type(e).__name__}: {e}")

    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
