"""SpotPerpCarryRunner — live & paper execution adapter for CarryA1 on Hyperliquid.

Implements the exact signal & margin rules frozen in
``docs/ops/PREREGISTER_CARRY_SHADOW.md``:
  - Entry: Spot BUY (``BASE/USDC`` / ``@idx``) + Perp SELL (``BASE``) maker at touch.
  - Committed capital: ``1.0x`` Spot + ``M0`` (``0.60x`` default) Perp margin
    (or ``1.1x`` under Portfolio Margin for eligible ``HYPE``).
  - Margin check: ``mf = equity / (open_qty * perp_mid)``:
      * ``mf <= maint`` -> emergency 100% taker flatten (``liquidated_guard``).
      * ``mf < m_trigger`` -> proportional deleverage fraction
        ``k = 1 - equity / (m0 * N_t)`` closed simultaneously on BOTH Spot and Perp
        so delta neutrality (``spot_qty == perp_qty``) is strictly preserved.
      * ``1 - k <= delev_floor (0.05)`` -> close 100% of episode.
"""

from __future__ import annotations

from typing import Optional

from src.exchanges.hyperliquid_live import normalize_size
from src.multileg.coordinator import MultiLegCoordinator
from src.multileg.types import (
    CarryMarginParams,
    DeleverageAction,
    LegSpec,
    MultiLegEpisode,
    PM_USDC_BUFFER,
    TopOfBook,
)


class SpotPerpCarryRunner:
    """High-level runner for delta-neutral Hyperliquid Spot+Perp Carry episodes."""

    def __init__(self, coordinator: MultiLegCoordinator) -> None:
        self._coord = coordinator

    async def open_carry_episode(
        self,
        *,
        episode_id: str,
        spot_symbol: str,
        perp_symbol: str,
        notional_usd: float,
        max_leverage: float,
        spot_book: TopOfBook,
        perp_book: TopOfBook,
        account_value_usd: float = 0.0,
        pm_enabled: bool = False,
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Size, margin-check, and submit both Spot BUY and Perp SELL maker legs."""
        if spot_book.mid <= 0 or perp_book.mid <= 0:
            raise ValueError("Valid Spot and Perp TopOfBook quotes are required")
        margin_params = CarryMarginParams.for_pair(
            perp_symbol,
            max_leverage,
            pm_enabled=pm_enabled,
            account_value_usd=account_value_usd,
        )
        perp_collateral = (
            float(notional_usd) * PM_USDC_BUFFER
            if margin_params.pm_branch
            else float(notional_usd) * margin_params.m0_initial
        )
        await self._coord.ensure_margin_ready(
            spot_notional_usd=float(notional_usd),
            perp_collateral_usd=perp_collateral,
        )

        raw_qty = float(notional_usd) / perp_book.mid
        # Floor to the coarser szDecimals of Spot and Perp so both legs hold identical quantity
        qty_spot = normalize_size(spot_symbol, raw_qty)
        qty_perp = normalize_size(perp_symbol, qty_spot)
        matched_qty = min(qty_spot, qty_perp)
        if matched_qty <= 0:
            raise ValueError(f"Carry matched quantity rounded to 0 for {perp_symbol}")

        legs = [
            LegSpec(
                leg_id="spot",
                symbol=spot_symbol,
                market_kind="spot",
                side="buy",
                target_size=matched_qty,
                limit_price=spot_book.bid,
            ),
            LegSpec(
                leg_id="perp",
                symbol=perp_symbol,
                market_kind="perp",
                side="sell",
                target_size=matched_qty,
                limit_price=perp_book.ask,
            ),
        ]
        ep = await self._coord.submit_entry(
            episode_id=episode_id,
            strategy="CarryA1",
            legs=legs,
            books={spot_symbol: spot_book, perp_symbol: perp_book},
            now_ms=now_ms,
        )
        ep.margin_params = margin_params
        ep.collateral_usd = matched_qty * perp_book.mid * (
            PM_USDC_BUFFER if margin_params.pm_branch else margin_params.m0_initial
        )
        self._sync_open_carry_fields(ep)
        return ep

    async def step_carry_entry(
        self,
        ep: MultiLegEpisode,
        *,
        spot_book: TopOfBook,
        perp_book: TopOfBook,
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Advance pending maker fills and sync carry basis once opened."""
        spot_sym = ep.legs["spot"].spec.symbol
        perp_sym = ep.legs["perp"].spec.symbol
        await self._coord.step_entry(
            ep,
            books={spot_sym: spot_book, perp_sym: perp_book},
            now_ms=now_ms,
        )
        self._sync_open_carry_fields(ep)
        return ep

    def accrue_hourly_funding(
        self,
        ep: MultiLegEpisode,
        *,
        hourly_funding_rate: float,
        perp_mark_px: float,
    ) -> float:
        """Credit/debit 1h perp short funding into ``collateral_usd``."""
        if ep.status != "open" or ep.open_qty <= 0 or perp_mark_px <= 0:
            return 0.0
        # Short perp receives positive funding and pays negative funding
        cashflow = ep.open_qty * float(perp_mark_px) * float(hourly_funding_rate)
        ep.funding_accrued_usd += cashflow
        ep.collateral_usd += cashflow
        return cashflow

    async def evaluate_margin_and_deleverage(
        self,
        ep: MultiLegEpisode,
        *,
        spot_book: TopOfBook,
        perp_book: TopOfBook,
        now_ms: Optional[int] = None,
    ) -> DeleverageAction:
        """Enforce PREREGISTER_CARRY_SHADOW §2 proportional deleveraging & maint guard."""
        if ep.status != "open" or ep.open_qty <= 0 or ep.margin_params is None:
            return DeleverageAction(zone="ok", margin_fraction=1.0, remaining_qty=ep.open_qty)

        mp = ep.margin_params
        perp_mid = perp_book.mid
        if perp_mid <= 0:
            return DeleverageAction(zone="ok", margin_fraction=1.0, remaining_qty=ep.open_qty)

        notional_now = ep.open_qty * perp_mid
        # Perp short unrealized PnL = open_qty * (entry_perp_px - perp_mid)
        perp_upnl = ep.open_qty * (ep.entry_perp_px - perp_mid)
        live_equity = ep.collateral_usd + perp_upnl
        mf = live_equity / notional_now if notional_now > 0 else 1.0

        spot_sym = ep.legs["spot"].spec.symbol
        perp_sym = ep.legs["perp"].spec.symbol
        books = {spot_sym: spot_book, perp_sym: perp_book}

        if not mp.pm_branch and mf <= mp.maint_fraction:
            closed_qty = ep.open_qty
            await self._coord.close_episode_taker(
                ep,
                books=books,
                reason="maint_guard_breach",
                fraction=1.0,
                now_ms=now_ms,
            )
            ep.status = "liquidated_guard"
            self._coord._ledger.save_episode(ep)
            return DeleverageAction(
                zone="liquidated_guard",
                margin_fraction=mf,
                close_fraction=1.0,
                closed_qty=closed_qty,
                remaining_qty=0.0,
            )

        if not mp.pm_branch and mf < mp.m_trigger:
            k = 1.0 - (live_equity / (mp.m0_initial * notional_now))
            k = min(1.0, max(0.0, k))
            keep = 1.0 - k
            if keep <= mp.delev_floor:
                closed_qty = ep.open_qty
                await self._coord.close_episode_taker(
                    ep,
                    books=books,
                    reason="delev_floor_close",
                    fraction=1.0,
                    now_ms=now_ms,
                )
                return DeleverageAction(
                    zone="closed_floor",
                    margin_fraction=mf,
                    close_fraction=1.0,
                    closed_qty=closed_qty,
                    remaining_qty=0.0,
                )

            qty_before = ep.open_qty
            fees = await self._coord.close_episode_taker(
                ep,
                books=books,
                reason=f"proportional_deleverage_{k:.4f}",
                fraction=k,
                now_ms=now_ms,
            )
            # Realize closed perp fraction PnL & taker fees into perp collateral;
            # selling k of spot releases cash outside perp margin while shrinking N_t,
            # restoring live_equity / N_new == M0.
            spot_rem = ep.legs["spot"].filled_size
            perp_rem = ep.legs["perp"].filled_size
            new_qty = min(spot_rem, perp_rem)
            closed_qty = max(0.0, qty_before - new_qty)
            realized_perp_loss = closed_qty * (ep.entry_perp_px - perp_mid)
            ep.collateral_usd = ep.collateral_usd + realized_perp_loss - fees
            ep.open_qty = new_qty
            ep.deleverages_count += 1
            self._coord._ledger.save_episode(ep)
            return DeleverageAction(
                zone="deleveraged",
                margin_fraction=mf,
                close_fraction=k,
                closed_qty=closed_qty,
                remaining_qty=new_qty,
            )

        return DeleverageAction(zone="ok", margin_fraction=mf, remaining_qty=ep.open_qty)

    def _sync_open_carry_fields(self, ep: MultiLegEpisode) -> None:
        if ep.status != "open" or ep.open_qty > 0:
            return
        spot_leg = ep.legs.get("spot")
        perp_leg = ep.legs.get("perp")
        if spot_leg and perp_leg and spot_leg.is_filled and perp_leg.is_filled:
            ep.open_qty = min(spot_leg.filled_size, perp_leg.filled_size)
            ep.entry_spot_px = spot_leg.avg_fill_price
            ep.entry_perp_px = perp_leg.avg_fill_price
            ep.collateral_usd -= (spot_leg.cumulative_fee + perp_leg.cumulative_fee)
            self._coord._ledger.save_episode(ep)
