"""Frozen config-hash CI guard (Fase 08 + Fase 10).

The bot refuses to start if the effective config drifts from the frozen
pre-registration window (``assert_config_matches_preregister`` in main.py
startup). These tests fail the CI suite the same way the bot would refuse
to boot:

  * ``compute_config_hash`` over the real ``config/settings.yaml`` must be
    exactly ``9456c6eb877b2391`` (the frozen Fase 10 hash), and
  * the Fase 08 / Fase 10 ``assert_config_matches_preregister`` must not
    raise (they check the frozen execution-strategy set and the full
    config hash against the on-disk manifests).

Any change to ``config/settings.yaml`` (or the effective DEFAULT_CONFIG
merge) that alters the hash — a parameter change mid-window — turns this
file red, forcing an explicit decision (re-freeze a new window, or revert).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SETTINGS_PATH = os.path.join(ROOT, "config", "settings.yaml")

FROZEN_FASE10_HASH = "9456c6eb877b2391"

pytestmark = pytest.mark.unit


def _load_production_config():
    from src.utils.config import load_config

    return load_config(SETTINGS_PATH)


def test_production_config_hash_is_frozen() -> None:
    """The effective settings.yaml hash must equal the frozen Fase 10 hash."""
    from src.utils.config import compute_config_hash

    cfg = _load_production_config()
    assert compute_config_hash(cfg) == FROZEN_FASE10_HASH


def test_fase10_preregister_assert_passes() -> None:
    """assert_config_matches_preregister (Fase 10) must not report drift."""
    from src.research.phase10_preregister import assert_config_matches_preregister

    cfg = _load_production_config()
    # Raises Phase10PreregisterError on drift (strategies changed or hash
    # changed mid-window) — exactly what main.py runs at startup.
    assert_config_matches_preregister(cfg)


def test_fase08_preregister_assert_passes() -> None:
    """assert_config_matches_preregister (Fase 08) must not report drift."""
    from src.research.phase08_preregister import assert_config_matches_preregister

    cfg = _load_production_config()
    assert_config_matches_preregister(cfg)


def test_hash_changes_when_config_parameter_changes() -> None:
    """Sanity: the frozen hash is not a tautology — a parameter change
    anywhere in the effective config (e.g. max_positions) must change it."""
    from src.utils.config import compute_config_hash

    cfg = _load_production_config()
    assert compute_config_hash(cfg) == FROZEN_FASE10_HASH

    mutated = {
        "risk": {"max_positions": 4},
    }
    assert compute_config_hash(mutated) != FROZEN_FASE10_HASH


def test_hash_changes_when_gate_key_changes() -> None:
    """A gate-key change (like adding a chase_filter param) also changes the
    hash — proving the drift guard covers the gate surface too."""
    from src.utils.config import compute_config_hash

    mutated = {
        "risk": {
            "chase_filter": {"enabled": False},
        },
    }
    assert compute_config_hash(mutated) != FROZEN_FASE10_HASH
