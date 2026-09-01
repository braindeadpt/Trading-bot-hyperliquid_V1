#!/usr/bin/env python3
"""Feed age creep detector — consistent daily max-age growth per contracted feed.

The ``FeedAgeRecorder`` (``src/data/feed_age_history.py``) persists one row
per UTC day per feed: the maximum age (seconds since last event) observed
that day. A healthy feed shows a *flat* daily max around its normal quiet
period. A feed whose delivery is slowly degrading shows a *staircase*: each
day's max is a little higher than the day before, even if it recovers before
the silence threshold — exactly the signal "age growing between resets".

This module detects that staircase. It is the metric home for the
``feed_age_creep`` watchdog (registered in
``scripts/research_watchdog_supervisor.py``), following the same
single-source-of-truth pattern as the bias / flush / IV gate rechecks:

* ``detect_creeping_age()`` — pure: reads the research DB daily rollup for
  the contracted feeds and flags feeds whose last N days are a meaningful
  non-decreasing staircase.
* ``resolve_contracts()`` — the deployment's contracted feeds (max_silence
  per feed), reusing the engine's opt-in logic unchanged.

Read-only: never trades, never touches the OMS. No DB is created here.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── detection parameters ─────────────────────────────────────────────
CREEP_LOOKBACK_DAYS = 14        # how many days of history to read
CREEP_MIN_DAYS = 5              # consecutive daily rows required
CREEP_MIN_GROWTH_FRAC = 0.15    # total growth must be >= 15% of max_silence
CREEP_MIN_LEVEL_FRAC = 0.25     # last day max age must be >= 25% of max_silence
CREEP_MIN_SAMPLES_PER_DAY = 2   # rows with fewer samples are excluded

REPORT_PATH = ROOT / "docs" / "FEED_AGE_CREEP_RECHECK_RESULT.md"


def utc_day_start_ms(ts_ms: int) -> int:
    return int(ts_ms) - (int(ts_ms) % 86_400_000)


def load_daily_history(
    db: Any,
    feed: str,
    start_ms: int,
    end_ms: int,
    min_samples_per_day: int = CREEP_MIN_SAMPLES_PER_DAY,
) -> List[Tuple[int, float, int]]:
    """Daily (day_start_ms, max_age_sec, samples) rows ascending, excluding
    rows with too few samples (a barely-sampled partial day is noise)."""
    rows = db.load_feed_age_history(feed, start_ms, end_ms)
    return [
        (int(day), float(age), int(samples))
        for day, age, samples in rows
        if int(samples) >= min_samples_per_day
    ]


def staircase_verdict(
    rows: List[Tuple[int, float, int]],
    max_silence: float,
    *,
    min_days: int = CREEP_MIN_DAYS,
    min_growth_frac: float = CREEP_MIN_GROWTH_FRAC,
    min_level_frac: float = CREEP_MIN_LEVEL_FRAC,
) -> Optional[Dict[str, Any]]:
    """The staircase rule on ascending daily rows — pure, no DB.

    ``rows`` = (day_start_ms, max_age_sec, samples) ascending. A feed is
    creeping at this point in time when its last ``min_days`` rows are a
    non-decreasing staircase with meaningful growth on the feed's own
    ``max_silence`` scale (see ``detect_creeping_age`` for the rationale).

    Returns the verdict dict (same shape as ``detect_creeping_age`` entries)
    or ``None`` when not creeping. This is THE rule — shared by the
    supervisor detector and the lead-time validation script, so both always
    measure the exact production behaviour.
    """
    if len(rows) < min_days:
        return None
    tail = rows[-min_days:]
    ages = [float(age) for _, age, _ in tail]
    # 1. non-decreasing staircase (a drop breaks it and re-arms)
    if any(ages[i] < ages[i - 1] for i in range(1, len(ages))):
        return None
    growth = ages[-1] - ages[0]
    # 2. real growth on the feed's scale
    if growth < min_growth_frac * max_silence:
        return None
    # 3. meaningful quiet level
    if ages[-1] < min_level_frac * max_silence:
        return None
    return {
        "creeping": True,
        "days": len(ages),
        "first_max_age_sec": round(ages[0], 1),
        "last_max_age_sec": round(ages[-1], 1),
        "growth_sec": round(growth, 1),
        "growth_frac": round(growth / max_silence, 4),
        "last_day_start_ms": int(tail[-1][0]),
    }


def detect_creeping_age(
    contracts: Dict[str, float],
    *,
    db: Optional[Any] = None,
    now_ms: Optional[int] = None,
    lookback_days: int = CREEP_LOOKBACK_DAYS,
    min_days: int = CREEP_MIN_DAYS,
    min_growth_frac: float = CREEP_MIN_GROWTH_FRAC,
    min_level_frac: float = CREEP_MIN_LEVEL_FRAC,
    min_samples_per_day: int = CREEP_MIN_SAMPLES_PER_DAY,
) -> Dict[str, Dict[str, Any]]:
    """Flag contracted feeds whose daily max age is a consistent staircase.

    ``contracts`` maps feed name -> max_silence_sec (only the feeds the
    deployment actually contracted). A feed is ``creeping`` when, over the
    last ``min_days`` recorded days:

    1. every consecutive delta is >= 0 (non-decreasing — a drop breaks the
       staircase and re-arms the detector), AND
    2. the total growth is >= ``min_growth_frac`` of the feed's own
       max_silence (real movement on the feed's scale, not a wobble), AND
    3. the last day's max age is >= ``min_level_frac`` of max_silence (the
       feed must actually be quiet for a meaningful part of the day).

    Returns ``{feed: {creeping, days, first_max_age_sec, last_max_age_sec,
    growth_sec, growth_frac, last_day_start_ms}}`` for the flagged feeds.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not contracts:
        return out
    try:
        from src.data.research_database import ResearchDatabase

        rdb = db or ResearchDatabase.open()
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        start = now - lookback_days * 86_400_000
        for feed, max_silence in sorted(contracts.items()):
            if not max_silence or max_silence <= 0:
                continue
            rows = load_daily_history(
                rdb, feed, start, now,
                min_samples_per_day=min_samples_per_day,
            )
            verdict = staircase_verdict(
                rows,
                max_silence,
                min_days=min_days,
                min_growth_frac=min_growth_frac,
                min_level_frac=min_level_frac,
            )
            if verdict is not None:
                out[feed] = verdict
    except Exception as exc:  # noqa: BLE001 — a broken DB must never error
        import logging

        logging.getLogger("feed_age_creep").warning(
            "detect_creeping_age failed: %s", exc
        )
        return out
    return out


def resolve_contracts(config: Optional[Any] = None) -> Dict[str, float]:
    """Contracted feeds for this deployment (feed -> max_silence_sec).

    Reuses the engine's ``feed_silence_contracts`` unchanged — the opt-in
    logic (binance_perp only under LeadLag, liquidation_binance only with
    ``LIQUIDATION_BINANCE_CONTRACTED``) is the single source of truth, so
    the creep watchdog never flags a feed the deployment can't deliver.
    """
    from src.core.engine import feed_silence_contracts
    from src.utils.config import load_config

    return feed_silence_contracts(config if config is not None else load_config())


def write_report(
    detected: Dict[str, Dict[str, Any]],
    contracts: Dict[str, float],
    *,
    path: Optional[Path] = None,
) -> Optional[Path]:
    """Best-effort markdown report of the current creep state."""
    report_path = path or REPORT_PATH
    try:
        creeping = {f: d for f, d in detected.items() if d.get("creeping")}
        lines = [
            "# Feed Age Creep — recheck",
            "",
            "Detector do **max age diário por feed contratado** (escada "
            "não-decrescente sobre o rollup `feed_age_history`).",
            "",
            f"- Feeds com creep ativo: **{len(creeping)}**",
            f"- Janela: últimos {CREEP_LOOKBACK_DAYS}d · mínimo "
            f"{CREEP_MIN_DAYS}d consecutivos · crescimento ≥ "
            f"{CREEP_MIN_GROWTH_FRAC * 100:.0f}% do threshold",
            "",
            "| Feed | Dias | 1º max (s) | Último max (s) | Cresc. (s) | "
            "Cresc. (% thr) |",
            "|---|---|---|---|---|---|",
        ]
        for feed in sorted(creeping):
            d = creeping[feed]
            lines.append(
                f"| `{feed}` | {d['days']} | {d['first_max_age_sec']} | "
                f"{d['last_max_age_sec']} | {d['growth_sec']} | "
                f"{d['growth_frac'] * 100:.0f}% |"
            )
        lines.append("")
        if not creeping:
            lines.append("_Sem feeds com creep — todos os maxes diários estáveis._")
        lines.append("")
        lines.append("_Gerado por `scripts/feed_age_creep_recheck.py` — "
                     "read-only, nunca trade._")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("feed_age_creep").warning(
            "write_report failed: %s", exc
        )
        return None


if __name__ == "__main__":
    import json

    _contracts = resolve_contracts()
    _detected = detect_creeping_age(_contracts)
    _path = write_report(_detected, _contracts)
    print(json.dumps(
        {
            "contracts": sorted(_contracts),
            "creeping": {
                f: {k: v for k, v in d.items() if k != "creeping"}
                for f, d in sorted(_detected.items())
            },
            "report": str(_path) if _path else None,
        },
        indent=2,
    ))
