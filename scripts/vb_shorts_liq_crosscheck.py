"""VB shorts x liquidation cross-check — test the 'flush reversal' hypothesis.

Context: VB forensics (data/backtests/vb_forensics_20260813_040003.csv)
shows shorts at WR 7.7%, net -$66.32 (vs longs WR 25%, -$15.47). The
hypothesis under test: "shorts lose because the flush reverses" — a
liquidation flush pushes price down, VB shorts ride the breakout, then the
flush reverts and runs over the short.

Structural limitation found first: the VB trade window (06-28..08-07) does
NOT overlap the real liquidation feed (okx/bybit, 08-09+). Only the proxy
source (06-08..06-29) overlaps, and only 1-2/39 shorts sit in it. The
cross-check therefore has two parts:

  PART A — does the flush reverse? (real liq 08-09+, 4 symbols)
      For each 1m flush >= p90 of dominant notional, measure the 30m
      return after the flush, split by dominant side. Liquidation side
      'long' = forced SELLS (price down) -> reversal shows as positive
      post-return; side 'short' = forced BUYS -> negative post-return.

  PART B — do the VB shorts behave like flush rides? (historical window)
      For each VB short, measure the pre-entry drop (worst low in the 30m
      before entry vs entry open — how violent the move INTO the short
      was) and the post-entry 30m return. If shorts are flush rides, the
      violent ones should show systematic reversal (positive post-return
      that grows with pre-drop) and carry the losses.

Read-only: never writes bot.db.
"""

from __future__ import annotations

import bisect
import csv
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "live" / "bot.db"
SYMBOLS = ["BTC", "ETH", "SOL", "HYPE"]
FORENSICS_CSV = ROOT / "data" / "backtests" / "vb_forensics_20260813_040003.csv"


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def load_liq_events(cur: sqlite3.Cursor, sources: Tuple[str, ...]) -> Dict[str, Dict[int, Dict[str, float]]]:
    """symbol -> minute -> {long: notional, short: notional}."""
    ph = ",".join("?" * len(sources))
    cur.execute(
        f"SELECT symbol, timestamp_ms, notional_usd, side FROM liquidation_events "
        f"WHERE source IN ({ph}) ORDER BY timestamp_ms ASC",
        sources,
    )
    buckets: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(dict)
    for sym, ts, notional, side in cur.fetchall():
        if sym not in SYMBOLS:
            continue
        m = ts // 60_000
        b = buckets[sym].setdefault(m, {"long": 0.0, "short": 0.0})
        b[side] += float(notional)
    return buckets


def load_candles(cur: sqlite3.Cursor) -> Dict[str, Dict[int, Tuple[float, float, float, float]]]:
    candles: Dict[str, Dict[int, Tuple[float, float, float, float]]] = {}
    for sym in SYMBOLS:
        cur.execute(
            "SELECT timestamp_ms, open, high, low, close FROM candles_1m "
            "WHERE symbol = ? ORDER BY timestamp_ms ASC",
            (sym,),
        )
        candles[sym] = {int(ts): (float(o), float(h), float(l), float(c)) for ts, o, h, l, c in cur.fetchall()}
    return candles


def part_a_flush_reversal(cur: sqlite3.Cursor, candles: Dict[str, Dict[int, Tuple[float, float, float, float]]]) -> None:
    print("\n" + "=" * 78)
    print("  PART A — does a real liquidation flush reverse? (real liq, 08-09+)")
    print("=" * 78)
    buckets = load_liq_events(cur, ("okx", "bybit"))
    for sym in SYMBOLS:
        dom = [max(b["long"], b["short"]) for b in buckets[sym].values()]
        if not dom:
            continue
        p90 = percentile(dom, 0.90)
        ts_list = sorted(candles[sym])
        rows: List[Tuple[str, float]] = []
        for m, b in buckets[sym].items():
            d = max(b["long"], b["short"])
            if d < p90:
                continue
            dom_side = "long" if b["long"] >= b["short"] else "short"
            i = bisect.bisect_left(ts_list, m * 60_000 + 60_000)
            if i + 30 >= len(ts_list):
                continue
            entry = candles[sym][ts_list[i]][0]
            px30 = candles[sym][ts_list[i + 30]][3]
            rows.append((dom_side, (px30 / entry - 1.0) * 100.0))
        if not rows:
            continue
        ll = [r for r in rows if r[0] == "long"]
        sl = [r for r in rows if r[0] == "short"]

        def avg(xs: List[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        print(f"  {sym}: {len(rows)} flushes>=p90 | long-liq (forced SELLS) "
              f"{len(ll)} -> post30 {avg([r[1] for r in ll]):+.2f}% "
              f"| short-liq (forced BUYS) {len(sl)} -> post30 {avg([r[1] for r in sl]):+.2f}%")


def part_b_vb_shorts(candles: Dict[str, Dict[int, Tuple[float, float, float, float]]]) -> None:
    print("\n" + "=" * 78)
    print("  PART B — do the VB shorts behave like flush rides? (06-28..08-07)")
    print("=" * 78)
    if not FORENSICS_CSV.exists():
        print("  forensics CSV missing — skipping (run vb_regime_forensics.py first)")
        return
    trades = list(csv.DictReader(open(FORENSICS_CSV, encoding="utf-8")))
    shorts = [t for t in trades if t["side"] == "short"]

    rows: List[Dict[str, Any]] = []
    for t in shorts:
        sym = t["symbol"]
        entry_ts = int(t["entry_time"])
        ts_list = sorted(candles[sym])
        i = bisect.bisect_left(ts_list, entry_ts)
        if i < 30 or i + 30 >= len(ts_list):
            continue
        entry_open = candles[sym][ts_list[i]][0]
        lows = [candles[sym][ts_list[j]][2] for j in range(i - 30, i)]
        pre_low = min(lows)
        pre_drop = (pre_low / entry_open - 1.0) * 100.0
        post = candles[sym][ts_list[i + 30]][3]
        post_ret = (post / entry_open - 1.0) * 100.0
        rows.append({"pre_drop": pre_drop, "post_ret": post_ret,
                     "pnl": float(t["pnl_usd"]), "exit_reason": t["exit_reason"]})

    n = len(rows)
    drops = sorted(r["pre_drop"] for r in rows)
    print(f"  VB shorts with candles: {n}/{len(shorts)}")
    print(f"  pre-entry drop dist: min={drops[0]:+.2f}% max={drops[-1]:+.2f}% "
          f"median={statistics.median(drops):+.2f}%")
    up = sum(1 for r in rows if r["post_ret"] > 0)
    avg_post = sum(r["post_ret"] for r in rows) / n
    avg_pre = sum(r["pre_drop"] for r in rows) / n
    print(f"  pre-drop avg {avg_pre:+.2f}% | post30 avg {avg_post:+.3f}% "
          f"({100 * up / n:.0f}% rose after entry) | avg trade {sum(r['pnl'] for r in rows) / n:+.2f} USD")

    def agg(label: str, r: List[Dict[str, Any]]) -> None:
        if not r:
            return
        a5 = sum(x["post_ret"] for x in r) / len(r)
        w = sum(1 for x in r if x["post_ret"] > 0)
        p = sum(x["pnl"] for x in r)
        print(f"  {label:26} n={len(r):>3} pre={sum(x['pre_drop'] for x in r) / len(r):+.2f}% "
              f"post30={a5:+.2f}% rose={100 * w / len(r):.0f}% PnL={p:+.2f}")

    print("\n  by pre-entry drop violence (proxy for flush intensity):")
    agg("drop >= -0.3%", [r for r in rows if r["pre_drop"] >= -0.3])
    agg("-0.6..-0.3%", [r for r in rows if -0.6 <= r["pre_drop"] < -0.3])
    agg("drop < -0.6%", [r for r in rows if r["pre_drop"] < -0.6])

    flushy = [r for r in rows if r["pre_drop"] < -0.6]
    calm = [r for r in rows if r["pre_drop"] >= -0.6]
    if flushy and calm:
        print("\n  FLUSH-like (< -0.6% drop):")
        agg("  ", flushy)
        print("  CALM (>= -0.6% drop):")
        agg("  ", calm)
        va = statistics.pstdev([x["post_ret"] for x in flushy])
        vb = statistics.pstdev([x["post_ret"] for x in calm])
        se = (va ** 2 / len(flushy) + vb ** 2 / len(calm)) ** 0.5
        diff = (sum(x["post_ret"] for x in flushy) / len(flushy)
                - sum(x["post_ret"] for x in calm) / len(calm))
        t = diff / se if se else 0.0
        print(f"  t-stat(post30 flushy vs calm) = {t:+.2f}")

    print("\n  by exit reason:")
    for er in sorted(set(r["exit_reason"] for r in rows)):
        agg(er[:26], [r for r in rows if r["exit_reason"] == er])

    # correlation pre-move vs post-return (negative = mean reversion)
    xs = [r["pre_drop"] for r in rows]
    ys = [r["post_ret"] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    corr = cov / (sx * sy) if sx and sy else 0.0
    print(f"\n  corr(pre_drop, post_ret) = {corr:+.3f}  (negative would mean mean-reversion)")


def part_b_failed_breakout(candles: Dict[str, Dict[int, Tuple[float, float, float, float]]]) -> None:
    """PART C — the 20 failed_breakout trades (WR 5%, -$55.47): do they behave
    like flush rides? Same proxy as PART B but filtered to the failed_breakout
    exit family, split by side."""
    print("\n" + "=" * 78)
    print("  PART C — failed_breakout trades (20) x reversal proxy")
    print("=" * 78)
    if not FORENSICS_CSV.exists():
        print("  forensics CSV missing — skipping")
        return
    trades = list(csv.DictReader(open(FORENSICS_CSV, encoding="utf-8")))
    fb = [t for t in trades if "failed_breakout" in t["exit_reason"]]

    rows: List[Dict[str, Any]] = []
    for t in fb:
        sym = t["symbol"]
        entry_ts = int(t["entry_time"])
        ts_list = sorted(candles[sym])
        i = bisect.bisect_left(ts_list, entry_ts)
        if i < 30 or i + 30 >= len(ts_list):
            continue
        entry_open = candles[sym][ts_list[i]][0]
        lows = [candles[sym][ts_list[j]][2] for j in range(i - 30, i)]
        pre_low = min(lows)
        pre_drop = (pre_low / entry_open - 1.0) * 100.0
        post = candles[sym][ts_list[i + 30]][3]
        post_ret = (post / entry_open - 1.0) * 100.0
        rows.append({"side": t["side"], "exit": t["exit_reason"],
                     "pre_drop": pre_drop, "post_ret": post_ret,
                     "pnl": float(t["pnl_usd"]), "regime": t["regime"]})

    if not rows:
        print("  no failed_breakout trades with candle context")
        return

    def agg(label: str, r: List[Dict[str, Any]]) -> None:
        if not r:
            return
        n = len(r)
        post = sum(x["post_ret"] for x in r) / n
        up = sum(1 for x in r if x["post_ret"] > 0)
        pnl = sum(x["pnl"] for x in r)
        pd = sum(x["pre_drop"] for x in r) / n
        print(f"  {label:22} n={n:>2} pre_drop={pd:+.2f}% post30={post:+.3f}% "
              f"rose={100 * up / n:.0f}% PnL={pnl:+.2f}")

    print(f"  all {len(rows)} failed_breakout trades:")
    agg("shorts", [r for r in rows if r["side"] == "short"])
    agg("longs", [r for r in rows if r["side"] == "long"])
    agg("short+above_mid", [r for r in rows if r["side"] == "short" and r["exit"].endswith("above_mid")])
    agg("long+below_mid", [r for r in rows if r["side"] == "long" and r["exit"].endswith("below_mid")])

    in_exp = sum(1 for r in rows if r["regime"] == "expansion")
    print(f"\n  blocked by expansion-only rework (non-expansion): {len(rows) - in_exp}/{len(rows)}")
    print("  (implication: the rework already removes most failed_breakout trades)")


def main() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()
    candles = load_candles(cur)
    part_a_flush_reversal(cur, candles)
    part_b_vb_shorts(candles)
    part_b_failed_breakout(candles)
    conn.close()


if __name__ == "__main__":
    main()
