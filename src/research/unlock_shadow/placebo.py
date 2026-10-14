"""Placebo windows (§5) — deterministic seeded draw + invalidation check.

seed = sha256(token, T) at registration time, so the draw is reproducible
byte-for-byte from the ledger. Candidate starts are 00:00 UTC day marks in
[T+15d, T+75d] — FUTURE windows only, so each placebo crosses the same
live pricing path (daily-open mid + L2 walk + real funding) as its parent
event; a historical window could never replay the L2 leg honestly.

A window is void if any known unlock >=0.5% (same token) falls within
+-14d of its 7d span. Post-draw invalidation applies the same guard when
later snapshots announce new unlocks.
"""
from __future__ import annotations

import hashlib
import random
from typing import List, Sequence

from src.research.unlock_shadow import spec


def placebo_seed(token: str, event_ms: int) -> int:
    h = hashlib.sha256(f"{token}:{event_ms}".encode()).digest()
    return int.from_bytes(h[:8], "big")


def _guarded(start_ms: int, unlock_ms_seq: Sequence[int]) -> bool:
    """True when a >=0.5% unlock sits within +-14d of the window span."""
    w0, w1 = start_ms, start_ms + 7 * spec.MS_D
    guard = spec.PLACEBO_GUARD_MS
    return any(w0 - guard <= u <= w1 + guard for u in unlock_ms_seq)


def candidate_starts(event_ms: int,
                     known_unlocks_ms: Sequence[int]) -> List[int]:
    """All valid 00:00 UTC window starts in the future horizon."""
    d0 = event_ms - (event_ms % spec.MS_D)
    first = d0 + spec.PLACEBO_HORIZON_MIN_D * spec.MS_D
    last = d0 + spec.PLACEBO_HORIZON_MAX_D * spec.MS_D
    return [s for s in range(first, last + 1, spec.MS_D)
            if not _guarded(s, known_unlocks_ms)]


def draw_placebos(token: str, event_ms: int,
                  known_unlocks_ms: Sequence[int],
                  n: int = spec.PLACEBO_N) -> List[int]:
    """Seeded pick of n distinct window-start timestamps (00:00 UTC).
    Deterministic: same (token, T, unlock-set) -> same windows."""
    cands = candidate_starts(event_ms, known_unlocks_ms)
    if len(cands) <= n:
        return sorted(cands)
    rng = random.Random(placebo_seed(token, event_ms))
    return sorted(rng.sample(cands, n))


def placebo_invalidated(window_start_ms: int,
                        known_unlocks_ms: Sequence[int]) -> bool:
    """Post-draw check — a newly-announced >=0.5% unlock inside the guard
    zone marks the placebo 'invalidated' (recorded, never silently dropped)."""
    return _guarded(window_start_ms, known_unlocks_ms)
