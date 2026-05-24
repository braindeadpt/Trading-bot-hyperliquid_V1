"""Transaction Cost Analysis (TCA) — reject signals with insufficient edge."""

from __future__ import annotations

from typing import Optional, Tuple

from src.strategies.base import Signal
from src.utils.helpers import safe_float


def estimate_expected_edge_pct(signal: Signal) -> Optional[float]:
    """Estimate expected profit as a fraction of price (e.g. 0.0025 = 0.25%).

    Uses explicit take-profit when set; otherwise derives from stop distance and R.
    """
    tp = signal.take_profit_pct
    if tp is not None and tp > 0:
        return safe_float(tp, 0.0)

    sl = safe_float(signal.stop_loss_pct, 0.0)
    if sl <= 0:
        return None

    meta = signal.metadata or {}
    r_mult = safe_float(meta.get("take_profit_r"), 0.0)
    if r_mult <= 0:
        r_mult = 1.5
    return sl * r_mult


def round_trip_cost_pct(
    taker_fee_pct: float,
    slippage_pct: float,
) -> float:
    """Total round-trip cost as fraction of notional (entry + exit) — symmetric taker."""
    fee = safe_float(taker_fee_pct, 0.0)
    slip = safe_float(slippage_pct, 0.0)
    return (2.0 * fee) + (2.0 * slip)


def round_trip_cost_from_spec(
    entry_fee_pct: float,
    exit_fee_pct: float,
    entry_slippage_pct: float,
    exit_slippage_pct: float,
) -> float:
    """Asymmetric round-trip cost (e.g. maker entry + taker exit)."""
    return (
        safe_float(entry_fee_pct, 0.0)
        + safe_float(exit_fee_pct, 0.0)
        + safe_float(entry_slippage_pct, 0.0)
        + safe_float(exit_slippage_pct, 0.0)
    )


def passes_tca_check(
    signal: Signal,
    taker_fee_pct: float,
    slippage_pct: float,
    min_edge_buffer_pct: float = 0.0005,
    *,
    entry_fee_pct: Optional[float] = None,
    exit_fee_pct: Optional[float] = None,
    entry_slippage_pct: Optional[float] = None,
    exit_slippage_pct: Optional[float] = None,
) -> Tuple[bool, str]:
    """Return (approved, reason). Rejects when expected edge <= all-in costs."""
    edge = estimate_expected_edge_pct(signal)
    if edge is None:
        return True, "tca_skipped_no_edge_estimate"

    meta = signal.metadata or {}
    ef = entry_fee_pct if entry_fee_pct is not None else safe_float(
        meta.get("entry_fee_pct"), taker_fee_pct
    )
    xf = exit_fee_pct if exit_fee_pct is not None else safe_float(
        meta.get("exit_fee_pct"), taker_fee_pct
    )
    es = entry_slippage_pct if entry_slippage_pct is not None else safe_float(
        meta.get("entry_slippage_pct"), slippage_pct
    )
    xs = exit_slippage_pct if exit_slippage_pct is not None else safe_float(
        meta.get("exit_slippage_pct"), slippage_pct
    )

    if meta.get("order_type") == "limit_maker" or entry_fee_pct is not None:
        cost = round_trip_cost_from_spec(ef, xf, es, xs)
    else:
        cost = round_trip_cost_pct(taker_fee_pct, slippage_pct)

    buffer = safe_float(min_edge_buffer_pct, 0.0)
    required = cost + buffer

    if edge <= required:
        return (
            False,
            f"TCA reject: edge {edge * 100:.3f}% <= "
            f"cost {cost * 100:.3f}% + buffer {buffer * 100:.3f}%",
        )
    return True, f"TCA ok: edge {edge * 100:.3f}% > cost {required * 100:.3f}%"
