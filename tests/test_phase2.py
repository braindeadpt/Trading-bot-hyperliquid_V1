"""Phase 2 tests: TCA, factory enabled flags, OI ratio priority."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.tca import estimate_expected_edge_pct, passes_tca_check, round_trip_cost_pct
from src.strategies.base import MarketEvent, Signal
from src.strategies.factory import build_ensemble, build_sub_strategies
from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.liquidation_catcher import LiquidationCatcher
from src.strategies.mean_reversion import MeanReversion
from src.strategies.orderbook_scalper import OrderBookScalper
from src.utils.config import load_config


def test_tca_rejects_low_edge() -> None:
    signal = Signal(
        strategy="OrderBookScalper",
        symbol="BTC",
        side="long",
        confidence=0.6,
        size_pct=0.005,
        stop_loss_pct=0.002,
        take_profit_pct=0.0015,  # 0.15% — below typical round-trip cost ~0.17%
    )
    fee = 0.00035  # 0.035%
    slip = 0.0005  # 0.05%
    cost = round_trip_cost_pct(fee, slip)
    edge = estimate_expected_edge_pct(signal)
    assert edge == 0.0015
    ok, reason = passes_tca_check(signal, fee, slip, min_edge_buffer_pct=0.0005)
    assert not ok, reason
    assert cost > 0


def test_tca_accepts_sufficient_edge() -> None:
    signal = Signal(
        strategy="VWAPDeviation",
        symbol="BTC",
        side="long",
        confidence=0.7,
        size_pct=0.01,
        stop_loss_pct=0.01,
        take_profit_pct=0.015,
    )
    ok, _ = passes_tca_check(signal, 0.00035, 0.0005, min_edge_buffer_pct=0.0005)
    assert ok


def test_factory_respects_enabled_flags() -> None:
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    subs = build_sub_strategies(cfg)
    names = {s.name for s in subs}
    # Active paper stack (settings.yaml v3.1.42+)
    assert "VolatilityBreakout" in names
    assert "VWAPDeviation" in names
    assert "ChecklistMeta" in names
    # Killed / dormant: enabled=false and auto_enable=false → not loaded by factory
    assert "OrderBookScalper" not in names
    assert "FundingArbitrage" not in names
    assert "LiquidationCatcher" not in names
    assert "SmartMoneyFlow" not in names

    mean_rev = cfg.get("strategy.mean_reversion", {}) or {}
    if mean_rev.get("enabled", True):
        assert "FundingExtreme" in names
    else:
        assert "FundingExtreme" not in names

    # auto_enable lifecycle still works when explicitly configured (see tests below)
    ob = OrderBookScalper({"enabled": False, "auto_enable": True})
    assert ob.AUTO_ENABLE is True
    assert ob.is_active() is False

    arb = FundingArbitrage({"enabled": False, "auto_enable": True})
    assert arb.AUTO_ENABLE is True
    assert arb.is_active() is False

    liq = LiquidationCatcher({"enabled": False, "auto_enable": True})
    assert liq.AUTO_ENABLE is True
    assert liq.is_active() is False

    ensemble = build_ensemble(cfg)
    total_weight = sum(w.weight for w in ensemble._weights.values())
    assert abs(total_weight - 1.0) < 1e-6


def test_mean_reversion_prefers_binance_oi_ratio() -> None:
    strat = MeanReversion({})
    event = MarketEvent(
        symbol="BTC",
        price=100_000.0,
        timestamp_ms=1,
        oi_long_ratio=0.72,
        oi_total=1_000_000.0,
        funding=0.0005,
        predicted_funding=0.0005,
    )
    ratio, is_real = strat._estimate_oi_ratio(event)
    assert ratio == 0.72
    assert is_real is True


def test_orderbook_scalper_auto_enables_on_tight_book() -> None:
    strat = OrderBookScalper({
        "enabled": False,
        "auto_enable": True,
        "take_profit_pct": 0.0025,
        "auto_enable_taker_fee_pct": 0.00035,
        "auto_enable_slippage_pct": 0.0005,
        "auto_enable_min_edge_buffer_pct": 0.0005,
        "auto_enable_max_book_spread_pct": 0.0004,
        "auto_disable_max_book_spread_pct": 0.0008,
        "viability_check_interval_ms": 0,
    })
    assert not strat.is_active()

    wide = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=1,
        orderbook_spread_pct=0.0010,
        orderbook_bid_ask_ratio=1.6,
    )
    assert strat.on_data(wide) is None
    assert not strat.is_active()

    tight = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=2,
        orderbook_spread_pct=0.0002,
        orderbook_bid_ask_ratio=1.6,
    )
    sig = strat.on_data(tight)
    assert sig is not None
    assert sig.side == "long"
    assert strat.is_active()


def test_orderbook_scalper_auto_disables_on_wide_book() -> None:
    strat = OrderBookScalper({
        "enabled": False,
        "auto_enable": True,
        "take_profit_pct": 0.0025,
        "auto_enable_max_book_spread_pct": 0.0004,
        "auto_disable_max_book_spread_pct": 0.0008,
        "viability_check_interval_ms": 0,
    })
    strat._auto_active = True
    strat._latest_book_spread = {"BTC": 0.0002}

    wide = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=1,
        orderbook_spread_pct=0.0010,
        orderbook_bid_ask_ratio=1.0,
    )
    assert strat.on_data(wide) is None
    assert not strat.is_active()


def test_funding_arbitrage_auto_enables_on_spread() -> None:
    strat = FundingArbitrage({
        "enabled": False,
        "auto_enable": True,
        "auto_enable_min_net_spread": 0.0008,
        "auto_disable_net_spread": 0.0005,
        "spread_check_interval_ms": 0,
    })
    assert not strat.is_active()

    low_btc = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=1,
        funding=0.0001, predicted_funding=0.0001,
    )
    low_eth = MarketEvent(
        symbol="ETH", price=3_000.0, timestamp_ms=2,
        funding=0.00015, predicted_funding=0.00015,
    )
    assert strat.on_data(low_btc) is None
    assert strat.on_data(low_eth) is None
    assert not strat.is_active()

    wide_eth = MarketEvent(
        symbol="ETH", price=3_000.0, timestamp_ms=3,
        funding=0.0010, predicted_funding=0.0010,
    )
    assert strat.on_data(wide_eth) is None
    assert strat.is_active()

    narrow_eth = MarketEvent(
        symbol="ETH", price=3_000.0, timestamp_ms=4,
        funding=0.00012, predicted_funding=0.00012,
    )
    assert strat.on_data(narrow_eth) is None
    assert not strat.is_active()


def test_funding_arbitrage_stays_active_with_open_pair() -> None:
    strat = FundingArbitrage({
        "enabled": False,
        "auto_enable": True,
        "auto_enable_min_net_spread": 0.0008,
        "auto_disable_net_spread": 0.0005,
        "spread_check_interval_ms": 0,
    })
    strat._auto_active = True
    strat._active_pair = ("BTC", "ETH")
    strat._latest_funding = {"BTC": 0.0001, "ETH": 0.00012}

    event = MarketEvent(
        symbol="ETH", price=3_000.0, timestamp_ms=1,
        funding=0.00012, predicted_funding=0.00012,
    )
    strat._update_auto_activation(event)
    assert strat.is_active() is True  # narrow spread but open pair — no auto-disable


def test_liquidation_catcher_auto_enables_on_feed_ready() -> None:
    strat = LiquidationCatcher({"enabled": False, "auto_enable": True})
    assert not strat.is_active()

    dormant = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=1,
        liquidation_feed_ready=False,
    )
    assert strat.on_data(dormant) is None
    assert not strat.is_active()

    ready = MarketEvent(
        symbol="BTC", price=100_000.0, timestamp_ms=2,
        liquidation_feed_ready=True,
        liquidation_notional_5m=100.0,
        liquidation_side_5m="long",
        liquidation_data_source="binance",
    )
    assert strat.on_data(ready) is None  # notional too small, but now active
    assert strat.is_active()


if __name__ == "__main__":
    test_tca_rejects_low_edge()
    test_tca_accepts_sufficient_edge()
    test_factory_respects_enabled_flags()
    test_mean_reversion_prefers_binance_oi_ratio()
    test_orderbook_scalper_auto_enables_on_tight_book()
    test_orderbook_scalper_auto_disables_on_wide_book()
    test_funding_arbitrage_auto_enables_on_spread()
    test_funding_arbitrage_stays_active_with_open_pair()
    test_liquidation_catcher_auto_enables_on_feed_ready()
    print("ALL PHASE 2 TESTS PASSED [OK]")
