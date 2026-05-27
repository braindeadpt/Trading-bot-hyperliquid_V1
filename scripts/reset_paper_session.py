"""Reset paper trading session: clear circuit breaker state and reconcile portfolio peaks.

Run while the bot is STOPPED, then restart with quickstart.bat or main.py --mode paper.

Usage:
    python scripts/reset_paper_session.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.portfolio import PortfolioState
from src.core.risk_manager import RiskManager
from src.utils.config import load_config


async def main() -> None:
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    initial = float(cfg.get("risk.initial_capital", 10_000.0))

    portfolio = PortfolioState(initial)
    await portfolio.reconcile_peaks()

    rm = RiskManager(cfg, None)
    rm.reset_circuit_breaker()
    dd = await portfolio.get_max_drawdown()
    equity = await portfolio.current_capital

    print("Paper session reset (in-memory template)")
    print(f"  initial_capital: ${initial:,.2f}")
    print(f"  equity:          ${equity:,.2f}")
    print(f"  drawdown:        {dd * 100:.2f}%")
    print(f"  circuit_breaker: {rm.is_circuit_breaker_tripped()}")
    print()
    print("Restart the bot to apply portfolio reconcile + breaker reset on startup.")
    print("Optional: delete data/live/bot.db for a fully fresh paper run.")


if __name__ == "__main__":
    asyncio.run(main())
