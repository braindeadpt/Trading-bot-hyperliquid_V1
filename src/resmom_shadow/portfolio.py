"""Paper portfolio accounting (prereg §Regra — carteira/custos/funding).

NAV convention: nav_t = NOTIONAL_USD + realized_price_pnl + funding_pnl
- costs + unrealized(open at current mids). Legs are sized at
|weight| * NOTIONAL_USD — fixed gross notional, never NAV-compounded.
Funding: rate>0 means longs pay shorts (pays = -rate * notional for
longs). Notional at event time uses the current allMids mid — the hourly
accrual pass runs close to event time; documented approximation.

Drawdown is descriptive (25% stop never acts); the <= -35% net-dd KILL is
flagged in meta + log by the daemon but never stops anything by itself.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from src.resmom_shadow import spec
from src.resmom_shadow.ledger import ResmomLedger

ARMS = ("A", "B", "BIN")
_NOTIONAL = spec.NOTIONAL_USD


def _k(arm: str, what: str) -> str:
    return f"{what}_{arm}"


def cum(ledger: ResmomLedger, arm: str, what: str) -> float:
    return float(ledger.meta_get(_k(arm, what)) or 0.0)


def _bump(ledger: ResmomLedger, arm: str, what: str, delta: float) -> float:
    v = cum(ledger, arm, what) + delta
    ledger.meta_set(_k(arm, what), repr(v))
    return v


def signed_weight(pos: Dict[str, Any]) -> float:
    return float(pos["weight"]) if pos["side"] == "long" else -float(pos["weight"])


def target_weights(portfolio_weights: Dict[str, float]) -> Dict[str, float]:
    return {s: float(w) for s, w in portfolio_weights.items() if abs(w) > 0}


def turnover_usd(prev: Dict[str, float], target: Dict[str, float]) -> float:
    return sum(abs(target.get(s, 0.0) - prev.get(s, 0.0)) * _NOTIONAL
               for s in set(prev) | set(target))


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def apply_rebalance(ledger: ResmomLedger, arm: str, week_key_ms: int,
                    rebalance_row_id: int, prev_open: List[Dict[str, Any]],
                    targets: Dict[str, float], prices: Dict[str, float],
                    entry_ms: int) -> Dict[str, Any]:
    """Close prev legs at `prices`, open target legs, charge turnover.

    positions_open must already be the CURRENT open rows for `arm` —
    callers pass the fresh fetch so ids stay valid.
    """
    realized = 0.0
    prev_w: Dict[str, float] = {}
    fills_used: Dict[str, float] = {}
    skipped: List[str] = []
    for pos in prev_open:
        sym = pos["symbol"]
        px = prices.get(sym)
        if px is None:
            skipped.append(sym)
            continue                            # stay open — no fill data
        pnl = float(pos["qty"]) * (px - float(pos["entry_px"])) \
            * _sign(pos["side"])
        ledger.position_close(int(pos["id"]), px, entry_ms, pnl, "rebalance")
        realized += pnl
        fills_used[sym] = px
        prev_w[sym] = signed_weight(pos)        # weight carried into turnover

    # legs that stayed open still count in the prev book for turnover
    for pos in prev_open:
        if pos["symbol"] in skipped:
            prev_w[pos["symbol"]] = signed_weight(pos)

    opened: List[str] = []
    for sym, w in sorted(targets.items()):
        px = prices.get(sym)
        side = "long" if w > 0 else "short"
        if px is None:
            skipped.append(sym)
            continue
        qty = abs(w) * _NOTIONAL / px
        ledger.position_insert({
            "rebalance_id": rebalance_row_id, "arm": arm, "symbol": sym,
            "side": side, "weight": abs(w), "qty": qty,
            "entry_px": px, "entry_ms": entry_ms})
        opened.append(sym)
        fills_used[sym] = px

    tover = turnover_usd(prev_w, targets)
    cost = tover * spec.COST_BPS_SIDE / 1e4
    _bump(ledger, arm, "realized", realized)
    _bump(ledger, arm, "costs", cost)
    return {"realized": realized, "turnover_usd": tover, "cost_usd": cost,
            "opened": opened, "skipped_no_fill": skipped,
            "fills": fills_used}


def unrealized(ledger: ResmomLedger, arm: str,
               mids: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
    tot = 0.0
    legs: Dict[str, float] = {}
    for pos in ledger.positions_open(arm):
        mid = mids.get(pos["symbol"])
        if mid is None:
            continue
        pnl = float(pos["qty"]) * (mid - float(pos["entry_px"])) \
            * _sign(pos["side"])
        tot += pnl
        legs[pos["symbol"]] = round(pnl, 4)
    return tot, legs


def nav_now(ledger: ResmomLedger, arm: str,
            mids: Optional[Dict[str, float]] = None) -> Tuple[float, Dict[str, float]]:
    unrl, legs = unrealized(ledger, arm, mids or {})
    nav = (_NOTIONAL + cum(ledger, arm, "realized")
           + cum(ledger, arm, "funding")
           - cum(ledger, arm, "costs") + unrl)
    return nav, legs


def mark_nav(ledger: ResmomLedger, arm: str, ts_ms: int,
             mids: Dict[str, float]) -> Dict[str, Any]:
    """Insert a resmom_nav mark (+ resmom_marks rows), return the row."""
    nav, legs = nav_now(ledger, arm, mids)
    for sym, mid in mids.items():
        ledger.mark_insert(ts_ms, arm, sym, mid)
    peak = max(ledger.nav_peak(arm), nav)
    dd = nav / peak - 1.0 if peak > 0 else 0.0
    n_pos = ledger.count("resmom_positions",
                         "WHERE arm='%s' AND status='open'" % arm)
    gross = sum(abs(p["weight"]) for p in ledger.positions_open(arm)) \
        * _NOTIONAL
    ledger.nav_insert(ts_ms, arm, nav, gross, n_pos, dd, legs)
    return {"arm": arm, "nav": nav, "dd": dd, "n": n_pos}


def accrue_funding(ledger: ResmomLedger, arm: str, ts_ms: int,
                   events_by_sym: Dict[str, List[Tuple[int, float]]],
                   mids: Dict[str, float]) -> Dict[str, float]:
    """Apply new funding events to open legs. Returns pnl per symbol.

    events_by_sym: symbol -> [(event_ms, rate)] — events already applied
    are skipped via resmom_funding's (arm, symbol, event_ms) ledger.
    """
    out: Dict[str, float] = {}
    for pos in ledger.positions_open(arm):
        sym = pos["symbol"]
        applied = ledger.funding_applied_events(arm, sym)
        for ev_ms, rate in events_by_sym.get(sym, []):
            if ev_ms in applied or ev_ms <= int(pos["entry_ms"]):
                continue
            mid = mids.get(sym)
            if mid is None:
                continue
            notional = abs(float(pos["qty"])) * mid
            pnl = -rate * notional * _sign(pos["side"])
            ledger.funding_insert(ts_ms, ev_ms, arm, sym, rate, notional, pnl)
            ledger.position_add_funding(int(pos["id"]), pnl)
            _bump(ledger, arm, "funding", pnl)
            out[sym] = out.get(sym, 0.0) + pnl
    return out


def check_kill(ledger: ResmomLedger, arm: str, ts_ms: int) -> Optional[Dict]:
    """Descriptive: flag net dd <= KILL_DD on arm A (prereg kill)."""
    if arm != "A":
        return None
    series = ledger.nav_series("A")
    if not series:
        return None
    nav = float(series[-1]["nav_usd"])
    peak = max(float(r["nav_usd"]) for r in series)
    dd = nav / peak - 1.0 if peak > 0 else 0.0
    if dd <= spec.KILL_DD and ledger.meta_get("kill_triggered") is None:
        ledger.meta_set("kill_triggered", json.dumps(
            {"ts_ms": ts_ms, "dd": dd, "nav": nav, "peak": peak}))
        ledger.log("kill_flag", {"dd": dd, "nav": nav, "peak": peak},
                   ts_ms=ts_ms)
        return {"dd": dd, "nav": nav, "peak": peak}
    return None
