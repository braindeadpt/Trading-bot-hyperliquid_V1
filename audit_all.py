#!/usr/bin/env python3
"""Quick component health check for the trading bot."""
import sys, traceback

print("=== TRADING BOT COMPONENT HEALTH CHECK ===\n")

# 1. Config
print("1. Config loading...")
try:
    from src.utils.config import load_config
    cfg = load_config("config/settings.yaml")
    print("   [OK] Config loads")
except Exception as e:
    print(f"   [FAIL] {e}")

# 2. Database + WAL
print("2. Database (WAL mode)...")
try:
    from src.data.database import Database
    db = Database("data/test_health.db")
    conn = db._conn()
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    print(f"   [OK] Database OK — journal_mode={mode}")
except Exception as e:
    print(f"   [FAIL] {e}")

# 3. DataBus
print("3. DataBus...")
try:
    from src.exchanges.hyperliquid_ws import DataBus
    bus = DataBus()
    print("   [OK] DataBus OK")
except Exception as e:
    print(f"   [FAIL] {e}")

# 4. CandleBuilder
print("4. CandleBuilder...")
try:
    from src.data.candle_builder import CandleBuilder
    from src.exchanges.hyperliquid_ws import DataBus
    cb = CandleBuilder(DataBus(), ["BTC"], {"BTC": 60})
    print("   [OK] CandleBuilder OK")
except Exception as e:
    print(f"   [FAIL] {e}")

# 5. Portfolio
print("5. Portfolio...")
try:
    from src.core.portfolio import PortfolioState
    p = PortfolioState(100000.0)
    print("   [OK] Portfolio OK")
except Exception as e:
    print(f"   [FAIL] {e}")

# 6. RiskManager
print("6. RiskManager...")
try:
    from src.core.risk_manager import RiskManager
    from src.data.database import Database
    from src.utils.config import load_config
    rm = RiskManager(load_config("config/settings.yaml"), Database("data/test_health.db"))
    print("   [OK] RiskManager OK (with db)")
except Exception as e:
    print(f"   [FAIL] {e}")

# 7. ExecutionEngine
print("7. ExecutionEngine...")
try:
    from src.core.execution import ExecutionEngine
    from src.data.database import Database
    from src.utils.config import load_config
    ex = ExecutionEngine(load_config("config/settings.yaml"), Database("data/test_health.db"))
    print("   [OK] ExecutionEngine OK")
except Exception as e:
    print(f"   [FAIL] {e}")

# 8. WS client
print("8. Hyperliquid WS client...")
try:
    from src.exchanges.hyperliquid_ws import HyperliquidWSClient
    print("   [OK] HyperliquidWSClient OK")
except Exception as e:
    print(f"   [FAIL] {e}")

# 9. Strategy instantiation
print("9. Strategies...")
from src.utils.config import load_config
from src.exchanges.hyperliquid_ws import DataBus
cfg = load_config("config/settings.yaml")
bus = DataBus()
strats = [
    ("TrendFollow", "src.strategies.trend_follow", "TrendFollow"),
    ("MeanReversion", "src.strategies.mean_reversion", "MeanReversion"),
    ("FundingArbitrage", "src.strategies.funding_arbitrage", "FundingArbitrage"),
    ("VWAPDeviation", "src.strategies.vwap_deviation", "VWAPDeviation"),
    ("VolatilityBreakout", "src.strategies.volatility_breakout", "VolatilityBreakout"),
    ("LiquidationCatcher", "src.strategies.liquidation_catcher", "LiquidationCatcher"),
    ("DonchianBreakout", "src.strategies.donchian_breakout", "DonchianBreakout"),
    ("OrderBookScalper", "src.strategies.orderbook_scalper", "OrderBookScalper"),
    ("CVDOrderFlow", "src.strategies.cvd_orderflow", "CVDOrderFlow"),
    ("LeadLag", "src.strategies.lead_lag", "LeadLag"),
    # v3.1.20
    ("SpotPerpCarry", "src.strategies.spot_perp_carry", "SpotPerpCarry"),
    ("RangeGrid", "src.strategies.range_grid", "RangeGrid"),
    ("TrendPyramid", "src.strategies.trend_pyramid", "TrendPyramid"),
    ("FundingMomentum", "src.strategies.funding_momentum", "FundingMomentum"),
]
for name, mod, cls in strats:
    try:
        m = __import__(mod, fromlist=[cls])
        s = getattr(m, cls)(cfg.get(f"strategy.{name.lower().replace(' ', '')}", {}))
        print(f"   [OK] {name} OK")
    except Exception as e:
        print(f"   [FAIL] {name}: {e}")

# 10. Signal generation
print("10. Strategy signal generation...")
from src.strategies.base import MarketEvent
import time
event = MarketEvent(
    symbol="BTC", timestamp_ms=int(time.time()*1000), price=81000.0,
    funding=-0.0001, predicted_funding=0.0, oi_total=30000.0, oi_delta=0.0,
    volume_1m=1e6, bid_ask_imbalance=0.0, vwap_15m=81000.0,
    funding_avg=-0.0001, funding_weighted=-0.0001, predicted_funding_avg=0.0,
    oi_total_aggregated=1e9, oi_exchange_count=3, orderbook_spread_pct=0.001,
    orderbook_oir=0.5, orderbook_depth_quality=0.5, orderbook_bid_ask_ratio=1.0,
    orderbook_largest_bid_wall=80900.0, orderbook_largest_ask_wall=81100.0,
    ema_20=81000.0, atr_14=200.0, rsi_14=50.0, adx_14=25.0,
    liquidation_notional_5m=0.0, liquidation_side_5m=None, liquidation_count_5m=0,
)
for name, mod, cls in strats:
    try:
        m = __import__(mod, fromlist=[cls])
        s = getattr(m, cls)(cfg.get(f"strategy.{name.lower().replace(' ', '')}", {}))
        sig = s.on_data(event)
        status = "SIGNAL" if sig else "no signal"
        print(f"   [OK] {name}: {status}")
    except Exception as e:
        print(f"   [FAIL] {name}: {e}")

print("\n=== HEALTH CHECK COMPLETE ===")
