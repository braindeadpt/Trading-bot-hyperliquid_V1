"""VB stop-out A/B — does the calibrated liquidation stop-out cut the short bleed?

Context: VB forensics (scripts/vb_regime_forensics.py) showed shorts at
WR ~7.7%, net -$66.32 — the biggest bleed slice. The liquidation stop-out
(``liquidation_stop_out``) exits a position when the rolling 5m liquidation
window validates the position SIDE at/above a notional floor. Its floor was
recalibrated 2026-08-14 from a provisional 5.0M to the real multi-venue p90
(2.5M, docs/LIQUIDATION_STOPOUT_FLOOR_CALIBRATION.md).

This script runs the SAME VB backtest twice over the full candle history:

  * BASELINE  — stop-out disabled (``min_notional = inf``): the pre-stop-out
                trade set, where shorts bleed.
  * STOPOUT   — the calibrated floor (2.5M): the same trades with the
                liquidation window able to exit shorts (and longs) first.

And compares the two on the slice that matters — SHORTS (n, WR, net, avg
loss, exit_reason mix) — plus the overall and per-symbol deltas. It also
counts how many baseline shorts flipped to ``liquidation_stop_out`` and
what the stop-out *prevented* (the loss those trades would otherwise have
taken to stop_loss / trailing).

Read-only: two VB backtests (~15 min each) over the frozen window, dumps
the trade sets to CSV, never touches bot.db writes or the frozen config.

Usage:
  python scripts/vb_stopout_ab.py            # both runs (~30 min)
  python scripts/vb_stopout_ab.py --off      # stop-out disabled (baseline)
  python scripts/vb_stopout_ab.py --on       # calibrated floor (2.5M)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.backtest.engine import BacktestConfig, BacktestEngine
from src.core.liquidation_stopout import LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD
from src.core.phase08_regime_router import classify_market_regime
from src.data.database import Database
from src.strategies.volatility_breakout import VolatilityBreakout
from src.utils.config import load_config

# Reuse the forensics harness (stats + regime tagging + the ADX machinery).
from regime_router_a_b_test import adx_at, precompute_adx  # noqa: E402
from vb_regime_forensics import (  # noqa: E402
    FULL_END,
    FULL_START,
    SYMBOLS,
    build_cfg,
    fmt,
    ms,
    stats,
    tag_regime,
)

logging.basicConfig(level=logging.ERROR)
for _n in (
    "src.core.volatility_circuit", "src.backtest.engine", "src.strategies",
    "src.core.risk_manager", "src.core.funding_blackout",
):
    logging.getLogger(_n).setLevel(logging.ERROR)

OUT_DIR = ROOT / "data" / "backtests"


def build_cfg_with_stopout(cfg: Any, floor: Optional[float]) -> BacktestConfig:
    """Production VB config with the liquidation stop-out floor overridden.

    ``None`` → the calibrated constant (live parity). ``float("inf")`` →
    disabled (baseline). Any other value → explicit sensitivity sweep.
    """
    c = build_cfg(cfg)
    c.liquidation_stopout_min_notional_usd = floor
    return c


def run_once(cfg: Any, floor: Optional[float], tag: str) -> Dict[str, Any]:
    """Run the VB backtest once and return {trades, elapsed, csv_path}."""
    db = Database(cfg.get("database.path", "data/live/bot.db"))
    s_ms, e_ms = ms(FULL_START), ms(FULL_END, True)
    p08 = (cfg.get("strategy.phase08", {}) or {}).get("regime_router", {}) or {}
    adx_range = float(p08.get("adx_range_threshold",
                              cfg.get("strategy.adx_range_threshold", 20.0)))
    adx_trend = float(p08.get("adx_trend_threshold",
                              cfg.get("strategy.adx_trend_threshold", 25.0)))

    t0 = time.time()
    print(f"\n[{tag}] Precomputing ADX(14) on 15m candles...")
    adx_series = precompute_adx(db, SYMBOLS, s_ms, e_ms)
    print(f"[{tag}] ADX done in {time.time()-t0:.0f}s")

    print(f"[{tag}] Running VB backtest (floor={floor if floor is not None else 'const(2.5M)'})...")
    section = dict(cfg.get("strategy.volatility_breakout", {}) or {})
    section["enabled"] = True
    engine = BacktestEngine(
        database=db,
        strategy=VolatilityBreakout(section),
        config=build_cfg_with_stopout(cfg, floor),
        symbols=SYMBOLS,
        risk_config=dict(cfg.get("risk", {}) or {}),
    )
    result = engine.run(start_ms=s_ms, end_ms=e_ms)
    trades = tag_regime(result.get("trades", []), adx_series, adx_range, adx_trend)
    elapsed = time.time() - t0
    print(f"[{tag}] {len(trades)} trades in {elapsed:.0f}s")

    csv_path = OUT_DIR / f"vb_stopout_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "entry_time", "symbol", "side", "entry_price", "exit_price",
            "pnl_usd", "pnl_pct", "r_multiple", "exit_reason",
            "regime", "adx", "hold_min",
        ])
        w.writeheader()
        for t in trades:
            w.writerow({
                "entry_time": t.get("entry_time"), "symbol": t.get("symbol"),
                "side": t.get("side"), "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"), "pnl_usd": t.get("pnl_usd"),
                "pnl_pct": t.get("pnl_pct"), "r_multiple": t.get("r_multiple"),
                "exit_reason": t.get("exit_reason"), "regime": t["_regime"],
                "adx": t.get("_adx"), "hold_min": t.get("_hold_min"),
            })
    print(f"[{tag}] CSV: {csv_path}")
    db.close()
    return {"trades": trades, "elapsed": elapsed, "csv_path": csv_path}


def short_slice(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    return stats([t for t in trades if t["side"] == "short"])


def compare(base: Dict[str, Any], so: Dict[str, Any]) -> int:
    b, s = base["trades"], so["trades"]
    b_shorts = [t for t in b if t["side"] == "short"]
    s_shorts = [t for t in s if t["side"] == "short"]

    print("\n" + "=" * 82)
    print("  VB STOP-OUT A/B — baseline (sem stop-out) vs calibrado (2.5M)")
    print(f"  {FULL_START} -> {FULL_END} | symbols: {','.join(SYMBOLS)}")
    print("=" * 82)
    print(f"\n  OVERALL   baseline: {fmt(stats(b))}")
    print(f"            stop-out: {fmt(stats(s))}")
    print(f"\n  SHORTS    baseline: {fmt(short_slice(b))}")
    print(f"            stop-out: {fmt(short_slice(s))}")
    print(f"\n  LONGS     baseline: {fmt(stats([t for t in b if t['side'] == 'long']))}")
    print(f"            stop-out: {fmt(stats([t for t in s if t['side'] == 'long']))}")

    # Exit-reason mix on shorts — did the stop-out fire, and what did it replace?
    def reasons(ts: List[Dict[str, Any]]) -> Counter:
        return Counter(str(t.get("exit_reason", "?")) for t in ts)

    print("\n  --- SHORTS: motivo de saída (baseline -> stop-out) ---")
    br, sr = reasons(b_shorts), reasons(s_shorts)
    for reason in sorted(set(br) | set(sr)):
        if br.get(reason) or sr.get(reason):
            print(f"    {str(reason)[:28]:28} {br.get(reason, 0):>4} -> {sr.get(reason, 0):>4}")

    so_fired = [t for t in s_shorts if str(t.get("exit_reason", "")).startswith("liquidation_stop_out")]
    print(f"\n  SHORTS saídos por liquidation_stop_out: {len(so_fired)} "
          f"(P&L ${sum(float(t['pnl_usd']) for t in so_fired):.2f})")

    # Match trades across runs by entry_time+symbol+side to see the SAME
    # short's fate under both regimes.
    b_key = {(t["entry_time"], t["symbol"], t["side"]): t for t in b}
    s_key = {(t["entry_time"], t["symbol"], t["side"]): t for t in s}
    matched = []
    for key, bt in b_key.items():
        st = s_key.get(key)
        if st is not None:
            matched.append((bt, st))
    print(f"\n  Trades emparelhados (mesma entrada, ambos os runs): {len(matched)}")

    # Delta on shorts that the stop-out actually intercepted.
    inter = [st for bt, st in matched if bt["side"] == "short"
             and str(st.get("exit_reason", "")).startswith("liquidation_stop_out")]
    print(f"  Shorts baseline que o stop-out interceptou: {len(inter)}")
    if inter:
        base_pnl = sum(float(b_key[(t['entry_time'], t['symbol'], t['side'])]['pnl_usd'])
                       for t in inter)
        so_pnl = sum(float(t["pnl_usd"]) for t in inter)
        print(f"    P&L desses shorts — baseline: ${base_pnl:.2f} -> stop-out: ${so_pnl:.2f} "
              f"(delta ${so_pnl - base_pnl:+.2f})")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--off", action="store_true", help="baseline only (stop-out disabled)")
    grp.add_argument("--on", action="store_true", help="stop-out only (calibrated floor)")
    ap.add_argument("--json", type=Path, help="write comparison as JSON")
    args = ap.parse_args()

    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    modes: List[Tuple[str, Optional[float]]] = []
    if args.off:
        modes = [("off", float("inf"))]
    elif args.on:
        modes = [("on", None)]
    else:
        modes = [("off", float("inf")), ("on", None)]

    results: Dict[str, Dict[str, Any]] = {}
    for tag, floor in modes:
        results[tag] = run_once(cfg, floor, tag)

    if len(results) == 2:
        rc = compare(results["off"], results["on"])
        if args.json:
            summary = {
                "window": f"{FULL_START} -> {FULL_END}",
                "floor_calibrated_usd": LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD,
                "off": {
                    "csv": str(results["off"]["csv_path"]),
                    "overall": stats(results["off"]["trades"]),
                    "shorts": short_slice(results["off"]["trades"]),
                },
                "on": {
                    "csv": str(results["on"]["csv_path"]),
                    "overall": stats(results["on"]["trades"]),
                    "shorts": short_slice(results["on"]["trades"]),
                },
            }
            args.json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(f"\nJSON: {args.json}")
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
