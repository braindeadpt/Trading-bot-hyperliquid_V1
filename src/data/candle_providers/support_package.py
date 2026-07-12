"""Credential-free support package for GoldRush parity escalations."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.helpers import safe_write_file, validate_safe_path

_SECRET_PATTERNS = (
    re.compile(r"cqt_[A-Za-z0-9]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"GOLDRUSH_API_KEY\s*=\s*\S+"),
)


def redact_secrets(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_secrets(obj)
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    return obj


def collect_support_entries(
    secondary_report: Dict[str, Any],
    *,
    max_per_comparison: int = 10,
) -> List[Dict[str, Any]]:
    """Extract divergent wire samples for vendor support."""
    entries: List[Dict[str, Any]] = []
    for sym_block in secondary_report.get("series", []):
        symbol = sym_block.get("symbol", "")
        for comp in sym_block.get("comparisons", []):
            kind = comp.get("kind", "")
            interval = comp.get("interval", "")
            for sample in comp.get("divergent_samples", [])[:max_per_comparison]:
                entries.append({
                    "symbol": symbol,
                    "interval": interval,
                    "comparison": kind,
                    "timestamp_ms": sample.get("timestamp_ms"),
                    "field": sample.get("field"),
                    "goldrush_or_left": sample.get("left_raw"),
                    "official_or_right": sample.get("right_raw"),
                    "left_label": sample.get("left_label"),
                    "right_label": sample.get("right_label"),
                    "abs_delta": sample.get("abs_delta"),
                    "rel_delta": sample.get("rel_delta"),
                    "delta_ticks": sample.get("delta_ticks"),
                })
    return entries


def write_support_package(
    secondary_report: Dict[str, Any],
    out_dir: Path,
    *,
    window_start_ms: Optional[int] = None,
    meta_cache: Optional[Dict[str, Dict[str, int]]] = None,
    project_root: Optional[Path] = None,
) -> Path:
    """Write redacted JSON bundle for GoldRush support (no credentials)."""
    root = project_root or Path(__file__).resolve().parents[3]
    rel = out_dir.relative_to(root) if out_dir.is_absolute() else out_dir
    safe = validate_safe_path(rel.as_posix())
    if safe is None:
        raise RuntimeError(f"Unsafe support package directory: {out_dir}")
    safe.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    entries = collect_support_entries(secondary_report)
    package = _redact_obj({
        "package_type": "goldrush_parity_support",
        "generated_at_ms": int(time.time() * 1000),
        "credentials_included": False,
        "validation_summary": secondary_report.get("summary"),
        "all_passed": secondary_report.get("all_passed"),
        "window_end_ms": secondary_report.get("window_end_ms"),
        "window_start_ms": window_start_ms,
        "gap_intervals": secondary_report.get("gap_intervals"),
        "tolerance": secondary_report.get("tolerance"),
        "meta_cache": meta_cache or {},
        "divergent_entries": entries,
        "entry_count": len(entries),
        "node_trades_reconstruction": secondary_report.get("node_trades_reconstruction"),
        "instructions": (
            "Compare listed timestamps against HL candleSnapshot and GoldRush HyperCore "
            "for the same coin/interval. Raw wire rows are in divergent_samples of the "
            "full secondary report."
        ),
    })

    path = safe / f"goldrush_support_package_{ts}.json"
    latest = safe / "goldrush_support_package_latest.json"
    body = json.dumps(package, indent=2, sort_keys=True)
    if not safe_write_file(path, body):
        raise RuntimeError(f"Failed to write {path}")
    if not safe_write_file(latest, body):
        raise RuntimeError(f"Failed to write {latest}")
    return path
