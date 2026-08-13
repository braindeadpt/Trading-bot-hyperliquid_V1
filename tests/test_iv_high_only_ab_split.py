"""Unit tests for scripts/iv_high_only_ab_split.py.

Pins the independent-window contract (non-overlapping 30d split + the
>p66 high-IV threshold that produced +42.99) and the verdict rules.
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.iv_high_only_ab_split import (  # noqa: E402
    IV_ONLY_PCT,
    _verdict_text,
)
from scripts.regime_router_a_b_test import split_windows  # noqa: E402


def _win(n_hi: int, hi_pnl: float) -> dict:
    return {"high_iv_only": {"n": n_hi, "net_pnl": hi_pnl}}


class TestThresholdContract:
    def test_threshold_matches_66_7(self):
        # The +42.99 figure came from _iv_pct > 66.7; pinning it keeps parity.
        assert IV_ONLY_PCT == 66.7


class TestSplitWindows:
    def test_three_non_overlapping_30d_windows(self):
        ws = split_windows("2026-05-18", "2026-08-07", 30)
        assert ws == [
            ("2026-05-18", "2026-06-16"),
            ("2026-06-17", "2026-07-16"),
            ("2026-07-17", "2026-08-07"),
        ]
        # non-overlapping: each window starts the day after the previous ends
        for (_, prev_end), (next_start, _) in zip(ws, ws[1:]):
            from datetime import datetime, timedelta
            d0 = datetime.strptime(prev_end, "%Y-%m-%d")
            d1 = datetime.strptime(next_start, "%Y-%m-%d")
            assert (d1 - d0).days == 1


class TestVerdictText:
    def test_inconclusive_when_total_n_below_30(self):
        ws = [_win(n_hi=10, hi_pnl=20.0), _win(n_hi=5, hi_pnl=-5.0)]
        v = _verdict_text(ws, positive_windows=1, tot_hi=15.0)
        assert v.startswith("Net high_iv-only +15.00 USD (n=15)")
        assert "INCONCLUSIVO" in v

    def test_robust_when_positive_in_all_windows(self):
        ws = [_win(n_hi=20, hi_pnl=10.0), _win(n_hi=20, hi_pnl=5.0)]
        v = _verdict_text(ws, positive_windows=2, tot_hi=15.0)
        assert "ROBUSTO" in v

    def test_rejected_when_negative_in_all_windows(self):
        ws = [_win(n_hi=20, hi_pnl=-10.0), _win(n_hi=20, hi_pnl=-5.0)]
        v = _verdict_text(ws, positive_windows=0, tot_hi=-15.0)
        assert "REJEITADO" in v

    def test_mixed_when_some_positive(self):
        ws = [_win(n_hi=20, hi_pnl=10.0), _win(n_hi=20, hi_pnl=-5.0)]
        v = _verdict_text(ws, positive_windows=1, tot_hi=5.0)
        assert "MISTO" in v
