"""Deterministic eligibility resolution (§2, §3).

Pure functions over normalized RawUnlock rows + their per-source
first-seen timestamps. The daemon feeds snapshot state; tests drive it
directly. Nothing here touches the network or the clock implicitly —
`now_ms` is always an argument.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.research.unlock_shadow.calendar_snapshot import RawUnlock
from src.research.unlock_shadow import spec


def day_floor(ms: int) -> int:
    """00:00 UTC of the day containing `ms`."""
    return ms - (ms % spec.MS_D)


def snapshot_ms_of_day(day_ms: int) -> int:
    """The daily snapshot timestamp on `day_ms` (00:30 UTC)."""
    h, m = spec.SNAPSHOT_UTC_HHMM
    return day_floor(day_ms) + (h * 60 + m) * 60_000


def eligibility_cutoff_ms(event_ms: int) -> int:
    """Latest snapshot at which a T-dated event can still be eligible:
    the 00:30 UTC snapshot of T-6 (§2 'snapshot de T-6 ou antes')."""
    return snapshot_ms_of_day(day_floor(event_ms) - spec.SEEN_CUTOFF_D
                              * spec.MS_D)


def pct_of_unlocked(ev: RawUnlock) -> Optional[float]:
    """Amount as a fraction of unlocked supply; None when the source gave
    no supply basis — the ≥2% claim is then unprovable and the event can
    never be eligible (conservative, §2)."""
    if ev.unlocked_supply is None or ev.unlocked_supply <= 0:
        return None
    return ev.amount_tokens / ev.unlocked_supply


def qualifies_cliff(ev: RawUnlock, pct: Optional[float] = None) -> bool:
    """Cliff-only, non-excluded, provable >= 2% of unlocked supply.
    `pct` lets the ledger path pass the stored pct_unlocked when the raw
    supply basis wasn't preserved."""
    if ev.kind != "cliff":
        return False
    p = pct if pct is not None else pct_of_unlocked(ev)
    return p is not None and p >= spec.MIN_CLIFF_PCT_UNLOCKED


def dates_match(a_ms: int, b_ms: int) -> bool:
    return abs(a_ms - b_ms) <= spec.DATE_TOL_MS


def amounts_match(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / max(a, b) <= spec.AMOUNT_TOL


def events_match(a: RawUnlock, b: RawUnlock) -> bool:
    """Same token + date ±1d + amount ±25% — the §2 cross-source rule."""
    return (a.token == b.token
            and a.kind == "cliff" and b.kind == "cliff"
            and dates_match(a.event_ms, b.event_ms)
            and amounts_match(a.amount_tokens, b.amount_tokens))


@dataclass
class Seen:
    """A RawUnlock plus when its source first listed it; `pct` carries a
    persisted pct_unlocked when the raw supply basis wasn't preserved.
    `last_seen_ms` is the latest snapshot that still listed the row —
    presence at a cutoff = first_seen <= cutoff <= last_seen (Emenda 1)."""
    ev: RawUnlock
    first_seen_ms: int
    last_seen_ms: Optional[int] = None
    pct: Optional[float] = None


def visible_at(s: Seen, cutoff_ms: int) -> bool:
    """The row was in the source's payload at the `cutoff_ms` snapshot:
    listed on/before it and still listed at/after it."""
    last = s.last_seen_ms if s.last_seen_ms is not None else s.first_seen_ms
    return s.first_seen_ms <= cutoff_ms <= last


@dataclass
class ResolvedEvent:
    token: str
    event_ms: int                    # canonical date = source A's
    amount_tokens: float             # canonical amount = source A's
    pct_unlocked: Optional[float]
    state: str                       # eligible|pending|not_eligible (Emenda 1)
    seen_a_ms: Optional[int] = None
    seen_b_ms: Optional[int] = None
    amt_b: Optional[float] = None
    dt_b_ms: Optional[int] = None
    merged_into_ms: Optional[int] = None
    b_confirmed: int = 0             # source-B descriptive match (Emenda 1)
    # populated by merge_clusters()
    cluster_head_ms: Optional[int] = None


def resolve_events(seen_a: List[Seen], seen_b: List[Seen],
                   now_ms: int) -> List[ResolvedEvent]:
    """Emenda 1 (2026-10-15): single-source gate — DefiLlama only.

    eligible     — cliff + >=2% (rule A unchanged) AND the calendar row's
                   first_seen <= the T-6 00:30 UTC cutoff AND now >= that
                   cutoff. The canonical date/amount are the row's frozen
                   values: if DefiLlama later edits the event, the edit
                   arrives as a NEW calendar row (different event_ms or
                   amount) seen after the cutoff -> not_eligible; the
                   frozen row keeps its date and the trade runs on it.
    pending      — qualifies and already seen, but the T-6 snapshot is
                   still in the future (awaits the frozen confirmation).
    not_eligible — fails rule A, or first seen after the cutoff (logged).
    b_confirmed  — descriptive flag: a source-B row matched +-1d/+-25%
                   with first_seen <= the same cutoff. Never gates.
    """
    out: List[ResolvedEvent] = []
    by_tok_b: Dict[str, List[Seen]] = {}
    for s in seen_b:
        by_tok_b.setdefault(s.ev.token, []).append(s)

    for sa in seen_a:
        ev = sa.ev
        if ev.kind != "cliff":
            continue                       # only declared cliffs are events
        pct = sa.pct if sa.pct is not None else pct_of_unlocked(ev)
        cutoff = eligibility_cutoff_ms(ev.event_ms)
        b_match: Optional[Seen] = None
        for sb in by_tok_b.get(ev.token, []):
            if visible_at(sb, cutoff) and events_match(ev, sb.ev):
                b_match = sb
                break
        if not qualifies_cliff(ev, pct) or sa.first_seen_ms > cutoff:
            state = "not_eligible"
        elif now_ms < cutoff:
            state = "pending"              # awaits the T-6 snapshot
        elif visible_at(sa, cutoff):
            state = "eligible"
        else:
            state = "not_eligible"         # absent from the T-6 snapshot
        out.append(ResolvedEvent(
            token=ev.token, event_ms=ev.event_ms,
            amount_tokens=ev.amount_tokens, pct_unlocked=pct,
            state=state, seen_a_ms=sa.first_seen_ms,
            seen_b_ms=b_match.first_seen_ms if b_match else None,
            amt_b=b_match.ev.amount_tokens if b_match else None,
            dt_b_ms=b_match.ev.event_ms if b_match else None,
            b_confirmed=1 if b_match is not None else 0))
    return out


def merge_clusters(events: List[ResolvedEvent]) -> List[ResolvedEvent]:
    """Same-token eligible events <14d apart merge into the first (§3).

    Chain-gap clustering: an event joins the previous event's cluster
    when their dates are < 14d apart; the cluster head is the first —
    it owns the single trade covering the whole cluster.
    """
    by_tok: Dict[str, List[ResolvedEvent]] = {}
    for e in events:
        if e.state == "eligible":
            by_tok.setdefault(e.token, []).append(e)
    for evs in by_tok.values():
        evs.sort(key=lambda e: e.event_ms)
        head: Optional[ResolvedEvent] = None
        prev: Optional[ResolvedEvent] = None
        for e in evs:
            if (prev is not None and head is not None
                    and e.event_ms - prev.event_ms < spec.MERGE_WINDOW_MS):
                e.merged_into_ms = head.event_ms
                e.cluster_head_ms = head.event_ms
            else:
                head = e
                e.cluster_head_ms = e.event_ms
            prev = e
    return events


def window_bounds_ms(event_ms: int) -> Tuple[int, int]:
    """00:00 UTC of T-5 -> 00:00 UTC of T+2 (§4)."""
    d0 = day_floor(event_ms)
    return (d0 - spec.WINDOW_PRE_D * spec.MS_D,
            d0 + spec.WINDOW_POST_D * spec.MS_D)
