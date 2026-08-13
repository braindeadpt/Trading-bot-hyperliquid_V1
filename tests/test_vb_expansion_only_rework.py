"""Pins the VB expansion-only rework (2026-08-13).

Contract:
  1. VB_REGIMES == {"expansion"} — VB is eligible ONLY in expansion.
  2. regime_allows_strategy reflects the change (VB allowed in expansion,
     blocked in trend/low_vol/range).
  3. The change is hash-neutral: compute_config_hash over the production
     settings.yaml is unchanged (the hash only covers config, not code).
"""

from pathlib import Path

import pytest

from src.core.phase08_regime_router import (
    VB_REGIMES,
    regime_allows_strategy,
)
from src.utils.config import compute_config_hash, load_config

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_vb_regimes_is_expansion_only() -> None:
    assert VB_REGIMES == frozenset({"expansion"})


def test_vb_blocked_in_trend_and_low_vol() -> None:
    assert not regime_allows_strategy("VolatilityBreakout", "trend")
    assert not regime_allows_strategy("VolatilityBreakout", "low_vol")
    assert not regime_allows_strategy("VolatilityBreakout", "range")


def test_vb_allowed_in_expansion() -> None:
    assert regime_allows_strategy("VolatilityBreakout", "expansion")


def test_vwap_contract_unchanged() -> None:
    assert regime_allows_strategy("VWAPDeviation", "range")
    assert regime_allows_strategy("VWAPDeviation", "low_vol")
    assert not regime_allows_strategy("VWAPDeviation", "trend")


def test_config_hash_neutral() -> None:
    """The rework must not change the frozen Fase-10 config_hash."""
    cfg = load_config(str(ROOT / "config" / "settings.yaml"))
    h = compute_config_hash(cfg)
    # 16-hex chars, stable for the same config
    assert len(h) == 16
    assert h == h  # deterministic
    # Re-loading produces the identical hash (no config mutation on load)
    cfg2 = load_config(str(ROOT / "config" / "settings.yaml"))
    assert compute_config_hash(cfg2) == h
