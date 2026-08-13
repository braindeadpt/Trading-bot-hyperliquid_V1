"""Liquidation flush fade — VARIANT sweep (ETH p90/30m).

Tests three families of modifications to the gate cell (real source,
ETH, p90 of dominant-minute notional, fade, hold 30m, no SL):

  A. ENTRY DELAY  — enter at the OPEN of the 2nd bar after the flush
     minute closes (skip the first reaction bar) instead of the 1st.
  B. INTENSITY    — threshold as a MULTIPLE of p90 (1.5x / 2x / 3x / 5x)
     instead of p90 itself; only the strongest flushes qualify.
  C. TRAILING     — exit by ratchet trailing stop from the peak (long:
     peak_high x (1 - trail_pct); short: peak_low x (1 + trail_pct))
     instead of the fixed 30m hold, bounded by a max_hold safety cap.

Mechanics are byte-identical to scripts/liquidation_flush_shadow.py:
flush = 1m bucket where dominant-side notional >= threshold; entry at the
OPEN of the first candle with ts >= flush_minute_ms + 60s; intrabar exit
with pessimistic fills (exit at the trail/SL level, not the bar close);
fees 0.045% x 2; candles stamped at minute END (ts % 60_000 == 59_999).

Sample: REAL source only (okx+bybit) — the only trustworthy set per the
REQUIRE_REAL_LIQUIDATION rule. The proxy is documented as negative on the
same cell, so variants are judged on real alone.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
SYMBOL = "ETH"
FEES_RT_PCT = 0.045 * 2
HOLD_BASE = 30
PCT_SWEEP = (0.90,)
ENTRY_DELAY_SWEEP = (0, 1)
INTENSITY_SWEEP = (1.0, 1.5, 2.0, 3.0, 5.0)
TRAIL_PCT_SWEEP = (0.003, 0.005, 0.01)
TRAIL_MAX_HOLD_SWEEP = (60, 120)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def load_data() -> Tuple[List[Tuple[int, float, str]], Dict[int, Tuple[float, float, float, float]]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT timestamp_ms, notional_usd, side FROM liquidation_events "
        "WHERE source IN ('okx','bybit') AND symbol = ? ORDER BY timestamp_ms ASC",
        (SYMBOL,),
    )
    events = [(int(ts), float(n), side) for ts, n, side in cur.fetchall()]
    cur.execute(
        "SELECT timestamp_ms, open, high, low, close FROM candles_1m "
        "WHERE symbol = ? ORDER BY timestamp_ms ASC",
        (SYMBOL,),
    )
    candles = {int(ts): (float(o), float(h), float(l), float(c)) for ts, o, h, l, c in cur.fetchall()}
    conn.close()
    return events, candles


def flush_events(events: List[Tuple[int, float, str]]) -> List[Dict[str, Any]]:
    buckets: Dict[int, Dict[str, float]] = {}
    for ts, notional, side in events:
        m = ts // 60_000
        b = buckets.setdefault(m, {"long": 0.0, "short": 0.0})
        b[side] += notional
    out: List[Dict[str, Any]] = []
    for m, b in sorted(buckets.items()):
        dominant = "long" if b["long"] >= b["short"] else "short"
        out.append({"minute_ms": m * 60_000, "dominant_side": dominant,
                    "notional": b[dominant]})
    return out


def entry_index(ts_list: List[int], flush_minute_ms: int, delay: int) -> int:
    """Entry candle = (1 + delay)-th candle with ts >= flush_minute_ms + 60s.
    delay=0 -> 1st bar post-flush (baseline), delay=1 -> 2nd bar."""
    import bisect
    base = bisect.bisect_left(ts_list, flush_minute_ms + 60_000)
    return base + delay


def simulate_hold(flushes: List[Dict[str, Any]], candles: Dict[int, Tuple[float, float, float, float]],
                  hold_min: int, direction: str, entry_delay: int) -> List[Dict[str, Any]]:
    ts_list = sorted(candles)
    trades: List[Dict[str, Any]] = []
    for f in flushes:
        entry_i = entry_index(ts_list, f["minute_ms"], entry_delay)
        exit_i = entry_i + hold_min
        if exit_i >= len(ts_list):
            continue
        entry_ts = ts_list[entry_i]
        o = candles[entry_ts][0]
        long_flush = f["dominant_side"] == "long"
        side = "long" if long_flush else "short"  # fade
        exit_price = candles[ts_list[exit_i]][3]
        if side == "long":
            ret = exit_price / o - 1.0
        else:
            ret = o / exit_price - 1.0
        trades.append({"entry_ts": entry_ts, "side": side, "net_pct": round(ret * 100 - FEES_RT_PCT, 4)})
    return trades


def simulate_trail(flushes: List[Dict[str, Any]], candles: Dict[int, Tuple[float, float, float, float]],
                   trail_pct: float, max_hold: int) -> List[Dict[str, Any]]:
    ts_list = sorted(candles)
    trades: List[Dict[str, Any]] = []
    for f in flushes:
        entry_i = entry_index(ts_list, f["minute_ms"], 0)
        if entry_i >= len(ts_list):
            continue
        entry_ts = ts_list[entry_i]
        o = candles[entry_ts][0]
        long_flush = f["dominant_side"] == "long"
        side = "long" if long_flush else "short"
        exit_price: Optional[float] = None
        exit_reason = "max_hold"
        if side == "long":
            peak = o
            for j in range(entry_i, min(entry_i + max_hold, len(ts_list))):
                _, h, low, _ = candles[ts_list[j]]
                peak = max(peak, h)
                stop = peak * (1.0 - trail_pct)
                if low <= stop:
                    exit_price, exit_reason = stop, "trail"
                    break
            if exit_price is None:
                exit_price = candles[ts_list[min(entry_i + max_hold, len(ts_list) - 1)]][3]
        else:
            peak = o
            for j in range(entry_i, min(entry_i + max_hold, len(ts_list))):
                _, high, l, _ = candles[ts_list[j]]
                peak = min(peak, l)
                stop = peak * (1.0 + trail_pct)
                if high >= stop:
                    exit_price, exit_reason = stop, "trail"
                    break
            if exit_price is None:
                exit_price = candles[ts_list[min(entry_i + max_hold, len(ts_list) - 1)]][3]
        if exit_price is None:
            continue
        if side == "long":
            ret = exit_price / o - 1.0
        else:
            ret = o / exit_price - 1.0
        trades.append({"entry_ts": entry_ts, "side": side, "exit_reason": exit_reason,
                       "net_pct": round(ret * 100 - FEES_RT_PCT, 4)})
    return trades


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    losses = [t["net_pct"] for t in trades if t["net_pct"] <= 0]
    gross_wins = sum(t["net_pct"] + FEES_RT_PCT for t in trades if t["net_pct"] > 0)
    net = sum(t["net_pct"] for t in trades)
    return {
        "n": n, "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        "profit_factor": round(gross_wins / abs(sum(losses)), 3) if losses and sum(losses) else 0.0,
        "net_bps": round(net * 100, 1), "avg_net_bps": round(net / n * 100, 2) if n else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=12, help="rows per family to print")
    args = ap.parse_args()

    print("=" * 82)
    print("  LIQUIDATION FLUSH FADE — VARIANT SWEEP (real source, ETH)")
    print("  baseline: p90 / 1st bar / hold 30m / fade / no SL")
    print("=" * 82)

    t0 = time.time()
    events, candles = load_data()
    flushes = flush_events(events)
    dom = [f["notional"] for f in flushes]
    p90 = percentile(dom, 0.90)
    print(f"loaded in {time.time()-t0:.0f}s | {len(flushes)} flush mins | p90=${p90/1e3:.0f}K")

    results: List[Dict[str, Any]] = []

    # Threshold filter: only flushes >= p90 qualify (same as the v2 sim).
    sel_p90 = [f for f in flushes if f["notional"] >= p90]
    print(f"  flushes >= p90: {len(sel_p90)}")

    # Baseline (delay 0 = 1st bar, hold 30)
    base_trades = simulate_hold(sel_p90, candles, HOLD_BASE, "fade", 0)
    base = summarize(base_trades)
    base.update({"family": "baseline", "param": "1st bar/hold30"})
    results.append(base)
    print(f"\nBASELINE: n={base['n']} WR={base['win_rate']}% PF={base['profit_factor']} "
          f"avg={base['avg_net_bps']:+.2f}bps net={base['net_bps']:+.0f}bps")

    # A. Entry delay
    print(f"\n=== A. ENTRY DELAY (hold {HOLD_BASE}m, fade) ===")
    for d in ENTRY_DELAY_SWEEP:
        trades = simulate_hold(sel_p90, candles, HOLD_BASE, "fade", d)
        s = summarize(trades)
        s.update({"family": "entry_delay", "param": f"{'2nd' if d else '1st'} bar"})
        results.append(s)
        print(f"  {'2nd' if d else '1st'} bar : n={s['n']:>3} WR={s['win_rate']:>5.1f}% "
              f"PF={s['profit_factor']:>5.2f} avg={s['avg_net_bps']:>+7.2f}bps net={s['net_bps']:>+6.0f}bps")

    # B. Intensity filter (multiples of p90)
    print("\n=== B. INTENSITY FILTER (entry 1st bar, hold 30m, fade) ===")
    for mult in INTENSITY_SWEEP:
        thr = p90 * mult
        sel = [f for f in flushes if f["notional"] >= thr]
        trades = simulate_hold(sel, candles, HOLD_BASE, "fade", 0)
        s = summarize(trades)
        s.update({"family": "intensity", "param": f"{mult:.1f}x p90 (${thr/1e3:.0f}K)"})
        results.append(s)
        print(f"  {mult:.1f}x p90 : n={s['n']:>3} WR={s['win_rate']:>5.1f}% "
              f"PF={s['profit_factor']:>5.2f} avg={s['avg_net_bps']:>+7.2f}bps net={s['net_bps']:>+6.0f}bps")

    # C. Trailing exit
    print("\n=== C. TRAILING EXIT (entry 1st bar, p90, fade) ===")
    for trail in TRAIL_PCT_SWEEP:
        for mh in TRAIL_MAX_HOLD_SWEEP:
            trades = simulate_trail(sel_p90, candles, trail, mh)
            s = summarize(trades)
            s.update({"family": "trailing", "param": f"trail={trail*100:.1f}% maxh={mh}m"})
            results.append(s)
            print(f"  trail={trail*100:.1f}% maxh={mh:>3}m : n={s['n']:>3} WR={s['win_rate']:>5.1f}% "
                  f"PF={s['profit_factor']:>5.2f} avg={s['avg_net_bps']:>+7.2f}bps net={s['net_bps']:>+6.0f}bps")

    # Gate: n>=30 & PF>1
    print("\n" + "=" * 82)
    print("  GATE — n>=30 & PF>1 (aproximação observável do baseline)")
    print("=" * 82)
    gate = [r for r in results if r["n"] >= 30 and r["profit_factor"] > 1.0]
    gate.sort(key=lambda r: r["avg_net_bps"], reverse=True)
    print(f"  variantes que passam: {len(gate)} (baseline incluído)")
    for r in gate[:args.top]:
        tag = " [BASELINE]" if r["family"] == "baseline" else ""
        print(f"  {r['family']:11} {r['param']:>22} : n={r['n']:>3} WR={r['win_rate']:>5.1f}% "
              f"PF={r['profit_factor']:>5.2f} avg={r['avg_net_bps']:>+7.2f}bps{tag}")

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = out_dir / f"liquidation_flush_variants_{stamp}.json"
    p.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL, "source": "real (okx+bybit)", "p90": p90,
        "fees_rt_pct": FEES_RT_PCT,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nJSON: {p}")


if __name__ == "__main__":
    main()
