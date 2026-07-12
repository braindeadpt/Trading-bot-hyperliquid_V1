"""Parity audit between GoldRush and official Hyperliquid candles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.data.candle_providers.tick_meta import (
    format_hl_price,
    load_meta_cache_from_meta_response,
    price_match_ticks,
    tick_size_for,
)
from src.utils.helpers import safe_float

VOLUME_REL_TOLERANCE = 1e-6
VOLUME_ABS_TOLERANCE = 1e-8


@dataclass
class CandleMismatch:
    timestamp_ms: int
    field: str
    official: Any
    goldrush: Any
    delta: float
    delta_ticks: float = 0.0


@dataclass
class ParityReport:
    """Overlap comparison between official and GoldRush rows."""

    symbol: str
    interval: str
    overlap_start_ms: int
    overlap_end_ms: int
    official_bars: int
    goldrush_bars: int
    matched_bars: int
    ohlc_identical: int
    ohlc_within_tolerance: int
    volume_within_tolerance: int
    official_only: int
    goldrush_only: int
    mismatches: List[CandleMismatch] = field(default_factory=list)
    passed: bool = False
    tick_size: float = 0.0
    tolerance_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "overlap_start_ms": self.overlap_start_ms,
            "overlap_end_ms": self.overlap_end_ms,
            "official_bars": self.official_bars,
            "goldrush_bars": self.goldrush_bars,
            "matched_bars": self.matched_bars,
            "ohlc_identical": self.ohlc_identical,
            "ohlc_within_tolerance": self.ohlc_within_tolerance,
            "volume_within_tolerance": self.volume_within_tolerance,
            "official_only": self.official_only,
            "goldrush_only": self.goldrush_only,
            "mismatch_count": len(self.mismatches),
            "mismatches_sample": [m.__dict__ for m in self.mismatches[:20]],
            "passed": self.passed,
            "tick_size": self.tick_size,
            "tolerance_note": self.tolerance_note,
        }


def _row_index(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(r["T"]): r for r in rows}


def _price_match(
    symbol: str,
    a: Any,
    b: Any,
    *,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
) -> Tuple[bool, bool, float]:
    return price_match_ticks(symbol, a, b, meta_cache)


def _volume_match(a: Any, b: Any) -> bool:
    fa = safe_float(a)
    fb = safe_float(b)
    if fa == fb:
        return True
    if abs(fa - fb) <= VOLUME_ABS_TOLERANCE:
        return True
    denom = max(abs(fa), abs(fb), VOLUME_ABS_TOLERANCE)
    return abs(fa - fb) / denom <= VOLUME_REL_TOLERANCE


def compare_candle_overlap(
    official_rows: List[Dict[str, Any]],
    goldrush_rows: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
) -> ParityReport:
    """Compare overlapping close timestamps between providers."""
    sym = symbol.upper()
    off_idx = _row_index(official_rows)
    gr_idx = _row_index(goldrush_rows)
    overlap_keys_preview = sorted(set(off_idx) & set(gr_idx))
    ref_close = (
        safe_float(off_idx[overlap_keys_preview[-1]].get("c"))
        if overlap_keys_preview
        else None
    )
    tick = tick_size_for(sym, meta_cache, reference_price=ref_close)
    tolerance_note = (
        f"OHLC within 1 dynamic HL quantum ({tick}); "
        f"volume rel tol={VOLUME_REL_TOLERANCE} abs tol={VOLUME_ABS_TOLERANCE}"
    )
    if not off_idx or not gr_idx:
        return ParityReport(
            symbol=sym,
            interval=interval,
            overlap_start_ms=0,
            overlap_end_ms=0,
            official_bars=len(off_idx),
            goldrush_bars=len(gr_idx),
            matched_bars=0,
            ohlc_identical=0,
            ohlc_within_tolerance=0,
            volume_within_tolerance=0,
            official_only=len(off_idx),
            goldrush_only=len(gr_idx),
            passed=False,
            tick_size=tick,
            tolerance_note=tolerance_note,
        )

    overlap_keys = sorted(set(off_idx) & set(gr_idx))
    overlap_start = overlap_keys[0] if overlap_keys else 0
    overlap_end = overlap_keys[-1] if overlap_keys else 0
    official_only = len(set(off_idx) - set(gr_idx))
    goldrush_only = len(set(gr_idx) - set(off_idx))

    mismatches: List[CandleMismatch] = []
    ohlc_identical = 0
    ohlc_tol = 0
    vol_tol = 0

    for ts in overlap_keys:
        o_row = off_idx[ts]
        g_row = gr_idx[ts]
        row_identical = True
        row_tol = True
        for fld in ("o", "h", "l", "c"):
            identical, within, delta_ticks = _price_match(
                sym, o_row.get(fld), g_row.get(fld), meta_cache=meta_cache,
            )
            if not identical:
                row_identical = False
            if not within:
                row_tol = False
                mismatches.append(
                    CandleMismatch(
                        timestamp_ms=ts,
                        field=fld,
                        official=o_row.get(fld),
                        goldrush=g_row.get(fld),
                        delta=abs(
                            format_hl_price(o_row.get(fld), sym, meta_cache)
                            - format_hl_price(g_row.get(fld), sym, meta_cache)
                        ),
                        delta_ticks=delta_ticks,
                    ),
                )
        if row_identical:
            ohlc_identical += 1
        elif row_tol:
            ohlc_tol += 1
        if _volume_match(o_row.get("v"), g_row.get("v")):
            vol_tol += 1
        elif row_tol:
            mismatches.append(
                CandleMismatch(
                    timestamp_ms=ts,
                    field="v",
                    official=o_row.get("v"),
                    goldrush=g_row.get("v"),
                    delta=abs(safe_float(o_row.get("v")) - safe_float(g_row.get("v"))),
                ),
            )

    passed = (
        len(overlap_keys) > 0
        and ohlc_identical + ohlc_tol == len(overlap_keys)
        and vol_tol == len(overlap_keys)
    )

    return ParityReport(
        symbol=sym,
        interval=interval,
        overlap_start_ms=overlap_start,
        overlap_end_ms=overlap_end,
        official_bars=len(off_idx),
        goldrush_bars=len(gr_idx),
        official_only=official_only,
        goldrush_only=goldrush_only,
        matched_bars=len(overlap_keys),
        ohlc_identical=ohlc_identical,
        ohlc_within_tolerance=ohlc_tol,
        volume_within_tolerance=vol_tol,
        mismatches=mismatches,
        passed=passed,
        tick_size=tick,
        tolerance_note=tolerance_note,
    )
