"""Walk-forward backtest — rolling train/test windows over DB candles.

Usage:
    python scripts/walk_forward.py
    python scripts/walk_forward.py --train-days 14 --test-days 7 --steps 4
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import build_ensemble
from src.utils.config import load_config


def _ms(days_ago: int) -> int:
    return int((time.time() - days_ago * 86400) * 1000)


def run_window(
    cfg: object,
    db: Database,
    start_ms: int,
    end_ms: int,
    symbols: list[str],
) -> dict:
    ensemble = build_ensemble(cfg)
    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.04)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        min_edge_buffer_pct=float(cfg.get("execution.min_edge_buffer_pct", 0.05)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.05)),
        use_regime_weights=bool(cfg.get("backtest.use_regime_weights", True)),
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_kelly=bool(cfg.get("backtest.use_kelly", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
        regime_weights=cfg.get("strategy.regime_weights", {}),
        adx_trend_threshold=float(cfg.get("strategy.adx_trend_threshold", 25.0)),
        adx_range_threshold=float(cfg.get("strategy.adx_range_threshold", 20.0)),
        cooldown_base_ms=int(cfg.get("strategy.cooldown.base_minutes", 60) * 60_000),
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 5)),
    )
    engine = BacktestEngine(database=db, strategy=ensemble, config=bt_cfg, symbols=symbols)
    return engine.run(start_ms=start_ms, end_ms=end_ms)


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward backtest")
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"))
    parser.add_argument("--train-days", type=int, default=14)
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--steps", type=int, default=3)
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = ROOT / cfg.get("database.path", "data/live/bot.db")
    db = Database(db_path)
    symbols = list(cfg.get("assets", ["BTC", "ETH", "SOL"]))

    print("=" * 60)
    print("WALK-FORWARD BACKTEST")
    print(f"train={args.train_days}d  test={args.test_days}d  steps={args.steps}")
    print("=" * 60)

    results = []
    for step in range(args.steps):
        test_end_days = step * args.test_days
        test_start_days = test_end_days + args.test_days
        train_start_days = test_start_days + args.train_days

        test_start_ms = _ms(test_start_days)
        test_end_ms = _ms(test_end_days) if test_end_days > 0 else int(time.time() * 1000)

        start_str = datetime.fromtimestamp(test_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        end_str = datetime.fromtimestamp(test_end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        print(f"\n--- Step {step + 1}: OOS {start_str} -> {end_str} ---")
        try:
            out = run_window(cfg, db, test_start_ms, test_end_ms, symbols)
        except ValueError as exc:
            print(f"  SKIP: {exc}")
            continue

        m = out["metrics"]
        row = {
            "step": step + 1,
            "start": start_str,
            "end": end_str,
            "trades": m.get("total_trades", 0),
            "win_rate": m.get("win_rate", 0),
            "sharpe": m.get("sharpe_ratio", 0),
            "max_dd": m.get("max_drawdown_pct", 0),
            "return_pct": round(float(out.get("total_return", 0)) * 100, 2),
        }
        results.append(row)
        print(
            f"  trades={row['trades']}  win_rate={row['win_rate']:.1f}%  "
            f"Sharpe={row['sharpe']:.3f}  maxDD={row['max_dd']:.2f}%  "
            f"return={row['return_pct']:.2f}%"
        )

    if results:
        avg_sharpe = sum(r["sharpe"] for r in results) / len(results)
        avg_return = sum(r["return_pct"] for r in results) / len(results)
        print("\n" + "=" * 60)
        print(f"SUMMARY: {len(results)} windows | avg Sharpe={avg_sharpe:.3f} | avg return={avg_return:.2f}%")
        print("=" * 60)


if __name__ == "__main__":
    main()
