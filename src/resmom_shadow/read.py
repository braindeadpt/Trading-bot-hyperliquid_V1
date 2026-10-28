"""Read gate evaluation (prereg §Leitura) — read-only, reusable by tests.

Weekly returns are computed off resmom_nav marks: a week W is CLOSED
when a nav mark exists at/after W+MS_WEEK (its exit got realized). The
single read happens at READ_REBALANCES closed rebalances; before that
the verdict is IN PROGRESS. KILL is checked weekly by the daemon (meta
flag) and recomputed here from the nav series.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.resmom_shadow import spec


def open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                          timeout=3.0)
    con.row_factory = sqlite3.Row
    return con


def _weeks(nav_rows: List[Dict[str, Any]]) -> List[Tuple[int, float, float]]:
    """(week_key_ms, nav_at_week_start, nav_at_week_end) — CLOSED weeks
    only: a week counts once a mark exists in the following week."""
    by_week: Dict[int, List[Tuple[int, float]]] = {}
    for r in nav_rows:
        days = int(r["ts_ms"]) // spec.MS_D
        wk = (days - ((days + 3) % 7)) * spec.MS_D
        by_week.setdefault(wk, []).append(
            (int(r["ts_ms"]), float(r["nav_usd"])))
    out: List[Tuple[int, float, float]] = []
    for wk in sorted(by_week):
        nxt = by_week.get(wk + spec.MS_WEEK)
        if not nxt:
            continue                       # week still open — no exit yet
        nav0 = sorted(by_week[wk])[0][1]
        nav1 = sorted(nxt)[0][1]
        out.append((wk, nav0, nav1))
    return out


def weekly_returns(nav_rows: List[Dict[str, Any]]) -> List[float]:
    """Closed weekly returns from nav marks (first mark per week basis)."""
    return [n1 / n0 - 1.0 for _wk, n0, n1 in _weeks(nav_rows) if n0 > 0]


def sharpe_annual(rets: List[float], periods_per_year: float = 52.0
                  ) -> Optional[float]:
    if len(rets) < 2:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return None
    return mu / sd * math.sqrt(periods_per_year)


def null_sharpes(con: sqlite3.Connection) -> List[float]:
    """Per-null annualized Sharpe over its scored weekly returns."""
    by_null: Dict[int, List[float]] = {}
    for r in con.execute(
            "SELECT null_id, week_pnl_usd, nav_usd FROM resmom_nulls"
            " WHERE week_pnl_usd IS NOT NULL ORDER BY null_id, week_key_ms"):
        # weekly return needs prior nav: nav_usd stored is POST-week; the
        # return is pnl / (nav - pnl)
        nav = float(r["nav_usd"])
        pnl = float(r["week_pnl_usd"])
        base = nav - pnl
        if base > 0:
            by_null.setdefault(int(r["null_id"]), []).append(pnl / base)
    out = []
    for nid, rets in by_null.items():
        s = sharpe_annual(rets)
        if s is not None:
            out.append(s)
    return out


def percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def symbol_pnl(con: sqlite3.Connection, arm: str = "A") -> Dict[str, float]:
    """Per-symbol closed contributions: price pnl + funding."""
    out: Dict[str, float] = {}
    for r in con.execute(
            "SELECT symbol, COALESCE(SUM(pnl_usd),0) pnl,"
            " COALESCE(SUM(funding_usd),0) f FROM resmom_positions"
            " WHERE arm=? AND status='closed' GROUP BY symbol", (arm,)):
        out[r["symbol"]] = float(r["pnl"]) + float(r["f"])
    return out


def evaluate(db_path: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Apply §Leitura to the ledger. Pure read-only."""
    if not Path(db_path).exists():
        return {"verdict": "NO_LEDGER", "db": db_path}
    now_ms = now_ms or int(time.time() * 1000)
    con = open_ro(db_path)
    try:
        meta = dict(con.execute("SELECT k, v FROM resmom_meta"))
        nav_rows = [dict(r) for r in con.execute(
            "SELECT ts_ms, nav_usd FROM resmom_nav WHERE arm='A'"
            " ORDER BY ts_ms")]
        n_reb = con.execute(
            "SELECT COUNT(*) c FROM resmom_rebalances WHERE arm='A'"
        ).fetchone()["c"]
        kill_meta = meta.get("kill_triggered")
        rets = weekly_returns(nav_rows)
        n_closed = len(rets)

        cum = 1.0
        for r in rets:
            cum *= 1.0 + r
        cum_ret = cum - 1.0
        sh = sharpe_annual(rets)
        ns = null_sharpes(con)
        p75 = percentile(ns, 75)
        spnl = symbol_pnl(con)
        top3 = sorted(spnl.values(), reverse=True)[:3]
        ex_top3 = sum(spnl.values()) - sum(top3)

        # net drawdown from nav marks (kill check, descriptive)
        peak = 0.0
        dd_min = 0.0
        for r in nav_rows:
            nav = float(r["nav_usd"])
            peak = max(peak, nav)
            if peak > 0:
                dd_min = min(dd_min, nav / peak - 1.0)

        kill = {
            "dd_min": dd_min,
            "breached": dd_min <= spec.KILL_DD,
            "flag_meta": json.loads(kill_meta) if kill_meta else None,
        }

        res: Dict[str, Any] = {
            "rebalances_A": n_reb,
            "closed_weeks": n_closed,
            "read_due_at": spec.READ_REBALANCES,
            "weekly_returns": rets,
            "cum_return": cum_ret,
            "sharpe_ann": sh,
            "null_sharpe_p75": p75,
            "n_nulls_scored": len(ns),
            "pnl_ex_top3": ex_top3,
            "kill": kill,
            "heartbeat_age_s": (
                now_ms - int(float(meta.get("hb_ts_ms") or 0))) / 1000.0
            if meta.get("hb_ts_ms") else None,
        }

        if kill["breached"]:
            res["verdict"] = "KILL"
        elif n_closed < spec.READ_REBALANCES:
            res["verdict"] = "IN_PROGRESS"
        else:
            checks = {
                "cum_return>0": cum_ret > 0,
                "sharpe>0.5": (sh or -99) > spec.PASS_SHARPE,
                "sharpe>p75_nulls": (sh is not None and p75 is not None
                                     and sh > p75),
                "pnl_ex_top3>0": ex_top3 > 0,
            }
            res["checks"] = checks
            res["verdict"] = "PASS" if all(checks.values()) else "C"
        return res
    finally:
        con.close()
