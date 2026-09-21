"""ShadowRecorder read-path bounds — Task 4 (memory).

Two regressions from the per-tick shadow path memory audit:

* ``load_decisions`` always parsed ``snapshot_json`` for every row —
  ~1.38M objects in json/decoder on a full-history read. Consumers that
  only need side/reason/timestamps can now pass ``parse_snapshots=False``
  and the column is not even selected.
* Unbounded reads: ``load_decisions`` had no default time window, so a
  caller forgetting ``since_ms`` scanned the whole (append-only) table.
  The 90d default is currently a no-op (oldest row is ~68d) but bounds
  future growth; ``window_ms=None`` opts out explicitly.
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.research.shadow_recorder as sr_mod  # noqa: E402
from src.data.research_database import ResearchDatabase  # noqa: E402
from src.research.shadow_recorder import (  # noqa: E402
    DEFAULT_WINDOW_MS,
    ShadowDecision,
    ShadowRecorder,
)

pytestmark = pytest.mark.unit

NOW = int(time.time() * 1000)


def _recorder() -> ShadowRecorder:
    path = Path(tempfile.gettempdir()) / f"shadow_mem_{uuid.uuid4().hex}.db"
    return ShadowRecorder(ResearchDatabase(path))


def _decision(ts_ms: int, *, snapshot: bool = True) -> ShadowDecision:
    return ShadowDecision(
        symbol="BTC",
        strategy="VolatilityBreakout",
        variant="phase08_shadow",
        side="long",
        would_enter=True,
        reason="entry_signal",
        timestamp_ms=ts_ms,
        market_snapshot={"price": 100.0, "confidence": 0.7} if snapshot else None,
    )


def test_parse_snapshots_false_never_parses_json() -> None:
    rec = _recorder()
    rec.record(_decision(NOW - 60_000))
    with patch.object(
        sr_mod, "parse_market_snapshot",
        side_effect=AssertionError("snapshot_json parsed eagerly"),
    ):
        rows = rec.load_decisions(parse_snapshots=False)
    assert len(rows) == 1
    assert rows[0].market_snapshot is None
    assert rows[0].side == "long"
    assert rows[0].reason == "entry_signal"


def test_parse_snapshots_default_still_parses() -> None:
    rec = _recorder()
    rec.record(_decision(NOW - 60_000))
    rows = rec.load_decisions()
    assert rows[0].market_snapshot == {"price": 100.0, "confidence": 0.7}


def test_load_decisions_default_window_bounds_history() -> None:
    rec = _recorder()
    old_ts = NOW - DEFAULT_WINDOW_MS - 86_400_000  # 1d beyond the window
    rec.record(_decision(old_ts))
    rec.record(_decision(NOW - 60_000))
    rows = rec.load_decisions()
    assert [r.timestamp_ms for r in rows] == [NOW - 60_000]
    # Explicit opt-outs preserve the full-history contract
    assert len(rec.load_decisions(window_ms=None)) == 2
    assert len(rec.load_decisions(since_ms=0)) == 2


def test_load_decisions_window_composes_with_filters() -> None:
    rec = _recorder()
    rec.record(_decision(NOW - 60_000))
    rows = rec.load_decisions(
        strategy="VolatilityBreakout", would_enter_only=True, limit=10,
    )
    assert len(rows) == 1
    rows = rec.load_decisions(strategy="OtherStrategy")
    assert rows == []
