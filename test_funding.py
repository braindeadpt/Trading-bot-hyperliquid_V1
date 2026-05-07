import asyncio
import os
from src.exchanges.funding_aggregator import FundingOIAggregator

async def test():
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
            print(f"    {ex}: funding={f.funding_rate}, predicted={f.predicted_funding}, OI={f.open_interest}")

asyncio.run(test())
