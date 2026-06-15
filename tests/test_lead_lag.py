"""Tests for LeadLag strategy (Binance lead / HL lag arb)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent, Position
from src.strategies.lead_lag import LeadLag


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
        "stop_loss_pct": 0.003,
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
    bn_mid: float,
    ts_ms: int,
    *,
    spread: float = 0.0002,
    oir: float = 0.2,
) -> MarketEvent:
    return MarketEvent(
        symbol=symbol,
        price=hl_mid,
        timestamp_ms=ts_ms,
        binance_mid=bn_mid,
        binance_timestamp_ms=ts_ms,
        orderbook_spread_pct=spread,
        orderbook_oir=oir,
    )


def _warm_up(strategy: LeadLag, symbol: str = "BTC") -> None:
    """Seed HL/BN buffers with flat prices before impulse."""
    base_ts = 1_000_000
    for i in range(3):
        strategy.on_data(_event(symbol, 100_000.0, 100_000.0, base_ts + i * 1_000))


def test_warmup_blocks_signal_until_enough_samples() -> None:
    strategy = LeadLag(_cfg())
    sig = strategy.on_data(_event("BTC", 100_000.0, 100_000.0, 1_000_000))
    assert sig is None


def test_long_signal_on_binance_impulse_and_negative_gap() -> None:
    strategy = LeadLag(_cfg())
    _warm_up(strategy)

    # Binance +0.10% impulse; HL still ~0.10% below BN
    sig = strategy.on_data(_event("BTC", 99_900.0, 100_100.0, 12_000))
    assert sig is not None
    assert sig.side == "long"
    assert sig.strategy == "LeadLag"
    assert sig.stop_loss_pct == 0.003
    assert sig.take_profit_pct is not None and sig.take_profit_pct > 0
    assert sig.metadata["entry_gap_pct"] < 0


def test_short_signal_on_binance_dump_and_positive_gap() -> None:
    strategy = LeadLag(_cfg())
    for i in range(3):
        strategy.on_data(_event("BTC", 100_500.0, 100_500.0, 1_000 + i * 1_000, oir=-0.2))

    sig = strategy.on_data(
        _event("BTC", 100_200.0, 100_000.0, 12_000, oir=-0.2)
    )
    assert sig is not None
    assert sig.side == "short"
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
            "entry_bn_mid": 100_100.0,
        },
    )
    exit_sig = strategy.on_position(
        position,
        _event("BTC", 100_099.0, 100_100.0, 20_000),
    )
    assert exit_sig is not None
    assert "gap_converged" in exit_sig.reason


if __name__ == "__main__":
    test_warmup_blocks_signal_until_enough_samples()
    test_long_signal_on_binance_impulse_and_negative_gap()
    test_short_signal_on_binance_dump_and_positive_gap()
    test_gap_convergence_take_profit_exit()
    print("ALL LEADLAG TESTS PASSED [OK]")
