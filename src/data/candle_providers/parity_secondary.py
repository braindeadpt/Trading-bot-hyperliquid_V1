"""Secondary GoldRush validation: 1m parity, local rollup, support export."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from src.data.candle_providers.candle_rollup import rollup_1m_all_targets, rollup_1m_to_interval
from src.data.candle_providers.parity import compare_candle_overlap
from src.data.candle_providers.parity_diagnostic import (
    diagnose_alignment,
    filter_closed_rows,
    last_closed_end_ms,
    run_full_diagnostic,
)
from src.data.candle_providers.tick_meta import (
    dynamic_quantum_for_price,
    format_hl_price,
    price_match_ticks,
    sz_decimals_for,
    tick_size_for,
)
from src.utils.helpers import safe_float

ComparisonKind = Literal[
    "gr_1m_vs_hl_1m",
    "gr_rollup_vs_gr_direct",
    "gr_rollup_vs_hl_official",
]

VOLUME_REL_TOLERANCE = 1e-6
VOLUME_ABS_TOLERANCE = 1e-8
COUNT_ABS_TOLERANCE = 0


def safe_relative_delta(a: float, b: float) -> float:
    """Relative |a-b|/denom with stable near-zero denominator."""
    denom = max(abs(a), abs(b), VOLUME_ABS_TOLERANCE)
    return abs(a - b) / denom


@dataclass
class NumericFieldStats:
    field: str
    matched: int = 0
    identical: int = 0
    mismatches: int = 0
    abs_deltas: List[float] = field(default_factory=list)
    rel_deltas: List[float] = field(default_factory=list)
    tick_deltas: List[float] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        def _pct(vals: List[float], p: float) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            idx = min(len(s) - 1, max(0, int(math.ceil(p * len(s))) - 1))
            return round(s[idx], 8)

        out: Dict[str, Any] = {
            "field": self.field,
            "matched": self.matched,
            "identical": self.identical,
            "mismatches": self.mismatches,
        }
        if self.tick_deltas:
            out.update({
                "tick_delta_median": round(statistics.median(self.tick_deltas), 6),
                "tick_delta_p95": _pct(self.tick_deltas, 0.95),
                "tick_delta_p99": _pct(self.tick_deltas, 0.99),
                "tick_delta_max": round(max(self.tick_deltas), 6),
            })
        if self.abs_deltas:
            out.update({
                "abs_delta_median": round(statistics.median(self.abs_deltas), 8),
                "abs_delta_p95": _pct(self.abs_deltas, 0.95),
                "abs_delta_p99": _pct(self.abs_deltas, 0.99),
                "abs_delta_max": round(max(self.abs_deltas), 8),
            })
        if self.rel_deltas:
            out.update({
                "rel_delta_median": round(statistics.median(self.rel_deltas), 8),
                "rel_delta_p95": _pct(self.rel_deltas, 0.95),
                "rel_delta_p99": _pct(self.rel_deltas, 0.99),
                "rel_delta_max": round(max(self.rel_deltas), 8),
            })
        return out


@dataclass
class DivergentSample:
    timestamp_ms: int
    field: str
    left_label: str
    right_label: str
    left_raw: Any
    right_raw: Any
    abs_delta: float
    rel_delta: float
    delta_ticks: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_ms": self.timestamp_ms,
            "field": self.field,
            "left_label": self.left_label,
            "right_label": self.right_label,
            "left_raw": self.left_raw,
            "right_raw": self.right_raw,
            "abs_delta": self.abs_delta,
            "rel_delta": self.rel_delta,
            "delta_ticks": self.delta_ticks,
        }


def _index_close(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {int(r["T"]): r for r in rows}


def compare_wire_rows(
    left_rows: List[Dict[str, Any]],
    right_rows: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    left_label: str,
    right_label: str,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    max_samples: int = 50,
) -> Dict[str, Any]:
    """Field-level comparison with dynamic quantum and volume/count stats."""
    sym = symbol.upper()
    left_idx = _index_close(left_rows)
    right_idx = _index_close(right_rows)
    keys = sorted(set(left_idx) & set(right_idx))

    fields = {f: NumericFieldStats(field=f) for f in ("o", "h", "l", "c", "v", "n")}
    samples: List[DivergentSample] = []
    ohlc_ok = True
    vol_ok = True
    count_ok = True

    for ts in keys:
        l_row = left_idx[ts]
        r_row = right_idx[ts]
        for fld in ("o", "h", "l", "c"):
            st = fields[fld]
            st.matched += 1
            lv, rv = l_row.get(fld), r_row.get(fld)
            identical, within, delta_ticks = price_match_ticks(sym, lv, rv, meta_cache)
            fa = format_hl_price(lv, sym, meta_cache)
            fb = format_hl_price(rv, sym, meta_cache)
            abs_d = abs(fa - fb)
            rel_d = safe_relative_delta(fa, fb)
            if identical:
                st.identical += 1
            elif within:
                st.mismatches += 1
                st.tick_deltas.append(delta_ticks)
                st.abs_deltas.append(abs_d)
                st.rel_deltas.append(rel_d)
            else:
                ohlc_ok = False
                st.mismatches += 1
                st.tick_deltas.append(delta_ticks)
                st.abs_deltas.append(abs_d)
                st.rel_deltas.append(rel_d)
                if len(samples) < max_samples:
                    samples.append(DivergentSample(
                        ts, fld, left_label, right_label, lv, rv, abs_d, rel_d, delta_ticks,
                    ))

        for fld, tol_ok_flag in (("v", "vol"), ("n", "count")):
            st = fields[fld]
            st.matched += 1
            la = safe_float(l_row.get(fld)) if fld == "v" else float(int(l_row.get(fld, 0)))
            ra = safe_float(r_row.get(fld)) if fld == "v" else float(int(r_row.get(fld, 0)))
            abs_d = abs(la - ra)
            rel_d = safe_relative_delta(la, ra)
            if la == ra:
                st.identical += 1
            elif fld == "v" and (
                abs_d <= VOLUME_ABS_TOLERANCE or rel_d <= VOLUME_REL_TOLERANCE
            ):
                pass
            elif fld == "n" and abs_d <= COUNT_ABS_TOLERANCE:
                st.identical += 1
            else:
                if fld == "v":
                    vol_ok = False
                else:
                    count_ok = False
                st.mismatches += 1
                st.abs_deltas.append(abs_d)
                st.rel_deltas.append(rel_d)
                if len(samples) < max_samples:
                    samples.append(DivergentSample(
                        ts, fld, left_label, right_label,
                        l_row.get(fld), r_row.get(fld), abs_d, rel_d,
                    ))

    passed = bool(keys) and ohlc_ok and vol_ok and count_ok
    ref_price = 0.0
    if keys:
        ref_price = safe_float(left_idx[keys[-1]].get("c"))
    return {
        "comparison": f"{left_label}_vs_{right_label}",
        "symbol": sym,
        "interval": interval,
        "left_label": left_label,
        "right_label": right_label,
        "matched_bars": len(keys),
        "left_only": len(set(left_idx) - set(right_idx)),
        "right_only": len(set(right_idx) - set(left_idx)),
        "passed": passed,
        "tolerance_note": (
            "OHLC within 1 dynamic HL quantum (5 sig figs); "
            f"volume rel<={VOLUME_REL_TOLERANCE} abs<={VOLUME_ABS_TOLERANCE}; "
            f"trade_count exact"
        ),
        "dynamic_quantum_at_ref_close": dynamic_quantum_for_price(ref_price, sym, meta_cache),
        "fields": {k: v.summary() for k, v in fields.items()},
        "divergent_samples": [s.to_dict() for s in samples],
    }


def run_secondary_validation(
    *,
    symbol: str,
    gr_1m: List[Dict[str, Any]],
    hl_1m: List[Dict[str, Any]],
    gr_direct: Dict[str, List[Dict[str, Any]]],
    hl_official: Dict[str, List[Dict[str, Any]]],
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    gap_intervals: int = 2,
    gap_intervals_by_tf: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Run all three comparison families for one symbol."""
    sym = symbol.upper()
    rollups = rollup_1m_all_targets(
        gr_1m,
        symbol=sym,
        gap_intervals=gap_intervals,
        gap_intervals_by_tf=gap_intervals_by_tf,
    )

    comparisons: List[Dict[str, Any]] = []

    gr1_diag = run_full_diagnostic(hl_1m, gr_1m, symbol=sym, interval="1m", meta_cache=meta_cache)
    gr1_parity = compare_candle_overlap(
        hl_1m, gr_1m, symbol=sym, interval="1m", meta_cache=meta_cache,
    )
    comparisons.append({
        "kind": "gr_1m_vs_hl_1m",
        "interval": "1m",
        **compare_wire_rows(
            gr_1m, hl_1m, symbol=sym, interval="1m",
            left_label="goldrush_1m", right_label="hl_1m", meta_cache=meta_cache,
        ),
        "diagnostic": gr1_diag,
        "tick_parity_report": gr1_parity.to_dict(),
    })

    for interval in ("5m", "15m", "1h"):
        rolled = rollups.get(interval, [])
        direct = gr_direct.get(interval, [])
        official = hl_official.get(interval, [])

        comparisons.append({
            "kind": "gr_rollup_vs_gr_direct",
            "interval": interval,
            **compare_wire_rows(
                rolled, direct, symbol=sym, interval=interval,
                left_label="goldrush_rollup_1m", right_label="goldrush_direct",
                meta_cache=meta_cache,
            ),
            "rollup_bars": len(rolled),
            "direct_bars": len(direct),
        })
        comparisons.append({
            "kind": "gr_rollup_vs_hl_official",
            "interval": interval,
            **compare_wire_rows(
                rolled, official, symbol=sym, interval=interval,
                left_label="goldrush_rollup_1m", right_label="hl_official",
                meta_cache=meta_cache,
            ),
            "rollup_bars": len(rolled),
            "official_bars": len(official),
        })

    all_passed = all(c.get("passed") for c in comparisons)
    return {
        "symbol": sym,
        "sz_decimals": sz_decimals_for(sym, meta_cache),
        "rollup_counts": {tf: len(rollups.get(tf, [])) for tf in ("5m", "15m", "1h")},
        "comparisons": comparisons,
        "all_passed": all_passed,
    }


NODE_TRADES_RECONSTRUCTION_PROPOSAL = {
    "trigger": "secondary_validation_inconclusive",
    "source": "hyperliquid_node_trades_s3",
    "description": (
        "Rebuild OHLCV from official archived node trades when GoldRush wire rows "
        "diverge from HL candleSnapshot and local 1m rollup cannot explain the delta."
    ),
    "steps": [
        "Download node_trades archives for divergent windows from HL S3 (per official docs).",
        "Aggregate trades into 1m buckets using HL bucket boundaries (t/T semantics).",
        "Apply format_hl_price / dynamic quantum when emitting OHLC.",
        "Upsert research DB with source=hl_node_trades_rebuild; never overwrite protected hl_candleSnapshot rows.",
        "Re-run secondary validation on rebuilt windows only.",
    ],
    "priority_windows": "Use divergent_samples timestamps from support package.",
    "credentials": "S3 access via HL-published archive paths — no GoldRush key required.",
}


def build_secondary_report(
    series: List[Dict[str, Any]],
    *,
    sample_bars: int,
    window_end_ms: int,
    gap_intervals: int,
) -> Dict[str, Any]:
    all_passed = all(s.get("all_passed") for s in series)
    return {
        "generated_at_ms": int(time.time() * 1000),
        "validation_type": "goldrush_secondary",
        "sample_bars": sample_bars,
        "window_end_ms": window_end_ms,
        "gap_intervals": gap_intervals,
        "tolerance": "dynamic_5_sig_fig_quantum_strict_gate",
        "all_passed": all_passed,
        "oos_dataset_ready": all_passed,
        "series": series,
        "summary": {
            "total_symbols": len(series),
            "passed": sum(1 for s in series if s.get("all_passed")),
            "failed": sum(1 for s in series if not s.get("all_passed")),
        },
        "node_trades_reconstruction": (
            NODE_TRADES_RECONSTRUCTION_PROPOSAL
            if not all_passed
            else None
        ),
    }
