"""Unit tests for Phase-2 liquidation reaction analysis (offline)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.research.liquidation_reaction_analysis import (
    LiquidationEvent,
    classify_forward_reaction,
    cluster_liquidation_prices,
    estimate_sample_need,
    extract_liquidation_events,
    measure_reaction,
)

pytestmark = pytest.mark.unit


def _liq_fill(
    address: str,
    *,
    coin: str,
    px: str,
    sz: str,
    side: str,
    dir_: str,
    time_ms: int,
    tid: int,
    liquidated_user: str,
    mark_px: str,
) -> list:
    return [
        address,
        {
            "coin": coin,
            "px": px,
            "sz": sz,
            "side": side,
            "time": time_ms,
            "dir": dir_,
            "tid": tid,
            "oid": tid,
            "crossed": True,
            "hash": "0xh",
            "startPosition": "0",
            "closedPnl": "0",
            "fee": "0",
            "feeToken": "USDC",
            "twapId": None,
            "liquidation": {
                "liquidatedUser": liquidated_user,
                "markPx": mark_px,
                "method": "market",
            },
        },
    ]


def _block(events: list) -> dict:
    return {
        "local_time": "2026-07-15T14:00:00",
        "block_time": "2026-07-15T14:00:00",
        "block_number": 1,
        "events": events,
    }


def test_extract_liquidation_keeps_user_leg_only() -> None:
    user = "0xdead"
    other = "0xbeef"
    t = 1_800_000_000_000
    # Same tid: liquidated user's Close Short + counterparty Close Long both carry liquidation
    blocks = [
        _block(
            [
                _liq_fill(
                    user, coin="BTC", px="65000", sz="0.1", side="B", dir_="Close Short",
                    time_ms=t, tid=1, liquidated_user=user, mark_px="64990",
                ),
                _liq_fill(
                    other, coin="BTC", px="65000", sz="0.1", side="A", dir_="Close Long",
                    time_ms=t, tid=1, liquidated_user=user, mark_px="64990",
                ),
            ],
        ),
    ]
    events = extract_liquidation_events(blocks, coins=["BTC"])
    assert len(events) == 1
    assert events[0].liquidated_side == "short"
    assert events[0].liquidated_user == user


def test_measure_reaction_short_liq_flush_up() -> None:
    """Short liquidated → flush further up within 5m; reverse dump by 30m."""
    t0 = 1_800_000_000_000
    event = LiquidationEvent(
        coin="BTC", time_ms=t0, price=100.0, size=1.0, liquidated_side="short",
        liquidated_user="0x1", mark_px=100.0, method="market", dir="Close Short",
        tid=1, notional_usd=100.0,
    )
    # entry at t0 close 100; +5m close 100.2 (+0.2%); +30m close 99.5 (-0.5%)
    candles = []
    for i in range(35):
        if i <= 5:
            px = 100.0 + 0.04 * i  # i=5 → 100.20
        elif i < 20:
            px = 100.2 - 0.02 * (i - 5)
        else:
            px = 99.5
        candles.append({
            "timestamp_ms": t0 + i * 60_000,
            "open": px, "high": px, "low": px, "close": px, "volume": 1.0,
        })
    rr = measure_reaction(
        event, candles, flush_minutes=5, reverse_minutes=30,
        flush_threshold_pct=0.05, reverse_threshold_pct=0.05,
        candle_source="test",
    )
    assert rr is not None
    assert rr.flushed is True
    assert rr.flush_return_pct == pytest.approx(0.2, abs=0.02)
    assert rr.reversed is True


def test_measure_reaction_long_liq_no_flush() -> None:
    t0 = 1_800_000_000_000
    event = LiquidationEvent(
        coin="ETH", time_ms=t0, price=2000.0, size=1.0, liquidated_side="long",
        liquidated_user="0x2", mark_px=2000.0, method="market", dir="Close Long",
        tid=2, notional_usd=2000.0,
    )
    candles = []
    for i in range(40):
        # Price drifts slightly up — no dump flush for long liq
        px = 2000.0 + i * 0.1
        candles.append({
            "timestamp_ms": t0 + i * 60_000,
            "open": px, "high": px, "low": px, "close": px, "volume": 1.0,
        })
    rr = measure_reaction(
        event, candles, flush_minutes=5, reverse_minutes=30,
        flush_threshold_pct=0.05, reverse_threshold_pct=0.05,
    )
    assert rr is not None
    assert rr.flushed is False


def test_cluster_liquidation_prices_min_events() -> None:
    t0 = 1_800_000_000_000
    events = [
        LiquidationEvent(
            "BTC", t0, 100.0, 1.0, "short", "0xa", 100.0, "market", "Close Short", 1, 100.0,
        ),
        LiquidationEvent(
            "BTC", t0 + 1, 100.1, 1.0, "short", "0xb", 100.0, "market", "Close Short", 2, 100.0,
        ),
        LiquidationEvent(
            "BTC", t0 + 2, 110.0, 1.0, "long", "0xc", 100.0, "market", "Close Long", 3, 100.0,
        ),
    ]
    clusters = cluster_liquidation_prices(events, bucket_pct=1.0, min_events=2, mark_px=100.0)
    # Two shorts near 100 should cluster; lone long at 110 should not (min_events=2)
    assert any(c["side"] == "short" and c["event_count"] >= 2 for c in clusters)
    assert not any(c["side"] == "long" for c in clusters)


def test_classify_forward_reaction() -> None:
    assert classify_forward_reaction("long", 100.0, 100.5) == "reverse"
    assert classify_forward_reaction("long", 100.0, 99.5) == "accelerate"
    assert classify_forward_reaction("short", 100.0, 99.5) == "reverse"
    assert classify_forward_reaction("short", 100.0, 100.5) == "accelerate"
    assert classify_forward_reaction("long", 100.0, 100.01) == "none"


def test_estimate_sample_need() -> None:
    est = estimate_sample_need(snapshots_per_day=24, approaches_per_snapshot=0.5, target_approaches=50)
    assert est["estimated_days"] == 5
