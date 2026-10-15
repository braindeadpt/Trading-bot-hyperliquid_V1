"""Boros markets fetcher + universe parsing (prereg §Phase-1.1).

Source: GET https://api.boros.finance/open-api/v1/markets (paginated
with skip). Real shapes verified 2026-10-15:
- payload is {"results": [...], "total": N, "skip": s}
- imData.name e.g. "Hyperliquid BTC 30 Oct 2026",
  "Binance ETHUSDT 30 Oct 2026",
  "Hyperliquid ETH - Gate ETHUSDT 27 Nov 2026" (spread)
- imData.maturity is unix seconds
- data.bestBid/bestAsk/floatingApr are decimal APR (0.075 = 7.5%/yr)
- config.takerFee / extConfig.settleFeeRate are scaled by 1e18
- metadata.platformName is the venue ("Hyperliquid", "Binance",
  "HL-GATE" on spread markets); metadata.assetSymbol is the asset.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.boros_shadow import spec

HTTP_TIMEOUT_S = 20
_UA = {"User-Agent": "boros-shadow-research",
       "Content-Type": "application/json"}   # HL info POST needs JSON CT


def fetch_json(url: str, body: Optional[bytes] = None) -> Any:
    # audit-ok: AUDIT-003 — public read-only market data, see SECURITY.md §2.4
    req = urllib.request.Request(url, data=body, headers=_UA)
    # audit-ok: AUDIT-003 — declared prereg endpoints; module-constant URLs
    return json.loads(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S)
                      .read().decode())


def sha256_payload(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def fetch_boros_markets(fetch_fn=None) -> List[Dict[str, Any]]:
    """All market rows, paginated by `skip` until an empty page."""
    fetch_fn = fetch_fn or fetch_json
    rows: List[Dict[str, Any]] = []
    skip = 0
    while True:
        url = (f"{spec.BOROS_MARKETS}?skip={skip}"
               f"&limit={spec.BOROS_PAGE_LIMIT}")
        page = fetch_fn(url)
        batch = page.get("results", []) if isinstance(page, dict) else page
        if not batch:
            break
        rows.extend(batch)
        skip += spec.BOROS_PAGE_LIMIT
        if len(batch) < spec.BOROS_PAGE_LIMIT:
            break
    return rows


@dataclass
class BorosMarket:
    market_id: int
    name: str
    asset: str                # metadata.assetSymbol, upper
    venue: str                # metadata.platformName, lower ("hl-gate" on spreads)
    venue_b: str              # "" for plain YU; the B venue on spread markets
    maturity_ms: int
    is_spread: bool
    best_bid: float           # decimal APR
    best_ask: float
    floating_apr: Optional[float]
    notional_oi: Optional[float]
    taker_fee: float          # already /1e18
    settle_fee: float         # already /1e18
    state: str


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _venue_b_of(venue: str, name: str) -> str:
    """Spread markets carry platformName like 'HL-GATE' -> 'gate'."""
    if venue.startswith("hl-"):
        return venue.split("-", 1)[1]
    # fallback: second leg of 'Hyperliquid X - Venue Y'
    if " - " in name:
        leg2 = name.split(" - ", 1)[1].split(" ", 1)[0].lower()
        for v in spec.B_VENUES:
            if leg2.startswith(v):
                return v
    return ""


def parse_markets(rows: List[Dict[str, Any]]) -> List[BorosMarket]:
    """Universe filter (§Phase-1.1): keep markets with a Hyperliquid leg
    (name starts 'Hyperliquid ' — plain YU or 'Hyperliquid X - Venue Y'
    spreads) plus the plain YU markets of the B venues. Everything else
    is dropped before evaluation."""
    out: List[BorosMarket] = []
    for r in rows:
        im = r.get("imData") or {}
        md = r.get("metadata") or {}
        name = str(im.get("name") or "")
        venue = str(md.get("platformName") or "").lower()
        is_spread = bool(md.get("isSpreadMarket"))
        is_hl_leg = name.startswith(spec.HL_PREFIX)
        is_b_yu = venue in spec.B_VENUES and not is_spread
        if not (is_hl_leg or is_b_yu):
            continue
        try:
            maturity_ms = int(float(im["maturity"]) * 1000)
            market_id = int(r["marketId"])
        except (KeyError, TypeError, ValueError):
            continue
        data = r.get("data") or {}
        cfg = r.get("config") or {}
        ext = r.get("extConfig") or {}
        out.append(BorosMarket(
            market_id=market_id, name=name,
            asset=str(md.get("assetSymbol") or "").upper(),
            venue=venue,
            venue_b=_venue_b_of(venue, name) if is_spread else "",
            maturity_ms=maturity_ms,
            is_spread=is_spread,
            best_bid=_f(data.get("bestBid")) or 0.0,
            best_ask=_f(data.get("bestAsk")) or 0.0,
            floating_apr=_f(data.get("floatingApr")),
            notional_oi=_f(data.get("notionalOI")),
            taker_fee=(_f(cfg.get("takerFee")) or 0.0) / 1e18,
            settle_fee=(_f(ext.get("settleFeeRate")) or 0.0) / 1e18,
            state=str(r.get("state") or "")))
    return out
