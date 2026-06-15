"""Compare backtest metrics: baseline 1.5R TP vs improved 2R exit economics."""

from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.ERROR)

from backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from data.database import Database  # noqa: E402
from strategies.factory import build_ensemble  # noqa: E402
from utils.config import load_config  # noqa: E402

FROM_DATE = "2026-05-24"
TO_DATE = "2026-06-15"


def _date_ms(date_str: str, end_of_day: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def _apply_baseline_tp(cfg: Any) -> None:
    """Revert TP-related keys to pre-change baseline (1.5R where applicable)."""
    cfg.set("strategy.liquidation_catcher.take_profit_r", 1.5)
    cfg.set("strategy.donchian_breakout.tp_r_mult", 1.5)
    cfg.set("strategy.mean_reversion.take_profit_r_multiple", 1.5)
    cfg.set("strategy.funding_arbitrage.take_profit_r", 1.5)
    cfg.set("strategy.trend_follow.take_profit_r_multiple", 1.5)
    cfg.set("strategy.vwap_deviation.take_profit_r_multiple", 1.5)
    cfg.set(
        "execution.maker_orders.strategies",
        ["OrderBookScalper", "VWAPDeviation"],
    )


def _build_bt_config(cfg: Any) -> BacktestConfig:
    return BacktestConfig(
        initial_capital=float(
            cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))
        ),
        commission_pct=float(cfg.get("backtest.commission_pct", cfg.get("risk.taker_fee_pct", 0.035))),
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


def _exit_stats(trades: List[Dict[str, Any]]) -> Tuple[int, int, int]:
  reasons = Counter(t.get("exit_reason", "unknown") for t in trades)
  tp = reasons.get("take_profit", 0)
  sl = reasons.get("stop_loss", 0)
  other = len(trades) - tp - sl
  return tp, sl, other


def _run_label(cfg: Any, db: Database, label: str) -> Dict[str, Any]:
    bt = BacktestEngine(
        database=db,
        strategy=build_ensemble(cfg),
        config=_build_bt_config(cfg),
        symbols=list(cfg.get("assets", ["BTC", "ETH", "SOL"])),
    )
    result = bt.run(start_ms=_date_ms(FROM_DATE), end_ms=_date_ms(TO_DATE, end_of_day=True))
    metrics = result["metrics"]
    tp, sl, other = _exit_stats(result["trades"])
    ratio = (tp / sl) if sl else float("inf")
    print(f"\n=== {label} ===")
    print(f"trades={metrics.get('n_trades', 0)}  net_return={metrics.get('total_return', 0)*100:.2f}%")
    print(f"max_dd={metrics.get('max_drawdown', 0)*100:.2f}%  sharpe={metrics.get('sharpe_ratio', 0):.3f}")
    print(f"exits: TP={tp}  SL={sl}  other={other}  TP/SL={ratio:.2f}")
    return {
        "label": label,
        "metrics": metrics,
        "tp": tp,
        "sl": sl,
        "other": other,
        "tp_sl_ratio": ratio,
    }


def main() -> None:
    cfg_path = ROOT / "config" / "settings.yaml"
    db_path = ROOT / "data" / "live" / "bot.db"
    db = Database(str(db_path))

    improved_cfg = load_config(str(cfg_path))
    baseline_cfg = load_config(str(cfg_path))
    _apply_baseline_tp(baseline_cfg)

    baseline = _run_label(baseline_cfg, db, "baseline (1.5R TP, old maker list)")
    improved = _run_label(improved_cfg, db, "improved (2R TP + expanded maker entries)")

    dd_ok = improved["metrics"].get("max_drawdown", 0) <= baseline["metrics"].get("max_drawdown", 0)
    pnl_ok = improved["metrics"].get("total_return", 0) >= baseline["metrics"].get("total_return", 0)
    ratio_ok = improved["tp_sl_ratio"] >= baseline["tp_sl_ratio"]
    print(
        f"\nAcceptance: PnL {'OK' if pnl_ok else 'FAIL'} | "
        f"TP/SL {'OK' if ratio_ok else 'FAIL'} | "
        f"max_dd {'OK' if dd_ok else 'FAIL'}"
    )


if __name__ == "__main__":
    main()
