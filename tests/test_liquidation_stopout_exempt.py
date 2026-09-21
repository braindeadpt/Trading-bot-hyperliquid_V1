"""Liquidation stop-out exemption — JevJudge audit fix (2026-09-20).

The 5-minute liquidation-window stop-out was calibrated for
LiquidationCatcher-style scalps. JevJudge carries a 4h horizon with its
own >=1% price stop, so a 5m flush aborting a 4h thesis is a timeframe
mismatch — measured: 8/29 Jev trades stopped out, -117.84 USD, one closed
while price was already in favor.

Covers:
1. Exempt strategy + triggering window -> NO stop-out.
2. Non-exempt strategy + same window -> stop-out fires (regression guard).
3. ``liquidation_stopout_decision`` itself is untouched — same args give
   the same verdict.
"""

from __future__ import annotations

import pytest

from src.core.engine import LIQUIDATION_STOPOUT_EXEMPT_STRATEGIES, TradingEngine
from src.core.liquidation_stopout import (
    LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD,
    STOPOUT_REASON,
    liquidation_stopout_decision,
)
from src.strategies.base import MarketEvent, Position


def _bare_engine(notional: float, liq_side: str) -> TradingEngine:
    engine = TradingEngine.__new__(TradingEngine)
    engine._get_liquidation_stats = (  # type: ignore[attr-defined]
        lambda symbol: (notional, liq_side, 3)
    )
    engine._exits: list = []  # type: ignore[attr-defined]

    async def _fake_exit(position, price, reason=None, **_kw):
        engine._exits.append((position.symbol, reason))  # type: ignore[attr-defined]

    engine._execute_exit = _fake_exit  # type: ignore[attr-defined]
    return engine


def _position(strategy: str | None) -> Position:
    return Position(
        symbol="BTC",
        side="short",
        entry_price=80_000.0,
        size=0.05,
        entry_time_ms=1_000,
        metadata={"strategy": strategy} if strategy else {},
    )


def _event() -> MarketEvent:
    return MarketEvent(symbol="BTC", price=80_100.0, timestamp_ms=2_000)


@pytest.mark.unit
async def test_exempt_strategy_skips_stopout():
    """JevJudge position + flush window that would trigger -> no exit."""
    engine = _bare_engine(notional=5_000_000.0, liq_side="short")
    fired = await engine._maybe_liquidation_stop_out(
        _position("JevJudge"), _event()
    )
    assert fired is False
    assert engine._exits == []  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_non_exempt_strategy_still_stops_out():
    """Regression: the protection stays intact for other strategies."""
    engine = _bare_engine(notional=5_000_000.0, liq_side="short")
    fired = await engine._maybe_liquidation_stop_out(
        _position("LiquidationCatcher"), _event()
    )
    assert fired is True
    assert engine._exits == [("BTC", STOPOUT_REASON)]  # type: ignore[attr-defined]


@pytest.mark.unit
async def test_missing_strategy_metadata_not_exempt():
    """A position without strategy metadata behaves as non-exempt."""
    engine = _bare_engine(notional=5_000_000.0, liq_side="short")
    fired = await engine._maybe_liquidation_stop_out(_position(None), _event())
    assert fired is True
    assert engine._exits == [("BTC", STOPOUT_REASON)]  # type: ignore[attr-defined]


@pytest.mark.unit
def test_decision_function_unchanged():
    """The pure decision is not modified — same args, same verdict."""
    assert (
        liquidation_stopout_decision("short", "short", 5_000_000.0) is True
    )
    assert (
        liquidation_stopout_decision("short", "long", 5_000_000.0) is False
    )
    assert (
        liquidation_stopout_decision(
            "short", "short", LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD - 1
        )
        is False
    )
    assert liquidation_stopout_decision("short", None, None) is False


@pytest.mark.unit
def test_exempt_set_is_frozenset_and_not_config():
    """Hash-neutral by contract: module constant, not a config key."""
    assert isinstance(LIQUIDATION_STOPOUT_EXEMPT_STRATEGIES, frozenset)
    assert "JevJudge" in LIQUIDATION_STOPOUT_EXEMPT_STRATEGIES
