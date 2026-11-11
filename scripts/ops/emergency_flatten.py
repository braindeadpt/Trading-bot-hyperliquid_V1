"""Standalone emergency kill-switch / flatten CLI for Hyperliquid testnet & mainnet.

Operates directly against Hyperliquid via ``HyperliquidLiveClient`` without
needing in-memory access to a running ``TradingEngine`` process:
  1. Arms immediate dead-man's switch (``schedule_cancel(now + 15s)``) as backup.
  2. Cancels all open resting and trigger orders (``cancel_all_orders``).
  3. Market-closes every open perpetual position (``flatten_all_positions``).
  4. Verifies zero open positions (``confirm_flat``) and disarms ``schedule_cancel``.

Usage:
  python scripts/ops/emergency_flatten.py --mode testnet --confirm FLATTEN
  python scripts/ops/emergency_flatten.py --mode mainnet --confirm FLATTEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.exchanges.hyperliquid_live import (  # noqa: E402
    HyperliquidLiveClient,
    resolve_account_address,
    resolve_private_key,
)


async def execute_emergency_flatten(
    *,
    use_testnet: bool,
    client: Any = None,
) -> Dict[str, Any]:
    """Cancel all orders and flatten all open perp positions on Hyperliquid."""
    owns_client = client is None
    if owns_client:
        priv_key = resolve_private_key()
        if not priv_key:
            raise RuntimeError(
                "HYPERLIQUID_PRIVATE_KEY is not configured in env or vault"
            )
        acct_addr = resolve_account_address()
        client = HyperliquidLiveClient(
            priv_key,
            use_testnet=use_testnet,
            account_address=acct_addr,
        )
        await client.open()

    try:
        # 1. Arm dead-man's switch 15s ahead in case this script drops mid-flight
        dms_ts = int(time.time() * 1000) + 15_000
        try:
            await client.schedule_cancel(dms_ts)
        except Exception:  # noqa: BLE001
            pass

        # 2. Cancel all resting and trigger orders
        cancelled_count = await client.cancel_all_orders()

        # 3. Flatten all open perp positions
        flatten_results = await client.flatten_all_positions()

        # 4. Confirm flat
        confirmed_flat = await client.confirm_flat()
        if confirmed_flat:
            try:
                await client.schedule_cancel(None)
            except Exception:  # noqa: BLE001
                pass

        return {
            "mode": "testnet" if use_testnet else "mainnet",
            "wallet": getattr(client, "wallet_address", None),
            "cancelled_orders": int(cancelled_count),
            "flatten_results": flatten_results,
            "confirmed_flat": bool(confirmed_flat),
            "timestamp_ms": int(time.time() * 1000),
        }
    finally:
        if owns_client:
            await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=["testnet", "mainnet"],
        help="Target Hyperliquid environment",
    )
    parser.add_argument(
        "--confirm",
        required=True,
        help="Must pass exact string FLATTEN to execute",
    )
    args = parser.parse_args()

    if args.confirm != "FLATTEN":
        print(
            "[ABORT] Pass --confirm FLATTEN to execute emergency flatten.",
            file=sys.stderr,
        )
        return 2

    report = asyncio.run(
        execute_emergency_flatten(use_testnet=(args.mode == "testnet"))
    )
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("confirmed_flat") else 1


if __name__ == "__main__":
    raise SystemExit(main())
