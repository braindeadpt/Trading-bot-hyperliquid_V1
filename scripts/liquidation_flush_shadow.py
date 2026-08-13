"""Liquidation flush shadow — fade focus + stop-loss + aggregated sample.

CORRECTION (2026-08-13): the original harness reported "60d" but the real
venues (okx+bybit) only hold 5 days of events (2026-08-09 -> 08-13); the
June window is the *proxy* estimate (2026-06-08 -> 06-29). Sample is
therefore reported PER SOURCE SET so the gate is not judged on a
mislabeled window:

  * real      — okx + bybit only (5d, trustworthy)
  * proxy     — Coinalyze-style estimate (22d, lower trust — the execution
                path rejects it via REQUIRE_REAL_LIQUIDATION)
  * combined  — everything (largest sample, for the n>=30 gate estimate)

Mechanics (no lookahead):
  * Flush: 1-minute bucket where dominant-side notional crosses a
    per-symbol threshold (p90/p95/p97.5/p99 of dominant-minute notional).
  * Entry: next 1m bar OPEN after the flush minute closes.
  * Exit:  min(hold exit at bar close, intrabar stop-loss). Fees 0.045%x2.
  * Directions: fade (against the flush — the only positive family from the
    first pass) and continuation (for comparison).

Gate approximation: a cell "approaches the baseline gate" when n>=30 and
PF>1 (B1 percentile is a live-shadows-only measure; here PF/n are the
observable half).
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
FEES_RT_PCT = 0.045 * 2
HOLD_SWEEP = (10, 15, 30, 60)
PCT_SWEEP = (0.90, 0.95, 0.975, 0.99)
SL_SWEEP = (None, 0.005, 0.01, 0.015, 0.02)  # None = hold-only


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def load_data(sources: List[str]) -> Tuple[Dict[str, List[Tuple[int, float, str]]],
                                           Dict[str, Dict[int, Tuple[float, float, float, float]]]]:
    """sources -> events; symbol -> {ts_ms: (open, high, low, close)}."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    placeholders = ",".join("?" * len(sources))
    events: Dict[str, List[Tuple[int, float, str]]] = {s: [] for s in SYMBOLS}
    cur.execute(
        f"""SELECT symbol, timestamp_ms, notional_usd, side
            FROM liquidation_events
            WHERE source IN ({placeholders})
            ORDER BY timestamp_ms ASC""",
        sources,
    )
    for sym, ts, notional, side in cur.fetchall():
        if sym in events:
            events[sym].append((int(ts), float(notional), side))
    candles: Dict[str, Dict[int, Tuple[float, float, float, float]]] = {s: {} for s in SYMBOLS}
    for sym in SYMBOLS:
        cur.execute(
            "SELECT timestamp_ms, open, high, low, close FROM candles_1m "
            "WHERE symbol = ? ORDER BY timestamp_ms ASC",
            (sym,),
        )
        for ts, o, h, l, c in cur.fetchall():
            candles[sym][int(ts)] = (float(o), float(h), float(l), float(c))
    conn.close()
    return events, candles


def flush_events(events: List[Tuple[int, float, str]]) -> List[Dict[str, Any]]:
    buckets: Dict[int, Dict[str, float]] = {}
    for ts, notional, side in events:
        m = ts // 60_000
        b = buckets.setdefault(m, {"long": 0.0, "short": 0.0})
        b[side] = b.get(side, 0.0) + notional
    out: List[Dict[str, Any]] = []
    for m, b in sorted(buckets.items()):
        dominant = "long" if b["long"] >= b["short"] else "short"
        out.append({"minute_ms": m * 60_000, "dominant_side": dominant,
                    "notional": b[dominant]})
    return out


def simulate(flushes: List[Dict[str, Any]],
             candles: Dict[int, Tuple[float, float, float, float]],
             hold_min: int, direction: str,
             stop_loss_pct: Optional[float]) -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    ts_list = sorted(candles)
    for f in flushes:
        entry_i = bisect.bisect_left(ts_list, f["minute_ms"] + 60_000)
        if entry_i >= len(ts_list):
            continue
        exit_i = entry_i + hold_min
        if exit_i >= len(ts_list):
            continue
        entry_ts = ts_list[entry_i]
        o, _, _, _ = candles[entry_ts]
        long_flush = f["dominant_side"] == "long"
        if direction == "continuation":
            side = "short" if long_flush else "long"
        else:
            side = "long" if long_flush else "short"

        exit_price, exit_reason = candles[ts_list[exit_i]][3], "hold"
        if stop_loss_pct:
            if side == "long":
                stop_price = o * (1.0 - stop_loss_pct)
                for j in range(entry_i, exit_i + 1):
                    low = candles[ts_list[j]][2]
                    if low <= stop_price:
                        exit_price, exit_reason = stop_price, "stop_loss"
                        break
            else:
                stop_price = o * (1.0 + stop_loss_pct)
                for j in range(entry_i, exit_i + 1):
                    high = candles[ts_list[j]][1]
                    if high >= stop_price:
                        exit_price, exit_reason = stop_price, "stop_loss"
                        break

        if side == "long":
            ret = (exit_price / o) - 1.0
        else:
            ret = (o / exit_price) - 1.0
        net_pct = ret * 100.0 - FEES_RT_PCT
        trades.append({"entry_ts": entry_ts, "side": side, "exit_reason": exit_reason,
                       "net_pct": round(net_pct, 4)})
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


def run_set(events: Dict[str, List[Tuple[int, float, str]]],
            candles: Dict[str, Dict[int, Tuple[float, float, float, float]]],
            label: str, results: List[Dict[str, Any]]) -> None:
    print(f"\n=== FONTE: {label} ===")
    for sym in SYMBOLS:
        flushes = flush_events(events[sym])
        if not flushes:
            print(f"  {sym}: sem eventos")
            continue
        dom = [f["notional"] for f in flushes]
        thr = {f"p{int(p*100)}": percentile(dom, p) for p in PCT_SWEEP}
        print(f"  {sym}: {len(flushes)} flush minutos | "
              f"avg ${sum(dom)/len(dom)/1e6:.2f}M max ${max(dom)/1e6:.1f}M | "
              + ", ".join(f"{k}=${v/1e3:.0f}K" for k, v in thr.items()))
        for pkey, t in thr.items():
            sel = [f for f in flushes if f["notional"] >= t]
            for hold in HOLD_SWEEP:
                for direction in ("fade", "continuation"):
                    for sl in SL_SWEEP:
                        trades = simulate(sel, candles[sym], hold, direction, sl)
                        s = summarize(trades)
                        s.update({"source": label, "symbol": sym, "threshold": pkey,
                                  "hold_min": hold, "direction": direction,
                                  "sl_pct": sl})
                        results.append(s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=0,
                    help="unused — kept for CLI compat (sample = full event history)")
    args = ap.parse_args()

    print("=" * 82)
    print("  LIQUIDATION FLUSH SHADOW v2 — fade + stop-loss + aggregated sample")
    print("  real(okx+bybit, 5d) | proxy(22d) | combined | fees 0.090% RT")
    print("=" * 82)

    t0 = time.time()
    candles = load_data(["okx", "bybit"])[1]  # candles are source-independent
    real_ev, _ = load_data(["okx", "bybit"])
    proxy_ev, _ = load_data(["proxy"])
    print(f"loaded in {time.time()-t0:.0f}s")

    results: List[Dict[str, Any]] = []
    run_set(real_ev, candles, "real", results)
    run_set(proxy_ev, candles, "proxy", results)
    combined = {s: real_ev[s] + proxy_ev[s] for s in SYMBOLS}
    run_set(combined, candles, "combined", results)

    print("\n" + "=" * 82)
    print("  GATE — fade, n>=30 e PF>1 (aproximação observável do baseline)")
    print("=" * 82)
    gate = [r for r in results if r["direction"] == "fade" and r["n"] >= 30 and r["profit_factor"] > 1.0]
    gate.sort(key=lambda r: r["avg_net_bps"], reverse=True)
    print(f"\n  células fade que passam n>=30 & PF>1: {len(gate)}")
    for r in gate[:12]:
        sl = f"sl={r['sl_pct']*100:.0f}%" if r["sl_pct"] else "hold"
        print(f"  {r['source']:9} {r['symbol']:5} {r['threshold']:5} hold={r['hold_min']:>3}m "
              f"{sl:6} n={r['n']:>3} WR={r['win_rate']:>5.1f}% PF={r['profit_factor']:>5.2f} "
              f"net={r['net_bps']:>7.0f}bps avg={r['avg_net_bps']:>6.1f}bps")

    print("\n  TOP 15 fade (qualquer n, por avg bps):")
    fades = [r for r in results if r["direction"] == "fade"]
    fades.sort(key=lambda r: r["avg_net_bps"], reverse=True)
    for r in fades[:15]:
        sl = f"sl={r['sl_pct']*100:.0f}%" if r["sl_pct"] else "hold"
        flag = " [GATE]" if r["n"] >= 30 and r["profit_factor"] > 1.0 else ""
        print(f"  {r['source']:9} {r['symbol']:5} {r['threshold']:5} hold={r['hold_min']:>3}m "
              f"{sl:6} n={r['n']:>3} WR={r['win_rate']:>5.1f}% PF={r['profit_factor']:>5.2f} "
              f"net={r['net_bps']:>7.0f}bps avg={r['avg_net_bps']:>6.1f}bps{flag}")

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = out_dir / f"liquidation_flush_shadow_v2_{stamp}.json"
    p.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "fees_rt_pct": FEES_RT_PCT,
        "note": "real=okx+bybit (5d 08-09..08-13); proxy=22d 06-08..06-29",
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nJSON: {p}")


if __name__ == "__main__":
    main()
