"""Per-pass marking of open trades (prereg Phase-2.1-2.2).

Runs on EVERY snapshot pass (descriptive and on-schedule alike — the
most conservative sampling of trajectory risk). Each mark stores perp
mids + exit VWAPs, YU mids, cumulative funding/floating cash, the
pooled-equity input and staleness flags — enough to replay liquidation
checks offline. Missing books carry the last known mid (`stale`); legs
stale >6h flip `data_gap` (descriptive).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from src.boros_shadow import risk, spec
from src.boros_shadow.books import book_mid
from src.boros_shadow.boros_client import BorosMarket

FetchFn = Callable[..., Any]

# direction -> yu entry-rate field on each venue's leg (simple) / spread
_YU_ENTRY = {"sell": "bid", "buy": "ask"}


def trade_legs(trade: Dict[str, Any]) -> Dict[str, Any]:
    """prices_json -> normalized legs:
    perp: {'hl': {venue:'hyperliquid',side,entry_px,entry_mid,slip_bps},
           'b':  {venue: venue_b, ...}}
    yu:   [{'key','market_id','side','entry_rate','underlying': [perp keys]}]
    """
    legs = json.loads(trade["prices_json"] or "{}")
    perp = {
        "hl": {"venue": "hyperliquid", "side": legs.get("hl_perp_side"),
               "entry_px": legs.get("hl_perp_px"),
               "entry_mid": legs.get("hl_perp_mid"),
               "slip_bps": legs.get("slip_hl_bps")},
        "b": {"venue": trade["venue_b"], "side": legs.get("b_perp_side"),
              "entry_px": legs.get("b_perp_px"),
              "entry_mid": legs.get("b_perp_mid"),
              "slip_bps": legs.get("slip_b_bps")},
    }
    yu: List[Dict[str, Any]] = []
    if trade["structure"] == "simple":
        for key, idf in (("hl", "hl_yu"), ("b", "b_yu")):
            side = perp[key]["side"]
            entry = legs.get(f"{key}_{_YU_ENTRY.get(side, 'bid')}")
            yu.append({"key": key, "market_id": legs.get(idf),
                       "side": side, "entry_rate": entry,
                       "underlying": [key]})
    else:  # spread — one Boros leg floating the venue differential
        side = "sell" if trade["direction"] == "short_spread" else "buy"
        entry = legs.get("sp_bid" if side == "sell" else "sp_ask")
        yu.append({"key": "spread", "market_id": legs.get("spread"),
                   "side": side, "entry_rate": entry,
                   "underlying": ["hl", "b"]})
    return {"perp": perp, "yu": yu}


def _funding_cash(side: str, rate: float) -> float:
    """Perp funding cash for one event: shorts receive when rate>0."""
    return (1.0 if side == "sell" else -1.0) * rate * spec.NOTIONAL_USD


def _floating_cash(yu_side: str, fl_rate: float, dt_years: float) -> float:
    """Boros floating cash: long YU receives, short YU pays."""
    return ((1.0 if yu_side == "buy" else -1.0)
            * fl_rate * spec.NOTIONAL_USD * dt_years)


def _mark_perp_leg(leg: Dict[str, Any], book, prev: Dict[str, Any],
                   now_ms: int) -> Dict[str, Any]:
    out = {"venue": leg["venue"], "side": leg["side"],
           "entry_px": leg["entry_px"], "qty": risk.leg_qty(
               leg["entry_px"] or 0.0)}
    if book is not None:
        mid = book_mid(book)
        close_side = "buy" if leg["side"] == "sell" else "sell"
        slip = risk.exit_slip(close_side, book, mid)
        out.update(mid=mid, exit_px=_exec_px(mid, close_side, slip),
                   exit_slip_bps=slip, stale=0, stale_since=None)
    else:                                       # carry last known
        out.update(mid=prev.get("mid"), exit_px=prev.get("exit_px"),
                   exit_slip_bps=prev.get("exit_slip_bps"), stale=1,
                   stale_since=prev.get("stale_since") or now_ms)
    stale_h = ((now_ms - out["stale_since"]) / spec.MS_H
               if out["stale_since"] else 0.0)
    out["data_gap"] = int(stale_h > spec.STALE_MAX_H)
    adverse = risk.adverse_frac(leg["side"], leg["entry_px"] or 0.0,
                              out["mid"] or 0.0)
    out["adverse_max"] = max(prev.get("adverse_max") or 0.0, adverse)
    return out


def _exec_px(mid: Optional[float], side: str,
             slip_bps: Optional[float]) -> Optional[float]:
    if mid is None or slip_bps is None:
        return mid
    return mid * (1 + slip_bps / 1e4) if side == "buy" \
        else mid * (1 - slip_bps / 1e4)


def _mark_yu_leg(yl: Dict[str, Any], row: Optional[BorosMarket],
                 prev: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    out = {"market_id": yl["market_id"], "side": yl["side"],
           "entry_rate": yl["entry_rate"], "underlying": yl["underlying"]}
    alive = row is not None and row.state == "Normal"
    if alive:
        out.update(mid_rate=(row.best_bid + row.best_ask) / 2.0,
                   floating_apr=row.floating_apr, stale=0,
                   stale_since=None)
    else:
        out.update(mid_rate=prev.get("mid_rate"),
                   floating_apr=prev.get("floating_apr"), stale=1,
                   stale_since=prev.get("stale_since") or now_ms)
    stale_h = ((now_ms - out["stale_since"]) / spec.MS_H
               if out["stale_since"] else 0.0)
    out["data_gap"] = int(stale_h > spec.STALE_MAX_H)
    out["alive"] = int(alive)
    return out


def mark_trade(ledger, trade: Dict[str, Any],
               markets_by_id: Dict[int, BorosMarket],
               books: Dict, now_ms: int, on_schedule: bool,
               funding_fetch: Optional[FetchFn] = None,
               rate_samples: Optional[Dict[str, float]] = None
               ) -> Dict[str, Any]:
    """Build + persist one mark row; returns it for the caller's
    liquidation/close checks. `funding_fetch(venue, asset, t0, t1)` and
    `rate_samples` (Lighter current-rate series, asset -> hourly rate)
    are injectable; the daemon wires the real fetchers.

    Funding/floating measurement contract (prereg Phase-2, amended
    2026-10-15): an interval where a leg's funding cannot be measured
    (venue history failure, or Lighter with no rate sample) contributes
    ZERO mismatch — its funding is proxied to the measured Boros
    floating (`funding_source='venue_proxy'`). Never funding=0 with the
    floating leg still counting."""
    from src.boros_shadow.funding import fetch_funding
    fetch = funding_fetch or fetch_funding
    tid = trade["id"]
    legs = trade_legs(trade)
    prev = ledger.last_mark(tid)
    prev_legs = json.loads(prev["legs_json"]) if prev else {}
    prev_yu = {y["market_id"]: y
               for y in json.loads(prev["yu_json"])["legs"]} if prev else {}
    prev_ts = int(prev["ts_ms"]) if prev else int(trade["entry_ms"])
    dt_years = max(0.0, (now_ms - prev_ts) / spec.MS_YEAR)

    # ── perp legs + funding events (cursor = last stored event ts) ──
    legs_out: Dict[str, Any] = {}
    interval_cash = {"hl": 0.0, "b": 0.0}
    funding_ok = {"hl": False, "b": False}
    for key, leg in legs["perp"].items():
        legs_out[key] = _mark_perp_leg(
            leg, books.get((leg["venue"], trade["asset"])),
            prev_legs.get(key) or {}, now_ms)
        t0 = ledger.funding_cursor(tid, key)
        t0 = int(t0) if t0 is not None else int(trade["entry_ms"])
        if leg["venue"] == "lighter":
            # no public history -> own sampled series, weighted by the
            # pass interval (rate is per-hour like HL's)
            r = (rate_samples or {}).get(trade["asset"])
            if r is not None:
                dt_h = max(0.0, (now_ms - prev_ts) / spec.MS_H)
                cash = _funding_cash(leg["side"], r) * dt_h
                ledger.funding_insert({
                    "trade_id": tid, "leg": key, "venue": "lighter",
                    "ts_ms": now_ms, "rate": r, "cash_usd": cash,
                    "source": "lighter_sampled"})
                interval_cash[key] += cash
                funding_ok[key] = True
                legs_out[key]["funding_source"] = "lighter_sampled"
        else:
            evs = fetch(leg["venue"], trade["asset"], t0, now_ms)
            if evs is not None:
                funding_ok[key] = True
                legs_out[key]["funding_source"] = "venue"
            for ts, rate in evs or []:
                if ts <= t0:
                    continue
                cash = _funding_cash(leg["side"], rate)
                ledger.funding_insert({
                    "trade_id": tid, "leg": key, "venue": leg["venue"],
                    "ts_ms": ts, "rate": rate, "cash_usd": cash,
                    "source": "venue"})
                interval_cash[key] += cash

    # ── Boros legs: mid rate + floating integration + proxies ──
    yu_out: List[Dict[str, Any]] = []
    floating_delta = 0.0
    for yl in legs["yu"]:
        prev_leg = prev_yu.get(yl["market_id"]) or {}
        row = markets_by_id.get(yl["market_id"])
        m = _mark_yu_leg(yl, row, prev_leg, now_ms)
        underlying = yl["underlying"]
        if m["alive"] and row.floating_apr is not None:
            fl = _floating_cash(yl["side"], row.floating_apr, dt_years)
            m["floating_source"] = "boros_floating_apr"
            floating_delta += fl
            failed = [k for k in underlying if not funding_ok[k]]
            if failed:
                # measured floating + unmeasurable funding -> the
                # interval mismatch is 0 by construction (amendment
                # 2026-10-15): failed legs absorb the residual
                resid = fl + sum(interval_cash[k] for k in underlying
                                 if funding_ok[k])
                share = -resid / len(failed)
                for k in failed:
                    ledger.funding_insert({
                        "trade_id": tid, "leg": k,
                        "venue": legs["perp"][k]["venue"],
                        "ts_ms": now_ms, "rate": 0.0,
                        "cash_usd": share, "source": "venue_proxy"})
                    interval_cash[k] += share
                    legs_out[k]["funding_source"] = "venue_proxy"
        else:
            # no floating measurement -> floating = -underlying funding
            # (venue_proxy); a failed leg contributes 0 on both sides
            m["floating_source"] = "venue_proxy"
            for k in underlying:
                if funding_ok[k]:
                    floating_delta += -interval_cash[k]
                else:
                    legs_out[k]["funding_source"] = "venue_proxy"
        yu_out.append(m)

    funding_net = ledger.funding_sum(tid)      # cumulative, all legs
    floating_net = (float(prev["floating_usd"]) if prev else 0.0) \
        + floating_delta

    upnl_hl = risk.perp_upnl(legs_out["hl"]["side"],
                             legs_out["hl"]["entry_px"] or 0.0,
                             legs_out["hl"]["mid"] or 0.0)
    upnl_b = risk.perp_upnl(legs_out["b"]["side"],
                            legs_out["b"]["entry_px"] or 0.0,
                            legs_out["b"]["mid"] or 0.0)
    equity = risk.pooled_equity(upnl_hl, upnl_b, funding_net)
    data_gap = int(any(l["data_gap"] for l in legs_out.values())
                   or any(y["data_gap"] for y in yu_out))

    row = {"trade_id": tid, "ts_ms": now_ms,
           "on_schedule": int(on_schedule),
           "legs_json": json.dumps(legs_out, default=str),
           "yu_json": json.dumps({"legs": yu_out}, default=str),
           "funding_usd": funding_net, "floating_usd": floating_net,
           "equity_pooled": equity, "data_gap": data_gap}
    row["id"] = ledger.mark_insert(row)
    row["upnl"] = {"hl": upnl_hl, "b": upnl_b}
    return row


def liquidation_hits(trade: Dict[str, Any], mark: Dict[str, Any],
                     now_ms: int) -> List[str]:
    """Death checks on the latest mark. Returns the dead layers
    (['pooled'], ['boros'], or both); a trade can die on both at once."""
    legs = json.loads(mark["legs_json"])
    hits: List[str] = []
    if risk.pooled_dead(mark["equity_pooled"]):
        hits.append("pooled")
    t_rem = max(0.0, (int(trade["maturity_ms"]) - now_ms) / spec.MS_YEAR)
    for yl in json.loads(mark["yu_json"])["legs"]:
        if yl.get("mid_rate") is None or yl.get("entry_rate") is None:
            continue
        if risk.boros_dead(risk.yu_mtm(
                yl["side"], yl["entry_rate"], yl["mid_rate"], t_rem)):
            hits.append("boros")
            break
    return hits


def split_flags(trade: Dict[str, Any], mark: Dict[str, Any]) -> List[str]:
    """Descriptive SPLIT flags — legs whose worst adverse excursion has
    reached 30% (would liquidate a 3x isolated account)."""
    legs = json.loads(mark["legs_json"])
    return [k for k, l in legs.items()
            if risk.split_liq(l.get("adverse_max") or 0.0)]
