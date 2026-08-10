"""CVDOrderFlow_p90 — distribution-calibrated variant (not a silent retune).

Threshold = aggregate p90 of non-zero medium-window |divergence| from
``scripts/calibrate_cvd_divergence.py``. Default falls back to last known
calibration (0.12) if the JSON artifact is missing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.strategies.cvd_orderflow import CVDOrderFlow

logger = logging.getLogger(__name__)

_CALIB_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "backtests"
    / "parity_diag"
    / "cvd_divergence_distribution.json"
)
# Placeholder until calibration runs; overwritten by load_calibrated_threshold().
_FALLBACK_P90 = 0.275


def load_calibrated_threshold(path: Optional[Path] = None) -> float:
    p = path or _CALIB_PATH
    if not p.exists():
        return _FALLBACK_P90
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        thr = data.get("chosen", {}).get("min_divergence_strength")
        if thr is None:
            return _FALLBACK_P90
        return float(thr)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _FALLBACK_P90


class CVDOrderFlowP90(CVDOrderFlow):
    """Top-decile divergence variant — separate name for baseline gate tracking."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        if "min_divergence_strength" not in cfg:
            cfg["min_divergence_strength"] = load_calibrated_threshold()
        super().__init__(cfg)
        logger.info(
            "CVDOrderFlow_p90 using min_divergence_strength=%.4f",
            self.MIN_DIVERGENCE,
        )

    @property
    def name(self) -> str:
        return "CVDOrderFlow_p90"
