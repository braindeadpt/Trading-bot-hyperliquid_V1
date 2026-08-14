"""Tests for scripts/feed_cadence_diagnostic.py.

Pins the cadence math (inter-event gaps, historical p95/p99, recent stats,
least-squares trend) and the verdict contract: a feed whose recent median
gap exceeds its historical p99 is DEGRADING (exit 1), above p95 or with a
rising trend is WATCH (exit 2), and a feed keeping its cadence is OK.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "feed_cadence_diagnostic.py"

pytestmark = pytest.mark.unit

NOW = int(time.time() * 1000)
MIN = 60_000


def _make_db(path: str, *, okx_gaps_min: list[float]) -> None:
    """Insert liquidation_okx events spaced by ``okx_gaps_min`` (minutes)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE liquidation_events (symbol TEXT, timestamp_ms INTEGER, "
               "notional_usd REAL, side TEXT, source TEXT)")
    db.execute("CREATE TABLE funding_history (symbol TEXT, current REAL, "
               "predicted REAL, timestamp INTEGER)")
    db.execute("CREATE TABLE candles_1m (symbol TEXT, timestamp_ms INTEGER, "
               "open REAL, high REAL, low REAL, close REAL, volume REAL, "
               "funding_rate REAL, oi_total REAL, oi_delta REAL, "
               "buy_volume REAL, sell_volume REAL, trade_count INTEGER)")
    db.execute("CREATE TABLE binance_perp_prices (symbol TEXT, timestamp_ms INTEGER, price REAL)")
    # funding + candles fresh so only liquidation_okx drives the verdict.
    db.execute("INSERT INTO funding_history VALUES ('BTC', 0.001, 0.001, ?)",
               (NOW - 5_000,))
    db.execute("INSERT INTO candles_1m VALUES ('BTC', ?, 1,2,0,1,10,0,100,0,5,5,100)",
               (NOW - 30_000,))
    t = NOW - int(sum(okx_gaps_min) * MIN) - 10 * MIN
    for g in okx_gaps_min:
        t += int(g * MIN)
        db.execute("INSERT INTO liquidation_events VALUES ('BTC', ?, 1e6, 'long', 'okx')",
                   (t,))
    db.commit()
    db.close()


def _run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


# ── pure helpers ─────────────────────────────────────────────────────────

def test_inter_event_gaps_and_percentile() -> None:
    from scripts.feed_cadence_diagnostic import (
        inter_event_gaps,
        percentile,
    )

    ts = [1000, 1000 + 60_000, 1000 + 3 * 60_000]
    gaps = inter_event_gaps(ts)
    assert [round(g, 1) for _, g in gaps] == [60.0, 120.0]
    # nearest-rank: index = int(0.5 * 4) = 2 -> ordered[2] = 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.99) == 4.0


def test_inter_event_gaps_caps_absurd_gaps() -> None:
    from scripts.feed_cadence_diagnostic import inter_event_gaps

    # A 30h gap is an outage, not cadence — capped out of the baseline.
    ts = [1000, 1000 + 60_000, 1000 + 30 * 3600_000]
    gaps = inter_event_gaps(ts)
    assert len(gaps) == 1


def test_least_squares_slope_positive_and_flat() -> None:
    from scripts.feed_cadence_diagnostic import least_squares_slope

    rising = least_squares_slope([0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert rising > 0
    flat = least_squares_slope([0.0, 1.0, 2.0, 3.0], [5.0, 5.0, 5.0, 5.0])
    assert flat == 0.0


def test_analyze_feed_degrading_when_recent_median_above_hist_p99() -> None:
    from scripts.feed_cadence_diagnostic import analyze_feed

    # History: 200 gaps of 1 min. Recent (last 1h): 5 gaps of 30 min.
    ts = []
    t = NOW - 300 * MIN
    for _ in range(200):
        ts.append(t)
        t += 1 * MIN
    recent_t = t + 10 * MIN
    for _ in range(5):
        ts.append(recent_t)
        recent_t += 30 * MIN
    st = analyze_feed("liquidation_okx", ts, now_ms=recent_t, recent_ms=2 * 3600_000,
                      min_history=50)
    assert st["status"] == "DEGRADING"
    assert st["hist_p99_sec"] <= 60.0 + 1e-6
    assert st["recent_median_sec"] >= 30 * 60.0


def test_analyze_feed_ok_when_cadence_unchanged() -> None:
    from scripts.feed_cadence_diagnostic import analyze_feed

    ts = []
    t = NOW - 300 * MIN
    for _ in range(250):
        ts.append(t)
        t += 1 * MIN
    st = analyze_feed("liquidation_okx", ts, now_ms=t, recent_ms=2 * 3600_000,
                      min_history=50)
    assert st["status"] == "OK"
    assert st["recent_median_sec"] <= 60.0 + 1e-6


def test_analyze_feed_insufficient_history() -> None:
    from scripts.feed_cadence_diagnostic import analyze_feed

    ts = [1000, 1000 + 60_000, 1000 + 120_000]  # only 2 gaps
    st = analyze_feed("liquidation_okx", ts, now_ms=1000 + 5 * MIN, recent_ms=3600_000,
                      min_history=50)
    assert st["status"] == "insufficient"


# ── CLI contract ─────────────────────────────────────────────────────────

def test_healthy_feed_exits_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, okx_gaps_min=[1.0] * 250)
        r = _run(["--db", db, "--min-history", "50",
                  "--report", os.path.join(tmp, "rep.md"),
                  "--history", os.path.join(tmp, "hist.json")])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[PASS]" in r.stdout
        assert "liquidation_okx" in r.stdout
        # the script wrote the markdown report
        assert os.path.exists(os.path.join(tmp, "rep.md"))


def test_degrading_feed_exits_one() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        # 200 one-minute gaps then 5 thirty-minute gaps — recent window must
        # be small so the 30-min gaps are "recent" vs the 1-min history.
        _make_db(db, okx_gaps_min=[1.0] * 200 + [30.0] * 5)
        r = _run(["--db", db, "--min-history", "50", "--recent-hours", "2",
                  "--report", os.path.join(tmp, "rep.md"),
                  "--history", os.path.join(tmp, "hist.json")])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "DEGRADING" in r.stdout
        assert "liquidation_okx" in r.stdout


def test_json_output_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, okx_gaps_min=[1.0] * 250)
        r = _run(["--db", db, "--min-history", "50", "--json",
                  "--report", os.path.join(tmp, "rep.md"),
                  "--history", os.path.join(tmp, "hist.json")])
        assert r.returncode == 0
        d = json.loads(r.stdout)
        assert "now_ms" in d
        assert "recent_hours" in d
        assert "liquidation_okx" in d["feeds"]
        st = d["feeds"]["liquidation_okx"]
        assert st["status"] == "OK"
        assert "hist_p95_sec" in st and "hist_p99_sec" in st
        assert "recent_median_sec" in st and "trend_sec_per_gap" in st


def test_missing_db_reports_error() -> None:
    r = _run(["--db", "/nonexistent/bot.db"])
    assert r.returncode == 1
    assert "not found" in r.stderr


# ── live snapshot cross-check (offline vs live monitor) ───────────────────

def _okx_ts(gap_secs, start=1_000_000):
    ts = [start]
    for g in gap_secs:
        ts.append(ts[-1] + int(g * 1000))
    return ts


def test_live_snapshot_equivalent_healthy() -> None:
    from scripts.feed_cadence_diagnostic import live_snapshot_equivalent

    ts = _okx_ts([60.0] * 120)
    now = ts[-1] + 30_000  # age 30s
    live = live_snapshot_equivalent(
        ts, now_ms=now, max_silence_sec=21600.0,
        warn_fraction=0.5, imminent_fraction=0.9,
        min_samples=100, gap_history=4000,
    )
    assert live["warn_level"] == "none"
    assert live["cadence_p50_sec"] == 60.0
    assert live["cadence_p99_sec"] == 60.0
    assert live["cadence_samples"] == 120
    # age 30s: no recorded gap is <= 30s -> rank 0%
    assert live["cadence_pct_current"] == 0.0


def test_live_snapshot_warn_levels_by_age() -> None:
    from scripts.feed_cadence_diagnostic import live_snapshot_equivalent

    ts = _okx_ts([60.0] * 120)
    max_sil = 21600.0
    for age_sec, expected in (
        (0.5 * max_sil, "early"),
        (0.9 * max_sil, "imminent"),
        (1.1 * max_sil, "degraded"),
    ):
        live = live_snapshot_equivalent(
            ts, now_ms=ts[-1] + int(age_sec * 1000), max_silence_sec=max_sil,
            warn_fraction=0.5, imminent_fraction=0.9,
            min_samples=100, gap_history=4000,
        )
        assert live["warn_level"] == expected, age_sec


def test_live_snapshot_none_without_events() -> None:
    from scripts.feed_cadence_diagnostic import live_snapshot_equivalent

    assert live_snapshot_equivalent(
        [], now_ms=1, max_silence_sec=21600.0, warn_fraction=0.5,
        imminent_fraction=0.9, min_samples=100, gap_history=4000,
    ) is None
    assert live_snapshot_equivalent(
        [1_000_000], now_ms=1_000_001, max_silence_sec=21600.0,
        warn_fraction=0.5, imminent_fraction=0.9,
        min_samples=100, gap_history=4000,
    ) is None


def test_cross_verdict_buckets() -> None:
    from scripts.feed_cadence_diagnostic import cross_verdict

    none = {"warn_level": "none"}
    early = {"warn_level": "early"}
    imminent = {"warn_level": "imminent"}
    degraded = {"warn_level": "degraded"}
    assert cross_verdict("OK", none) == "aligned_ok"
    assert cross_verdict("WATCH", none) == "aligned_ok"
    assert cross_verdict("DEGRADING", imminent) == "aligned_trouble"
    assert cross_verdict("DEGRADING", degraded) == "aligned_trouble"
    assert cross_verdict("DEGRADING", none) == "offline_ahead"
    assert cross_verdict("DEGRADING", early) == "offline_ahead"
    assert cross_verdict("OK", degraded) == "live_ahead"
    assert cross_verdict("WATCH", early) == "live_escalating"
    assert cross_verdict("OK", None) == "no_live_data"
    assert cross_verdict("insufficient", none) == "no_diagnosis"


def test_json_report_crosses_with_live_snapshot() -> None:
    """The JSON report carries the reconstructed live monitor state per feed
    (warn_level + cadence percentiles) and the cross verdict."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, okx_gaps_min=[1.0] * 250)
        r = _run(["--db", db, "--min-history", "50", "--json",
                  "--report", os.path.join(tmp, "rep.md"),
                  "--history", os.path.join(tmp, "hist.json")])
        assert r.returncode == 0
        d = json.loads(r.stdout)
        assert "live_thresholds" in d
        assert d["live_thresholds"]["warn_fraction"] == 0.5
        st = d["feeds"]["liquidation_okx"]
        assert st["cross"] == "aligned_ok"
        live = st["live_snapshot"]
        assert live["warn_level"] == "none"
        assert live["cadence_p99_sec"] == 60.0
        # the last event is ~10min old; every 60s gap is <= that age
        assert live["cadence_pct_current"] == 100.0


def test_report_accumulates_history_per_feed() -> None:
    """Two runs accumulate two history rows per feed in the JSON, and the
    markdown report shows the current state + the per-feed trend table."""
    from scripts.feed_cadence_diagnostic import (
        load_cadence_history,
        render_markdown_report,
        write_cadence_report,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, okx_gaps_min=[1.0] * 250)
        rep = os.path.join(tmp, "rep.md")
        hist = os.path.join(tmp, "hist.json")
        for _ in range(2):
            r = _run(["--db", db, "--min-history", "50",
                      "--report", rep, "--history", hist])
            assert r.returncode == 0

        # history accumulated 2 rows for the okx feed
        history = load_cadence_history(path=Path(hist))
        okx_rows = [h for h in history if h["feed"] == "liquidation_okx"]
        assert len(okx_rows) == 2
        assert okx_rows[-1]["status"] == "OK"
        assert okx_rows[-1]["cross"] == "aligned_ok"

        # the markdown report renders both the current table and the trend
        md = Path(rep).read_text(encoding="utf-8")
        assert "# Feed Cadence Report" in md
        assert "## Estado actual" in md
        assert "liquidation_okx" in md
        assert "## Histórico de tendências por feed" in md
        assert "| Run (UTC) | Status | rec med | hist p99 | trend | cross |" in md
        assert md.count("| 2026-") >= 2  # two accumulated runs rendered


def test_history_cap_per_feed() -> None:
    from scripts.feed_cadence_diagnostic import (
        HISTORY_CAP_PER_FEED,
        record_and_save_history,
    )

    with tempfile.TemporaryDirectory() as tmp:
        hp = Path(tmp) / "hist.json"
        report = {
            "now_ms": 1,
            "feeds": {"liquidation_okx": {"status": "OK"}},
        }
        # 3 runs would exceed the tiny cap -> only the cap survives
        old = HISTORY_CAP_PER_FEED
        from scripts import feed_cadence_diagnostic as dg
        dg.HISTORY_CAP_PER_FEED = 3
        try:
            for _ in range(5):
                record_and_save_history(report, path=hp)
        finally:
            dg.HISTORY_CAP_PER_FEED = old
        from scripts.feed_cadence_diagnostic import load_cadence_history
        history = load_cadence_history(path=hp)
        assert len([h for h in history if h["feed"] == "liquidation_okx"]) == 3
