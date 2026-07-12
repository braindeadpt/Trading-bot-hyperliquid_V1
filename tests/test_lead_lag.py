"""Tests for LeadLag strategy (Binance perp lead / HL lag arb)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent, Position
from src.strategies.lead_lag import LeadLag
import pytest

pytestmark = pytest.mark.unit


def _cfg() -> dict:
    return {
        "impulse_window_ms": 10_000,
        "impulse_min_pct": 0.0005,
        "gap_min_pct": 0.0001,
        "min_edge_buffer_pct": 0.0001,
        "taker_fee_pct": 0.00035,
        "max_hl_spread_pct": 0.001,
        "require_oir_confirm": True,
        "oir_threshold": 0.05,
        "convergence_pct": 0.0001,
        "stop_gap_mult": 1.5,
        "stop_floor_pct": 0.0008,
        "stop_ceiling_pct": 0.004,
        "min_tp_r": 1.0,
        "max_hold_seconds": 120,
        "base_size_pct": 0.005,
        "max_size_pct": 0.008,
        "min_confidence": 0.40,
        "signal_throttle_ms": 0,
        "buffer_ms": 30_000,
        "max_binance_stale_ms": 5_000,
        "warmup_samples": 3,
    }


def _event(
    symbol: str,
    hl_mid: float,
    bn_perp_mid: float,
    ts_ms: int,
    *,
    bn_spot_mid: float | None = None,
    spread: float = 0.0002,
    oir: float = 0.2,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=hl_mid,
        timestamp_ms=ts_ms,
        binance_perp_mid=bn_perp_mid,
        binance_perp_timestamp_ms=ts_ms,
        binance_mid=bn_spot_mid if bn_spot_mid is not None else bn_perp_mid,
        binance_timestamp_ms=ts_ms,
        orderbook_spread_pct=spread,
        orderbook_oir=oir,
    )


def _warm_up(strategy: LeadLag, symbol: str = "BTC") -> None:
    base_ts = 1_000_000
    for i in range(3):
        strategy.on_data(_event(symbol, 100_000.0, 100_000.0, base_ts + i * 1_000))


def test_warmup_blocks_signal_until_enough_samples() -> None:
    strategy = LeadLag(_cfg())
    sig = strategy.on_data(_event("BTC", 100_000.0, 100_000.0, 1_000_000))
    assert sig is None


def test_long_signal_uses_perp_gap_with_proportional_stop() -> None:
    strategy = LeadLag(_cfg())
    _warm_up(strategy)

    # ~0.50% perp gap + impulse — large enough for R:R >= 1:1
    sig = strategy.on_data(_event("BTC", 99_500.0, 100_500.0, 12_000))
    assert sig is not None
    assert sig.side == "long"
    assert sig.take_profit_pct is not None
    assert sig.stop_loss_pct is not None
    assert sig.take_profit_pct >= sig.stop_loss_pct
    assert sig.stop_loss_pct == 0.004  # clamp(|gap|*1.5, floor, ceiling)
    assert sig.metadata["entry_gap_pct"] < 0
    assert sig.metadata["take_profit_r"] >= 1.0


def test_rejects_signal_when_rr_below_minimum() -> None:
    strategy = LeadLag(_cfg())
    _warm_up(strategy)
    strategy.on_data(_event("BTC", 100_000.0, 100_100.0, 11_500))

    # ~0.20% perp gap with impulse — fails min_tp_r vs proportional stop
    sig = strategy.on_data(_event("BTC", 99_800.0, 100_000.0, 12_000))
    assert sig is None


def test_spot_basis_alone_does_not_trigger_without_perp_gap() -> None:
    """Spot shows lag but perp is aligned with HL — no signal."""
    strategy = LeadLag(_cfg())
    _warm_up(strategy)

    # Perp flat at 100k; spot pumped to 100.15k would fake lag if used
    strategy.on_data(
        _event(
            "BTC",
            100_000.0,
            100_000.0,
            11_000,
            bn_spot_mid=100_150.0,
        )
    )
    sig = strategy.on_data(
        _event(
            "BTC",
            100_000.0,
            100_000.0,
            12_000,
            bn_spot_mid=100_150.0,
        )
    )
    assert sig is None


def test_requires_binance_perp_mid_not_spot_only() -> None:
    strategy = LeadLag(_cfg())
    _warm_up(strategy)

    sig = strategy.on_data(
        MarketEvent(
            symbol="BTC",
            price=99_500.0,
            timestamp_ms=12_000,
            binance_mid=100_000.0,
            binance_timestamp_ms=12_000,
            orderbook_spread_pct=0.0002,
            orderbook_oir=0.2,
        )
    )
    assert sig is None


def test_short_signal_on_perp_dump_and_positive_gap() -> None:
    strategy = LeadLag(_cfg())
    for i in range(3):
        strategy.on_data(_event("BTC", 100_500.0, 100_500.0, 1_000 + i * 1_000, oir=-0.2))

    sig = strategy.on_data(
        _event("BTC", 100_500.0, 100_000.0, 12_000, oir=-0.2)
    )
    assert sig is not None
    assert sig.side == "short"
    assert sig.take_profit_pct >= sig.stop_loss_pct
    assert sig.metadata["entry_gap_pct"] > 0


def test_gap_convergence_take_profit_exit() -> None:
    strategy = LeadLag(_cfg())
    position = Position(
        symbol="BTC",
        side="long",
        entry_price=100_020.0,
        size=0.01,
        entry_time_ms=10_000,
        metadata={
            "original_strategy": "LeadLag",
            "entry_gap_pct": -0.0008,
            "entry_bn_perp_mid": 100_100.0,
        },
    )
    exit_sig = strategy.on_position(
        position,
        _event("BTC", 100_099.0, 100_100.0, 20_000),
    )
    assert exit_sig is not None
    assert "gap_converged" in exit_sig.reason


def _basis_cfg() -> dict:
    cfg = _cfg()
    cfg["mode"] = "basis"
    cfg["min_excess_basis"] = 0.0015
    cfg["signal_throttle_ms"] = 0
    cfg["warmup_samples"] = 3
    cfg["buffer_ms"] = 30_000
    cfg["min_confidence"] = 0.30
    return cfg


def _basis_event(
    symbol: str,
    hl_mid: float,
    bn_spot_mid: float,
    ts_ms: int,
    *,
    predicted_funding: float = 0.0001,
    spread: float = 0.0002,
    oir: float = 0.2,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=hl_mid,
        timestamp_ms=ts_ms,
        binance_mid=bn_spot_mid,
        binance_timestamp_ms=ts_ms,
        predicted_funding=predicted_funding,
        orderbook_spread_pct=spread,
        orderbook_oir=oir,
    )


def _warm_basis(strategy: LeadLag, symbol: str = "BTC") -> None:
    base_ts = 1_000_000
    for i in range(3):
        strategy.on_data(_basis_event(symbol, 100_000.0, 100_000.0, base_ts + i * 1_000))


def test_basis_short_signal_when_perp_above_spot_plus_funding() -> None:
    """Perp rich vs spot: basis > fair_basis → short the perp."""
    strategy = LeadLag(_basis_cfg())
    _warm_basis(strategy)
    # HL perp at 100.4, spot at 100.0 → basis = 0.4%
    # predicted funding 0.0001 (~0.01%) → fair_basis ≈ 0
    # excess = 0.4% > min_excess_basis 0.15%
    sig = strategy.on_data(
        _basis_event("BTC", 100_400.0, 100_000.0, 12_000,
                     predicted_funding=0.0001, oir=-0.2)
    )
    assert sig is not None
    assert sig.side == "short"
    assert sig.metadata.get("mode") == "basis"
    assert sig.take_profit_pct is not None
    assert sig.stop_loss_pct is not None
    assert sig.take_profit_pct / sig.stop_loss_pct >= 1.5


def test_basis_long_signal_when_perp_below_spot_plus_funding() -> None:
    """Perp cheap vs spot: basis < -fair_basis → long the perp."""
    strategy = LeadLag(_basis_cfg())
    _warm_basis(strategy)
    sig = strategy.on_data(
        _basis_event("BTC", 99_600.0, 100_000.0, 12_000,
                     predicted_funding=0.0001, oir=0.2)
    )
    assert sig is not None
    assert sig.side == "long"
    assert sig.metadata.get("mode") == "basis"


def test_basis_no_signal_when_excess_below_threshold() -> None:
    strategy = LeadLag(_basis_cfg())
    _warm_basis(strategy)
    # basis 0.1% (HL 100.1, spot 100.0) but threshold is 0.15%
    sig = strategy.on_data(
        _basis_event("BTC", 100_100.0, 100_000.0, 12_000, predicted_funding=0.0)
    )
    assert sig is None


def test_basis_requires_fresh_spot() -> None:
    """A stale Binance spot tick (> 2s) is rejected."""
    strategy = LeadLag(_basis_cfg())
    _warm_basis(strategy)
    ev = MarketEvent(
        symbol="BTC",
        price=100_400.0,
        timestamp_ms=20_000,
        binance_mid=100_000.0,
        binance_timestamp_ms=10_000,  # 10s old
        predicted_funding=0.0001,
        orderbook_spread_pct=0.0002,
        orderbook_oir=0.2,
    )
    sig = strategy.on_data(ev)
    assert sig is None


def test_basis_close_exit_on_excess_reverted() -> None:
    strategy = LeadLag(_basis_cfg())
    position = Position(
        symbol="BTC", side="short", entry_price=100_400.0, size=0.01,
        entry_time_ms=10_000,
        metadata={"original_strategy": "LeadLag", "mode": "basis"},
    )
    # Excess reverts to near 0
    ev = _basis_event("BTC", 100_010.0, 100_000.0, 20_000, predicted_funding=0.0001)
    exit_sig = strategy.on_position(position, ev)
    assert exit_sig is not None
    assert "basis_closed" in exit_sig.reason


if __name__ == "__main__":
    test_warmup_blocks_signal_until_enough_samples()
    test_long_signal_uses_perp_gap_with_proportional_stop()
    test_rejects_signal_when_rr_below_minimum()
    test_spot_basis_alone_does_not_trigger_without_perp_gap()
    test_requires_binance_perp_mid_not_spot_only()
    test_short_signal_on_perp_dump_and_positive_gap()
    test_gap_convergence_take_profit_exit()
    test_basis_short_signal_when_perp_above_spot_plus_funding()
    test_basis_long_signal_when_perp_below_spot_plus_funding()
    test_basis_no_signal_when_excess_below_threshold()
    test_basis_requires_fresh_spot()
    test_basis_close_exit_on_excess_reverted()
    print("ALL LEADLAG TESTS PASSED [OK]")
