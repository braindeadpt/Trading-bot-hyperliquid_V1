"""Unit tests for shadow panel payload (no network)."""

from __future__ import annotations

import os
import tempfile

import pytest

from src.data.database import LiquidationRecord
from src.data.research_database import ResearchDatabase
from src.research.shadow_panel import (
    _liquidation_provenance,
    build_shadow_panel_payload,
)
from src.strategies.cvd_orderflow_p90 import CVDOrderFlowP90
from src.utils.config import Config


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


# ---------------------------------------------------------------------------
# Liquidation provenance in the shadow panel tier
# ---------------------------------------------------------------------------


def _research_db_with_liq(sources: list[str]) -> ResearchDatabase:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ResearchDatabase(path)
    for i, src in enumerate(sources):
        db.save_liquidation(LiquidationRecord(
            symbol="BTC",
            timestamp_ms=1_700_000_000_000 + i * 60_000,
            notional_usd=2_000_000.0,
            side="long",
            source=src,
        ))
    return db


def _cfg_with_research_db(path: str) -> Config:
    return Config({"research": {"database": {"path": path}}})


@pytest.mark.unit
def test_liquidation_provenance_real():
    db = _research_db_with_liq(["okx", "bybit"])
    cfg = _cfg_with_research_db(str(db.db_path))
    assert _liquidation_provenance(cfg) == "real"


@pytest.mark.unit
def test_liquidation_provenance_proxy_only():
    db = _research_db_with_liq(["proxy", "proxy"])
    cfg = _cfg_with_research_db(str(db.db_path))
    assert _liquidation_provenance(cfg) == "proxy"


@pytest.mark.unit
def test_liquidation_provenance_mixed():
    db = _research_db_with_liq(["okx", "proxy"])
    cfg = _cfg_with_research_db(str(db.db_path))
    assert _liquidation_provenance(cfg) == "mixed"


@pytest.mark.unit
def test_liquidation_provenance_none_when_empty_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ResearchDatabase(path)
    cfg = _cfg_with_research_db(str(db.db_path))
    assert _liquidation_provenance(cfg) == "none"


@pytest.mark.unit
def test_liquidation_provenance_none_when_missing_db():
    # A missing research DB opens empty (Database creates the schema on first
    # connect) — no liquidation rows means "none", not an error.
    cfg = _cfg_with_research_db(os.path.join(tempfile.gettempdir(), "no_such_research.db"))
    assert _liquidation_provenance(cfg) == "none"


@pytest.mark.unit
def test_liquidation_catcher_tier_reflects_real_provenance():
    """LiquidationCatcher row shows Tier A + real provenance when the research
    DB has real-venue liquidation rows — matching strict-research acceptance."""
    db = _research_db_with_liq(["okx", "bybit"])
    cfg = _cfg_with_research_db(str(db.db_path))
    payload = build_shadow_panel_payload(
        shadow_names=["LiquidationCatcher"],
        config=cfg,
        evaluate=False,
    )
    obs = next(r for r in payload["rows"] if r["strategy"] == "LiquidationCatcher")
    assert obs["liquidation_provenance"] == "real"
    assert obs["fidelity_tier"] == "tier_a_hl_liquidation"


@pytest.mark.unit
def test_liquidation_catcher_tier_reflects_proxy_provenance():
    """Proxy-only liquidation rows → Tier B proxy label, matching what a
    strict-research replay would refuse (refused_insufficient_feeds)."""
    db = _research_db_with_liq(["proxy"])
    cfg = _cfg_with_research_db(str(db.db_path))
    payload = build_shadow_panel_payload(
        shadow_names=["LiquidationCatcher"],
        config=cfg,
        evaluate=False,
    )
    obs = next(r for r in payload["rows"] if r["strategy"] == "LiquidationCatcher")
    assert obs["liquidation_provenance"] == "proxy"
    assert obs["fidelity_tier"] == "tier_b_liquidation_proxy_not_production"


@pytest.mark.unit
def test_shadow_panel_template_shows_liquidation_badge():
    """The shadow panel tier cell renders a colored provenance badge."""
    root = os.path.join(os.path.dirname(__file__), "..")
    html = open(
        os.path.join(root, "src", "dashboard", "templates", "index.html"),
        encoding="utf-8",
    ).read()
    assert "liquidation_provenance" in html
    assert "liq:" in html  # badge label
    assert "strict research recusa" in html  # proxy tooltip
    assert "produção-grade" in html  # real tooltip
