"""Intensive tests for the shadow outcome evaluator.

Hand-computed expected values for every deterministic case. Property checks
use a fixed seed. Promotion decisions will eventually depend on these numbers.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.data.database import Candle
from src.data.research_database import ResearchDatabase
from src.research.shadow_outcome_evaluator import (
    EXIT_GAP_SL,
    EXIT_GAP_TP,
    EXIT_SL,
    EXIT_TIMEOUT,
    EXIT_TP,
    SKIP_INSUFFICIENT_CANDLES,
    SKIP_MISSING_BRACKET,
    aggregate_scoreboard,
    evaluate_shadow_decisions,
    resolve_candle_exit,
    resolve_max_hold_ms,
    simulate_decision,
)
from src.research.shadow_recorder import (
    ShadowDecision,
    ShadowRecorder,
    build_enriched_market_snapshot,
    extract_bracket_params,
    parse_market_snapshot,
)
from src.strategies.base import MarketEvent, Signal
from src.utils.config import Config

pytestmark = [pytest.mark.unit, pytest.mark.integration_offline]


def _candle(
    ts: int,
    o: float,
    h: float,
    l: float,  # noqa: E741
    c: float,
    symbol: str = "BTC",
) -> Candle:
    return Candle(
        symbol=symbol,
        timestamp_ms=ts,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1.0,
    )


def _decision(
    *,
    side: str = "long",
    price: float = 100.0,
    stop: float = 0.01,
    take: float = 0.02,
    size: float = 0.01,
    ts: int = 1_000_000,
    strategy: str = "TestStrat",
    would_enter: bool = True,
    include_bracket: bool = True,
    row_id: int = 1,
) -> ShadowDecision:
    if include_bracket:
        snap = build_enriched_market_snapshot(
            price=price,
            confidence=0.7,
            stop_loss_pct=stop,
            take_profit_pct=take,
            size_pct=size,
            metadata={"k": "v"},
        )
    else:
        snap = {"price": price, "confidence": 0.7}
    return ShadowDecision(
        symbol="BTC",
        strategy=strategy,
        variant="phase08_shadow",
        side=side,
        would_enter=would_enter,
        reason="entry_signal",
        timestamp_ms=ts,
        market_snapshot=snap,
        row_id=row_id,
    )


# ── a. TP hit cleanly ────────────────────────────────────────────────────────

def test_a_tp_hit_cleanly_long_hand_computed() -> None:
    # Entry 100, stop 1%, TP 2% → SL=99, TP=102
    # Candle at +3min: range 100.5–102.5, closes 102.2 → TP at 102
    entry_ts = 1_000_000
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    candles = [
        _candle(entry_ts + 60_000, 100.0, 100.5, 99.8, 100.2),
        _candle(entry_ts + 120_000, 100.2, 101.0, 100.0, 100.8),
        _candle(entry_ts + 180_000, 100.8, 102.5, 100.5, 102.2),
    ]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.evaluated is True
    assert out.exit_reason == EXIT_TP
    assert out.exit_price == pytest.approx(102.0)
    # pnl_pct = (102-100)/100 = 0.02; R = 0.02/0.01 = 2.0
    assert out.pnl_pct == pytest.approx(0.02)
    assert out.r_multiple == pytest.approx(2.0)
    assert out.hold_minutes == pytest.approx(3.0)


# ── b. SL hit cleanly → R = -1.0 ─────────────────────────────────────────────

def test_b_sl_hit_cleanly_long_r_equals_minus_one() -> None:
    entry_ts = 1_000_000
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    candles = [
        _candle(entry_ts + 60_000, 100.0, 100.2, 98.8, 99.7),  # low=98.8 ≤ SL=99
    ]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_SL
    assert out.exit_price == pytest.approx(99.0)
    assert out.r_multiple == pytest.approx(-1.0)
    assert out.hold_minutes == pytest.approx(1.0)


# ── c. BOTH SL and TP in one candle → SL (conservative) ──────────────────────

def test_c_conservative_both_sl_and_tp_in_one_candle_resolves_as_sl() -> None:
    """CONSERVATIVE AMBIGUITY RULE: dual-touch candle → assume SL first."""
    entry_ts = 1_000_000
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    # Single candle spans 98–103 — both SL(99) and TP(102) touched
    candles = [_candle(entry_ts + 60_000, 100.0, 103.0, 98.0, 101.0)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_SL
    assert out.exit_price == pytest.approx(99.0)
    assert out.r_multiple == pytest.approx(-1.0)


# ── d. Gap-through SL at open → fill at open, R < -1 ─────────────────────────

def test_d_gap_through_sl_long_filled_at_open_worse_than_minus_one_r() -> None:
    entry_ts = 1_000_000
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    # Open at 98.0 < SL 99 → gap fill at 98
    candles = [_candle(entry_ts + 60_000, 98.0, 98.5, 97.5, 98.2)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_GAP_SL
    assert out.exit_price == pytest.approx(98.0)
    # pnl = (98-100)/100 = -0.02; R = -0.02/0.01 = -2.0
    assert out.r_multiple == pytest.approx(-2.0)
    assert out.r_multiple < -1.0


# ── e. Gap-through TP at open → fill at open, R better than planned ──────────

def test_e_gap_through_tp_long_filled_at_open_better_than_planned() -> None:
    entry_ts = 1_000_000
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    # Planned TP=102 (2R). Open gaps to 104 → fill at 104 = 4R
    candles = [_candle(entry_ts + 60_000, 104.0, 104.5, 103.5, 104.2)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_GAP_TP
    assert out.exit_price == pytest.approx(104.0)
    assert out.r_multiple == pytest.approx(4.0)
    assert out.r_multiple > 2.0  # better than planned 2R


# ── f. Max-hold timeout between levels ───────────────────────────────────────

def test_f_timeout_exit_at_close_pnl_sign_both_ways() -> None:
    entry_ts = 1_000_000
    max_hold = 3 * 60_000  # 3 minutes

    # Long, timeout with close above entry → positive pnl
    d_long = _decision(side="long", price=100.0, stop=0.05, take=0.10, ts=entry_ts)
    candles_up = [
        _candle(entry_ts + 60_000, 100.0, 100.5, 99.8, 100.3),
        _candle(entry_ts + 120_000, 100.3, 100.8, 100.1, 100.6),
        _candle(entry_ts + 180_000, 100.6, 101.0, 100.4, 100.9),  # hits deadline
    ]
    out_up = simulate_decision(d_long, candles_up, max_hold_ms=max_hold)
    assert out_up.exit_reason == EXIT_TIMEOUT
    assert out_up.exit_price == pytest.approx(100.9)
    assert out_up.pnl_pct > 0

    # Long, timeout with close below entry → negative pnl
    candles_dn = [
        _candle(entry_ts + 60_000, 100.0, 100.2, 99.5, 99.7),
        _candle(entry_ts + 120_000, 99.7, 99.9, 99.3, 99.4),
        _candle(entry_ts + 180_000, 99.4, 99.6, 99.0, 99.2),
    ]
    out_dn = simulate_decision(d_long, candles_dn, max_hold_ms=max_hold)
    assert out_dn.exit_reason == EXIT_TIMEOUT
    assert out_dn.exit_price == pytest.approx(99.2)
    assert out_dn.pnl_pct < 0


# ── g. Missing candles → insufficient_candles ────────────────────────────────

def test_g_missing_candles_skipped_insufficient() -> None:
    d = _decision(side="long", price=100.0, stop=0.01, take=0.02)
    out = simulate_decision(d, [], max_hold_ms=6 * 3600_000)
    assert out.evaluated is False
    assert out.skip_reason == SKIP_INSUFFICIENT_CANDLES


# ── h. Old-format row without stops ──────────────────────────────────────────

def test_h_old_format_row_missing_bracket_params() -> None:
    d = _decision(include_bracket=False)
    out = simulate_decision(
        d,
        [_candle(d.timestamp_ms + 60_000, 100.0, 101.0, 99.0, 100.5)],
        max_hold_ms=6 * 3600_000,
    )
    assert out.evaluated is False
    assert out.skip_reason == SKIP_MISSING_BRACKET
    assert extract_bracket_params(d.market_snapshot) is None


# ── i. Short-side mirrors of a–d ─────────────────────────────────────────────

def test_i_short_tp_hit_cleanly_hand_computed() -> None:
    # Entry 100, stop 1%, TP 2% → SL=101, TP=98
    entry_ts = 1_000_000
    d = _decision(side="short", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    candles = [
        _candle(entry_ts + 60_000, 100.0, 100.3, 99.5, 99.6),
        _candle(entry_ts + 120_000, 99.6, 99.7, 97.5, 98.0),  # low≤98 → TP
    ]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_TP
    assert out.exit_price == pytest.approx(98.0)
    # pnl = (100-98)/100 = 0.02; R = 2.0
    assert out.r_multiple == pytest.approx(2.0)


def test_i_short_sl_hit_cleanly_r_equals_minus_one() -> None:
    entry_ts = 1_000_000
    d = _decision(side="short", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    candles = [_candle(entry_ts + 60_000, 100.0, 101.5, 99.8, 101.2)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_SL
    assert out.exit_price == pytest.approx(101.0)
    assert out.r_multiple == pytest.approx(-1.0)


def test_i_short_conservative_both_sl_and_tp_resolves_as_sl() -> None:
    entry_ts = 1_000_000
    d = _decision(side="short", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    candles = [_candle(entry_ts + 60_000, 100.0, 102.0, 97.0, 99.0)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_SL
    assert out.exit_price == pytest.approx(101.0)
    assert out.r_multiple == pytest.approx(-1.0)


def test_i_short_gap_through_sl_at_open_worse_than_minus_one_r() -> None:
    entry_ts = 1_000_000
    d = _decision(side="short", price=100.0, stop=0.01, take=0.02, ts=entry_ts)
    # Open 102 > SL 101 → gap fill at 102; R = (100-102)/100 / 0.01 = -2.0
    candles = [_candle(entry_ts + 60_000, 102.0, 102.5, 101.5, 102.1)]
    out = simulate_decision(d, candles, max_hold_ms=6 * 3600_000)
    assert out.exit_reason == EXIT_GAP_SL
    assert out.exit_price == pytest.approx(102.0)
    assert out.r_multiple == pytest.approx(-2.0)
    assert out.r_multiple < -1.0


# ── j. PF / expectancy_R on 6 crafted outcomes ───────────────────────────────

def test_j_pf_and_expectancy_r_on_six_mixed_outcomes_exact() -> None:
    """
    Crafted R multiples: +2, +2, -1, -1, -1, +1
    gross_profit R = 5; gross_loss R = 3; PF = 5/3
    expectancy_R = (2+2-1-1-1+1)/6 = 2/6 = 1/3
    """
    entry_ts = 1_000_000
    max_hold = 60 * 60_000
    specs = [
        # (side path description) → via candles
        ("tp", 0.02),   # +2R
        ("tp", 0.02),   # +2R
        ("sl", -0.01),  # -1R
        ("sl", -0.01),
        ("sl", -0.01),
        ("tp_half", 0.01),  # +1R via TP at 1%
    ]
    outcomes = []
    for i, (kind, _) in enumerate(specs):
        stop, take = 0.01, 0.02
        d = _decision(
            side="long",
            price=100.0,
            stop=stop,
            take=take if kind != "tp_half" else 0.01,
            ts=entry_ts + i,
            row_id=i + 1,
        )
        if kind == "tp":
            candles = [_candle(entry_ts + i + 60_000, 100.0, 103.0, 100.0, 102.5)]
        elif kind == "tp_half":
            candles = [_candle(entry_ts + i + 60_000, 100.0, 101.5, 100.0, 101.2)]
        else:
            candles = [_candle(entry_ts + i + 60_000, 100.0, 100.2, 98.5, 99.0)]
        outcomes.append(simulate_decision(d, candles, max_hold_ms=max_hold))

    board = aggregate_scoreboard(
        "TestStrat",
        outcomes,
        max_hold_ms=max_hold,
        candle_source="synthetic",
        n_decisions=6,
    )
    assert board.n_evaluated == 6
    assert board.wins == 3
    assert board.losses == 3
    assert board.profit_factor == pytest.approx(5.0 / 3.0)
    assert board.expectancy_r == pytest.approx(1.0 / 3.0)


# ── k. Recorder round-trip ───────────────────────────────────────────────────

def test_k_recorder_round_trip_enriched_and_legacy() -> None:
    path = Path(tempfile.gettempdir()) / f"shadow_rt_{uuid.uuid4().hex}.db"
    db = ResearchDatabase(path)
    try:
        rec = ShadowRecorder(db)
        enriched = ShadowDecision(
            symbol="ETH",
            strategy="CVDOrderFlow",
            variant="phase08_shadow",
            side="long",
            would_enter=True,
            reason="entry_signal",
            timestamp_ms=2_000_000,
            market_snapshot=build_enriched_market_snapshot(
                price=3500.0,
                confidence=0.66,
                stop_loss_pct=0.015,
                take_profit_pct=0.03,
                size_pct=0.008,
                metadata={"atr_pct": 0.012},
            ),
        )
        legacy = ShadowDecision(
            symbol="ETH",
            strategy="CVDOrderFlow",
            variant="phase08_shadow",
            side="short",
            would_enter=True,
            reason="entry_signal",
            timestamp_ms=2_100_000,
            market_snapshot={"price": 3490.0, "confidence": 0.55},
        )
        rec.record(enriched)
        rec.record(legacy)
        loaded = rec.load_decisions(strategy="CVDOrderFlow")
        assert len(loaded) == 2
        e = loaded[0]
        assert e.market_snapshot is not None
        assert e.market_snapshot["stop_loss_pct"] == pytest.approx(0.015)
        assert e.market_snapshot["take_profit_pct"] == pytest.approx(0.03)
        assert e.market_snapshot["size_pct"] == pytest.approx(0.008)
        assert e.market_snapshot["metadata"]["atr_pct"] == pytest.approx(0.012)
        # Round-trip snapshot JSON is byte-identical to what we would dump
        dumped = json.dumps(enriched.market_snapshot, sort_keys=True)
        reloaded = json.dumps(e.market_snapshot, sort_keys=True)
        assert dumped == reloaded

        leg = loaded[1]
        assert extract_bracket_params(leg.market_snapshot) is None
        assert leg.market_snapshot["price"] == pytest.approx(3490.0)
        # Old-format load never errors
        assert parse_market_snapshot(None) == {}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


# ── l. Property-style invariant over seeded random paths ─────────────────────

def test_l_property_exit_never_on_wrong_side_of_sl_or_tp() -> None:
    rng = random.Random(42)
    entry = 100.0
    stop = 0.01
    take = 0.02
    sl_long, tp_long = entry * (1 - stop), entry * (1 + take)
    sl_short, tp_short = entry * (1 + stop), entry * (1 - take)

    for i in range(48):
        side = "long" if i % 2 == 0 else "short"
        # Random OHLC that may or may not touch levels
        mid = entry + rng.uniform(-0.03, 0.03) * entry
        wiggle = abs(rng.uniform(0.001, 0.04)) * entry
        o = mid + rng.uniform(-wiggle, wiggle)
        c = mid + rng.uniform(-wiggle, wiggle)
        h = max(o, c) + abs(rng.uniform(0, wiggle))
        l = min(o, c) - abs(rng.uniform(0, wiggle))  # noqa: E741
        candle = _candle(1_000_000 + 60_000, o, h, l, c)
        hit = resolve_candle_exit(
            side=side,
            entry=entry,
            stop_loss_pct=stop,
            take_profit_pct=take,
            candle=candle,
        )
        if hit is None:
            continue
        exit_px, reason = hit
        if side == "long":
            if reason in (EXIT_SL, EXIT_GAP_SL):
                # Loss exit must not be above the SL level (gap can be below)
                assert exit_px <= sl_long + 1e-12
            if reason in (EXIT_TP, EXIT_GAP_TP):
                assert exit_px >= tp_long - 1e-12
        else:
            if reason in (EXIT_SL, EXIT_GAP_SL):
                assert exit_px >= sl_short - 1e-12
            if reason in (EXIT_TP, EXIT_GAP_TP):
                assert exit_px <= tp_short + 1e-12


def test_max_hold_resolves_per_strategy_from_config() -> None:
    cfg = Config(
        {
            "strategy": {
                "orderbook_scalper": {"max_hold_seconds": 300},
                "cvd_orderflow": {"max_hold_hours": 6},
                "checklist_meta": {"max_hold_hours": 6},
                "funding_arbitrage": {"max_hold_hours": 8},
                "funding_momentum": {"max_hold_hours": 12},
                "spot_perp_carry": {"max_hold_hours": 24},
            }
        }
    )
    assert resolve_max_hold_ms("OrderBookScalper", cfg) == 300_000
    assert resolve_max_hold_ms("CVDOrderFlow", cfg) == 6 * 3_600_000
    assert resolve_max_hold_ms("ChecklistMeta", cfg) == 6 * 3_600_000
    assert resolve_max_hold_ms("FundingArbitrage", cfg) == 8 * 3_600_000
    assert resolve_max_hold_ms("FundingMomentum", cfg) == 12 * 3_600_000
    assert resolve_max_hold_ms("SpotPerpCarry", cfg) == 24 * 3_600_000


def test_evaluate_batch_with_injected_candle_loader() -> None:
    entry_ts = 5_000_000
    d_ok = _decision(side="long", price=100.0, stop=0.01, take=0.02, ts=entry_ts, strategy="OBS")
    d_old = _decision(
        include_bracket=False, ts=entry_ts + 1, strategy="OBS", row_id=2
    )

    def loader(symbol: str, ts: int, max_hold: int):
        return (
            [_candle(ts + 60_000, 100.0, 103.0, 100.0, 102.5)],
            "synthetic",
        )

    boards = evaluate_shadow_decisions(
        [d_ok, d_old],
        config=Config({"strategy": {"orderbook_scalper": {"max_hold_seconds": 300}}}),
        candle_loader=loader,
    )
    # Strategy name is OBS — max_hold falls back to default 6h
    board = boards["OBS"]
    assert board.n_evaluated == 1
    assert board.n_skipped == 1
    assert board.skip_reasons[SKIP_MISSING_BRACKET] == 1
    assert board.wins == 1


# ── Engine enrichment + exception isolation ──────────────────────────────────

def test_engine_shadow_records_enriched_fields_and_swallows_recorder_errors() -> None:
    from src.core.engine import TradingEngine
    from src.core.execution import ExecutionEngine
    from src.core.risk_manager import RiskManager
    from src.data.database import Database
    from src.exchanges.hyperliquid_ws import DataBus

    class _StubStrategy:
        name = "StubShadow"

        def __init__(self) -> None:
            self._shadow_instance = True

        def on_data(self, event: MarketEvent) -> Optional[Signal]:
            return Signal(
                strategy=self.name,
                symbol=event.symbol,
                side="long",
                confidence=0.8,
                size_pct=0.01,
                stop_loss_pct=0.012,
                take_profit_pct=0.024,
                metadata={"unit": "test"},
            )

    class _BoomRecorder:
        def __init__(self) -> None:
            self.calls = 0
            self.last = None

        def record(self, decision: ShadowDecision) -> None:
            self.calls += 1
            self.last = decision
            raise RuntimeError("recorder boom")

    class _CaptureRecorder:
        def __init__(self) -> None:
            self.last = None

        def record(self, decision: ShadowDecision) -> None:
            self.last = decision

    cfg = Config(
        {
            "symbols": ["BTC"],
            "strategy": {"cooldown": {"base_minutes": 30, "max_minutes": 120}},
            "risk": {"max_position_size_pct": 5.0, "leverage_max": 5.0},
        }
    )
    db = Database(":memory:")
    risk = RiskManager(cfg, db)
    bus = DataBus()
    executor = ExecutionEngine(cfg, db, "paper")
    engine = TradingEngine(cfg, db, bus, [], risk, executor, shadow_strategies=[_StubStrategy()])

    # Enrichment fields present
    capture = _CaptureRecorder()
    engine._shadow_recorder = capture  # type: ignore[assignment]
    event = MarketEvent(
        symbol="BTC",
        timestamp_ms=9_000_000,
        price=50_000.0,
    )
    engine._evaluate_shadow_strategies(event, "BTC")
    assert capture.last is not None
    snap = capture.last.market_snapshot or {}
    assert snap["stop_loss_pct"] == pytest.approx(0.012)
    assert snap["take_profit_pct"] == pytest.approx(0.024)
    assert snap["size_pct"] == pytest.approx(0.01)
    assert snap["metadata"]["unit"] == "test"
    assert snap["price"] == pytest.approx(50_000.0)

    # Recorder exception must not propagate
    boom = _BoomRecorder()
    engine._shadow_recorder = boom  # type: ignore[assignment]
    engine._evaluate_shadow_strategies(event, "BTC")
    assert boom.calls == 1  # was invoked, error swallowed
