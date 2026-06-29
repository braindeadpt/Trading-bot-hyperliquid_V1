"""Analyse exit economics — why realized R:R is negative despite 2R TP config.

Runs baseline + A/B variations and reports per-strategy breakdown.
"""
from __future__ import annotations

import csv
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.factory import build_ensemble, build_sub_strategies, _STRATEGY_REGISTRY
from src.utils.config import load_config, Config

logging.basicConfig(level=logging.WARNING)
logging.getLogger("src.backtest.engine").setLevel(logging.ERROR)
logging.getLogger("src.strategies").setLevel(logging.ERROR)
logging.getLogger("src.core.volatility_circuit").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

FROM_DATE = "2026-06-01"
TO_DATE = "2026-06-25"
CSV_DIR = "data/backtests"


def ms(date_str: str, end: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def bt_config(cfg: Any, overrides: Optional[Dict] = None) -> BacktestConfig:
    base = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 10_000)),
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
    if overrides:
        for k, v in overrides.items():
            setattr(base, k, v)
    return base


def run_bt(cfg: Any, db: Database, config_overrides: Optional[Dict] = None, label: str = "") -> Dict:
    ensemble = build_ensemble(cfg)
    bt = BacktestEngine(
        database=db,
        strategy=ensemble,
        config=bt_config(cfg, config_overrides),
        symbols=list(cfg.get("assets", ["BTC", "ETH", "SOL"])),
    )
    result = bt.run(start_ms=ms(FROM_DATE), end_ms=ms(TO_DATE, end=True))
    m = result["metrics"]
    trades = result.get("trades", [])

    # Exit breakdown per strategy
    by_strategy: Dict[str, list] = defaultdict(list)
    for t in trades:
        strat = str(t.get("sub_strategy") or t.get("strategy", "unknown"))
        by_strategy[strat].append(t)

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  n_trades={m.get('n_trades',0):4d}  return={m.get('total_return',0)*100:.3f}%  "
          f"Sharpe={m.get('sharpe_ratio',0):.3f}  DD={m.get('max_drawdown',0)*100:.2f}%  "
          f"WinRate={m.get('win_rate',0)*100:.1f}%")
    print(f"  PF={m.get('profit_factor',0):.3f}  avg_trade=${m.get('avg_trade',0):.2f}")

    fees_total = sum(abs(t.get("fees_paid", 0)) for t in trades)
    avg_fee = fees_total / len(trades) if trades else 0
    print(f"  Fees total=${fees_total:.2f}  avg/trade=${avg_fee:.4f}")

    for strat, st in sorted(by_strategy.items(), key=lambda x: -len(x[1])):
        pnls = [t["pnl_usd"] for t in st]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        reasons = Counter(t.get("exit_reason", "?") for t in st)
        tp = reasons.get("take_profit", 0)
        sl = reasons.get("stop_loss", 0)
        trail = sum(1 for r in reasons if "trail" in r.lower())
        time_exits = sum(1 for r in reasons if "time" in r.lower() or "hold" in r.lower())
        other = len(st) - tp - sl - trail - time_exits
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        avg_fee_s = sum(abs(t.get("fees_paid", 0)) for t in st) / len(st)

        print(f"\n  --- {strat} ({len(st)} trades) ---")
        print(f"    PnL: avg_win=${avg_win:>7.2f}  avg_loss=${avg_loss:>7.2f}  R:R={rr:.2f}")
        print(f"    Exits: TP={tp}  SL={sl}  trail={trail}  time={time_exits}  other={other}")
        print(f"    Avg fee/trade=${avg_fee_s:.4f}")

    return {"metrics": m, "trades": trades, "by_strategy": dict(by_strategy)}


def override_strategy_param(cfg: Any, strategy_path: str, key: str, value: Any) -> None:
    """Override a single param in a strategy section (in-memory)."""
    section = dict(cfg.get(strategy_path, {}) or {})
    section[key] = value
    cfg.set(strategy_path, section)


def main() -> None:
    os.makedirs(CSV_DIR, exist_ok=True)
    cfg_path = "config/settings.yaml"
    db_path = "data/live/bot.db"
    db = Database(db_path)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results: List[Dict] = []

    # ═══════════════════════════════════════════════════════════════
    # 0. BASELINE (current config)
    # ═══════════════════════════════════════════════════════════════
    cfg = load_config(cfg_path)
    base = run_bt(cfg, db, label="0. BASELINE (current settings)")

    # ═══════════════════════════════════════════════════════════════
    # 1. EXIT ECONOMICS BY STRATEGY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*60}")
    print(f"  EXIT ECONOMICS PER STRATEGY")
    print(f"{'#'*60}")

    rows_csv = []
    for strat, trades in sorted(base["by_strategy"].items(), key=lambda x: -len(x[1])):
        pnls = [t["pnl_usd"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        reasons = Counter(t.get("exit_reason", "?") for t in trades)
        tp = reasons.get("take_profit", 0)
        sl = reasons.get("stop_loss", 0)
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        rr = abs(avg_win / avg_loss) if avg_loss else 0
        fees = sum(abs(t.get("fees_paid", 0)) for t in trades)
        slip = sum(abs(t.get("slippage_paid", 0)) for t in trades) if "slippage_paid" in trades[0] else 0
        rows_csv.append({
            "strategy": strat, "n": len(trades), "tp": tp, "sl": sl,
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "rr": round(rr, 3), "fees": round(fees, 2),
        })

    # Save CSV
    csv_path = os.path.join(CSV_DIR, f"exit_economics_{ts}.csv")
    with open(csv_path, "w", newline="") as f:
        if rows_csv:
            w = csv.DictWriter(f, fieldnames=rows_csv[0].keys())
            w.writeheader()
            w.writerows(rows_csv)
    print(f"\nCSV: {csv_path}")

    # ═══════════════════════════════════════════════════════════════
    # 2. A/B VARIATIONS
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*60}")
    print(f"  A/B PARAMETER VARIATIONS")
    print(f"{'#'*60}")

    variants = []

    # (a) Wider ATR stops in high vol — scale stop_loss_atr_mult to 3.0
    cfg_a = load_config(cfg_path)
    for path, _ in _STRATEGY_REGISTRY:
        section = dict(cfg_a.get(path, {}) or {})
        if "stop_loss_atr_multiplier" in section:
            section["stop_loss_atr_multiplier"] = section.get("stop_loss_atr_multiplier", 2.0) * 1.5
            cfg_a.set(path, section)
    variants.append(("2a. Wider ATR stops (1.5x)", cfg_a, None))

    # (b) TP at 2.5R and 3R
    cfg_b1 = load_config(cfg_path)
    for path, _ in _STRATEGY_REGISTRY:
        section = dict(cfg_b1.get(path, {}) or {})
        for tp_key in ("take_profit_r_multiple", "take_profit_r", "take_profit_atr_multiplier"):
            if tp_key in section:
                val = float(section[tp_key])
                # Scale: if it's an ATR mult (e.g. 4.0), scale proportionally
                section[tp_key] = val * 2.5 / 2.0 if "atr_mult" in tp_key else 2.5
                cfg_b1.set(path, section)
    variants.append(("2b. TP at 2.5R", cfg_b1, None))

    cfg_b2 = load_config(cfg_path)
    for path, _ in _STRATEGY_REGISTRY:
        section = dict(cfg_b2.get(path, {}) or {})
        for tp_key in ("take_profit_r_multiple", "take_profit_r", "take_profit_atr_multiplier"):
            if tp_key in section:
                val = float(section[tp_key])
                section[tp_key] = val * 3.0 / 2.0 if "atr_mult" in tp_key else 3.0
                cfg_b2.set(path, section)
    variants.append(("2b2. TP at 3.0R", cfg_b2, None))

    # (c) Trailing params — simulated by using tighter SL at exit
    #   The backtest can't model trailing directly. We approximate by
    #   checking what would happen if we exit at X% from peak.
    #   For now, just note that trailing is excluded from backtest.

    # (d) Pullback entry for breakout strategies — not easily simulated
    #   in the backtest. Skip for now.

    # Run all variants
    for label, cfg_v, overrides in variants:
        r = run_bt(cfg_v, db, config_overrides=overrides, label=label)
        results.append({"label": label, **r["metrics"]})

    # ═══════════════════════════════════════════════════════════════
    # 3. SUMMARY TABLE
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f"  COMPARISON TABLE")
    print(f"{'='*70}")
    print(f"  {'Variant':35s} {'n':>5s} {'Ret%':>7s} {'Sharpe':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s}")
    print(f"  {'-'*35} {'-'*5} {'-'*7} {'-'*7} {'-'*6} {'-'*5} {'-'*6}")

    # Baseline first
    m = base["metrics"]
    print(f"  {'0. BASELINE':35s} {m.get('n_trades',0):5d} {m.get('total_return',0)*100:7.3f} "
          f"{m.get('sharpe_ratio',0):7.3f} {m.get('max_drawdown',0)*100:6.2f} "
          f"{m.get('win_rate',0)*100:5.1f} {m.get('profit_factor',0):6.3f}")

    for r in results:
        print(f"  {r['label']:35s} {r.get('n_trades',0):5d} {r.get('total_return',0)*100:7.3f} "
              f"{r.get('sharpe_ratio',0):7.3f} {r.get('max_drawdown',0)*100:6.2f} "
              f"{r.get('win_rate',0)*100:5.1f} {r.get('profit_factor',0):6.3f}")


if __name__ == "__main__":
    main()