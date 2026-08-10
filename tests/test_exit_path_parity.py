"""Regression: replay exit path produces sl_to_be comparable to live (2026-07-07)."""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine, build_backtest_config_from_yaml
from src.data.database import Database
from src.strategies.base import ExitSignal, MarketEvent, Position
from src.strategies.factory import build_backtest_strategy
from src.utils.config import load_config
from src.utils.helpers import safe_float

pytestmark = pytest.mark.integration_offline

DAY = "2026-07-07"
DB_CANDIDATES = [
    ROOT / "data" / "live" / "bot_ruleset_validate.db",
    ROOT / "data" / "live" / "bot.db",
]


def _ms(day: str, end: bool = False) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _db_path() -> Path:
    for p in DB_CANDIDATES:
        if p.exists():
            return p
    pytest.skip("no live/validate DB")


def _live_cm_exits(db: Database, day: str) -> List[Dict[str, Any]]:
    rows = db._conn().execute(
        """
        SELECT exit_reason, pnl_usd, sub_strategy, strategy
        FROM trades
        WHERE exit_time BETWEEN ? AND ?
        """,
        (_ms(day), _ms(day, end=True)),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if str(d.get("sub_strategy") or d.get("strategy") or "") != "ChecklistMeta":
            continue
        out.append(d)
    return out


def _replay_cm(path_policy: str) -> List[Dict[str, Any]]:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    # ChecklistMeta is intentionally shadow-only in the active Phase08 roster.
    # This historical regression must opt into the archived strategy explicitly.
    cfg.set("strategy.phase08.execution_strategies", ["ChecklistMeta"])
    cfg.set("strategy.phase08.shadow_strategies", [])
    db = Database(str(_db_path()))
    symbols = list(cfg.get("assets") or ["BTC", "ETH", "SOL", "HYPE"])
    bt = build_backtest_config_from_yaml(cfg)
    bt.use_volatility_circuit = False
    bt.use_funding_blackout = False
    bt.max_daily_trades = 0
    bt.use_microstructure_proxy = True
    bt.exit_path_policy = path_policy
    eng = BacktestEngine(
        database=db,
        strategy=build_backtest_strategy(cfg),
        config=bt,
        symbols=symbols,
        risk_config=cfg,
    )
    result = eng.run(start_ms=_ms(DAY), end_ms=_ms(DAY, end=True))
    return [
        t
        for t in (result.get("trades") or [])
        if str(t.get("strategy") or t.get("sub_strategy") or "") == "ChecklistMeta"
    ]


def _be_count(trades: List[Dict[str, Any]], key: str = "exit_reason") -> int:
    return sum(1 for t in trades if str(t.get(key) or "").startswith("sl_to_be"))


@dataclass
class _FakeCandle:
    open: float
    high: float
    low: float
    close: float


def test_exit_path_prices_orderings() -> None:
    """Unit: P1 vs P2 OHLC sequences differ as documented."""
    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(exit_path_policy="favorable_first")
    c1m = _FakeCandle(open=100.0, high=110.0, low=90.0, close=105.0)
    p1_long = eng._exit_path_prices("long", c1m)  # type: ignore[arg-type]
    assert p1_long == [100.0, 110.0, 90.0, 105.0]
    eng.cfg = BacktestConfig(exit_path_policy="adverse_first")
    p2_long = eng._exit_path_prices("long", c1m)  # type: ignore[arg-type]
    assert p2_long == [100.0, 90.0, 110.0, 105.0]
    p2_short = eng._exit_path_prices("short", c1m)  # type: ignore[arg-type]
    assert p2_short == [100.0, 110.0, 90.0, 105.0]


def test_strategy_exit_fill_price_be_not_close() -> None:
    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(sl_to_be_buffer_pct=0.001)
    pos = MagicMock()
    pos.side = "long"
    pos.entry_price = 100.0
    pos.take_profit_price = 110.0
    be = eng._strategy_exit_fill_price(pos, "sl_to_be_hit_r0.60", path_price=95.0)
    assert abs(be - 100.1) < 1e-9
    pos.side = "short"
    be_s = eng._strategy_exit_fill_price(pos, "sl_to_be_hit_r0.60", path_price=105.0)
    assert abs(be_s - 99.9) < 1e-9


def test_process_exits_arms_be_on_favorable_then_cuts() -> None:
    """Synthetic: P1 walks H then L → BE ExitSignal filled at BE, not L/close."""

    class _ArmCutStrategy:
        name = "ChecklistMeta"

        def __init__(self) -> None:
            self.armed = False
            self.seen: List[float] = []

        def on_position(self, position: Position, event: MarketEvent) -> Optional[ExitSignal]:
            self.seen.append(float(event.price))
            entry = float(position.entry_price)
            if position.side == "long":
                if event.price >= entry * 1.005:
                    self.armed = True
                if self.armed and event.price <= entry * 1.001:
                    return ExitSignal(
                        strategy=self.name,
                        symbol=position.symbol,
                        side=position.side,
                        confidence=0.7,
                        reason="sl_to_be_hit_r0.60",
                    )
            return None

    eng = object.__new__(BacktestEngine)
    eng.cfg = BacktestConfig(exit_path_policy="favorable_first", sl_to_be_buffer_pct=0.001)
    eng.strategy = _ArmCutStrategy()
    eng.positions_by_symbol = {"BTC": 1}
    closed: List[Any] = []

    @dataclass
    class _Pos:
        id: int = 1
        strategy: str = "ChecklistMeta"
        symbol: str = "BTC"
        side: str = "long"
        entry_price: float = 100.0
        size: float = 1.0
        entry_time_ms: int = 1
        stop_loss_price: float = 99.0
        take_profit_price: float = 103.0
        metadata: Dict[str, Any] = None  # type: ignore[assignment]
        excursion_id: str = "x"
        entry_commission: float = 0.0
        funding_paid: float = 0.0

        def __post_init__(self) -> None:
            if self.metadata is None:
                self.metadata = {"strategy": "ChecklistMeta"}

    pos = _Pos()
    eng.positions = {1: pos}

    def _close(pos_id, fill_price, ts, reason, capital):
        closed.append({"fill": fill_price, "reason": reason, "ts": ts})
        eng.positions_by_symbol.pop("BTC", None)
        eng.positions.pop(pos_id, None)
        return capital

    eng._close_position = _close  # type: ignore[method-assign]
    eng._intrabar_stop_tp = lambda *_a, **_k: None  # type: ignore[method-assign]

    event = MarketEvent(symbol="BTC", price=100.5, timestamp_ms=60_000)
    c1m = _FakeCandle(open=100.0, high=100.6, low=99.5, close=100.2)
    eng._process_exits(event, 100_000.0, c1m)  # type: ignore[arg-type]

    assert closed, f"expected BE exit; seen={eng.strategy.seen}"
    assert str(closed[0]["reason"]).startswith("sl_to_be")
    assert abs(float(closed[0]["fill"]) - 100.1) < 1e-9
    assert eng.strategy.seen == [100.0, 100.6, 99.5]  # stopped at L after arm


@pytest.mark.skipif(
    not any(p.exists() for p in DB_CANDIDATES),
    reason="no candle/trade DB",
)
def test_replay_sl_to_be_present_and_near_flat_0707() -> None:
    """0707: at least one BE when live has many; BE PnL near flat (not −$49 close-fill).

    Full count parity vs live is validated on paired days (entry sets differ;
    most replay stop_loss trades never reach BE trigger MFE on this day).
    """
    db = Database(str(_db_path()))
    live = _live_cm_exits(db, DAY)
    live_be = _be_count(live)
    if live_be < 3:
        pytest.skip(f"live {DAY} has unexpected BE count={live_be}")

    p1 = _replay_cm("favorable_first")
    p2 = _replay_cm("adverse_first")
    p1_be = _be_count(p1)
    p2_be = _be_count(p2)
    assert max(p1_be, p2_be) >= 1, (
        f"expected >=1 BE after path fix; P1={p1_be} P2={p2_be} "
        f"reasons_p1={Counter(str(t.get('exit_reason')) for t in p1)}"
    )
    for label, trades in (("P1", p1), ("P2", p2)):
        for t in trades:
            if not str(t.get("exit_reason") or "").startswith("sl_to_be"):
                continue
            pnl = float(t.get("pnl_usd") or 0.0)
            assert abs(pnl) < 25.0, f"{label} BE pnl={pnl} (close-fill bug was ~-49)"


@pytest.mark.skipif(
    not any(p.exists() for p in DB_CANDIDATES),
    reason="no candle/trade DB",
)
def test_replay_sl_to_be_pnl_near_zero() -> None:
    """BE fills must be near flat — never the old close-fill −$49 bug."""
    for policy in ("adverse_first", "favorable_first"):
        replay = _replay_cm(policy)
        be = [t for t in replay if str(t.get("exit_reason") or "").startswith("sl_to_be")]
        if not be:
            continue
        for t in be:
            pnl = float(t.get("pnl_usd") or 0.0)
            assert abs(pnl) < 25.0, (
                f"policy={policy} BE pnl={pnl} too large (expected near BE/~0); "
                f"trade={t.get('symbol')} {t.get('exit_reason')}"
            )
