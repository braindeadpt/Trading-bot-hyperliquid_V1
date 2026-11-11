"""HL + Binance fetchers — public market data only (AUDIT-003 class).

Sync urllib on purpose: the daemon is a weekly REST job like boros; every
fetcher is injectable for tests (`fetch_fn` / constructor `fetch` dict).

HL daily candle shape kept: {"t","o","h","l","c","v"} (t=open ms).
HL 1m candle: same keys — used for the 00:05 UTC fill price (open).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from src.resmom_shadow import spec

FetchFn = Callable[..., Any]
HTTP_TIMEOUT_S = 20
_UA = {"User-Agent": "resmom-shadow-research",
       "Content-Type": "application/json"}


def fetch_json(url: str, body: Optional[bytes] = None) -> Any:
    # audit-ok: AUDIT-003 — public read-only market data, spec-constant URLs
    req = urllib.request.Request(url, data=body, headers=_UA)
    # audit-ok: AUDIT-003 — declared prereg endpoints; module-constant URLs
    return json.loads(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)
                      .read().decode())


def hl_info(payload: Dict[str, Any],
            fetch_fn: Optional[FetchFn] = None) -> Any:
    f = fetch_fn or fetch_json
    return f(spec.HL_INFO, body=json.dumps(payload).encode())


def hl_meta_universe(fetch_fn: Optional[FetchFn] = None) -> List[str]:
    """Main-dex perp names — perpMeta universe entries."""
    d = hl_info({"type": "meta"}, fetch_fn)
    universe = (d or {}).get("universe") or []
    return [str(u.get("name") or "") for u in universe if u.get("name")]


def hl_candles(coin: str, interval: str, start_ms: int, end_ms: int,
               fetch_fn: Optional[FetchFn] = None) -> List[Dict[str, Any]]:
    """candleSnapshot rows -> [{"t","o","h","l","c","v"}] (t=open ms)."""
    d = hl_info({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval,
                        "startTime": start_ms, "endTime": end_ms}},
                fetch_fn)
    out: List[Dict[str, Any]] = []
    for r in d or []:
        try:
            out.append({"t": int(r["t"]), "o": float(r["o"]),
                        "h": float(r["h"]), "l": float(r["l"]),
                        "c": float(r["c"]), "v": float(r["v"])})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: c["t"])


def hl_candles_1d(coin: str, days: int, end_ms: int,
                  fetch_fn: Optional[FetchFn] = None) -> List[Dict[str, Any]]:
    return hl_candles(coin, "1d", end_ms - days * spec.MS_D, end_ms, fetch_fn)


def first_1m_open(coin: str, not_before_ms: int,
                  fetch_fn: Optional[FetchFn] = None) -> Optional[float]:
    """Open of the first 1m candle with t >= not_before_ms (paper fill)."""
    rows = hl_candles(coin, "1m", not_before_ms, not_before_ms + 10 * 60_000,
                      fetch_fn)
    rows = [r for r in rows if r["t"] >= not_before_ms]
    return rows[0]["o"] if rows else None


def hl_funding_events(coin: str, t0: int, t1: int,
                      fetch_fn: Optional[FetchFn] = None
                      ) -> List[tuple]:
    """(ts_ms, rate) realized funding — reuses the pinned boros fetcher."""
    from src.boros_shadow.funding import fetch_funding
    ev = fetch_funding("hyperliquid", coin, t0, t1, fetch_fn=fetch_fn)
    return ev or []


def hl_all_mids(fetch_fn: Optional[FetchFn] = None) -> Dict[str, float]:
    d = hl_info({"type": "allMids"}, fetch_fn)
    out: Dict[str, float] = {}
    for k, v in (d or {}).items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def binance_klines_1d(symbol: str, days: int, end_ms: int,
                      fetch_fn: Optional[FetchFn] = None
                      ) -> List[Dict[str, Any]]:
    """Binance USDT-M daily closes for the descriptive reference + parity.

    Returns the same {"t","o","h","l","c","v"} shape as hl_candles so the
    signal pipeline consumes them identically.
    """
    f = fetch_fn or fetch_json
    start = end_ms - days * spec.MS_D
    url = (f"{spec.BINANCE_KLINES}?symbol={symbol}&interval=1d"
           f"&startTime={start}&endTime={end_ms - 1}&limit=1500")
    rows = f(url)
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        try:
            out.append({"t": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]),
                        "v": float(r[5])})    # r[5] = base vol — keeps c*v = notional
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda c: c["t"])
