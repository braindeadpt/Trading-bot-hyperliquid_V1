"""Factor-level predictive screen — IC / ICIR / decay, per window.

Cheap hypothesis filter that runs BEFORE strategy sweeps: for each factor
series, measure the Spearman rank correlation (IC) between the factor at
candle close t and the forward return t -> t+h, per fixed window. A factor
whose IC is sign-consistent across windows has something a strategy could
harvest; one with IC ~ 0 kills every strategy built on it at the cheapest
possible level.

Factors (all from persisted data, no live calls):
  cvd_5m        - order-flow imbalance: sum(buy-sell)/sum(volume) over 5m
  liq_not_5m    - liquidation notional over trailing 5m (bot.db feed)
  funding       - funding_rate column at close
  oi_delta_5m   - sum(oi_delta) over 5m
  vol_surge     - volume / rolling-median volume (activity, direction-free)
  ret_15m       - trailing 15m return (momentum factor)

Targets: forward returns at +5m / +15m / +60m (the "decay" curve).

Verdict language (advisory, not a KEEP):
  SIGNAL   - |mean IC| >= 0.02 and sign-consistent in >=3/4 windows at some
             horizon
  FLAT     - otherwise (no evidence; building on this factor is fishing)

Caveat (recorded honestly): forward returns at 1m sampling overlap, so
t-stats would be inflated; IC magnitude + window sign-consistency is the
screen, not a formal test. Strategy-level proof still runs through the
overnight runner's gates.

Usage:
    python scripts/research/factor_screen.py --start 2026-05-18 --end 2026-09-08
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ARTIFACT_DIR = ROOT / "data" / "research" / "overnight_experiments"
BOT_DB = ROOT / "data" / "live" / "bot.db"

HORIZONS_MIN = (5, 15, 60)
IC_SIGNAL_FLOOR = 0.02


def _window_ms(start: str, end: str) -> tuple:
    s = int(datetime.strptime(start, "%Y-%m-%d").replace(
        tzinfo=timezone.utc).timestamp() * 1000)
    e = int(datetime.strptime(end, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, microsecond=999000,
        tzinfo=timezone.utc).timestamp() * 1000)
    return s, e


def load_candles(db_path: Path, symbol: str, s_ms: int, e_ms: int):
    import sqlite3
    import pandas as pd
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "SELECT timestamp_ms, close, volume, buy_volume, sell_volume,"
        "       oi_delta, funding_rate FROM candles_1m"
        " WHERE symbol=? AND timestamp_ms BETWEEN ? AND ?"
        " ORDER BY timestamp_ms",
        conn, params=(symbol, s_ms, e_ms))
    conn.close()
    return df


def load_liq(db_path: Path, symbol: str, s_ms: int, e_ms: int):
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT timestamp_ms, notional_usd FROM liquidation_events"
            " WHERE symbol=? AND timestamp_ms BETWEEN ? AND ?",
            (symbol, s_ms, e_ms)).fetchall()
    except sqlite3.Error:
        rows = []
    conn.close()
    return rows


def build_factors(df, liq_rows) -> Any:
    """One factor column per idea, aligned to candle close times."""
    import numpy as np
    import pandas as pd
    df = df.copy()
    buy = df["buy_volume"].fillna(0.0)
    sell = df["sell_volume"].fillna(0.0)
    vol = df["volume"].fillna(0.0)

    flow = (buy - sell).rolling(5, min_periods=1).sum()
    tot = vol.rolling(5, min_periods=1).sum()
    df["cvd_5m"] = flow / tot.replace(0, np.nan)

    df["oi_delta_5m"] = df["oi_delta"].fillna(0.0).rolling(5, min_periods=1).sum()
    df["funding"] = df["funding_rate"]
    med = vol.rolling(60, min_periods=10).median()
    df["vol_surge"] = vol / med.replace(0, np.nan)
    df["ret_15m"] = df["close"].pct_change(15)

    # liquidation notional per trailing 5m bucket — sparse join
    if liq_rows:
        liq = np.zeros(len(df))
        ts = df["timestamp_ms"].to_numpy()
        for lts, notional in liq_rows:
            i = np.searchsorted(ts, lts, side="right") - 1
            if i >= 0:
                liq[i] += float(notional or 0.0)
        df["liq_not_5m"] = (
            pd.Series(liq).rolling(5, min_periods=1).sum().to_numpy())
    else:
        df["liq_not_5m"] = 0.0
    return df


FACTORS = ("cvd_5m", "liq_not_5m", "funding", "oi_delta_5m",
           "vol_surge", "ret_15m")


def screen_window(df, factor: str, horizons=HORIZONS_MIN) -> Dict[int, Optional[float]]:
    """Spearman IC of factor vs forward return per horizon (in minutes)."""
    out: Dict[int, Optional[float]] = {}
    f = df[factor]
    for h in horizons:
        fwd = df["close"].shift(-h) / df["close"] - 1.0
        pair = df.assign(_f=f, _r=fwd).dropna(subset=["_f", "_r"])
        if len(pair) < 100 or pair["_f"].nunique() < 2:
            out[h] = None  # constant factor -> IC undefined, not zero
            continue
        out[h] = float(pair["_f"].corr(pair["_r"], method="spearman"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--split-days", type=int, default=30)
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    from scripts.research.regime_router_a_b_test import split_windows
    windows = split_windows(args.start, args.end, args.split_days)
    print(f"factor_screen: {len(windows)} windows x {len(symbols)} symbols "
          f"x {len(FACTORS)} factors x {len(HORIZONS_MIN)} horizons")

    rows: List[Dict[str, Any]] = []
    for sym in symbols:
        for w_start, w_end in windows:
            s_ms, e_ms = _window_ms(w_start, w_end)
            # pull 1 extra day forward so +60m returns exist at the edge
            df = load_candles(BOT_DB, sym, s_ms, e_ms + 86_400_000)
            if df.empty:
                rows.append({"symbol": sym, "window": w_end,
                             "error": "no candles"})
                continue
            liq = load_liq(BOT_DB, sym, s_ms, e_ms + 86_400_000)
            df = build_factors(df, liq)
            df = df[df["timestamp_ms"] <= e_ms]  # targets may reach past end
            for factor in FACTORS:
                ics = screen_window(df, factor)
                rows.append({"symbol": sym, "window": w_end,
                             "factor": factor,
                             "ic": {str(k): (round(v, 4) if v is not None else None)
                                    for k, v in ics.items()}})

    # ---- aggregate per (factor, horizon) across windows/symbols ----
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if "error" in r:
            continue
        for h, ic in r["ic"].items():
            if ic is None:
                continue
            key = f"{r['factor']}@{h}m"
            agg.setdefault(key, []).append(ic)

    print("\n  factor@horizon          meanIC   sign+ windows  verdict")
    report: Dict[str, Any] = {}
    for key, ics in sorted(agg.items()):
        mean_ic = sum(ics) / len(ics)
        pos = sum(1 for x in ics if x > 0)
        neg = sum(1 for x in ics if x < 0)
        cons = max(pos, neg)
        verdict = ("SIGNAL" if abs(mean_ic) >= IC_SIGNAL_FLOOR and cons >= 3
                   else "FLAT")
        report[key] = {"mean_ic": round(mean_ic, 4), "n_cells": len(ics),
                       "sign_consistent": cons, "verdict": verdict}
        print(f"  {key:<22} {mean_ic:+.4f}   {cons}/{len(ics)}"
              f"          {verdict}")

    artifact = {
        "kind": "factor_screen",
        "generated_ms": int(time.time() * 1000),
        "span": {"start": args.start, "end": args.end},
        "symbols": symbols,
        "windows": windows,
        "horizons_min": list(HORIZONS_MIN),
        "ic_signal_floor": IC_SIGNAL_FLOOR,
        "per_cell": rows,
        "aggregate": report,
        "caveat": ("overlapping 1m forward returns inflate any t-stat; "
                   "IC + window sign-consistency is the screen only"),
    }
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACT_DIR / (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        "_factor_screen.json")
    out.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"\nartifact: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
