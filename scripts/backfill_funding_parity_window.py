"""Backfill + densify funding for parity replay windows.

Binance settlements are 8h; live bot samples ~30s. The replay funding-stale
gate (300s) therefore needs densified forward-filled samples between
settlements — same rate, higher timestamp density (honest: funding is
constant between settlements).

Usage:
  python scripts/backfill_funding_parity_window.py
  python scripts/backfill_funding_parity_window.py --db-path data/live/bot_ruleset_validate.db
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import aiohttp

from src.data.database import Database, FundingRecord
from src.data.funding_backfill import (
    PAGE_SLEEP_SEC,
    PER_REQUEST_TIMEOUT_SEC,
    _download_funding,
    _download_oi,
)
from src.utils.config import load_config

# HYPE listed from ~2026-06-19 only — excluded from May window by default.
DEFAULT_START = "2026-05-16"
DEFAULT_END = "2026-06-13"
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]
# Live W2 median gap ~31s; 60s keeps stale<<300s with fewer rows.
DEFAULT_DENSIFY_SEC = 60


def _ms(day: str, end: bool = False) -> int:
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def densify_forward_fill(
    settlements: Sequence[FundingRecord],
    *,
    start_ms: int,
    end_ms: int,
    step_ms: int,
) -> List[FundingRecord]:
    """Emit step_ms samples carrying the last known settlement rate."""
    if not settlements or step_ms <= 0:
        return list(settlements)
    ordered = sorted(settlements, key=lambda r: int(r.timestamp))
    out: List[FundingRecord] = []
    idx = 0
    cur_rate = float(ordered[0].current)
    pred = float(ordered[0].predicted if ordered[0].predicted is not None else cur_rate)
    sym = ordered[0].symbol
    # Advance idx to first settlement <= start
    while idx + 1 < len(ordered) and int(ordered[idx + 1].timestamp) <= start_ms:
        idx += 1
        cur_rate = float(ordered[idx].current)
        pred = float(
            ordered[idx].predicted if ordered[idx].predicted is not None else cur_rate
        )
        sym = ordered[idx].symbol

    t = start_ms
    # Align to step grid
    t = (t // step_ms) * step_ms
    while t <= end_ms:
        while idx + 1 < len(ordered) and int(ordered[idx + 1].timestamp) <= t:
            idx += 1
            cur_rate = float(ordered[idx].current)
            pred = float(
                ordered[idx].predicted
                if ordered[idx].predicted is not None
                else cur_rate
            )
            sym = ordered[idx].symbol
        if int(ordered[idx].timestamp) <= t:
            out.append(
                FundingRecord(
                    symbol=sym,
                    current=cur_rate,
                    predicted=pred,
                    timestamp=t,
                )
            )
        t += step_ms
    return out


def density_report(db: Database, symbols: Sequence[str], start_ms: int, end_ms: int) -> None:
    print("\n=== Funding density (per symbol) ===")
    for sym in symbols:
        rows = db.get_funding_history(sym, limit=500_000, start_ms=start_ms, end_ms=end_ms)
        ts = sorted(int(r["timestamp"]) for r in rows)
        gaps = [ts[i] - ts[i - 1] for i in range(1, len(ts))] if len(ts) > 1 else []
        gaps_sorted = sorted(gaps)
        p50 = gaps_sorted[len(gaps_sorted) // 2] if gaps_sorted else None
        print(
            f"  {sym}: n={len(ts)} gap_p50_ms={p50} "
            f"gap_max_ms={gaps_sorted[-1] if gaps_sorted else None}"
        )


async def _run(
    db: Database,
    symbols: Sequence[str],
    start_ms: int,
    end_ms: int,
    densify_sec: int,
    oi_period: str,
) -> Tuple[int, int, int]:
    timeout = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT_SEC)
    n_settle = 0
    n_dense = 0
    n_oi = 0
    step_ms = densify_sec * 1000
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for sym in symbols:
            print(f"Downloading Binance funding {sym}...", flush=True)
            settlements = await _download_funding(session, sym, start_ms, end_ms)
            await asyncio.sleep(PAGE_SLEEP_SEC)
            if settlements:
                db.save_funding_batch(list(settlements))
                n_settle += len(settlements)
                print(f"  settlements={len(settlements)}", flush=True)
                if densify_sec > 0:
                    dense = densify_forward_fill(
                        settlements,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        step_ms=step_ms,
                    )
                    # Batch in chunks to keep memory bounded
                    chunk = 5000
                    for i in range(0, len(dense), chunk):
                        db.save_funding_batch(dense[i : i + chunk])
                    n_dense += len(dense)
                    print(f"  densified={len(dense)} @{densify_sec}s", flush=True)
            try:
                oi_rows = await _download_oi(
                    session, sym, start_ms, end_ms, period=oi_period,
                )
                await asyncio.sleep(PAGE_SLEEP_SEC)
                if oi_rows:
                    db.save_oi_batch(oi_rows)
                    n_oi += len(oi_rows)
                    print(f"  oi={len(oi_rows)}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  OI skip {sym}: {exc}", flush=True)
    return n_settle, n_dense, n_oi


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        default="data/live/bot_ruleset_validate.db",
        help="Target DB (default: validate snapshot — does not touch live bot.db)",
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--densify-sec",
        type=int,
        default=DEFAULT_DENSIFY_SEC,
        help="Forward-fill interval seconds (0=settlements only)",
    )
    parser.add_argument("--oi-period", default="1h")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_ms = _ms(args.start)
    end_ms = _ms(args.end, end=True)

    print(f"db={db_path}")
    print(f"window={args.start}..{args.end} symbols={symbols}")
    print(
        "NOTE: HYPE excluded (listed ~2026-06-19; no May history).",
        flush=True,
    )
    print(
        f"densify={args.densify_sec}s (live W2 median gap~31s; stale gate=300s)",
        flush=True,
    )

    db = Database(str(db_path))
    print("\nBEFORE:", flush=True)
    density_report(db, symbols, start_ms, end_ms)

    t0 = time.time()
    n_settle, n_dense, n_oi = asyncio.run(
        _run(db, symbols, start_ms, end_ms, args.densify_sec, args.oi_period)
    )
    print(
        f"\nWrote settlements={n_settle} densified={n_dense} oi={n_oi} "
        f"in {time.time() - t0:.1f}s",
        flush=True,
    )
    print("\nAFTER:", flush=True)
    density_report(db, symbols, start_ms, end_ms)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
