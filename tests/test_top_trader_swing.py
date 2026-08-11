"""Unit tests for TopTrader bias store, virtual book, offline bias_flip."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.exchanges.top_trader_tracker import TopTraderSymbolSnapshot
from src.research.shadow_outcome_evaluator import (
    EXIT_BIAS_FLIP,
    EXIT_SL,
    EXIT_TIMEOUT,
    resolve_max_hold_ms,
    simulate_decision,
)
from src.research.shadow_recorder import ShadowDecision, build_enriched_market_snapshot
from src.research.top_trader_store import TopTraderStore
from src.research.top_trader_virtual_book import TopTraderVirtualBook
from src.utils.config import Config

pytestmark = pytest.mark.unit


def _snap(
    symbol: str,
    *,
    bias: float,
    n_long: int = 4,
    n_short: int = 1,
    long_n: float = 400_000,
    short_n: float = 50_000,
    ts: int = 1_000_000,
) -> TopTraderSymbolSnapshot:
    tot = long_n + short_n
    return TopTraderSymbolSnapshot(
        symbol=symbol,
        n_wallets=n_long + n_short,
        n_long=n_long,
        n_short=n_short,
        long_notional_usd=long_n,
        short_notional_usd=short_n,
        net_bias=bias,
        long_frac=(long_n / tot) if tot else 0.0,
        updated_ms=ts,
    )


def test_bias_sample_persist_and_load(tmp_path: Path) -> None:
    db = ResearchDatabase(tmp_path / "tt.db")
    store = TopTraderStore(db)
    n = store.persist_bias_samples(
        [
            _snap("BTC", bias=0.7, ts=1000),
            _snap("ETH", bias=-0.6, ts=1000),
        ]
    )
    assert n == 2
    rows = store.load_bias_samples("BTC", start_ms=0, end_ms=5000)
    assert len(rows) == 1
    assert rows[0]["net_bias"] == pytest.approx(0.7)
    latest = store.latest_bias_by_symbol()
    assert "BTC" in latest and "ETH" in latest


def test_virtual_book_open_flip_and_sl(tmp_path: Path) -> None:
    store = TopTraderStore(ResearchDatabase(tmp_path / "virt.db"))
    book = TopTraderVirtualBook(
        bias_threshold=0.55,
        min_wallets=3,
        min_notional_usd=10_000,
        max_hold_ms=10_000,
        stop_loss_pct=0.04,
        take_profit_pct=0.10,
        signal_throttle_ms=0,
        flip_confirm_polls=1,
        store=store,
    )
    # Open long on strong bias
    closed = book.on_snapshots(
        {"BTC": _snap("BTC", bias=0.8, ts=1_000_000)},
        prices={"BTC": 50_000.0},
    )
    assert closed == []
    assert len(book.open_positions()) == 1
    assert book.open_positions()[0]["side"] == "long"

    # Flip to short bias → close
    closed = book.on_snapshots(
        {"BTC": _snap("BTC", bias=-0.8, n_long=1, n_short=4, long_n=50_000, short_n=400_000, ts=1_000_100)},
        prices={"BTC": 50_100.0},
    )
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "bias_flip"
    assert book.open_positions() == []

    # Re-open then SL
    book.on_snapshots(
        {"BTC": _snap("BTC", bias=0.8, ts=2_000_000)},
        prices={"BTC": 50_000.0},
    )
    ev = book.on_price("BTC", 47_000.0, 2_000_500)  # -6% < -4% SL
    assert ev is not None
    assert ev["exit_reason"] == "stop_loss"


def test_virtual_book_ignores_thin_false_flip(tmp_path: Path) -> None:
    """Partial poll (1 wallet, bias=-1) must NOT close a solid long."""
    store = TopTraderStore(ResearchDatabase(tmp_path / "thin.db"))
    book = TopTraderVirtualBook(
        bias_threshold=0.55,
        min_wallets=3,
        min_notional_usd=10_000,
        max_hold_ms=10_000_000,
        stop_loss_pct=0.50,
        take_profit_pct=0.50,
        signal_throttle_ms=0,
        flip_confirm_polls=2,
        store=store,
    )
    book.on_snapshots(
        {"ETH": _snap("ETH", bias=0.8, n_long=1, n_short=2, long_n=20_000_000, short_n=2_000_000)},
        prices={"ETH": 1900.0},
    )
    assert len(book.open_positions()) == 1

    # One-wallet garbage snap (the production bug)
    closed = book.on_snapshots(
        {
            "ETH": _snap(
                "ETH",
                bias=-1.0,
                n_long=0,
                n_short=1,
                long_n=0,
                short_n=2_400_000,
                ts=2_000_000,
            )
        },
        prices={"ETH": 1860.0},
    )
    assert closed == []
    assert len(book.open_positions()) == 1

    # Real flip needs coverage + 2 confirms
    opposing = _snap(
        "ETH",
        bias=-0.8,
        n_long=1,
        n_short=4,
        long_n=50_000,
        short_n=400_000,
        ts=3_000_000,
    )
    assert book.on_snapshots({"ETH": opposing}, prices={"ETH": 1860.0}) == []
    closed = book.on_snapshots(
        {"ETH": _snap("ETH", bias=-0.85, n_long=1, n_short=4, long_n=50_000, short_n=450_000, ts=3_000_100)},
        prices={"ETH": 1855.0},
    )
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "bias_flip"


def test_virtual_book_timeout(tmp_path: Path) -> None:
    store = TopTraderStore(ResearchDatabase(tmp_path / "to.db"))
    book = TopTraderVirtualBook(
        bias_threshold=0.55,
        min_wallets=3,
        min_notional_usd=10_000,
        max_hold_ms=1_000,
        stop_loss_pct=0.50,
        take_profit_pct=0.50,
        signal_throttle_ms=0,
        store=store,
    )
    book.on_snapshots(
        {"BTC": _snap("BTC", bias=0.8, ts=1_000_000)},
        prices={"BTC": 50_000.0},
    )
    # Force entry_ts into the past via mutating open book
    pos = book._open["BTC"]
    pos.entry_ts_ms = 1_000_000
    ev = book.on_price("BTC", 50_050.0, 1_002_000)
    assert ev is not None
    assert ev["exit_reason"] == EXIT_TIMEOUT


def test_simulate_decision_bias_flip() -> None:
    snap = build_enriched_market_snapshot(
        price=100.0,
        confidence=0.7,
        stop_loss_pct=0.20,
        take_profit_pct=0.40,
        size_pct=0.01,
        metadata={"bias_threshold": 0.55},
    )
    decision = ShadowDecision(
        symbol="BTC",
        strategy="TopTraderFlow",
        variant="phase08_shadow",
        side="long",
        would_enter=True,
        reason="test",
        timestamp_ms=1_000_000,
        market_snapshot=snap,
        row_id=1,
    )
    candles = [
        Candle("BTC", 1_060_000, 100.0, 101.0, 99.0, 100.5, 1.0),
        Candle("BTC", 1_120_000, 100.5, 101.0, 99.5, 100.0, 1.0),
    ]
    samples = [
        {"timestamp_ms": 1_090_000, "net_bias": -0.7},
    ]
    out = simulate_decision(
        decision,
        candles,
        max_hold_ms=24 * 3_600_000,
        bias_samples=samples,
        bias_threshold=0.55,
    )
    assert out.evaluated
    assert out.exit_reason == EXIT_BIAS_FLIP


def test_simulate_decision_sl_without_samples() -> None:
    snap = build_enriched_market_snapshot(
        price=100.0,
        confidence=0.7,
        stop_loss_pct=0.02,
        take_profit_pct=0.40,
        size_pct=0.01,
    )
    decision = ShadowDecision(
        symbol="BTC",
        strategy="TopTraderFlow",
        variant="phase08_shadow",
        side="long",
        would_enter=True,
        reason="test",
        timestamp_ms=1_000_000,
        market_snapshot=snap,
        row_id=2,
    )
    candles = [
        Candle("BTC", 1_060_000, 100.0, 100.0, 97.0, 97.5, 1.0),
    ]
    out = simulate_decision(
        decision,
        candles,
        max_hold_ms=24 * 3_600_000,
        bias_samples=[],
    )
    assert out.evaluated
    assert out.exit_reason == EXIT_SL


def test_top_trader_max_hold_resolve() -> None:
    cfg = Config({"strategy": {"top_trader_flow": {"max_hold_hours": 120}}})
    assert resolve_max_hold_ms("TopTraderFlow", cfg) == 120 * 3_600_000


def test_top_traders_panel_empty_wallets() -> None:
    from src.exchanges.top_trader_tracker import set_tracker
    from src.research.top_trader_panel import build_top_traders_panel_payload
    from src.research.top_trader_virtual_book import set_virtual_book

    set_tracker(None)
    set_virtual_book(None)
    payload = build_top_traders_panel_payload(config=Config({
        "strategy": {"top_trader_flow": {"enabled": True, "bias_threshold": 0.55}}
    }))
    assert payload["wallets_configured"] == 0
    assert payload["empty_reason"]
    assert "snapshots" in payload
