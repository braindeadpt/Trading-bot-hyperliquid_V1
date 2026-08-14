"""Liquidation stop-out decision — shared by live engine and backtest replay.

A liquidation flush of the **same side as the position** (longs liquidated
while we are long, shorts liquidated while we are short) is forced selling /
buying that runs **against** the open position. The real liquidation window
*validates the side*: it confirms the adverse move is driven by genuine
forced unwinds, not noise. When that window shows dominant notional on our
own side above a floor, the position is stopped out.

This is a pure function on ``(position_side, liq_side, liq_notional)`` — the
live engine derives those from its rolling accumulator
(``TradingEngine._get_liquidation_stats``), the backtest replay derives them
from the same ``LiquidationAccumulator`` fed by persisted rows
(``_advance_liquidation_replay``). Because both paths call this one function,
the replay is guaranteed to replicate the live decision for the same window
state — real or proxy provenance, same numbers → same decision.

Provenance note: the decision itself is provenance-agnostic (it is a function
of notional + side). Provenance is a **separate** concern: the live engine
``real`` mode already refuses proxy events before they ever enter the window
(``_accepts_liquidation_source``), and the backtest replays the stored label
verbatim. ``is_real_liquidation_source`` stays the entry-chain gate; this
module never duplicates it.
"""

from __future__ import annotations

from typing import Optional

# Floor (USD) for the dominant liquidation notional before a stop-out fires.
# Kept in code (hash-neutral) — recalibrating it is a reviewed decision, not a
# runtime knob.
#
# Calibrated 2026-08-14 against the real multi-venue 5m window (okx+bybit,
# 13,268 events, 6,651 samples) by scripts/calibrate_liquidation_stopout_floor.py:
# the p90 of the dominant window notional is 2.47M USD (p95 6.98M, p99 45.1M).
# The previous 5.0M sat ~2x above that p90 — super-calibrated, it would almost
# never fire. 2.5M keeps the p90 gate (only the top ~10% of windows exceed it)
# while making the stop-out effective. Full distribution and the per-symbol
# sensitivity in docs/LIQUIDATION_STOPOUT_FLOOR_CALIBRATION.md.
LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD = 2_500_000.0

STOPOUT_REASON = "liquidation_stop_out"


def liquidation_stopout_decision(
    position_side: Optional[str],
    liq_side: Optional[str],
    liq_notional: Optional[float],
    min_notional_usd: float = LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD,
) -> bool:
    """Decide whether the liquidation window validates a stop-out.

    True when the dominant 5m liquidation side equals the position side with
    notional at/above the floor. ``None`` window state (no events yet) never
    stops a position out — cold start must not fake a flush.
    """
    if not position_side or not liq_side or liq_notional is None:
        return False
    if float(liq_notional) < float(min_notional_usd):
        return False
    return str(liq_side).strip().lower() == str(position_side).strip().lower()
