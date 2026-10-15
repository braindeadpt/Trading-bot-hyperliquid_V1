"""Realized perp funding fetchers (prereg §PnL — funding leg).

Each venue normalizes to a list of (ts_ms, rate) events — rate>0 means
longs pay shorts. `fetch_funding` returns None when the venue has no
public history for the asset (Lighter) or the call fails — the caller
records source='unavailable' and contributes 0 that interval, never a
guessed number. Endpoint shapes verified 2026-10-15 against live APIs.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.boros_shadow import spec
from src.boros_shadow.boros_client import fetch_json

FetchFn = Callable[..., Any]
Event = Tuple[int, float]          # (ts_ms, signed rate)


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _hl(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    out: List[Event] = []
    start = t0
    while True:                    # fundingHistory caps at 500 rows
        d = f(spec.HL_INFO, body=json.dumps(
            {"type": "fundingHistory", "coin": asset,
             "startTime": start, "endTime": t1}).encode())
        rows = [(int(r["time"]), _f(r["fundingRate"])) for r in d or []]
        rows = [(t, r) for t, r in rows
                if r is not None and t0 <= t <= t1]
        out.extend(rows)
        if not d or len(d) < 500 or not rows:
            break
        start = max(t for t, _ in rows) + 1
    return out


def _binance(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    d = f(f"{spec.BINANCE_FUNDING}?symbol={asset}USDT"
          f"&startTime={t0}&endTime={t1}&limit=1000")
    return [(int(r["fundingTime"]), _f(r["fundingRate"]))
            for r in d or [] if _f(r.get("fundingRate")) is not None]


def _gate(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    # gate timestamps are unix SECONDS; no from/to params -> filter
    d = f(f"{spec.GATE_FUNDING}?contract={asset}_USDT&limit=1000")
    return [(int(r["t"]) * 1000, _f(r["r"]))
            for r in d or []
            if _f(r.get("r")) is not None
            and t0 <= int(r["t"]) * 1000 <= t1]


def _okx(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    out: List[Event] = []
    after = ""
    while True:                    # okx paginates with `after` (newest first)
        d = f(f"{spec.OKX_FUNDING}?instId={asset}-USDT-SWAP"
              f"&limit=100{after}")
        rows = (d.get("data") or []) if isinstance(d, dict) else []
        if not rows:
            break
        for r in rows:
            ts = int(r.get("fundingTime") or 0)
            rate = _f(r.get("realizedRate") or r.get("fundingRate"))
            if rate is not None and t0 <= ts <= t1:
                out.append((ts, rate))
        oldest = min(int(r.get("fundingTime") or 0) for r in rows)
        if oldest <= t0 or len(rows) < 100:
            break
        after = f"&after={oldest}"
    return out


def _bybit(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    d = f(f"{spec.BYBIT_FUNDING}?category=linear&symbol={asset}USDT"
          f"&startTime={t0}&endTime={t1}&limit=200")
    res = d.get("result") or {}
    return [(int(r["fundingRateTimestamp"]), _f(r["fundingRate"]))
            for r in res.get("list") or []
            if _f(r.get("fundingRate")) is not None]


def _kucoin(asset: str, t0: int, t1: int, f: FetchFn) -> List[Event]:
    sym = "XBTUSDTM" if asset == "BTC" else f"{asset}USDTM"
    d = f(f"{spec.KUCOIN_FUNDING}?symbol={sym}&from={t0}&to={t1}")
    return [(int(r["timepoint"]), _f(r["fundingRate"]))
            for r in (d.get("data") or [])
            if _f(r.get("fundingRate")) is not None]


_FUNDING = {
    "hyperliquid": _hl,
    "binance": _binance,
    "gate": _gate,
    "okx": _okx,
    "bybit": _bybit,
    "kucoin": _kucoin,
    # lighter: no public funding-history endpoint (2026-10-15) -> None
}


def fetch_lighter_rates(fetch_fn: Optional[FetchFn] = None
                        ) -> Dict[str, float]:
    """Lighter current funding rates, sampled per pass: the public
    `funding-rates` table carries one row per (market_id, exchange);
    exchange='lighter' is the venue's own rate (per-hour convention,
    same regime as HL). Returns {asset: rate} — {} on failure."""
    f = fetch_fn or fetch_json
    try:
        d = f(f"{spec.LIGHTER_RATES}?limit=500")
    except Exception:  # noqa: BLE001
        return {}
    inv = {v: k for k, v in spec.LIGHTER_MARKET_ID.items()}
    out: Dict[str, float] = {}
    for r in (d.get("funding_rates") or []):
        if r.get("exchange") != "lighter":
            continue
        asset = inv.get(r.get("market_id"))
        rate = _f(r.get("rate"))
        if asset and rate is not None:
            out[asset] = rate
    return out


def fetch_funding(venue: str, asset: str, start_ms: int, end_ms: int,
                  fetch_fn: Optional[FetchFn] = None
                  ) -> Optional[List[Event]]:
    """Sorted (ts_ms, rate) events in [start,end]; None when the venue
    lacks a public history or the fetch fails."""
    fn = _FUNDING.get(venue)
    if fn is None or end_ms <= start_ms:
        return None if fn is None else []
    f = fetch_fn or fetch_json
    try:
        ev = fn(asset, start_ms, end_ms, f)
    except Exception:  # noqa: BLE001 — unavailable venue != failed pass
        return None
    ev = [(t, r) for t, r in ev if start_ms <= t <= end_ms and r is not None]
    return sorted(set(ev))
