"""Freqtrade-inspired HL hygiene: liquidation fills + aggressive market limits."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.portfolio import PortfolioState
from src.core.reconciliation import ExchangeReconciler
from src.core.trigger_reconcile import fill_is_liquidation, match_closing_fills
from src.exchanges.hyperliquid_live import HyperliquidLiveClient
from src.strategies.base import Position

pytestmark = pytest.mark.unit


def test_fill_is_liquidation_markers() -> None:
    assert fill_is_liquidation({"liquidationMarkPx": "49000.5"})
    assert fill_is_liquidation({"liquidation": {"markPx": "49000", "method": "market"}})
    assert not fill_is_liquidation({"px": "50000", "sz": "0.01"})
    assert not fill_is_liquidation({})


def test_match_closing_fills_tags_liquidation() -> None:
    fills = [{
        "coin": "BTC",
        "side": "A",
        "sz": "0.01",
        "px": "49100",
        "time": 2_000,
        "fee": "0.1",
        "closedPnl": "-12.5",
        "liquidationMarkPx": "49050",
        "oid": 77,
    }]
    match = match_closing_fills(
        fills,
        symbol="BTC",
        position_side="long",
        expected_size=0.01,
        entry_time_ms=1_000,
        sl_price=48_000.0,
        tp_price=55_000.0,
    )
    assert match is not None
    assert match.exit_reason == "liquidation"
    assert abs(match.exit_price - 49_050.0) < 1e-6


def test_orphan_local_liquidation_reconciles_without_native() -> None:
    async def _run() -> bool:
        class _Client:
            user_fills: List[Dict[str, Any]] = [{
                "coin": "BTC",
                "side": "A",
                "sz": "0.01",
                "px": "49000",
                "time": 5_000,
                "fee": "0.2",
                "closedPnl": "-10",
                "liquidationMarkPx": "48950",
                "oid": 9,
            }]

            async def get_user_fills(self, *, lookback_ms: int = 0) -> List[Dict[str, Any]]:
                return list(self.user_fills)

            async def get_user_state(self) -> Dict[str, Any]:
                return {"assetPositions": [], "marginSummary": {"totalValue": "10000"}}

            async def get_open_orders(self) -> List[Dict[str, Any]]:
                return []

        live = _Client()
        portfolio = PortfolioState(10_000)
        await portfolio.add_position(
            Position(
                symbol="BTC",
                side="long",
                entry_price=50_000,
                size=0.01,
                entry_time_ms=1_000,
                metadata={},
            ),
            cost=500,
        )
        recon = ExchangeReconciler(
            live_client=live,
            portfolio=portfolio,
            liquidation_reconcile_enabled=True,
        )
        report = await recon.reconcile_once()
        mem = await portfolio.positions
        return (
            "BTC" not in mem
            and any("liquidation" in a for a in report.actions)
            and recon.entries_blocked()
        )

    assert asyncio.run(_run())


def test_place_aggressive_limit_price_band() -> None:
    client = HyperliquidLiveClient.__new__(HyperliquidLiveClient)
    calls: List[Any] = []

    def _order(symbol, is_buy, sz, px, order_type, reduce_only=False):
        calls.append({
            "symbol": symbol,
            "is_buy": is_buy,
            "sz": sz,
            "px": px,
            "order_type": order_type,
            "reduce_only": reduce_only,
        })
        return {"status": "ok"}

    client._exchange = MagicMock()
    client._exchange.order = _order

    async def _run() -> None:
        # Bypass normalize by patching helpers used inside
        import src.exchanges.hyperliquid_live as hl

        orig_sz = hl.normalize_size
        orig_px = hl.normalize_price
        hl.normalize_size = lambda _s, x: float(x)
        hl.normalize_price = lambda _s, x: float(x)
        try:
            await client.place_aggressive_limit(
                "BTC",
                "long",
                0.01,
                reference_price=100.0,
                max_slippage_pct=5.0,
            )
            await client.place_aggressive_limit(
                "BTC",
                "short",
                0.01,
                reference_price=100.0,
                max_slippage_pct=5.0,
                reduce_only=True,
            )
        finally:
            hl.normalize_size = orig_sz
            hl.normalize_price = orig_px

    asyncio.run(_run())
    assert len(calls) == 2
    assert calls[0]["is_buy"] is True
    assert abs(calls[0]["px"] - 105.0) < 1e-9
    assert calls[0]["order_type"] == {"limit": {"tif": "Ioc"}}
    assert calls[1]["is_buy"] is False
    assert abs(calls[1]["px"] - 95.0) < 1e-9
    assert calls[1]["reduce_only"] is True
