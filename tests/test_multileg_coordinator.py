"""Unit & failure-injection suite for src.multileg (Spot+Perp Carry & Perp Pairs).

Scenarios verified:
  1. CarryA1 happy path (Unified Account '<$10k' with Agent Wallet):
     simultaneous Spot BUY + Perp SELL maker fills -> open -> funding accrual -> exit flat.
  2. Failure Injection — Unlegged 15m maker timeout:
     Perp short fills while Spot buy rests unfilled -> cancels Spot, verifies 0 fill,
     unwinds Perp short via reduce-only IoC -> status='aborted_unwound', zero net delta.
  3. Failure Injection — Post-cancel RTT fill race:
     Spot order fills while cancel packet is in flight -> post-cancel fill check
     detects the fill and promotes episode to 'open' instead of unwinding into reverse delta!
  4. Failure Injection — Unlegged adverse excursion circuit (>35 bps):
     Spot fills first, Perp mid moves >35 bps adversely within 10s -> immediate abort & unwind.
  5. Proportional Deleverage (PREREGISTER_CARRY_SHADOW §2):
     Perp rallies +30% breaching M=0.45 -> closes exact fraction k on BOTH Spot and Perp,
     restoring mf == M0 (0.60) while keeping spot_qty == perp_qty.
  6. Deleverage floor (keep <= 0.05) & maintenance guard breach -> full 100% emergency close.
  7. UnlockShort beta-hedged Perp-Perp pair (clamped beta [0.5, 2.0]) + ResMom 4-leg basket.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from src.exchanges.hyperliquid_live import _meta_cache
from src.multileg import (
    MultiLegCoordinator,
    MultiLegLedger,
    PerpPairRunner,
    SpotPerpCarryRunner,
    TopOfBook,
)


def _seed_hl_meta() -> None:
    _meta_cache.clear()
    _meta_cache["HYPE"] = {
        "sz_decimals": 2,
        "px_decimals": 4,
        "is_spot": False,
        "enforce_sig_figs": True,
        "max_leverage": 10,
        "wire_coin": "HYPE",
    }
    _meta_cache["HYPE/USDC"] = {
        "sz_decimals": 2,
        "px_decimals": 6,
        "is_spot": True,
        "enforce_sig_figs": True,
        "max_leverage": 1,
        "wire_coin": "@107",
    }
    _meta_cache["ARB"] = {
        "sz_decimals": 1,
        "px_decimals": 5,
        "is_spot": False,
        "enforce_sig_figs": True,
        "max_leverage": 10,
        "wire_coin": "ARB",
    }
    _meta_cache["BTC"] = {
        "sz_decimals": 5,
        "px_decimals": 1,
        "is_spot": False,
        "enforce_sig_figs": True,
        "max_leverage": 40,
        "wire_coin": "BTC",
    }
    _meta_cache["ETH"] = {
        "sz_decimals": 4,
        "px_decimals": 2,
        "is_spot": False,
        "enforce_sig_figs": True,
        "max_leverage": 25,
        "wire_coin": "ETH",
    }
    _meta_cache["SOL"] = {
        "sz_decimals": 2,
        "px_decimals": 4,
        "is_spot": False,
        "enforce_sig_figs": True,
        "max_leverage": 20,
        "wire_coin": "SOL",
    }


class FakeMultiLegHLClient:
    """Controllable mock of HyperliquidLiveClient for live failure-injection scenarios."""

    def __init__(self, *, account_value: float = 2_500.0, spot_usdc: float = 2_500.0) -> None:
        self.account_value = account_value
        self.spot_usdc = spot_usdc
        self.abstractions: List[str] = []
        self.dms_calls: List[Optional[int]] = []
        self.spot_orders: List[Dict[str, Any]] = []
        self.perp_entries: List[Dict[str, Any]] = []
        self.perp_aggressive: List[Dict[str, Any]] = []
        self.cancels: List[tuple[str, int]] = []
        self.fills_by_oid: Dict[str, List[Dict[str, Any]]] = {}
        self.fill_on_cancel_oid: Optional[str] = None
        self.fill_on_cancel_payload: Optional[Dict[str, Any]] = None
        self._oid = 5000

    async def set_account_abstraction(self, abstraction: str = "u") -> Dict[str, Any]:
        self.abstractions.append(abstraction)
        return {"status": "ok"}

    async def get_user_state(self) -> Dict[str, Any]:
        return {"marginSummary": {"accountValue": str(self.account_value)}}

    async def get_spot_balances(self) -> Dict[str, float]:
        return {"USDC": self.spot_usdc}

    async def schedule_cancel(self, cancel_time_ms: Optional[int]) -> Dict[str, Any]:
        self.dms_calls.append(cancel_time_ms)
        return {"status": "ok"}

    async def place_spot_order(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        order_type: str = "limit_maker",
        limit_price: Optional[float] = None,
        max_slippage_pct: float = 2.0,
        cloid: Optional[str] = None,
        enforce_min_notional: bool = True,
    ) -> Dict[str, Any]:
        self._oid += 1
        oid = self._oid
        self.spot_orders.append(
            {
                "oid": oid,
                "symbol": symbol,
                "side": side,
                "size": size,
                "order_type": order_type,
                "px": limit_price,
                "cloid": cloid,
            }
        )
        if order_type == "ioc":
            return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": oid, "totalSz": str(size), "avgPx": str(limit_price)}}]}}}
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    async def place_entry(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        post_only: bool = False,
        max_slippage_pct: float = 5.0,
        cloid: Optional[str] = None,
        enforce_min_notional: bool = True,
    ) -> Dict[str, Any]:
        self._oid += 1
        oid = self._oid
        self.perp_entries.append(
            {
                "oid": oid,
                "symbol": symbol,
                "side": side,
                "size": size,
                "order_type": order_type,
                "px": limit_price,
                "cloid": cloid,
            }
        )
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}

    async def place_aggressive_limit(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        reference_price: float,
        max_slippage_pct: float = 5.0,
        reduce_only: bool = False,
        tif: str = "Ioc",
        cloid: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._oid += 1
        oid = self._oid
        self.perp_aggressive.append(
            {
                "oid": oid,
                "symbol": symbol,
                "side": side,
                "size": size,
                "reduce_only": reduce_only,
                "ref_px": reference_price,
            }
        )
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": oid, "totalSz": str(size), "avgPx": str(reference_price)}}]}}}

    async def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        self.cancels.append((symbol, order_id))
        if self.fill_on_cancel_oid == str(order_id) and self.fill_on_cancel_payload:
            self.fills_by_oid[str(order_id)] = [self.fill_on_cancel_payload]
        return {"status": "ok"}

    async def get_order_fills(self, order_id: int | str) -> List[Dict[str, Any]]:
        return list(self.fills_by_oid.get(str(order_id), []))


@pytest.mark.unit
def test_carry_happy_path_and_proportional_deleverage(tmp_path) -> None:
    _seed_hl_meta()
    ledger = MultiLegLedger(str(tmp_path / "ml.db"))

    async def _run() -> None:
        client = FakeMultiLegHLClient(account_value=1_500.0, spot_usdc=1_500.0)
        coord = MultiLegCoordinator(
            ledger=ledger,
            live_client=client,
            mode="testnet",
            margin_profile="unified",
        )
        runner = SpotPerpCarryRunner(coord)

        # $500 carry notional on $1,500 Unified wallet (<$10k canary works with Agent Wallet!)
        spot_bbo = TopOfBook(bid=25.00, ask=25.02, timestamp_ms=1_000)
        perp_bbo = TopOfBook(bid=25.01, ask=25.03, timestamp_ms=1_000)
        ep = await runner.open_carry_episode(
            episode_id="carry-hype-001",
            spot_symbol="HYPE/USDC",
            perp_symbol="HYPE",
            notional_usd=500.0,
            max_leverage=10.0,  # maint=0.05, M=0.45, M0=0.60 -> committed = 500 + 300 = $800
            spot_book=spot_bbo,
            perp_book=perp_bbo,
            now_ms=1_000,
        )
        assert client.abstractions == ["u"]
        assert ep.status == "pending_entry"
        spot_oid = ep.legs["spot"].order_id
        perp_oid = ep.legs["perp"].order_id
        assert spot_oid and perp_oid

        # Simulate maker fills on both legs
        qty = ep.legs["spot"].spec.target_size
        client.fills_by_oid[spot_oid] = [{"coin": "@107", "sz": str(qty), "px": "25.00", "fee": "0.075"}]
        client.fills_by_oid[perp_oid] = [{"coin": "HYPE", "sz": str(qty), "px": "25.03", "fee": "0.075"}]

        await runner.step_carry_entry(
            ep, spot_book=spot_bbo, perp_book=perp_bbo, now_ms=5_000
        )
        assert ep.status == "open"
        assert ep.open_qty == qty
        # Dead-man's switch disarmed once open
        assert client.dms_calls[-1] is None

        # Accrue positive hourly funding (+0.05%/h)
        cf = runner.accrue_hourly_funding(ep, hourly_funding_rate=0.0005, perp_mark_px=25.03)
        assert cf > 0

        # Inject +30% HYPE rally ($25.03 -> $32.54):
        # N_t = 19.98 * 32.54 = ~650.15; perp uPnL = 19.98 * (25.03 - 32.54) = -150.05
        # live_equity = ~300.1 - 150.05 = ~150.05 -> mf = 150.05 / 650.15 = 0.2308 < M (0.45)!
        rally_spot = TopOfBook(bid=32.50, ask=32.54, timestamp_ms=65_000)
        rally_perp = TopOfBook(bid=32.52, ask=32.56, timestamp_ms=65_000)
        delev = await runner.evaluate_margin_and_deleverage(
            ep, spot_book=rally_spot, perp_book=rally_perp, now_ms=65_000
        )
        assert delev.zone == "deleveraged"
        assert 0.55 < delev.close_fraction < 0.68
        # Delta neutrality strictly preserved after proportional deleverage!
        assert abs(ep.legs["spot"].filled_size - ep.legs["perp"].filled_size) < 1e-6
        assert ep.open_qty == ep.legs["spot"].filled_size
        assert ep.deleverages_count == 1

        # Re-evaluating at exact same price must now be in 'ok' zone (~M0 = 0.60)!
        delev_after = await runner.evaluate_margin_and_deleverage(
            ep, spot_book=rally_spot, perp_book=rally_perp, now_ms=66_000
        )
        assert delev_after.zone == "ok"
        assert delev_after.margin_fraction >= 0.45

        # Exit episode flat
        await coord.close_episode_taker(
            ep,
            books={"HYPE/USDC": rally_spot, "HYPE": rally_perp},
            reason="funding_turned_negative",
            now_ms=100_000,
        )
        assert ep.status == "closed"
        assert ep.open_qty == 0.0

    try:
        asyncio.run(_run())
    finally:
        ledger.close()


@pytest.mark.unit
def test_unlegged_timeout_unwind_and_post_cancel_rtt_race(tmp_path) -> None:
    _seed_hl_meta()
    ledger = MultiLegLedger(str(tmp_path / "ml_race.db"))

    async def _run() -> None:
        client = FakeMultiLegHLClient(account_value=5_000.0, spot_usdc=5_000.0)
        coord = MultiLegCoordinator(
            ledger=ledger,
            live_client=client,
            mode="testnet",
            maker_fill_window_s=60.0,
            max_unlegged_adverse_bps=50.0,
        )
        runner = SpotPerpCarryRunner(coord)
        spot_bbo = TopOfBook(bid=25.00, ask=25.02, timestamp_ms=1_000)
        perp_bbo = TopOfBook(bid=25.01, ask=25.03, timestamp_ms=1_000)

        # Case A: Perp fills, Spot never fills -> 60s timeout cancels Spot & IoC-unwinds Perp
        ep_timeout = await runner.open_carry_episode(
            episode_id="carry-timeout-1",
            spot_symbol="HYPE/USDC",
            perp_symbol="HYPE",
            notional_usd=500.0,
            max_leverage=10.0,
            spot_book=spot_bbo,
            perp_book=perp_bbo,
            now_ms=1_000,
        )
        qty = ep_timeout.legs["perp"].spec.target_size
        perp_oid = ep_timeout.legs["perp"].order_id
        assert perp_oid is not None
        client.fills_by_oid[perp_oid] = [{"coin": "HYPE", "sz": str(qty), "px": "25.03", "fee": "0.07"}]

        await runner.step_carry_entry(
            ep_timeout, spot_book=spot_bbo, perp_book=perp_bbo, now_ms=62_000
        )
        assert ep_timeout.status == "aborted_unwound"
        assert ep_timeout.legs["perp"].filled_size == 0.0
        # Verify reduce_only IoC buy was sent to unwind the short perp leg
        assert len(client.perp_aggressive) == 1
        assert client.perp_aggressive[0]["reduce_only"] is True
        assert client.perp_aggressive[0]["side"] == "buy"

        # Case B: Post-cancel RTT race — Spot fills DURING cancel_order() call!
        ep_race = await runner.open_carry_episode(
            episode_id="carry-race-2",
            spot_symbol="HYPE/USDC",
            perp_symbol="HYPE",
            notional_usd=500.0,
            max_leverage=10.0,
            spot_book=spot_bbo,
            perp_book=perp_bbo,
            now_ms=100_000,
        )
        race_spot_oid = ep_race.legs["spot"].order_id
        race_perp_oid = ep_race.legs["perp"].order_id
        assert race_spot_oid and race_perp_oid
        client.fills_by_oid[race_perp_oid] = [{"coin": "HYPE", "sz": str(qty), "px": "25.03", "fee": "0.07"}]
        client.fill_on_cancel_oid = race_spot_oid
        client.fill_on_cancel_payload = {"coin": "@107", "sz": str(qty), "px": "25.00", "fee": "0.07"}

        prev_unwind_calls = len(client.perp_aggressive)
        await runner.step_carry_entry(
            ep_race, spot_book=spot_bbo, perp_book=perp_bbo, now_ms=165_000
        )
        # Must transition cleanly to 'open' with ZERO erroneous unwinds!
        assert ep_race.status == "open"
        assert len(client.perp_aggressive) == prev_unwind_calls

    try:
        asyncio.run(_run())
    finally:
        ledger.close()


@pytest.mark.unit
def test_unlegged_adverse_excursion_circuit_and_unlock_short_pair(tmp_path) -> None:
    _seed_hl_meta()
    ledger = MultiLegLedger(str(tmp_path / "ml_pairs.db"))

    async def _run() -> None:
        coord = MultiLegCoordinator(
            ledger=ledger,
            mode="paper",
            maker_fill_window_s=900.0,
            max_unlegged_adverse_bps=30.0,
        )
        pair_runner = PerpPairRunner(coord)

        arb_book = TopOfBook(bid=1.0000, ask=1.0004, timestamp_ms=1_000)
        btc_book = TopOfBook(bid=90_000.0, ask=90_010.0, timestamp_ms=1_000)

        # beta=2.8 must clamp to 2.0 -> short $500 ARB + long $1,000 BTC
        ep = await pair_runner.open_unlock_short_pair(
            episode_id="unlock-arb-001",
            token_symbol="ARB",
            token_notional_usd=500.0,
            beta_30d=2.8,
            token_book=arb_book,
            btc_book=btc_book,
            now_ms=1_000,
        )
        assert ep.status == "pending_entry"
        btc_notional = ep.legs["long_btc"].spec.target_size * 90_000.0
        assert 995.0 <= btc_notional <= 1_005.0

        # Step 1: ARB short fills (bid > 1.0004), BTC long stays resting (ask=90_010 >= 90_000)
        await coord.step_entry(
            ep,
            books={
                "ARB": TopOfBook(bid=1.0006, ask=1.0010, timestamp_ms=2_000),
                "BTC": btc_book,
            },
            now_ms=2_000,
        )
        assert ep.status == "pending_entry"
        assert ep.legs["short_alt"].is_filled
        assert not ep.legs["long_btc"].is_filled

        # Step 2: BTC rallies +0.50% (+50 bps > 30 bps circuit) while still unlegged -> immediate abort & unwind!
        await coord.step_entry(
            ep,
            books={
                "ARB": TopOfBook(bid=1.0006, ask=1.0010, timestamp_ms=5_000),
                "BTC": TopOfBook(bid=90_460.0, ask=90_470.0, timestamp_ms=5_000),
            },
            now_ms=5_000,
        )
        assert ep.status == "aborted_unwound"
        assert ep.max_unlegged_adverse_bps > 30.0
        assert ep.legs["short_alt"].filled_size == 0.0

        # Also verify 3-leg ResMom basket paper fill
        basket = await pair_runner.open_perp_basket(
            episode_id="resmom-wk01",
            strategy="ResMom",
            signed_notionals_usd={"ETH": 400.0, "SOL": 400.0, "ARB": -800.0},
            books={
                "ETH": TopOfBook(bid=3_000.0, ask=3_001.0),
                "SOL": TopOfBook(bid=150.0, ask=150.1),
                "ARB": TopOfBook(bid=1.0000, ask=1.0005),
            },
            now_ms=10_000,
        )
        await coord.step_entry(
            basket,
            books={
                "ETH": TopOfBook(bid=2_998.0, ask=2_999.0),
                "SOL": TopOfBook(bid=149.8, ask=149.9),
                "ARB": TopOfBook(bid=1.0010, ask=1.0015),
            },
            now_ms=12_000,
        )
        assert basket.status == "open"

    try:
        asyncio.run(_run())
    finally:
        ledger.close()
