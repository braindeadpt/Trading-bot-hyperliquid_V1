"""MultiLegCoordinator — atomic Spot+Perp & Perp-Perp coordination on Hyperliquid.

Handles:
  - Pre-flight L1 account abstraction ('u' Unified for <$10k Agent Wallets,
    'p' Portfolio Margin for >$10k eligible accounts, or 'standard_transfer'
    fallback via master-key usd_class_transfer).
  - Simultaneous maker-at-touch ('Alo') submission across N legs with
    deterministic 16-byte hex Cloids.
  - Dead-man's switch (``schedule_cancel``) while resting maker legs await fill.
  - 15-minute maker fill window + basis-free unlegged adverse-excursion circuit.
  - Post-cancel RTT fill verification before unwinding any partially/singly
    filled legs via aggressive IoC orders.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from src.core.order_lifecycle import (
    ORDER_FILLED,
    ORDER_PARTIAL,
    ORDER_RESTING,
    parse_hyperliquid_close_response,
    parse_hyperliquid_entry_response,
)
from src.exchanges.hyperliquid_live import (
    MIN_ORDER_NOTIONAL_USD,
    get_symbol_info,
    make_cloid,
    normalize_price,
    normalize_size,
    resolve_wire_coin,
)
from src.multileg.ledger import MultiLegLedger
from src.multileg.types import (
    LegSpec,
    LegState,
    MarginProfile,
    MultiLegEpisode,
    TopOfBook,
)
from src.utils.helpers import safe_float

logger = logging.getLogger(__name__)

MAKER_FEE_RATE: float = 0.00015  # 1.5 bps Hyperliquid tier-0 maker
TAKER_FEE_RATE: float = 0.00045  # 4.5 bps Hyperliquid tier-0 taker


class MultiLegCoordinator:
    """Coordinates atomic multi-leg entry, unlegged protection, and unwind on HL."""

    def __init__(
        self,
        *,
        ledger: MultiLegLedger,
        live_client: Optional[Any] = None,
        mode: str = "paper",
        margin_profile: MarginProfile = "unified",
        maker_fill_window_s: float = 900.0,
        max_unlegged_adverse_bps: float = 35.0,
        dead_mans_switch_s: float = 120.0,
    ) -> None:
        if mode not in ("paper", "testnet", "mainnet"):
            raise ValueError(f"Unsupported MultiLegCoordinator mode: {mode!r}")
        if mode in ("testnet", "mainnet") and live_client is None:
            raise ValueError(f"live_client is required when mode={mode!r}")
        self._ledger = ledger
        self._client = live_client
        self._mode = mode
        self._margin_profile: MarginProfile = margin_profile
        self._maker_window_ms = int(float(maker_fill_window_s) * 1000.0)
        self._max_unlegged_adverse_bps = float(max_unlegged_adverse_bps)
        self._dms_horizon_ms = int(float(dead_mans_switch_s) * 1000.0)
        self._abstraction_verified = False

    @property
    def mode(self) -> str:
        return self._mode

    async def ensure_margin_ready(
        self,
        *,
        spot_notional_usd: float = 0.0,
        perp_collateral_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """Configure L1 abstraction and verify available USDC before placing legs."""
        required_usd = float(spot_notional_usd) + float(perp_collateral_usd)
        if self._mode == "paper":
            return {
                "ready": True,
                "profile": self._margin_profile,
                "required_usd": required_usd,
            }

        assert self._client is not None
        if not self._abstraction_verified:
            if self._margin_profile == "unified":
                await self._client.set_account_abstraction("u")
            elif self._margin_profile == "portfolio_margin":
                await self._client.set_account_abstraction("p")
            self._abstraction_verified = True

        user_state = await self._client.get_user_state()
        margin_summary = (
            user_state.get("marginSummary", {}) if isinstance(user_state, dict) else {}
        )
        perp_acct_val = safe_float(margin_summary.get("accountValue"), 0.0)
        total_margin_used = safe_float(margin_summary.get("totalMarginUsed"), 0.0)
        if isinstance(user_state, dict) and user_state.get("withdrawable") is not None:
            free_perp_usd = max(0.0, safe_float(user_state.get("withdrawable"), 0.0))
        else:
            free_perp_usd = max(0.0, perp_acct_val - total_margin_used)

        avail_spot_fn = getattr(self._client, "get_available_spot_balances", None)
        if callable(avail_spot_fn):
            spot_balances = await avail_spot_fn()
        else:
            spot_balances = await self._client.get_spot_balances()
        spot_usdc = safe_float(spot_balances.get("USDC"), 0.0)

        # Under Unified ('u') / PM ('p'), Spot and Perp draw from the same USDC pool;
        # deduct locked perp margin when computing free USDC capacity.
        if self._margin_profile in ("unified", "portfolio_margin"):
            available_usd = max(free_perp_usd, max(0.0, spot_usdc - total_margin_used))
            if perp_acct_val > 0 and total_margin_used > 0:
                available_usd = min(available_usd, free_perp_usd)
        else:
            available_usd = free_perp_usd + spot_usdc
            if spot_notional_usd > 0 and spot_usdc < spot_notional_usd:
                shortfall = round(spot_notional_usd - spot_usdc + 1.0, 2)
                await self._client.transfer_usd_class(shortfall, to_perp=False)

        if required_usd > 0 and available_usd + 1e-6 < required_usd:
            raise ValueError(
                f"Insufficient Hyperliquid USDC for multi-leg entry: "
                f"available=${available_usd:.2f} < required=${required_usd:.2f}"
            )
        return {
            "ready": True,
            "profile": self._margin_profile,
            "available_usd": available_usd,
            "required_usd": required_usd,
        }

    async def submit_entry(
        self,
        *,
        episode_id: str,
        strategy: str,
        legs: Sequence[LegSpec],
        books: Dict[str, TopOfBook],
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Submit resting maker ('Alo') orders for all *legs* and persist episode."""
        if len(legs) < 2:
            raise ValueError("MultiLegCoordinator requires at least 2 legs")
        ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)

        ep = MultiLegEpisode(
            episode_id=episode_id,
            strategy=strategy,
            mode=self._mode,
            margin_profile=self._margin_profile,
            created_ms=ts_ms,
        )

        # Pre-validate sizes and $10 notional across EVERY leg before submitting leg 1
        prepared: List[tuple[LegSpec, float, float, str]] = []
        for idx, spec in enumerate(legs):
            book = books.get(spec.symbol)
            if book is None or book.bid <= 0 or book.ask <= 0:
                raise ValueError(f"Missing valid TopOfBook for leg {spec.leg_id} ({spec.symbol})")
            raw_px = (
                spec.limit_price
                if spec.limit_price and spec.limit_price > 0
                else (book.bid if spec.side == "buy" else book.ask)
            )
            sz = normalize_size(spec.symbol, spec.target_size)
            px = normalize_price(spec.symbol, raw_px)
            if sz <= 0 or px <= 0:
                raise ValueError(f"Invalid normalized sz={sz} px={px} for {spec.symbol}")
            if not spec.reduce_only and (sz * px) < MIN_ORDER_NOTIONAL_USD:
                raise ValueError(
                    f"Leg {spec.leg_id} ({spec.symbol}) notional ${sz * px:.2f} < ${MIN_ORDER_NOTIONAL_USD:.0f}"
                )
            cloid = make_cloid(spec.symbol, spec.side, timestamp_ms=ts_ms, salt=f"{episode_id}:{idx}")
            norm_spec = LegSpec(
                leg_id=spec.leg_id,
                symbol=spec.symbol,
                market_kind=spec.market_kind,
                side=spec.side,
                target_size=sz,
                limit_price=px,
                reduce_only=spec.reduce_only,
            )
            prepared.append((norm_spec, sz, px, cloid))

        if self._mode in ("testnet", "mainnet") and self._client is not None:
            await self._arm_dead_mans_switch(ts_ms)

        submitted_legs: List[LegState] = []
        try:
            for norm_spec, sz, px, cloid in prepared:
                state = LegState(spec=norm_spec, status="resting", cloid=cloid)
                if self._mode in ("testnet", "mainnet"):
                    assert self._client is not None
                    if norm_spec.market_kind == "spot":
                        resp = await self._client.place_spot_order(
                            norm_spec.symbol,
                            norm_spec.side,
                            sz,
                            order_type="limit_maker",
                            limit_price=px,
                            cloid=cloid,
                        )
                    else:
                        perp_side = "long" if norm_spec.side == "buy" else "short"
                        resp = await self._client.place_entry(
                            norm_spec.symbol,
                            perp_side,
                            sz,
                            order_type="limit_maker",
                            limit_price=px,
                            post_only=True,
                            cloid=cloid,
                        )
                    parsed = parse_hyperliquid_entry_response(resp)
                    if parsed.lifecycle_state not in (ORDER_RESTING, ORDER_FILLED, ORDER_PARTIAL):
                        raise RuntimeError(
                            f"Leg {norm_spec.leg_id} rejected: {parsed.error_message or parsed.lifecycle_state}"
                        )
                    state.order_id = parsed.exchange_order_id
                    if parsed.lifecycle_state == ORDER_FILLED:
                        state.status = "filled"
                        state.filled_size = parsed.filled_size or sz
                        state.avg_fill_price = parsed.avg_fill_price or px
                        state.cumulative_fee = state.filled_size * state.avg_fill_price * MAKER_FEE_RATE
                        state.filled_at_ms = ts_ms
                else:
                    state.order_id = f"paper-{episode_id}-{norm_spec.leg_id}"
                ep.legs[norm_spec.leg_id] = state
                submitted_legs.append(state)
        except Exception as exc:
            # Immediate rollback of any leg already placed during multi-leg submission
            ep.close_reason = f"entry_submit_failed:{exc}"
            await self._abort_and_unwind(ep, books=books, now_ms=ts_ms, reason=ep.close_reason)
            return ep

        self._ledger.save_episode(ep)
        self._ledger.append_event(
            ep.episode_id,
            "entry_submitted",
            {
                "legs": {
                    lid: {
                        "symbol": st.spec.symbol,
                        "kind": st.spec.market_kind,
                        "side": st.spec.side,
                        "sz": st.spec.target_size,
                        "px": st.spec.limit_price,
                        "oid": st.order_id,
                        "status": st.status,
                    }
                    for lid, st in ep.legs.items()
                }
            },
            timestamp_ms=ts_ms,
        )
        await self._evaluate_post_fill_transition(ep, books=books, now_ms=ts_ms)
        return ep

    async def step_entry(
        self,
        ep: MultiLegEpisode,
        *,
        books: Dict[str, TopOfBook],
        now_ms: Optional[int] = None,
    ) -> MultiLegEpisode:
        """Advance a ``pending_entry`` episode by polling fills, unlegged excursion & timeout."""
        if ep.status != "pending_entry":
            return ep
        ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)

        if self._mode == "paper":
            for leg in ep.legs.values():
                if leg.is_filled:
                    continue
                book = books.get(leg.spec.symbol)
                lim = leg.spec.limit_price or 0.0
                if book and lim > 0:
                    # Conservative strict-cross maker rule (matches carry_shadow/sim.py)
                    crossed = book.ask < lim if leg.spec.side == "buy" else book.bid > lim
                    if crossed:
                        leg.status = "filled"
                        leg.filled_size = leg.spec.target_size
                        leg.avg_fill_price = lim
                        leg.cumulative_fee = lim * leg.filled_size * MAKER_FEE_RATE
                        leg.filled_at_ms = ts_ms
                        self._ledger.append_event(
                            ep.episode_id,
                            "leg_fill",
                            {"leg_id": leg.spec.leg_id, "px": lim, "sz": leg.filled_size},
                            timestamp_ms=ts_ms,
                        )
        else:
            await self._arm_dead_mans_switch(ts_ms)
            for leg in ep.legs.values():
                if leg.is_filled or not leg.order_id:
                    continue
                await self._refresh_live_leg_fills(leg, ts_ms)

        await self._evaluate_post_fill_transition(ep, books=books, now_ms=ts_ms)
        if ep.status != "pending_entry":
            return ep

        # Check unlegged adverse excursion circuit
        if (
            ep.unlegged_missing_leg
            and self._max_unlegged_adverse_bps > 0
            and ep.max_unlegged_adverse_bps > self._max_unlegged_adverse_bps
        ):
            await self._abort_and_unwind(
                ep,
                books=books,
                now_ms=ts_ms,
                reason=f"unlegged_adverse_{ep.max_unlegged_adverse_bps:.1f}bps",
            )
            return ep

        # Check 15m maker fill timeout
        if (ts_ms - ep.created_ms) >= self._maker_window_ms:
            await self._abort_and_unwind(
                ep,
                books=books,
                now_ms=ts_ms,
                reason="maker_window_timeout",
            )
            return ep

        self._ledger.save_episode(ep)
        return ep

    @staticmethod
    def _spot_base_token(symbol: str) -> str:
        info = get_symbol_info(symbol)
        pair_name = str(info.get("pair_name", "")) if info else ""
        candidate = pair_name or symbol
        if "/" in candidate:
            return candidate.split("/", 1)[0].upper()
        return candidate.upper()

    async def close_episode_taker(
        self,
        ep: MultiLegEpisode,
        *,
        books: Dict[str, TopOfBook],
        reason: str,
        fraction: float = 1.0,
        now_ms: Optional[int] = None,
    ) -> float:
        """Close *fraction* (0..1] of every open leg via aggressive IoC. Returns fees USD."""
        ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        frac = min(1.0, max(0.0, float(fraction)))
        if frac <= 0:
            return 0.0

        spot_avail: Dict[str, float] = {}
        if self._mode in ("testnet", "mainnet") and self._client is not None:
            avail_fn = getattr(self._client, "get_available_spot_balances", None)
            if callable(avail_fn):
                spot_avail = await avail_fn()
            else:
                spot_avail = await self._client.get_spot_balances()

        fees_added = 0.0
        incomplete_legs: List[str] = []
        for leg in ep.legs.values():
            if leg.filled_size <= 0:
                continue
            requested_qty = normalize_size(leg.spec.symbol, leg.filled_size * frac)
            if requested_qty <= 0:
                continue
            exit_side = "sell" if leg.spec.side == "buy" else "buy"
            book = books.get(leg.spec.symbol)
            ref_px = (
                (book.bid if exit_side == "sell" else book.ask)
                if book and book.mid > 0
                else leg.avg_fill_price
            )

            eff_close_qty = requested_qty
            fee_dust_qty = 0.0
            if (
                self._mode in ("testnet", "mainnet")
                and leg.spec.market_kind == "spot"
                and exit_side == "sell"
            ):
                base_tok = self._spot_base_token(leg.spec.symbol)
                if base_tok in spot_avail:
                    held_qty = normalize_size(leg.spec.symbol, spot_avail[base_tok])
                    if 0.0 < held_qty < requested_qty:
                        gap = requested_qty - held_qty
                        info = get_symbol_info(leg.spec.symbol)
                        sz_dec = int(info.get("sz_decimals", 4)) if info else 4
                        max_fee_dust = max(leg.filled_size * 0.002, 2.0 * (10.0 ** (-sz_dec)))
                        eff_close_qty = held_qty
                        if gap <= max_fee_dust and frac >= 0.999:
                            fee_dust_qty = gap

            confirmed_closed = eff_close_qty
            exec_px = ref_px
            if self._mode in ("testnet", "mainnet") and self._client is not None:
                if leg.spec.market_kind == "spot":
                    resp = await self._client.place_spot_order(
                        leg.spec.symbol,
                        exit_side,
                        eff_close_qty,
                        order_type="ioc",
                        limit_price=ref_px,
                        enforce_min_notional=False,
                    )
                else:
                    resp = await self._client.place_aggressive_limit(
                        leg.spec.symbol,
                        exit_side,
                        eff_close_qty,
                        reference_price=ref_px,
                        max_slippage_pct=3.0,
                        reduce_only=True,
                    )
                parsed_close = parse_hyperliquid_close_response(
                    resp, expected_size=eff_close_qty
                )
                if parsed_close.avg_fill_price and parsed_close.avg_fill_price > 0:
                    exec_px = float(parsed_close.avg_fill_price)
                if parsed_close.lifecycle_state == ORDER_FILLED:
                    confirmed_closed = eff_close_qty
                elif parsed_close.lifecycle_state == ORDER_PARTIAL and parsed_close.filled_size:
                    confirmed_closed = float(parsed_close.filled_size)
                    fee_dust_qty = 0.0
                    incomplete_legs.append(
                        f"{leg.spec.leg_id}:partial:{confirmed_closed:.6f}/{eff_close_qty:.6f}"
                    )
                else:
                    confirmed_closed = 0.0
                    fee_dust_qty = 0.0
                    incomplete_legs.append(
                        f"{leg.spec.leg_id}:{parsed_close.error_message or parsed_close.lifecycle_state}"
                    )

            if confirmed_closed > 0:
                fee_usd = confirmed_closed * exec_px * TAKER_FEE_RATE
                fees_added += fee_usd
                leg.cumulative_fee += fee_usd
                leg.filled_size = max(0.0, leg.filled_size - confirmed_closed - fee_dust_qty)
                if leg.filled_size <= 1e-10:
                    leg.filled_size = 0.0
                    leg.status = "closed"

        ep.total_fees_usd += fees_added
        if incomplete_legs:
            ep.status = "pending_exit"
            self._ledger.save_episode(ep)
            self._ledger.append_event(
                ep.episode_id,
                "close_incomplete",
                {"reason": reason, "incomplete_legs": incomplete_legs, "fees_usd": fees_added},
                timestamp_ms=ts_ms,
            )
            raise RuntimeError(f"multileg_close_incomplete:{','.join(incomplete_legs)}")

        if frac >= 0.999 and all(lg.filled_size <= 1e-10 for lg in ep.legs.values()):
            ep.status = "closed"
            ep.open_qty = 0.0
            ep.closed_ms = ts_ms
            ep.close_reason = reason
            await self._disarm_dead_mans_switch()
        self._ledger.save_episode(ep)
        self._ledger.append_event(
            ep.episode_id,
            "taker_close",
            {"fraction": frac, "reason": reason, "fees_usd": fees_added},
            timestamp_ms=ts_ms,
        )
        return fees_added

    # ── Internal helpers ────────────────────────────────────────

    async def _evaluate_post_fill_transition(
        self,
        ep: MultiLegEpisode,
        *,
        books: Dict[str, TopOfBook],
        now_ms: int,
    ) -> None:
        filled_legs = [lg for lg in ep.legs.values() if lg.is_filled]
        unfilled_legs = [lg for lg in ep.legs.values() if not lg.is_filled]

        if not unfilled_legs:
            ep.status = "open"
            ep.opened_ms = now_ms
            ep.total_fees_usd = sum(lg.cumulative_fee for lg in ep.legs.values())
            await self._disarm_dead_mans_switch()
            self._ledger.save_episode(ep)
            self._ledger.append_event(
                ep.episode_id,
                "episode_opened",
                {
                    lid: {"avg_px": lg.avg_fill_price, "sz": lg.filled_size}
                    for lid, lg in ep.legs.items()
                },
                timestamp_ms=now_ms,
            )
            return

        # Track unlegged adverse excursion when at least one leg has filled
        any_exposure = any(lg.filled_size > 0 for lg in ep.legs.values())
        if any_exposure and unfilled_legs:
            missing = unfilled_legs[0]
            m_book = books.get(missing.spec.symbol)
            m_mid = m_book.mid if m_book else 0.0
            if ep.unlegged_since_ms == 0 and m_mid > 0:
                ep.unlegged_since_ms = now_ms
                ep.unlegged_missing_leg = missing.spec.leg_id
                ep.unlegged_ref_mid = m_mid
            elif ep.unlegged_ref_mid > 0 and m_mid > 0:
                # Pending BUY loses when mid rises above ref; pending SELL loses when mid falls below ref
                if missing.spec.side == "buy":
                    adverse_bps = ((m_mid - ep.unlegged_ref_mid) / ep.unlegged_ref_mid) * 10_000.0
                else:
                    adverse_bps = ((ep.unlegged_ref_mid - m_mid) / ep.unlegged_ref_mid) * 10_000.0
                if adverse_bps > ep.max_unlegged_adverse_bps:
                    ep.max_unlegged_adverse_bps = adverse_bps

    async def _abort_and_unwind(
        self,
        ep: MultiLegEpisode,
        *,
        books: Dict[str, TopOfBook],
        now_ms: int,
        reason: str,
    ) -> None:
        """Cancel resting legs, re-check post-cancel RTT fills, and unwind filled exposure."""
        # Step 1: Cancel all resting orders AND re-read fills that landed during cancel RTT
        for leg in ep.legs.values():
            if leg.is_filled:
                continue
            if self._mode in ("testnet", "mainnet") and self._client is not None and leg.order_id:
                try:
                    await self._client.cancel_order(leg.spec.symbol, int(leg.order_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Cancel failed during abort on %s: %s", leg.spec.symbol, exc)
                await self._refresh_live_leg_fills(leg, now_ms)
            if not leg.is_filled and leg.filled_size <= 0:
                leg.status = "cancelled"

        # Step 2: If all legs actually filled during the cancel RTT, promote to OPEN!
        if all(lg.is_filled for lg in ep.legs.values()):
            await self._evaluate_post_fill_transition(ep, books=books, now_ms=now_ms)
            return

        # Step 3: Unwind any singly/partially filled legs via aggressive IoC
        had_filled_exposure = any(lg.filled_size > 0 for lg in ep.legs.values())
        if had_filled_exposure:
            await self.close_episode_taker(
                ep,
                books=books,
                reason=f"unwind:{reason}",
                fraction=1.0,
                now_ms=now_ms,
            )
            for lg in ep.legs.values():
                if lg.status == "closed":
                    lg.status = "unwound"
            ep.status = "aborted_unwound"
        else:
            ep.status = "aborted"

        ep.closed_ms = now_ms
        ep.close_reason = reason
        await self._disarm_dead_mans_switch()
        self._ledger.save_episode(ep)
        self._ledger.append_event(
            ep.episode_id,
            "episode_aborted",
            {"status": ep.status, "reason": reason},
            timestamp_ms=now_ms,
        )

    async def _refresh_live_leg_fills(self, leg: LegState, now_ms: int) -> None:
        if self._client is None or not leg.order_id:
            return
        fills = await self._client.get_order_fills(leg.order_id)
        if not fills:
            return
        total_sz = 0.0
        notional_sum = 0.0
        fee_sum = 0.0
        expected_coin = resolve_wire_coin(leg.spec.symbol).upper()
        for f in fills:
            coin = str(f.get("coin", "")).upper()
            if coin and coin not in (expected_coin, leg.spec.symbol.upper()):
                continue
            sz = safe_float(f.get("sz"), 0.0)
            px = safe_float(f.get("px"), 0.0)
            fee = abs(safe_float(f.get("fee"), 0.0))
            if sz > 0 and px > 0:
                total_sz += sz
                notional_sum += sz * px
                fee_sum += fee
        if total_sz > 0:
            leg.filled_size = total_sz
            leg.avg_fill_price = notional_sum / total_sz
            leg.cumulative_fee = fee_sum if fee_sum > 0 else notional_sum * MAKER_FEE_RATE
            leg.filled_at_ms = leg.filled_at_ms or now_ms
            leg.status = "filled" if leg.is_filled else "partial"

    async def _arm_dead_mans_switch(self, now_ms: int) -> None:
        if self._mode not in ("testnet", "mainnet") or self._client is None:
            return
        try:
            await self._client.schedule_cancel(now_ms + self._dms_horizon_ms)
        except Exception as exc:  # noqa: BLE001
            logger.debug("schedule_cancel arm skipped: %s", exc)

    async def _disarm_dead_mans_switch(self) -> None:
        if self._mode not in ("testnet", "mainnet") or self._client is None:
            return
        try:
            await self._client.schedule_cancel(None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("schedule_cancel disarm skipped: %s", exc)
