"""MFE / MAE analytics — intrade metrics vs post-exit analytical pass (Phase 07).

``post_exit_*`` fields are computed ONLY in :func:`enrich_trades_post_exit`
after the backtest decision loop completes. They must never appear on trade
dicts consumed during replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.utils.helpers import safe_divide, safe_float

POST_EXIT_HORIZONS_MS = (
    ("post_exit_15m", 15 * 60_000),
    ("post_exit_1h", 60 * 60_000),
    ("post_exit_4h", 4 * 60 * 60_000),
)

# Keys that must never be present on in-run trade records.
POST_EXIT_KEYS = frozenset({
    "post_exit_15m_pct",
    "post_exit_1h_pct",
    "post_exit_4h_pct",
})


@dataclass
class ExcursionTracker:
    """Running MFE/MAE state for one open position."""

    entry_price: float
    entry_time_ms: int
    side: str
    risk_usd: float
    mfe_price: float = 0.0
    mae_price: float = 0.0
    mfe_ts: int = 0
    mae_ts: int = 0

    def __post_init__(self) -> None:
        self.mfe_price = self.entry_price
        self.mae_price = self.entry_price
        self.mfe_ts = self.entry_time_ms
        self.mae_ts = self.entry_time_ms

    def update_bar(self, high: float, low: float, ts_ms: int) -> None:
        if self.side == "long":
            if high > self.mfe_price:
                self.mfe_price = high
                self.mfe_ts = ts_ms
            if low < self.mae_price:
                self.mae_price = low
                self.mae_ts = ts_ms
        else:
            if low < self.mfe_price:
                self.mfe_price = low
                self.mfe_ts = ts_ms
            if high > self.mae_price:
                self.mae_price = high
                self.mae_ts = ts_ms

    def favorable_usd(self, size: float) -> float:
        if self.side == "long":
            return (self.mfe_price - self.entry_price) * size
        return (self.entry_price - self.mfe_price) * size

    def adverse_usd(self, size: float) -> float:
        if self.side == "long":
            return (self.entry_price - self.mae_price) * size
        return (self.mae_price - self.entry_price) * size


def build_price_index(
    timeline: Sequence[Tuple[int, str, Any]],
) -> Dict[str, List[Tuple[int, float]]]:
    """Per-symbol sorted (timestamp_ms, close) series from backtest timeline."""
    out: Dict[str, List[Tuple[int, float]]] = {}
    for ts, sym, c1m in timeline:
        close = safe_float(getattr(c1m, "close", 0.0), 0.0)
        if close <= 0:
            continue
        out.setdefault(sym, []).append((ts, close))
    for sym in out:
        out[sym].sort(key=lambda x: x[0])
    return out


def _price_at_or_after(
    series: List[Tuple[int, float]],
    target_ms: int,
) -> Optional[float]:
    for ts, px in series:
        if ts >= target_ms:
            return px
    return None


def compute_intrade_excursion_fields(
    tracker: ExcursionTracker,
    *,
    size: float,
    net_pnl_usd: float,
    fees_paid: float,
) -> Dict[str, Any]:
    """MFE/MAE metrics using only data from entry through exit (no lookahead)."""
    risk = tracker.risk_usd
    mfe_usd = tracker.favorable_usd(size)
    mae_usd = tracker.adverse_usd(size)
    mfe_r = safe_divide(mfe_usd, risk, 0.0) if risk > 0 else 0.0
    mae_r = safe_divide(mae_usd, risk, 0.0) if risk > 0 else 0.0
    giveback_usd = max(0.0, mfe_usd - max(net_pnl_usd, 0.0))
    giveback_r = safe_divide(giveback_usd, risk, 0.0) if risk > 0 else 0.0
    return {
        "mfe_usd": round(mfe_usd, 4),
        "mae_usd": round(mae_usd, 4),
        "mfe_r": round(mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "time_to_mfe_ms": max(0, tracker.mfe_ts - tracker.entry_time_ms),
        "time_to_mae_ms": max(0, tracker.mae_ts - tracker.entry_time_ms),
        "giveback_usd": round(giveback_usd, 4),
        "giveback_r": round(giveback_r, 4),
        "total_costs_usd": round(fees_paid, 4),
    }


def compute_post_exit_fields(
    *,
    side: str,
    exit_price: float,
    exit_time_ms: int,
    price_index: Dict[str, List[Tuple[int, float]]],
    symbol: str,
) -> Dict[str, Optional[float]]:
    """Post-exit horizon returns — analytical only, never for decisions."""
    series = price_index.get(symbol, [])
    out: Dict[str, Optional[float]] = {}
    for key, horizon in POST_EXIT_HORIZONS_MS:
        px = _price_at_or_after(series, exit_time_ms + horizon)
        if px is None or exit_price <= 0:
            out[f"{key}_pct"] = None
            continue
        if side == "long":
            out[f"{key}_pct"] = round((px - exit_price) / exit_price, 6)
        else:
            out[f"{key}_pct"] = round((exit_price - px) / exit_price, 6)
    return out


def strip_post_exit_from_trades(
    trades: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Ensure in-run trade dicts carry no post-exit lookahead fields."""
    cleaned: List[Dict[str, Any]] = []
    for t in trades:
        row = {k: v for k, v in t.items() if k not in POST_EXIT_KEYS}
        cleaned.append(row)
    return cleaned


def enrich_trades_post_exit(
    trades: Sequence[Dict[str, Any]],
    price_index: Dict[str, List[Tuple[int, float]]],
) -> List[Dict[str, Any]]:
    """Post-backtest analytical enrichment — separate from decision pipeline."""
    enriched: List[Dict[str, Any]] = []
    for t in trades:
        base = {k: v for k, v in t.items() if k not in POST_EXIT_KEYS}
        post = compute_post_exit_fields(
            side=str(t.get("side", "long")),
            exit_price=float(t.get("exit_price", 0.0)),
            exit_time_ms=int(t.get("exit_time", 0)),
            price_index=price_index,
            symbol=str(t.get("symbol", "")),
        )
        row = {**base, **post}
        enriched.append(row)
    return enriched
