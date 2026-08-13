"""Unit tests for scripts/iv_vs_adx_disagreement.py.

Pins the keep/block decision rules of both gates and the disagreement
verdict logic (which gate wins where the two signals disagree).
"""

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.iv_vs_adx_disagreement import (  # noqa: E402
    adx_keep,
    disagreement_verdict,
    iv_keep,
    iv_tercile,
)


class TestIvKeep:
    def test_keeps_above_66_7(self):
        assert iv_keep(70.0) is True
        assert iv_keep(66.8) is True

    def test_blocks_at_or_below_66_7(self):
        assert iv_keep(66.7) is False  # strict > (parity with +42.99)
        assert iv_keep(40.0) is False

    def test_blocks_missing_iv(self):
        assert iv_keep(None) is False


class TestAdxKeep:
    def test_vb_kept_in_expansion_only(self):
        assert adx_keep("VolatilityBreakout", "expansion") is True
        assert adx_keep("VolatilityBreakout", "trend") is False
        assert adx_keep("VolatilityBreakout", "low_vol") is False

    def test_vwap_kept_in_low_vol_and_unknown(self):
        assert adx_keep("VWAPDeviation", "low_vol") is True
        assert adx_keep("VWAPDeviation", "unknown") is True  # fallback
        assert adx_keep("VWAPDeviation", "expansion") is False
        assert adx_keep("VWAPDeviation", "trend") is False


class TestIvTercile:
    def test_mapping(self):
        assert iv_tercile(None) == "no_iv"
        assert iv_tercile(10.0) == "low_iv"
        assert iv_tercile(50.0) == "mid_iv"
        assert iv_tercile(80.0) == "high_iv"


class TestDisagreementVerdict:
    def test_iv_wins_when_right_on_both_cells(self):
        # IV keeps winners ADX blocked (+), IV blocks bleeders ADX kept (-)
        assert "IV" in disagreement_verdict(iv_keep_adx_block_pnl=12.75,
                                            iv_block_adx_keep_pnl=-12.81)

    def test_adx_wins_when_right_on_both_cells(self):
        # IV kept losers (-), IV blocked winners (+)
        assert "ADX" in disagreement_verdict(iv_keep_adx_block_pnl=-5.0,
                                             iv_block_adx_keep_pnl=8.0)

    def test_tie_when_split(self):
        # IV right on one cell, ADX right on the other
        v = disagreement_verdict(iv_keep_adx_block_pnl=5.0,
                                 iv_block_adx_keep_pnl=8.0)
        assert "empate" in v

    def test_ignores_zero_cells(self):
        # zero on both -> no one wins -> tie
        v = disagreement_verdict(iv_keep_adx_block_pnl=0.0,
                                 iv_block_adx_keep_pnl=0.0)
        assert "empate" in v

    def test_oos_one_sided_cell_iv_wins(self):
        """OOS 08-07..08-13: no high_iv trades, so the only populated
        disagreement cell is IV blocks / ADX keeps (n=4, -6.76). IV was
        right to block -> IV vence, even with zero keep-side evidence."""
        v = disagreement_verdict(iv_keep_adx_block_pnl=0.0,
                                 iv_block_adx_keep_pnl=-6.76)
        assert "IV" in v
