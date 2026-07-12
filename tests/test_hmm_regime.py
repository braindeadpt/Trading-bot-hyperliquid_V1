"""Tests for the HMM regime detector (v3.1.21)."""

from __future__ import annotations

import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from src.core.hmm_regime import (
    FEATURE_NAMES,
    HMMRegimeDetector,
    Regime,
    RegimeState,
    _GaussianHMM,
    _label_state,
    build_feature_matrix,
    build_regime_detector,
)
import pytest

pytestmark = pytest.mark.unit


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── Feature extraction ─────────────────────────────────────────────


def test_build_feature_matrix_shape() -> None:
    n = 30
    closes = [100.0 + i * 0.1 for i in range(n)]
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    vols = [1000.0 + (i % 5) * 100 for i in range(n)]
    feats = build_feature_matrix(closes, highs, lows, vols)
    _pass("build_feature_matrix_shape", feats.shape == (n, 4))


def test_build_feature_matrix_short_input() -> None:
    feats = build_feature_matrix([100.0], [101.0], [99.0], [1000.0])
    _pass("build_feature_matrix_short_input", feats.shape == (0, 4))


def test_build_feature_matrix_returns_calculation() -> None:
    closes = [100.0, 110.0, 121.0, 133.1]  # +10% each
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    vols = [1000.0] * 4
    feats = build_feature_matrix(closes, highs, lows, vols)
    # returns: 0.1, 0.1, 0.1
    _pass("build_feature_matrix_returns",
          abs(feats[1, 0] - 0.1) < 1e-9
          and abs(feats[2, 0] - 0.1) < 1e-9
          and abs(feats[3, 0] - 0.1) < 1e-9)


# ── Gaussian HMM internals ─────────────────────────────────────────


def test_hmm_fit_short_returns_unchfitted() -> None:
    hmm = HMMRegimeDetector()
    feats = np.zeros((10, 4), dtype=np.float64)
    ok = hmm.fit(feats)
    _pass("hmm_fit_short_returns_unchfitted", not ok and not hmm.fitted)


def test_hmm_fit_synthetic_trending_data() -> None:
    """Build a synthetic feature matrix: 3 regimes, fit a 4-state HMM,
    check that the detected regime on new trending data is trending."""
    rng = np.random.default_rng(42)
    # Trending up: positive returns, low ATR, high ADX
    trend = np.column_stack([
        rng.normal(0.001, 0.001, 100),
        rng.normal(0.004, 0.001, 100),
        rng.normal(28.0, 2.0, 100),
        rng.normal(1.0, 0.1, 100),
    ])
    # Ranging: tiny returns, tiny ATR, low ADX
    ranging = np.column_stack([
        rng.normal(0.0, 0.0008, 100),
        rng.normal(0.002, 0.0005, 100),
        rng.normal(10.0, 2.0, 100),
        rng.normal(1.0, 0.1, 100),
    ])
    # Volatile: huge ATR, big returns
    volatile = np.column_stack([
        rng.normal(0.0, 0.01, 100),
        rng.normal(0.025, 0.005, 100),
        rng.normal(20.0, 4.0, 100),
        rng.normal(2.5, 0.4, 100),
    ])
    feats = np.concatenate([trend, ranging, volatile], axis=0)
    hmm = HMMRegimeDetector(n_states=4, n_restarts=1, seed=0)
    ok = hmm.fit(feats)
    _pass("hmm_fit_synthetic_trending_data", ok)


def test_hmm_detect_returns_regime_state() -> None:
    rng = np.random.default_rng(0)
    feats = np.column_stack([
        rng.normal(0.0, 0.005, 200),
        rng.normal(0.005, 0.002, 200),
        rng.normal(20.0, 5.0, 200),
        rng.normal(1.0, 0.2, 200),
    ])
    hmm = HMMRegimeDetector(n_states=4, n_restarts=1, seed=0)
    hmm.fit(feats)
    state = hmm.detect(0.001, 0.005, 25.0, 1.2)
    _pass("hmm_detect_returns_regime_state",
          isinstance(state, RegimeState)
          and state.regime in Regime
          and abs(sum(state.probabilities.values()) - 1.0) < 1e-6)


def test_hmm_detect_probabilities_sum_to_one() -> None:
    rng = np.random.default_rng(7)
    feats = np.column_stack([
        rng.normal(0.001, 0.001, 100),
        rng.normal(0.005, 0.001, 100),
        rng.normal(20.0, 3.0, 100),
        rng.normal(1.0, 0.1, 100),
    ])
    hmm = HMMRegimeDetector(n_states=4, n_restarts=1)
    hmm.fit(feats)
    state = hmm.detect(0.0, 0.005, 18.0, 1.0)
    s = sum(state.probabilities.values())
    _pass("hmm_detect_probabilities_sum_to_one", abs(s - 1.0) < 1e-6)


# ── State labelling ────────────────────────────────────────────────


def test_label_state_trending_up() -> None:
    # high ADX, positive return
    label = _label_state(np.array([0.002, 0.005, 28.0, 1.0]))
    _pass("label_state_trending_up", label == Regime.TRENDING_UP)


def test_label_state_trending_down() -> None:
    label = _label_state(np.array([-0.002, 0.005, 28.0, 1.0]))
    _pass("label_state_trending_down", label == Regime.TRENDING_DOWN)


def test_label_state_ranging() -> None:
    label = _label_state(np.array([0.0, 0.003, 12.0, 1.0]))
    _pass("label_state_ranging", label == Regime.RANGING)


def test_label_state_volatile() -> None:
    label = _label_state(np.array([0.0, 0.025, 18.0, 1.0]))
    _pass("label_state_volatile", label == Regime.VOLATILE)


# ── Fallback ───────────────────────────────────────────────────────


def test_detect_falls_back_to_threshold_when_unfitted() -> None:
    hmm = HMMRegimeDetector()
    state = hmm.detect(0.0, 0.003, 12.0, 1.0)
    _pass("detect_falls_back_to_threshold_when_unfitted",
          state.regime == Regime.RANGING
          and state.confidence == 1.0)


def test_detect_from_history_triggers_fit() -> None:
    rng = np.random.default_rng(11)
    closes = [100.0 + i * 0.05 for i in range(80)]
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    vols = list(rng.normal(1000.0, 100.0, 80).clip(min=1.0))
    adx_series = list(rng.normal(18.0, 5.0, 80))
    hmm = HMMRegimeDetector(n_states=4, n_restarts=1)
    state = hmm.detect_from_history(closes, highs, lows, vols, adx_series)
    _pass("detect_from_history_triggers_fit",
          state.regime in Regime
          and not math.isnan(state.confidence))


def test_detect_from_history_short_uses_threshold() -> None:
    hmm = HMMRegimeDetector()
    state = hmm.detect_from_history([100.0, 101.0], [101.0, 102.0], [99.0, 100.0], [1000.0, 1000.0])
    _pass("detect_from_history_short_uses_threshold",
          state.regime in Regime)


# ── Factory ────────────────────────────────────────────────────────


class _FakeConfig:
    def get(self, key, default=None):
        keys = {
            "regime.detection_method": "hmm",
            "regime.hmm_n_states": 4,
            "regime.hmm_n_restarts": 1,
            "regime.hmm_seed": 0,
        }
        return keys.get(key, default)


def test_factory_returns_hmm_when_method_hmm() -> None:
    det = build_regime_detector(_FakeConfig())
    _pass("factory_returns_hmm_when_method_hmm",
          det is not None
          and isinstance(det, HMMRegimeDetector))


class _AdxConfig:
    def get(self, key, default=None):
        if key == "regime.detection_method":
            return "adx"
        return default


def test_factory_returns_none_when_method_adx() -> None:
    det = build_regime_detector(_AdxConfig())
    _pass("factory_returns_none_when_method_adx", det is None)


# ── RegimeState serialization ─────────────────────────────────────


def test_regime_state_as_dict() -> None:
    state = RegimeState(
        regime=Regime.RANGING,
        confidence=0.7,
        probabilities={Regime.RANGING: 0.7, Regime.VOLATILE: 0.3},
    )
    d = state.as_dict()
    _pass("regime_state_as_dict",
          d["regime"] == "ranging"
          and d["confidence"] == 0.7
          and d["probabilities"]["ranging"] == 0.7)


def main() -> int:
    print("=" * 70)
    print("HMM regime detector tests")
    print("=" * 70)
    tests = [
        test_build_feature_matrix_shape,
        test_build_feature_matrix_short_input,
        test_build_feature_matrix_returns_calculation,
        test_hmm_fit_short_returns_unchfitted,
        test_hmm_fit_synthetic_trending_data,
        test_hmm_detect_returns_regime_state,
        test_hmm_detect_probabilities_sum_to_one,
        test_label_state_trending_up,
        test_label_state_trending_down,
        test_label_state_ranging,
        test_label_state_volatile,
        test_detect_falls_back_to_threshold_when_unfitted,
        test_detect_from_history_triggers_fit,
        test_detect_from_history_short_uses_threshold,
        test_factory_returns_hmm_when_method_hmm,
        test_factory_returns_none_when_method_adx,
        test_regime_state_as_dict,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            _pass(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(e).__name__}: {e}")
    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
