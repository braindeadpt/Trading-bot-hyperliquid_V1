#!/usr/bin/env python3
"""Validate the cadence detector — does it anticipate the 6h silence?

The cadence watchdog (``FEED CADENCE``) fires when a feed's current gap
exceeds its own historical p99 inter-event gap — the thinning signal
long before ``FEED SILENT`` trips at ``max_silence_sec`` (6h for
liquidation_okx). This script validates that claim against the REAL
history recorded in the live DB.

Data
----
* **Gaps** — ``liquidation_events`` rows with ``source='okx'`` (the real
  OKX prints the monitor beats on), ordered by ``timestamp_ms``.
* **Ground truth** — a gap that reaches ``max_silence_sec`` is the exact
  condition that would trip ``FEED SILENT`` (degraded): a real silence.

Method (walk-forward, same rule as production)
----------------------------------------------
* Walk the event timestamps one by one. After each event the rolling gap
  deque holds the inter-event gaps recorded so far (capped at
  ``gap_history``); the p99 baseline for the NEXT gap is the percentile
  of that deque (``cadence_percentile`` — the single shared function the
  monitor itself uses). A fire happens during a gap when the age crosses
  that p99 (fire-once per gap — an event is a beat, which resets the
  episode).
* A gap reaching ``max_silence_sec`` is a **degradation episode**. If a
  fire occurred in that same gap, the lead time is
  ``max_silence_sec - p99`` (how long before ``FEED SILENT`` the
  cadence warned); otherwise it is a **MISS** (typically cold start —
  fewer than ``min_samples`` gaps recorded yet).
* Fires in gaps that recovered before 6h are **unconfirmed** (warning
  without a real silence).
* The open gap after the last event is evaluated too (in-progress
  silence).

Cross-check with reality
------------------------
The research DB persists every alert the monitor actually emitted
(``feed_silence_alerts``, ``alert_type='cadence'``, since the ``on_alert``
sink was wired). The script matches those real fired timestamps against
the simulated fires to confirm the simulation reproduces what the
monitor did where both exist.

Outputs: a console table, ``docs/FEED_CADENCE_LEADTIME_VALIDATION.md``
and ``data/research/feed_cadence_leadtime.json`` (gitignored). Read-only
— never trades, never touches the OMS.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.engine import (  # noqa: E402
    feed_silence_cadence_gap_history,
    feed_silence_cadence_min_samples,
)
from src.data.market_data_health import cadence_percentile  # noqa: E402

DEFAULT_LIVE_DB = ROOT / "data" / "live" / "bot.db"
REPORT_PATH = ROOT / "docs" / "FEED_CADENCE_LEADTIME_VALIDATION.md"
JSON_PATH = ROOT / "data" / "research" / "feed_cadence_leadtime.json"
FEED = "liquidation_okx"
MAX_SILENCE_DEFAULT_SEC = 6 * 3600.0


def _load_config() -> Dict[str, Any]:
    cfg_path = ROOT / "config" / "settings.yaml"
    if not cfg_path.exists():
        return {}
    try:
        from src.config.loader import load_config

        cfg = load_config(str(cfg_path))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001 — best-effort; CLI flags override anyway
        return {}


def resolve_max_silence() -> float:
    """The deployment's liquidation_okx silence threshold (default 6h), the
    same value the engine passes to the monitor."""
    cfg = _load_config()
    md = cfg.get("market_data") or {}
    fs = md.get("feed_silence") or {}
    try:
        return float(fs.get("liquidation_okx_max_sec", MAX_SILENCE_DEFAULT_SEC))
    except (TypeError, ValueError):
        return MAX_SILENCE_DEFAULT_SEC


def load_okx_timestamps(live_db: Path, limit: int = 500_000) -> List[int]:
    """Ascending event timestamps (ms) of the real OKX prints in the live
    DB — the exact series the monitor beats on. Read-only connection."""
    if not live_db.exists():
        return []
    con = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE source = 'okx' ORDER BY timestamp_ms ASC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [int(r[0]) for r in rows]


def load_real_cadence_alerts(research_db: Path) -> List[int]:
    """fired_ms of the cadence alerts the monitor ACTUALLY emitted for this
    feed (research DB ``feed_silence_alerts``), ascending."""
    if not research_db.exists():
        return []
    con = sqlite3.connect(f"file:{research_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT fired_ms FROM feed_silence_alerts "
            "WHERE feed = ? AND alert_type = 'cadence' "
            "ORDER BY fired_ms ASC",
            (FEED,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [int(r[0]) for r in rows]


def _evaluate_gap(
    gap_index: int,
    start_ms: int,
    end_ms: int,
    gap_sec: float,
    baseline: Sequence[float],
    *,
    min_samples: int,
    gap_history: int,
    max_silence_sec: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Evaluate one gap (closed, or the open tail) against the baseline.

    Returns ``(fire_record, degradation_record)`` — each None when the
    condition does not hold. The baseline is the rolling deque of gaps
    recorded BEFORE this gap started (capped at ``gap_history``), exactly
    what the monitor's ``check_cadence`` sees.
    """
    baseline = list(baseline)
    p99 = cadence_percentile(baseline, 0.99, min_samples)
    fire: Optional[Dict[str, Any]] = None
    if p99 is not None and gap_sec > p99:
        fired_at = start_ms + int(p99 * 1000)
        confirmed = gap_sec >= max_silence_sec
        fire = {
            "gap_index": gap_index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "gap_sec": round(gap_sec, 1),
            "baseline_n": len(baseline),
            "p99_sec": round(p99, 1),
            "fired_at_ms": fired_at,
            "confirmed": confirmed,
            "lead_sec": (
                round(max_silence_sec - p99, 1) if confirmed else None
            ),
        }
    degradation: Optional[Dict[str, Any]] = None
    if gap_sec >= max_silence_sec:
        degradation = {
            "gap_index": gap_index,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "gap_sec": round(gap_sec, 1),
            "fired": p99 is not None and gap_sec > p99,
            "p99_sec": round(p99, 1) if p99 is not None else None,
        }
    return fire, degradation


def simulate_cadence_fires(
    timestamps_ms: Sequence[int],
    *,
    min_samples: int,
    gap_history: int,
    max_silence_sec: float,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Walk-forward simulation of the production cadence detector over the
    real event series (see module docstring for semantics)."""
    ts = sorted(int(t) for t in timestamps_ms if t is not None)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    gap_secs: List[float] = []
    for a, b in zip(ts, ts[1:]):
        # the monitor records only gaps >= 0 (negative = clock skew/dup)
        gap_secs.append(max(0.0, (b - a) / 1000.0))

    rolling: List[float] = []  # gaps recorded so far (capped at history)
    fires: List[Dict[str, Any]] = []
    degradations: List[Dict[str, Any]] = []
    for j, g in enumerate(gap_secs):
        baseline = rolling[max(0, len(rolling) - gap_history):]
        fire, degradation = _evaluate_gap(
            j, ts[j], ts[j + 1], g, baseline,
            min_samples=min_samples,
            gap_history=gap_history,
            max_silence_sec=max_silence_sec,
        )
        if fire is not None:
            fires.append(fire)
        if degradation is not None:
            degradations.append(degradation)
        rolling.append(g)

    # Open tail: the silence after the last event (in-progress episode).
    if ts and now > ts[-1]:
        open_gap = (now - ts[-1]) / 1000.0
        baseline = rolling[max(0, len(rolling) - gap_history):]
        fire, degradation = _evaluate_gap(
            len(gap_secs), ts[-1], now, open_gap, baseline,
            min_samples=min_samples,
            gap_history=gap_history,
            max_silence_sec=max_silence_sec,
        )
        if fire is not None:
            fires.append(fire)
        if degradation is not None:
            degradations.append(degradation)

    confirmed = [f for f in fires if f["confirmed"]]
    unconfirmed = [f for f in fires if not f["confirmed"]]
    anticipated = [d for d in degradations if d["fired"]]
    misses = [d for d in degradations if not d["fired"]]
    lead_secs = [float(f["lead_sec"]) for f in confirmed]
    return {
        "feed": FEED,
        "events": len(ts),
        "gaps": len(gap_secs),
        "min_samples": min_samples,
        "gap_history": gap_history,
        "max_silence_sec": max_silence_sec,
        "fires_total": len(fires),
        "confirmed": len(confirmed),
        "unconfirmed": len(unconfirmed),
        "degradations": len(degradations),
        "anticipated": len(anticipated),
        "misses": len(misses),
        "lead_secs": lead_secs,
        "avg_lead_sec": (
            round(sum(lead_secs) / len(lead_secs), 1) if lead_secs else None
        ),
        "min_lead_sec": min(lead_secs) if lead_secs else None,
        "max_lead_sec": max(lead_secs) if lead_secs else None,
        "first_event_ms": ts[0] if ts else None,
        "last_event_ms": ts[-1] if ts else None,
        "generated_ms": int(now),
        "fires": fires,
        "degradation_details": degradations,
    }


def cross_check_with_real(
    simulated: Dict[str, Any],
    real_alerts: Sequence[int],
    *,
    tolerance_sec: float = 900.0,
) -> Dict[str, Any]:
    """Match real cadence alerts (research DB) to simulated fires.

    A real alert matches if a simulated ``fired_at_ms`` falls within
    ``tolerance_sec`` of it. Reports recall of the simulation against
    what the monitor actually emitted (the sink only exists since the
    ``on_alert`` recorder was wired, so the real history may be short).
    """
    simulated = simulated or {}
    sim_fires = simulated.get("fires") or []
    matched_real = 0
    matched_sim = set()
    tol_ms = int(tolerance_sec * 1000)
    for real_ms in real_alerts:
        for i, f in enumerate(sim_fires):
            if i in matched_sim:
                continue
            if abs(int(f["fired_at_ms"]) - int(real_ms)) <= tol_ms:
                matched_real += 1
                matched_sim.add(i)
                break
    return {
        "real_alerts": len(real_alerts),
        "matched_real": matched_real,
        "matched_sim": len(matched_sim),
        "tolerance_sec": tolerance_sec,
        "real_window_start_ms": (
            min(real_alerts) if real_alerts else None
        ),
        "real_window_end_ms": (
            max(real_alerts) if real_alerts else None
        ),
    }


def _fmt_ts(ts_ms: Optional[int]) -> str:
    if ts_ms is None:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%m-%d %H:%M"
    )


def _fmt_dur(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    return f"{sec / 3600:.2f}h"


def render_markdown(report: Dict[str, Any]) -> str:
    a = report
    n_deg = a["degradations"]
    if n_deg:
        pct = 100.0 * a["anticipated"] / n_deg
        lead_txt = _fmt_dur(a["avg_lead_sec"]) if a["avg_lead_sec"] is not None else "—"
    else:
        pct = 0.0
        lead_txt = "—"
    lines = [
        "# Cadence detector — lead-time validation",
        "",
        "O detector `FEED CADENCE` avisa quando o gap actual ultrapassa o "
        "**p99 histórico** dos gaps do próprio feed — o sinal de thinning "
        "muito antes do `FEED SILENT` aos 6h. Este script valida essa "
        "promessa contra o histórico real: walk-forward sobre os "
        "`liquidation_events` do OKX no live DB (a série exacta em que o "
        "monitor bate), com a mesma regra (`cadence_percentile`, a função "
        "partilhada com produção). Um gap que atinge o threshold de 6h é a "
        "condição exacta que dispararia `FEED SILENT` — a degradação real.",
        "",
        f"- Série: {a['events']} eventos okx · {a['gaps']} gaps · "
        f"{_fmt_ts(a['first_event_ms'])} → {_fmt_ts(a['last_event_ms'])} (UTC)",
        f"- Detector: p99 com min {a['min_samples']} gaps · história "
        f"{a['gap_history']} gaps · threshold silêncio "
        f"{a['max_silence_sec'] / 3600:.1f}h",
        "",
        "| Métrica | Valor |",
        "|---|---|",
        f"| Gaps | {a['gaps']} |",
        f"| Fires do detector | {a['fires_total']} "
        f"({a['confirmed']} confirmados · {a['unconfirmed']} sem confirmação) |",
        f"| Degradações (gap ≥ 6h) | {a['degradations']} |",
        f"| Antecipadas | {a['anticipated']} |",
        f"| Misses | {a['misses']} |",
        f"| Lead médio (vs 6h) | {lead_txt} |",
        "",
    ]
    if n_deg:
        lines.append(
            f"**Total: {a['anticipated']}/{n_deg} degradações antecipadas "
            f"({pct:.0f}%) · {a['misses']} misses · lead médio {lead_txt} "
            f"antes do threshold de 6h.**"
        )
    else:
        lines.append("**Sem degradações (gap ≥ 6h) na janela — nada a validar.**")
    lines += ["", "## Detalhe por degradação", "",
              "| Gap | Início | Fim | Duração | p99 base | Lead vs 6h |",
              "|---|---|---|---|---|---|"]
    for d in a["degradation_details"]:
        if d["fired"] and d["p99_sec"] is not None:
            lead_txt = _fmt_dur(a["max_silence_sec"] - d["p99_sec"])
        else:
            lead_txt = "MISS"
        lines.append(
            f"| {d['gap_index']} | {_fmt_ts(d['start_ms'])} | "
            f"{_fmt_ts(d['end_ms'])} | {_fmt_dur(d['gap_sec'])} | "
            f"{_fmt_dur(d['p99_sec'])} | {lead_txt} |"
        )
    cc = report.get("cross_check") or {}
    lines += [
        "",
        "## Cruzamento com os alertas reais",
        "",
        f"| Alerta real (`feed_silence_alerts`) | Valor |",
        "|---|---|",
        f"| Cadence emitidos pelo monitor | {cc.get('real_alerts', 0)} |",
        f"| Casados com fires simulados (±{cc.get('tolerance_sec', 900):.0f}s) | "
        f"{cc.get('matched_real', 0)} |",
        f"| Janela dos alertas reais | "
        f"{_fmt_ts(cc.get('real_window_start_ms'))} → "
        f"{_fmt_ts(cc.get('real_window_end_ms'))} |",
        "",
        "_Gerado por `scripts/validate_feed_cadence_leadtime.py` — read-only, "
        "nunca trade._",
    ]
    return "\n".join(lines)


def write_report(
    report: Dict[str, Any], *, path: Optional[Path] = None
) -> Optional[Path]:
    report_path = path or REPORT_PATH
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(report), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001
        print(f"write_report failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the cadence detector against real okx history "
                    "(lead time vs the 6h silence threshold)."
    )
    parser.add_argument("--live-db", type=Path, default=None)
    parser.add_argument("--research-db", type=Path, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--gap-history", type=int, default=None)
    parser.add_argument("--max-silence-sec", type=float, default=None)
    parser.add_argument("--limit", type=int, default=500_000)
    parser.add_argument("--now-ms", type=int, default=None,
                        help="Override 'now' for reproducible runs")
    args = parser.parse_args()

    cfg = _load_config()
    live_db = args.live_db or cfg.get("database.path", str(DEFAULT_LIVE_DB))
    live_db = Path(live_db)
    research_db = Path(
        args.research_db or str(Path("data") / "research" / "hyperliquid.db")
    )

    min_samples = (
        args.min_samples if args.min_samples is not None
        else feed_silence_cadence_min_samples()
    )
    gap_history = (
        args.gap_history if args.gap_history is not None
        else feed_silence_cadence_gap_history()
    )
    max_silence = (
        args.max_silence_sec if args.max_silence_sec is not None
        else resolve_max_silence()
    )

    ts = load_okx_timestamps(live_db, limit=args.limit)
    report = simulate_cadence_fires(
        ts,
        min_samples=min_samples,
        gap_history=gap_history,
        max_silence_sec=max_silence,
        now_ms=args.now_ms,
    )
    real = load_real_cadence_alerts(research_db)
    report["cross_check"] = cross_check_with_real(report, real)

    md_path = write_report(report)
    try:
        JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSON_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"json write failed: {exc}", file=sys.stderr)

    print(
        f"Cadence lead-time validation "
        f"({datetime.now(timezone.utc).isoformat(timespec='seconds')})"
    )
    print(f"feed={FEED}  events={report['events']}  gaps={report['gaps']}  "
          f"min_samples={min_samples}  gap_history={gap_history}  "
          f"threshold={max_silence / 3600:.1f}h")
    print(f"{'fires':>7}{'conf':>6}{'unconf':>8}{'degr':>6}{'antic':>7}"
          f"{'miss':>6}{'avgLead':>10}")
    avg = report["avg_lead_sec"]
    print(f"{report['fires_total']:>7}{report['confirmed']:>6}"
          f"{report['unconfirmed']:>8}{report['degradations']:>6}"
          f"{report['anticipated']:>7}{report['misses']:>6}"
          f"{_fmt_dur(avg) if avg is not None else '-':>10}")
    cc = report["cross_check"]
    print(f"real alerts: {cc['real_alerts']}  matched: "
          f"{cc['matched_real']}/{cc['matched_sim']}")
    if md_path:
        print(f"report -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
