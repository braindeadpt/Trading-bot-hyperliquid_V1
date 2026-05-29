"""Audit market data feeds (HL + CEX funding/OI). Run: python scripts/audit_market_data.py"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.market_data_health import compute_feed_status
from src.exchanges.funding_aggregator import FundingOIAggregator
from src.exchanges.hl_predicted_funding import HyperliquidPredictedFundingClient


async def main() -> int:
    symbols = ["BTC", "ETH", "SOL"]
    min_exchanges = 2
    cz_key = os.environ.get("COINALYZE_API_KEY")
    agg = FundingOIAggregator(coinalyze_key=cz_key)
    hl = HyperliquidPredictedFundingClient()
    exit_code = 0

    print("=== Hyperliquid predictedFundings ===")
    hl_data = await hl.poll(symbols)
    for sym in symbols:
        snap = hl_data.get(sym)
        if not snap:
            print(f"  {sym}: MISSING")
            exit_code = 1
            continue
        print(
            f"  {sym}: HL={snap.predicted_funding_hl} HL_8h={snap.predicted_funding_hl_8h} "
            f"venues={list(snap.venues.keys())} stale={snap.stale}"
        )

    print("\n=== CEX aggregator (8h-normalized, parallel) ===")
    cex = await agg.poll(symbols)
    for sym in symbols:
        row = cex.get(sym)
        if not row:
            print(f"  {sym}: MISSING")
            exit_code = 1
            continue
        bybit = row.by_exchange.get("bybit")
        bybit_oi = bybit.open_interest if bybit else None
        oi_note = f"bybit_OI=${bybit_oi:,.0f}" if bybit_oi else "bybit_OI=MISSING"
        status = compute_feed_status(
            cex_ok=row.exchange_count > 0,
            cex_stale=row.stale,
            cex_exchange_count=row.exchange_count,
            min_exchanges=min_exchanges,
            hl_ok=sym in hl_data and bool(hl_data[sym].venues),
            hl_stale=hl_data[sym].stale if sym in hl_data else True,
        )
        print(
            f"  {sym}: status={status} avg={row.funding_avg} predicted_avg={row.predicted_funding_avg} "
            f"OI=${row.oi_total:,.0f} exchanges={row.exchange_count} "
            f"stale={row.stale} age={row.age_sec:.0f}s "
            f"({','.join(row.by_exchange.keys())}) {oi_note}"
        )
        if status == "red":
            exit_code = 1

    if exit_code:
        print("\nAUDIT FAILED — one or more symbols missing or RED")
    else:
        print("\nAUDIT OK")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
