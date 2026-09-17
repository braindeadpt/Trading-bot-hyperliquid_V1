#!/usr/bin/env python3
"""Jev experiment evaluator — do the recorded judgments beat noise?

Joins `jev_decisions` (jev_shadow_judge.py) to forward 1m-candle returns at
1h/4h/8h horizons and computes:

  * rank correlation of the directional score (long_noul - short_noul)
    vs forward return — the IC, the same currency as every feature screen
  * directional accuracy of `action` when confidence >= threshold
  * hypothetical PF of a "follow Jev" policy under tier-0 costs
    (enter on action=long/short with conf>=thr, exit at +4h, 0.045%/side)

This is a MEASUREMENT, not a promotion path: a positive IC across
non-overlapping weeks is the precondition for even considering a real
family preregistration.

Usage:
    python -X utf8 scripts/research/jev_eval.py
    python -X utf8 scripts/research/jev_eval.py --conf 0.6 --horizon-h 4
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import load_config  # noqa: E402

LIVE_DB = ROOT / "data" / "live" / "bot.db"
FEE_RT = 0.0009  # tier-0 taker 0.045% x 2 sides


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 4:
        return None

    def ranks(v: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = sum((r - mx) ** 2 for r in rx)
    dy = sum((r - my) ** 2 for r in ry)
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy) ** 0.5


def _fwd_ret(ts: List[int], cl: List[float], t0: int, p0: float,
             h_ms: int) -> Optional[float]:
    i = bisect.bisect_right(ts, t0 + h_ms) - 1
    if i < 0 or ts[i] <= t0 or ts[i] < t0 + h_ms - 120_000:
        return None
    return cl[i] / p0 - 1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", type=float, default=0.6)
    ap.add_argument("--horizon-h", type=float, default=4.0)
    ap.add_argument("--min-n", type=int, default=30)
    args = ap.parse_args()

    cfg = load_config(ROOT / "config" / "settings.yaml")
    rdb_path = Path(ResearchDatabase.resolve_path(cfg))
    rdb = sqlite3.connect(f"file:{rdb_path}?mode=ro", uri=True)
    db = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)

    decisions = rdb.execute(
        "select ts_ms, symbol, action_choice, action_conf, long_noul, "
        "short_noul, regime_choice from jev_decisions where error is null "
        "order by ts_ms",
    ).fetchall()
    print(f"jev_eval: {len(decisions)} decisions")
    if not decisions:
        return 1

    # candle caches per symbol
    cache: Dict[str, Tuple[List[int], List[float]]] = {}
    for sym in {d[1] for d in decisions}:
        rows = db.execute(
            "select timestamp_ms, close from candles_1m where symbol=? "
            "order by timestamp_ms", (sym,),
        ).fetchall()
        cache[sym] = ([r[0] for r in rows], [r[1] for r in rows])

    horizons = [int(args.horizon_h * 3_600_000), 3_600_000, 8 * 3_600_000]
    # per-horizon IC of (long_noul - short_noul)
    for h in horizons:
        xs, ys = [], []
        for ts_ms, sym, _a, _c, ln, sn, _r in decisions:
            if ln is None or sn is None:
                continue
            ts_, cl = cache[sym]
            p0_i = bisect.bisect_right(ts_, ts_ms) - 1
            if p0_i < 0:
                continue
            fr = _fwd_ret(ts_, cl, ts_ms, cl[p0_i], h)
            if fr is None:
                continue
            xs.append(ln - sn)
            ys.append(fr)
        rho = _spearman(xs, ys)
        print(f"  h={h // 3_600_000}h: n={len(xs)} IC(score vs fwd_ret)="
              f"{rho if rho is None else round(rho, 3)}")

    # hypothetical follow-Jev policy at chosen horizon
    h = int(args.horizon_h * 3_600_000)
    trades: List[float] = []
    per_week: Dict[str, float] = {}
    for ts_ms, sym, act, conf, _ln, _sn, _reg in decisions:
        if act not in ("long", "short") or conf is None or conf < args.conf:
            continue
        ts_, cl = cache[sym]
        i = bisect.bisect_right(ts_, ts_ms) - 1
        if i < 0:
            continue
        fr = _fwd_ret(ts_, cl, ts_ms, cl[i], h)
        if fr is None:
            continue
        net = (fr if act == "long" else -fr) - FEE_RT
        trades.append(net)
        wk = f"{ts_ms // (7 * 86_400_000)}"
        per_week[wk] = per_week.get(wk, 0.0) + net
    n = len(trades)
    wins = sum(1 for t in trades if t > 0)
    gross_p = sum(t for t in trades if t > 0)
    gross_l = -sum(t for t in trades if t < 0)
    pf = gross_p / gross_l if gross_l > 0 else float("inf")
    print(f"\nfollow-Jev (conf>={args.conf}, h={args.horizon_h}h): "
          f"n={n} win%={wins / n * 100:.0f} PF={pf:.2f} net_bps={sum(trades) * 1e4:.1f}")
    for wk, v in sorted(per_week.items()):
        print(f"   week {wk}: net={v * 1e4:+.1f} bps")
    print(f"\nverdict hint: needs n>={args.min_n} AND PF>1 across most weeks "
          f"to justify a real preregistration — this is measurement only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
