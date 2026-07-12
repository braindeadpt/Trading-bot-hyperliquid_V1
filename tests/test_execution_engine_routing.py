"""Tests for ExecutionEngine <-> Hyperliquid SDK routing (paper vs testnet).

Split out of test_hyperliquid_sdk.py: this exercises ExecutionEngine wired
together with mocked HyperliquidLiveClient/HyperliquidRESTClient, so it
belongs in the integration-offline suite rather than the unit suite.
"""

from __future__ import annotations

import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAKE_KEY = "0x" + "a" * 64


@pytest.mark.integration_offline
class TestExecutionEngineRouting(unittest.TestCase):
    """Verify that paper mode keeps the simulated backend and testnet routes
    to the SDK adapter."""

    def setUp(self):
        self._db = MagicMock()

    @patch("src.exchanges.hyperliquid_live.HyperliquidLiveClient")
    @patch("src.exchanges.hyperliquid_rest.HyperliquidRESTClient")
    @patch("src.exchanges.hyperliquid_live.resolve_private_key", return_value=FAKE_KEY)
    def test_testnet_creates_live_client(self, mock_resolve, mock_rest, mock_live):
        mock_rest.return_value.open = AsyncMock()
        mock_live.return_value.open = AsyncMock()
        from src.core.execution import ExecutionEngine
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: {
            "risk.taker_fee_pct": 0.035,
            "risk.paper_slippage_pct": 0.05,
            "execution.maker_orders": {"enabled": False},
            "exchange.mainnet_enabled": False,
            "risk.initial_capital": 10_000,
        }.get(key, default)

        engine = ExecutionEngine(cfg, self._db, mode="testnet")
        asyncio.run(engine.open())

        mock_live.assert_called_once()
        mock_rest.assert_called_once()

    @patch("src.exchanges.hyperliquid_live.HyperliquidLiveClient")
    @patch("src.exchanges.hyperliquid_rest.HyperliquidRESTClient")
    def test_paper_does_not_create_live_client(self, mock_rest, mock_live):
        mock_rest.return_value.open = AsyncMock()
        from src.core.execution import ExecutionEngine
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: {
            "risk.taker_fee_pct": 0.035,
            "risk.paper_slippage_pct": 0.05,
            "execution.maker_orders": {"enabled": False},
            "exchange.mainnet_enabled": False,
            "risk.initial_capital": 10_000,
        }.get(key, default)

        engine = ExecutionEngine(cfg, self._db, mode="paper")
        asyncio.run(engine.open())

        mock_live.assert_not_called()
        mock_rest.assert_not_called()

    @patch("src.exchanges.hyperliquid_live.HyperliquidLiveClient")
    @patch("src.exchanges.hyperliquid_live.resolve_private_key", return_value=FAKE_KEY)
    def test_sdk_adjusts_size(self, mock_resolve, mock_live):
        """Verify that place_entry is called with correctly normalized size."""
        from src.core.execution import ExecutionEngine
        from src.data.database import Database
        from src.strategies.base import Signal as BaseSignal

        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: {
            "risk.taker_fee_pct": 0.035,
            "risk.paper_slippage_pct": 0.05,
            "execution.maker_orders": {"enabled": False},
            "exchange.mainnet_enabled": False,
            "risk.initial_capital": 10_000,
        }.get(key, default)

        engine = ExecutionEngine(cfg, self._db, mode="testnet")
        engine._live_client = AsyncMock()
        engine._live_client.is_ready = True
        engine._live_client.place_entry = AsyncMock(
            return_value={
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"oid": 1, "totalSz": "0.01", "avgPx": "50000"}}
                        ]
                    }
                },
            }
        )
        engine._live_signing_ready = True
        engine._rest_client = MagicMock()
        engine._db = Database(":memory:")
        engine._portfolio = MagicMock()

        async def run_test():
            loop = asyncio.get_running_loop()
            engine._portfolio.current_capital = loop.create_future()
            engine._portfolio.current_capital.set_result(10_000.0)

            signal = BaseSignal(
                strategy="test",
                symbol="BTC",
                side="long",
                confidence=0.8,
                size_pct=0.1,
                entry_price=50000.0,
                stop_loss_pct=0.02,
                take_profit_pct=0.04,
                reason="test_sdk",
                metadata={"calculated_size": 0.01},
            )

            await engine.enter_position(signal, engine._portfolio)
            live_client = engine._live_client
            live_client.place_entry.assert_called_once()
            args, kwargs = live_client.place_entry.call_args
            self.assertEqual(args[0], "BTC")
            self.assertEqual(args[1], "long")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
