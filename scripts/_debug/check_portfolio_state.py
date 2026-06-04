#!/usr/bin/env python3
"""Quick portfolio / circuit-breaker diagnostic."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.portfolio import PortfolioState
from src.core.risk_manager import RiskManager
from src.data.database import Database
from src.utils.config import load_config


async def main() -> None:
    cfg = load_config(ROOT / "config" / "settings.yaml")
    initial = float(cfg.get("risk.initial_capital", 10_000.0))
    db = Database(ROOT / cfg.get("database.path", "data/live/bot.db"))

    hist = db.get_portfolio_history(limit=1)
    portfolio = PortfolioState(initial)
    if hist:
        import json
        snap = hist[0]
        positions_data = json.loads(snap["positions_json"])
        await portfolio.from_dict({
            "cash": snap["capital"],
            "peak_capital": snap.get("peak_capital", snap["capital"]),
            "daily_peak_capital": snap.get("daily_peak_capital", snap["capital"]),
            "initial_capital": snap.get("initial_capital", snap["capital"]),
            "daily_pnl": snap["daily_pnl"],
            "positions": positions_data,
        })
        print("Restored from DB snapshot:")
    else:
        print("Fresh portfolio (no snapshot):")

    state = await portfolio.to_dict()
    dd = await portfolio.get_max_drawdown()
    cap = await portfolio.current_capital
    rm = RiskManager(cfg, db)

    print(f"  capital:      ${cap:,.2f}")
    print(f"  peak_capital: ${state['peak_capital']:,.2f}")
    print(f"  daily_pnl:    ${state['daily_pnl']:,.2f}")
    print(f"  open pos:     {len(state['positions'])}")
    print(f"  max drawdown: {dd * 100:.4f}%")
    print(f"  CB limit:     {cfg.get('risk.circuit_breaker_drawdown_pct', 10)}%")
    print(f"  CB tripped:   {rm.is_circuit_breaker_tripped()} — {rm._circuit_breaker_reason}")

    open_trades = db.get_open_trades()
    print(f"  open trades in DB: {len(open_trades)}")
    for t in open_trades[:5]:
        print(f"    {t.symbol} {t.side} entry={t.entry_price} size={t.size}")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
