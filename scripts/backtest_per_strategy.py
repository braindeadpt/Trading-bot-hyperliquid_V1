"""Backtest each strategy in isolation across 3 windows to measure real edge.

Output: console table + CSV at data/backtests/per_strategy_<ts>.csv
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.engine import BacktestEngine, BacktestConfig
from src.data.database import Database
from src.strategies.factory import _STRATEGY_REGISTRY
from src.utils.config import load_config

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(message)s",
)
# Suppress spam from the volatility circuit breaker and other verbose modules
logging.getLogger("src.core.volatility_circuit").setLevel(logging.ERROR)
logging.getLogger("src.backtest.engine").setLevel(logging.ERROR)
logging.getLogger("src.strategies").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# ── Windows ──────────────────────────────────────────────────────
WINDOWS: List[Tuple[str, str, str]] = [
    ("A_volatile_3d",   "2026-06-23", "2026-06-25"),
    ("B_2weeks",        "2026-06-11", "2026-06-25"),
    ("C_full",          "2026-05-18", "2026-06-25"),
]

# Column order for CSV / table
COLS = [
    "strategy", "window", "n_trades", "win_rate", "avg_win_pct", "avg_loss_pct",
    "real_rr", "expectancy", "profit_factor", "sharpe", "max_dd_pct",
    "tp_trades", "tp_pnl", "sl_trades", "sl_pnl",
    "time_trades", "time_pnl", "other_trades", "other_pnl",
]

STRATEGY_NAMES = [
    "VolatilityBreakout", "DonchianBreakout", "VWAPDeviation",
    "LiquidationCatcher", "CVDOrderFlow", "LeadLag",
    "SpotPerpCarry", "RangeGrid", "TrendPyramid", "FundingMomentum",
    "MeanReversion", "FundingArbitrage", "TrendFollow", "OrderBookScalper",
]


def ms_from_date(date_str: str, end: bool = False) -> int:
    """Convert YYYY-MM-DD to millisecond timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def extract_exit_reasons(trades: List[Dict]) -> Dict[str, Any]:
    """Group trades by exit_reason and compute per-group PnL."""
    groups: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        reason = str(t.get("exit_reason", "unknown")).lower()
        groups[reason].append(float(t.get("pnl_usd", 0.0)))

    def _cat(keys: Tuple[str, ...]) -> Tuple[int, float]:
        n, pnl = 0, 0.0
        for k in keys:
            vals = groups.pop(k, [])
            n += len(vals)
            pnl += sum(vals)
        return n, round(pnl, 2)

    tp_n, tp_pnl = _cat(("take_profit", "tp"))
    sl_n, sl_pnl = _cat(("stop_loss", "sl", "stoploss"))

    # Time-based exits
    time_n, time_pnl = _cat((
        "time_limit", "time", "max_hold", "max_hold_time",
        "cooldown", "exit_time", "time_stop", "time_exit",
    ))

    # Everything else
    other_n = 0
    other_pnl = 0.0
    for reason, vals in groups.items():
        other_n += len(vals)
        other_pnl += sum(vals)
    other_pnl = round(other_pnl, 2)

    return {
        "tp_trades": tp_n, "tp_pnl": tp_pnl,
        "sl_trades": sl_n, "sl_pnl": sl_pnl,
        "time_trades": time_n, "time_pnl": time_pnl,
        "other_trades": other_n, "other_pnl": other_pnl,
    }


def run_backtest(
    cfg: Any,
    db: Database,
    strategy_cls: Any,
    strategy_section: dict,
    window_label: str,
    start_ms: int,
    end_ms: int,
) -> Optional[Dict[str, Any]]:
    """Run a single backtest with *one* strategy active and return metrics."""
    try:
        # Instantiate the strategy with its config section
        strategy = strategy_cls(strategy_section)
    except Exception as exc:
        logger.warning("Failed to instantiate %s: %s", strategy_cls.__name__, exc)
        return None

    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", cfg.get("risk.initial_capital", 10_000.0))),
        commission_pct=float(cfg.get("backtest.commission_pct", cfg.get("risk.taker_fee_pct", 0.035))),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.05)),
        use_regime_weights=False,
        use_cooldown=bool(cfg.get("backtest.use_cooldown", True)),
        use_kelly=bool(cfg.get("backtest.use_kelly", True)),
        use_microstructure_proxy=bool(cfg.get("backtest.use_microstructure_proxy", True)),
    )
    symbols = cfg.get("assets", ["BTC", "ETH", "SOL"])
    risk_cfg = cfg.get("risk", {}) or {}

    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )

    name = strategy_cls.__name__
    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except ValueError as exc:
        # "No 1m candles found" means no data in window for this strategy
        logger.info("%s %s: no data — %s", name, window_label, exc)
        return None
    except Exception as exc:
        logger.warning("%s %s crashed: %s", name, window_label, exc)
        return None

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])

    if not trades:
        return {
            "strategy": name,
            "window": window_label,
            "n_trades": 0, "win_rate": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "real_rr": 0.0, "expectancy": 0.0,
            "profit_factor": 0.0, "sharpe": 0.0, "max_dd_pct": 0.0,
            "tp_trades": 0, "tp_pnl": 0.0,
            "sl_trades": 0, "sl_pnl": 0.0,
            "time_trades": 0, "time_pnl": 0.0,
            "other_trades": 0, "other_pnl": 0.0,
        }

    avg_win_pct = abs(float(metrics.get("avg_win", 0))) if metrics.get("n_wins", 0) > 0 else 0.0
    avg_loss_pct = abs(float(metrics.get("avg_loss", 0))) if metrics.get("n_losses", 0) > 0 else 0.0
    real_rr = round(avg_win_pct / avg_loss_pct, 4) if avg_loss_pct > 0 else 0.0
    expectancy = round(float(metrics.get("avg_trade", 0)), 4)

    exit_r = extract_exit_reasons(trades)

    return {
        "strategy": name,
        "window": window_label,
        "n_trades": int(metrics.get("n_trades", 0)),
        "win_rate": round(float(metrics.get("win_rate", 0)) * 100, 1),
        "avg_win_pct": round(float(metrics.get("avg_win", 0)), 2),
        "avg_loss_pct": round(float(metrics.get("avg_loss", 0)), 2),
        "real_rr": real_rr,
        "expectancy": expectancy,
        "profit_factor": round(float(metrics.get("profit_factor", 0)), 4),
        "sharpe": round(float(metrics.get("sharpe_ratio", 0)), 4),
        "max_dd_pct": round(float(metrics.get("max_drawdown", 0)) * 100, 2),
        **exit_r,
    }


def print_table(rows: List[Dict]) -> None:
    """Print formatted table to console."""
    header = (
        f"{'Strategy':22s} {'Win':6s} {'n':5s} {'WR%':6s} {'AvgW':8s} {'AvgL':8s} "
        f"{'R:R':6s} {'Exp':8s} {'PF':6s} {'Sharpe':7s} {'DD%':6s} "
        f"{'TP#':4s} {'TP$':8s} {'SL#':4s} {'SL$':8s} {'Time#':5s} {'Time$':8s}"
    )
    sep = "─" * len(header)
    print(f"\n{'═' * len(header)}")
    print(f"  PER-STRATEGY BACKTEST RESULTS")
    print(f"{'═' * len(header)}")
    print(header)
    print(sep)

    for r in rows:
        strategy = r["strategy"][:22]
        win = "✅" if (r["sharpe"] > 0 and r["expectancy"] > 0 and r["n_trades"] >= 3) else " "
        print(
            f"{strategy:22s} {win:6s} {r['n_trades']:5d} "
            f"{r['win_rate']:5.1f}% "
            f"${r['avg_win_pct']:>6.2f} "
            f"${r['avg_loss_pct']:>6.2f} "
            f"{r['real_rr']:>5.2f}x "
            f"${r['expectancy']:>6.2f} "
            f"{r['profit_factor']:>5.2f}x "
            f"{r['sharpe']:>6.3f} "
            f"{r['max_dd_pct']:>5.2f}% "
            f"{r['tp_trades']:4d} ${r['tp_pnl']:>6.2f} "
            f"{r['sl_trades']:4d} ${r['sl_pnl']:>7.2f} "
            f"{r['time_trades']:5d} ${r['time_pnl']:>7.2f}"
        )
    print(sep)


def print_summary(rows: List[Dict]) -> None:
    """Print keep/drop recommendations."""
    print(f"\n{'═' * 70}")
    print("  CANDIDATAS A MANTER (expectancy>0 E Sharpe>0 nas 3 janelas)")
    print(f"{'─' * 70}")

    by_strategy: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        by_strategy[r["strategy"]].append(r)

    candidates_keep = []
    candidates_drop = []
    for name, results in sorted(by_strategy.items()):
        n_pos = sum(1 for rr in results if rr["expectancy"] > 0 and rr["sharpe"] > 0 and rr["n_trades"] >= 3)
        n_neg = sum(1 for rr in results if rr["expectancy"] <= 0 and rr["sharpe"] <= 0)
        n_total = len(results)
        if n_pos == n_total and n_total >= 2:
            candidates_keep.append(name)
        elif n_neg >= 2:
            candidates_drop.append(name)

    if candidates_keep:
        for name in candidates_keep:
            print(f"  ✅ {name}")
    else:
        print("  (nenhuma — expectancy>0 e Sharpe>0 nas 3 janelas é um critério exigente)")

    print(f"\n{'─' * 70}")
    print("  CANDIDATAS A DESLIGAR (negativas em ≥2 janelas)")
    print(f"{'─' * 70}")
    if candidates_drop:
        for name in candidates_drop:
            print(f"  ❌ {name}")
    else:
        print("  (nenhuma)")

    # Show all with notable stats
    print(f"\n{'─' * 70}")
    print("  TODAS AS ESTRATÉGIAS — melhor Sharpe por janela")
    print(f"{'─' * 70}")
    for w_label, _, _ in WINDOWS:
        best = max(
            (r for r in rows if r["window"] == w_label and r["n_trades"] >= 5),
            key=lambda r: r["sharpe"],
            default=None,
        )
        if best:
            print(f"  {w_label:20s} → {best['strategy']:22s} Sharpe={best['sharpe']:.3f}  n={best['n_trades']}")
        else:
            print(f"  {w_label:20s} → (nenhuma com ≥5 trades)")


def main() -> None:
    cfg = load_config("config/settings.yaml")
    db_path = cfg.get("database.path", "data/live/bot.db")
    db = Database(db_path)

    # Map class name → (config_path, class)
    registry_map: Dict[str, Tuple[str, Any]] = {}
    for path, cls in _STRATEGY_REGISTRY:
        registry_map[cls.__name__] = (path, cls)

    rows: List[Dict] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = "data/backtests"
    os.makedirs(out_dir, exist_ok=True)

    total = len(STRATEGY_NAMES) * len(WINDOWS)
    done = 0

    for sname in STRATEGY_NAMES:
        info = registry_map.get(sname)
        if info is None:
            print(f"⚠  {sname} not found in registry (skipping)")
            continue
        path, cls = info

        # Build the section — forced enabled for this run only
        section = dict(cfg.get(path, {}) or {})
        section["enabled"] = True

        for w_label, w_start, w_end in WINDOWS:
            done += 1
            start_ms = ms_from_date(w_start)
            end_ms = ms_from_date(w_end, end=True)
            print(f"  [{done}/{total}] {sname:24s} {w_label:20s} ...", end=" ", flush=True)

            result = run_backtest(cfg, db, cls, section, w_label, start_ms, end_ms)
            if result is not None:
                rows.append(result)
                print(f"{result['n_trades']:3d} trades")
            else:
                print("SKIP")

    # Save CSV
    csv_path = os.path.join(out_dir, f"per_strategy_{timestamp}.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {csv_path}  ({len(rows)} rows)")

    # Print table
    print_table(rows)

    # Summary
    print_summary(rows)


if __name__ == "__main__":
    main()