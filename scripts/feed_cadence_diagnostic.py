"""Feed cadence diagnostic — recent gaps vs historical p95/p99 per feed.

For each feed contracted by THIS deployment, report:

  * historical inter-event gap p95 / p99 (the cadence the feed normally
    keeps, from persisted event timestamps);
  * recent gap stats (median / p95 / p99 / latest gap) over the last N
    hours;
  * a linear trend of the recent gaps (seconds per gap, least squares) —
    a positive slope means the gaps are widening over time;
  * a verdict: OK / WATCH / DEGRADING.

``DEGRADING`` = recent median above the historical p99 (the feed used to
deliver at this cadence; now it is consistently slower). ``WATCH`` = recent
median above historical p95, or the latest gap above the historical p99, or
a clearly positive recent trend. This is the offline companion to the live
``FEED CADENCE`` alert (which uses the in-process rolling p99).

Evidence sources (persisted artifacts, so it works with the bot stopped):

  * liquidation_okx / liquidation_bybit / liquidation_binance
      -> liquidation_events (timestamp_ms per source)
  * funding_hl / funding_cex
      -> funding_history (timestamp)
  * taker_split
      -> candles_1m (timestamp_ms where buy/sell volume > 0)
  * binance_perp
      -> binance_perp_prices (timestamp_ms)

Exit codes: 0 all feeds OK/skipped · 2 at least one feed WATCH · 1 at
least one feed DEGRADING.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.engine import (  # noqa: E402
    feed_silence_cadence_gap_history,
    feed_silence_cadence_min_samples,
    feed_silence_contracts,
    feed_silence_imminent_fraction,
    feed_silence_warn_fraction,
)
from src.data.market_data_health import cadence_percentile  # noqa: E402
from src.utils.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "live" / "bot.db"
REPORT_PATH = ROOT / "docs" / "FEED_CADENCE_REPORT.md"
HISTORY_PATH = ROOT / "data" / "research" / "feed_cadence_history.json"
# Per-feed caps: the history file is gitignored and bounded; the report shows
# the most recent rows so a feed's trend is readable without a huge table.
HISTORY_CAP_PER_FEED = 500
REPORT_HISTORY_ROWS = 30


def _fmt_dur(sec) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    return f"{sec / 3600:.1f}h"


def _event_timestamps(
    db: sqlite3.Connection,
    query: str,
    params: tuple = (),
) -> list[int]:
    rows = db.execute(query, params).fetchall()
    return [int(r[0]) for r in rows]


def _feed_timestamps(db: sqlite3.Connection) -> dict:
    """Sorted event timestamps per feed key (ascending). Absent = no data.

    Best-effort per table: a live DB missing a table (fresh deployment,
    research-only instance) degrades that feed to no data instead of
    blowing up the whole diagnostic."""
    queries = {
        "liquidation_okx": (
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE source='okx' ORDER BY timestamp_ms ASC"
        ),
        "liquidation_bybit": (
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE source='bybit' ORDER BY timestamp_ms ASC"
        ),
        "liquidation_binance": (
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE source='binance' ORDER BY timestamp_ms ASC"
        ),
        # funding_history holds one row per symbol per poll — the cadence of
        # the *table* is the funding_hl/cex delivery cadence (same rows feed
        # both).
        "funding_hl": (
            "SELECT timestamp FROM funding_history ORDER BY timestamp ASC"
        ),
        "funding_cex": (
            "SELECT timestamp FROM funding_history ORDER BY timestamp ASC"
        ),
        "taker_split": (
            "SELECT timestamp_ms FROM candles_1m "
            "WHERE (buy_volume > 0 OR sell_volume > 0) ORDER BY timestamp_ms ASC"
        ),
        "binance_perp": (
            "SELECT timestamp_ms FROM binance_perp_prices "
            "ORDER BY timestamp_ms ASC"
        ),
    }
    out: dict = {}
    for feed, query in queries.items():
        try:
            out[feed] = _event_timestamps(db, query)
        except sqlite3.OperationalError:
            out[feed] = []  # table missing — feed has no persisted events
    return out


def inter_event_gaps(ts: list[int], max_gap_sec: float = 12 * 3600.0) -> list[tuple[int, float]]:
    """(gap_end_ms, gap_sec) for consecutive events; sane gaps only.

    ``max_gap_sec`` caps the retained gap so a single weekend outage does not
    inflate the p95/p99 baseline of an otherwise healthy feed.
    """
    gaps: list[tuple[int, float]] = []
    for a, b in zip(ts, ts[1:]):
        gap_sec = (b - a) / 1000.0
        if 0 < gap_sec <= max_gap_sec:
            gaps.append((b, gap_sec))
    return gaps


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(p * len(ordered)))
    return ordered[idx]


def least_squares_slope(xs: list[float], ys: list[float]) -> float:
    """Slope of the least-squares fit of ys vs xs (sec of gap per gap)."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def analyze_feed(
    name: str,
    ts: list[int],
    *,
    now_ms: int,
    recent_ms: int,
    min_history: int = 50,
) -> dict:
    """Report per-feed cadence: history p95/p99 vs recent stats + trend."""
    if len(ts) < 2:
        return {"status": "no_data", "events": len(ts)}
    gaps = inter_event_gaps(ts)
    if len(gaps) < min_history:
        return {"status": "insufficient", "events": len(ts), "gaps": len(gaps)}

    cutoff = now_ms - recent_ms
    history = [g for end_ms, g in gaps if end_ms < cutoff]
    recent = [g for end_ms, g in gaps if end_ms >= cutoff]
    if not recent:
        recent = [g for _, g in gaps[-min_history:]]  # tail of the series
    if not history:
        history = [g for _, g in gaps[:-len(recent)]] or [g for _, g in gaps]

    h_p95 = percentile(history, 0.95)
    h_p99 = percentile(history, 0.99)
    r_med = percentile(recent, 0.5)
    r_p95 = percentile(recent, 0.95)
    r_p99 = percentile(recent, 0.99)
    latest_gap = recent[-1] if recent else None

    # Trend: slope of recent gaps (index as x). Positive = widening.
    slope = least_squares_slope(
        [float(i) for i in range(len(recent))], recent
    ) if len(recent) >= 5 else 0.0

    if r_med > h_p99:
        status = "DEGRADING"
    elif r_med > h_p95 or (latest_gap is not None and latest_gap > h_p99):
        status = "WATCH"
    elif slope > 0.15 * h_p95 and r_med > h_p95:
        status = "WATCH"
    else:
        status = "OK"

    return {
        "status": status,
        "events": len(ts),
        "gaps": len(gaps),
        "history_gaps": len(history),
        "recent_gaps": len(recent),
        "hist_p95_sec": round(h_p95, 1),
        "hist_p99_sec": round(h_p99, 1),
        "recent_median_sec": round(r_med, 1),
        "recent_p95_sec": round(r_p95, 1),
        "recent_p99_sec": round(r_p99, 1),
        "latest_gap_sec": None if latest_gap is None else round(latest_gap, 1),
        "trend_sec_per_gap": round(slope, 3),
    }


def live_snapshot_equivalent(
    ts: list[int],
    *,
    now_ms: int,
    max_silence_sec: float,
    warn_fraction: float,
    imminent_fraction: float,
    min_samples: int,
    gap_history: int,
) -> dict | None:
    """Reconstruct what the live ``FeedSilenceMonitor.snapshot()`` would
    report for this feed, from the same persisted events.

    The monitor's cadence fields are pure functions of the event series
    (percentiles of the rolling gap deque via the shared
    ``cadence_percentile``, plus the rank of the current age), so an offline
    script reproduces them exactly. ``warn_level`` is rebuilt from the
    current age vs the fractions — the fire-once flags mirror the age
    crossing after the first check. ``None`` when the feed has no events.
    """
    if len(ts) < 2:
        return None
    raw_gaps: list[float] = []
    for a, b in zip(ts, ts[1:]):
        g = (b - a) / 1000.0
        if g >= 0:  # the monitor records only gaps >= 0
            raw_gaps.append(g)
    if not raw_gaps:
        return None
    recent = raw_gaps[-gap_history:]  # same deque cap as the monitor
    p50 = cadence_percentile(recent, 0.50, min_samples)
    p95 = cadence_percentile(recent, 0.95, min_samples)
    p99 = cadence_percentile(recent, 0.99, min_samples)
    age_sec = max(0.0, (now_ms - ts[-1]) / 1000.0)
    pct_current: float | None = None
    if recent:
        below = sum(1 for g in recent if g <= age_sec)
        pct_current = round(100.0 * below / len(recent), 1)
    if age_sec >= max_silence_sec:
        warn_level = "degraded"
    elif age_sec >= max_silence_sec * imminent_fraction:
        warn_level = "imminent"
    elif age_sec >= max_silence_sec * warn_fraction:
        warn_level = "early"
    else:
        warn_level = "none"
    return {
        "age_sec": round(age_sec, 1),
        "max_silence_sec": max_silence_sec,
        "warn_level": warn_level,
        "cadence_p50_sec": None if p50 is None else round(p50, 1),
        "cadence_p95_sec": None if p95 is None else round(p95, 1),
        "cadence_p99_sec": None if p99 is None else round(p99, 1),
        "cadence_pct_current": pct_current,
        "cadence_samples": len(recent),
    }


def cross_verdict(status: str, live: dict | None) -> str:
    """Agreement between the offline diagnostic and the reconstructed live
    snapshot:

    * ``aligned_ok`` — both see the feed healthy;
    * ``aligned_trouble`` — DEGRADING and live already imminent/degraded;
    * ``offline_ahead`` — DEGRADING while live is still none/early (the
      recent-vs-history trend is visible offline before the live flags);
    * ``live_ahead`` — OK/WATCH while live degraded (a silence the recent
      window missed);
    * ``live_escalating`` — OK/WATCH while live is early/imminent;
    * ``no_live_data`` / ``no_diagnosis`` — missing side.
    """
    if live is None:
        return "no_live_data"
    level = live.get("warn_level") or "none"
    if status == "DEGRADING":
        return "aligned_trouble" if level in ("degraded", "imminent") \
            else "offline_ahead"
    if status in ("OK", "WATCH"):
        if level == "degraded":
            return "live_ahead"
        if level in ("imminent", "early"):
            return "live_escalating"
        return "aligned_ok"
    return "no_diagnosis"


def _feed_history_record(report: dict) -> list[dict]:
    """One history row per feed for the current run (status + trend + cross)."""
    ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    now = report.get("now_ms")
    rows: list[dict] = []
    for feed, st in sorted((report.get("feeds") or {}).items()):
        live = st.get("live_snapshot") or {}
        rows.append({
            "ts": ts_iso,
            "now_ms": now,
            "feed": feed,
            "status": st.get("status"),
            "recent_median_sec": st.get("recent_median_sec"),
            "recent_p99_sec": st.get("recent_p99_sec"),
            "hist_p95_sec": st.get("hist_p95_sec"),
            "hist_p99_sec": st.get("hist_p99_sec"),
            "latest_gap_sec": st.get("latest_gap_sec"),
            "trend_sec_per_gap": st.get("trend_sec_per_gap"),
            "cross": st.get("cross"),
            "live_warn_level": live.get("warn_level"),
        })
    return rows


def load_cadence_history(path: Optional[Path] = None) -> list[dict]:
    """The gitignored accumulated history (append-only rows per run)."""
    hp = path or HISTORY_PATH
    if hp.exists():
        try:
            data = json.loads(hp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def record_and_save_history(
    report: dict, *, path: Optional[Path] = None
) -> list[dict]:
    """Append the current run's per-feed rows (capped per feed) and persist.

    Best-effort: a failing history write never breaks the diagnostic.
    """
    hp = path or HISTORY_PATH
    history = load_cadence_history(path=hp)
    history.extend(_feed_history_record(report))
    by_feed: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in history:
        f = str(row.get("feed") or "?")
        if f not in by_feed:
            by_feed[f] = []
            order.append(f)
        by_feed[f].append(row)
    capped: list[dict] = []
    for f in order:
        capped.extend(by_feed[f][-HISTORY_CAP_PER_FEED:])
    try:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps(capped, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"history write failed: {exc}", file=sys.stderr)
    return capped


def render_markdown_report(report: dict, history: list[dict]) -> str:
    """Markdown for docs/FEED_CADENCE_REPORT.md: current state + the
    accumulated per-feed trend history (recent runs, newest last)."""
    lines = [
        "# Feed Cadence Report",
        "",
        f"Gerado a {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"(UTC) · janela recente {report.get('recent_hours', 48):.0f}h · "
        "apenas feeds contratados neste deployment",
        "",
        "## Estado actual",
        "",
        "| Feed | Status | hist p95 | hist p99 | rec med | rec p99 | latest | "
        "trend | cross |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for feed, st in sorted((report.get("feeds") or {}).items()):
        live = st.get("live_snapshot") or {}
        level = live.get("warn_level")
        cross = st.get("cross") or "-"
        if level not in (None, "none"):
            cross = f"{cross} ({level})"
        if st["status"] in ("no_data", "insufficient"):
            lines.append(
                f"| `{feed}` | {st['status']} | — | — | — | — | — | — | {cross} |"
            )
            continue
        lines.append(
            f"| `{feed}` | {st['status']} | "
            f"{_fmt_dur(st.get('hist_p95_sec'))} | {_fmt_dur(st.get('hist_p99_sec'))} | "
            f"{_fmt_dur(st.get('recent_median_sec'))} | {_fmt_dur(st.get('recent_p99_sec'))} | "
            f"{_fmt_dur(st.get('latest_gap_sec'))} | "
            f"{st.get('trend_sec_per_gap'):+.2f} | {cross} |"
        )
    lines += ["", "## Histórico de tendências por feed", ""]
    feeds = sorted({str(r.get("feed") or "?") for r in history})
    if not feeds:
        lines.append("_Sem histórico acumulado ainda._")
    for feed in feeds:
        rows = [r for r in history if str(r.get("feed")) == feed]
        rows = rows[-REPORT_HISTORY_ROWS:]
        lines += [
            f"### `{feed}`",
            "",
            "| Run (UTC) | Status | rec med | hist p99 | trend | cross |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            ts_txt = str(r.get("ts") or "")[:16].replace("T", " ")
            lines.append(
                f"| {ts_txt} | {r.get('status')} | "
                f"{_fmt_dur(r.get('recent_median_sec'))} | "
                f"{_fmt_dur(r.get('hist_p99_sec'))} | "
                f"{r.get('trend_sec_per_gap') or 0:+.2f} | {r.get('cross')} |"
            )
        lines.append("")
    lines.append(
        "_Gerado por `scripts/feed_cadence_diagnostic.py` — read-only, nunca "
        "trade._"
    )
    return "\n".join(lines)


def write_cadence_report(
    report: dict,
    *,
    path: Optional[Path] = None,
    history_path: Optional[Path] = None,
) -> Optional[Path]:
    """Record the run in the accumulated history and render the markdown
    report (docs/FEED_CADENCE_REPORT.md). Best-effort — a write failure
    never breaks the diagnostic or the supervisor."""
    try:
        history = record_and_save_history(report, path=history_path)
        md = render_markdown_report(report, history)
        rp = path or REPORT_PATH
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(md, encoding="utf-8")
        return rp
    except Exception as exc:  # noqa: BLE001
        print(f"write_cadence_report failed: {exc}", file=sys.stderr)
        return None


def run_cadence_diagnostic(
    db_path: Path,
    contracts: dict,
    *,
    now_ms: int | None = None,
    recent_hours: float = 48.0,
    min_history: int = 50,
    warn_fraction: float | None = None,
    imminent_fraction: float | None = None,
    min_samples: int | None = None,
    gap_history: int | None = None,
) -> dict:
    """Per-feed cadence verdict for the contracted feeds, from the live DB.

    Pure read — the reusable core behind the CLI and the research watchdog
    (``scripts/research_watchdog_supervisor.py`` / dashboard): one function
    computes the status every gate reads, so the dashboard and the watchdog
    can never disagree on a feed's verdict. Each feed also carries a
    ``live_snapshot`` (the reconstructed monitor state: warn_level + cadence
    percentiles) and a ``cross`` verdict, so the JSON report lets the
    operator compare the offline diagnosis with the live monitor.
    """
    if warn_fraction is None:
        warn_fraction = feed_silence_warn_fraction()
    if imminent_fraction is None:
        imminent_fraction = feed_silence_imminent_fraction()
    if min_samples is None:
        min_samples = feed_silence_cadence_min_samples()
    if gap_history is None:
        gap_history = feed_silence_cadence_gap_history()
    if not Path(db_path).exists():
        return {"now_ms": now_ms, "recent_hours": recent_hours, "feeds": {}}
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    recent_ms = int(recent_hours * 3600_000)
    timestamps = _feed_timestamps(db)
    db.close()
    report: dict = {
        "now_ms": now,
        "recent_hours": recent_hours,
        "live_thresholds": {
            "warn_fraction": warn_fraction,
            "imminent_fraction": imminent_fraction,
            "cadence_min_samples": min_samples,
            "cadence_gap_history": gap_history,
        },
        "feeds": {},
    }
    for feed in sorted(contracts):
        st = analyze_feed(
            feed, timestamps.get(feed, []),
            now_ms=now, recent_ms=recent_ms,
            min_history=min_history,
        )
        max_sil = contracts.get(feed) or 0.0
        if max_sil > 0:
            live = live_snapshot_equivalent(
                timestamps.get(feed, []),
                now_ms=now,
                max_silence_sec=float(max_sil),
                warn_fraction=warn_fraction,
                imminent_fraction=imminent_fraction,
                min_samples=min_samples,
                gap_history=gap_history,
            )
            st["live_snapshot"] = live
            st["cross"] = cross_verdict(st["status"], live)
        report["feeds"][feed] = st
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="bot.db path (default: data/live/bot.db)")
    parser.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"),
                        help="settings.yaml path")
    parser.add_argument("--recent-hours", type=float, default=48.0,
                        help="recent window for the cadence comparison (default 48h)")
    parser.add_argument("--min-history", type=int, default=50,
                        help="minimum recorded gaps to report p95/p99 (default 50)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    parser.add_argument("--report", type=Path, default=None,
                        help="markdown report path (default: docs/FEED_CADENCE_REPORT.md)")
    parser.add_argument("--history", type=Path, default=None,
                        help="accumulated history JSON path (default: "
                             "data/research/feed_cadence_history.json)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    contracts = feed_silence_contracts(cfg)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: bot DB not found: {db_path}", file=sys.stderr)
        return 1
    now_ms = int(time.time() * 1000)
    report = run_cadence_diagnostic(
        db_path, contracts,
        now_ms=now_ms, recent_hours=args.recent_hours,
        min_history=args.min_history,
    )
    codes = {"OK": 0, "WATCH": 2, "DEGRADING": 1, "insufficient": 0, "no_data": 0}
    worst = 0
    for st in report["feeds"].values():
        worst = max(worst, codes.get(st["status"], 0))

    md_path = write_cadence_report(
        report,
        path=args.report,
        history_path=args.history,
    )

    if args.json:
        print(json.dumps(report, indent=2))
        if md_path:
            print(f"report -> {md_path}", file=sys.stderr)
        return worst

    print(f"Feed cadence diagnostic — recent window {args.recent_hours:.0f}h "
          f"(now {time.strftime('%Y-%m-%d %H:%M', time.localtime(now_ms / 1000))} UTC)")
    print(f"{'feed':30s} {'status':10s} {'hist p95':>8s} {'hist p99':>8s} "
          f"{'rec med':>8s} {'rec p99':>8s} {'latest':>8s} {'trend':>8s} "
          f"{'cross':>14s}")
    print("-" * 112)
    for feed, st in report["feeds"].items():
        if st["status"] in ("no_data", "insufficient"):
            print(f"{feed:30s} {st['status']:10s} ({st.get('events', 0)} events, "
                  f"{st.get('gaps', 0)} gaps)")
            continue
        live = st.get("live_snapshot")
        cross = st.get("cross") or "-"
        if live and live.get("warn_level") not in (None, "none"):
            cross = f"{cross}({live['warn_level']})"
        print(
            f"{feed:30s} {st['status']:10s} "
            f"{_fmt_dur(st['hist_p95_sec']):>8s} {_fmt_dur(st['hist_p99_sec']):>8s} "
            f"{_fmt_dur(st['recent_median_sec']):>8s} {_fmt_dur(st['recent_p99_sec']):>8s} "
            f"{_fmt_dur(st['latest_gap_sec']):>8s} {st['trend_sec_per_gap']:>+8.2f}s "
            f"{cross:>14s}"
        )
    if worst >= 1:
        print("\n[DEGRADING] feed(s) consistently slower than their historical "
              "p99 — check delivery path.", file=sys.stderr)
    elif worst == 2:
        print("\n[WATCH] feed(s) above historical p95 or with a rising gap "
              "trend — delivery thinning?", file=sys.stderr)
    else:
        print("\n[PASS] all contracted feeds keep their historical cadence.")
    if md_path:
        print(f"report -> {md_path}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
