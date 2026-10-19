"""Opportunity math (prereg §Phase-1.3) — pure functions, no I/O.

Structure of the locked spread (per the prereg):
- Simple YU pair: short the YU on the rich venue (receive its fixed
  bid), long the YU on the cheap venue (pay its fixed ask). The floating
  legs are the two venues' realized fundings, neutralized by perp legs
  on both venues (short perp under a short YU, long perp under a long
  YU). Gross fixed = bid_rich - ask_cheap.
- Boros spread market ("Hyperliquid X - Venue Y"): one Boros leg whose
  floating is the funding differential itself; the same two perp legs
  neutralize it. Receive the fixed by selling at bid; when ask < 0,
  buying receives -ask.

All rates are decimal APR. Costs (per leg, all on notional N):
  perps — (taker_bps + vwap_slip_bps)/1e4 * N * 2   (open + close)
  boros — (taker_fee + settle_fee + 0.0001) * N * T_years
Capital = 0.2*N (perp legs pooled) + 0.1*N per Boros leg.
Entry: net-on-capital >= 8%/yr, maturity in [14,90]d, asset in the
whitelist. Nothing here reads the clock implicitly — `now_ms` is an
argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.boros_shadow import spec
from src.boros_shadow.books import book_mid, vwap_slip_bps
from src.boros_shadow.boros_client import BorosMarket

# direction -> perp-leg sides on (hyperliquid, venue_b)
_PERP_SIDES = {
    "short_hl_long_b": ("sell", "buy"),
    "short_b_long_hl": ("buy", "sell"),
    "short_spread": ("sell", "buy"),
    "long_spread": ("buy", "sell"),
}
# perp hedge direction: short YU (pay floating) -> short perp receives
# the same funding; spread pays (HL - B) floating when short.


@dataclass
class Candidate:
    asset: str
    venue_b: str
    maturity_ms: int
    structure: str            # simple|spread
    direction: str
    fixed_bruto: float        # annualized fixed rate received
    legs: Dict = field(default_factory=dict)   # market ids + quotes used
    ttm_days: float = 0.0
    costs_usd: float = 0.0
    capital_usd: float = 0.0
    net_apy: Optional[float] = None
    eligible: bool = False
    block_reason: str = ""


def _dedupe(markets: List[BorosMarket]) -> List[BorosMarket]:
    """Repeated (name, maturity) rows exist (multiple marketIds — e.g.
    two 'Hyperliquid BTC 30 Oct 2026'). Keep the most liquid one
    (max notionalOI) so each leg is a single deterministic quote."""
    best: Dict[Tuple[str, int], BorosMarket] = {}
    for m in markets:
        key = (m.name, m.maturity_ms)
        cur = best.get(key)
        if cur is None or (m.notional_oi or 0) > (cur.notional_oi or 0):
            best[key] = m
    return list(best.values())


def _perp_cost_usd(venue: str, side: str, slip_bps: float) -> float:
    """Taker VIP0 + VWAP slip, x2 for open+close, on notional N."""
    return (spec.TAKER_BPS[venue] + slip_bps) / 1e4 * spec.NOTIONAL_USD * 2


def _boros_cost_usd(m: BorosMarket, t_years: float,
                    has_book: bool = False) -> float:
    """Boros taker + settle fee pro-rata to maturity, plus 1 bp/yr
    slippage per Boros leg — prereg makes the slip conditional on no
    Boros book existing; the public API exposes none, so has_book stays
    False until a book fetcher lands (forward amendment territory)."""
    slip = 0.0 if has_book else spec.BOROS_SLIP_RATE
    rate = m.taker_fee + m.settle_fee + slip
    return rate * spec.NOTIONAL_USD * t_years


def evaluate(markets: List[BorosMarket],
             books: Dict[Tuple[str, str], object],
             now_ms: int) -> List[Candidate]:
    """markets: parsed universe (parse_markets output).
    books: {(venue, asset): (bids, asks)} — missing key = venue
    unavailable. Returns every evaluated candidate (winners +
    recorded rejects)."""

    hl_yu: Dict[Tuple[str, int], BorosMarket] = {}
    b_yu: Dict[Tuple[str, str, int], BorosMarket] = {}
    spread: Dict[Tuple[str, str, int], BorosMarket] = {}
    for m in _dedupe(markets):
        if m.state != "Normal":
            continue
        if m.is_spread:
            spread[(m.asset, m.venue_b, m.maturity_ms)] = m
        elif m.venue == "hyperliquid":
            hl_yu[(m.asset, m.maturity_ms)] = m
        elif m.venue in spec.B_VENUES:
            b_yu[(m.asset, m.venue, m.maturity_ms)] = m

    out: List[Candidate] = []
    keys = set(hl_yu) | {(a, m) for a, _v, m in spread}
    for asset, mat in sorted(keys):
        hl = hl_yu.get((asset, mat))
        ttm_d = (mat - now_ms) / spec.MS_D
        if not (spec.MIN_MATURITY_D <= ttm_d <= spec.MAX_MATURITY_D):
            continue
        for venue_b in spec.B_VENUES:
            b = b_yu.get((asset, venue_b, mat))
            sp = spread.get((asset, venue_b, mat))
            cands = []
            if b is not None and hl is not None:
                cands.append(Candidate(
                    asset, venue_b, mat, "simple", "short_hl_long_b",
                    fixed_bruto=hl.best_bid - b.best_ask,
                    legs={"hl_yu": hl.market_id, "b_yu": b.market_id}))
                cands.append(Candidate(
                    asset, venue_b, mat, "simple", "short_b_long_hl",
                    fixed_bruto=b.best_bid - hl.best_ask,
                    legs={"hl_yu": hl.market_id, "b_yu": b.market_id}))
            if sp is not None:
                cands.append(Candidate(
                    asset, venue_b, mat, "spread", "short_spread",
                    fixed_bruto=sp.best_bid,
                    legs={"spread": sp.market_id}))
                cands.append(Candidate(
                    asset, venue_b, mat, "spread", "long_spread",
                    fixed_bruto=-sp.best_ask,
                    legs={"spread": sp.market_id}))
            for c in cands:
                _price_candidate(c, hl, b, sp, books, now_ms)
            out.extend(cands)
    return out


def _price_candidate(c: Candidate, hl: BorosMarket,
                     b: Optional[BorosMarket], sp: Optional[BorosMarket],
                     books, now_ms: int) -> None:
    """Fill costs/capital/net_apy/eligible on one candidate."""
    c.ttm_days = (c.maturity_ms - now_ms) / spec.MS_D
    t_years = (c.maturity_ms - now_ms) / spec.MS_YEAR
    n_boros = 1 if c.structure == "spread" else 2
    c.capital_usd = (spec.PERP_CAPITAL_FRAC * spec.NOTIONAL_USD
                     + spec.BOROS_CAPITAL_FRAC * spec.NOTIONAL_USD
                     * n_boros)
    hl_side, b_side = _PERP_SIDES[c.direction]

    # books needed: HL perp on the asset + venue-B perp on the asset
    book_hl = books.get(("hyperliquid", c.asset))
    book_b = books.get((c.venue_b, c.asset))
    if book_hl is None or book_b is None:
        c.block_reason = ("book_unavailable:"
                          f"{'hyperliquid' if book_hl is None else c.venue_b}")
        return
    slip_hl = vwap_slip_bps(hl_side, spec.NOTIONAL_USD, book_hl,
                            book_mid(book_hl))
    slip_b = vwap_slip_bps(b_side, spec.NOTIONAL_USD, book_b,
                           book_mid(book_b))
    if slip_hl is None or slip_b is None:
        c.block_reason = "book_thin"      # VWAP for N unfillable
        return

    boros_legs = [sp] if c.structure == "spread" else [hl, b]
    boros_cost = sum(_boros_cost_usd(m, t_years)
                     for m in boros_legs if m is not None)
    perp_cost = (_perp_cost_usd("hyperliquid", hl_side, slip_hl)
                 + _perp_cost_usd(c.venue_b, b_side, slip_b))
    c.costs_usd = perp_cost + boros_cost
    mid_hl = book_mid(book_hl)
    mid_b = book_mid(book_b)
    c.legs.update({
        "hl_perp_side": hl_side, "b_perp_side": b_side,
        # all entry prices (prereg: "guardar o trade com todos os preços")
        "hl_perp_mid": mid_hl, "b_perp_mid": mid_b,
        "hl_perp_px": mid_hl * (1 + slip_hl / 1e4) if hl_side == "buy"
        else mid_hl * (1 - slip_hl / 1e4),
        "b_perp_px": mid_b * (1 + slip_b / 1e4) if b_side == "buy"
        else mid_b * (1 - slip_b / 1e4),
        "slip_hl_bps": slip_hl, "slip_b_bps": slip_b,
        **({"hl_bid": hl.best_bid, "hl_ask": hl.best_ask} if hl else {}),
        **({"b_bid": b.best_bid, "b_ask": b.best_ask} if b else {}),
        **({"sp_bid": sp.best_bid, "sp_ask": sp.best_ask} if sp else {})})

    fixed_usd = c.fixed_bruto * spec.NOTIONAL_USD * t_years
    c.net_apy = ((fixed_usd - c.costs_usd) / c.capital_usd / t_years
                 if t_years > 0 else None)
    if c.asset not in spec.ASSETS:
        # prereg: assets outside {BTC,ETH,HYPE} are descriptive — priced
        # and recorded, never eligible
        c.block_reason = "off_gate_asset"
    elif c.net_apy is not None and c.net_apy >= spec.NET_APY_MIN:
        c.eligible = True
    else:
        c.block_reason = (f"net_apy "
                          f"{c.net_apy:.4f} < {spec.NET_APY_MIN}"
                          if c.net_apy is not None else "ttm_nonpositive")


def needed_books(markets: List[BorosMarket], now_ms: int
                 ) -> List[Tuple[str, str]]:
    """(venue, asset) perp books the current candidates consume — shared
    with the daemon so the book set matches the eval exactly."""
    hl_yu = {(m.asset, m.maturity_ms) for m in markets
             if m.venue == "hyperliquid" and not m.is_spread
             and m.state == "Normal"}
    out = set()
    for m in markets:
        if m.state != "Normal":
            continue
        # off-gate assets are descriptive — still priced, so still need books
        ttm_d = (m.maturity_ms - now_ms) / spec.MS_D
        if not (spec.MIN_MATURITY_D <= ttm_d <= spec.MAX_MATURITY_D):
            continue
        if m.is_spread and m.venue_b:
            out |= {("hyperliquid", m.asset), (m.venue_b, m.asset)}
        elif (m.venue in spec.B_VENUES
                and (m.asset, m.maturity_ms) in hl_yu):
            out |= {("hyperliquid", m.asset), (m.venue, m.asset)}
    return sorted(out)


def pick_winner(cands: List[Candidate]) -> Optional[Candidate]:
    """Winner = the structure/direction with the largest NET return on
    committed capital (conformance note 2026-10-19: 'usa-se o melhor dos
    dois' binds on net-on-capital, not gross fixed — the spread leg pays
    one set of Boros fees on 0.3N, so gross and net can rank
    differently). Only priced candidates compete; an unpriced one can
    never enter."""
    priced = [c for c in cands if c.net_apy is not None]
    return max(priced, key=lambda c: c.net_apy, default=None)
