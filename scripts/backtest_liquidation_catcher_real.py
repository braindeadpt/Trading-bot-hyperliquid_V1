"""Backtest LiquidationCatcher over the REAL liquidation feed (08-09+).

The research DB holds HL candles but zero liquidation rows (the aggregator
persists every real event to `data/live/bot.db` — okx/bybit/proxy — not the
research store). The replay engine reads candles AND liquidations from the
same DB, so this script assembles a dedicated backtest DB:

  * candles_1m/5m/15m/1h copied from the research DB (HL provenance),
  * liquidation rows copied from the live bot DB (real venues okx/bybit,
    plus the proxy-synthesis rows for comparison),

then runs LiquidationCatcher (force-enabled, `require_real_liquidation_data:
true` — the production contract) over the 08-09+ window and reports the
effective manifest, including the per-strategy fidelity tier with
liquidation provenance.

Usage:
    python scripts/backtest_liquidation_catcher_real.py [--start 2026-08-09]
        [--end 2026-08-14] [--symbols BTC ETH] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.data.database import FundingRecord, LiquidationRecord
from src.data.research_database import DEFAULT_RESEARCH_DB_PATH, ResearchDatabase
from src.data.series_metadata import SeriesMetadata
from src.strategies.liquidation_catcher import LiquidationCatcher
from src.utils.config import Config, get_strategy_section, load_config

logging.basicConfig(level=logging.WARNING, format="%(levelname)s | %(message)s")
logging.getLogger("src.backtest.engine").setLevel(logging.ERROR)
logging.getLogger("src.strategies").setLevel(logging.ERROR)
logger = logging.getLogger("liq_catcher_backtest")

LIVE_DB = ROOT / "data" / "live" / "bot.db"
REAL_SOURCES = ("okx", "bybit")


def ms(date_str: str, end: bool = False) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.timestamp() * 1000)


def _copy_candles(
    src: ResearchDatabase, dst: ResearchDatabase, symbol: str,
    start_ms: int, end_ms: int,
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for tf in ("1m", "5m", "15m", "1h"):
        rows = src.get_candles(symbol, tf, limit=500_000, start_ms=start_ms, end_ms=end_ms)
        if rows:
            dst.save_research_candles(rows, tf, SeriesMetadata.hl_candles())
        counts[tf] = len(rows)
    return counts


def _copy_liquidations(
    src_db: Path, dst: ResearchDatabase, symbol: str,
    start_ms: int, end_ms: int,
) -> Dict[str, int]:
    import sqlite3

    counts: Dict[str, int] = {}
    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT timestamp_ms, notional_usd, side, source FROM liquidation_events "
            "WHERE symbol = ? AND timestamp_ms >= ? AND timestamp_ms <= ? "
            "ORDER BY timestamp_ms ASC",
            (symbol, start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    for ts, notional, side, source in rows:
        src = str(source or "binance")
        counts[src] = counts.get(src, 0) + 1
        dst.save_liquidation(LiquidationRecord(
            symbol=symbol, timestamp_ms=int(ts),
            notional_usd=float(notional), side=str(side), source=src,
        ))
    return counts


def _copy_funding(
    src_db: Path, dst: ResearchDatabase, symbol: str,
    start_ms: int, end_ms: int,
) -> int:
    """Copy funding history rows from the live bot DB (research DB has none)."""
    import sqlite3

    con = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT timestamp, current, predicted FROM funding_history "
            "WHERE symbol = ? AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY timestamp ASC",
            (symbol, start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()
    for ts, current, predicted in rows:
        dst.save_funding(FundingRecord(
            symbol=symbol, timestamp=int(ts),
            current=float(current) if current is not None else 0.0,
            predicted=(float(predicted) if predicted is not None else None),
        ))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-08-09")
    ap.add_argument("--end", default="2026-08-14")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--json", type=Path, help="write full result as JSON")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms = ms(args.start)
    end_ms = ms(args.end, end=True)
    cfg = load_config(ROOT / "config" / "settings.yaml")

    research_src = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    bt_db = ResearchDatabase(tmp_path)

    print("=" * 78)
    print(f"  LiquidationCatcher backtest - REAL feed {args.start} -> {args.end}")
    print(f"  symbols: {', '.join(symbols)}")

    for sym in symbols:
        candle_counts = _copy_candles(research_src, bt_db, sym, start_ms, end_ms)
        liq_counts = _copy_liquidations(LIVE_DB, bt_db, sym, start_ms, end_ms)
        n_funding = _copy_funding(LIVE_DB, bt_db, sym, start_ms, end_ms)
        n_real = sum(v for k, v in liq_counts.items() if k in REAL_SOURCES)
        print(
            f"  {sym}: candles 1m={candle_counts['1m']} 5m={candle_counts['5m']} "
            f"15m={candle_counts['15m']} 1h={candle_counts['1h']} · "
            f"liquidations real={n_real} proxy={liq_counts.get('proxy', 0)} · funding={n_funding}"
        )

    # Force-enable LiquidationCatcher with the production data contract.
    section = get_strategy_section(cfg, "liquidation_catcher")
    section["enabled"] = True
    section["auto_enable"] = True
    strategy = LiquidationCatcher(section)

    # Production contract FIRST: what strict research would decide on this
    # exact DB (refuse_insufficient_feeds + strict_mode from settings.yaml).
    from src.backtest.data_contract import evaluate_data_contract

    contract = evaluate_data_contract(
        bt_db,
        symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        config=cfg,
        active_strategies=["LiquidationCatcher"],
    )
    print()
    print("  PRODUCTION DATA CONTRACT (strict, refuse_insufficient_feeds):")
    print(f"    fidelity_tier : {contract.fidelity_tier}")
    print(f"    refused       : {contract.refused}")
    print(f"    degraded      : {contract.degraded}")
    if contract.reasons:
        for r in contract.reasons:
            print(f"    reason        : {r}")
    sf = contract.strategy_fidelity.get("LiquidationCatcher")
    if sf is not None:
        print(
            f"    LiquidationCatcher: tier={sf.tier} tier_a={sf.tier_a_eligible} "
            f"missing={','.join(sf.missing_feeds) or '-'} "
            f"liq_provenance={sf.liquidation_provenance}"
        )

    # Run the backtest with the contract in DEGRADED mode (refuse=false) so
    # the replay executes and the manifest carries the effective tier the
    # contract would assign (degraded coverage + real liquidation provenance).
    risk_cfg = dict(cfg.get("risk", {}) or {})
    pg = dict(risk_cfg.get("portfolio_governance", {}) or {})
    pg["max_correlation"] = 0.98
    risk_cfg["portfolio_governance"] = pg
    risk_cfg = dict(risk_cfg)
    risk_cfg["research"] = dict((cfg.get("research", {}) or {}))
    risk_cfg["research"]["refuse_insufficient_feeds"] = False
    risk_cfg["research"]["strict_mode"] = True

    bt_cfg = BacktestConfig(
        initial_capital=float(cfg.get("backtest.initial_capital", 100_000)),
        commission_pct=float(cfg.get("backtest.commission_pct", 0.035)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 5)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=True,
        use_cooldown=True,
        use_microstructure_proxy=True,
        use_risk_manager=True,
        use_volatility_circuit=False,
        use_funding_blackout=False,
        use_external_feeds_replay=True,
        max_daily_trades=0,
        warmup_15m_bars=110,
    )

    engine = BacktestEngine(
        database=bt_db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )

    print("-" * 78)
    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:
        print(f"  BACKTEST FAILED: {exc}")
        return 1

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])
    manifest = result.get("manifest", {})
    n = int(metrics.get("n_trades", 0))
    wins = sum(1 for t in trades if float(t.get("pnl_usd", 0)) > 0)
    total_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)

    print()
    print(f"  n_trades      : {n}")
    print(f"  win_rate      : {(wins / n * 100 if n else 0):.1f}%")
    print(f"  profit_factor : {float(metrics.get('profit_factor', 0)):.3f}")
    print(f"  total_pnl_usd : {total_pnl:.2f}")
    print(f"  total_return  : {float(metrics.get('total_return', 0)) * 100:.2f}%")
    print()

    print("  MANIFEST — effective fidelity:")
    print(f"    data_source        : {manifest.get('data_source')}")
    print(f"    fidelity_tier      : {manifest.get('fidelity_tier')}")
    sf = manifest.get("strategy_fidelity") or {}
    for strat, v in sf.items():
        print(
            f"    strategy_fidelity[{strat}]: "
            f"tier={v.get('fidelity_tier')} "
            f"tier_a={v.get('tier_a_eligible')} "
            f"missing={','.join(v.get('missing_feeds') or []) or '-'} "
            f"liq_provenance={v.get('liquidation_provenance')}"
        )
    dc = manifest.get("data_contract") or {}
    print(f"    data_contract.refused : {dc.get('refused')}")
    print(f"    data_contract.reasons : {dc.get('reasons')}")

    from collections import Counter, defaultdict

    exit_stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        r = str(t.get("exit_reason") or "unknown")
        exit_stats[r]["n"] += 1
        exit_stats[r]["pnl"] += float(t.get("pnl_usd", 0.0))

    out = {
        "window": {"start": args.start, "end": args.end},
        "symbols": symbols,
        "metrics": metrics,
        "n_trades": n,
        "wins": wins,
        "losses": n - wins,
        "total_pnl_usd": round(total_pnl, 2),
        "trades_summary": {
            k: {"n": int(v["n"]), "pnl_usd": round(v["pnl"], 2)}
            for k, v in sorted(exit_stats.items())
        },
        "manifest": manifest,
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(f"\n  JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
