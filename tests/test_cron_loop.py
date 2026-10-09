"""Boundary math for the pm2 aux-loop sleep (src/utils/cron_loop.py)."""

from __future__ import annotations

import pytest

from src.utils.cron_loop import ms_to_next_boundary, sleep_to_next_boundary

pytestmark = pytest.mark.unit


def test_next_strict_multiple_of_interval() -> None:
    iv = 300_000  # 5 min
    assert ms_to_next_boundary(0, iv) == iv            # boundary -> next one
    assert ms_to_next_boundary(1, iv) == iv - 1
    assert ms_to_next_boundary(iv - 1, iv) == 1
    assert ms_to_next_boundary(iv, iv) == iv           # exact boundary waits full
    assert ms_to_next_boundary(iv + 137_000, iv) == 300_000 - 137_000


def test_arbitrary_interval_3h() -> None:
    iv = 10_800_000  # 3h
    assert ms_to_next_boundary(5 * iv + 123, iv) == iv - 123


def test_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValueError):
        ms_to_next_boundary(1000, 0)


def test_sleep_to_next_boundary_uses_injected_clock() -> None:
    slept: list[float] = []
    # freeze at 1.5min into a 5min window -> expect 3.5min wait
    waited = sleep_to_next_boundary(
        300, _sleep=slept.append, _now=lambda: 90.0
    )
    assert slept == [210.0]
    assert waited == pytest.approx(210.0)
