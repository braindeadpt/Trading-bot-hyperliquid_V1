"""ChecklistMeta regime gates — chop ADX, dominance margin, anti-flip."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.base import MarketEvent
from src.strategies.checklist_meta import ChecklistMeta


def test_chop_gate_blocks_low_adx() -> None:
    s = ChecklistMeta({"signal_throttle_ms": 0, "min_adx_gate": 18.0})
    event = MarketEvent(
        symbol="BTC",
        price=100_000.0,
        timestamp_ms=1_700_000_000_000,
        adx_14=12.0,
    )
    assert s.on_data(event) is None
    reason = s._entry_gates_block_reason("BTC", 5.0, 1.0, 12.0, event.timestamp_ms)
    assert reason is not None and reason.startswith("chop_gate")


def test_dominance_gate_blocks_tied_scores() -> None:
    s = ChecklistMeta({"dominance_margin": 1.5, "score_threshold": 2.0})
    reason = s._entry_gates_block_reason(
        "BTC", directional_bull=3.2, directional_bear=2.5,
        adx=25.0, timestamp_ms=1_700_000_000_000,
    )
    assert reason is not None and reason.startswith("dominance_gate")


def test_flip_block_after_stop_long() -> None:
    s = ChecklistMeta({"flip_block_minutes": 60.0})
    base_ms = 1_700_000_000_000
    s.record_stop_exit("SOL", "long", base_ms)

    state = s._get_state("SOL")
    short_block = s._flip_block_reason(state, "short", base_ms + 30 * 60_000)
    long_ok = s._flip_block_reason(state, "long", base_ms + 30 * 60_000)
    short_later = s._flip_block_reason(state, "short", base_ms + 61 * 60_000)

    assert short_block is not None and "flip_block" in short_block
    assert long_ok is None
    assert short_later is None


if __name__ == "__main__":
    test_chop_gate_blocks_low_adx()
    test_dominance_gate_blocks_tied_scores()
    test_flip_block_after_stop_long()
    print("ALL CHECKLIST META GATE TESTS PASSED [OK]")
