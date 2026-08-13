"""Liquidation flush shadow — continuation vs fade (research only).

The existing LiquidationCatcher is a FADE (longs liquidated -> go long, buy
the dip). The project's Phase-2 liquidation-map study found 12/15 flushes
CONTINUED short-term — the continuation hypothesis was never tested. This
harness tests BOTH directions on the same flush events and lets the data
decide:

  * continuation: trade WITH the liquidation flow (longs liquidated -> SHORT)
  * fade:         trade AGAINST the flow (longs liquidated -> LONG)

Mechanics (no lookahead):
  * Flush event: a 1-minute bucket where the dominant-side liquidation
    notional (real venues okx+bybit only by default) crosses a per-symbol
    threshold.
  * Entry: next 1m bar OPEN after the flush minute closes.
  * Exit:  close of the bar at entry+H minutes. Fees: taker 0.045% x2.
  * Sweeps: threshold percentile (p95/p97.5/p99), hold (5/10/15/30 min).

Reads bot.db read-only. Writes nothing. Output: console + JSON in
data/backtests/. Judged later by the baseline-signal gate (n>=30, B1>=p95,
PF>1) — never wired to the frozen Fase 10 config.
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
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
SOURCES_REAL = ("okx", "bybit")
FEES_RT_PCT = 0.045 * 2  # tier-0 taker, both sides
HOLD_SWEEP = (5, 10, 15, 30)
PCT_SWEEP = (0.95, 0.975, 0.99)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = int(k) + 1 if f + 1 < len(s) else f
    return s[f] + (s[c] - s[f]) * (k - f)


def load_data(include_proxy: bool) -> Tuple[Dict[str, List[Tuple[int, float, str]]],
                                            Dict[str, Dict[int, Tuple[float, float]]]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    sources = list(SOURCES_REAL)
    if include_proxy:
        sources.append("proxy")
    placeholders = ",".join("?" * len(sources))

    # Liquidation events: (symbol, ts, notional, side)
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

    # 1m candles: symbol -> {ts_ms: (open, close)}
    candles: Dict[str, Dict[int, Tuple[float, float]]] = {s: {} for s in SYMBOLS}
    for sym in SYMBOLS:
        cur.execute(
            "SELECT timestamp_ms, open, close FROM candles_1m "
            "WHERE symbol = ? ORDER BY timestamp_ms ASC",
            (sym,),
        )
        for ts, o, c in cur.fetchall():
            candles[sym][int(ts)] = (float(o), float(c))
    conn.close()
    return events, candles


def flush_events(events: List[Tuple[int, float, str]]) -> List[Dict[str, Any]]:
    """Per-minute dominant-side liquidation buckets (only minutes with events)."""
    buckets: Dict[int, Dict[str, float]] = {}
    for ts, notional, side in events:
        m = ts // 60_000
        b = buckets.setdefault(m, {"long": 0.0, "short": 0.0})
        b[side] = b.get(side, 0.0) + notional
    out: List[Dict[str, Any]] = []
    for m, b in sorted(buckets.items()):
        dominant = "long" if b["long"] >= b["short"] else "short"
        out.append({
            "minute_ms": m * 60_000,
            "dominant_side": dominant,
            "notional": b[dominant],
        })
    return out


def simulate(flushes: List[Dict[str, Any]], candles: Dict[int, Tuple[float, float]],
             hold_min: int, direction: str) -> List[Dict[str, Any]]:
    """direction: 'continuation' (with flow) or 'fade' (against flow)."""
    trades: List[Dict[str, Any]] = []
    ts_list = sorted(candles)
    idx = {t: i for i, t in enumerate(ts_list)}
    for f in flushes:
        # Entry at the first 1m bar whose OPEN is strictly after the flush minute.
        # The flush minute is [minute_ms, minute_ms+60s); its own bar's open is
        # the flush START (lookahead) — so we require ts >= minute_ms + 60_000.
        entry_i = None
        for t in ts_list:
            if t >= f["minute_ms"] + 60_000:
                entry_i = idx[t]
                break
        if entry_i is None:
            continue
        entry_ts = ts_list[entry_i]
        exit_i = entry_i + hold_min
        if exit_i >= len(ts_list):
            continue
        entry_price = candles[entry_ts][0]
        exit_price = candles[ts_list[exit_i]][1]

        # Price impact of a long-liquidation flush is DOWN.
        # continuation: long flush -> SHORT; fade: long flush -> LONG.
        long_flush = f["dominant_side"] == "long"
        if direction == "continuation":
            side = "short" if long_flush else "long"
        else:
            side = "long" if long_flush else "short"
        if side == "long":
            ret = (exit_price / entry_price) - 1.0
        else:
            ret = (entry_price / exit_price) - 1.0
        gross_pct = ret * 100.0
        net_pct = gross_pct - FEES_RT_PCT
        trades.append({
            "entry_ts": entry_ts,
            "side": side,
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "net_pct": round(net_pct, 4),
        })
    return trades


def summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    gross = sum(t["net_pct"] + FEES_RT_PCT for t in trades)
    net = sum(t["net_pct"] for t in trades)
    return {
        "n": n, "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        "net_bps_sum": round(net * 100, 1),
        "avg_net_bps": round(net / n * 100, 2) if n else 0.0,
        "gross_bps_sum": round(gross * 100, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--include-proxy", action="store_true",
                    help="also aggregate the proxy liquidation estimate")
    ap.add_argument("--days", type=int, default=60, help="recent window in days")
    args = ap.parse_args()

    print("=" * 80)
    print("  LIQUIDATION FLUSH SHADOW — continuation vs fade (research only)")
    print(f"  sources: {','.join(SOURCES_REAL)}{' +proxy' if args.include_proxy else ''} "
          f"| window: last {args.days}d | fees: {FEES_RT_PCT:.3f}% RT")
    print("=" * 80)

    t0 = time.time()
    events, candles = load_data(args.include_proxy)
    print(f"loaded in {time.time()-t0:.0f}s")

    results: List[Dict[str, Any]] = []
    for sym in SYMBOLS:
        sym_events = events[sym]
        if not sym_events:
            print(f"\n{sym}: no liquidation events")
            continue
        flushes = flush_events(sym_events)
        dom_values = [f["notional"] for f in flushes]
        thresholds = {f"p{int(p*100)}": percentile(dom_values, p) for p in PCT_SWEEP}
        print(f"\n=== {sym}: {len(flushes)} flush minutes "
              f"(dominant notional avg ${sum(dom_values)/len(dom_values)/1e6:.2f}M, "
              f"max ${max(dom_values)/1e6:.1f}M)")
        print(f"    thresholds: " + ", ".join(f"{k}=${v/1e3:.0f}K" for k, v in thresholds.items()))
        for pkey, thr in thresholds.items():
            sel = [f for f in flushes if f["notional"] >= thr]
            for hold in HOLD_SWEEP:
                for direction in ("continuation", "fade"):
                    trades = simulate(sel, candles[sym], hold, direction)
                    s = summarize(trades)
                    s.update({"symbol": sym, "threshold": pkey,
                              "hold_min": hold, "direction": direction})
                    results.append(s)
                    print(f"  {pkey} hold={hold:>2}m {direction:13s} "
                          f"n={s['n']:>4} WR={s['win_rate']:>5}% "
                          f"net={s['net_bps_sum']:>8.0f}bps avg={s['avg_net_bps']:>6.1f}bps")

    print("\n" + "=" * 80)
    print("  MELHORES CÉLULAS (net bps por trade, n>=20)")
    print("=" * 80)
    valid = [r for r in results if r["n"] >= 20]
    valid.sort(key=lambda r: r["avg_net_bps"], reverse=True)
    hdr = f"{'sym':5}{'thr':5}{'hold':>5}{'dir':13}{'n':>5}{'WR%':>6}{'netSumBps':>10}{'avgBps':>8}"
    print(hdr)
    for r in valid[:20]:
        print(f"{r['symbol']:5}{r['threshold']:5}{r['hold_min']:>5}{r['direction']:13}"
              f"{r['n']:>5}{r['win_rate']:>6}{r['net_bps_sum']:>9.0f}{r['avg_net_bps']:>8.1f}")

    worst = sorted(valid, key=lambda r: r["avg_net_bps"])[:10]
    print("\n  PIORES CÉLULAS")
    for r in worst:
        print(f"{r['symbol']:5}{r['threshold']:5}{r['hold_min']:>5}{r['direction']:13}"
              f"{r['n']:>5}{r['win_rate']:>6}{r['net_bps_sum']:>9.0f}{r['avg_net_bps']:>8.1f}")

    out_dir = ROOT / "data" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = out_dir / f"liquidation_flush_shadow_{stamp}.json"
    p.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "sources": list(SOURCES_REAL) + (["proxy"] if args.include_proxy else []),
        "fees_rt_pct": FEES_RT_PCT,
        "results": results,
    }, indent=2), encoding="utf-8")
    print(f"\nJSON: {p}")


if __name__ == "__main__":
    main()
