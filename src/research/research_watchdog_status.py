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
    project_decision,
    run_comparison as run_iv_comparison,
)
from scripts.feed_age_creep_recheck import (  # noqa: E402
    CREEP_MIN_DAYS,
    detect_creeping_age,
    resolve_contracts as resolve_creep_contracts,
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
    """IV gate watchdog with the projected decision from the CURRENT slices.

    One ``run_iv_comparison()`` call feeds both the progress counters and the
    projection (PROMOTE/REJECT before the n>=30 trigger fires). A broken DB
    degrades to zero counters + an N/A projection — never an error.
    """
    state = load_shared_state()["iv_gate_shadow"]
    runs = state.get("runs") or []
    report = run_iv_comparison()
    if report is None or report.get("error"):
        n_closed = n_high = n_low = 0
        projected = {
            "status": "N/A",
            "provisional": True,
            "n_closed": 0,
            "high_net_usd": None,
            "low_net_usd": None,
            "detail": "sem relatório de comparação (DB em falta?).",
        }
    else:
        hi = report["slices"]["high_iv"]
        lo = report["slices"]["low_iv"]
        n_high = hi["n_closed"] or 0
        n_low = lo["n_closed"] or 0
        n_closed = n_high + n_low
        projected = project_decision(hi, lo)
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
        "projected": projected,
        "triggered": bool(state.get("triggered")),
        "last_run": runs[-1] if runs else None,
        "report_path": "docs/IV_GATE_SHADOW_RECHECK_RESULT.md",
    }


def _creeping_age_watchdog() -> Dict[str, Any]:
    """Feed age creep — feeds with consistent daily max-age growth.

    Reads the live detection (research DB rollup + deployment contracts) and
    the supervisor's shared state (episodes alerted so far). ``current`` is
    the number of feeds creeping RIGHT NOW; ``feeds`` carries the detail for
    the panel; ``target`` is 0 so the progress bar stays neutral.
    """
    detected = detect_creeping_age(resolve_creep_contracts())
    state = load_shared_state()["feed_age_creep"]
    runs = state.get("runs") or []
    feeds = [
        {
            "feed": f,
            "days": d["days"],
            "first_max_age_sec": d["first_max_age_sec"],
            "last_max_age_sec": d["last_max_age_sec"],
            "growth_sec": d["growth_sec"],
            "growth_frac": d["growth_frac"],
            "last_day_start_ms": d["last_day_start_ms"],
        }
        for f, d in sorted(detected.items())
        if d.get("creeping")
    ]
    return {
        "id": "feed_age_creep",
        "label": "Feed age creep (max diário a subir)",
        "script": "scripts/research_watchdog_supervisor.py",
        "metric_label": "feeds com max age diário em crescimento",
        "unit": "feeds",
        "current": len(feeds),
        "target": 0,
        "progress_pct": 0.0,
        "min_days": CREEP_MIN_DAYS,
        "samples": len(runs),
        "feeds": feeds,
        "triggered": bool(state.get("triggered")),
        "last_run": runs[-1] if runs else None,
        "report_path": "docs/FEED_AGE_CREEP_RECHECK_RESULT.md",
    }


def build_research_watchdogs_payload() -> Dict[str, Any]:
    """Assemble the read-only watchdog status (bias + flush + iv + creep)."""
    watchdogs: List[Dict[str, Any]] = []
    builders = [
        ("top_trader_bias", _bias_watchdog),
        ("liquidation_flush", _flush_watchdog),
        ("iv_gate_shadow", _iv_gate_watchdog),
        ("feed_age_creep", _creeping_age_watchdog),
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
