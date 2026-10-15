"""Trade close + realized-PnL accounting (prereg Phase-2.4-2.5).

Closes happen on the FIRST on-schedule pass at/after maturity (the YU
expires free; perps pay real taker+VWAP of that book), or immediately
on a liquidation / delisted-market anomaly. Realized PnL is decomposed
per component and written to boros_close — sealed until the read gate.

Components (prereg):
  fixed    = locked fixed rate x N x T_held
  floating = perp funding received + Boros floating paid (signed sum;
             venue_proxy intervals cancel to 0 by construction)
  basis    = (dP_long - dP_short) x qty  — entry VWAP -> exit VWAP
  costs    = real entry + exit perp costs + Boros fees pro-rata
  realized = fixed + floating + basis - costs
  locked   = net-apy-at-entry x capital x T_planned (Phase-1 record)
  ratio    = realized / locked
cluster_id: same asset with overlapping [entry, close] windows share a
cluster (union-find over all closed trades, deterministic id = min
trade_id of the component).
"""
from __future__ import annotations

import json
import statistics
from typing import Any, Dict, List, Optional

from src.boros_shadow import spec
from src.boros_shadow.books import book_mid, vwap_slip_bps
from src.boros_shadow.marks import trade_legs

CLOSE_MATURITY = "maturity"
CLOSE_LIQ_POOLED = "liquidated_pooled"
CLOSE_LIQ_BOROS = "liquidated_boros"
CLOSE_DELISTED = "delisted"


def _exit_leg(leg: Dict[str, Any], book) -> Optional[Dict[str, float]]:
    """Real taker+VWAP exit for one perp leg on this pass's book.
    None when the book can't price the exit."""
    if book is None:
        return None
    mid = book_mid(book)
    if mid is None or mid <= 0:
        return None
    close_side = "buy" if leg["side"] == "sell" else "sell"
    slip = vwap_slip_bps(close_side, spec.NOTIONAL_USD, book, mid)
    if slip is None:
        return None
    px = mid * (1 + slip / 1e4) if close_side == "buy" \
        else mid * (1 - slip / 1e4)
    cost = (spec.TAKER_BPS[leg["venue"]] + slip) / 1e4 * spec.NOTIONAL_USD
    return {"exit_px": px, "exit_mid": mid, "exit_slip_bps": slip,
            "exit_cost": cost, "close_side": close_side}


def _fallback_exit(leg: Dict[str, Any], mid: Optional[float]
                   ) -> Optional[Dict[str, float]]:
    """24h fallback: mid + 2x the leg's ENTRY slip (prereg §4)."""
    if mid is None or mid <= 0:
        return None
    slip = (leg.get("slip_bps") or 0.0) * 2
    close_side = "buy" if leg["side"] == "sell" else "sell"
    px = mid * (1 + slip / 1e4) if close_side == "buy" \
        else mid * (1 - slip / 1e4)
    cost = (spec.TAKER_BPS[leg["venue"]] + slip) / 1e4 * spec.NOTIONAL_USD
    return {"exit_px": px, "exit_mid": mid, "exit_slip_bps": slip,
            "exit_cost": cost, "close_side": close_side}


def _entry_costs(legs: Dict[str, Any]) -> float:
    """Recorded real entry cost: (taker + entry VWAP slip) x N per leg."""
    out = 0.0
    for leg in legs["perp"].values():
        out += ((spec.TAKER_BPS[leg["venue"]] + (leg["slip_bps"] or 0.0))
                / 1e4 * spec.NOTIONAL_USD)
    return out


def _boros_fees(trade: Dict[str, Any], legs: Dict[str, Any]) -> float:
    """Boros taker+settle+slip accrual = stored costs minus the perp
    component (both legs, x2 open+close as Phase-1 estimated)."""
    perp_est = sum(
        (spec.TAKER_BPS[l["venue"]] + (l["slip_bps"] or 0.0))
        / 1e4 * spec.NOTIONAL_USD * 2 for l in legs["perp"].values())
    return max(0.0, (trade["costs_usd"] or 0.0) - perp_est)


def _clusters(closes: List[Dict[str, Any]]) -> Dict[int, int]:
    """Union-find over closed trades: same asset + overlapping
    [entry_ms, close_ms] windows merge. Returns trade_id -> cluster_id
    (min trade_id of the component — deterministic)."""
    parent: Dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in closes:
        parent.setdefault(c["trade_id"], c["trade_id"])
    for i, a in enumerate(closes):
        for b in closes[i + 1:]:
            if a["asset"] != b["asset"]:
                continue
            if min(a["ts_ms"], b["ts_ms"]) >= max(a["entry_ms"],
                                                b["entry_ms"]) - 1:
                pa, pb = find(a["trade_id"]), find(b["trade_id"])
                if pa != pb:
                    parent[max(pa, pb)] = min(pa, pb)
    return {tid: find(tid) for tid in parent}


def close_trade(ledger, trade: Dict[str, Any], books: Dict,
                markets_by_id: Dict, now_ms: int, reason: str,
                mark: Optional[Dict[str, Any]] = None
                ) -> Optional[int]:
    """Close an open trade; returns boros_close id or None when a
    missing book defers a maturity close (retry next on-schedule pass
    within CLOSE_FALLBACK_H, then the mid+2x-slip fallback)."""
    legs = trade_legs(trade)
    funding_net = ledger.funding_sum(trade["id"])
    floating_net = float(mark["floating_usd"]) if mark else 0.0
    held_ms = max(0, now_ms - int(trade["entry_ms"]))
    held_years = held_ms / spec.MS_YEAR

    if reason in (CLOSE_LIQ_POOLED, CLOSE_LIQ_BOROS):
        # prereg: the loss is the dead layer's margin
        margin = (spec.PERP_CAPITAL_FRAC * spec.NOTIONAL_USD
                  if reason == CLOSE_LIQ_POOLED
                  else spec.BOROS_CAPITAL_FRAC * spec.NOTIONAL_USD
                  * len(legs["yu"]))
        return _write_close(ledger, trade, now_ms, reason,
                            realized_usd=-margin, funding_net=funding_net,
                            floating_net=floating_net, basis_usd=0.0,
                            costs_usd=0.0, held_years=held_years,
                            flags={"liquidated_layer": reason})

    exits: Dict[str, Dict[str, float]] = {}
    flags: Dict[str, Any] = {}
    for key, leg in legs["perp"].items():
        book = books.get((leg["venue"], trade["asset"]))
        ex = _exit_leg(leg, book)
        if ex is None:
            if reason == CLOSE_DELISTED:
                mid = book_mid(book) if book else None
                if mid is None and mark:
                    mid = (json.loads(mark["legs_json"])
                           .get(key, {}).get("mid"))
                ex = _fallback_exit(leg, mid)
                flags["close_fallback"] = True
            elif reason == CLOSE_MATURITY:
                over = now_ms - int(trade["maturity_ms"])
                if over <= spec.CLOSE_FALLBACK_H * spec.MS_H:
                    return None                 # retry next on-schedule
                mid = book_mid(book) if book else None
                if mid is None and mark:
                    mid = (json.loads(mark["legs_json"])
                           .get(key, {}).get("mid"))
                ex = _fallback_exit(leg, mid)
                flags["close_fallback"] = True
        if ex is None:
            return None                         # nothing to price on
        exits[key] = ex

    if mark and mark.get("data_gap"):
        flags["data_gap"] = True

    qty = {k: spec.NOTIONAL_USD / (l["entry_px"] or 1.0)
           for k, l in legs["perp"].items()}
    basis = 0.0
    for key, leg in legs["perp"].items():
        diff = exits[key]["exit_px"] - (leg["entry_px"] or 0.0)
        basis += (diff if leg["side"] == "buy" else -diff) * qty[key]

    entry_costs = _entry_costs(legs)
    exit_costs = sum(e["exit_cost"] for e in exits.values())
    boros_fees = _boros_fees(trade, legs)
    costs = entry_costs + exit_costs + boros_fees

    t_eff_end = min(now_ms, int(trade["maturity_ms"]))
    t_held = max(0.0, (t_eff_end - int(trade["entry_ms"])) / spec.MS_YEAR)
    fixed = (trade["fixed_bruto"] or 0.0) * spec.NOTIONAL_USD * t_held
    realized = fixed + funding_net + floating_net + basis - costs

    exit_json = {k: {"exit_px": e["exit_px"], "exit_mid": e["exit_mid"],
                     "exit_slip_bps": e["exit_slip_bps"],
                     "close_side": e["close_side"]}
                 for k, e in exits.items()}
    flags["exit"] = exit_json
    return _write_close(ledger, trade, now_ms, reason,
                        realized_usd=realized, funding_net=funding_net,
                        floating_net=floating_net, basis_usd=basis,
                        costs_usd=costs, held_years=held_years,
                        fixed_usd=fixed, flags=flags)


def _write_close(ledger, trade: Dict[str, Any], now_ms: int,
                 reason: str, realized_usd: float, funding_net: float,
                 floating_net: float, basis_usd: float, costs_usd: float,
                 held_years: float, fixed_usd: float = 0.0,
                 flags: Optional[Dict] = None) -> int:
    capital = trade["capital_usd"] or 1.0
    realized_apy = (realized_usd / capital / held_years
                    if held_years > 0 else 0.0)
    t_plan = (int(trade["maturity_ms"]) - int(trade["entry_ms"])) \
        / spec.MS_YEAR
    locked = (trade["net_apy"] or 0.0) * capital * t_plan
    ratio = realized_usd / locked if locked > 0 else 0.0
    liquidated = reason in (CLOSE_LIQ_POOLED, CLOSE_LIQ_BOROS)
    cid = ledger.close_insert({
        "trade_id": trade["id"], "ts_ms": now_ms, "reason": reason,
        "flags_json": json.dumps(flags or {}, default=str),
        "fixed_usd": fixed_usd, "floating_usd": floating_net,
        "basis_usd": basis_usd, "costs_usd": costs_usd,
        "realized_usd": realized_usd, "realized_apy_cap": realized_apy,
        "locked_usd": locked, "ratio": ratio, "cluster_id": None})
    ledger.trade_update(trade["id"], state=(
        "liquidated" if liquidated else "closed"))
    _recluster(ledger)
    ledger.log("close", {"reason": reason, "asset": trade["asset"],
                         "venue_b": trade["venue_b"]},
               trade["id"], now_ms)
    return cid


def _recluster(ledger) -> None:
    """Re-derive cluster ids over all closed/liquidated trades."""
    closes = ledger.closes()
    for tid, cluster in _clusters(closes).items():
        ledger._con.execute(
            "UPDATE boros_close SET cluster_id=? WHERE trade_id=?",
            (cluster, tid))
    ledger._con.commit()


def n5_kill(ledger) -> bool:
    """Early-KILL (prereg): at >=5 closed trades, flag n5_kill when the
    internal median realized/locked < 0.5. Exposed as a boolean only."""
    closed = ledger.count("boros_close")
    if closed < spec.N5_CLOSED:
        return False
    if ledger.flag_get("n5_kill"):
        return True
    ratios = [r["ratio"] for r in ledger.closes() if r["ratio"] is not None]
    if len(ratios) < spec.N5_CLOSED:
        return False
    if statistics.median(ratios) < spec.N5_KILL_MEDIAN:
        return True
    return False


def closed_clusters(ledger) -> int:
    return len({c["cluster_id"] for c in ledger.closes()
                if c["cluster_id"] is not None})
