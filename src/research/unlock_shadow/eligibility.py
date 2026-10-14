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
    persisted pct_unlocked when the raw supply basis wasn't preserved."""
    ev: RawUnlock
    first_seen_ms: int
    pct: Optional[float] = None


@dataclass
class ResolvedEvent:
    token: str
    event_ms: int                    # canonical date = source A's
    amount_tokens: float             # canonical amount = source A's
    pct_unlocked: Optional[float]
    state: str                       # eligible|single|pending
    seen_a_ms: Optional[int] = None
    seen_b_ms: Optional[int] = None
    amt_b: Optional[float] = None
    dt_b_ms: Optional[int] = None
    merged_into_ms: Optional[int] = None
    # populated by merge_clusters()
    cluster_head_ms: Optional[int] = None


def resolve_events(seen_a: List[Seen], seen_b: List[Seen],
                   now_ms: int) -> List[ResolvedEvent]:
    """Fold both sources' seen-sets into per-event verdicts.

    eligible — cliff + >=2% in A, a matching cliff row in B, and BOTH
               first-seen timestamps <= the T-6 snapshot cutoff.
    single   — past the cutoff with no matching B row (recorded,
               descriptive, outside the gate).
    pending  — cutoff not reached yet; may still resolve either way.
    """
    out: List[ResolvedEvent] = []
    by_tok_b: Dict[str, List[Seen]] = {}
    for s in seen_b:
        by_tok_b.setdefault(s.ev.token, []).append(s)

    for sa in seen_a:
        ev = sa.ev
        pct = sa.pct if sa.pct is not None else pct_of_unlocked(ev)
        if ev.kind == "excluded" or not qualifies_cliff(ev, pct):
            continue
        cutoff = eligibility_cutoff_ms(ev.event_ms)
        match: Optional[Seen] = None
        for sb in by_tok_b.get(ev.token, []):
            if events_match(ev, sb.ev):
                match = sb
                break
        if match is not None:
            ok = (sa.first_seen_ms <= cutoff
                  and match.first_seen_ms <= cutoff)
            out.append(ResolvedEvent(
                token=ev.token, event_ms=ev.event_ms,
                amount_tokens=ev.amount_tokens,
                pct_unlocked=pct,
                state="eligible" if ok else "single",
                seen_a_ms=sa.first_seen_ms,
                seen_b_ms=match.first_seen_ms,
                amt_b=match.ev.amount_tokens,
                dt_b_ms=match.ev.event_ms))
        else:
            out.append(ResolvedEvent(
                token=ev.token, event_ms=ev.event_ms,
                amount_tokens=ev.amount_tokens,
                pct_unlocked=pct,
                state="pending" if now_ms <= cutoff else "single",
                seen_a_ms=sa.first_seen_ms))
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
