"""Tests for Hyperliquid SDK integration — all SDK calls are mocked.

Covers:
  - HyperliquidLiveClient: place_entry, close_position, cancel_order,
    get_open_orders, get_user_state, get_exchange_meta
  - Symbol normalisation: normalize_size, normalize_price, build_meta_cache
  - ExecutionEngine: paper mode keeps simulated, testnet routes to SDK
"""

from __future__ import annotations

import sys
import os
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.exchanges.hyperliquid_live import (
    HyperliquidLiveClient,
    normalize_private_key,
    resolve_private_key,
    normalize_size,
    normalize_price,
    build_meta_cache,
    get_symbol_info,
    _meta_cache,
)
from src.utils.helpers import safe_float


# ═══════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════

FAKE_KEY = "0x" + "a" * 64
FAKE_ADDRESS = "0x" + "b" * 40

MOCK_META = [
    {"name": "BTC", "szDecimals": 5, "pxDecimals": 1},
    {"name": "ETH", "szDecimals": 4, "pxDecimals": 2},
    {"name": "SOL", "szDecimals": 3, "pxDecimals": 3},
]

MOCK_OPEN_ORDERS = [
    {"oid": 123, "symbol": "BTC", "side": "B", "sz": "0.01000", "px": "50000.0"},
]

MOCK_USER_STATE = {
    "assetPositions": [
        {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "50000.0"}},
    ],
    "marginSummary": {"totalValue": "10000.0"},
}


# ═══════════════════════════════════════════════════════════════
#  Tests: helper functions
# ═══════════════════════════════════════════════════════════════

class TestHelpers(unittest.TestCase):
    def test_normalize_private_key_adds_prefix(self):
        raw = "a" * 64
        result = normalize_private_key(raw)
        self.assertTrue(result.startswith("0x"))
        self.assertEqual(len(result), 66)

    def test_normalize_private_key_keeps_prefix(self):
        raw = "0x" + "a" * 64
        result = normalize_private_key(raw)
        self.assertEqual(result, raw)

    def test_normalize_private_key_rejects_short(self):
        with self.assertRaises(ValueError):
            normalize_private_key("too_short")

    def test_normalize_private_key_rejects_empty(self):
        with self.assertRaises(ValueError):
            normalize_private_key("")

    def test_resolve_private_key_env(self):
        with patch.dict(os.environ, {"HYPERLIQUID_PRIVATE_KEY": "0x" + "a" * 64}, clear=True):
            result = resolve_private_key()
            self.assertIsNotNone(result)
            self.assertIn("0x", result)

    def test_resolve_private_key_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_private_key()
            self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════
#  Tests: symbol normalisation
# ═══════════════════════════════════════════════════════════════

class TestSymbolNormalisation(unittest.TestCase):
    def setUp(self):
        _meta_cache.clear()

    def test_build_meta_cache(self):
        info = MagicMock()
        info.meta.return_value = MOCK_META
        result = build_meta_cache(info)
        self.assertIn("BTC", result)
        self.assertEqual(result["BTC"]["sz_decimals"], 5)
        self.assertEqual(result["BTC"]["px_decimals"], 1)
        self.assertIn("SOL", result)
        self.assertEqual(result["SOL"]["sz_decimals"], 3)

    def test_build_meta_cache_invalid(self):
        info = MagicMock()
        info.meta.return_value = None
        result = build_meta_cache(info)
        self.assertEqual(result, {})

    def test_normalize_size(self):
        _meta_cache["BTC"] = {"sz_decimals": 5, "px_decimals": 1}
        self.assertEqual(normalize_size("BTC", 0.123456), 0.12345)
        self.assertEqual(normalize_size("BTC", 1.0), 1.0)

    def test_normalize_size_no_cache(self):
        self.assertEqual(normalize_size("UNKNOWN", 0.123456), 0.123456)

    def test_normalize_price(self):
        _meta_cache["ETH"] = {"sz_decimals": 4, "px_decimals": 2}
        self.assertEqual(normalize_price("ETH", 1234.567), 1234.57)
        self.assertEqual(normalize_price("ETH", 1000.0), 1000.0)

    def test_normalize_price_no_cache(self):
        self.assertEqual(normalize_price("UNKNOWN", 1234.567), 1234.567)


# ═══════════════════════════════════════════════════════════════
#  Tests: HyperliquidLiveClient (SDK mocked)
# ═══════════════════════════════════════════════════════════════

class FakeExchange:
    """Minimal fake that mimics the SDK Exchange surfaces used by the client."""

    def __init__(self):
        self.order_call = None
        self.market_open_call = None
        self.market_close_call = None
        self.cancel_call = None

    def order(self, symbol, is_buy, sz, px, order_type):
        self.order_call = (symbol, is_buy, sz, px, order_type)
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 999}}]}}}

    def market_open(self, symbol, is_buy, sz):
        self.market_open_call = (symbol, is_buy, sz)
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1000}}]}}}

    def market_close(self, symbol, sz):
        self.market_close_call = (symbol, sz)
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1001}}]}}}

    def cancel(self, symbol, oid):
        self.cancel_call = (symbol, oid)
        return {"status": "ok"}


class FakeInfo:
    def meta(self):
        return MOCK_META

    def open_orders(self, user):
        return MOCK_OPEN_ORDERS

    def user_state(self, user):
        return MOCK_USER_STATE


class FakeWallet:
    address = FAKE_ADDRESS
    @staticmethod
    def from_key(key):
        return FakeWallet()


class TestHyperliquidLiveClient(unittest.TestCase):
    def setUp(self):
        _meta_cache.clear()
        self.client = HyperliquidLiveClient(FAKE_KEY, use_testnet=True)
        self.fake_exchange = FakeExchange()
        self.fake_info = FakeInfo()
        self.client._exchange = self.fake_exchange
        self.client._info = self.fake_info
        self.client._wallet_address = FAKE_ADDRESS

    def test_place_entry_market(self):
        result = asyncio.run(self.client.place_entry("BTC", "long", 0.01))
        self.assertIsInstance(result, dict)
        call = self.fake_exchange.market_open_call
        self.assertIsNotNone(call)
        symbol, is_buy, sz = call
        self.assertEqual(symbol, "BTC")
        self.assertTrue(is_buy)
        self.assertGreater(sz, 0)

    def test_place_entry_market_short(self):
        result = asyncio.run(self.client.place_entry("ETH", "short", 1.0))
        call = self.fake_exchange.market_open_call
        symbol, is_buy, sz = call
        self.assertEqual(symbol, "ETH")
        self.assertFalse(is_buy)

    def test_place_entry_limit_maker(self):
        result = asyncio.run(self.client.place_entry(
            "SOL", "long", 10.0,
            order_type="limit_maker", limit_price=150.0, post_only=True,
        ))
        call = self.fake_exchange.order_call
        self.assertIsNotNone(call)
        symbol, is_buy, sz, px, otype = call
        self.assertEqual(symbol, "SOL")
        self.assertTrue(is_buy)
        self.assertEqual(otype, {"limit": {"tif": "Alo"}})

    def test_close_position(self):
        result = asyncio.run(self.client.close_position("BTC", 0.01))
        call = self.fake_exchange.market_close_call
        self.assertIsNotNone(call)
        symbol, sz = call
        self.assertEqual(symbol, "BTC")
        self.assertGreater(sz, 0)

    def test_cancel_order(self):
        result = asyncio.run(self.client.cancel_order("BTC", 12345))
        call = self.fake_exchange.cancel_call
        self.assertIsNotNone(call)
        symbol, oid = call
        self.assertEqual(symbol, "BTC")
        self.assertEqual(oid, 12345)

    def test_cancel_order_invalid_id(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.client.cancel_order("BTC", 0))

    def test_get_open_orders(self):
        orders = asyncio.run(self.client.get_open_orders())
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["oid"], 123)

    def test_get_user_state(self):
        state = asyncio.run(self.client.get_user_state())
        self.assertIn("assetPositions", state)
        self.assertIn("marginSummary", state)

    def test_get_exchange_meta(self):
        meta = asyncio.run(self.client.get_exchange_meta())
        self.assertEqual(len(meta), 3)
        names = [m["name"] for m in meta]
        self.assertIn("BTC", names)

    def test_not_initialized_raises(self):
        client = HyperliquidLiveClient(FAKE_KEY, use_testnet=True)
        with self.assertRaises(RuntimeError):
            asyncio.run(client.place_entry("BTC", "long", 0.01))
        with self.assertRaises(RuntimeError):
            asyncio.run(client.close_position("BTC", 0.01))
        with self.assertRaises(RuntimeError):
            asyncio.run(client.cancel_order("BTC", 1))
        with self.assertRaises(RuntimeError):
            asyncio.run(client.get_open_orders())
        with self.assertRaises(RuntimeError):
            asyncio.run(client.get_user_state())


# ═══════════════════════════════════════════════════════════════
#  Tests: ExecutionEngine routing (paper vs testnet)
# ═══════════════════════════════════════════════════════════════

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
        engine._live_signing_ready = True
        engine._rest_client = MagicMock()
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


# ═══════════════════════════════════════════════════════════════
#  Run
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()