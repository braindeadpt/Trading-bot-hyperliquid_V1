"""Unit tests for scripts/liquidation_flush_recheck.py.

Covers the pure comparison logic (cell extraction, verdict rule) so the
30-day auto re-run contract is pinned even before the feed reaches 30d.
"""

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.liquidation_flush_recheck import (  # noqa: E402
    BASELINE,
    TARGET_DAYS,
    extract_cell,
    verdict,
)


def _cell(n: int, wr: float, pf: float, avg: float, net: float = 0.0,
          source: str = "real") -> dict:
    return {"n": n, "win_rate": wr, "profit_factor": pf, "avg_net_bps": avg,
            "net_bps": net, "source": source, "symbol": "ETH",
            "threshold": "p90", "hold_min": 30, "direction": "fade",
            "sl_pct": None}


class TestExtractCell:
    def test_finds_exact_cell_among_many(self):
        results = [
            {"source": "real", "symbol": "BTC", "threshold": "p90", "hold_min": 30,
             "direction": "fade", "sl_pct": None, "n": 10},
            _cell(n=46, wr=50.0, pf=2.353, avg=6.98, net=321.0),
            {"source": "proxy", "symbol": "ETH", "threshold": "p90", "hold_min": 30,
             "direction": "fade", "sl_pct": None, "n": 99},
        ]
        cell = extract_cell(results, "real")
        assert cell is not None
        assert cell["n"] == 46 and cell["profit_factor"] == 2.353

    def test_rejects_other_sources_and_variants(self):
        # same shape but proxy source and sl=1% variant must not match
        results = [
            _cell(n=46, wr=50.0, pf=2.353, avg=6.98, source="proxy"),
            dict(_cell(n=46, wr=50.0, pf=2.353, avg=6.98), sl_pct=0.01),
            dict(_cell(n=46, wr=50.0, pf=2.353, avg=6.98), direction="continuation"),
        ]
        assert extract_cell(results, "real") is None

    def test_missing_cell_returns_none(self):
        assert extract_cell([], "real") is None


class TestVerdict:
    def test_confirm_when_gate_met(self):
        assert "CONFIRMED" in verdict(_cell(n=40, wr=55.0, pf=1.6, avg=5.0))

    def test_kill_when_negative(self):
        assert "DEAD" in verdict(_cell(n=35, wr=40.0, pf=0.7, avg=-2.0))

    def test_inconclusive_when_n_below_gate(self):
        assert "INCONCLUSIVE" in verdict(_cell(n=20, wr=55.0, pf=1.6, avg=5.0))

    def test_inconclusive_when_pf_below_threshold(self):
        assert "INCONCLUSIVE" in verdict(_cell(n=40, wr=55.0, pf=1.1, avg=3.0))


class TestBaselineContract:
    def test_baseline_matches_v2_evidence(self):
        # The contract: v2 real ETH p90/30m/fade cell values (pinned in the
        # watchdog so the 30d comparison is against the recorded evidence).
        assert BASELINE["n"] == 46
        assert BASELINE["profit_factor"] == 2.353
        assert BASELINE["avg_net_bps"] == 6.98

    def test_target_is_30_days(self):
        assert TARGET_DAYS == 30


class TestV2EvidenceJSON:
    """Guard: the on-disk v2 evidence JSON must still contain the cell the
    watchdog compares against — if it drifts, the contract is broken."""

    def test_evidence_json_has_baseline_cell(self):
        p = ROOT / "data" / "backtests" / "liquidation_flush_shadow_v2_20260813_043848.json"
        if not p.exists():
            pytest.skip("v2 evidence JSON not present (data/backtests is gitignored)")
        data = json.loads(p.read_text(encoding="utf-8"))
        cell = extract_cell(data["results"], "real")
        assert cell is not None
        assert cell["n"] == BASELINE["n"]
        assert abs(cell["profit_factor"] - BASELINE["profit_factor"]) < 1e-6
