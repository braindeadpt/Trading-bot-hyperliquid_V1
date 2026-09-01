"""CLI: rebuild OHLCV from official Hyperliquid node_trades archives.

Implements the ``node_trades_reconstruction`` fallback prescribed by the
GoldRush support package when GoldRush HyperCore candles diverge from the
official ``hl_candleSnapshot`` feed by a few ticks and local rollup/parsing
cannot explain it (see ``data/research/goldrush_support_package_latest.json``
-> ``node_trades_reconstruction``, and
``src/data/candle_providers/node_trades_rebuild.py``).

Safety default: this script is **dry-run by default**. It lists the exact
S3 object keys that would be needed, an estimated object count, and prints
the access requirements (AWS credentials, ``boto3``, requester-pays cost)
without ever contacting AWS. Pass ``--execute`` to perform a real download
(requires ``pip install boto3`` and configured AWS credentials).

The GoldRush parity gate and its tolerance are unchanged by this script.
Rebuilt data lands under source=hl_node_trades_rebuild and must still pass
its own secondary validation before being used for OOS/tuning — see
docs/NODE_TRADES_REBUILD.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.candle_providers.node_trades_fetcher import DEFAULT_BUCKET, DEFAULT_KEY_TEMPLATE
from src.data.candle_providers.node_trades_rebuild import (
    DEFAULT_SUPPORT_PACKAGE_PATH,
    NodeTradesRebuildError,
    extract_priority_windows,
    load_support_package,
    plan_object_keys,
    rebuild_from_support_package,
)
from src.data.research_database import ResearchDatabase

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


def _print_dry_run(package_path: Path, symbols, interval: str) -> int:
    package = load_support_package(package_path)
    windows = extract_priority_windows(package, symbols=symbols, interval=interval)
    if not windows:
        print("No priority windows found in support package for the requested symbols.")
        return 0

    keys = plan_object_keys(windows)
    print("DRY RUN — no network access performed.\n")
    print(f"Support package: {package_path}")
    print(f"Priority windows ({len(windows)}):")
    for w in windows:
        print(f"  {w.symbol}: [{w.start_ms}, {w.end_ms}] interval={w.interval}")
    print(f"\nS3 objects required ({len(keys)}), bucket={DEFAULT_BUCKET!r} "
          f"template={DEFAULT_KEY_TEMPLATE!r}:")
    for obj in keys:
        print(f"  {obj.uri}")
    print(
        "\nAccess requirements to actually fetch this data:\n"
        "  - AWS account with credentials configured (env vars, ~/.aws/credentials, or IAM role)\n"
        "  - `pip install boto3` (optional dependency, not installed by default)\n"
        "  - Requester-pays S3 transfer costs are billed to your AWS account "
        f"for all {len(keys)} object(s) above\n"
        "  - Re-run with --execute to perform the real download and rebuild"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild OHLCV from official HL node_trades archives (GoldRush fallback)",
    )
    parser.add_argument(
        "--package",
        default=str(DEFAULT_SUPPORT_PACKAGE_PATH),
        help="Path to a goldrush_support_package_*.json file",
    )
    parser.add_argument("--symbols", default=None, help="Comma-separated, e.g. BTC,ETH")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--db", default=None, help="Research DB path (defaults to standard path)")
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="List required S3 objects and access requirements only (default: on)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the real S3 download + rebuild (requires boto3 + AWS credentials)",
    )
    args = parser.parse_args()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    )
    package_path = Path(args.package)

    if not args.execute:
        try:
            return _print_dry_run(package_path, symbols, args.interval)
        except NodeTradesRebuildError as exc:
            logger.error("%s", exc)
            return 2

    # --execute: real download path.
    try:
        from src.data.candle_providers.node_trades_fetcher import S3NodeTradesFetcher
    except Exception as exc:  # noqa: BLE001
        logger.error("%s", exc)
        return 2

    try:
        fetcher = S3NodeTradesFetcher()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    if args.db:
        db = ResearchDatabase(Path(args.db))
    else:
        from src.utils.config import load_config

        db = ResearchDatabase.open(load_config(ROOT / "config" / "settings.yaml"))
    result = rebuild_from_support_package(
        package_path, fetcher, db, symbols=symbols, interval=args.interval,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
