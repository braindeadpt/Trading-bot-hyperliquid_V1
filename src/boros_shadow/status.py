"""Counts-only status board — feeds a future /api/hypotheses row.

The read gate is the contract: until n>=10 closed clusters or
2027-06-30, this board exposes counts and kill-flag placeholders ONLY —
no PnL, no bps, no returns, no APY. (Kill flags are a Phase-2 surface;
they sit at zero until liquidations go live.)
"""
from __future__ import annotations

import time
from typing import Any, Dict

from src.boros_shadow import spec
from src.boros_shadow.ledger import BorosLedger


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
    }
    return {
        "hypothesis": "BorosLockedSpread",
        "spec": "docs/PREREGISTER_BOROS_SPREAD.md",
        "generated_ms": now_ms,
        "counts": counts,
        "kill_flags": {},          # Phase 2 — liquidation watches not live
        "read_gate": {
            "min_clusters": spec.READ_MIN_CLUSTERS,
            "expiry": spec.EXPIRY_ISO,
            "mode": "counts_only",   # no PnL until the gate opens
        },
    }
