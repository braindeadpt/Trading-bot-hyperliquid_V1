"""Profile the backtest hot path — cProfile over a bounded window.

Read-only against the live DB (WAL allows concurrent readers; the engine
only calls get_* methods — verified). Does NOT go through main.py, so it
does not take the instance lock and can run while the paper bot is live.

Usage:
    python scripts/research/profile_backtest.py --from 2026-08-25 --to 2026-09-08
    python scripts/research/profile_backtest.py --days 14 --top 40

Output: pstats dump under data/profiles/ + top-N table to stdout.
"""
from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from src.backtest.engine import BacktestEngine, build_backtest_config_from_yaml  # noqa: E402
from src.data.database import Database  # noqa: E402
from src.strategies.factory import build_backtest_strategy  # noqa: E402
from src.utils.config import load_config  # noqa: E402


def _ms(date_s: str, *, end: bool = False) -> int:
    dt = datetime.strptime(date_s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="from_d", default=None)
    ap.add_argument("--to", dest="to_d", default=None)
    ap.add_argument("--days", type=float, default=14.0,
                    help="window size when --from/--to omitted (ends at latest data)")
    ap.add_argument("--top", type=int, default=35)
    ap.add_argument("--config", default="config/settings.yaml")
    ap.add_argument("--db", default="data/live/bot.db")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = Database(args.db)

    end_ms = _ms(args.to_d, end=True) if args.to_d else None
    start_ms = _ms(args.from_d) if args.from_d else None
    if start_ms is None:
        latest = db._conn().execute(
            "SELECT MAX(timestamp_ms) FROM candles_1m"
        ).fetchone()[0]
        end_ms = latest if end_ms is None else end_ms
        start_ms = end_ms - int(args.days * 86_400_000)

    symbols = cfg.get("assets", ["BTC", "ETH", "SOL"])
    ensemble = build_backtest_strategy(cfg)
    bt = BacktestEngine(
        database=db,
        strategy=ensemble,
        config=build_backtest_config_from_yaml(cfg),
        symbols=symbols,
        risk_config=cfg,
    )

    out_dir = os.path.join(_ROOT, "data", "profiles")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = os.path.join(out_dir, f"backtest_{stamp}.pstats")

    t0 = datetime.now(timezone.utc)
    prof = cProfile.Profile()
    prof.enable()
    result = bt.run(start_ms=start_ms, end_ms=end_ms)
    prof.disable()
    wall = (datetime.now(timezone.utc) - t0).total_seconds()

    prof.dump_stats(out)
    metrics = result.get("metrics", {})
    print(f"\n=== wall {wall:.1f}s | trades={metrics.get('n_trades')} "
          f"| pstats -> {out} ===")
    st = pstats.Stats(out)
    st.sort_stats("cumulative").print_stats(args.top)
    st.sort_stats("tottime").print_stats(15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
