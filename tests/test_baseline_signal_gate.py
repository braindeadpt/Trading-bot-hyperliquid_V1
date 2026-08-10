"""Baseline-signal gate: three-condition verdict + preregister entry asymmetry."""

from __future__ import annotations

import pytest

from scripts.baseline_signal_gate import gate_verdict
from src.research.phase08_preregister import (
    LEGACY_EXECUTION_WITHOUT_BASELINE_GATE,
    PreregisterManifestError,
    assert_baseline_signal_gate,
    assert_can_promote_to_execution,
)


def _fold(*, n: int, pf: float, exp: float, b1_pct: float) -> dict:
    return {
        "strategy_engine": {
            "n_trades": n,
            "profit_factor": pf,
            "expectancy": exp,
        },
        "baselines": {
            "B1_random_direction": {
                "vs_real_fast": {
                    "profit_factor": {
                        "percentile": b1_pct,
                        "above_p95": b1_pct >= 95.0,
                        "above_p50": b1_pct >= 50.0,
                    }
                }
            }
        },
    }


@pytest.mark.unit
def test_gate_inconclusive_underpowered():
    g = gate_verdict(_fold(n=10, pf=2.0, exp=1.0, b1_pct=99.0))
    assert g["verdict"] == "INCONCLUSIVE"
    assert any("n_trades" in f for f in g["failed_conditions"])


@pytest.mark.unit
def test_gate_fail_b1_only():
    g = gate_verdict(_fold(n=50, pf=1.5, exp=0.5, b1_pct=48.0))
    assert g["verdict"] == "FAIL"
    assert any("B1_pf_percentile" in f for f in g["failed_conditions"])
    assert not any("not_profitable" in f for f in g["failed_conditions"])


@pytest.mark.unit
def test_gate_fail_profitable_only_smf_case():
    """SmartMoneyFlow W3 cautionary case: beats noise, still loses money."""
    g = gate_verdict(_fold(n=171, pf=0.268, exp=-54.0, b1_pct=96.0))
    assert g["verdict"] == "FAIL"
    assert any("not_profitable" in f for f in g["failed_conditions"])
    assert not any("B1_pf_percentile" in f for f in g["failed_conditions"])


@pytest.mark.unit
def test_gate_pass_three_conditions():
    g = gate_verdict(_fold(n=80, pf=1.4, exp=0.3, b1_pct=97.0))
    assert g["verdict"] == "PASS"
    assert g["failed_conditions"] == []


@pytest.mark.unit
def test_promote_without_field_fails():
    with pytest.raises(PreregisterManifestError, match="cannot promote"):
        assert_can_promote_to_execution("DonchianBreakout", None)


@pytest.mark.unit
def test_promote_with_pass_ok():
    assert_can_promote_to_execution(
        "DonchianBreakout",
        {"strategy": "DonchianBreakout", "verdict": "PASS"},
    )


@pytest.mark.unit
def test_legacy_execution_without_gate_does_not_fail():
    """Paper bot: ChecklistMeta / VWAPDeviation may lack gate field."""
    assert LEGACY_EXECUTION_WITHOUT_BASELINE_GATE == frozenset(
        {"ChecklistMeta", "VWAPDeviation"}
    )
    assert_baseline_signal_gate(
        {
            "execution_scope": {"strategies": ["ChecklistMeta", "VWAPDeviation"]},
        },
        require=False,
        hard_for_new=True,
    )


@pytest.mark.unit
def test_non_legacy_in_execution_without_gate_fails():
    with pytest.raises(PreregisterManifestError, match="missing PASS"):
        assert_baseline_signal_gate(
            {
                "execution_scope": {
                    "strategies": ["ChecklistMeta", "SmartMoneyFlow"],
                },
            },
            require=False,
            hard_for_new=True,
        )


@pytest.mark.unit
def test_non_legacy_with_pass_ok():
    assert_baseline_signal_gate(
        {
            "execution_scope": {"strategies": ["SmartMoneyFlow"]},
            "baseline_signal_gate": {
                "strategy": "SmartMoneyFlow",
                "verdict": "PASS",
            },
        },
        require=False,
        hard_for_new=True,
    )
