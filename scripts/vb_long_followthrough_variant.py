"""VB long-only + follow-through variant — same forensics CSV, new simulation.

Tests the backlog hypothesis (RESEARCH_BACKLOG #1.5): "require the candle
*after* the breakout to hold above the range before entering". The VB
strategy breaks a Bollinger band (BB 20/2.0) on 15m closes; the signal fires
at the close of the breakout bar and the backtest fills at that close
(entry_time == breakout bar ts, entry_price == its close + slippage). The
follow-through rule waits one bar: only keep the trade if the candle AFTER
the breakout (bar i+1) closes back on the breakout side of the band.

Two views of the SAME variant:

  * FT filter (upper bound)     — keep only follow-through trades, entries
    unchanged (what a perfect selector would earn).
  * FT delayed entry (tradeable)— the honest version: enter at the OPEN of
    bar i+2 (the bar after the confirmation candle) instead of the breakout
    close, recomputing PnL from the same exit_price. Costs the gap between
    breakout close and delayed open — this is the real entry the rule
    produces.

Combined with long-only (the forensics verdict direction (a): shorts are the
structural bleed, WR 7.7%, -$66.32), the surviving slice is compared against
the baseline (all trades), long-only, and the live expansion-only gate.

Read-only: never writes bot.db. Uses the same BB implementation as the
strategy (SMA20 +- 2*pop-sigma on prior closes).
"""

from __future__ import annotations

import bisect
import csv
import glob
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
BB_PERIOD = 20
BB_STD = 2.0


def bollinger_bands(prices: List[float], period: int = BB_PERIOD,
                    std: float = BB_STD) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Identical to src/strategies/indicators.calculate_bollinger_bands."""
    if len(prices) < period:
        return None, None, None
    window = prices[-period:]
    middle = sum(window) / period
    variance = sum((p - middle) ** 2 for p in window) / period
    sigma = variance ** 0.5
    return middle - std * sigma, middle, middle + std * sigma


def load_15m(db: sqlite3.Connection) -> Dict[str, List[Tuple[int, float, float, float, float]]]:
    """symbol -> sorted [(ts, open, high, low, close)] (open-stamped bars)."""
    out: Dict[str, List[Tuple[int, float, float, float, float]]] = {}
    for sym in SYMBOLS:
        rows = db.execute(
            "SELECT timestamp_ms, open, high, low, close FROM candles_15m "
            "WHERE symbol = ? ORDER BY timestamp_ms ASC", (sym,),
        ).fetchall()
        out[sym] = [(int(ts), float(o), float(h), float(l), float(c))
                    for ts, o, h, l, c in rows]
    return out


def stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    pnl = sum(float(t.get("pnl_usd", 0.0)) for t in trades)
    wins = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) > 0]
    losses = [float(t["pnl_usd"]) for t in trades if float(t.get("pnl_usd", 0)) <= 0]
    return {
        "n": n,
        "win_rate": 100.0 * len(wins) / n if n else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "expectancy": pnl / n if n else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else 0.0,
        "net": pnl,
    }


def fmt(r: Dict[str, Any]) -> str:
    return (f"n={r['n']:>4} WR={r['win_rate']:>5.1f}% "
            f"avgW=${r['avg_win']:>7.2f} avgL=${r['avg_loss']:>7.2f} "
            f"E[x]=${r['expectancy']:>7.2f} PF={r['profit_factor']:>5.2f} "
            f"net=${r['net']:>9.2f}")


def enrich(trades: List[Dict[str, Any]],
           bars: Dict[str, List[Tuple[int, float, float, float, float]]]
           ) -> Tuple[List[Dict[str, Any]], int, int]:
    """Attach band/follow-through/delayed-entry fields. Returns (trades, ok, repro).

    ok    — trades with a reconstructable breakout bar (indexing worked).
    repro — of those, how many reproduce the original signal (close(i) beyond
            the band) — validates that the indexing matches the backtest.
    """
    ok = 0
    repro = 0
    for t in trades:
        sym = t["symbol"]
        ts_list = [b[0] for b in bars[sym]]
        i = bisect.bisect_left(ts_list, int(t["entry_time"]))
        if i <= 0 or i + 2 >= len(ts_list) or ts_list[i] != int(t["entry_time"]):
            t["_ft"] = None
            t["_delayed_entry"] = None
            continue
        o_i, h_i, l_i, c_i = bars[sym][i][1:]
        closes_prior = [b[4] for b in bars[sym][i - BB_PERIOD:i]]
        lower, _mid, upper = bollinger_bands(closes_prior)
        if upper is None:
            t["_ft"] = None
            t["_delayed_entry"] = None
            continue
        side = t["side"]
        sgn = 1.0 if side == "long" else -1.0
        band = upper if side == "long" else lower
        ok += 1
        broke = (c_i > upper) if side == "long" else (c_i < lower)
        if broke:
            repro += 1
        _t1, o_n1, _, _, c_n1 = bars[sym][i + 1]
        _t2, o_n2, _, _, _ = bars[sym][i + 2]
        ft = (c_n1 > band) if side == "long" else (c_n1 < band)
        t["_ft"] = bool(ft)
        t["_band"] = band
        t["_breakout_close"] = c_i
        t["_confirm_close"] = c_n1
        t["_delayed_entry"] = o_n2
        entry = float(t["entry_price"])
        exit_px = float(t["exit_price"])
        pnl_pct = float(t["pnl_pct"])
        if abs(pnl_pct) > 1e-9:
            pnl_pct_delay = (exit_px - o_n2) / o_n2 * sgn * 100.0
            t["_pnl_usd_delay"] = float(t["pnl_usd"]) * pnl_pct_delay / pnl_pct
        else:
            t["_pnl_usd_delay"] = float(t["pnl_usd"])
        t["_entry_gap_pct"] = (o_n2 - entry) / entry * 100.0 * sgn
    return trades, ok, repro


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="",
                    help="forensics CSV (default: latest vb_forensics_*.csv)")
    args = ap.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
    else:
        found = sorted(glob.glob(str(ROOT / "data" / "backtests" / "vb_forensics_*.csv")))
        if not found:
            print("no vb_forensics CSV found — run vb_regime_forensics.py first")
            return
        csv_path = Path(found[-1])

    with open(csv_path, "r", encoding="utf-8") as f:
        raw = list(csv.DictReader(f))
    trades: List[Dict[str, Any]] = []
    for r in raw:
        trades.append({
            "entry_time": int(r["entry_time"]), "symbol": r["symbol"],
            "side": r["side"], "entry_price": float(r["entry_price"]),
            "exit_price": float(r["exit_price"]),
            "pnl_usd": float(r["pnl_usd"]), "pnl_pct": float(r["pnl_pct"]),
            "r_multiple": float(r["r_multiple"] or 0),
            "exit_reason": r["exit_reason"], "regime": r["regime"],
            "adx": float(r["adx"] or 0) or None, "hold_min": float(r["hold_min"] or 0),
        })

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    bars = load_15m(db)
    db.close()

    trades, ok, repro = enrich(trades, bars)
    print("=" * 82)
    print("  VB long-only + follow-through — mesma amostra forense, nova simulação")
    print(f"  CSV: {csv_path} | {len(trades)} trades | indexação OK em {ok} | "
          f"sinal reproduzido em {repro}/{ok}")
    print("=" * 82)

    print("\n  --- VALIDAÇÃO DA INDEXAÇÃO (breakout bar -> banda) ---")
    print(f"  trades com bar de breakout reconstruído: {ok}/{len(trades)}")
    print(f"  sinais reproduzidos (close do bar > banda): {repro}/{ok} "
          f"({100.0 * repro / ok:.0f}%)")
    miss = [t for t in trades if t["_ft"] is None]
    if miss:
        print(f"  WARNING: {len(miss)} trades sem contexto de 15m (fora do span do DB)")

    def slice_stats(name: str, sl: List[Dict[str, Any]], pnl_key: str = "pnl_usd") -> Dict[str, Any]:
        s = stats([dict(t, pnl_usd=t.get(pnl_key, t["pnl_usd"])) for t in sl])
        print(f"  {name:34} {fmt(s)}")
        return s

    print("\n  --- COMPARAÇÃO DE FATIAS ---")
    baseline_s = slice_stats("BASELINE (todos)", trades)
    longs_trades = [t for t in trades if t["side"] == "long"]
    longs_s = slice_stats("long-only", longs_trades)
    exp_trades = [t for t in trades if t["regime"] == "expansion"]
    exp_s = slice_stats("expansion-only (gate live)", exp_trades)
    ft_all = [t for t in trades if t["_ft"]]
    print(f"\n  follow-through (qualquer lado): {len(ft_all)}/{len(trades)} "
          f"({100.0 * len(ft_all) / len(trades):.0f}% dos trades confirmam)")
    long_ft = [t for t in trades if t["side"] == "long" and t["_ft"]]
    slice_stats("long-only + FT (upper bound)", long_ft)
    slice_stats("long-only + FT delayed entry", long_ft, pnl_key="_pnl_usd_delay")
    short_ft = [t for t in trades if t["side"] == "short" and t["_ft"]]
    slice_stats("short-only + FT (referência)", short_ft)

    if long_ft:
        print("\n  --- long-only + FT por REGIME ---")
        for reg in sorted(set(t["regime"] for t in long_ft)):
            slice_stats(f"  {reg}", [t for t in long_ft if t["regime"] == reg])
        print("\n  --- long-only + FT por MOTIVO DE SAÍDA ---")
        for er in sorted(set(t["exit_reason"] for t in long_ft),
                         key=lambda e: -sum(t["pnl_usd"] for t in long_ft if t["exit_reason"] == e)):
            slice_stats(f"  {er[:30]}", [t for t in long_ft if t["exit_reason"] == er])

        print("\n  --- CUSTO DA CONFIRMAÇÃO (gap de entrada) ---")
        gaps = sorted(t["_entry_gap_pct"] for t in long_ft)
        avg_gap = sum(gaps) / len(gaps)
        print(f"  entry gap (delayed open vs breakout close, na direção do trade): "
              f"min={gaps[0]:+.3f}% avg={avg_gap:+.3f}% max={gaps[-1]:+.3f}%")

    print("\n" + "=" * 82)
    print("  VEREDITO")
    print("=" * 82)
    s_base = stats(trades)
    s_ft = stats(long_ft) if long_ft else stats([])
    s_delay = stats([dict(t, pnl_usd=t.get("_pnl_usd_delay", t["pnl_usd"])) for t in long_ft]) if long_ft else stats([])
    better = s_ft["net"] > s_base["net"] and s_ft["expectancy"] > 0
    delay_positive = s_delay["net"] > 0
    print(f"  baseline:            {fmt(s_base)}")
    print(f"  long-only+FT:        {fmt(s_ft)}")
    print(f"  long-only+FT delay:  {fmt(s_delay)}")
    if better and delay_positive:
        print("\n  A fatia sobrevivente é POSITIVA mesmo com o custo da confirmação ->")
        print("  candidata a shadow-live (thresholds pinned, sem re-fit).")
    elif better:
        print("\n  A fatia é melhor que o baseline mas o delayed entry perde o edge ->")
        print("  a confirmação custa mais do que filtra; manter VB como está.")
    else:
        print("\n  A fatia NÃO sobrevive — a combinação não resgata o VB.")

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"vb_long_ft_variant_{stamp}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(),
            "csv": str(csv_path),
            "index_ok": ok, "signal_reproduced": repro,
            "baseline": s_base, "long_only": longs_s,
            "expansion_gate": exp_s,
            "long_ft_upper": s_ft, "long_ft_delayed": s_delay,
            "short_ft": stats(short_ft),
        }, f, indent=1)
    print(f"\n  JSON: {out_json}")


if __name__ == "__main__":
    main()
