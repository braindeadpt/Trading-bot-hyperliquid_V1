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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.engine import feed_silence_contracts  # noqa: E402
from src.utils.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "live" / "bot.db"


def _event_timestamps(
    db: sqlite3.Connection,
    query: str,
    params: tuple = (),
) -> list[int]:
    rows = db.execute(query, params).fetchall()
    return [int(r[0]) for r in rows]


def _feed_timestamps(db: sqlite3.Connection) -> dict:
    """Sorted event timestamps per feed key (ascending). Absent = no data."""
    out: dict = {}
    out["liquidation_okx"] = _event_timestamps(
        db, "SELECT timestamp_ms FROM liquidation_events "
        "WHERE source='okx' ORDER BY timestamp_ms ASC"
    )
    out["liquidation_bybit"] = _event_timestamps(
        db, "SELECT timestamp_ms FROM liquidation_events "
        "WHERE source='bybit' ORDER BY timestamp_ms ASC"
    )
    out["liquidation_binance"] = _event_timestamps(
        db, "SELECT timestamp_ms FROM liquidation_events "
        "WHERE source='binance' ORDER BY timestamp_ms ASC"
    )
    # funding_history holds one row per symbol per poll — the cadence of the
    # *table* is the funding_hl/cex delivery cadence (same rows feed both).
    out["funding_hl"] = _event_timestamps(
        db, "SELECT timestamp FROM funding_history ORDER BY timestamp ASC"
    )
    out["funding_cex"] = _event_timestamps(
        db, "SELECT timestamp FROM funding_history ORDER BY timestamp ASC"
    )
    out["taker_split"] = _event_timestamps(
        db, "SELECT timestamp_ms FROM candles_1m "
        "WHERE (buy_volume > 0 OR sell_volume > 0) ORDER BY timestamp_ms ASC"
    )
    out["binance_perp"] = _event_timestamps(
        db, "SELECT timestamp_ms FROM binance_perp_prices "
        "ORDER BY timestamp_ms ASC"
    )
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
    args = parser.parse_args()

    cfg = load_config(args.config)
    contracts = feed_silence_contracts(cfg)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: bot DB not found: {db_path}", file=sys.stderr)
        return 1
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    now_ms = int(time.time() * 1000)
    recent_ms = int(args.recent_hours * 3600_000)
    timestamps = _feed_timestamps(db)
    db.close()

    report: dict = {"now_ms": now_ms, "recent_hours": args.recent_hours, "feeds": {}}
    codes = {"OK": 0, "WATCH": 2, "DEGRADING": 1, "insufficient": 0, "no_data": 0}
    worst = 0
    for feed in sorted(contracts):
        st = analyze_feed(
            feed, timestamps.get(feed, []),
            now_ms=now_ms, recent_ms=recent_ms,
            min_history=args.min_history,
        )
        report["feeds"][feed] = st
        worst = max(worst, codes.get(st["status"], 0))

    if args.json:
        print(json.dumps(report, indent=2))
        return worst

    def _dur(sec):
        if sec is None:
            return "—"
        if sec < 60:
            return f"{sec:.0f}s"
        if sec < 3600:
            return f"{sec / 60:.1f}m"
        return f"{sec / 3600:.1f}h"

    print(f"Feed cadence diagnostic — recent window {args.recent_hours:.0f}h "
          f"(now {time.strftime('%Y-%m-%d %H:%M', time.localtime(now_ms / 1000))} UTC)")
    print(f"{'feed':30s} {'status':10s} {'hist p95':>8s} {'hist p99':>8s} "
          f"{'rec med':>8s} {'rec p99':>8s} {'latest':>8s} {'trend':>8s}")
    print("-" * 96)
    for feed, st in report["feeds"].items():
        if st["status"] in ("no_data", "insufficient"):
            print(f"{feed:30s} {st['status']:10s} ({st.get('events', 0)} events, "
                  f"{st.get('gaps', 0)} gaps)")
            continue
        print(
            f"{feed:30s} {st['status']:10s} "
            f"{_dur(st['hist_p95_sec']):>8s} {_dur(st['hist_p99_sec']):>8s} "
            f"{_dur(st['recent_median_sec']):>8s} {_dur(st['recent_p99_sec']):>8s} "
            f"{_dur(st['latest_gap_sec']):>8s} {st['trend_sec_per_gap']:>+8.2f}s"
        )
    if worst >= 1:
        print("\n[DEGRADING] feed(s) consistently slower than their historical "
              "p99 — check delivery path.", file=sys.stderr)
    elif worst == 2:
        print("\n[WATCH] feed(s) above historical p95 or with a rising gap "
              "trend — delivery thinning?", file=sys.stderr)
    else:
        print("\n[PASS] all contracted feeds keep their historical cadence.")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
