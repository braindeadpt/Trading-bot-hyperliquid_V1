"""Unit tests for shadow panel payload (no network)."""

from __future__ import annotations

import pytest

from src.research.shadow_panel import build_shadow_panel_payload
from src.strategies.cvd_orderflow_p90 import CVDOrderFlowP90


@pytest.mark.unit
def test_shadow_panel_payload_smoke():
    payload = build_shadow_panel_payload(
        shadow_names=["OrderBookScalper", "ChecklistMeta"],
        evaluate=False,
    )
    assert "rows" in payload
    assert len(payload["rows"]) == 2
    assert payload["min_trades_for_gate"] == 30
    assert "disclaimer" in payload
    obs = next(r for r in payload["rows"] if r["strategy"] == "OrderBookScalper")
    assert obs["fidelity_tier"].startswith("tier_b")
    assert obs["gate_progress_target"] == 30


@pytest.mark.unit
def test_cvd_p90_variant_name_and_threshold():
    s = CVDOrderFlowP90({"enabled": True, "min_divergence_strength": 0.11})
    assert s.name == "CVDOrderFlow_p90"
    assert abs(s.MIN_DIVERGENCE - 0.11) < 1e-9
