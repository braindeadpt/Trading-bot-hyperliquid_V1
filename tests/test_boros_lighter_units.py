"""Live unit-pin: Lighter funding-rates are per-8h, not per-hour.

Pins spec.LIGHTER_RATE_PERIOD_H against real data: the sampled Lighter
rate annualized at 1095 periods/year must sit within 30% of the Boros
YU market's floatingApr for the same asset — Boros literally settles
the YU float leg off that index, so the units must agree. Rates drift
intraday; 30% absorbs drift but cleanly separates 1095 (8h) from 8760
(1h), a factor-of-8 error.

Marked `network`: opt-in, excluded from default CI.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.network

TOL = 0.30
PERIODS_PER_YEAR = 8760 / 8  # 1095


@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_lighter_rate_unit_matches_boros_floating_apr(asset: str) -> None:
    from src.boros_shadow import spec
    from src.boros_shadow.boros_client import (
        fetch_boros_markets, parse_markets)
    from src.boros_shadow.funding import fetch_lighter_rates

    assert spec.LIGHTER_RATE_PERIOD_H == 8
    rate = fetch_lighter_rates()[asset]
    derived_apr = rate * (8760 / spec.LIGHTER_RATE_PERIOD_H)

    aprs = [m.floating_apr for m in parse_markets(fetch_boros_markets())
            if m.venue == "lighter" and m.asset == asset
            and not m.is_spread]
    assert aprs, f"no lighter {asset} YU market in Boros universe"
    for ref in aprs:
        assert abs(derived_apr - ref) <= TOL * abs(ref), (
            f"{asset}: sampled rate {rate} -> APR {derived_apr:.4f} vs "
            f"Boros floatingApr {ref:.4f} (> {TOL:.0%} off — the "
            "LIGHTER_RATE_PERIOD_H unit assumption broke)")
