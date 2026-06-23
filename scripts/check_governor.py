"""Print StrategyGovernor disabled set from live DB."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.strategy_governor import StrategyGovernor
from src.data.database import Database
from src.utils.config import load_config

def main() -> None:
    db = Database(str(ROOT / "data" / "live" / "bot.db"))
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    gov = StrategyGovernor(cfg, db)
    gov.evaluate(int(time.time() * 1000))
    print("DISABLED BY GOVERNOR:", sorted(gov.disabled_strategies))
    for name, m in sorted(gov._last_metrics.items()):
        print(
            f"  {name:22s} trades={int(m['trades']):3d}  "
            f"sharpe={m['sharpe']:7.3f}  pnl=${m['total_pnl']:8.2f}  "
            f"wr={m['win_rate']:.1%}"
        )


if __name__ == "__main__":
    main()
