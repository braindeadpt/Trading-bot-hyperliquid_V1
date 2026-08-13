"""Unit tests for TopTraderFlow + aggregate snapshots."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from src.exchanges.top_trader_tracker import (
    TopTraderSymbolSnapshot,
    TopTraderTracker,
    _iter_positions,
    set_tracker,
)
from src.strategies.base import MarketEvent, Position
from src.strategies.top_trader_flow import TopTraderFlow

pytestmark = pytest.mark.unit


def test_iter_positions_basic() -> None:
    raw = {
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "0.5",
                    "entryPx": "50000",
                    "positionValue": "25000",
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "-2",
                    "entryPx": "3000",
                    "positionValue": "6000",
                }
            },
        ]
    }
    rows = _iter_positions(raw, coins={"BTC", "ETH"})
    assert len(rows) == 2
    by = {r["coin"]: r for r in rows}
    assert by["BTC"]["side"] == "long"
    assert by["ETH"]["side"] == "short"


def test_top_trader_flow_signals_on_bias() -> None:
    set_tracker(None)
    tracker = TopTraderTracker(top_n=5, enabled=True)
    tracker._snapshots = {
        "BTC": TopTraderSymbolSnapshot(
            symbol="BTC",
            n_wallets=5,
            n_long=4,
            n_short=1,
            long_notional_usd=400_000,
            short_notional_usd=50_000,
            net_bias=(400_000 - 50_000) / 450_000,
            long_frac=400_000 / 450_000,
            updated_ms=1_000_000,
        )
    }
    set_tracker(tracker)
    strat = TopTraderFlow(
        {
            "enabled": True,
            "bias_threshold": 0.5,
            "min_wallets_with_position": 3,
            "min_aggregate_notional_usd": 10_000,
            "signal_throttle_ms": 0,
            "max_snapshot_age_ms": 60_000,
        }
    )
    ev = MarketEvent(symbol="BTC", price=50_000, timestamp_ms=1_000_500)
    sig = strat.on_data(ev)
    assert sig is not None
    assert sig.side == "long"
    assert sig.strategy == "TopTraderFlow"
    set_tracker(None)


def test_top_trader_flow_exit_on_flip() -> None:
    tracker = TopTraderTracker()
    tracker._snapshots = {
        "BTC": TopTraderSymbolSnapshot(
            symbol="BTC",
            n_wallets=4,
            n_long=0,
            n_short=4,
            long_notional_usd=0,
            short_notional_usd=200_000,
            net_bias=-1.0,
            long_frac=0.0,
            updated_ms=1_000_000,
        )
    }
    set_tracker(tracker)
    strat = TopTraderFlow({"bias_threshold": 0.5})
    pos = Position(
        symbol="BTC",
        side="long",
        entry_price=50_000,
        size=0.01,
        entry_time_ms=1,
    )
    ev = MarketEvent(symbol="BTC", price=49_000, timestamp_ms=1_000_100)
    ex = strat.on_position(pos, ev)
    assert ex is not None
    assert "bias_flip" in ex.reason
    set_tracker(None)


def test_poll_once_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        async def clearinghouse_state(self, address: str) -> Dict[str, Any]:
            if address.endswith("1"):
                return {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "BTC",
                                "szi": "1",
                                "entryPx": "50000",
                                "positionValue": "50000",
                            }
                        }
                    ]
                }
            return {
                "assetPositions": [
                    {
                        "position": {
                            "coin": "BTC",
                            "szi": "-0.5",
                            "entryPx": "50000",
                            "positionValue": "25000",
                        }
                    }
                ]
            }

    tr = TopTraderTracker(
        wallets=["0x1", "0x2"],
        symbols=["BTC"],
        top_n=10,
        min_notional_usd=1_000,
        request_delay_sec=0.0,
        min_publish_wallets=2,
    )
    tr._persist_samples = False

    async def _run() -> None:
        await tr.bind_client(_Client())
        snaps = await tr.poll_once()
        assert "BTC" in snaps
        assert snaps["BTC"].n_long == 1
        assert snaps["BTC"].n_short == 1
        assert snaps["BTC"].long_notional_usd == 50_000
        assert snaps["BTC"].short_notional_usd == 25_000

    asyncio.run(_run())


# ── Forced-liquidation hypothesis: wallet position collapse detection ──


def test_detect_collapses_no_change() -> None:
    tr = TopTraderTracker()
    prev = {"0xaaa": {"BTC": 100_000.0}}
    cur = {"0xaaa": {"BTC": 99_000.0}}
    assert tr.detect_collapses(prev, cur, now_ms=1_000) == []


def test_detect_collapses_full_wipe() -> None:
    tr = TopTraderTracker()
    prev = {"0xaaa": {"BTC": 250_000.0}}
    cur = {}  # position gone entirely
    evts = tr.detect_collapses(prev, cur, now_ms=1_000)
    assert len(evts) == 1
    e = evts[0]
    assert e["symbol"] == "BTC"
    assert e["side"] == "long"
    assert e["from_notional"] == 250_000.0
    assert e["to_notional"] == 0.0
    assert e["drop_pct"] == 1.0
    assert e["ts_ms"] == 1_000
    assert e["wallet"] == "0xaaa"


def test_detect_collapses_short_position_wipe() -> None:
    tr = TopTraderTracker()
    prev = {"0xaaa": {"ETH": -120_000.0}}  # short 120k
    cur = {"0xaaa": {"ETH": -5_000.0}}     # mostly gone
    evts = tr.detect_collapses(prev, cur, now_ms=1_000)
    assert len(evts) == 1
    assert evts[0]["side"] == "short"
    assert evts[0]["drop_pct"] >= 0.9


def test_detect_collapses_ignores_small_positions() -> None:
    tr = TopTraderTracker(collapse_min_from_notional_usd=50_000.0)
    prev = {"0xaaa": {"BTC": 30_000.0}}  # below min
    cur = {}
    assert tr.detect_collapses(prev, cur, now_ms=1_000) == []


def test_detect_collapses_ignores_partial_reductions() -> None:
    """A 50% reduction is a normal exit, not a forced liquidation."""
    tr = TopTraderTracker(collapse_drop_pct=0.9)
    prev = {"0xaaa": {"BTC": 200_000.0}}
    cur = {"0xaaa": {"BTC": 100_000.0}}
    assert tr.detect_collapses(prev, cur, now_ms=1_000) == []


def test_detect_collapses_persists_via_store(tmp_path, monkeypatch) -> None:
    """poll_once persists detected collapses to the research store."""
    import src.research.top_trader_store as store_mod

    class _Client:
        async def clearinghouse_state(self, address: str):
            return {"assetPositions": []}  # wallet fully flat now

    recorded: list = []
    class _FakeStore:
        def persist_collapse_event(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr(store_mod, "TopTraderStore", lambda: _FakeStore())

    tr = TopTraderTracker(
        wallets=["0xaaa"], symbols=["BTC"], request_delay_sec=0.0,
        min_notional_usd=1_000,
    )
    tr._persist_samples = True
    tr._prev_wallet_positions = {"0xaaa": {"BTC": 250_000.0}}

    async def _run() -> None:
        await tr.bind_client(_Client())
        await tr.poll_once()

    asyncio.run(_run())
    assert len(recorded) == 1
    assert recorded[0]["symbol"] == "BTC"
    assert recorded[0]["from_notional"] == 250_000.0


def test_store_collapse_roundtrip(tmp_path) -> None:
    """Collapse events persist and reload from a temp research DB."""
    from src.data.research_database import ResearchDatabase
    from src.research.top_trader_store import TopTraderStore

    store = TopTraderStore(db=ResearchDatabase(str(tmp_path / "tt_research.db")))
    ok = store.persist_collapse_event(
        ts_ms=1_000_000, wallet="0xaaa", symbol="BTC", side="long",
        from_notional=250_000.0, to_notional=0.0, drop_pct=1.0,
    )
    assert ok is True
    events = store.collapse_events(symbol="BTC", since_ms=999_999)
    assert len(events) == 1
    assert events[0]["symbol"] == "BTC"
    assert events[0]["from_notional"] == 250_000.0
    assert events[0]["side"] == "long"
    # filters work
    assert store.collapse_events(symbol="ETH") == []
    assert store.collapse_events(since_ms=2_000_000) == []
