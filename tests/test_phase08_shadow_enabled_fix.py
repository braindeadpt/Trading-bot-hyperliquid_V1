"""Phase08 shadow factory: in-memory enabled overrides for dormant strategies."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.strategies.base import MarketEvent
from src.strategies.checklist_meta import ChecklistMeta
from src.strategies.cvd_orderflow import CVDOrderFlow
from src.strategies.factory import (
    _REGISTRY_BY_NAME,
    _instantiate_from_registry,
    build_phase08_strategies,
)
from src.strategies.funding_arbitrage import FundingArbitrage
from src.strategies.funding_momentum import FundingMomentum
from src.strategies.indicators import Candle
from src.strategies.orderbook_scalper import OrderBookScalper
from src.strategies.spot_perp_carry import SpotPerpCarry
from src.strategies.volatility_breakout import VolatilityBreakout
from src.strategies.vwap_deviation import VWAPDeviation
from src.utils.config import Config, compute_config_hash, load_config

pytestmark = pytest.mark.unit

# The five Phase08 shadow strategies that previously self-gated on enabled:false
_SHADOW_GATED = (
    ("CVDOrderFlow", CVDOrderFlow),
    ("OrderBookScalper", OrderBookScalper),
    ("FundingArbitrage", FundingArbitrage),
    ("FundingMomentum", FundingMomentum),
    ("SpotPerpCarry", SpotPerpCarry),
)


def _live_cfg() -> Config:
    return load_config(Path("config/settings.yaml"))


def _candle(ts: int, px: float = 100.0, buy: float = 1.0, sell: float = 0.5) -> Candle:
    return Candle(
        open=px,
        high=px * 1.001,
        low=px * 0.999,
        close=px,
        volume=buy + sell,
        timestamp_ms=ts,
        buy_volume=buy,
        sell_volume=sell,
    )


def _event(**kwargs) -> MarketEvent:
    base = dict(
        symbol="BTC",
        price=100.0,
        timestamp_ms=1_800_000_000_000,
        candle_1m=_candle(1_800_000_000_000),
        funding=0.0005,
        predicted_funding=0.0005,
        market_data_health="green",
    )
    base.update(kwargs)
    return MarketEvent(**base)


def test_shadow_overrides_do_not_mutate_live_config() -> None:
    cfg = _live_cfg()
    before = copy.deepcopy(cfg.get("strategy.cvd_orderflow", {}))
    assert before.get("enabled") is False
    inst = _instantiate_from_registry(
        cfg, "strategy.cvd_orderflow", CVDOrderFlow, force=True, shadow=True,
    )
    assert inst is not None
    assert inst.MANUAL_ENABLED is True
    after = cfg.get("strategy.cvd_orderflow", {})
    assert after.get("enabled") is False
    assert after == before


@pytest.mark.parametrize("name,cls", _SHADOW_GATED)
def test_non_shadow_force_stays_dormant(name: str, cls: type) -> None:
    cfg = _live_cfg()
    path, _ = _REGISTRY_BY_NAME[name]
    dormant = _instantiate_from_registry(cfg, path, cls, force=True, shadow=False)
    assert dormant is not None
    assert getattr(dormant, "MANUAL_ENABLED", True) is False
    if hasattr(dormant, "AUTO_ENABLE"):
        assert dormant.AUTO_ENABLE is False
    if hasattr(dormant, "is_active"):
        assert dormant.is_active() is False

    ev = _event(
        orderbook_bid_ask_ratio=2.0,
        orderbook_spread_pct=0.0001,
        predicted_funding=0.001,
        funding=0.001,
    )
    assert dormant.on_data(ev) is None


@pytest.mark.parametrize("name,cls", _SHADOW_GATED)
def test_shadow_force_bypasses_enabled_dormancy(name: str, cls: type) -> None:
    cfg = _live_cfg()
    path, _ = _REGISTRY_BY_NAME[name]
    active = _instantiate_from_registry(cfg, path, cls, force=True, shadow=True)
    assert active is not None
    assert getattr(active, "_shadow_instance", False) is True
    assert active.MANUAL_ENABLED is True
    if path in ("strategy.orderbook_scalper", "strategy.funding_arbitrage"):
        assert active.AUTO_ENABLE is True
    else:
        assert not hasattr(active, "AUTO_ENABLE")

    if name == "CVDOrderFlow":
        ev = _event(candle_1m=_candle(1_800_000_000_000))
        assert active.on_data(ev) is None  # warm-up, but past enabled gate
        st = active._get_state("BTC")
        assert len(st.bars_1m) == 1
    elif name == "OrderBookScalper":
        assert active.is_active() is True
        ev = _event(orderbook_bid_ask_ratio=2.5, orderbook_spread_pct=0.0001)
        sig = active.on_data(ev)
        assert sig is not None
        assert sig.strategy == "OrderBookScalper"
    elif name == "FundingArbitrage":
        assert active.is_active() is True
        # Past enabled gate: funding cache updates even if pair scan needs more symbols
        ev = _event(predicted_funding=0.001, funding=0.001, market_data_health="green")
        active.on_data(ev)
        assert "BTC" in active._latest_funding
    elif name == "FundingMomentum":
        # Past enabled gate: funding history updates
        ev1 = _event(timestamp_ms=1_800_000_000_000, predicted_funding=-0.001)
        ev2 = _event(timestamp_ms=1_800_000_060_000, predicted_funding=0.001)
        active.on_data(ev1)
        active.on_data(ev2)
        st = active._get_state("BTC")
        assert len(st.funding_history) >= 1
    elif name == "SpotPerpCarry":
        ev = _event(predicted_funding=0.001, funding=0.001)
        active.on_data(ev)
        st = active._get_state("BTC")
        assert len(st.funding_history) >= 1
        dormant = _instantiate_from_registry(
            cfg, path, cls, force=True, shadow=False,
        )
        dormant.on_data(ev)
        assert len(dormant._get_state("BTC").funding_history) == 0


def test_checklist_meta_unchanged_no_enabled_gate() -> None:
    cfg = _live_cfg()
    path, cls = _REGISTRY_BY_NAME["ChecklistMeta"]
    shadow = _instantiate_from_registry(cfg, path, cls, force=True, shadow=True)
    assert shadow is not None
    assert isinstance(shadow, ChecklistMeta)
    assert not hasattr(shadow, "MANUAL_ENABLED")
    ts = 1_800_000_000_000
    c15 = Candle(100, 101, 99, 100.5, 10.0, ts)
    ev = _event(candle_15m=c15, timestamp_ms=ts, adx_14=25.0)
    # Still reaches real evaluation (candle ingest) — no new enabled short-circuit
    shadow.on_data(ev)
    st = shadow._get_state("BTC")
    assert len(st.candles_15m) == 1


def test_execution_vb_vwap_shadow_false_does_not_force_enable() -> None:
    """Execution path must keep YAML enabled flags; no shadow override."""
    cfg = _live_cfg()
    vb_path = "strategy.volatility_breakout"
    vwap_path = "strategy.vwap_deviation"
    assert cfg.get(vb_path, {}).get("enabled") is True
    assert cfg.get(vwap_path, {}).get("enabled") is True

    vb = _instantiate_from_registry(
        cfg, vb_path, VolatilityBreakout, force=True, shadow=False,
    )
    vwap = _instantiate_from_registry(
        cfg, vwap_path, VWAPDeviation, force=True, shadow=False,
    )
    assert vb is not None and vwap is not None
    assert getattr(vb, "_shadow_instance", False) is False
    assert cfg.get(vb_path, {}).get("enabled") is True
    assert cfg.get(vwap_path, {}).get("enabled") is True

    # Explicit regression: shadow=False + enabled:false must stay dormant
    # for a gated strategy (must NOT flip like the shadow path).
    raw = copy.deepcopy(cfg.raw)
    raw["strategy"]["cvd_orderflow"] = {
        **(raw["strategy"].get("cvd_orderflow") or {}),
        "enabled": False,
    }
    muted = Config(raw)
    cvd = _instantiate_from_registry(
        muted, "strategy.cvd_orderflow", CVDOrderFlow, force=True, shadow=False,
    )
    assert cvd.MANUAL_ENABLED is False
    assert cvd.on_data(_event()) is None


def test_phase08_build_shadow_instances_are_enabled() -> None:
    cfg = _live_cfg()
    _execution, shadow = build_phase08_strategies(cfg)
    by_name = {s.name: s for s in shadow}
    for name, _cls in _SHADOW_GATED:
        inst = by_name[name]
        assert inst.MANUAL_ENABLED is True
        if name in ("OrderBookScalper", "FundingArbitrage"):
            assert inst.AUTO_ENABLE is True


def test_shadow_factory_does_not_mutate_config_or_hash() -> None:
    """Factory shadow overrides must not touch live cfg / config_hash.

    Does not require gitignored ``data/research/*preregister.json`` files
    (those are absent on CI).
    """
    cfg = _live_cfg()
    h_before = compute_config_hash(cfg)
    cvd_before = copy.deepcopy(cfg.get("strategy.cvd_orderflow", {}))
    obs_before = copy.deepcopy(cfg.get("strategy.orderbook_scalper", {}))

    build_phase08_strategies(cfg)

    assert compute_config_hash(cfg) == h_before
    assert cfg.get("strategy.cvd_orderflow", {}) == cvd_before
    assert cfg.get("strategy.orderbook_scalper", {}) == obs_before
    assert cfg.get("strategy.cvd_orderflow", {}).get("enabled") is False
    assert cfg.get("strategy.orderbook_scalper", {}).get("auto_enable") is False
