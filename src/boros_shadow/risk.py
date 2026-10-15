"""Hypothetical-liquidation math (prereg §Liquidações) — pure functions.

Three layers:
- POOLED: the two perp legs share 0.2*N margin (CrossEx-style). Death
  when equity = margin + unrealized perp PnL + net realized funding
  falls to the maintenance floor (0.5% of N per leg).
- BOROS: each YU leg posts 0.1*N. Death when the leg's MTM loss reaches
  that margin.
- SPLIT (descriptive only): no margin sharing, 3x per leg — flag when
  any leg's worst adverse mid-move since entry reaches 30%.
"""
from __future__ import annotations

from typing import Optional

from src.boros_shadow import spec


def leg_qty(entry_px: float) -> float:
    """Perp units for N notional at the recorded entry exec price."""
    return spec.NOTIONAL_USD / entry_px if entry_px > 0 else 0.0


def perp_upnl(side: str, entry_px: float, mid_now: float) -> float:
    """Unrealized USD for one perp leg marked at mid."""
    q = leg_qty(entry_px)
    if q <= 0 or mid_now <= 0:
        return 0.0
    diff = mid_now - entry_px
    return (diff if side == "buy" else -diff) * q


def pooled_equity(upnl_hl: float, upnl_b: float,
                  funding_net: float) -> float:
    """Pooled perp equity: shared margin + both legs' uPnL + funding."""
    return spec.PERP_CAPITAL_FRAC * spec.NOTIONAL_USD \
        + upnl_hl + upnl_b + funding_net


def pooled_dead(equity: float) -> bool:
    """Death when equity <= maintenance floor (0.5% of N per leg)."""
    return equity <= spec.MM_FLOOR_PER_LEG * spec.NOTIONAL_USD * 2


def yu_mtm(yu_side: str, entry_rate: float, mid_rate: float,
           t_rem_years: float) -> float:
    """MTM of one Boros YU leg: rate move vs the locked entry over the
    remaining time, signed by position (a short YU received the entry
    fixed and loses when the mid rate rises)."""
    move = mid_rate - entry_rate
    return (move if yu_side == "buy" else -move) \
        * spec.NOTIONAL_USD * t_rem_years


def boros_dead(mtm_usd: float) -> bool:
    """Death when a Boros leg's MTM loss >= its 0.1*N margin."""
    return mtm_usd <= -spec.BOROS_LEG_MARGIN * spec.NOTIONAL_USD


def adverse_frac(side: str, entry_px: float, mid_now: float) -> float:
    """Adverse mid move since entry as a fraction of entry price —
    positive when the move hurts the leg (up for shorts, down for longs).
    """
    if entry_px <= 0 or mid_now <= 0:
        return 0.0
    diff = (mid_now - entry_px) / entry_px
    return diff if side == "sell" else -diff


def split_liq(adverse: float) -> bool:
    """SPLIT descriptive flag: adverse excursion >= 30% would liquidate
    a 3x isolated leg (kept for the report, never a kill)."""
    return adverse >= spec.SPLIT_ADVERSE_FRAC


def exit_slip(close_side: str, book, mid: float) -> Optional[float]:
    """VWAP slip (bps) to CLOSE a perp leg on this pass's book."""
    from src.boros_shadow.books import vwap_slip_bps
    if book is None or mid is None or mid <= 0:
        return None
    return vwap_slip_bps(close_side, spec.NOTIONAL_USD, book, mid)
