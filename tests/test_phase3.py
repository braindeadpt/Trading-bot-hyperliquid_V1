"""Phase 3 tests: DB metrics, strategy governor, regime helper, trailing."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.regime import apply_regime_weights, regime_strategy_name
from src.core.strategy_governor import StrategyGovernor
from src.data.database import Database, TradeEntry, TradeExit
from src.strategies.base import Signal
from src.utils.config import Config


def test_regime_strategy_name_resolves_ensemble() -> None:
    sig = Signal(
        strategy="StrategyEnsemble",
        symbol="BTC",
        side="long",
        confidence=0.7,
        size_pct=0.01,
        metadata={"original_strategy": "SmartMoneyFlow"},
    )
    assert regime_strategy_name(sig) == "SmartMoneyFlow"


def test_apply_regime_weights_boosts_trend_strategy() -> None:
    sig = Signal(
        strategy="StrategyEnsemble",
        symbol="BTC",
        side="long",
        confidence=0.8,
        size_pct=0.01,
        metadata={"original_strategy": "SmartMoneyFlow"},
    )
    weights = {"trend": {"SmartMoneyFlow": 1.3, "FundingExtreme": 0.7}}
    out = apply_regime_weights([sig], 30.0, weights, 25.0, 20.0)
    assert len(out) == 1
    assert out[0].confidence == 1.0  # capped at 1.0


def test_database_metrics_by_strategy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        now = int(time.time() * 1000)
        t1 = db.save_trade_entry(
            TradeEntry("BTC", "long", 100.0, now - 1000, 1.0, "StrategyEnsemble", sub_strategy="VWAPDeviation")
        )
        db.update_trade_exit(
            TradeExit(t1, 101.0, now, 1.0, 0.01, "tp", "closed")
        )
        t2 = db.save_trade_entry(
            TradeEntry("ETH", "short", 50.0, now - 500, 2.0, "StrategyEnsemble", sub_strategy="VWAPDeviation")
        )
        db.update_trade_exit(
            TradeExit(t2, 49.0, now, 2.0, 0.02, "tp", "closed")
        )

        since = now - 86_400_000
        metrics = db.get_metrics_by_strategy(since)
        assert "VWAPDeviation" in metrics
        assert metrics["VWAPDeviation"]["total_trades"] == 2
        assert metrics["VWAPDeviation"]["wins"] == 2

        daily = db.get_daily_pnl_series(7)
        assert isinstance(daily, list)
        assert len(daily) >= 1
        db.close()


def test_strategy_governor_disables_weak_strategy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "gov.db")
        now = int(time.time() * 1000)
        for i in range(12):
            tid = db.save_trade_entry(
                TradeEntry("BTC", "long", 100.0, now - i * 1000, 1.0, "StrategyEnsemble", sub_strategy="FundingExtreme")
            )
            loss = -0.01 - (i * 0.001)
            db.update_trade_exit(
                TradeExit(tid, 99.0, now - i * 500, loss * 100, loss, "sl", "closed")
            )

        cfg = Config({
            "strategy": {
                "strategy_governance": {
                    "enabled": True,
                    "lookback_days": 30,
                    "min_trades": 10,
                    "min_sharpe": 0.0,
                    "eval_interval_ms": 0,
                }
            }
        })
        gov = StrategyGovernor(cfg, db)
        gov.evaluate(now)
        assert not gov.is_enabled("FundingExtreme")
        db.close()


if __name__ == "__main__":
    test_regime_strategy_name_resolves_ensemble()
    test_apply_regime_weights_boosts_trend_strategy()
    test_database_metrics_by_strategy()
    test_strategy_governor_disables_weak_strategy()
    print("ALL PHASE 3 TESTS PASSED [OK]")
