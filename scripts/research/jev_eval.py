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
import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402
from src.utils.config import load_config  # noqa: E402

LIVE_DB = ROOT / "data" / "live" / "bot.db"
FEE_RT = 0.0009  # tier-0 taker 0.045% x 2 sides

# ── executed-trade evaluation (current geometry only) ────────────────────
# Geometry boundary: commit 96f1b15 (2026-10-04 14:39:40 UTC) — SL floor
# 1%→0.5%, TP 2R→1R, maker entry; the OOS window restarted there, so only
# post-boundary trades measure the current geometry.
GEOMETRY_BOUNDARY_MS = 1791124780000
# Preregistered kill criterion (docs/PREREGISTER_JEV_OOS_KILL_2026-10-08.md):
# at indep_n >= 100 post-boundary, net PF <= 1.0 → JevJudge off, no retry.
JEV_KILL_TARGET_N = 100
JEV_SL_FLOOR = 0.005
JEV_SL_ATR_MULT = 2.0
JEV_MAX_HOLD_MS = 4 * 3_600_000
# Post-boundary economics: maker entry 0.015% + taker exit 0.045% (modelled —
# paper fills instantly at limit; adverse selection not simulated).
JEV_FEE_RT = 0.00015 + 0.00045


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
    ap.add_argument("--baseline-runs", type=int, default=200,
                    help="random-direction control runs (fixed seed)")
    ap.add_argument("--seed", type=int, default=42,
                    help="fixed seed for the random baseline")
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

    # ── executed paper trades under the CURRENT geometry ────────────────
    print("\n=== executed JevJudge trades — post-2026-10-04 geometry ===")
    print("(boundary = commit 96f1b15: SL floor 0.5%, TP 1R, maker entry; "
          "earlier trades are a different geometry and do not count)")
    rows = db.execute(
        "select symbol, side, entry_price, entry_time, exit_time, pnl_pct, "
        "signal_metadata from trades where strategy='JevJudge' "
        "and status='closed' and entry_time > ? order by entry_time",
        (GEOMETRY_BOUNDARY_MS,),
    ).fetchall()
    indep = _independent_trades(
        [
            {
                "symbol": r[0], "side": r[1], "entry_price": r[2],
                "entry_time": r[3], "exit_time": r[4], "pnl_pct": r[5],
                "signal_metadata": r[6],
            }
            for r in rows
        ]
    )
    print(f"closed trades post-boundary: raw={len(rows)} indep={len(indep)} "
          f"(kill read at indep_n>={JEV_KILL_TARGET_N})")
    pnls = [float(t["pnl_pct"]) for t in indep]
    if pnls:
        w = sum(1 for p in pnls if p > 0)
        gp = sum(p for p in pnls if p > 0)
        gl = -sum(p for p in pnls if p < 0)
        net_pf = gp / gl if gl > 0 else float("inf")
        print(f"  recorded: n={len(pnls)} win%={100 * w / len(pnls):.0f} "
              f"net PF={net_pf:.2f} net_pnl={sum(pnls) * 1e4:.1f} bps")

        # random-direction control: same entries/brackets, coin-flip sides
        candles_hlc: Dict[str, List[Tuple[int, float, float, float]]] = {}
        for sym in {str(t["symbol"]) for t in indep}:
            crows = db.execute(
                "select timestamp_ms, high, low, close from candles_1m "
                "where symbol=? order by timestamp_ms", (sym,),
            ).fetchall()
            candles_hlc[sym] = [
                (int(r[0]), float(r[1]), float(r[2]), float(r[3]))
                for r in crows
            ]
        bracketed = []
        for t in indep:
            sl, tp = _bracket_from_metadata(t["signal_metadata"])
            bracketed.append({**t, "sl_pct": sl, "tp_pct": tp})
        modelled_real = 0.0
        modelled_n = 0
        for t in bracketed:
            r = simulate_bracket(
                candles_hlc[str(t["symbol"])],
                int(t["entry_time"]), float(t["entry_price"]),
                str(t["side"]), float(t["sl_pct"]), float(t["tp_pct"]),
            )
            if r is not None:
                modelled_real += r
                modelled_n += 1
        dist = sorted(
            random_baseline(
                bracketed, candles_hlc,
                n_runs=args.baseline_runs, seed=args.seed,
            )
        )
        pct = _percentile(dist, modelled_real)
        print(f"  random baseline ({args.baseline_runs} runs, seed={args.seed}, "
              f"coin-flip side, same brackets): "
              f"median={dist[len(dist) // 2] * 1e4:.1f} bps")
        print(f"  Jev modelled net={modelled_real * 1e4:.1f} bps over "
              f"{modelled_n} trades -> percentile {pct:.0f} "
              f"(50 = indistinguishable from noise)")
        if len(pnls) >= JEV_KILL_TARGET_N:
            verdict = "KILL — preregistered criterion met" if net_pf <= 1.0 else "hold"
            print(f"  kill criterion armed: net PF={net_pf:.2f} -> {verdict}")
        else:
            print(f"  kill criterion: pending ({len(pnls)}/{JEV_KILL_TARGET_N})")
    return 0


# ── executed JevJudge trades: current geometry only ──────────────────────

def _independent_trades(
    trades: Sequence[Dict[str, object]],
) -> List[Dict[str, object]]:
    """One open position per symbol — the same independence rule the shadow
    evaluator uses (entry must be >= last counted exit on that symbol)."""
    busy: Dict[str, int] = {}
    kept: List[Dict[str, object]] = []
    for t in sorted(trades, key=lambda r: int(r["entry_time"])):
        sym = str(t["symbol"])
        if int(t["entry_time"]) >= busy.get(sym, -1):
            kept.append(t)
            busy[sym] = int(t["exit_time"] or t["entry_time"])
    return kept


def _bracket_from_metadata(signal_metadata: Optional[str]) -> Tuple[float, float]:
    """Recover (sl_pct, tp_pct) for a post-boundary JevJudge trade.

    The engine stores ``atr_pct = signal.stop_loss_pct / 2`` in the trade's
    signal_metadata (JevJudge never sets atr_pct itself), and the post
    2026-10-04 geometry is TP = 1R. Fallback to the frozen floor.
    """
    atr = 0.0
    if signal_metadata:
        try:
            atr = float(json.loads(signal_metadata).get("atr_pct") or 0.0)
        except (ValueError, TypeError):
            atr = 0.0
    sl = max(JEV_SL_FLOOR, 2.0 * atr) if atr > 0 else JEV_SL_FLOOR
    return sl, sl * 1.0  # tp_r_mult = 1.0


def simulate_bracket(
    candles: Sequence[Tuple[int, float, float, float]],
    entry_ts_ms: int,
    entry_px: float,
    side: str,
    sl_pct: float,
    tp_pct: float,
    max_hold_ms: int = JEV_MAX_HOLD_MS,
    fee_rt: float = JEV_FEE_RT,
) -> Optional[float]:
    """Resolve a bracketed paper trade over 1m candles → net pct fraction.

    ``candles`` = (ts_ms, high, low, close) rows for the symbol. Ambiguous
    candle (both barriers touched) resolves to SL first — conservative.
    Returns None when no forward candle covers the window.
    """
    fwd = [
        (ts, h, l, c)
        for ts, h, l, c in candles
        if entry_ts_ms < ts <= entry_ts_ms + max_hold_ms
    ]
    if not fwd:
        return None
    if side == "long":
        sl_px, tp_px = entry_px * (1 - sl_pct), entry_px * (1 + tp_pct)
    else:
        sl_px, tp_px = entry_px * (1 + sl_pct), entry_px * (1 - tp_pct)
    move: Optional[float] = None
    for _ts, h, l, c in fwd:
        hit_sl = (l <= sl_px) if side == "long" else (h >= sl_px)
        hit_tp = (h >= tp_px) if side == "long" else (l <= tp_px)
        if hit_sl:  # SL first on the ambiguous both-hit candle
            move = -sl_pct
            break
        if hit_tp:
            move = tp_pct
            break
    if move is None:
        move = fwd[-1][3] / entry_px - 1.0
        if side == "short":
            move = -move
    return move - fee_rt


def random_baseline(
    trades: Sequence[Dict[str, object]],
    candles_by_symbol: Dict[str, List[Tuple[int, float, float, float]]],
    *,
    n_runs: int = 200,
    seed: int = 42,
) -> List[float]:
    """Random-direction control with identical geometry/frequency/symbols.

    Each run re-enters every real trade at the same time/symbol/bracket with
    a coin-flip side. Returns the list of per-run total net pct.
    """
    rng = random.Random(seed)
    totals: List[float] = []
    for _ in range(n_runs):
        total = 0.0
        for t in trades:
            candles = candles_by_symbol.get(str(t["symbol"]), [])
            side = rng.choice(("long", "short"))
            r = simulate_bracket(
                candles,
                int(t["entry_time"]),
                float(t["entry_price"]),
                side,
                float(t["sl_pct"]),
                float(t["tp_pct"]),
            )
            if r is not None:
                total += r
        totals.append(total)
    return totals


def _percentile(sorted_vals: Sequence[float], v: float) -> float:
    if not sorted_vals:
        return float("nan")
    below = sum(1 for x in sorted_vals if x < v)
    return 100.0 * below / len(sorted_vals)


if __name__ == "__main__":
    sys.exit(main())
