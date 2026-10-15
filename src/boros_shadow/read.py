"""Gated read (prereg §Leitura) — the only surface that ever sees PnL.

Refuses to run until n >= 10 independent closed clusters or the expiry
date (2027-06-30), whichever first. When it runs it reports the four
PASS criteria plus the kill counters. This module is import-safe: no
side effects, ledger read-only.
"""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from typing import Any, Dict

from src.boros_shadow import spec
from src.boros_shadow.ledger import BorosLedger
from src.boros_shadow.settle import closed_clusters


def _expiry_ms() -> int:
    return int(datetime.fromisoformat(spec.EXPIRY_ISO)
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def gate_open(ledger: BorosLedger, now_ms: int | None = None) -> bool:
    now_ms = now_ms or int(time.time() * 1000)
    return closed_clusters(ledger) >= spec.READ_MIN_CLUSTERS \
        or now_ms >= _expiry_ms()


def run_read(ledger: BorosLedger,
             now_ms: int | None = None) -> Dict[str, Any]:
    """The sealed read. Returns {'refused': True, ...} before the gate;
    the full PASS/FAIL report after."""
    now_ms = now_ms or int(time.time() * 1000)
    n_clusters = closed_clusters(ledger)
    if not gate_open(ledger, now_ms):
        return {"refused": True, "hypothesis": "BorosLockedSpread",
                "closed_clusters": n_clusters,
                "min_clusters": spec.READ_MIN_CLUSTERS,
                "expiry": spec.EXPIRY_ISO}
    closes = ledger.closes()
    n_liq = sum(1 for c in closes
                if str(c["reason"]).startswith("liquidated"))
    pnl = [c["realized_usd"] for c in closes]
    pos = sum(p for p in pnl if p > 0)
    neg = sum(-p for p in pnl if p < 0)
    pf = pos / neg if neg > 0 else (float("inf") if pos > 0 else 0.0)
    apys = [c["realized_apy_cap"] for c in closes]
    ratios = [c["ratio"] for c in closes]
    criteria = {
        "mean_realized_apy_cap_ge_8pct": (
            statistics.mean(apys) >= spec.NET_APY_MIN if apys else False),
        "median_ratio_ge_0_7": (
            statistics.median(ratios) >= spec.READ_RATIO_MIN
            if ratios else False),
        "profit_factor_gt_1_5": pf > spec.READ_PF_MIN,
        "zero_pooled_boros_liquidations": n_liq == 0,
    }
    return {
        "refused": False,
        "hypothesis": "BorosLockedSpread",
        "generated_ms": now_ms,
        "n_closed_trades": len(closes),
        "n_closed_clusters": n_clusters,
        "n_liquidations": n_liq,
        "mean_realized_apy_cap": (statistics.mean(apys)
                                  if apys else None),
        "median_ratio": statistics.median(ratios) if ratios else None,
        "profit_factor": pf,
        "criteria": criteria,
        "verdict": "PASS" if all(criteria.values()) else "FAIL",
    }
