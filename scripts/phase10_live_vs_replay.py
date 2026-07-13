"""Fase 10 live-vs-replay drift-detection CLI.

Compares live paper-trading trades/gate-rejections recorded in
``data/live/bot.db`` for a given historical window against a fresh
BacktestEngine replay of the same window (same candles, same effective
config). See ``src/research/live_vs_replay.py`` for the full design notes,
drift-tolerance thresholds, and documented gaps (e.g. live trades have no
MFE/MAE columns).

Never writes to ``data/live/bot.db`` — opened strictly read-only (mode=ro)
and via a backup-based snapshot for the replay's candle data.

Usage:
    python scripts/phase10_live_vs_replay.py --start 2026-06-01T00:00:00Z --end 2026-06-02T00:00:00Z
    python scripts/phase10_live_vs_replay.py --start-ms 1748736000000 --end-ms 1748822400000
    python scripts/phase10_live_vs_replay.py --start ... --end ... --symbols BTC,ETH --json

Exit code 0 only when every dimension is PASS or NOT_COMPARABLE. Any
DRIFT_DETECTED dimension produces a non-zero exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.research.live_vs_replay import (  # noqa: E402
    build_live_vs_replay_report,
    format_report_text,
)
from src.utils.config import load_config  # noqa: E402


def _parse_iso_ms(value: str) -> int:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings.yaml")
    parser.add_argument("--db", default=None, help="Path to bot.db (default: data/live/bot.db)")
    parser.add_argument("--start", default=None, help="Window start, ISO-8601 (e.g. 2026-06-01T00:00:00Z)")
    parser.add_argument("--end", default=None, help="Window end, ISO-8601")
    parser.add_argument("--start-ms", type=int, default=None, help="Window start, epoch ms")
    parser.add_argument("--end-ms", type=int, default=None, help="Window end, epoch ms")
    parser.add_argument(
        "--symbols", default=None,
        help="Comma-separated symbol list (default: config 'assets' section)",
    )
    parser.add_argument(
        "--strategies", default=None,
        help="Comma-separated execution strategy override "
             "(default: strategy.phase08.execution_strategies from config)",
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    args = parser.parse_args()

    if args.start_ms is not None:
        start_ms = args.start_ms
    elif args.start is not None:
        start_ms = _parse_iso_ms(args.start)
    else:
        parser.error("one of --start or --start-ms is required")
        return 2

    if args.end_ms is not None:
        end_ms = args.end_ms
    elif args.end is not None:
        end_ms = _parse_iso_ms(args.end)
    else:
        parser.error("one of --end or --end-ms is required")
        return 2

    config = load_config(args.config)
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else list(config.get("assets", ["BTC", "ETH", "SOL"]))
    )
    strategies = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies
        else None
    )
    db_path = Path(args.db) if args.db else None

    report = build_live_vs_replay_report(
        config=config,
        start_ms=start_ms,
        end_ms=end_ms,
        symbols=symbols,
        live_db_path=db_path,
        execution_strategies=strategies,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(format_report_text(report))

    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
