"""Basic tests for the trading bot components."""
import sys
import os
import unittest

# Ensure src/ is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils.config import load_config
from src.exchanges.hyperliquid_ws import DataBus, HlPriceTick, HlAssetCtx
from src.data.orderbook_metrics import PriceLevel
from src.data.candle_builder import CandleBuilder
from src.core.portfolio import PortfolioState
from src.core.risk_manager import RiskManager
from src.core.execution import ExecutionEngine
from src.core.correlation_monitor import CorrelationMonitor
from src.data.database import Database
from src.strategies.base import MarketEvent
from src.alerts import AlertNotifier, AlertConfig


class TestConfig(unittest.TestCase):
    def test_load(self):
        cfg = load_config("config/settings.yaml")
        self.assertIsNotNone(cfg)
        self.assertIn("risk", cfg)


class TestDataBus(unittest.TestCase):
    def test_publish_subscribe(self):
        import asyncio
        bus = DataBus()
        received = []
        asyncio.run(bus.subscribe("test", lambda x: received.append(x)))
        bus.publish("test", 42)
        self.assertEqual(received, [42])


class TestCandleBuilder(unittest.TestCase):
    def test_init(self):
        bus = DataBus()
        cb = CandleBuilder(bus, ["BTC"], {"BTC": 60})
        self.assertIn("BTC", cb.symbols)


class TestPortfolio(unittest.TestCase):
    def test_init(self):
        p = PortfolioState(100000.0)
        self.assertEqual(p.sync_capital(), 100000.0)


class TestRiskManager(unittest.TestCase):
    def test_init(self):
        cfg = load_config("config/settings.yaml")
        from src.data.database import Database
        db = Database(":memory:")
        rm = RiskManager(cfg, db)
        self.assertIsNotNone(rm)


class TestExecutionEngine(unittest.TestCase):
    def test_init(self):
        cfg = load_config("config/settings.yaml")
        db = Database(":memory:")
        ex = ExecutionEngine(cfg, db)
        self.assertIsNotNone(ex)


class TestCorrelationMonitor(unittest.TestCase):
    def test_correlation(self):
        cm = CorrelationMonitor(lookback=5)
        # Add some correlated returns
        for i in range(10):
            cm.add_return("BTC", 100.0 + i)
            cm.add_return("ETH", 100.0 + i * 0.9)
        corr = cm.get_correlation("BTC", "ETH")
        self.assertIsNotNone(corr)
        self.assertGreater(corr, 0.5)


class TestDatabase(unittest.TestCase):
    def test_init(self):
        db = Database(":memory:")
        self.assertIsNotNone(db)


class TestAlertNotifier(unittest.TestCase):
    def test_init_disabled(self):
        cfg = AlertConfig(enabled=False)
        notifier = AlertNotifier(cfg)
        self.assertIsNotNone(notifier)

    def test_no_send_when_disabled(self):
        cfg = AlertConfig(enabled=False)
        notifier = AlertNotifier(cfg)
        # Should not raise
        import asyncio
        asyncio.run(notifier.send("test", "info"))


class TestMarketEvent(unittest.TestCase):
    def test_create(self):
        import time
        e = MarketEvent(
            symbol="BTC",
            timestamp_ms=int(time.time() * 1000),
            price=81000.0,
            funding=-0.0001,
            predicted_funding=0.0,
            oi_total=30000.0,
            oi_delta=0.0,
            volume_1m=1e6,
            bid_ask_imbalance=0.0,
            vwap_15m=81000.0,
            funding_avg=-0.0001,
            funding_weighted=-0.0001,
            predicted_funding_avg=0.0,
            oi_total_aggregated=1e9,
            oi_exchange_count=3,
            orderbook_spread_pct=0.001,
            orderbook_oir=0.5,
            orderbook_depth_quality=0.5,
            orderbook_bid_ask_ratio=1.0,
            orderbook_largest_bid_wall=80900.0,
            orderbook_largest_ask_wall=81100.0,
            ema_20=81000.0,
            atr_14=200.0,
            rsi_14=50.0,
            adx_14=25.0,
            liquidation_notional_5m=0.0,
            liquidation_side_5m=None,
            liquidation_count_5m=0,
        )
        self.assertEqual(e.symbol, "BTC")


if __name__ == "__main__":
    unittest.main(verbosity=2)
