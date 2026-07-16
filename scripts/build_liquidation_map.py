"""CLI: build Hyperliquid-native liquidation map (research-only).

Dry-run is the default — no network unless ``--execute`` is passed.
Persists snapshots only to the research DB (never data/live/).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase
from src.exchanges.hyperliquid_rest import HyperliquidRESTClient
from src.research.liquidation_map import (
    build_zones,
    fetch_positions,
    format_confluence_summary,
    harvest_addresses_from_files,
    persist_snapshot,
    summarize_zone_confluence,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_COINS = ("BTC", "ETH", "SOL", "HYPE")


def _parse_coins(raw: Optional[str]) -> List[str]:
    if not raw:
        return list(DEFAULT_COINS)
    return [c.strip().upper() for c in raw.split(",") if c.strip()]


def _parse_addresses(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def _load_fills_paths(paths: Sequence[str]) -> List[Any]:
    """Return list of Path objects for harvest (supports .lz4 and plain NDJSON)."""
    out: List[Path] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(f"Fills file not found: {path}")
        out.append(path)
    return out


def _print_zones(zones: Sequence[Any], *, top_per_coin: int = 10) -> None:
    by_coin: Dict[str, List[Any]] = {}
    for z in zones:
        by_coin.setdefault(z.coin, []).append(z)
    if not by_coin:
        print("\n(no zones above min notional)\n")
        return
    for coin in sorted(by_coin):
        rows = by_coin[coin][:top_per_coin]
        print(f"\n=== {coin} — top {len(rows)} zones ===")
        print(
            f"{'side':6} {'price_low':>12} {'price_high':>12} "
            f"{'notional_$':>14} {'#pos':>6} {'dist%':>8}"
        )
        for z in rows:
            print(
                f"{z.side:6} {z.price_low:12.4f} {z.price_high:12.4f} "
                f"{z.total_notional_usd:14,.0f} {z.position_count:6d} "
                f"{z.distance_pct_from_mark:8.3f}"
            )


async def _run_execute(
    addresses: List[str],
    *,
    coins: List[str],
    delay_ms: int,
    max_addresses: int,
    bucket_pct: float,
    min_zone_notional_usd: float,
    max_distance_pct: Optional[float],
    min_position_count: int,
    db_path: Path,
) -> Dict[str, Any]:
    logger.info(
        "EXECUTE: fetching clearinghouseState for %d addresses (delay_ms=%d)",
        len(addresses),
        delay_ms,
    )
    async with HyperliquidRESTClient() as client:
        mids = await client.all_mids()
        mark_prices = {c: float(mids[c]) for c in coins if c in mids}
        fetch_result = await fetch_positions(
            addresses,
            client=client,
            delay_ms=delay_ms,
            max_addresses=max_addresses,
            coins=coins,
        )
    # Unfiltered candidates for confluence stats, then filtered for persist/print.
    candidates = build_zones(
        fetch_result.positions,
        bucket_pct=bucket_pct,
        min_zone_notional_usd=min_zone_notional_usd,
        mark_prices=mark_prices,
        max_distance_pct=None,
        min_position_count=1,
    )
    zones = build_zones(
        fetch_result.positions,
        bucket_pct=bucket_pct,
        min_zone_notional_usd=min_zone_notional_usd,
        mark_prices=mark_prices,
        max_distance_pct=max_distance_pct,
        min_position_count=min_position_count,
    )
    conf_rows = summarize_zone_confluence(
        candidates,
        max_distance_pct=max_distance_pct,
        min_position_count=min_position_count,
    )
    print(format_confluence_summary(conf_rows))

    db = ResearchDatabase(db_path)
    try:
        sid = persist_snapshot(
            db,
            zones,
            {
                "addresses_queried": fetch_result.addresses_queried,
                "positions": len(fetch_result.positions),
                "errors": len(fetch_result.errors),
                "coins": coins,
                "max_distance_pct": max_distance_pct,
                "min_position_count": min_position_count,
            },
        )
    finally:
        db.close()
    _print_zones(zones)
    return {
        "snapshot_id": sid,
        "zones": len(zones),
        "positions": len(fetch_result.positions),
        "errors": fetch_result.errors,
        "api_calls": fetch_result.addresses_queried,
        "confluence": conf_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build HL liquidation map (research)")
    parser.add_argument("--from-fills", nargs="+", default=None, help="node_fills archive path(s)")
    parser.add_argument("--addresses", default=None, help="Comma-separated 0x addresses")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Perform real info API fetches (overrides dry-run)",
    )
    parser.add_argument("--coins", default=",".join(DEFAULT_COINS))
    parser.add_argument("--top-n", type=int, default=200)
    parser.add_argument("--min-notional-usd", type=float, default=50_000.0)
    parser.add_argument("--bucket-pct", type=float, default=0.25)
    parser.add_argument("--min-zone-notional-usd", type=float, default=100_000.0)
    parser.add_argument(
        "--max-distance-pct",
        type=float,
        default=50.0,
        help="Drop zones farther than this %% from mark (default 50)",
    )
    parser.add_argument(
        "--min-position-count",
        type=int,
        default=1,
        help="Require at least N positions per zone (default 1 = no-op)",
    )
    parser.add_argument("--delay-ms", type=int, default=150)
    parser.add_argument("--max-addresses", type=int, default=300)
    parser.add_argument(
        "--research-db",
        default=str(ROOT / "data" / "research" / "hyperliquid.db"),
    )
    args = parser.parse_args()

    dry_run = not bool(args.execute)
    coins = _parse_coins(args.coins)
    addresses = _parse_addresses(args.addresses)

    if args.from_fills:
        paths = _load_fills_paths(args.from_fills)
        logger.info("Harvesting addresses from %d fills file(s)...", len(paths))
        addresses = harvest_addresses_from_files(
            paths,
            top_n=args.top_n,
            min_notional_usd=args.min_notional_usd,
            coins=coins,
        )
        logger.info("Harvested %d addresses (top_n=%d)", len(addresses), args.top_n)
    elif not addresses:
        logger.error("Provide --from-fills PATH... or --addresses 0x..,0x..")
        return 2

    # Cap preview
    would_call = min(len(addresses), int(args.max_addresses))
    summary = {
        "mode": "dry-run" if dry_run else "execute",
        "address_count": len(addresses),
        "api_calls_would_be_made": would_call,
        "coins": coins,
        "top_n": args.top_n,
        "bucket_pct": args.bucket_pct,
        "max_distance_pct": args.max_distance_pct,
        "min_position_count": args.min_position_count,
        "delay_ms": args.delay_ms,
        "max_addresses": args.max_addresses,
        "sample_addresses": addresses[:5],
    }
    print(json.dumps(summary, indent=2))

    if dry_run:
        logger.info(
            "DRY-RUN: no network calls. Pass --execute to fetch clearinghouseState "
            "for %d addresses.",
            would_call,
        )
        return 0

    result = asyncio.run(
        _run_execute(
            addresses,
            coins=coins,
            delay_ms=args.delay_ms,
            max_addresses=args.max_addresses,
            bucket_pct=args.bucket_pct,
            min_zone_notional_usd=args.min_zone_notional_usd,
            max_distance_pct=args.max_distance_pct,
            min_position_count=args.min_position_count,
            db_path=Path(args.research_db),
        ),
    )
    print(json.dumps({k: result[k] for k in ("snapshot_id", "zones", "positions", "api_calls")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
