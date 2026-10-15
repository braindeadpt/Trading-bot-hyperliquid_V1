"""Public perp-L2 fetchers for the hedge legs (prereg §Phase-1.2).

Every fetcher normalizes to (bids, asks) — lists of (price, size) float
tuples, best first. A venue that fails returns None and is marked
`unavailable` for that snapshot (never blocks the others). Endpoint
shapes verified 2026-10-15 against the live public APIs.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.boros_shadow import spec
from src.boros_shadow.boros_client import fetch_json

Book = Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]
FetchFn = Callable[..., Any]


def _pairs(raw: List[Any], pk: str = "px", sk: str = "sz") -> List[Tuple[float, float]]:
    out = []
    for lvl in raw or []:
        if isinstance(lvl, dict):
            px, sz = lvl.get(pk), lvl.get(sk)
        else:
            px, sz = lvl[0], lvl[1]
        try:
            out.append((float(px), float(sz)))
        except (TypeError, ValueError):
            continue
    return out


def _hl_book(asset: str, f: FetchFn) -> Optional[Book]:
    d = f(spec.HL_INFO,
          body=json.dumps({"type": "l2Book", "coin": asset}).encode())
    lv = d.get("levels") or []
    if len(lv) != 2:
        return None
    return _pairs(lv[0]), _pairs(lv[1])


def _binance_book(asset: str, f: FetchFn) -> Optional[Book]:
    d = f(f"{spec.BINANCE_DEPTH}?symbol={asset}USDT"
          f"&limit={spec.BOOK_DEPTH_LIMIT}")
    return _pairs(d.get("bids")), _pairs(d.get("asks"))


def _gate_book(asset: str, f: FetchFn) -> Optional[Book]:
    d = f(f"{spec.GATE_ORDERBOOK}?contract={asset}_USDT"
          f"&limit={spec.BOOK_DEPTH_LIMIT}")
    return _pairs(d.get("bids"), "p", "s"), _pairs(d.get("asks"), "p", "s")


def _okx_book(asset: str, f: FetchFn) -> Optional[Book]:
    d = f(f"{spec.OKX_BOOKS}?instId={asset}-USDT-SWAP"
          f"&sz={spec.BOOK_DEPTH_LIMIT}")
    rows = d.get("data") or []
    if not rows:
        return None
    return _pairs(rows[0].get("bids")), _pairs(rows[0].get("asks"))


def _bybit_book(asset: str, f: FetchFn) -> Optional[Book]:
    d = f(f"{spec.BYBIT_ORDERBOOK}?category=linear&symbol={asset}USDT"
          f"&limit={spec.BOOK_DEPTH_LIMIT}")
    res = d.get("result") or {}
    return _pairs(res.get("b")), _pairs(res.get("a"))


def _kucoin_book(asset: str, f: FetchFn) -> Optional[Book]:
    sym = "XBTUSDTM" if asset == "BTC" else f"{asset}USDTM"
    d = f(f"{spec.KUCOIN_DEPTH20}?symbol={sym}")
    data = d.get("data") or {}
    return _pairs(data.get("bids")), _pairs(data.get("asks"))


def _lighter_book(asset: str, f: FetchFn) -> Optional[Book]:
    mid = spec.LIGHTER_MARKET_ID.get(asset)
    if mid is None:
        return None
    d = f(f"{spec.LIGHTER_ORDERS}?market_id={mid}&limit=100")
    # raw resting orders — aggregate to price levels
    def agg(key: str) -> List[Tuple[float, float]]:
        lv: Dict[float, float] = {}
        for o in d.get(key) or []:
            try:
                px = float(o["price"]); sz = float(o["remaining_base_amount"])
            except (KeyError, TypeError, ValueError):
                continue
            lv[px] = lv.get(px, 0.0) + sz
        return sorted(lv.items(), key=lambda kv: kv[0],
                      reverse=(key == "bids"))[:spec.BOOK_DEPTH_LIMIT]
    return agg("bids"), agg("asks")


_BOOK_FETCHERS = {
    "hyperliquid": _hl_book,
    "binance": _binance_book,
    "gate": _gate_book,
    "okx": _okx_book,
    "bybit": _bybit_book,
    "kucoin": _kucoin_book,
    "lighter": _lighter_book,
}

VENUES = tuple(_BOOK_FETCHERS)


def fetch_perp_book(venue: str, asset: str,
                    fetch_fn: Optional[FetchFn] = None) -> Optional[Book]:
    """Normalized (bids, asks) or None when the venue/asset is
    unavailable this snapshot."""
    f = fetch_fn or fetch_json
    fn = _BOOK_FETCHERS.get(venue)
    if fn is None:
        return None
    try:
        book = fn(asset, f)
    except Exception:  # noqa: BLE001 — one bad venue != a failed snapshot
        return None
    if not book or not book[0] or not book[1]:
        return None
    return book


def book_mid(book: Book) -> Optional[float]:
    bids, asks = book
    if not bids or not asks:
        return None
    return (bids[0][0] + asks[0][0]) / 2.0


def vwap_slip_bps(side: str, notional_usd: float, book: Book,
                  mid: float) -> Optional[float]:
    """VWAP slippage (bps, >=0 = cost) to fill `notional_usd` against the
    adverse side of the book. None when the book can't fill the size —
    the opportunity then fails honestly instead of assuming zero cost."""
    if mid <= 0:
        return None
    levels = book[1] if side == "buy" else book[0]
    filled_usd = 0.0
    filled_units = 0.0
    for px, sz in levels:
        if px <= 0:
            continue
        take_usd = min(px * sz, notional_usd - filled_usd)
        filled_units += take_usd / px
        filled_usd += take_usd
        if filled_usd >= notional_usd - 1e-9:
            vwap = filled_usd / filled_units
            # buy above mid / sell below mid => positive cost
            return abs(vwap - mid) / mid * 1e4
    return None
