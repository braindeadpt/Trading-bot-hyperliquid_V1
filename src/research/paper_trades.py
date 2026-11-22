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

import json
import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
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
_STALE_CARRY = 90 * 60_000    # events arrive in ~hourly bursts
_STALE_UNLOCK = 30 * 3_600_000 # snapshots daily ~00:30 UTC
_STALE_BOROS = 90 * 60_000    # snapshot + marks hourly (log: :00 and :05)
_STALE_RESMOM = 30 * 3_600_000  # nav ~daily
_STALE_IVGATE = 12 * 3_600_000  # board re-eval on demand

# Living strategies shown on the main page, in display order. `id` is the
# stable key used by /api/paper_strategy/<id> and the frontend card click.
STRATEGY_IDS: Tuple[str, ...] = (
    "jev", "carry", "unlock", "boros", "resmom", "ivgate", "fundcarry",
)

# UnlockShort registered read (docs/ops/PREREGISTER_UNLOCK_SHADOW.md §6):
# "n >= 15 closed eligible events or 2027-10-14, whichever comes first";
# "fewer than 15 eligible events at expiry -> INCONCLUSIVE". Kept equal to
# paper_ledger._unlock_row (a test pins the equality).
_UNLOCK_N_READOUT = 15
_UNLOCK_READOUT_MS = int(datetime(2027, 10, 14, tzinfo=timezone.utc)
                         .timestamp() * 1000)

# FundCarry Phase 1 (historical backtest, not paper) result artifact.
_FUNDCARRY_JSON = ROOT / "data" / "backtests" / "fundcarry" / "phase1_result.json"


def _fmt_day(ms: Optional[int]) -> str:
    """UTC dd/mm/yyyy — year included so a 2027 readout is never read as 2026."""
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d/%m/%Y")


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


# Minimum closed observations before an annualized figure is treated as
# more than noise. Annualizing 2 trades (or 1 week) extrapolates a tiny
# sample by ~50x, so below this the APY is still shown (owner wants every
# number) but flagged `apy_indicative`. 10 = a coarse floor, not a
# significance test. `n` is the card's own closed-observation count:
# closed trades (Boros) or closed weeks (ResMom); a missing n counts as 0.
APY_MIN_N = 10


def _mark_apy_reliability(card: Dict[str, Any]) -> None:
    """Add apy_indicative / apy_n to a card that reports an APY."""
    if card.get("apy") is None:
        return
    try:
        n = int(card.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    card["apy_n"] = n
    card["apy_indicative"] = n < APY_MIN_N


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
        "jev", "JevJudge v2", "execution",
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
        # n = the gated count (post-boundary, non-stale, deduped,
        # one-open-per-symbol) — the number the kill read actually
        # uses, not the raw closed-trade total.
        try:
            from scripts.research.jev_eval import (
                GEOMETRY_BOUNDARY_MS, _independent_trades,
                is_stale_verdict, split_duplicate_verdicts)
            grow = con.execute(
                "SELECT symbol, entry_time, exit_time, signal_metadata"
                " FROM trades WHERE strategy='JevJudge' AND status='closed'"
                " AND entry_time > ? ORDER BY entry_time",
                (GEOMETRY_BOUNDARY_MS,)).fetchall()
            trows = [dict(x) for x in grow]
            trows = [t for t in trows
                     if not is_stale_verdict(t["signal_metadata"],
                                             int(t["entry_time"]))]
            trows, _dupes = split_duplicate_verdicts(trows)
            card["n"] = len(_independent_trades(trows))
        except Exception:  # noqa: BLE001 — fall back to raw count
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
        # JevJudge was formally KILLED on 2026-11-11 (indep_n=325 >= 100,
        # net PF=0.8373 <= 1.0) and hl-jev-judge is stopped. Report 'closed'
        # instead of 'error · stale' once open paper rows drain.
        if card["n_open"] > 0:
            card["state"], card["state_detail"] = "running", "drenando posições abertas (KILLED)"
        else:
            card["state"], card["state_detail"] = "closed", "KILLED 11/11 · PF 0.84"
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
        for t in rows:
            out["closed"].append({
                "symbol": t["symbol"], "side": t["side"], "size": t["size"],
                "entry": t["entry_price"], "exit": t["exit_price"],
                "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"],
                "funding_usd": None, "fees_usd": None,
                "entry_ms": t["entry_time"], "exit_ms": t["exit_time"],
                "reason": t["exit_reason"]})
        cum = 0.0
        series: List[Tuple[int, float]] = []
        for t in reversed(rows):
            if t["exit_time"]:
                cum += float(t["pnl_usd"] or 0.0)
                series.append((int(t["exit_time"]), round(cum, 4)))
        out["equity"] = series
        # Drawdown % of the paper base ($10k initial_capital) so the modal's
        # "max DD %" is comparable across strategies.
        peak = 10_000.0
        for ts, v in series:
            eq_usd = 10_000.0 + v
            peak = max(peak, eq_usd)
            out["dd"].append((ts, round((eq_usd - peak) / peak * 100.0, 4)
                              if peak else 0.0))
        return out
    except Exception:  # noqa: BLE001
        return out
    finally:
        con.close()


# ── CarryA1 spot–perp (carry_shadow.db) ───────────────────────────────

def _carry_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "carry", "CarryA1 spot–perp", "shadow",
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
            card["_hb_failed"] = True
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
        "unlock", "UnlockShort", "shadow",
        prereg_state="in_progress")
    con = _ro(_RESEARCH / "unlock_shadow.db")
    if con is None:
        _apply_unlock_readout(card, now_ms)
        return card
    try:
        st = con.execute(
            "SELECT state, COUNT(*) c FROM unlock_sims WHERE kind='event'"
            " GROUP BY state").fetchall()
        states = {r["state"]: int(r["c"]) for r in st}
        card["n"] = states.get("closed", 0)
        card["n_open"] = states.get("open", 0)
        # Readout threshold (§6): only events closed at/before the
        # deadline count. Events closed later stay in `n` for display but
        # must never flip a post-deadline INCONCLUSIVO.
        card["_n_by_deadline"] = int(con.execute(
            "SELECT COUNT(*) c FROM unlock_sims WHERE kind='event'"
            " AND state='closed' AND priced_out_ms <= ?",
            (_UNLOCK_READOUT_MS,)).fetchone()["c"])
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
            card["_hb_failed"] = True
        card["state"], card["state_detail"] = _age_state(
            hb_ts or agg["last_ts"],
            card["n_open"] > 0, now_ms, _STALE_UNLOCK)
        _apply_unlock_readout(card, now_ms)
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        con.close()


def _apply_unlock_readout(card: Dict[str, Any], now_ms: int) -> None:
    """Surface the pre-registered readout outcome once the read is due.

    Rule (PREREGISTER_UNLOCK_SHADOW §6): read at n>=15 closed eligible
    events or at 2027-10-14, whichever first; <15 at expiry = INCONCLUSIVE.
    PASS/FAIL themselves need the four §6 criteria (not computed here), so
    when n>=15 the card only says the read is due — never invents a verdict.
    A healthy-but-stale writer keeps its error state; the readout note is
    appended instead of replacing it.
    """
    n = int(card.get("n") or 0)
    n_due = int(card.pop("_n_by_deadline", 0) or 0)
    if now_ms >= _UNLOCK_READOUT_MS:
        # Deadline reached — the preregistered read is frozen by the
        # events that had closed at that instant, nothing later.
        if n_due >= _UNLOCK_N_READOUT:
            card["verdict"] = "por registar"
            card["state_detail"] = (
                f"n {n_due}/{_UNLOCK_N_READOUT} atingido antes do prazo "
                "— leitura §6 por registar")
        else:
            card["verdict"] = "INCONCLUSIVO"
            card["state"] = "verdict"
            card["state_detail"] = (
                f"readout passou em {_fmt_day(_UNLOCK_READOUT_MS)} · "
                f"n {n_due}/{_UNLOCK_N_READOUT} < {_UNLOCK_N_READOUT} → "
                "INCONCLUSIVO (prereg §6)")
        return
    if n >= _UNLOCK_N_READOUT:
        card["verdict"] = "por registar"
        card["state_detail"] = (
            f"n {n}/{_UNLOCK_N_READOUT} atingido — leitura §6 por registar")


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

def _boros_pooled_margin_base() -> float:
    """Return Boros pooled perp margin base (0.2 * $10,000 = $2,000.0)."""
    try:
        from src.boros_shadow import spec as _bs

        return float(_bs.PERP_CAPITAL_FRAC) * float(_bs.NOTIONAL_USD)
    except Exception:  # noqa: BLE001
        return 2_000.0


def _boros_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "boros", "Boros locked spread", "shadow",
        prereg_state="in_progress")
    con = _ro(_RESEARCH / "boros_shadow.db")
    if con is None:
        return card
    try:
        open_ids = [
            int(r["id"])
            for r in con.execute(
                "SELECT id FROM boros_trades WHERE state='open'"
            ).fetchall()
        ]
        card["n_open"] = len(open_ids)
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
        _mark_apy_reliability(card)
        mk = con.execute(
            "SELECT ts_ms, equity_pooled FROM boros_marks"
            " WHERE equity_pooled IS NOT NULL ORDER BY ts_ms").fetchall()
        series = [(int(m["ts_ms"]), float(m["equity_pooled"])) for m in mk]
        card["equity"] = _downsample(series)
        # equity_pooled = pooled_margin_base ($2,000) + perp uPnL + net funding
        # on that single trade. Sum (equity_pooled - base) across currently
        # OPEN trades instead of subtracting closed realized PnL from raw NAV.
        if open_ids:
            base_usd = _boros_pooled_margin_base()
            unreal_sum = 0.0
            has_mark = False
            for tid in open_ids:
                lm = con.execute(
                    "SELECT equity_pooled FROM boros_marks"
                    " WHERE trade_id=? AND equity_pooled IS NOT NULL"
                    " ORDER BY ts_ms DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                if lm is not None and lm["equity_pooled"] is not None:
                    unreal_sum += float(lm["equity_pooled"]) - base_usd
                    has_mark = True
            card["open_unreal_usd"] = round(unreal_sum, 4) if has_mark else 0.0
        else:
            card["open_unreal_usd"] = 0.0
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
        base_usd = _boros_pooled_margin_base()
        for t in con.execute(
                "SELECT id, asset, structure, direction, entry_ms,"
                " capital_usd, fixed_bruto, net_apy FROM boros_trades"
                " WHERE state='open'").fetchall():
            last = con.execute(
                "SELECT equity_pooled, funding_usd, ts_ms FROM boros_marks"
                " WHERE trade_id=? ORDER BY ts_ms DESC LIMIT 1",
                (t["id"],)).fetchone()
            unreal_val = (
                round(float(last["equity_pooled"]) - base_usd, 4)
                if last and last["equity_pooled"] is not None
                else None
            )
            out["open"].append({
                "symbol": t["asset"], "side": t["direction"],
                "size": t["capital_usd"], "entry": t["fixed_bruto"],
                "mark": None, "unreal_usd": unreal_val,
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

_RESMOM_ARMS: Tuple[str, ...] = ("A", "B", "BIN")
# Registered arm = A (HL, prereg 2026-10-28); B is descriptive, BIN is a
# descriptive Binance reference. Only A feeds the card's headline numbers.
_RESMOM_REG_ARM = "A"
# Fixed gross notional per arm — same constant the daemon accounts NAV
# against (src/resmom_shadow/spec.py). Returns must be vs this base: the
# first NAV mark already discounts entry cost.
try:
    from src.resmom_shadow.spec import NOTIONAL_USD as _RESMOM_NOTIONAL
except Exception:  # noqa: BLE001 — spec import must never break the page
    _RESMOM_NOTIONAL = 10_000.0
_RESMOM_ARM_LABEL = {
    "A": "ResMom·A",
    "B": "ResMom·B (desc.)",
    "BIN": "ResMom·BIN (ref.)",
}


def _resmom_arm_label(arm: Any) -> str:
    return _RESMOM_ARM_LABEL.get(str(arm), f"ResMom·{arm}")


def _resmom_card(now_ms: int) -> Dict[str, Any]:
    card = _base_card(
        "resmom", "ResMom forward", "shadow",
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
        arms: Dict[str, Dict[str, Any]] = {}
        nav_by_arm: Dict[str, List[Tuple[int, float]]] = {}
        for arm in _RESMOM_ARMS:
            nav = con.execute(
                "SELECT ts_ms, nav_usd FROM resmom_nav WHERE arm=?"
                " ORDER BY ts_ms", (arm,)).fetchall()
            ser = [(int(m["ts_ms"]), float(m["nav_usd"])) for m in nav]
            nav_by_arm[arm] = ser
            n_open = int(con.execute(
                "SELECT COUNT(*) c FROM resmom_positions"
                " WHERE arm=? AND status='open'", (arm,)).fetchone()["c"])
            wr_rows = con.execute(
                "SELECT pnl_usd p FROM resmom_positions"
                " WHERE arm=? AND status='closed'", (arm,)).fetchall()
            n_reb = int(con.execute(
                "SELECT COUNT(*) c FROM resmom_rebalances WHERE arm=?",
                (arm,)).fetchone()["c"])
            # cum_return is vs the fixed gross NOTIONAL — the first NAV
            # mark already discounts entry cost, so basing the return on
            # it would hide that cost (Devin Review PR#21).
            ret = None
            if ser:
                ret = round(
                    (ser[-1][1] / _RESMOM_NOTIONAL - 1.0) * 100.0, 3)
            arms[arm] = {
                "nav": round(ser[-1][1], 2) if ser else None,
                "cum_return_pct": ret,
                "n_open": n_open,
                "wr": _wr([r["p"] for r in wr_rows]),
                "n_rebalances": n_reb,
                "n_nav": len(ser),
                "registered": arm == _RESMOM_REG_ARM,
            }
        card["arms"] = arms
        series = nav_by_arm[_RESMOM_REG_ARM]
        card["equity"] = _downsample(series)
        card["n_open"] = arms[_RESMOM_REG_ARM]["n_open"]
        card["wr"] = arms[_RESMOM_REG_ARM]["wr"]
        if len(series) >= 2 and series[-1][0] > series[0][0]:
            span = series[-1][0] - series[0][0]
            card["apy"] = round(
                ((series[-1][1] / _RESMOM_NOTIONAL) ** (_MS_YEAR / span)
                 - 1.0) * 100.0, 2)
        last_ts = con.execute(
            "SELECT MAX(entry_ms) ts FROM resmom_positions").fetchone()["ts"]
        # Heartbeat = freshest nav of ANY arm (daemon liveness); the
        # headline numbers above stay arm-A only.
        navs_last = [v[-1][0] for v in nav_by_arm.values() if v]
        hb = max(navs_last) if navs_last else last_ts
        if hb is None:
            # Newborn shadow (first nav snapshot may lag deploy) — flag as
            # starting, not error.
            card["state"], card["state_detail"] = "no_signal", "a iniciar"
        else:
            card["state"], card["state_detail"] = _age_state(
                hb, card["n_open"] > 0, now_ms, _STALE_RESMOM)
        _mark_apy_reliability(card)
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
                "SELECT arm, symbol, side, weight, qty, entry_px, entry_ms,"
                " funding_usd FROM resmom_positions"
                " WHERE status='open' ORDER BY arm, symbol").fetchall():
            out["open"].append({
                "arm": p["arm"],
                "symbol": p["symbol"], "side": p["side"],
                "size": p["qty"], "entry": p["entry_px"], "mark": None,
                "unreal_usd": p["funding_usd"], "entry_ms": p["entry_ms"]})
        for p in con.execute(
                "SELECT arm, symbol, side, qty, entry_px, exit_px, pnl_usd,"
                " funding_usd, entry_ms, exit_ms, close_reason"
                " FROM resmom_positions WHERE status='closed'"
                " ORDER BY exit_ms DESC LIMIT 200").fetchall():
            out["closed"].append({
                "arm": p["arm"],
                "symbol": p["symbol"], "side": p["side"], "size": p["qty"],
                "entry": p["entry_px"], "exit": p["exit_px"],
                "pnl_usd": p["pnl_usd"], "pnl_pct": None,
                "funding_usd": p["funding_usd"], "fees_usd": None,
                "entry_ms": p["entry_ms"], "exit_ms": p["exit_ms"],
                "reason": p["close_reason"]})
        # Headline equity/drawdown = registered arm A only.
        nav = con.execute(
            "SELECT ts_ms, nav_usd, dd_pct FROM resmom_nav WHERE arm=?"
            " ORDER BY ts_ms", (_RESMOM_REG_ARM,)).fetchall()
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
    con = None
    try:
        import json as _json
        con = _ro(_RESEARCH / "hyperliquid.db")
        boards: Dict[str, Any] = {}
        batch_ts = None
        if con is not None:
            try:
                row = con.execute(
                    "SELECT MAX(evaluated_at_ms) AS ts "
                    "FROM shadow_outcome_scoreboards").fetchone()
                batch_ts = (int(row["ts"]) if row and row["ts"] is not None
                            else None)
                if batch_ts is not None:
                    for r in con.execute(
                            "SELECT metrics_json FROM "
                            "shadow_outcome_scoreboards WHERE "
                            "evaluated_at_ms = ?", (batch_ts,)):
                        try:
                            d = _json.loads(r["metrics_json"])
                        except Exception:  # noqa: BLE001
                            continue
                        boards[f"{d.get('strategy')}::{d.get('variant')}"] = d
            except Exception:  # noqa: BLE001 — table may not exist
                pass
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
        if con is None:
            card["state"], card["state_detail"] = (
                "error", "sem dados (research DB ausente)")
            return card
        if board:
            # `sealed` is only present in to_dict() for confirmation boards
            # (shadow_outcome_evaluator.StrategyScoreboard.to_dict): an
            # absent key means "not a sealed board", NOT sealed.
            sealed = bool(board.get("sealed", False)) and not board.get(
                "confirmation_expired")
            card["sealed"] = sealed
            if sealed:
                card["prereg_state"] = "SEALED"
            # n = independent outcomes (the gate's n). Fall back to
            # n_evaluated only when n_independent is absent - a real 0 stays 0.
            n_ind = board.get("n_independent")
            card["n"] = n_ind if n_ind is not None else board.get(
                "n_evaluated")
            min_indep = board.get("confirmation_min_indep")
            if min_indep:
                card["n_readout"] = min_indep
            card["net_unit"] = "pct"
            if sealed:
                # A sealed board deliberately leaves every metric at its
                # dataclass default (0.0) - build_scoreboard returns early.
                # Those zeros are "not computed", not "0% WR / 0% net":
                # show "-" instead of fabricating a result.
                card["net"], card["wr"] = None, None
            else:
                card["net"] = board.get("net_hypothetical_pnl_pct")
                card["wr"] = (round(board["win_rate"] * 100.0, 1)
                              if board.get("win_rate") is not None
                              else None)
            detail = (board.get("evaluated_ms")
                      or board.get("batch_ms") or batch_ts)
            card["data_ms"] = detail
            card["state"], card["state_detail"] = _age_state(
                detail, False, now_ms, _STALE_IVGATE)
            if detail is not None and now_ms - detail > _STALE_IVGATE:
                # Explicit "stale" with the data date - never a silent
                # "sem sinal" for a board nobody is refreshing.
                card["state"] = "stale"
                card["state_detail"] = (
                    f"dados de {_fmt_day(detail)} "
                    f"(há {int((now_ms - detail) / _MS_DAY)}d) — "
                    "research DB sem escritas")
            elif sealed:
                card["state_detail"] += (
                    f" · métricas seladas até n≥{min_indep}"
                    if min_indep else " · métricas seladas")
            return card
        card["state"], card["state_detail"] = "no_signal", "sem board"
        return card
    except Exception:  # noqa: BLE001
        return card
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass


def _ivgate_detail(now_ms: int) -> Dict[str, Any]:
    return {"open": [], "closed": [], "equity": [], "dd": []}


# -- FundCarry - HISTORICAL backtest (not paper) --------------------------

_FC_VERDICT_BADGE = {
    "PASS": ("badge-green", "PASS"),
    "PASS-provisório": ("badge-yellow", "PASS-provisório"),
    "KILL (C)": ("badge-red", "KILL (C)"),
    "INCONCLUSIVO": ("badge-dim", "INCONCLUSIVO"),
}


def _fc_norm_verdict(raw: Any) -> Optional[str]:
    if raw is None or not str(raw).strip():
        return None
    v = str(raw).strip().lower().replace("ó", "o").replace("í", "i")
    if v.startswith("pass") and "provis" in v:
        return "PASS-provisório"
    if v.startswith("pass"):
        return "PASS"
    if v.startswith("kill"):
        return "KILL (C)"
    if v.startswith("inconcl"):
        return "INCONCLUSIVO"
    return str(raw).strip()


def _fc_get(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _fc_num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fc_first(d: Dict[str, Any], *paths: Tuple[str, ...]) -> Any:
    for p in paths:
        v = _fc_get(d, *p)
        if v is not None:
            return v
    return None


def _fc_week_ts(w: Dict[str, Any]) -> Optional[int]:
    """Weekly-row timestamp: ts_ms, then week_ms, then the date string."""
    try:
        ts = w.get("ts_ms")
        if ts is None:
            ts = w.get("week_ms")
        if ts is not None:
            return int(ts)
        return int(datetime.strptime(str(w.get("date")), "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _fc_equity(res: Dict[str, Any]) -> List[Tuple[int, float]]:
    """Cumulative net USD series: explicit `equity` [[ts_ms, v], ...] if
    present, else cumulative sum of `weekly[].pnl_usd` keyed by `date`."""
    out: List[Tuple[int, float]] = []
    eq = res.get("equity")
    if isinstance(eq, list):
        for pt in eq:
            try:
                out.append((int(pt[0]), float(pt[1])))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        if out:
            return _downsample(out)
    cum = 0.0
    for w in res.get("weekly") or []:
        if not isinstance(w, dict):
            continue
        pnl = _fc_num(w.get("pnl_usd"))
        ts = _fc_week_ts(w)
        if pnl is None or ts is None:
            continue
        cum += pnl
        out.append((ts, round(cum, 4)))
    return _downsample(out)


def _fc_dd(res: Dict[str, Any]) -> List[Tuple[int, float]]:
    """Drawdown series in percentage points, rebuilt from weekly
    compounded returns — the same convention the artifact uses for
    `max_drawdown` (nav *= 1+ret, drop vs running peak), so the series
    minimum equals the artifact's value."""
    nav = 1.0
    peak = 1.0
    out: List[Tuple[int, float]] = []
    for w in res.get("weekly") or []:
        if not isinstance(w, dict):
            continue
        r = _fc_num(w.get("ret"))
        ts = _fc_week_ts(w)
        if r is None or ts is None:
            continue
        nav *= 1.0 + r
        peak = max(peak, nav)
        out.append((ts, round((nav / peak - 1.0) * 100.0, 4)))
    if not out:
        return out
    ds = _downsample(out)
    # The modal shows min(dd) as the max drawdown — stride downsampling
    # can drop the true trough, so re-insert it if it was decimated.
    argmin = min(range(len(out)), key=lambda i: out[i][1])
    if out[argmin] not in ds:
        pos = next((i for i, p in enumerate(ds)
                    if p[0] > out[argmin][0]), len(ds))
        ds.insert(pos, out[argmin])
    return ds


def _fundcarry_result() -> Tuple[str, Optional[Dict[str, Any]]]:
    """('missing'|'unreadable'|'ok', parsed JSON)."""
    if not _FUNDCARRY_JSON.exists():
        return "missing", None
    try:
        res = json.loads(_FUNDCARRY_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable", None
    return ("ok", res) if isinstance(res, dict) else ("unreadable", None)


def _fundcarry_card(now_ms: int) -> Dict[str, Any]:
    """FundCarry Phase 1 historical card (kind="historical").

    Reads ``data/backtests/fundcarry/phase1_result.json`` defensively.
    Keys looked up (first hit wins, every one optional -> shown as "-"):
      verdict                         PASS | PASS-provisorio | KILL (C) |
                                      INCONCLUSIVO (accent/case tolerant)
      decomposition_usd.net | net_usd net cumulative PnL (USD)
      cum_return                      fraction (0.12 = +12%), fallback net
      sharpe                          annualised Sharpe
      sign_flip.n_pos / sign_flip.K   positive windows / K  (also n_pos, K)
      sign_flip.p | sign_flip_p       sign-flip p-value
      nulls.real_percentile | null_percentile   percentile vs null Sharpes
      ex_top3.pnl_ex_top3_usd | pnl_ex_top3_usd PnL excluding top-3
      equity [[ts_ms, usd]] | weekly[{date|ts_ms, pnl_usd}]  equity curve
      max_drawdown                    fraction (optional)
    Never touches paper ledgers; the card is flagged as not paper.
    """
    card = _base_card("fundcarry", "FundCarry", "historical",
                      prereg_state="HISTÓRICO (backtest) — não é paper")
    status, res = _fundcarry_result()
    if status == "missing":
        card["state"] = "testing"
        card["state_detail"] = "a testar — Fase 1 histórica em curso"
        card["badge"] = ["badge-blue", "a testar"]
        return card
    if res is None:
        card["state"], card["state_detail"] = (
            "error", "phase1_result.json ilegível")
        return card
    verdict = _fc_norm_verdict(res.get("verdict"))
    net_usd = _fc_num(_fc_first(res, ("decomposition_usd", "net"),
                                ("net_usd",), ("net_pnl_usd",)))
    cum = _fc_num(res.get("cum_return"))
    n_pos = _fc_first(res, ("sign_flip", "n_pos"), ("n_pos",))
    k = _fc_first(res, ("sign_flip", "K"), ("K",))
    mdd = _fc_num(res.get("max_drawdown"))
    hist = {
        "verdict": verdict,
        "net_usd": net_usd,
        "cum_return_pct": round(cum * 100.0, 2) if cum is not None else None,
        "sharpe": _fc_num(res.get("sharpe")),
        "windows_pos": int(n_pos) if _fc_num(n_pos) is not None else None,
        "windows_k": int(k) if _fc_num(k) is not None else None,
        "signflip_p": _fc_num(_fc_first(res, ("sign_flip", "p"),
                                        ("sign_flip_p",))),
        "null_percentile": _fc_num(_fc_first(
            res, ("nulls", "real_percentile"), ("null_percentile",))),
        "pnl_ex_top3_usd": _fc_num(_fc_first(
            res, ("ex_top3", "pnl_ex_top3_usd"), ("pnl_ex_top3_usd",))),
        "max_drawdown_pct": round(mdd * 100.0, 2) if mdd is not None else None,
    }
    card["hist"] = hist
    if net_usd is not None:
        card["net"], card["net_unit"] = round(net_usd, 2), "usd"
    elif hist["cum_return_pct"] is not None:
        card["net"], card["net_unit"] = hist["cum_return_pct"], "pct"
    card["n"] = hist["windows_k"]
    card["equity"] = _fc_equity(res)
    card["state"] = "historical"
    card["badge"] = list(_FC_VERDICT_BADGE.get(
        verdict or "", ("badge-dim", verdict or "sem veredicto")))
    card["state_detail"] = (verdict or "sem veredicto") + " · backtest histórico"
    return card


def _fundcarry_detail(now_ms: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {"open": [], "closed": [], "equity": [], "dd": []}
    _status, res = _fundcarry_result()
    if res is not None:
        out["equity"] = _fc_equity(res)
        out["dd"] = _fc_dd(res)
    return out


# ── assembly ──────────────────────────────────────────────────────────

_CARDS = {
    "jev": _jev_card, "carry": _carry_card, "unlock": _unlock_card,
    "boros": _boros_card, "resmom": _resmom_card, "ivgate": _ivgate_card,
    "fundcarry": _fundcarry_card,
}
_DETAILS = {
    "jev": _jev_detail, "carry": _carry_detail, "unlock": _unlock_detail,
    "boros": _boros_detail, "resmom": _resmom_detail,
    "ivgate": _ivgate_detail, "fundcarry": _fundcarry_detail,
}


# Last-good card cache: a transient writer lock or a mid-write snapshot
# blanks a whole card build. Serve the previous good card (flagged) for a
# few minutes instead of flashing "error" on a healthy daemon.
_LAST_GOOD: Dict[str, Tuple[int, Dict[str, Any]]] = {}
_LAST_GOOD_MS = 15 * 60_000


def _card_or_last_good(sid: str, now_ms: int) -> Dict[str, Any]:
    try:
        card = _CARDS[sid](now_ms)
    except Exception as exc:  # noqa: BLE001 — degrade, never 500
        logger.debug("paper_trades card %s failed: %s", sid, exc)
        card = _base_card(sid, sid, "shadow")
    _mark_apy_reliability(card)
    hb_failed = bool(card.pop("_hb_failed", False))
    build_failed = card.get("state") == "error" and card.get(
        "state_detail") in ("", "sem dados")
    if hb_failed or build_failed:
        # Transient build/heartbeat failure — serve the previous good
        # card rather than a misleading error badge.
        prev = _LAST_GOOD.get(sid)
        if prev and (now_ms - prev[0]) < _LAST_GOOD_MS:
            old = dict(prev[1])
            old["state_detail"] = (
                (old.get("state_detail") or "") + " · cache")
            return old
    else:
        _LAST_GOOD[sid] = (now_ms, card)
    return card


# Card id -> canonical paper_ledger row id. n_readout/readout_ms come
# from that layer (single source for the registered numbers) — never
# hardcode them per card.
_LEDGER_ID = {
    "jev": "JevJudge::experiment",
    "carry": "CarryA1::shadow",
    "unlock": "UnlockShort::shadow",
    "boros": "BorosLockedSpread::shadow",
    "resmom": "ResidualMomentum::forward",
}


def _is_sealed(prereg_state: Optional[str]) -> bool:
    """Factual prereg seal flag — surfaced on the card for context; the
    owner decided the dashboard shows all metrics regardless."""
    return "seal" in str(prereg_state or "").lower() or "selad" in str(
        prereg_state or "").lower()


def build_strategies_payload(now_ms: Optional[int] = None
                             ) -> Dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    ledger: Dict[str, Dict[str, Any]] = {}
    try:
        from src.research.paper_ledger import build_paper_ledger_payload
        for r in build_paper_ledger_payload().get("rows", []):
            if isinstance(r, dict) and r.get("id"):
                ledger[str(r["id"])] = r
    except Exception:  # noqa: BLE001 — cards still render without it
        pass
    rows = []
    for sid in STRATEGY_IDS:
        card = _card_or_last_good(sid, now)
        lr = ledger.get(_LEDGER_ID.get(sid, ""))
        if lr:
            if lr.get("n_readout") is not None:
                card["n_readout"] = lr["n_readout"]
            if lr.get("readout_ms") is not None:
                card["readout_ms"] = lr["readout_ms"]
            if lr.get("prereg_state"):
                card["prereg_state"] = lr["prereg_state"]
        if _is_sealed(card.get("prereg_state")):
            card["sealed"] = True
        rows.append(card)
    return {"rows": rows, "generated_ms": now}


def _ledger_row_for(sid: str) -> Optional[Dict[str, Any]]:
    try:
        from src.research.paper_ledger import build_paper_ledger_payload
        lid = _LEDGER_ID.get(sid)
        for r in build_paper_ledger_payload().get("rows", []):
            if r.get("id") == lid:
                return r
    except Exception:  # noqa: BLE001
        pass
    return None


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
        if sid == "ivgate":
            out["sealed"] = True
        else:
            lr = _ledger_row_for(sid)
            if lr and _is_sealed(lr.get("prereg_state")):
                out["sealed"] = True
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
        push([{**t, "strat": _resmom_arm_label(t.get("arm"))}
              for t in d["closed"]])
    except Exception:  # noqa: BLE001
        pass

    feed.sort(key=lambda t: t.get("exit_ms") or 0, reverse=True)
    return feed[:max(1, int(limit))]
