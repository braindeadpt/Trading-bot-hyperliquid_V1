"""A/B: Phase-08 regime router — with vs without (the evidence it never had).

The regime router (src/core/phase08_regime_router.py) is ON in the live
engine but was never validated: every backtest that informed strategy
decisions ran single-strategy with the router effectively inert (a lone
strategy equals its own fallback). This script produces the missing A/B:

  1. Runs VolatilityBreakout + VWAPDeviation backtests (production configs,
     full candle history) WITHOUT the router.
  2. For every trade, recomputes the 15m-closed-candle ADX at entry time —
     the exact input the live engine uses (calculate_adx, period 14).
  3. Applies the router's hard gate the way the live multi-strategy batch
     would (VB eligible in trend/expansion; VWAP in range/low_vol, with
     fallback promoting VWAP when no strategy is eligible — i.e. unknown).
  4. Reports per-strategy and combined PnL with vs without, plus the PnL of
     the trades the router would have BLOCKED (the key evidence).

Verdict rule: if blocked trades are net negative, the router demonstrably
saves money (and VB could return to execution under its protection); if net
positive, the router premise is wrong and VB should stay shadow.

Research-only: never writes bot.db, never touches the frozen Fase 10 window.
"""

from __future__ import annotations

import bisect
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.core.phase08_regime_router import classify_market_regime
from src.data.database import Database
from src.strategies.indicators import calculate_adx
from src.strategies.volatility_breakout import VolatilityBreakout
from src.strategies.vwap_deviation import VWAPDeviation
from src.utils.config import load_config

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit", "src.backtest.engine", "src.strategies",
    "src.core.risk_manager", "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

FULL_START, FULL_END = "2026-05-18", "2026-08-07"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]

# Tunable window (the full 80-day backtest is slow on the live WAL DB).
START_ARG = "2026-07-08"   # --start override
END_ARG = "2026-08-07"     # --end override
SYMBOLS_ARG = ["BTC", "ETH", "SOL", "HYPE"]  # --symbols override

# Router gate in the 2-strategy world (VB + VWAP, fallback VWAP):
# VB trades pass only in trend/expansion; VWAP passes in range/low_vol and
# is the fallback when no strategy is eligible (unknown regime).
VB_ALLOWED_REGIMES = frozenset({"trend", "expansion"})
VWAP_ALLOWED_REGIMES = frozenset({"range", "low_vol", "unknown"})


def ms(s: str, end: bool = False) -> int:
    d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        d = d.replace(hour=23, minute=59, second=59)
    return int(d.timestamp() * 1000)


def build_cfg(cfg: Any) -> BacktestConfig:
    """Mirror live production settings (router OFF — we want raw trades)."""
    return BacktestConfig(
        initial_capital=float(cfg.get("risk.initial_capital", 10_000.0)),
        commission_pct=float(cfg.get("risk.taker_fee_pct", 0.045)),
        slippage_bps=float(cfg.get("backtest.slippage_bps", 2.0)),
        max_positions=int(cfg.get("risk.max_positions", 3)),
        per_trade_risk_pct=float(cfg.get("risk.per_trade_risk_pct", 1.0)),
        tca_enabled=bool(cfg.get("execution.tca_enabled", True)),
        paper_slippage_pct=float(cfg.get("risk.paper_slippage_pct", 0.02)),
        use_regime_weights=False,
        use_cooldown=True,
        use_microstructure_proxy=True,
        use_risk_manager=True,
        use_volatility_circuit=True,
        use_funding_blackout=True,
        use_external_feeds_replay=True,
        use_phase08_regime_router=False,  # raw trades — router applied post-hoc
        max_daily_trades=int(cfg.get("risk.max_daily_trades", 0)),
    )


def precompute_adx(db: Database, symbols: List[str], start_ms: int, end_ms: int) -> Dict[str, List[Tuple[int, Optional[float]]]]:
    """ADX(14) per 15m closed candle, per symbol: [(bar_ts, adx), ...]."""
    out: Dict[str, List[Tuple[int, Optional[float]]]] = {}
    for sym in symbols:
        candles = db.get_candles(sym, "15m", limit=100_000, start_ms=start_ms, end_ms=end_ms)
        series: List[Tuple[int, Optional[float]]] = []
        for i in range(len(candles)):
            adx = None
            if i >= 2 * 14:
                adx = calculate_adx(candles[i - 28 : i + 1], 14)
            series.append((candles[i].timestamp_ms, adx))
        out[sym] = series
        print(f"  ADX series {sym}: {len(series)} bars")
    return out


def adx_at(series: List[Tuple[int, Optional[float]]], ts: int) -> Optional[float]:
    """ADX of the last closed 15m bar at or before ``ts``."""
    if not series:
        return None
    idx = bisect.bisect_right([t for t, _ in series], ts) - 1
    if idx < 0:
        return None
    return series[idx][1]


def run_strategy(cfg: Any, db: Database, cls: Any, section_path: str,
                 start_ms: int, end_ms: int, symbols: List[str]) -> List[Dict[str, Any]]:
    section = dict(cfg.get(section_path, {}) or {})
    section["enabled"] = True
    engine = BacktestEngine(
        database=db,
        strategy=cls(section),
        config=build_cfg(cfg),
        symbols=symbols,
        risk_config=dict(cfg.get("risk", {}) or {}),
    )
    result = engine.run(start_ms=start_ms, end_ms=end_ms)
    return result.get("trades", [])


def summarize(trades: List[Dict[str, Any]], label: str) -> Dict[str, Any]:
    n = len(trades)
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in trades)
    wins = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    return {
        "label": label,
        "n": n,
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else 0.0,
        "net_pnl": round(pnl, 2),
        "expectancy": round(pnl / n, 2) if n else 0.0,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default=START_ARG)
    ap.add_argument("--end", default=END_ARG)
    ap.add_argument("--symbols", default=",".join(SYMBOLS_ARG))
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    s_ms, e_ms = ms(args.start), ms(args.end, True)

    print("=" * 78)
    print("  REGIME ROUTER A/B — with vs without (Phase-08, trade-level)")
    print(f"  {args.start} -> {args.end} | symbols: {','.join(symbols)}")
    print("=" * 78)

    print("\n[0] Precomputing ADX(14) on 15m closed candles...")
    t0 = time.time()
    adx_series = precompute_adx(db, SYMBOLS, s_ms, e_ms)
    print(f"    done in {time.time()-t0:.0f}s")

    adx_range = float((cfg.get("strategy.phase08", {}) or {}).get("regime_router", {})
                      .get("adx_range_threshold", cfg.get("strategy.adx_range_threshold", 20.0)))
    adx_trend = float((cfg.get("strategy.phase08", {}) or {}).get("regime_router", {})
                      .get("adx_trend_threshold", cfg.get("strategy.adx_trend_threshold", 25.0)))

    results: List[Dict[str, Any]] = []
    blocked_report: List[Dict[str, Any]] = []
    combined_kept: List[Dict[str, Any]] = []
    combined_all: List[Dict[str, Any]] = []

    for name, cls, path, allowed in [
        ("VolatilityBreakout", VolatilityBreakout, "strategy.volatility_breakout", VB_ALLOWED_REGIMES),
        ("VWAPDeviation", VWAPDeviation, "strategy.vwap_deviation", VWAP_ALLOWED_REGIMES),
    ]:
        print(f"\n[{name}] running raw backtest...")
        t1 = time.time()
        trades = run_strategy(cfg, db, cls, path, s_ms, e_ms, symbols)
        print(f"    {len(trades)} trades in {time.time()-t1:.0f}s")

        kept, blocked = [], []
        for t in trades:
            adx = adx_at(adx_series.get(t["symbol"], []), int(t["entry_time"]))
            regime = classify_market_regime(
                adx, adx_range_threshold=adx_range, adx_trend_threshold=adx_trend
            )
            t["_regime"] = regime
            t["_adx"] = adx
            if regime in allowed:
                kept.append(t)
            else:
                blocked.append(t)

        no_router = summarize(trades, f"{name} (without router)")
        with_router = summarize(kept, f"{name} (with router)")
        results.append(no_router)
        results.append(with_router)
        combined_all.extend(trades)
        combined_kept.extend(kept)

        for b in blocked:
            blocked_report.append({
                "strategy": name, "symbol": b["symbol"], "side": b.get("side"),
                "regime": b["_regime"], "adx": b["_adx"],
                "pnl_usd": round(float(b.get("pnl_usd", 0.0)), 2),
            })

        print(f"    without router: {no_router}")
        print(f"    with router:    {with_router}")
        print(f"    BLOCKED:        {summarize(blocked, 'blocked')}")

    print("\n" + "=" * 78)
    print("  RESULTADO")
    print("=" * 78)
    hdr = f"{'':28}{'n':>5}{'WR%':>7}{'PF':>7}{'E[x]':>8}{'net':>10}"
    print(hdr)
    for r in results:
        print(f"{r['label']:28}{r['n']:>5}{r['win_rate']:>7}{r['profit_factor']:>7}"
              f"{r['expectancy']:>8}{r['net_pnl']:>10}")

    print("\n--- Combined (indicative) ---")
    for r in (summarize(combined_all, "combined without"), summarize(combined_kept, "combined with")):
        print(f"{r['label']:28}{r['n']:>5}{r['win_rate']:>7}{r['profit_factor']:>7}"
              f"{r['expectancy']:>8}{r['net_pnl']:>10}")

    print("\n--- Blocked trades by regime ---")
    by_regime: Dict[str, List[Dict[str, Any]]] = {}
    for b in blocked_report:
        by_regime.setdefault(f"{b['regime']} | {b['strategy']}", []).append(b)
    for key, rows in sorted(by_regime.items()):
        pnl = sum(r["pnl_usd"] for r in rows)
        wins = sum(1 for r in rows if r["pnl_usd"] > 0)
        print(f"  {key:34} n={len(rows):>4}  wins={wins:>3}  pnl={pnl:>9.2f}")

    blocked_pnl = sum(b["pnl_usd"] for b in blocked_report)
    print(f"\n  TOTAL blocked PnL: {blocked_pnl:+.2f} USD  "
          f"({'router SAVES money' if blocked_pnl < 0 else 'router DESTROYS money'})")

    db.close()


if __name__ == "__main__":
    main()
