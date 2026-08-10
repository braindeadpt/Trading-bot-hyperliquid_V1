#!/usr/bin/env python3
"""Backfill open-interest history from Bybit into research DB (never bot.db).

Bybit public ``/v5/market/open-interest`` retains ~22 months of 1h OI
(and years of 1d/4h) — enough to resolve the OI family's underpowered
66-day HL-native sample.

Limitation (declared): Bybit linear OI is a **CEX proxy**, not Hyperliquid
native OI. Stored with source/venue metadata accordingly.

Usage:
  python scripts/backfill_oi_bybit_research.py --days 400
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.database import OIRecord
from src.data.research_database import ResearchDatabase
from src.data.series_metadata import SeriesMetadata

BYBIT = "https://api.bybit.com/v5/market/open-interest"
SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
DB_DEFAULT = ROOT / "data" / "research" / "hyperliquid.db"
SLEEP_SEC = 0.12


def _get(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "hl-research-oi-backfill/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_oi_symbol(
    base: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> List[Tuple[int, float]]:
    """Paginate Bybit OI; return (ts_ms, oi) sorted ascending within window."""
    sym = f"{base}USDT"
    out: Dict[int, float] = {}
    cursor: Optional[str] = None
    pages = 0
    while pages < 120:
        q: Dict[str, str] = {
            "category": "linear",
            "symbol": sym,
            "intervalTime": interval,
            "limit": "200",
        }
        if cursor:
            q["cursor"] = cursor
        url = BYBIT + "?" + urllib.parse.urlencode(q)
        body = _get(url)
        if int(body.get("retCode", -1)) != 0:
            raise RuntimeError(f"Bybit {sym}: {body.get('retMsg')}")
        result = body.get("result") or {}
        lst = result.get("list") or []
        if not lst:
            break
        for row in lst:
            ts = int(row["timestamp"])
            if ts < start_ms or ts > end_ms:
                continue
            # Prefer USD notional when present; else base-coin OI (%% delta OK either way)
            raw = row.get("openInterestValue") or row.get("openInterest")
            if raw is None:
                continue
            out[ts] = float(raw)
        cursor = result.get("nextPageCursor")
        pages += 1
        # Stop if oldest on page already before window
        oldest = min(int(r["timestamp"]) for r in lst)
        if oldest < start_ms or not cursor:
            break
        time.sleep(SLEEP_SEC)
    rows = sorted(out.items())
    print(f"  {base}: pages={pages} n={len(rows)}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--days", type=int, default=400, help="Lookback days (1h Bybit ~667d max)")
    ap.add_argument("--interval", default="1h", choices=("1h", "4h", "1d"))
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    args = ap.parse_args()

    if "live" in str(args.db).replace("\\", "/").lower() and "research" not in str(args.db):
        print("REFUSING to write OI backfill into a live/bot path:", args.db, file=sys.stderr)
        return 2

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days) * 86_400_000
    print(
        f"Backfill Bybit OI → {args.db} interval={args.interval} "
        f"days={args.days} symbols={symbols}",
        flush=True,
    )

    db = ResearchDatabase(args.db)
    meta = SeriesMetadata(
        source="bybit_open_interest",
        venue="bybit",
        api_version="v5",
        ingested_at_ms=int(time.time() * 1000),
        quality_flags={
            "proxy": True,
            "note": "CEX linear OI proxy for HL research; not HL-native",
            "interval": args.interval,
        },
    )
    total = 0
    for sym in symbols:
        raw = fetch_oi_symbol(sym, args.interval, start_ms, end_ms)
        records: List[OIRecord] = []
        prev: Optional[float] = None
        for ts, oi in raw:
            delta = (oi - prev) if prev is not None else 0.0
            prev = oi
            records.append(
                OIRecord(symbol=sym, oi_total=oi, oi_delta=delta, timestamp=ts)
            )
        db.save_oi_with_meta(records, meta)
        total += len(records)
        if records:
            d0 = datetime.fromtimestamp(records[0].timestamp / 1000, tz=timezone.utc)
            d1 = datetime.fromtimestamp(records[-1].timestamp / 1000, tz=timezone.utc)
            print(f"    stored {len(records)} {d0.date()} → {d1.date()}")

    print(f"Done. rows={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
