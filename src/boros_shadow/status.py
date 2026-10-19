"""Counts-only status board — feeds a future /api/hypotheses row.

The read gate is the contract: until n>=10 closed clusters or
2027-06-30, this board exposes counts and kill-flag booleans ONLY —
no PnL, no bps, no returns, no APY. Kill flags are permitted by the
prereg: a hypothetical liquidation shows KILL immediately.
"""
from __future__ import annotations

import time
from typing import Any, Dict

from src.boros_shadow import spec
from src.boros_shadow.ledger import BorosLedger
from src.boros_shadow.settle import closed_clusters


def board(ledger: BorosLedger, now_ms: int | None = None) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    counts = {
        "snapshots": ledger.count("boros_snapshots"),
        "opportunities": ledger.count("boros_opportunities"),
        "entries": ledger.count("boros_trades"),
        "near_entries": ledger.count(
            "boros_opportunities",
            "WHERE entered=0 AND net_apy >= ?", (spec.NEAR_MISS_APY,)),
        "open": ledger.count("boros_trades", "WHERE state='open'"),
        "matured": ledger.count("boros_trades", "WHERE state='matured'"),
        "closed": ledger.count("boros_trades", "WHERE state='closed'"),
        "liquidated": ledger.count(
            "boros_trades", "WHERE state='liquidated'"),
        "marks": ledger.count("boros_marks"),
        "closed_clusters": closed_clusters(ledger),
        # per-structure (conformance 2026-10-19): counts only
        "opportunities_spread": ledger.count(
            "boros_opportunities", "WHERE structure='spread'"),
        "opportunities_simple": ledger.count(
            "boros_opportunities", "WHERE structure='simple'"),
        "near_entries_spread": ledger.count(
            "boros_opportunities",
            "WHERE structure='spread' AND entered=0 AND net_apy >= ?",
            (spec.NEAR_MISS_APY,)),
        "near_entries_simple": ledger.count(
            "boros_opportunities",
            "WHERE structure='simple' AND entered=0 AND net_apy >= ?",
            (spec.NEAR_MISS_APY,)),
        "entries_spread": ledger.count(
            "boros_trades", "WHERE structure='spread'"),
        "entries_simple": ledger.count(
            "boros_trades", "WHERE structure='simple'"),
    }
    kill_flags = {
        # booleans only — prereg permits kill surfacing pre-gate
        "liquidation": bool(ledger.flag_get("liquidation")),
        "n5_kill": bool(ledger.flag_get("n5_kill")),
        "split_liq": bool(ledger.flag_get("split_liq")),
    }
    return {
        "hypothesis": "BorosLockedSpread",
        "spec": "docs/PREREGISTER_BOROS_SPREAD.md",
        "generated_ms": now_ms,
        "counts": counts,
        "kill_flags": kill_flags,
        "read_gate": {
            "min_clusters": spec.READ_MIN_CLUSTERS,
            "expiry": spec.EXPIRY_ISO,
            "mode": "counts_only",   # no PnL until the gate opens
        },
    }
