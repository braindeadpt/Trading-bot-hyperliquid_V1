"""Dashboard payload for the research watchdog auto-rerun gates.

One supervisor process runs both evidence gates (bias screening + liquidation
flush) and writes a SINGLE shared state file
(``scripts/research_watchdog_supervisor.py`` ->
``data/research/research_watchdogs_state.json``). This module reads the live
DBs + that shared state and shapes them into a small read-only payload for
the dashboard. It imports the supervisors'/scripts' pure metric helpers so
the thresholds and queries stay single-source-of-truth.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from scripts.iv_gate_shadow_recheck import (
    TARGET_CLOSED as IV_TARGET_CLOSED,
    iv_decision_count,
)
from scripts.liquidation_flush_recheck import (
    TARGET_DAYS as FLUSH_TARGET_DAYS,
)
from scripts.liquidation_flush_recheck import real_span_days
from scripts.research_watchdog_supervisor import load_shared_state
from scripts.top_trader_bias_recheck import (
    TARGET_DATES as BIAS_TARGET_DATES,
)
from scripts.top_trader_bias_recheck import bias_date_count


def _progress_pct(current: float, target: int) -> float:
    if target <= 0:
        return 0.0
    return round(min(100.0, 100.0 * current / target), 1)


def _bias_watchdog() -> Dict[str, Any]:
    n_dates, n_samples, _mn, _mx = bias_date_count()
    state = load_shared_state()["top_trader_bias"]
    runs = state.get("runs") or []
    return {
        "id": "top_trader_bias",
        "label": "Top-trader bias screening",
        "script": "scripts/research_watchdog_supervisor.py",
        "metric_label": "datas de bias cobertas",
        "unit": "datas",
        "current": n_dates,
        "target": BIAS_TARGET_DATES,
        "progress_pct": _progress_pct(n_dates, BIAS_TARGET_DATES),
        "samples": n_samples,
        "triggered": bool(state.get("triggered")),
        "last_run": runs[-1] if runs else None,
        "report_path": "docs/TOP_TRADER_BIAS_RECHECK_RESULT.md",
        "probe_report_path": "docs/FEATURE_SCREENING_TOP_TRADER_BIAS.md",
    }


def _flush_watchdog() -> Dict[str, Any]:
    span_days, n_events = real_span_days()
    state = load_shared_state()["liquidation_flush"]
    runs = state.get("runs") or []
    return {
        "id": "liquidation_flush",
        "label": "Liquidation flush recheck",
        "script": "scripts/research_watchdog_supervisor.py",
        "metric_label": "dias do feed real (okx/bybit)",
        "unit": "dias",
        "current": round(span_days, 2),
        "target": FLUSH_TARGET_DAYS,
        "progress_pct": _progress_pct(span_days, FLUSH_TARGET_DAYS),
        "samples": n_events,
        "triggered": bool(state.get("triggered")),
        "last_run": runs[-1] if runs else None,
        "report_path": "docs/LIQUIDATION_FLUSH_RECHECK_RESULT.md",
    }


def _iv_gate_watchdog() -> Dict[str, Any]:
    n_closed, n_high, n_low = iv_decision_count()
    state = load_shared_state()["iv_gate_shadow"]
    runs = state.get("runs") or []
    return {
        "id": "iv_gate_shadow",
        "label": "IV gate shadow recheck",
        "script": "scripts/research_watchdog_supervisor.py",
        "metric_label": "closed trades com decisão IV",
        "unit": "trades",
        "current": n_closed,
        "target": IV_TARGET_CLOSED,
        "progress_pct": _progress_pct(n_closed, IV_TARGET_CLOSED),
        "samples": n_high + n_low,
        "triggered": bool(state.get("triggered")),
        "last_run": runs[-1] if runs else None,
        "report_path": "docs/IV_GATE_SHADOW_RECHECK_RESULT.md",
    }


def build_research_watchdogs_payload() -> Dict[str, Any]:
    """Assemble the read-only watchdog status (bias + flush + iv gate)."""
    watchdogs: List[Dict[str, Any]] = []
    builders = [
        ("top_trader_bias", _bias_watchdog),
        ("liquidation_flush", _flush_watchdog),
        ("iv_gate_shadow", _iv_gate_watchdog),
    ]
    for wd_id, builder in builders:
        try:
            watchdogs.append(builder())
        except Exception as exc:  # noqa: BLE001
            # A watchdog with a broken DB/state must not take the panel down.
            watchdogs.append({"id": wd_id, "error": str(exc)})
    return {
        "generated_ms": int(time.time() * 1000),
        "watchdogs": watchdogs,
    }
