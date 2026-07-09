"""ChecklistMeta before/after forensic fixes — SOL & ETH 2026-07-03..09."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import Database
from src.strategies.checklist_meta import ChecklistMeta
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)


def ms_from_date(s: str, end: bool = False) -> int:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def summarize(trades: list) -> dict:
    closed = list(trades or [])
    stops = [
        t for t in closed
        if "stop_loss" in str(t.get("exit_reason", "")).lower()
    ]
    tps = [
        t for t in closed
        if "tp" in str(t.get("exit_reason", "")).lower()
    ]
    pnl = sum(float(t.get("pnl_usd") or 0) for t in closed)
    return {
        "trades": len(closed),
        "stops": len(stops),
        "tps": len(tps),
        "pnl": pnl,
    }


def run_variant(name: str, overrides: dict, symbols: list[str]) -> dict:
    cfg = load_config()
    db_path = cfg.get("database.path", "data/live/bot.db")
    db = Database(db_path)
    base = dict(cfg.get("strategy.checklist_meta", {}) or {})
    base.update(overrides)
    base["enabled"] = True
    strategy = ChecklistMeta(base)
    risk_cfg = dict(cfg.get("risk", {}) or {})
    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("risk.initial_capital", 10_000)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 3)),
        use_regime_weights=False,
        use_cooldown=True,
        use_kelly=False,
        use_microstructure_proxy=True,
        use_risk_manager=True,
        use_volatility_circuit=False,
        use_funding_blackout=False,
        use_external_feeds_replay=True,
        max_daily_trades=0,
    )
    engine = BacktestEngine(
        database=db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )
    start_ms = ms_from_date("2026-07-03")
    end_ms = ms_from_date("2026-07-09", end=True)
    result = engine.run(start_ms=start_ms, end_ms=end_ms)
    stats = summarize(result.get("trades", []))
    stats["name"] = name
    return stats


def main() -> None:
    symbols = ["SOL", "ETH"]
    before_overrides = {
        "min_adx_gate": 0.0,
        "dominance_margin": 0.0,
        "flip_block_minutes": 0.0,
        "sl_to_be_trigger_r": 1.0,
        "counter_trend_adx_block": 99.0,
        "require_oir_alignment": False,
    }
    after_overrides = {
        "min_adx_gate": 18.0,
        "dominance_margin": 1.5,
        "flip_block_minutes": 60.0,
        "sl_to_be_trigger_r": 0.6,
        "sl_to_be_vol_atr_factor": 0.75,
        "counter_trend_adx_block": 30.0,
        "require_oir_alignment": True,
        "oir_min_alignment": 0.10,
        "use_sl_to_be_after_1r": True,
    }

    before = run_variant("before (pre-forensic)", before_overrides, symbols)
    after = run_variant("after (v3.1.47 forensic)", after_overrides, symbols)

    print("ChecklistMeta replay SOL+ETH 2026-07-03 .. 2026-07-09")
    print("(replay understates live fidelity — directional only)")
    for row in (before, after):
        print(
            f"  {row['name']}: trades={row['trades']} "
            f"stops={row['stops']} tps={row['tps']} pnl=${row['pnl']:.2f}"
        )
    print(
        f"  delta stops: {after['stops'] - before['stops']:+d} "
        f"delta pnl: ${after['pnl'] - before['pnl']:+.2f}"
    )


if __name__ == "__main__":
    main()
