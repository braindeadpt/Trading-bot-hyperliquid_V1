#!/usr/bin/env python3
"""Research watchdog supervisor — one process, one shared state, three gates.

Unifies the three auto-rerun evidence gates that previously ran as separate
``nohup`` processes:

  * **Top-trader bias screening** re-runs
    ``scripts/feature_screening_top_trader_bias.py --json-out`` once
    ``top_trader_bias_samples`` covers ≥20 distinct UTC dates (the strict
    bootstrap gate is structurally unreachable below that).
  * **Liquidation flush recheck** re-runs ``scripts/liquidation_flush_shadow.py``
    once the real okx/bybit feed spans ≥30 days.
  * **IV gate shadow recheck** re-runs the high_iv vs low_iv comparison
    (``scripts/iv_gate_shadow_recheck.py``) once ≥30 closed executed trades
    carry an IV decision, and decides shadow vs enforcement (threshold 66.7).
  * **Feed age creep** (``scripts/feed_age_creep_recheck.py``) monitors the
    daily max-age rollup and alerts when any contracted feed shows a
    consistent staircase (non-decreasing daily max over ≥5 days, growing a
    meaningful fraction of its silence threshold) — fire-once per episode,
    re-armed when the feed recovers.
  * **Feed cadence** (``scripts/feed_cadence_diagnostic.py``) compares each
    contracted feed's recent inter-event gaps against its own historical
    p95/p99 and alerts when a feed turns **DEGRADING** (recent median above
    its historical p99 — consistently slower than it used to be) —
    fire-once per episode, re-armed on recovery.

All five are **read-only evidence gates**: they run probes and write decision
reports; nothing here trades or touches the OMS.

State is held in ONE gitignored file (``data/research/research_watchdogs_state.json``)
with a per-watchdog subtree, so a restart never re-triggers a completed run
and the dashboard reads a single source of truth. On first run the supervisor
migrates the legacy per-watchdog state files
(``top_trader_bias_recheck_state.json`` / ``liquidation_flush_recheck_state.json``)
so an already-consumed trigger is not re-fired.

The probe/verdict/report helpers are imported from the two original scripts
(no duplication): ``scripts/top_trader_bias_recheck.py`` and
``scripts/liquidation_flush_recheck.py`` remain the metric + report home.

Usage:
  python scripts/research_watchdog_supervisor.py            # daemon (every 6h)
  python scripts/research_watchdog_supervisor.py --once     # single check, exit
  python scripts/research_watchdog_supervisor.py --force    # run probes now
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATE_DIR = ROOT / "data" / "research"
STATE_PATH = STATE_DIR / "research_watchdogs_state.json"

CHECK_HOURS = 6.0

WATCHDOG_IDS = (
    "top_trader_bias",
    "liquidation_flush",
    "iv_gate_shadow",
    "feed_age_creep",
    "feed_cadence",
)

# ── helpers reused from the per-gate scripts (single source of truth) ──
from scripts.top_trader_bias_recheck import (  # noqa: E402
    TARGET_DATES as BIAS_TARGET_DATES,
    best_candidate,
    bias_date_count,
    load_result,
    run_probe as run_bias_probe,
    verdict as bias_verdict,
    write_report as write_bias_report,
)
from scripts.liquidation_flush_recheck import (  # noqa: E402
    TARGET_DAYS as FLUSH_TARGET_DAYS,
    extract_cell,
    real_span_days,
    run_simulation as run_flush_simulation,
    verdict as flush_verdict,
    write_report as write_flush_report,
)
from scripts.iv_gate_shadow_recheck import (  # noqa: E402
    IV_THRESHOLD,
    REPORT_PATH as IV_REPORT_PATH,
    TARGET_CLOSED as IV_TARGET_CLOSED,
    iv_decision_count,
    run_comparison as run_iv_comparison,
    verdict as iv_verdict,
    write_report as write_iv_report,
)
from scripts.feed_age_creep_recheck import (  # noqa: E402
    CREEP_MIN_DAYS,
    detect_creeping_age,
    resolve_contracts,
    write_report as write_creep_report,
)
from scripts.feed_cadence_diagnostic import (  # noqa: E402
    DEFAULT_DB as CADENCE_DEFAULT_DB,
    run_cadence_diagnostic,
)
from src.core.engine import feed_silence_contracts  # noqa: E402
from src.utils.config import load_config  # noqa: E402

# Legacy per-watchdog state files (migration source on first run).
LEGACY_STATE_PATHS: Dict[str, Path] = {
    "top_trader_bias": STATE_DIR / "top_trader_bias_recheck_state.json",
    "liquidation_flush": STATE_DIR / "liquidation_flush_recheck_state.json",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def build_alert_notifier() -> Optional[Any]:
    """Build the AlertNotifier from config + env (same resolution as main.py).

    Returns None when alerts are disabled or unconfigured — the watchdog then
    only logs the PROMOTE decision (never blocks the gate on a missing
    notifier).
    """
    try:
        from src.alerts.notifier import AlertConfig, AlertNotifier
        from src.utils.config import load_config

        cfg = load_config()
        token = (cfg.get("alerts.telegram_bot_token") or "").strip()
        if not token:
            token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_ids: list = []
        raw_chat = (cfg.get("alerts.telegram_chat_id") or "").strip()
        if not raw_chat:
            raw_chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if raw_chat:
            chat_ids.append(raw_chat)
        extra = (os.environ.get("TELEGRAM_CHAT_IDS") or "").strip()
        if extra:
            chat_ids.extend(c.strip() for c in extra.split(",") if c.strip())
        alert_cfg = AlertConfig(
            enabled=bool(cfg.get("alerts.enabled", False)),
            telegram_bot_token=token or None,
            telegram_chat_id=chat_ids[0] if chat_ids else None,
            discord_webhook_url=cfg.get("alerts.discord_webhook_url"),
            min_level=cfg.get("alerts.min_level", "info"),
        )
        notifier = AlertNotifier(alert_cfg)
        if not alert_cfg.enabled:
            log("alerts disabled (alerts.enabled=false) — PROMOTE será só logado")
        return notifier
    except Exception as exc:  # noqa: BLE001
        log(f"alert notifier unavailable: {exc}")
        return None


def notify_iv_promote(report: Dict[str, Any], v: Dict[str, Any]) -> None:
    """Fire the PROMOTE alert best-effort — never blocks the gate.

    Builds the notifier per call (cheap, and the supervisor may run outside
    the bot process), sends with a timeout, and swallows every failure so the
    watchdog's own decision/state flow is unaffected.
    """
    notifier = build_alert_notifier()
    if notifier is None:
        log("iv: PROMOTE alert skipped — no notifier")
        return
    try:
        asyncio.run(
            asyncio.wait_for(
                notifier.iv_gate_promote(report=report, verdict=v),
                timeout=15,
            )
        )
        log("iv: PROMOTE alert sent")
    except Exception as exc:  # noqa: BLE001
        log(f"iv: PROMOTE alert failed (best-effort): {exc}")


def fresh_state() -> Dict[str, Dict[str, Any]]:
    """Empty shared state: one (triggered, runs) subtree per watchdog.

    ``feed_age_creep`` additionally keeps ``feeds_alerted`` — feed -> day
    when the current alert episode started. A feed stays in the map while
    it keeps creeping (no re-alert), and is dropped when it recovers so a
    new episode re-arms.
    """
    return {
        "top_trader_bias": {"triggered": False, "runs": []},
        "liquidation_flush": {"triggered": False, "runs": []},
        "iv_gate_shadow": {"triggered": False, "runs": []},
        "feed_age_creep": {
            "triggered": False,
            "runs": [],
            "feeds_alerted": {},
        },
        "feed_cadence": {
            "triggered": False,
            "runs": [],
            "feeds_alerted": {},
        },
    }


def _normalize_sub(raw: Any) -> Dict[str, Any]:
    sub = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {
        "triggered": bool(sub.get("triggered", False)),
        "runs": list(sub.get("runs") or []),
    }
    if sub.get("feeds_alerted") is not None:
        out["feeds_alerted"] = dict(sub.get("feeds_alerted") or {})
    return out


def load_shared_state(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load the shared state; migrate legacy per-watchdog files if needed.

    The shared file is canonical. When it is missing, adopt the legacy files'
    ``triggered`` / ``runs`` (so a completed gate is never re-fired after the
    upgrade) and persist the merged result.
    """
    state_path = path or STATE_PATH
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            state = fresh_state()
            for key in WATCHDOG_IDS:
                state[key] = _normalize_sub(raw.get(key))
            return state
        except Exception:
            log("shared state corrupt — rebuilding from legacy files")

    state = fresh_state()
    migrated = False
    for key, legacy in LEGACY_STATE_PATHS.items():
        if legacy.exists():
            try:
                old = json.loads(legacy.read_text(encoding="utf-8"))
                state[key] = _normalize_sub(old)
                migrated = True
                log(f"migrated '{key}' state from {legacy.name}")
            except Exception:
                log(f"legacy state for '{key}' unreadable — starting fresh")
    if migrated:
        save_shared_state(state, path=state_path)
    return state


def save_shared_state(
    state: Dict[str, Dict[str, Any]], path: Optional[Path] = None
) -> None:
    state_path = path or STATE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


# ── bias screening gate ──────────────────────────────────────────────

def check_bias(
    shared: Dict[str, Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    """Re-run the bias screening probe when ≥20 dates and not yet triggered."""
    sub = shared["top_trader_bias"]
    if sub.get("triggered") and not force:
        n_dates, n_samples, _mn, _mx = bias_date_count()
        log(f"bias: watch-only — already triggered ({n_dates} datas, "
            f"{n_samples} amostras)")
        return False
    n_dates, n_samples, _mn, _mx = bias_date_count()
    log(f"bias: {n_dates}/{BIAS_TARGET_DATES} datas ({n_samples} amostras)")
    if not force and n_dates < BIAS_TARGET_DATES:
        log(f"bias: abaixo de {BIAS_TARGET_DATES} datas — a saltar "
            f"({n_dates / BIAS_TARGET_DATES * 100:.0f}%)")
        return False

    json_path = run_bias_probe()
    if json_path is None:
        log("bias: recheck abortado — probe sem JSON")
        return False
    data = load_result(json_path)
    cells = data.get("cells") or []
    meta = data.get("meta") or {}
    v = bias_verdict(cells, int(meta.get("n_dates") or n_dates))
    write_bias_report(cells, meta, n_dates=n_dates, n_samples=n_samples, v=v)

    best = best_candidate(cells)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_dates": int(meta.get("n_dates") or n_dates),
        "n_samples": n_samples,
        "verdict": v,
        "n_survived": sum(
            1 for c in cells if not c.get("is_control") and c.get("survives")
        ),
        "best_feature": best.get("feature") if best else None,
        "best_ic": round(float(best.get("ic") or 0.0), 4) if best else None,
        "json": str(json_path),
    }
    sub["runs"].append(run)
    if not force:
        sub["triggered"] = True
    save_shared_state(shared)
    log(f"bias: gate completo ({run['n_dates']} datas, "
        f"{run['n_survived']} survived) -> {v}")
    return True


# ── liquidation flush gate ───────────────────────────────────────────

def check_flush(
    shared: Dict[str, Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    """Re-run the flush simulation when the real feed spans ≥30 days."""
    sub = shared["liquidation_flush"]
    if sub.get("triggered") and not force:
        span, n_events = real_span_days()
        log(f"flush: watch-only — already triggered ({span:.2f}d, "
            f"{n_events} eventos)")
        return False
    span, n_events = real_span_days()
    log(f"flush: feed real {span:.2f}/{FLUSH_TARGET_DAYS} dias "
        f"({n_events} eventos)")
    if not force and span < FLUSH_TARGET_DAYS:
        log(f"flush: abaixo de {FLUSH_TARGET_DAYS}d — a saltar "
            f"({span / FLUSH_TARGET_DAYS * 100:.0f}%)")
        return False

    sim_path = run_flush_simulation()
    if sim_path is None:
        log("flush: recheck abortado — simulação sem JSON")
        return False
    data = json.loads(sim_path.read_text(encoding="utf-8"))
    cell = extract_cell(data.get("results") or [], "real")
    if cell is None:
        log("flush: recheck abortado — célula REAL ETH p90/30m/fade não encontrada")
        return False

    write_flush_report(cell, span, n_events, sim_path)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "span_days": round(span, 2),
        "events": n_events,
        "sim_json": str(sim_path),
        "cell": cell,
        "verdict": flush_verdict(cell),
    }
    sub["runs"].append(run)
    if not force:
        sub["triggered"] = True
    save_shared_state(shared)
    log(f"flush: gate completo (n={cell['n']} PF={cell['profit_factor']} "
        f"avg={cell['avg_net_bps']:+.2f}bps) -> {run['verdict']}")
    return True


# ── IV gate shadow gate ───────────────────────────────────────────────

def check_iv_gate(
    shared: Dict[str, Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    """Re-run the IV comparison when ≥30 closed trades carry an IV decision.

    Decides shadow vs enforcement (threshold 66.7) via the recheck verdict.
    """
    sub = shared["iv_gate_shadow"]
    if sub.get("triggered") and not force:
        n_closed, _h, _l = iv_decision_count()
        log(f"iv: watch-only — already triggered ({n_closed} closed com decisão IV)")
        return False
    n_closed, n_high, n_low = iv_decision_count()
    log(f"iv: {n_closed}/{IV_TARGET_CLOSED} closed com decisão IV "
        f"(high_iv={n_high}, low_iv={n_low})")
    if not force and n_closed < IV_TARGET_CLOSED:
        log(f"iv: abaixo de {IV_TARGET_CLOSED} closed — a saltar "
            f"({n_closed / IV_TARGET_CLOSED * 100:.0f}%)")
        return False

    report = run_iv_comparison()
    if report is None:
        log("iv: recheck abortado — sem relatório de comparação")
        return False
    v = iv_verdict(report)
    write_iv_report(report, v, n_closed, n_high, n_low)
    run = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_closed": n_closed,
        "n_high_closed": n_high,
        "n_low_closed": n_low,
        "verdict": v["status"],
        "detail": v["detail"],
        "threshold": IV_THRESHOLD,
        "report_path": str(IV_REPORT_PATH),
    }
    sub["runs"].append(run)
    if not force:
        sub["triggered"] = True
    save_shared_state(shared)
    # Human-in-the-loop: a PROMOTE is a recommendation to flip the router
    # from shadow to enforcement — notify the operator with the exact diff.
    if v["status"] == "PROMOTE":
        notify_iv_promote(report, run)
    log(f"iv: gate completo (n={n_closed}, high={n_high}, low={n_low}) -> {run['verdict']}")
    return True


# ── feed age creep watchdog ──────────────────────────────────────────

def notify_creeping_age(feed: str, d: Dict[str, Any], run: Dict[str, Any]) -> None:
    """Fire the creep alert best-effort — never blocks the detector.

    Same contract as ``notify_iv_promote``: build the notifier per call,
    send with a timeout, swallow every failure.
    """
    notifier = build_alert_notifier()
    if notifier is None:
        log(f"creep: alert skipped for `{feed}` — no notifier")
        return
    msg = (
        f"⚠️ <b>FEED AGE CREEP</b>\n"
        f"`{feed}` max age diário a subir há {d['days']} dias consecutivos — "
        f"escada de {d['first_max_age_sec'] / 3600:.1f}h → "
        f"{d['last_max_age_sec'] / 3600:.1f}h "
        f"(+{d['growth_frac'] * 100:.0f}% do threshold)\n"
        f"A cadência está a degradar antes do silêncio — verificar o path "
        f"de entrega. Episódio: {run['ts']}"
    )
    try:
        asyncio.run(
            asyncio.wait_for(notifier.send(msg, "warning"), timeout=15)
        )
        log(f"creep: alert sent for `{feed}`")
    except Exception as exc:  # noqa: BLE001
        log(f"creep: alert failed for `{feed}` (best-effort): {exc}")


def check_creeping_age(
    shared: Dict[str, Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    """Alert when any contracted feed shows consistent daily max-age growth.

    Edge-triggered per feed: a feed alerts once when it *starts* creeping
    (enters ``feeds_alerted``) and stays quiet while the episode continues;
    when it recovers (no longer flagged), it is dropped from
    ``feeds_alerted`` and a later re-creep alerts again. ``--force`` does not
    consume the episode state (it only forces a fresh detection + report).
    """
    sub = shared["feed_age_creep"]
    contracts = resolve_contracts()
    detected = detect_creeping_age(contracts)
    creeping = {f: d for f, d in detected.items() if d.get("creeping")}
    alerted = dict(sub.get("feeds_alerted") or {})
    fired = False
    for feed, d in sorted(creeping.items()):
        if feed in alerted:
            continue  # already alerting this episode
        alerted[feed] = d["last_day_start_ms"]
        run = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feed": feed,
            "days": d["days"],
            "first_max_age_sec": d["first_max_age_sec"],
            "last_max_age_sec": d["last_max_age_sec"],
            "growth_sec": d["growth_sec"],
            "growth_frac": d["growth_frac"],
            "verdict": "CREEP DETECTED",
        }
        sub["runs"].append(run)
        sub["triggered"] = True
        fired = True
        notify_creeping_age(feed, d, run)
    # Recovery: a feed that stopped creeping leaves the alerted map, so the
    # next episode re-arms the alert.
    recovered = [f for f in alerted if f not in creeping]
    for feed in recovered:
        del alerted[feed]
    sub["feeds_alerted"] = alerted
    write_creep_report(detected, contracts)
    save_shared_state(shared)
    if recovered:
        log(f"creep: {len(recovered)} feed(s) recuperaram — episódio fechado "
            f"({', '.join(sorted(recovered))})")
    if creeping:
        log(f"creep: {len(creeping)} feed(s) em crescimento consistente "
            f"({', '.join(sorted(creeping))})")
    else:
        log("creep: sem feeds com max age diário em crescimento")
    return fired


# ── feed cadence watchdog ────────────────────────────────────────────

def cadence_diagnostic() -> Dict[str, Any]:
    """Live cadence verdict per contracted feed (OK/WATCH/DEGRADING...).

    Reuses ``run_cadence_diagnostic`` from the diagnostic script with THIS
    deployment's contracts and live DB — the single function the CLI, the
    dashboard and this watchdog all read, so the panel and the alert can
    never disagree on a feed's status.
    """
    contracts = feed_silence_contracts(load_config())
    return run_cadence_diagnostic(CADENCE_DEFAULT_DB, contracts)


def notify_cadence_degrading(feed: str, d: Dict[str, Any], run: Dict[str, Any]) -> None:
    """Fire the DEGRADING alert best-effort — never blocks the detector.

    Same contract as ``notify_creeping_age``: build the notifier per call,
    send with a timeout, swallow every failure.
    """
    notifier = build_alert_notifier()
    if notifier is None:
        log(f"cadence: alert skipped for `{feed}` — no notifier")
        return
    msg = (
        f"⚠️ <b>FEED CADENCE DEGRADING</b>\n"
        f"`{feed}` está consistentemente mais lento que o seu histórico: "
        f"mediana recente {d['recent_median_sec'] / 60:.1f}m vs p99 "
        f"histórico {d['hist_p99_sec'] / 60:.1f}m "
        f"(trend {d.get('trend_sec_per_gap', 0):+.2f}s/gap)\n"
        f"Entrega a degradar antes do silêncio — verificar o path. "
        f"Episódio: {run['ts']}"
    )
    try:
        asyncio.run(
            asyncio.wait_for(notifier.send(msg, "warning"), timeout=15)
        )
        log(f"cadence: alert sent for `{feed}`")
    except Exception as exc:  # noqa: BLE001
        log(f"cadence: alert failed for `{feed}` (best-effort): {exc}")


def check_cadence_degrading(
    shared: Dict[str, Dict[str, Any]],
    *,
    force: bool = False,
) -> bool:
    """Alert when any contracted feed turns DEGRADING (recent cadence
    consistently slower than its own historical p99).

    Edge-triggered per feed, exactly like ``check_creeping_age``: a feed
    alerts once when it *starts* DEGRADING (enters ``feeds_alerted``) and
    stays quiet while the episode continues; when the verdict recovers
    (OK/WATCH/insufficient/no_data), it is dropped and a later re-degrading
    alerts again. ``--force`` only forces a fresh diagnostic; it never
    consumes episode state.
    """
    sub = shared["feed_cadence"]
    report = cadence_diagnostic()
    degrading = {
        f: d for f, d in (report.get("feeds") or {}).items()
        if d.get("status") == "DEGRADING"
    }
    alerted = dict(sub.get("feeds_alerted") or {})
    fired = False
    for feed, d in sorted(degrading.items()):
        if feed in alerted:
            continue  # already alerting this episode
        alerted[feed] = int(report.get("now_ms") or time.time() * 1000)
        run = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feed": feed,
            "status": "DEGRADING",
            "recent_median_sec": d.get("recent_median_sec"),
            "hist_p99_sec": d.get("hist_p99_sec"),
            "hist_p95_sec": d.get("hist_p95_sec"),
            "latest_gap_sec": d.get("latest_gap_sec"),
            "trend_sec_per_gap": d.get("trend_sec_per_gap"),
            "verdict": "DEGRADING",
        }
        sub["runs"].append(run)
        sub["triggered"] = True
        fired = True
        notify_cadence_degrading(feed, d, run)
    recovered = [f for f in alerted if f not in degrading]
    for feed in recovered:
        del alerted[feed]
    sub["feeds_alerted"] = alerted
    save_shared_state(shared)
    if recovered:
        log(f"cadence: {len(recovered)} feed(s) recuperaram — episódio fechado "
            f"({', '.join(sorted(recovered))})")
    if degrading:
        log(f"cadence: {len(degrading)} feed(s) DEGRADING "
            f"({', '.join(sorted(degrading))})")
    else:
        log("cadence: todos os feeds contratados mantêm a cadência histórica")
    return fired


def check_all(
    shared: Dict[str, Dict[str, Any]], *, force: bool = False
) -> Tuple[bool, bool, bool, bool, bool]:
    """Run all five gates once. Returns (bias_ran, flush_ran, iv_ran,
    creep_ran, cadence_ran)."""
    bias_ran = check_bias(shared, force=force)
    flush_ran = check_flush(shared, force=force)
    iv_ran = check_iv_gate(shared, force=force)
    creep_ran = check_creeping_age(shared, force=force)
    cadence_ran = check_cadence_degrading(shared, force=force)
    return bias_ran, flush_ran, iv_ran, creep_ran, cadence_ran


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="single check of both gates, then exit")
    ap.add_argument("--force", action="store_true",
                    help="run both probes now regardless of progress (smoke test / manual)")
    ap.add_argument("--hours", type=float, default=CHECK_HOURS,
                    help="check interval hours (daemon mode)")
    args = ap.parse_args()

    log("=== research watchdog supervisor "
        f"(bias >= {BIAS_TARGET_DATES} datas, flush >= {FLUSH_TARGET_DAYS}d real, "
        f"iv >= {IV_TARGET_CLOSED} closed com decisão, "
        f"creep >= {CREEP_MIN_DAYS}d de escada no max diário, "
        f"cadence DEGRADING (rec mediana > p99 histórico), "
        f"check a cada {args.hours:.0f}h) ===")
    shared = load_shared_state()
    # Always persist the canonical shared file (fresh or migrated) so the
    # dashboard reads the same single source of truth from day one.
    save_shared_state(shared)
    if args.once:
        check_all(shared, force=args.force)
        return 0
    while True:
        check_all(shared, force=False)
        time.sleep(args.hours * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
