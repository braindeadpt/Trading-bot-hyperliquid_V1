"""paper_trades — unified read-only layer over every strategy ledger.

Post-cutover dashboard reorganization (2026-11-03): the single place the
dashboard reads strategy cards, per-strategy detail and the merged trades
feed. Composes on top of the same ledgers ``paper_ledger`` reads — this
module never writes and never changes how strategies persist.

Row shapes (stable):

    card    per living strategy — state, open n, net, apy, wr,
            n/n_readout + readout date, equity sparkline
    detail  open positions, closed trades, equity + drawdown series
    feed    one merged line per closed trade across every ledger

Everything opened read-only (sqlite ``mode=ro``); a missing or locked
ledger degrades that fragment to ``None``/``[]`` — never a 500.
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
_RESEARCH = ROOT / "data" / "research"
_LIVE = ROOT / "data" / "live"

_MS_DAY = 86_400_000
_MS_YEAR = 365.25 * _MS_DAY
_SPARK_MAX = 120          # sparkline point cap per series
# Per-ledger staleness thresholds — each writer has its own cadence
# (hourly verdicts, minutely marks, daily nav), so one global window
# would either false-alarm slow writers or miss dead fast ones.
_STALE_JEV = 90 * 60_000      # judge verdicts hourly
_STALE_CARRY = 30 * 60_000    # events stream ~minutely
_STALE_UNLOCK = 3 * 3_600_000 # snapshots ~hourly
_STALE_BOROS = 30 * 60_000    # marks ~minutely
_STALE_RESMOM = 30 * 3_600_000  # nav ~daily
_STALE_IVGATE = 12 * 3_600_000  # board re-eval on demand

# Living strategies shown on the main page, in display order. `id` is the
# stable key used by /api/paper_strategy/<id> and the frontend card click.
STRATEGY_IDS: Tuple[str, ...] = (
    "jev", "carry", "unlock", "boros", "resmom", "ivgate",
)


def _ro(path: Path, timeout: float = 6.0) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                              timeout=timeout)
        con.row_factory = sqlite3.Row
        return con
    except Exception:  # noqa: BLE001
        return None


def _downsample(series: List[Tuple[int, float]],
                cap: int = _SPARK_MAX) -> List[Tuple[int, float]]:
    if len(series) <= cap:
        return series
    step = len(series) / cap
    out = [series[int(i * step)] for i in range(cap)]
    if out[-1] != series[-1]:
        out.append(series[-1])
    return out


def _wr(rows: List[Optional[float]]) -> Optional[float]:
    vals = [r for r in rows if r is not None]
    if not vals:
        return None
    return round(100.0 * sum(1 for v in vals if v > 0) / len(vals), 1)


def _age_state(last_write_ms: Optional[int], has_open: bool,
               now_ms: int, stale_ms: int = 3_600_000) -> Tuple[str, str]:
    """running | no_signal | error — from ledger write freshness.

    ``last_write_ms`` is the freshest ledger timestamp (mark, episode
    event, rebalance, probe event). Fresh + nothing open -> the daemon
    ran but produced no position yet (``no_signal``); stale -> ``error``.
    """
    if last_write_ms is None:
        return "error", "sem escritas"
    age = now_ms - last_write_ms
    if age > stale_ms:
        return "error", f"stale {int(age / 60_000)}m"
    if has_open:
        return "running", "em posição"
    return "no_signal", "sem sinal"


def _base_card(sid: str, name: str, kind: str, n_readout=None,
               readout_ms=None, prereg_state="") -> Dict[str, Any]:
    return {
        "id": sid, "name": name, "kind": kind,
        "state": "error", "state_detail": "sem dados",
        "n_open": 0, "open_unreal_usd": None,
        "net": None, "net_unit": "usd",
        "apy": None, "wr": None,
        "n": None, "n_readout": n_readout,
        "readout_ms": readout_ms,
        "prereg_state": prereg_state,
        "equity": [],
    }


# ── JevJudge — paper execution (bot.db) ───────────────────────────────

def _jev_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "jev", "JevJudge v2", "execution", n_readout=100,
        readout_ms=int(1_798_387_200_000),   # 2026-12-31
        prereg_state="EXPERIMENT")
    con = _ro(_LIVE / "bot.db")
    if con is None:
        return card
    try:
        r = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(pnl_usd),0) pnl,"
            " COALESCE(SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END),0) w,"
            " MAX(COALESCE(exit_time,entry_time)) last_ts"
            " FROM trades WHERE strategy='JevJudge' AND status='closed'"
        ).fetchone()
        card["n"] = int(r["c"])
        card["net"] = round(float(r["pnl"]), 2)
        card["wr"] = (round(100.0 * int(r["w"]) / int(r["c"]), 1)
                      if r["c"] else None)
        card["n_open"] = int(con.execute(
            "SELECT COUNT(*) c FROM trades WHERE strategy='JevJudge'"
            " AND status='open'").fetchone()["c"])
        # unrealized PnL of open rows vs latest candle close
        opens = con.execute(
            "SELECT symbol, side, size, entry_price FROM trades"
            " WHERE strategy='JevJudge' AND status='open'").fetchall()
        unreal = 0.0
        for o in opens:
            px = con.execute(
                "SELECT close FROM candles_1m WHERE symbol=?"
                " ORDER BY timestamp_ms DESC LIMIT 1", (o["symbol"],)
            ).fetchone()
            if px and px["close"]:
                sign = 1.0 if str(o["side"]).lower() in ("long", "buy") else -1.0
                unreal += sign * (px["close"] - o["entry_price"]) * o["size"]
        card["open_unreal_usd"] = round(unreal, 2)
        eq = con.execute(
            "SELECT exit_time ts, pnl_usd FROM trades"
            " WHERE strategy='JevJudge' AND status='closed'"
            " AND exit_time IS NOT NULL ORDER BY exit_time"
        ).fetchall()
        cum = 0.0
        series: List[Tuple[int, float]] = []
        for e in eq:
            cum += float(e["pnl_usd"] or 0.0)
            series.append((int(e["ts"]), round(cum, 4)))
        card["equity"] = _downsample(series)
        # Heartbeat = last judge decision (hourly cadence), not last trade —
        # a flat market means few closes but the loop is still alive.
        hb = None
        try:
            hb = con.execute(
                "SELECT MAX(timestamp) ts FROM decision_audit"
                " WHERE strategy LIKE '%Jev%'").fetchone()["ts"]
        except sqlite3.OperationalError:
            pass
        card["state"], card["state_detail"] = _age_state(
            hb or r["last_ts"], card["n_open"] > 0, now_ms, _STALE_JEV)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _jev_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    con = _ro(_LIVE / "bot.db")
    if con is None:
        return out
    try:
        opens = con.execute(
            "SELECT symbol, side, size, entry_price, entry_time"
            " FROM trades WHERE strategy='JevJudge' AND status='open'"
            " ORDER BY entry_time DESC").fetchall()
        for o in opens:
            px = con.execute(
                "SELECT close FROM candles_1m WHERE symbol=?"
                " ORDER BY timestamp_ms DESC LIMIT 1", (o["symbol"],)
            ).fetchone()
            mark = px["close"] if px else None
            sign = 1.0 if str(o["side"]).lower() in ("long", "buy") else -1.0
            unreal = (sign * (mark - o["entry_price"]) * o["size"]
                      if mark else None)
            out["open"].append({
                "symbol": o["symbol"], "side": o["side"],
                "size": o["size"], "entry": o["entry_price"],
                "mark": mark, "unreal_usd": unreal,
                "entry_ms": o["entry_time"]})
        rows = con.execute(
            "SELECT symbol, side, size, entry_price, exit_price, pnl_usd,"
            " pnl_pct, entry_time, exit_time, exit_reason FROM trades"
            " WHERE strategy='JevJudge' AND status='closed'"
            " ORDER BY exit_time DESC LIMIT 200").fetchall()
        cum = 0.0
        series: List[Tuple[int, float]] = []
        for t in reversed(rows):
            out["closed"].append({
                "symbol": t["symbol"], "side": t["side"], "size": t["size"],
                "entry": t["entry_price"], "exit": t["exit_price"],
                "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"],
                "funding_usd": None, "fees_usd": None,
                "entry_ms": t["entry_time"], "exit_ms": t["exit_time"],
                "reason": t["exit_reason"]})
            if t["exit_time"]:
                cum += float(t["pnl_usd"] or 0.0)
                series.append((int(t["exit_time"]), round(cum, 4)))
        out["equity"] = series
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── CarryA1 spot–perp (carry_shadow.db) ───────────────────────────────

def _carry_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "carry", "CarryA1 spot–perp", "shadow", n_readout=10,
        readout_ms=int(1_807_142_400_000),    # 2027-04-09
        prereg_state="in_progress")
    con = _ro(_RESEARCH / "carry_shadow.db")
    if con is None:
        return card
    try:
        st = con.execute(
            "SELECT state, COUNT(*) c FROM episodes GROUP BY state"
        ).fetchall()
        states = {r["state"]: int(r["c"]) for r in st}
        card["n"] = states.get("closed", 0)
        card["n_open"] = states.get("open", 0)
        agg = con.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) pnl,"
            " COALESCE(SUM(cum_f),0) fund, MAX(COALESCE(closed_ms,"
            " opened_ms, entry_decision_ms)) last_ts FROM episodes"
        ).fetchone()
        card["net"] = round(float(agg["pnl"]) + float(agg["fund"]), 4)
        wr_rows = con.execute(
            "SELECT realized_pnl + cum_f p FROM episodes"
            " WHERE state='closed'").fetchall()
        card["wr"] = _wr([r["p"] for r in wr_rows])
        cum = 0.0
        series: List[Tuple[int, float]] = []
        for e in con.execute(
                "SELECT closed_ms ts, realized_pnl + cum_f p FROM episodes"
                " WHERE state='closed' AND closed_ms IS NOT NULL"
                " ORDER BY closed_ms").fetchall():
            cum += float(e["p"] or 0.0)
            series.append((int(e["ts"]), round(cum, 6)))
        card["equity"] = _downsample(series)
        # Heartbeat = writer's events stream (fills/rebalances/marks) —
        # episodes stay open for days so last-episode is not a liveness sign.
        hb = None
        try:
            hb_row = con.execute("SELECT MAX(ts_ms) ts FROM events").fetchone()
            hb = hb_row["ts"] if hb_row else None
        except sqlite3.OperationalError:
            pass
        dead = con.execute(
            "SELECT value FROM meta WHERE key='dead'").fetchone()
        if dead and dead["value"]:
            card["state"], card["state_detail"] = "error", str(dead["value"])[:60]
        else:
            card["state"], card["state_detail"] = _age_state(
                hb or agg["last_ts"], card["n_open"] > 0, now_ms, _STALE_CARRY)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _carry_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    con = _ro(_RESEARCH / "carry_shadow.db")
    if con is None:
        return out
    try:
        for e in con.execute(
                "SELECT pair, perp, q, margin, cum_f, realized_pnl, p0, s0,"
                " opened_ms FROM episodes WHERE state='open'"
                " ORDER BY opened_ms DESC").fetchall():
            out["open"].append({
                "symbol": f"{e['pair']}/{e['perp']}", "side": "spot+perp",
                "size": e["q"], "entry": e["s0"], "mark": None,
                "unreal_usd": e["realized_pnl"] + e["cum_f"],
                "entry_ms": e["opened_ms"]})
        cum = 0.0
        for e in con.execute(
                "SELECT pair, perp, realized_pnl, cum_f, cost_bps, margin,"
                " opened_ms, closed_ms, close_reason FROM episodes"
                " WHERE state IN ('closed','liquidated','aborted')"
                " ORDER BY closed_ms DESC LIMIT 200").fetchall():
            pnl = float(e["realized_pnl"] or 0.0) + float(e["cum_f"] or 0.0)
            out["closed"].append({
                "symbol": f"{e['pair']}/{e['perp']}", "side": "spot+perp",
                "size": e["margin"], "entry": None, "exit": None,
                "pnl_usd": round(pnl, 6), "pnl_pct": None,
                "funding_usd": e["cum_f"], "fees_usd": e["cost_bps"],
                "entry_ms": e["opened_ms"], "exit_ms": e["closed_ms"],
                "reason": e["close_reason"]})
            if e["closed_ms"]:
                cum += pnl
                out["equity"].append((int(e["closed_ms"]), round(cum, 6)))
        out["equity"].sort(key=lambda x: x[0])
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── UnlockShort (unlock_shadow.db) ────────────────────────────────────

def _unlock_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "unlock", "UnlockShort", "shadow", n_readout=15,
        readout_ms=int(1_838_419_200_000),    # 2027-10-14
        prereg_state="in_progress")
    con = _ro(_RESEARCH / "unlock_shadow.db")
    if con is None:
        return card
    try:
        st = con.execute(
            "SELECT state, COUNT(*) c FROM unlock_sims WHERE kind='event'"
            " GROUP BY state").fetchall()
        states = {r["state"]: int(r["c"]) for r in st}
        card["n"] = states.get("closed", 0)
        card["n_open"] = states.get("open", 0)
        agg = con.execute(
            "SELECT COALESCE(SUM(net_usd),0) net, COALESCE(SUM(gross_usd),0)"
            " g, MAX(priced_out_ms) last_ts FROM unlock_sims"
            " WHERE kind='event' AND state='closed'").fetchone()
        card["net"] = round(float(agg["net"]), 4)
        wr_rows = con.execute(
            "SELECT net_usd p FROM unlock_sims WHERE kind='event'"
            " AND state='closed'").fetchall()
        card["wr"] = _wr([r["p"] for r in wr_rows])
        cum = 0.0
        series: List[Tuple[int, float]] = []
        for e in con.execute(
                "SELECT priced_out_ms ts, net_usd FROM unlock_sims"
                " WHERE kind='event' AND state='closed'"
                " AND priced_out_ms IS NOT NULL"
                " ORDER BY priced_out_ms").fetchall():
            cum += float(e["net_usd"] or 0.0)
            series.append((int(e["ts"]), round(cum, 4)))
        card["equity"] = _downsample(series)
        # Heartbeat = periodic snapshot writer, not event closes (monthly).
        # Non-fatal: a transient writer lock on snapshots shouldn't blank
        # the whole card — fall back to last sim close.
        hb_ts = None
        try:
            hb_row = con.execute(
                "SELECT MAX(ts_ms) ts FROM unlock_snapshots").fetchone()
            hb_ts = hb_row["ts"] if hb_row else None
        except sqlite3.OperationalError:
            pass
        card["state"], card["state_detail"] = _age_state(
            hb_ts or agg["last_ts"],
            card["n_open"] > 0, now_ms, _STALE_UNLOCK)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _unlock_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    con = _ro(_RESEARCH / "unlock_shadow.db")
    if con is None:
        return out
    try:
        for e in con.execute(
                "SELECT token, hedge_notional_usd, tok_px_in, btc_px_in,"
                " window_start_ms FROM unlock_sims"
                " WHERE kind='event' AND state='open'").fetchall():
            out["open"].append({
                "symbol": e["token"], "side": "short/hedge",
                "size": e["hedge_notional_usd"], "entry": e["tok_px_in"],
                "mark": None, "unreal_usd": None,
                "entry_ms": e["window_start_ms"]})
        cum = 0.0
        for e in con.execute(
                "SELECT token, hedge_notional_usd, tok_px_in, tok_px_out,"
                " gross_usd, fees_usd, funding_tok_usd, funding_btc_usd,"
                " net_usd, net_bps, priced_in_ms, priced_out_ms, state"
                " FROM unlock_sims WHERE kind='event'"
                " AND state IN ('closed','invalidated')"
                " ORDER BY priced_out_ms DESC LIMIT 200").fetchall():
            fund = (float(e["funding_tok_usd"] or 0.0)
                    + float(e["funding_btc_usd"] or 0.0))
            out["closed"].append({
                "symbol": e["token"], "side": "short/hedge",
                "size": e["hedge_notional_usd"], "entry": e["tok_px_in"],
                "exit": e["tok_px_out"], "pnl_usd": e["net_usd"],
                "pnl_pct": None, "funding_usd": round(fund, 4),
                "fees_usd": e["fees_usd"], "entry_ms": e["priced_in_ms"],
                "exit_ms": e["priced_out_ms"], "reason": e["state"]})
            if e["priced_out_ms"] and e["state"] == "closed":
                cum += float(e["net_usd"] or 0.0)
                out["equity"].append((int(e["priced_out_ms"]), round(cum, 4)))
        out["equity"].sort(key=lambda x: x[0])
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── Boros locked spread (boros_shadow.db) ─────────────────────────────

def _boros_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "boros", "Boros locked spread", "shadow", n_readout=5,
        prereg_state="in_progress")
    con = _ro(_RESEARCH / "boros_shadow.db")
    if con is None:
        return card
    try:
        card["n_open"] = int(con.execute(
            "SELECT COUNT(*) c FROM boros_trades WHERE state='open'"
        ).fetchone()["c"])
        agg = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(realized_usd),0) pnl,"
            " AVG(realized_apy_cap) apy, MAX(ts_ms) last_ts"
            " FROM boros_close").fetchone()
        card["n"] = int(agg["c"])
        card["net"] = round(float(agg["pnl"]), 4)
        card["apy"] = (round(float(agg["apy"]) * 100.0, 2)
                       if agg["apy"] is not None else None)
        wr_rows = con.execute(
            "SELECT realized_usd p FROM boros_close").fetchall()
        card["wr"] = _wr([r["p"] for r in wr_rows])
        mk = con.execute(
            "SELECT ts_ms, equity_pooled FROM boros_marks"
            " WHERE equity_pooled IS NOT NULL ORDER BY ts_ms").fetchall()
        series = [(int(m["ts_ms"]), float(m["equity_pooled"])) for m in mk]
        card["equity"] = _downsample(series)
        if series:
            card["open_unreal_usd"] = round(
                series[-1][1] - float(agg["pnl"]), 4)
        card["state"], card["state_detail"] = _age_state(
            series[-1][0] if series else agg["last_ts"],
            card["n_open"] > 0, now_ms, _STALE_BOROS)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _boros_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    con = _ro(_RESEARCH / "boros_shadow.db")
    if con is None:
        return out
    try:
        for t in con.execute(
                "SELECT id, asset, structure, direction, entry_ms,"
                " capital_usd, fixed_bruto, net_apy FROM boros_trades"
                " WHERE state='open'").fetchall():
            last = con.execute(
                "SELECT equity_pooled, funding_usd, ts_ms FROM boros_marks"
                " WHERE trade_id=? ORDER BY ts_ms DESC LIMIT 1",
                (t["id"],)).fetchone()
            out["open"].append({
                "symbol": t["asset"], "side": t["direction"],
                "size": t["capital_usd"], "entry": t["fixed_bruto"],
                "mark": None, "unreal_usd": (last["equity_pooled"]
                                            if last else None),
                "entry_ms": t["entry_ms"]})
        cum = 0.0
        for e in con.execute(
                "SELECT c.ts_ms, c.reason, c.fixed_usd, c.floating_usd,"
                " c.basis_usd, c.costs_usd, c.realized_usd,"
                " c.realized_apy_cap, t.asset, t.capital_usd, t.entry_ms"
                " FROM boros_close c JOIN boros_trades t"
                " ON t.id=c.trade_id ORDER BY c.ts_ms DESC LIMIT 200"
        ).fetchall():
            out["closed"].append({
                "symbol": e["asset"], "side": "spread",
                "size": e["capital_usd"], "entry": e["basis_usd"],
                "exit": None, "pnl_usd": e["realized_usd"],
                "pnl_pct": e["realized_apy_cap"],
                "funding_usd": e["floating_usd"], "fees_usd": e["costs_usd"],
                "entry_ms": e["entry_ms"], "exit_ms": e["ts_ms"],
                "reason": e["reason"]})
            if e["ts_ms"]:
                cum += float(e["realized_usd"] or 0.0)
                out["equity"].append((int(e["ts_ms"]), round(cum, 4)))
        out["equity"].sort(key=lambda x: x[0])
        marks = con.execute(
            "SELECT ts_ms, equity_pooled FROM boros_marks"
            " WHERE equity_pooled IS NOT NULL ORDER BY ts_ms").fetchall()
        peak = -math.inf
        for m in marks:
            v = float(m["equity_pooled"])
            peak = max(peak, v)
            out["dd"].append((int(m["ts_ms"]),
                              round((v - peak) / peak * 100.0
                                    if peak else 0.0, 4)))
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── ResidualMomentum forward (resmom_shadow.db) ───────────────────────

def _resmom_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "resmom", "ResMom forward", "shadow", n_readout=26,
        readout_ms=int(1_840_896_000_000),    # 2027-05-03
        prereg_state="in_progress")
    db = _RESEARCH / "resmom_shadow.db"
    if not db.exists():
        return card
    try:
        from src.resmom_shadow.read import evaluate
        res = evaluate(str(db), now_ms=now_ms)
        card["n"] = res.get("closed_weeks")
        cum = res.get("cum_return")
        if isinstance(cum, (int, float)):
            card["net"] = round(cum * 100.0, 3)
            card["net_unit"] = "pct"
        v = res.get("verdict")
        if v and v != "IN_PROGRESS":
            card["prereg_state"] = str(v)
    except Exception:  # noqa: BLE001
        pass
    con = _ro(db)
    if con is None:
        return card
    try:
        nav = con.execute(
            "SELECT ts_ms, nav_usd FROM resmom_nav ORDER BY ts_ms"
        ).fetchall()
        series = [(int(m["ts_ms"]), float(m["nav_usd"])) for m in nav]
        card["equity"] = _downsample(series)
        card["n_open"] = int(con.execute(
            "SELECT COUNT(*) c FROM resmom_positions"
            " WHERE status='open'").fetchone()["c"])
        wr_rows = con.execute(
            "SELECT pnl_usd p FROM resmom_positions"
            " WHERE status='closed'").fetchall()
        card["wr"] = _wr([r["p"] for r in wr_rows])
        if len(series) >= 2 and series[-1][0] > series[0][0]:
            span = series[-1][0] - series[0][0]
            base = series[0][1] or 1.0
            card["apy"] = round(
                ((series[-1][1] / base) ** (_MS_YEAR / span) - 1.0)
                * 100.0, 2)
        last_ts = con.execute(
            "SELECT MAX(entry_ms) ts FROM resmom_positions").fetchone()["ts"]
        hb = series[-1][0] if series else last_ts
        if hb is None:
            # Newborn shadow (first nav snapshot may lag deploy) — flag as
            # starting, not error.
            card["state"], card["state_detail"] = "no_signal", "a iniciar"
        else:
            card["state"], card["state_detail"] = _age_state(
                hb, card["n_open"] > 0, now_ms, _STALE_RESMOM)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _resmom_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    con = _ro(_RESEARCH / "resmom_shadow.db")
    if con is None:
        return out
    try:
        for p in con.execute(
                "SELECT symbol, side, weight, qty, entry_px, entry_ms,"
                " funding_usd FROM resmom_positions"
                " WHERE status='open'").fetchall():
            out["open"].append({
                "symbol": p["symbol"], "side": p["side"],
                "size": p["qty"], "entry": p["entry_px"], "mark": None,
                "unreal_usd": p["funding_usd"], "entry_ms": p["entry_ms"]})
        for p in con.execute(
                "SELECT symbol, side, qty, entry_px, exit_px, pnl_usd,"
                " funding_usd, entry_ms, exit_ms, close_reason"
                " FROM resmom_positions WHERE status='closed'"
                " ORDER BY exit_ms DESC LIMIT 200").fetchall():
            out["closed"].append({
                "symbol": p["symbol"], "side": p["side"], "size": p["qty"],
                "entry": p["entry_px"], "exit": p["exit_px"],
                "pnl_usd": p["pnl_usd"], "pnl_pct": None,
                "funding_usd": p["funding_usd"], "fees_usd": None,
                "entry_ms": p["entry_ms"], "exit_ms": p["exit_ms"],
                "reason": p["close_reason"]})
        nav = con.execute(
            "SELECT ts_ms, nav_usd, dd_pct FROM resmom_nav"
            " ORDER BY ts_ms").fetchall()
        out["equity"] = [(int(m["ts_ms"]), float(m["nav_usd"]))
                         for m in nav]
        out["dd"] = [(int(m["ts_ms"]), float(m["dd_pct"])) for m in nav]
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── VWAPDeviation iv_gate — engine shadow board (no own ledger) ───────

def _ivgate_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "ivgate", "VWAP iv_gate", "shadow", prereg_state="confirm")
    try:
        from src.research.shadow_panel import _load_persisted_boards
        from src.utils.config import load_config
        cfg = load_config(ROOT / "config" / "settings.yaml")
        latest = _load_persisted_boards(cfg)
        boards = latest[0] if latest else {}
        batch_ts = latest[1] if latest else None
        board = None
        if isinstance(boards, dict):
            # The card is the VWAPDeviation iv_gate confirm variant —
            # prefer a strategy=VWAPDeviation iv_gate board; fall back to
            # any iv_gate board only if VWAP's is absent.
            for want_vwap in (True, False):
                for k, v in boards.items():
                    if not isinstance(v, dict):
                        continue
                    name = f"{v.get('strategy','')}#{v.get('variant','')}"
                    if "iv_gate" not in (k + name):
                        continue
                    if want_vwap and "VWAP" not in str(v.get("strategy")):
                        continue
                    board = v
                    break
                if board:
                    break
        if board:
            card["n"] = (board.get("n_independent")
                         or board.get("n_evaluated"))
            card["net"] = board.get("net_hypothetical_pnl_pct")
            card["net_unit"] = "pct"
            card["wr"] = (round(board["win_rate"] * 100.0, 1)
                          if board.get("win_rate") is not None else None)
            detail = (board.get("evaluated_ms") or board.get("batch_ms")
                      or batch_ts)
            card["state"], card["state_detail"] = _age_state(
                detail, False, now_ms, _STALE_IVGATE)
            return card
        card["state"], card["state_detail"] = "no_signal", "sem board"
        return card
    except Exception:  # noqa: BLE001
        return card


def _ivgate_detail(now_ms: int) -> Dict[str, Any]:
    return {"open": [], "closed": [], "equity": [], "dd": []}


# ── assembly ──────────────────────────────────────────────────────────

_CARDS = {
    "jev": _jev_card, "carry": _carry_card, "unlock": _unlock_card,
    "boros": _boros_card, "resmom": _resmom_card, "ivgate": _ivgate_card,
}
_DETAILS = {
    "jev": _jev_detail, "carry": _carry_detail, "unlock": _unlock_detail,
    "boros": _boros_detail, "resmom": _resmom_detail,
    "ivgate": _ivgate_detail,
}


def build_strategies_payload(now_ms: Optional[int] = None
                             ) -> Dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    rows = []
    for sid in STRATEGY_IDS:
        try:
            rows.append(_CARDS[sid](now))
        except Exception as exc:  # noqa: BLE001 — degrade, never 500
            logger.debug("paper_trades card %s failed: %s", sid, exc)
            rows.append(_base_card(sid, sid, "shadow"))
    return {"rows": rows, "generated_ms": now}


def build_strategy_detail(sid: str,
                          now_ms: Optional[int] = None) -> Dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    fn = _DETAILS.get(sid)
    if fn is None:
        return {"error": f"unknown strategy id {sid!r}"}
    try:
        out = fn(now)
        out["id"] = sid
        out["generated_ms"] = now
        return out
    except Exception as exc:  # noqa: BLE001
        return {"id": sid, "error": str(exc)[:200],
                "open": [], "closed": [], "equity": [], "dd": []}


def build_trades_feed(limit: int = 60,
                      now_ms: Optional[int] = None) -> List[Dict[str, Any]]:
    """Merged closed-trade feed across every ledger, newest first."""
    now = now_ms or int(time.time() * 1000)
    feed: List[Dict[str, Any]] = []

    def push(rows: List[Dict[str, Any]]) -> None:
        feed.extend(rows)

    try:
        d = _jev_detail(now)
        push([{**t, "strat": "JevJudge"} for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass
    try:
        d = _carry_detail(now)
        push([{**t, "strat": "CarryA1"} for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass
    try:
        d = _unlock_detail(now)
        push([{**t, "strat": "UnlockShort"} for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass
    try:
        d = _boros_detail(now)
        push([{**t, "strat": "Boros"} for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass
    try:
        d = _resmom_detail(now)
        push([{**t, "strat": "ResMom"} for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass

    feed.sort(key=lambda t: t.get("exit_ms") or 0, reverse=True)
    return feed[:max(1, int(limit))]
