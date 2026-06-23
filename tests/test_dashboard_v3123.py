"""Tests for v3.1.23 dashboard v3.1.23 additions:
- /health endpoint with all v3.1.22 fields
- engine_monitor payload includes regime + reconcile + ws_health + governor
- strategies payload includes strategy_class + governor_disabled + sharpe
- trades payload includes funding_paid
- TradeExit has funding_paid field
- update_trade_funding exists
- StrategyGovernor.last_metrics exposed
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.data.database import TradeEntry, TradeExit  # noqa: E402


def test_trade_exit_has_funding_paid():
    """TradeExit v3.1.23: must carry funding_paid field."""
    fields = TradeExit.__dataclass_fields__
    assert "funding_paid" in fields, f"TradeExit missing funding_paid: {list(fields)}"
    assert fields["funding_paid"].default == 0.0


def test_trade_exit_funding_paid_kwarg():
    """Can construct TradeExit with funding_paid kwarg."""
    ex = TradeExit(
        trade_id=1, exit_price=100.0, exit_time=0,
        pnl_usd=10.0, pnl_pct=0.01, exit_reason="test",
        funding_paid=-0.25,
    )
    assert ex.funding_paid == -0.25


def test_dashboard_safe_float_import():
    """web.py must import safe_float from src.utils.helpers."""
    from src.dashboard import web
    assert hasattr(web, "safe_float")
    assert web.safe_float(0.5) == 0.5
    assert web.safe_float(None, 99.0) == 99.0
    assert web.safe_float("abc", 0.0) == 0.0


def test_strategy_class_map_covers_all_strategies():
    """All 14 known strategies must be in STRATEGY_CLASS."""
    from src.strategies.ensemble import STRATEGY_CLASS
    expected = {
        "SmartMoneyFlow", "TrendPyramid", "VolatilityBreakout", "DonchianBreakout",
        "VWAPDeviation", "RangeGrid", "CVDOrderFlow", "LiquidationCatcher",
        "FundingArbitrage", "SpotPerpCarry", "FundingMomentum",
        "LeadLag", "OrderBookScalper",
    }
    missing = expected - set(STRATEGY_CLASS.keys())
    assert not missing, f"STRATEGY_CLASS missing: {missing}"
    # Each strategy must have one of 4 valid classes
    valid_classes = {"trend", "revert", "carry", "micro"}
    for name, cls in STRATEGY_CLASS.items():
        assert cls in valid_classes, f"{name} has invalid class {cls}"


def test_engine_strategies_have_min_attrs():
    """TradingEngine._mode must exist for reconcile loop."""
    import inspect
    from src.core.engine import TradingEngine
    src = inspect.getsource(TradingEngine.__init__)
    assert "self._mode" in src, "TradingEngine.__init__ must set self._mode"


def test_db_update_trade_funding_exists():
    """Database must expose update_trade_funding for live funding persistence."""
    from src.data.database import Database
    assert hasattr(Database, "update_trade_funding"), \
        "Database.update_trade_funding is required for v3.1.23 funding accounting"
    import inspect
    sig = inspect.signature(Database.update_trade_funding)
    params = list(sig.parameters.keys())
    assert "self" in params
    assert "trade_id" in params
    assert "funding_paid" in params


def test_db_trades_has_funding_paid_column():
    """trades table must include funding_paid column (via migration)."""
    import gc, shutil
    tmpdir = tempfile.mkdtemp()
    try:
        from src.data.database import Database
        db = Database(os.path.join(tmpdir, "test.db"))
        with db._conn():
            cols = [row[1] for row in db._conn().execute("PRAGMA table_info(trades)").fetchall()]
        assert "funding_paid" in cols, f"trades table missing funding_paid: {cols}"
        db.close()
        del db
        gc.collect()
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def test_db_save_and_update_funding_roundtrip():
    """save_trade_entry + update_trade_funding should persist funding_paid."""
    import gc, shutil
    tmpdir = tempfile.mkdtemp()
    try:
        from src.data.database import Database
        db = Database(os.path.join(tmpdir, "test.db"))
        entry = TradeEntry(
            symbol="BTC", side="long", entry_price=100.0, entry_time=1,
            size=0.1, strategy="test", sub_strategy="t", status="open",
        )
        trade_id = db.save_trade_entry(entry)
        db.update_trade_funding(trade_id, -0.5)
        with db._conn():
            row = db._conn().execute(
                "SELECT funding_paid FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        assert row is not None
        assert abs(row[0] - (-0.5)) < 1e-9, f"funding_paid not persisted: {row[0]}"
        db.close()
        del db
        gc.collect()
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def test_governor_last_metrics_property():
    """StrategyGovernor must expose last_metrics property for dashboard."""
    from src.core.strategy_governor import StrategyGovernor
    src = inspect_getsource(StrategyGovernor)
    assert "def last_metrics" in src, "StrategyGovernor must expose last_metrics"


def test_engine_emits_governor_in_engine_monitor():
    """engine_monitor payload must include governor + regime + reconcile fields."""
    from src.dashboard import web
    src = inspect_getsource(web.DashboardEmitter._emit_engine_monitor)
    for field in ["governor", "regime_per_symbol", "reconcile", "ws_health", "adx_per_symbol"]:
        assert field in src, f"engine_monitor missing {field}"


def test_engine_emits_strategies_with_class_and_gov():
    """strategies payload must include strategy_class + governor_disabled."""
    from src.dashboard import web
    src = inspect_getsource(web.DashboardEmitter._emit_strategies)
    for field in ["strategy_class", "governor_disabled", "sharpe_30d", "trades_30d"]:
        assert field in src, f"strategies missing {field}"


def test_engine_emits_trades_with_funding_paid():
    """trades payload must include funding_paid."""
    from src.dashboard import web
    src = inspect_getsource(web.DashboardEmitter._emit_trades)
    assert "funding_paid" in src, "_emit_trades must include funding_paid"


def test_health_endpoint_passes_topic():
    """/health must pass a topic to last_publish_age_sec (v3.1.13 signature)."""
    from src.dashboard import web
    src = inspect_getsource(web)  # whole module
    # Find the /health function: lines containing "def health" up to next def
    lines = src.split("\n")
    in_health = False
    health_src = []
    for line in lines:
        if "def health" in line:
            in_health = True
        elif in_health and (line.startswith("def ") or line.startswith("class ")):
            break
        if in_health:
            health_src.append(line)
    health_text = "\n".join(health_src)
    assert 'last_publish_age_sec("price:BTC")' in health_text, \
        "/health must pass a topic to last_publish_age_sec"
    assert "ws_health_loop_running" in health_text, \
        "/health must include ws_health_loop_running"
    assert "reconcile_running" in health_text, \
        "/health must include reconcile_running"
    assert "governor_disabled" in health_text, \
        "/health must include governor_disabled"


# helper
def inspect_getsource(obj) -> str:
    import inspect
    return inspect.getsource(obj)


# ════════════════════════════════════════════════════════════════
# Runner
# ════════════════════════════════════════════════════════════════
def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = 0
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}: {e}")
            failed += 1
    print(f"\n{passed}/{passed + failed} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
