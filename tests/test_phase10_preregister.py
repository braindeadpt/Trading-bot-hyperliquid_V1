"""Tests for the Fase 10 frozen-window pre-registration manifest.

Marker: unit (pure-function / tmp-file behavior, no network, no live DB).
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from src.research.phase10_preregister import (
    GATE_EXPECTANCY_R_GT,
    GATE_MAX_DRAWDOWN_PCT,
    GATE_MIN_PROFIT_FACTOR,
    GATE_MIN_TRADES,
    MAX_WINDOW_WEEKS,
    MIN_WINDOW_WEEKS,
    Phase10PreregisterError,
    assert_config_matches_preregister,
    build_preregister_manifest,
    load_preregister_manifest,
    persist_preregister_manifest,
    verify_preregister_integrity,
)
from src.utils.config import Config


def _make_config(execution_strategies=None, extra=None):
    data = {
        "mode": "paper",
        "risk": {"initial_capital": 10_000.0},
        "strategy": {
            "phase08": {
                "execution_strategies": execution_strategies
                if execution_strategies is not None
                else ["VWAPDeviation"],
            },
        },
    }
    if extra:
        data.update(extra)
    return Config(data)


@pytest.fixture()
def tmp_path(tmp_path):  # noqa: ARG001 - shadow pytest's tmp_path
    """Redirect to a scratch dir INSIDE the project.

    ``_resolve_preregister_path`` (by design, mirroring phase08_preregister's
    AUDIT-004 safe-path guard) refuses to write outside the project root, so
    the stock pytest ``tmp_path`` (which lives under the OS temp dir) cannot
    be used here. Use a disposable directory under ``data/research/`` instead
    and remove it afterwards.
    """
    project_root = Path(__file__).resolve().parents[1]
    scratch = project_root / "data" / "research" / f"_test_phase10_preregister_{uuid.uuid4().hex}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.unit
def test_build_manifest_captures_window_and_thresholds():
    config = _make_config()
    now_ms = 1_700_000_000_000
    manifest = build_preregister_manifest(config, now_ms=now_ms)

    assert manifest["window_start_ms"] == now_ms
    assert manifest["window"]["min_weeks"] == MIN_WINDOW_WEEKS == 8
    assert manifest["window"]["max_weeks"] == MAX_WINDOW_WEEKS == 12
    week_ms = 7 * 24 * 3600 * 1000
    assert manifest["window"]["min_end_ms"] == now_ms + 8 * week_ms
    assert manifest["window"]["max_end_ms"] == now_ms + 12 * week_ms

    # Canonical form is sorted, regardless of config list order.
    assert manifest["execution_strategies"] == ["VWAPDeviation"]

    thresholds = manifest["gate_thresholds"]
    assert thresholds["min_trades"] == GATE_MIN_TRADES == 100
    assert thresholds["min_profit_factor"] == GATE_MIN_PROFIT_FACTOR == 1.20
    assert thresholds["expectancy_r_gt"] == GATE_EXPECTANCY_R_GT == 0.0
    assert thresholds["max_drawdown_pct"] == GATE_MAX_DRAWDOWN_PCT == 5.0

    # Hash must be present and reproducible.
    assert manifest["manifest_hash"]
    verify_preregister_integrity(manifest)


@pytest.mark.unit
def test_manifest_reads_execution_strategies_live_not_hardcoded():
    config = _make_config(execution_strategies=["OnlyStrategyA"])
    manifest = build_preregister_manifest(config, now_ms=1_700_000_000_000)
    assert manifest["execution_strategies"] == ["OnlyStrategyA"]


@pytest.mark.unit
def test_persist_is_immutable_first_write_wins(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    config_a = _make_config(execution_strategies=["ChecklistMeta", "VWAPDeviation"])
    config_b = _make_config(execution_strategies=["SomethingElseEntirely"])

    out1 = persist_preregister_manifest(config_a, path=path)
    manifest1 = load_preregister_manifest(out1)
    assert manifest1["execution_strategies"] == sorted(["ChecklistMeta", "VWAPDeviation"])

    # Second call with a *different* config must NOT overwrite (first write wins).
    out2 = persist_preregister_manifest(config_b, path=path)
    manifest2 = load_preregister_manifest(out2)
    assert manifest2["execution_strategies"] == sorted(["ChecklistMeta", "VWAPDeviation"])
    assert manifest2["experiment_id"] == manifest1["experiment_id"]


@pytest.mark.unit
def test_persist_overwrite_flag_replaces_manifest(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    config_a = _make_config(execution_strategies=["A"])
    config_b = _make_config(execution_strategies=["B"])

    persist_preregister_manifest(config_a, path=path)
    first = load_preregister_manifest(path)
    first_id = first["experiment_id"]

    persist_preregister_manifest(
        config_b,
        path=path,
        overwrite=True,
        reregistration_reason="deadlock estrutural — test",
        in_sample_selection_note="in-sample selection documented",
    )

    manifest = load_preregister_manifest(path)
    assert manifest["execution_strategies"] == ["B"]
    assert manifest["reregistration_reason"] == "deadlock estrutural — test"
    assert manifest["in_sample_selection_note"] == "in-sample selection documented"
    assert manifest["supersedes_experiment_id"] == first_id
    archive = path.with_name(f"{path.stem}.superseded.{first_id}{path.suffix}")
    assert archive.exists()


@pytest.mark.unit
def test_verify_integrity_detects_tamper(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    config = _make_config()
    persist_preregister_manifest(config, path=path)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["gate_thresholds"]["min_profit_factor"] = 0.5  # tamper after the fact
    path.write_text(json.dumps(manifest), encoding="utf-8")

    tampered = load_preregister_manifest(path)
    with pytest.raises(Phase10PreregisterError):
        verify_preregister_integrity(tampered)


@pytest.mark.unit
def test_assert_config_matches_preregister_passes_when_unchanged(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    config = _make_config()
    persist_preregister_manifest(config, path=path)

    manifest = assert_config_matches_preregister(config, path=path)
    assert manifest["execution_strategies"] == ["VWAPDeviation"]


@pytest.mark.unit
def test_assert_config_matches_preregister_detects_strategy_drift(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    frozen_config = _make_config(execution_strategies=["VWAPDeviation"])
    persist_preregister_manifest(frozen_config, path=path)

    drifted_config = _make_config(
        execution_strategies=["VWAPDeviation", "CVDOrderFlow"]
    )
    with pytest.raises(Phase10PreregisterError, match="execution_strategies"):
        assert_config_matches_preregister(drifted_config, path=path)


@pytest.mark.unit
def test_assert_config_matches_preregister_detects_config_hash_drift(tmp_path):
    path = tmp_path / "phase10_preregister.json"
    frozen_config = _make_config(extra={"risk": {"initial_capital": 10_000.0, "max_positions": 3}})
    persist_preregister_manifest(frozen_config, path=path)

    # Same execution_strategies, but an unrelated risk parameter changed —
    # this must still be caught as drift (whole-config hash, not just strategies).
    drifted_config = _make_config(extra={"risk": {"initial_capital": 10_000.0, "max_positions": 5}})
    with pytest.raises(Phase10PreregisterError, match="config_hash"):
        assert_config_matches_preregister(drifted_config, path=path)


@pytest.mark.unit
def test_assert_config_matches_preregister_raises_when_missing(tmp_path):
    path = tmp_path / "does_not_exist.json"
    config = _make_config()
    with pytest.raises(Phase10PreregisterError):
        assert_config_matches_preregister(config, path=path)
