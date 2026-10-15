"""Counts-only status board (§6) — feeds the future /api/hypotheses row.

The read gate is the contract: until n>=15 eligible closes or 2027-10-14,
this board exposes counts and lifecycle states ONLY — no PnL, no bps, no
returns. The unit test greps the emitted dict for forbidden keys.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from src.research.unlock_shadow import spec
from src.research.unlock_shadow.ledger import UnlockLedger


def board(ledger: UnlockLedger, now_ms: int | None = None) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    counts = {
        "snapshots": ledger.count("unlock_snapshots"),
        "events_registered": ledger.count("unlock_events"),
        "events_pending": ledger.count(
            "unlock_events", "WHERE state='pending'"),
        "events_eligible": ledger.count(
            "unlock_events", "WHERE state='eligible'"),
        "events_not_eligible": ledger.count(
            "unlock_events", "WHERE state='not_eligible'"),
        "events_b_confirmed": ledger.count(
            "unlock_events", "WHERE b_confirmed=1"),
        "events_merged": ledger.count(
            "unlock_events",
            "WHERE cluster_head_ms IS NOT NULL"
            " AND cluster_head_ms != event_ms"),
        "events_adv_gated": ledger.count(
            "unlock_events", "WHERE adv_gated=1"),
        "sims_scheduled": ledger.count(
            "unlock_sims", "WHERE state='scheduled'"),
        "sims_open": ledger.count("unlock_sims", "WHERE state='open'"),
        "sims_closed": ledger.count("unlock_sims", "WHERE state='closed'"),
        "placebos_scheduled": ledger.count(
            "unlock_sims", "WHERE kind='placebo' AND state='scheduled'"),
        "placebos_closed": ledger.count(
            "unlock_sims", "WHERE kind='placebo' AND state='closed'"),
        "placebos_invalidated": ledger.count(
            "unlock_sims", "WHERE state='invalidated'"),
        "manual_review_flags": ledger.count(
            "unlock_sims", "WHERE review_flag=1"),
    }
    n_closed_eligible = ledger.count(
        "unlock_sims",
        "WHERE kind='event' AND state='closed'")
    return {
        "hypothesis": "UnlockShadow",
        "spec": "docs/PREREGISTER_UNLOCK_SHADOW.md",
        "generated_ms": now_ms,
        "counts": counts,
        "read_gate": {
            "min_closed": spec.READ_MIN_CLOSED,
            "closed_so_far": n_closed_eligible,
            "expiry": spec.EXPIRY_ISO,
            "mode": "counts_only",   # no PnL until the gate opens (§6)
        },
    }
