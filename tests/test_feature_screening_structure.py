"""Unit tests for causal price-structure features (confirmation lag)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.feature_screening_24m_structure import (
    CONTROL_LOOKAHEAD,
    PIVOT_CONFIRM_K,
    _confirmed_pivots,
    build_structure_on_ohlcv,
    side_distribution,
)


@pytest.mark.unit
def test_pivot_not_visible_before_confirmation() -> None:
    # Flat then a clear swing low at index 10, then rise
    n = 40
    low = np.full(n, 100.0)
    high = np.full(n, 101.0)
    low[10] = 90.0  # swing low
    high[10] = 91.0
    last_hi, last_lo, _, _ = _confirmed_pivots(high, low, k=3)
    # Known only at i+k = 13
    assert not np.isfinite(last_lo[12])
    assert np.isfinite(last_lo[13])
    assert abs(last_lo[13] - 90.0) < 1e-9


@pytest.mark.unit
def test_structure_build_has_lags_and_lookahead_control() -> None:
    n = 200
    rng = np.random.default_rng(0)
    px = 100 + np.cumsum(rng.normal(0, 0.5, size=n))
    raw = pd.DataFrame(
        {
            "symbol": ["BTC"] * n,
            "timestamp_ms": np.arange(n) * 900_000,
            "open": px,
            "high": px + 1,
            "low": px - 1,
            "close": px,
            "volume": np.ones(n),
        }
    )
    out = build_structure_on_ohlcv(raw)
    lags = out.attrs["feature_lags"]
    assert lags["dist_nearest_sr_pct"] == PIVOT_CONFIRM_K
    assert lags["donchian_pos_20"] == 0
    assert lags[CONTROL_LOOKAHEAD] < 0
    assert CONTROL_LOOKAHEAD in out.columns
    # Look-ahead control uses future highs — finite after warm-up
    fut = out[CONTROL_LOOKAHEAD].to_numpy()
    assert np.isfinite(fut).sum() > 50


@pytest.mark.unit
def test_side_distribution_flags_always_long() -> None:
    # Always-positive feature + positive IC + sign rule → unidirectional long
    f = np.abs(np.random.default_rng(1).normal(size=500)) + 0.1
    s = side_distribution(f, ic=0.05, rule="sign")
    assert s["unidirectional"] is True
    assert s["pct_long"] > 80
    med = side_distribution(f, ic=0.05, rule="median_split")
    assert med["unidirectional"] is False
