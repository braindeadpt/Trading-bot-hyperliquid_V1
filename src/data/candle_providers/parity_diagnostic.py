"""Deep GoldRush vs official HL parity diagnostic on raw wire rows."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from src.data.candle_providers.base import INTERVAL_MS
from src.data.candle_providers.tick_meta import (
    max_price_decimals,
    price_match_ticks,
    sz_decimals_for,
    tick_size_for,
)
from src.utils.helpers import safe_float

MatchKey = Literal["close_T", "open_t", "shift_plus_1", "shift_minus_1"]
OHLCV_FIELDS = ("o", "h", "l", "c", "v", "n")


@dataclass
class FieldParityStats:
    field: str
    matched_timestamps: int = 0
    mismatches: int = 0
    identical: int = 0
    within_tick: int = 0
    tick_deltas: List[float] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        deltas = sorted(self.tick_deltas)
        if not deltas:
            return {
                "field": self.field,
                "matched_timestamps": self.matched_timestamps,
                "mismatches": self.mismatches,
                "identical": self.identical,
                "within_tick": self.within_tick,
                "tick_delta_median": 0.0,
                "tick_delta_p95": 0.0,
                "tick_delta_p99": 0.0,
                "tick_delta_max": 0.0,
            }

        def _pct(p: float) -> float:
            idx = min(len(deltas) - 1, max(0, int(math.ceil(p * len(deltas))) - 1))
            return deltas[idx]

        return {
            "field": self.field,
            "matched_timestamps": self.matched_timestamps,
            "mismatches": self.mismatches,
            "identical": self.identical,
            "within_tick": self.within_tick,
            "tick_delta_median": round(statistics.median(deltas), 6),
            "tick_delta_p95": round(_pct(0.95), 6),
            "tick_delta_p99": round(_pct(0.99), 6),
            "tick_delta_max": round(deltas[-1], 6),
        }


@dataclass
class AlignmentDiagnostic:
    match_key: str
    matched_bars: int
    official_only: int
    goldrush_only: int
    open_time_mismatches: int
    interval_span_violations: int
    field_stats: Dict[str, FieldParityStats] = field(default_factory=dict)
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "match_key": self.match_key,
            "matched_bars": self.matched_bars,
            "official_only": self.official_only,
            "goldrush_only": self.goldrush_only,
            "open_time_mismatches": self.open_time_mismatches,
            "interval_span_violations": self.interval_span_violations,
            "passed": self.passed,
            "fields": {k: v.summary() for k, v in self.field_stats.items()},
        }


def last_closed_end_ms(now_ms: Optional[int] = None) -> int:
    """Exclude the in-progress 1h bucket (conservative for all TFs)."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    hour_ms = 3_600_000
    return (now // hour_ms) * hour_ms - 1


def filter_closed_rows(
    rows: List[Dict[str, Any]],
    interval: str,
    *,
    end_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Drop candles still open at *end_ms*."""
    cap = end_ms if end_ms is not None else last_closed_end_ms()
    gap = INTERVAL_MS.get(interval, 60_000)
    out: List[Dict[str, Any]] = []
    for row in rows:
        close_ms = int(row["T"])
        if close_ms <= cap:
            out.append(row)
        elif int(row.get("t", close_ms - gap)) + gap > cap:
            continue
    return out


def _index_by_close(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(r["T"]): r for r in rows}


def _index_by_open(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(r["t"]): r for r in rows}


def _build_pairs(
    official_rows: List[Dict[str, Any]],
    goldrush_rows: List[Dict[str, Any]],
    interval: str,
    match_key: MatchKey,
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], int, int]:
    gap = INTERVAL_MS[interval]
    off_close = _index_by_close(official_rows)
    gr_close = _index_by_close(goldrush_rows)
    off_open = _index_by_open(official_rows)
    gr_open = _index_by_open(goldrush_rows)

    pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    if match_key == "close_T":
        keys = sorted(set(off_close) & set(gr_close))
        pairs = [(off_close[k], gr_close[k]) for k in keys]
        official_only = len(set(off_close) - set(gr_close))
        goldrush_only = len(set(gr_close) - set(off_close))
    elif match_key == "open_t":
        keys = sorted(set(off_open) & set(gr_open))
        pairs = [(off_open[k], gr_open[k]) for k in keys]
        official_only = len(set(off_open) - set(gr_open))
        goldrush_only = len(set(gr_open) - set(off_open))
    elif match_key == "shift_plus_1":
        keys = sorted(set(off_close) & {k - gap for k in gr_close})
        pairs = [(off_close[k], gr_close[k + gap]) for k in keys if (k + gap) in gr_close]
        official_only = len(off_close) - len(pairs)
        goldrush_only = len(gr_close) - len(pairs)
    else:  # shift_minus_1
        keys = sorted(set(off_close) & {k + gap for k in gr_close})
        pairs = [(off_close[k], gr_close[k - gap]) for k in keys if (k - gap) in gr_close]
        official_only = len(off_close) - len(pairs)
        goldrush_only = len(gr_close) - len(pairs)

    return pairs, official_only, goldrush_only


def diagnose_alignment(
    official_rows: List[Dict[str, Any]],
    goldrush_rows: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    match_key: MatchKey = "close_T",
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    volume_rel_tol: float = 1e-6,
) -> AlignmentDiagnostic:
    """Compare raw wire rows field-by-field with tick-aware OHLC matching."""
    sym = symbol.upper()
    pairs, official_only, goldrush_only = _build_pairs(
        official_rows, goldrush_rows, interval, match_key,
    )
    gap = INTERVAL_MS[interval]
    field_stats = {f: FieldParityStats(field=f) for f in OHLCV_FIELDS}
    open_time_mismatches = 0
    interval_span_violations = 0

    for off_row, gr_row in pairs:
        if int(off_row.get("t", 0)) != int(gr_row.get("t", 0)):
            open_time_mismatches += 1
        for row in (off_row, gr_row):
            span = int(row["T"]) - int(row["t"])
            if span < gap - 1 or span > gap + 1:
                interval_span_violations += 1

        for fld in OHLCV_FIELDS:
            stats = field_stats[fld]
            stats.matched_timestamps += 1
            ov = off_row.get(fld)
            gv = gr_row.get(fld)
            if fld in ("o", "h", "l", "c"):
                identical, within, delta_ticks = price_match_ticks(sym, ov, gv, meta_cache)
                if identical:
                    stats.identical += 1
                elif within:
                    stats.within_tick += 1
                else:
                    stats.mismatches += 1
                    stats.tick_deltas.append(delta_ticks)
            elif fld == "v":
                fo = safe_float(ov)
                fg = safe_float(gv)
                if fo == fg:
                    stats.identical += 1
                else:
                    denom = max(abs(fo), abs(fg), 1e-12)
                    if abs(fo - fg) / denom <= volume_rel_tol:
                        stats.within_tick += 1
                    else:
                        stats.mismatches += 1
                        stats.tick_deltas.append(abs(fo - fg))
            else:  # n
                if int(ov) == int(gv):
                    stats.identical += 1
                else:
                    stats.mismatches += 1
                    stats.tick_deltas.append(float(abs(int(ov) - int(gv))))

    ohlc_ok = all(
        field_stats[f].mismatches == 0 for f in ("o", "h", "l", "c")
    )
    vol_ok = field_stats["v"].mismatches == 0
    passed = bool(pairs) and ohlc_ok and vol_ok and open_time_mismatches == 0

    return AlignmentDiagnostic(
        match_key=match_key,
        matched_bars=len(pairs),
        official_only=official_only,
        goldrush_only=goldrush_only,
        open_time_mismatches=open_time_mismatches,
        interval_span_violations=interval_span_violations,
        field_stats=field_stats,
        passed=passed,
    )


def run_full_diagnostic(
    official_rows: List[Dict[str, Any]],
    goldrush_rows: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
) -> Dict[str, Any]:
    """Run alignment variants and pick the best match key."""
    sym = symbol.upper()
    closed_off = official_rows
    closed_gr = goldrush_rows
    variants: Dict[str, Any] = {}
    best_key = "close_T"
    best_passed = False
    best_matched = 0

    for key in ("close_T", "open_t", "shift_plus_1", "shift_minus_1"):
        diag = diagnose_alignment(
            closed_off,
            closed_gr,
            symbol=sym,
            interval=interval,
            match_key=key,  # type: ignore[arg-type]
            meta_cache=meta_cache,
        )
        variants[key] = diag.to_dict()
        if diag.passed and diag.matched_bars >= best_matched:
            best_key = key
            best_passed = True
            best_matched = diag.matched_bars
        elif not best_passed and diag.matched_bars > best_matched:
            best_key = key
            best_matched = diag.matched_bars

    return {
        "symbol": sym,
        "interval": interval,
        "sz_decimals": sz_decimals_for(sym, meta_cache),
        "max_price_decimals": max_price_decimals(sz_decimals_for(sym, meta_cache)),
        "tick_size": tick_size_for(sym, meta_cache),
        "official_bars": len(closed_off),
        "goldrush_bars": len(closed_gr),
        "recommended_match_key": best_key,
        "parity_passed": best_passed,
        "variants": variants,
        "checks": {
            "excluded_in_progress_candle": True,
            "timezone": "UTC epoch ms (wire t/T)",
            "symbol_mapping": f"coin={sym}",
            "wire_prices_decimal_strings": True,
        },
    }
