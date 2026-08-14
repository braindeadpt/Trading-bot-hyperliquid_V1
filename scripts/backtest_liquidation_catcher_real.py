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

The real-feed backtest (docs/LIQUIDATION_CATCHER_REAL_BACKTEST.md) exposed a
structural loop: the strategy enters at the flush peak and the liquidation
stop-out (which validates the position side from the SAME window that
generated the signal) exits ~1 minute later — 16/16 trades at -142.42 USD.

This script also runs the variants that break the loop (--delay-min /
--stopout-off / --variants):

  * ``--delay-min N`` — entry waits N minutes after the flush (confirmation
    delay, the same idea as the ETH p90/30m fade harness) so the fade rides
    the reversal instead of the peak.
  * ``--stopout-off`` — bypass the liquidation stop-out for this strategy
    (the fade needs the flush to revert; exiting when the window validates
    the side is the loop). Hash-neutral: ``liquidation_stopout_min_notional_usd
    = inf`` in the BacktestConfig.
  * ``--variants`` — run the grid (delay x stopout) and print a comparison
    table against the current -142.42 baseline.

Usage:
    python scripts/backtest_liquidation_catcher_real.py [--start 2026-08-09]
        [--end 2026-08-14] [--symbols BTC ETH] [--json out.json]
    python scripts/backtest_liquidation_catcher_real.py --variants
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


def _prepare_db(cfg: Any, symbols: List[str], start_ms: int, end_ms: int) -> ResearchDatabase:
    """Copy candles (research) + liquidations/funding (live) into a temp DB."""
    research_src = ResearchDatabase(ResearchDatabase.resolve_path(cfg))
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    bt_db = ResearchDatabase(tmp_path)

    print("=" * 78)
    print(f"  LiquidationCatcher backtest - REAL feed {ms_to_dt(start_ms)} -> {ms_to_dt(end_ms, end=True)}")
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
    return bt_db


def run_cell(
    cfg: Any,
    bt_db: ResearchDatabase,
    symbols: List[str],
    start_ms: int,
    end_ms: int,
    *,
    delay_min: int = 0,
    stopout_on: bool = True,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Run one LiquidationCatcher backtest cell with the given variant knobs.

    ``delay_min`` → confirmation_delay_ms on the strategy (entry waits N min
    post-flush). ``stopout_on=False`` → liquidation stop-out disabled (inf
    floor, hash-neutral) so the fade can let the flush revert.
    """
    # Force-enable LiquidationCatcher with the production data contract.
    section = get_strategy_section(cfg, "liquidation_catcher")
    section["enabled"] = True
    section["auto_enable"] = True
    if delay_min:
        section["confirmation_delay_ms"] = delay_min * 60_000
    else:
        section.pop("confirmation_delay_ms", None)
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
        # Variant knob: None → calibrated constant (loop), inf → bypass
        # (the fade needs the flush to revert, not validate the side).
        liquidation_stopout_min_notional_usd=None if stopout_on else float("inf"),
    )

    engine = BacktestEngine(
        database=bt_db,
        strategy=strategy,
        config=bt_cfg,
        symbols=symbols,
        risk_config=risk_cfg,
    )

    if verbose:
        print("-" * 78)
    try:
        result = engine.run(start_ms=start_ms, end_ms=end_ms)
    except Exception as exc:
        print(f"  BACKTEST FAILED: {exc}")
        return {"error": str(exc)}

    metrics = result.get("metrics", {})
    trades = result.get("trades", [])
    manifest = result.get("manifest", {})
    n = int(metrics.get("n_trades", 0))
    wins = sum(1 for t in trades if float(t.get("pnl_usd", 0)) > 0)
    total_pnl = sum(float(t.get("pnl_usd", 0)) for t in trades)

    if verbose:
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

    from collections import defaultdict

    exit_stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        r = str(t.get("exit_reason") or "unknown")
        exit_stats[r]["n"] += 1
        exit_stats[r]["pnl"] += float(t.get("pnl_usd", 0.0))

    return {
        "delay_min": delay_min,
        "stopout_on": stopout_on,
        "n_trades": n,
        "wins": wins,
        "losses": n - wins,
        "total_pnl_usd": round(total_pnl, 2),
        "total_return_pct": float(metrics.get("total_return", 0)) * 100,
        "win_rate": (wins / n * 100 if n else 0.0),
        "trades_summary": {
            k: {"n": int(v["n"]), "pnl_usd": round(v["pnl"], 2)}
            for k, v in sorted(exit_stats.items())
        },
        "manifest": manifest,
    }


def _print_cell(header: str, cell: Dict[str, Any]) -> None:
    print(f"\n  {header}:")
    print(f"    n_trades      : {cell.get('n_trades', 0)}")
    print(f"    win_rate      : {cell.get('win_rate', 0):.1f}%")
    print(f"    total_pnl_usd : {cell.get('total_pnl_usd', 0):.2f}")
    ts = cell.get("trades_summary") or {}
    for reason, v in sorted(ts.items()):
        print(f"    {str(reason)[:30]:30} n={v['n']:3d} pnl=${v['pnl_usd']:8.2f}")


BASELINE_CELL = "delay=0 · stopout=ON (loop actual)"


def ms_to_dt(ts: int, end: bool = False) -> str:
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    return (dt.replace(hour=23, minute=59, second=59, microsecond=999000)
            .strftime("%Y-%m-%d") if end else dt.strftime("%Y-%m-%d"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-08-09")
    ap.add_argument("--end", default="2026-08-14")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--json", type=Path, help="write full result as JSON")
    ap.add_argument("--delay-min", type=int, default=0,
                    help="entry confirmation delay in minutes (0 = immediate)")
    ap.add_argument("--stopout-off", action="store_true",
                    help="disable the liquidation stop-out for this strategy")
    ap.add_argument("--variants", action="store_true",
                    help="run the delay x stopout grid and compare vs baseline")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms = ms(args.start)
    end_ms = ms(args.end, end=True)
    cfg = load_config(ROOT / "config" / "settings.yaml")

    if args.variants:
        bt_db = _prepare_db(cfg, symbols, start_ms, end_ms)
        grid = [
            (0, True),      # baseline: the loop (flush → stop-out next minute)
            (0, False),     # stop-out bypass only
            (10, True),     # 10 min confirmation delay, stop-out still on
            (10, False),    # delay + bypass
            (30, True),     # 30 min delay (harness fade hold)
            (30, False),    # delay + bypass
        ]
        print("\n  Variantes (delay-min x stopout):")
        results: Dict[str, Dict[str, Any]] = {}
        for delay, stopout_on in grid:
            tag = f"delay={delay} stopout={'ON' if stopout_on else 'OFF'}"
            print(f"\n  >>> {tag}")
            cell = run_cell(cfg, bt_db, symbols, start_ms, end_ms,
                            delay_min=delay, stopout_on=stopout_on, verbose=False)
            results[tag] = cell

        print("\n" + "=" * 78)
        print("  COMPARAÇÃO (vs baseline atual: 16 trades, -142.42 USD)")
        print("=" * 78)
        print(f"  {'variante':34} {'n':>3} {'WR':>6} {'pnl':>10}")
        for tag, cell in results.items():
            print(f"  {tag:34} {cell.get('n_trades', 0):>3} "
                  f"{cell.get('win_rate', 0):>5.1f}% "
                  f"{cell.get('total_pnl_usd', 0):>10.2f}")
        base = results.get(BASELINE_CELL, {})
        base_pnl = base.get("total_pnl_usd", 0)
        for tag, cell in results.items():
            if tag == BASELINE_CELL:
                continue
            pnl = cell.get("total_pnl_usd", 0)
            print(f"  {('delta vs baseline: ' + tag):34} {'':>3} {'':>6} "
                  f"{pnl - base_pnl:>+10.2f}")
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(
                json.dumps({"window": {"start": args.start, "end": args.end},
                            "symbols": symbols,
                            "baseline": base,
                            "cells": results}, indent=2, default=str),
                encoding="utf-8",
            )
            print(f"\n  JSON -> {args.json}")
        return 0

    bt_db = _prepare_db(cfg, symbols, start_ms, end_ms)
    cell = run_cell(cfg, bt_db, symbols, start_ms, end_ms,
                    delay_min=args.delay_min, stopout_on=not args.stopout_off)
    _print_cell(f"Resultado (delay={args.delay_min}min, stopout="
                f"{'ON' if not args.stopout_off else 'OFF'})", cell)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(cell, indent=2, default=str), encoding="utf-8")
        print(f"\n  JSON -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
