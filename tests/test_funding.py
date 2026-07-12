import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.exchanges.funding_aggregator import FundingOIAggregator
import pytest

pytestmark = pytest.mark.network


async def _run_funding_poll() -> None:
    key = os.getenv("COINALYZE_API_KEY")
    print(f"Coinalyze key present: {bool(key)}")
    agg = FundingOIAggregator(coinalyze_key=key)
    results = await agg.poll(["BTC", "ETH", "SOL"])
    for sym, data in results.items():
        print(f"\n{sym}:")
        print(f"  Funding avg: {data.funding_avg}")
        print(f"  Funding weighted: {data.funding_weighted}")
        print(f"  Predicted avg: {data.predicted_funding_avg}")
        print(f"  OI total: {data.oi_total}")
        print(f"  Exchanges: {data.exchange_count}")
        for ex, f in data.by_exchange.items():
            print(
                f"    {ex}: funding={f.funding_rate}, "
                f"predicted={f.predicted_funding}, OI={f.open_interest}"
            )


def test_funding_poll() -> None:
    """Live integration smoke — requires network; logic unchanged from manual script."""
    asyncio.run(_run_funding_poll())


if __name__ == "__main__":
    asyncio.run(_run_funding_poll())
