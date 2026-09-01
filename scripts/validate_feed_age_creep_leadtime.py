#!/usr/bin/env python3
"""Validate the feed age creep detector — does it anticipate real silences?

Ground truth for "real degradation": the monitor's ``FEED SILENT`` fires when
a feed's age reaches ``max_silence_sec``. Those alerts are not persisted, so
the daily rollup is the closest recorded proxy: a day whose ``max_age_sec >=
max_silence_sec`` is a **degraded day** — the age actually crossed the
threshold that trips the alert that day.

Method (walk-forward, per contracted feed, on the production rule):

  * **Fires** — walk the daily rows day by day; apply the exact staircase
    rule the supervisor uses (``staircase_verdict`` from
    ``scripts/feed_age_creep_recheck.py``) on the rows up to each day. The
    first day the rule fires opens a creeping episode; a drop (recovery)
    closes it. Each opening day is a detector "fire".
  * **Degradation episodes** — runs of consecutive degraded days. The
    episode start is the first degraded day.
  * **Lead time** — for each episode, the number of days between the
    latest fire that is still active the day before the episode start and
    the episode start. ``>= 1`` = anticipated; ``0`` = same-day (the fire
    and the degradation happened on the same UTC day); no active fire =
    **MISS**. Fires with no degradation later in the window are reported as
    "anticipation without confirmation".

Outputs: a console table, ``docs/FEED_AGE_CREEP_LEADTIME_VALIDATION.md``
and ``data/research/feed_age_creep_leadtime.json``. Read-only — never
trades, never touches the OMS.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.feed_age_creep_recheck import (  # noqa: E402
    CREEP_MIN_DAYS,
    CREEP_MIN_GROWTH_FRAC,
    CREEP_MIN_LEVEL_FRAC,
    CREEP_MIN_SAMPLES_PER_DAY,
    load_daily_history,
    resolve_contracts,
    staircase_verdict,
)

DAY_MS = 86_400_000
REPORT_PATH = ROOT / "docs" / "FEED_AGE_CREEP_LEADTIME_VALIDATION.md"
JSON_PATH = ROOT / "data" / "research" / "feed_age_creep_leadtime.json"


def degraded_days(
    rows: Sequence[Tuple[int, float, int]],
    max_silence: float,
) -> List[int]:
    """Days whose max age reached the silence threshold (FEED SILENT day)."""
    return [day for day, age, _ in rows if age >= max_silence]


def degradation_episodes(
    rows: Sequence[Tuple[int, float, int]],
    max_silence: float,
) -> List[Tuple[int, int]]:
    """Runs of consecutive degraded days -> [(start_day, end_day)]."""
    episodes: List[Tuple[int, int]] = []
    current: List[int] = []
    for day in degraded_days(rows, max_silence):
        if current and day - current[-1] == DAY_MS:
            current.append(day)
        else:
            if current:
                episodes.append((current[0], current[-1]))
            current = [day]
    if current:
        episodes.append((current[0], current[-1]))
    return episodes


def walk_forward_fires(
    rows: Sequence[Tuple[int, float, int]],
    max_silence: float,
    *,
    min_days: int = CREEP_MIN_DAYS,
    min_growth_frac: float = CREEP_MIN_GROWTH_FRAC,
    min_level_frac: float = CREEP_MIN_LEVEL_FRAC,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Days when the staircase rule (re)opened a creeping episode.

    Walks the ascending daily rows; the rule is applied on the prefix up to
    each day, exactly as the production detector sees it. A fire is recorded
    only on the *transition* quiet -> creeping (fire-once per episode, the
    supervisor's contract); a drop closes the episode.
    """
    fires: List[Tuple[int, Dict[str, Any]]] = []
    creeping = False
    for i in range(len(rows)):
        verdict = staircase_verdict(
            list(rows[: i + 1]),
            max_silence,
            min_days=min_days,
            min_growth_frac=min_growth_frac,
            min_level_frac=min_level_frac,
        )
        if verdict is not None and not creeping:
            creeping = True
            fires.append((rows[i][0], verdict))
        elif verdict is None and creeping:
            creeping = False
    return fires


def analyse_feed(
    rows: Sequence[Tuple[int, float, int]],
    max_silence: float,
    *,
    min_days: int = CREEP_MIN_DAYS,
    min_growth_frac: float = CREEP_MIN_GROWTH_FRAC,
    min_level_frac: float = CREEP_MIN_LEVEL_FRAC,
) -> Dict[str, Any]:
    """Per-feed lead-time analysis (see module docstring for semantics)."""
    rows = list(rows)
    episodes = degradation_episodes(rows, max_silence)
    fires = walk_forward_fires(
        rows, max_silence,
        min_days=min_days,
        min_growth_frac=min_growth_frac,
        min_level_frac=min_level_frac,
    )
    fire_days = {fd for fd, _ in fires}
    episodes_out: List[Dict[str, Any]] = []
    anticipated = same_day = misses = 0
    lead_times: List[int] = []
    for start, end in episodes:
        prefix_before = [r for r in rows if r[0] < start]
        active = staircase_verdict(
            prefix_before,
            max_silence,
            min_days=min_days,
            min_growth_frac=min_growth_frac,
            min_level_frac=min_level_frac,
        )
        if active is not None and prefix_before:
            prior = [fd for fd, _ in fires if fd < start]
            fire_day = max(prior)
            lead = (start - fire_day) // DAY_MS
            lead_times.append(lead)
            if lead >= 1:
                anticipated += 1
                bucket = "anticipated"
            else:
                same_day += 1
                bucket = "same-day"
            episodes_out.append({
                "start_day_ms": start,
                "end_day_ms": end,
                "fire_day_ms": fire_day,
                "lead_days": int(lead),
                "bucket": bucket,
            })
        elif start in fire_days:
            same_day += 1
            lead_times.append(0)
            episodes_out.append({
                "start_day_ms": start,
                "end_day_ms": end,
                "fire_day_ms": start,
                "lead_days": 0,
                "bucket": "same-day",
            })
        else:
            misses += 1
            episodes_out.append({
                "start_day_ms": start,
                "end_day_ms": end,
                "fire_day_ms": None,
                "lead_days": None,
                "bucket": "miss",
            })
    # fires whose episode never led to a degradation episode start
    used = {e["fire_day_ms"] for e in episodes_out if e["fire_day_ms"] is not None}
    unconfirmed = [
        {"fire_day_ms": fd, **{k: v for k, v in v.items() if k != "creeping"}}
        for fd, v in fires if fd not in used
    ]
    return {
        "feed": "",
        "rows": len(rows),
        "degraded_days": len(degraded_days(rows, max_silence)),
        "episodes": len(episodes),
        "anticipated": anticipated,
        "same_day": same_day,
        "misses": misses,
        "unconfirmed_fires": len(unconfirmed),
        "lead_days": lead_times,
        "avg_lead_days": (round(sum(lead_times) / len(lead_times), 2)
                          if lead_times else None),
        "max_lead_days": max(lead_times) if lead_times else None,
        "episode_details": episodes_out,
        "unconfirmed": unconfirmed,
    }


def analyse_all(
    contracts: Dict[str, float],
    *,
    db: Optional[Any] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the lead-time analysis over every contracted feed."""
    from src.data.research_database import ResearchDatabase

    rdb = db or ResearchDatabase.open()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    # full available window: from the earliest daily row to now
    start = now - 400 * DAY_MS  # generous cap; rows outside simply won't exist
    per_feed: Dict[str, Dict[str, Any]] = {}
    for feed, max_silence in sorted(contracts.items()):
        if not max_silence or max_silence <= 0:
            continue
        rows = load_daily_history(
            rdb, feed, start, now,
            min_samples_per_day=CREEP_MIN_SAMPLES_PER_DAY,
        )
        if not rows:
            per_feed[feed] = {"rows": 0, "degraded_days": 0, "episodes": 0,
                              "anticipated": 0, "same_day": 0, "misses": 0,
                              "unconfirmed_fires": 0, "lead_days": [],
                              "avg_lead_days": None, "max_lead_days": None,
                              "episode_details": [], "unconfirmed": []}
            continue
        analysis = analyse_feed(rows, max_silence)
        analysis["feed"] = feed
        per_feed[feed] = analysis
    return {
        "generated_ms": int(now),
        "min_days": CREEP_MIN_DAYS,
        "min_growth_frac": CREEP_MIN_GROWTH_FRAC,
        "min_level_frac": CREEP_MIN_LEVEL_FRAC,
        "feeds": per_feed,
    }


def _fmt_day(day_ms: Optional[int]) -> str:
    if day_ms is None:
        return "—"
    return datetime.fromtimestamp(day_ms / 1000, tz=timezone.utc).strftime("%m-%d")


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# Feed Age Creep — lead-time validation",
        "",
        "O detector antecipa os silêncios reais? Para cada feed contratado, "
        "o script cruza os daily max ages (`feed_age_history`) com os dias de "
        "degradação (max age ≥ threshold — a condição que dispara `FEED "
        "SILENT`) e mede quantos dias antes o detector (regra `staircase` de "
        "produção) disparou.",
        "",
        f"- Regra: {report['min_days']}d não-decrescentes, crescimento ≥ "
        f"{report['min_growth_frac'] * 100:.0f}% do threshold, último dia ≥ "
        f"{report['min_level_frac'] * 100:.0f}% do threshold",
        "",
        "| Feed | Dias | Degradados | Episódios | Antecipados | Same-day | "
        "Misses | Lead médio (d) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    totals = {"anticipated": 0, "same_day": 0, "misses": 0,
              "episodes": 0, "leads": []}
    for feed, a in sorted(report["feeds"].items()):
        totals["anticipated"] += a["anticipated"]
        totals["same_day"] += a["same_day"]
        totals["misses"] += a["misses"]
        totals["episodes"] += a["episodes"]
        totals["leads"].extend(a["lead_days"])
        lines.append(
            f"| `{feed}` | {a['rows']} | {a['degraded_days']} | "
            f"{a['episodes']} | {a['anticipated']} | {a['same_day']} | "
            f"{a['misses']} | "
            f"{a['avg_lead_days'] if a['avg_lead_days'] is not None else '—'} |"
        )
    lines.append("")
    n_ep = totals["episodes"]
    if n_ep:
        pct = 100.0 * totals["anticipated"] / n_ep
        avg = (sum(totals["leads"]) / len(totals["leads"])
               if totals["leads"] else 0.0)
        lines += [
            f"**Total: {totals['anticipated']}/{n_ep} episódios antecipados "
            f"({pct:.0f}%) · {totals['same_day']} same-day · "
            f"{totals['misses']} misses · lead médio {avg:.1f}d.**",
            "",
        ]
    else:
        lines += ["**Sem episódios de degradação na janela — nada a validar.**", ""]
    lines += ["## Detalhe por episódio", "", "| Feed | Início | Fim | Fire | Lead |",
              "|---|---|---|---|---|"]
    for feed, a in sorted(report["feeds"].items()):
        for e in a["episode_details"]:
            lines.append(
                f"| `{feed}` | {_fmt_day(e['start_day_ms'])} | "
                f"{_fmt_day(e['end_day_ms'])} | {_fmt_day(e['fire_day_ms'])} | "
                f"{e['lead_days'] if e['lead_days'] is not None else 'MISS'} |"
            )
    lines += ["", "_Gerado por `scripts/validate_feed_age_creep_leadtime.py` — "
              "read-only, nunca trade._"]
    return "\n".join(lines)


def write_report(report: Dict[str, Any],
                 *, path: Optional[Path] = None) -> Optional[Path]:
    report_path = path or REPORT_PATH
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(report), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001
        print(f"write_report failed: {exc}", file=sys.stderr)
        return None


def main() -> int:
    contracts = resolve_contracts()
    report = analyse_all(contracts)
    md_path = write_report(report)
    try:
        JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSON_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"json write failed: {exc}", file=sys.stderr)

    print(f"Feed age creep lead-time validation "
          f"({datetime.now(timezone.utc).isoformat(timespec='seconds')})")
    print(f"{'feed':<22}{'rows':>5}{'deg':>5}{'ep':>4}{'ant':>5}"
          f"{'same':>6}{'miss':>6}{'avgLead':>9}")
    for feed, a in sorted(report["feeds"].items()):
        avg = a["avg_lead_days"]
        print(f"{feed:<22}{a['rows']:>5}{a['degraded_days']:>5}{a['episodes']:>4}"
              f"{a['anticipated']:>5}{a['same_day']:>6}{a['misses']:>6}"
              f"{avg if avg is not None else '-':>9}")
    if md_path:
        print(f"report -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
