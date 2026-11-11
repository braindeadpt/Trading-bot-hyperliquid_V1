"""Readout — §Leitura of the frozen prereg. Pure read-only.

"Worth studying" requires ALL of:
  * mean net gain > 0 with session-clustered 95% CI above 0
  * > 50% of sessions positive
  * not explained by 1-2 events (top-2 share of total gain < 50%)
Otherwise: latency closed (C). Read needs >=10 sessions AND >=100
events, collected within at most 4 weeks.
"""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.latency_probe import spec


def open_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                          timeout=3.0)
    con.row_factory = sqlite3.Row
    return con


def _ci95(session_means: List[float]) -> Optional[tuple]:
    """Clustered CI: sessions are the clusters; mean +/- t*SE."""
    n = len(session_means)
    if n < 2:
        return None
    mu = sum(session_means) / n
    var = sum((m - mu) ** 2 for m in session_means) / (n - 1)
    se = math.sqrt(var / n)
    # t(0.975, n-1) approximations for small n
    t = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
         7: 2.447, 8: 2.365, 9: 2.306}.get(n, 1.96)
    return mu - t * se, mu + t * se


def evaluate(db_path: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    if not Path(db_path).exists():
        return {"verdict": "NO_LEDGER", "db": db_path}
    now_ms = now_ms or int(time.time() * 1000)
    con = open_ro(db_path)
    try:
        sessions = [dict(r) for r in con.execute(
            "SELECT * FROM probe_sessions WHERE n_events > 0"
            " ORDER BY start_ms")]
        ev_rows = [dict(r) for r in con.execute(
            "SELECT session_id, gain_bps, delay_ms, status"
            " FROM probe_events")]
        hb = con.execute(
            "SELECT v FROM probe_meta WHERE k='hb_ts_ms'").fetchone()

        closed = [e for e in ev_rows if e["gain_bps"] is not None]
        by_sess: Dict[str, List[float]] = {}
        for e in closed:
            by_sess.setdefault(e["session_id"], []).append(
                float(e["gain_bps"]))
        sess_means = [sum(v) / len(v) for v in by_sess.values() if v]
        n_pos = sum(1 for m in sess_means if m > 0)

        delays = [float(e["delay_ms"]) for e in ev_rows
                  if e["delay_ms"] is not None]
        gains = sorted(float(e["gain_bps"]) for e in closed
                       if float(e["gain_bps"]) > 0)
        top2_share = (sum(gains[-2:]) / sum(gains)) if gains else 1.0

        span_w = ((sessions[-1]["start_ms"] - sessions[0]["start_ms"])
                  / (7 * 86400_000)) if len(sessions) > 1 else 0.0
        ci = _ci95(sess_means)
        mean_net = (sum(sess_means) / len(sess_means)) if sess_means else 0.0

        res: Dict[str, Any] = {
            "sessions": len(sessions),
            "events_total": len(ev_rows),
            "events_scored": len(closed),
            "span_weeks": round(span_w, 2),
            "mean_net_bps": mean_net,
            "ci95_bps": ci,
            "positive_sessions": n_pos,
            "top2_gain_share": top2_share,
            "median_delay_ms": (sorted(delays)[len(delays) // 2]
                                if delays else None),
            "heartbeat_age_s": (
                (now_ms - int(float(hb["v"]))) / 1000.0
                if hb else None),
        }

        enough = (len(sessions) >= spec.MIN_SESSIONS
                  and len(closed) >= spec.MIN_EVENTS)
        if not enough:
            res["verdict"] = ("CLOSED_MAX_SPAN"
                              if span_w > spec.MAX_WEEKS
                              else "IN_PROGRESS")
        else:
            checks = {
                "mean_net>0": mean_net > 0,
                "ci95>0": ci is not None and ci[0] > 0,
                ">50%_sessions_pos": n_pos / len(sess_means) > 0.5
                if sess_means else False,
                "not_dominant": top2_share < spec.DOMINANT_SHARE,
            }
            res["checks"] = checks
            res["verdict"] = ("WORTH_STUDYING" if all(checks.values())
                              else "C")
        return res
    finally:
        con.close()
