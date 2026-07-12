"""Tests for ensemble sub-strategy propagation into trades."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.execution import ExecutionEngine
from src.core.regime import regime_strategy_name
from src.data.database import Database
from src.strategies.base import MarketEvent, Signal
from src.utils.config import Config
import pytest

pytestmark = pytest.mark.integration_offline


class _PortfolioStub:
    def __init__(self, capital: float) -> None:
        self._capital = capital

    @property
    async def current_capital(self) -> float:
        return self._capital


def _ensemble_signal(
    *,
    original: str | None = None,
    agreeing: list[str] | None = None,
) -> Signal:
    meta: dict = {}
    if original:
        meta["original_strategy"] = original
    if agreeing:
        meta["strategies_agreeing"] = agreeing
    return Signal(
        strategy="StrategyEnsemble",
        symbol="BTC",
        side="long",
        confidence=0.85,
        size_pct=0.01,
        entry_price=50_000.0,
        stop_loss_pct=0.01,
        metadata={**meta, "calculated_size": 0.02},
    )


def test_regime_strategy_name_confluence_path() -> None:
    sig = _ensemble_signal(agreeing=["CVDOrderFlow", "DonchianBreakout"])
    assert regime_strategy_name(sig) == "CVDOrderFlow"


async def _enter_and_fetch_sub_strategy(sig: Signal) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        cfg = Config({"mode": "paper", "database": {"path": str(db_path)}})
        db = Database(db_path)
        executor = ExecutionEngine(cfg, db)
        portfolio = _PortfolioStub(10_000.0)
        event = MarketEvent(symbol="BTC", price=50_000.0, timestamp_ms=int(time.time() * 1000))
        result = await executor.enter_position(sig, portfolio, market_event=event)
        assert result.status == "open", result.reason
        rows = db.get_open_trades()
        assert len(rows) == 1
        sub = rows[0].get("sub_strategy")
        db.close()
        return sub


def test_high_conviction_trade_records_original_strategy() -> None:
    sub = asyncio.run(
        _enter_and_fetch_sub_strategy(_ensemble_signal(original="CVDOrderFlow"))
    )
    assert sub == "CVDOrderFlow"


def test_confluence_trade_records_first_agreeing_strategy() -> None:
    sub = asyncio.run(
        _enter_and_fetch_sub_strategy(
            _ensemble_signal(agreeing=["VWAPDeviation", "DonchianBreakout"])
        )
    )
    assert sub == "VWAPDeviation"


def test_sub_strategy_not_strategy_ensemble() -> None:
    for sub in (
        asyncio.run(_enter_and_fetch_sub_strategy(_ensemble_signal(original="VolatilityBreakout"))),
        asyncio.run(
            _enter_and_fetch_sub_strategy(
                _ensemble_signal(agreeing=["SmartMoneyFlow", "CVDOrderFlow"])
            )
        ),
    ):
        assert sub is not None
        assert sub != "StrategyEnsemble"


def main() -> None:
    test_regime_strategy_name_confluence_path()
    test_high_conviction_trade_records_original_strategy()
    test_confluence_trade_records_first_agreeing_strategy()
    test_sub_strategy_not_strategy_ensemble()
    print("test_ensemble_sub_strategy: all passed")


if __name__ == "__main__":
    main()
