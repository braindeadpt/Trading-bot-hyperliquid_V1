"""Tests for scripts/research/feed_cadence_diagnostic.py.

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
SCRIPT = ROOT / "scripts" / "research" / "feed_cadence_diagnostic.py"

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
    from scripts.research.feed_cadence_diagnostic import (
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
    from scripts.research.feed_cadence_diagnostic import inter_event_gaps

    # A 30h gap is an outage, not cadence — capped out of the baseline.
    ts = [1000, 1000 + 60_000, 1000 + 30 * 3600_000]
    gaps = inter_event_gaps(ts)
    assert len(gaps) == 1


def test_least_squares_slope_positive_and_flat() -> None:
    from scripts.research.feed_cadence_diagnostic import least_squares_slope

    rising = least_squares_slope([0.0, 1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0])
    assert rising > 0
    flat = least_squares_slope([0.0, 1.0, 2.0, 3.0], [5.0, 5.0, 5.0, 5.0])
    assert flat == 0.0


def test_analyze_feed_degrading_when_recent_median_above_hist_p99() -> None:
    from scripts.research.feed_cadence_diagnostic import analyze_feed

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
    from scripts.research.feed_cadence_diagnostic import analyze_feed

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
    from scripts.research.feed_cadence_diagnostic import analyze_feed

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
    from scripts.research.feed_cadence_diagnostic import live_snapshot_equivalent

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
    from scripts.research.feed_cadence_diagnostic import live_snapshot_equivalent

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
    from scripts.research.feed_cadence_diagnostic import live_snapshot_equivalent

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
    from scripts.research.feed_cadence_diagnostic import cross_verdict

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
    from scripts.research.feed_cadence_diagnostic import (
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
    from scripts.research.feed_cadence_diagnostic import (
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
        from scripts.research import feed_cadence_diagnostic as dg
        dg.HISTORY_CAP_PER_FEED = 3
        try:
            for _ in range(5):
                record_and_save_history(report, path=hp)
        finally:
            dg.HISTORY_CAP_PER_FEED = old
        from scripts.research.feed_cadence_diagnostic import load_cadence_history
        history = load_cadence_history(path=hp)
        assert len([h for h in history if h["feed"] == "liquidation_okx"]) == 3


# ── Task 4: streaming reads (no fetchall over unbounded tables) ─────────

def test_run_cadence_streams_without_fetchall(tmp_path, monkeypatch) -> None:
    """run_cadence_diagnostic must not materialize per-feed timestamp lists.

    Task 4 (memory): the old ``_feed_timestamps`` fetchall'd ~1.9M rows
    across 7 feeds (funding_history alone ~700k, loaded twice for
    funding_hl + funding_cex). The streaming path consumes rows pairwise
    via fetchmany — a cursor whose fetchall raises must still produce the
    full report.
    """
    import sqlite3 as _sq

    from scripts.research import feed_cadence_diagnostic as dg

    db_path = tmp_path / "bot.db"
    _make_db(str(db_path), okx_gaps_min=[1.0] * 250)
    real_connect = _sq.connect

    class _CursorProxy:
        def __init__(self, cur):
            self._cur = cur

        def fetchall(self):
            raise AssertionError("fetchall() used — stream rows via fetchmany")

        def fetchmany(self, n):
            return self._cur.fetchmany(n)

        def fetchone(self):
            return self._cur.fetchone()

        def __iter__(self):
            return iter(self._cur)

    class _ConnProxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, params=()):
            return _CursorProxy(self._conn.execute(query, params))

        def close(self):
            self._conn.close()

    monkeypatch.setattr(
        dg.sqlite3, "connect", lambda *a, **k: _ConnProxy(real_connect(*a, **k))
    )
    report = dg.run_cadence_diagnostic(
        db_path,
        {"liquidation_okx": 3600.0, "funding_hl": 7200.0},
        now_ms=NOW,
        recent_hours=48.0,
        warn_fraction=0.5,
        imminent_fraction=0.9,
        min_samples=10,
        gap_history=4000,
    )
    st = report["feeds"]["liquidation_okx"]
    assert st["status"] == "OK"
    assert st["events"] == 250


def test_run_cadence_matches_materialized_list_path(tmp_path) -> None:
    """Streaming output must equal the old list-based computation."""
    from scripts.research import feed_cadence_diagnostic as dg

    db_path = tmp_path / "bot.db"
    _make_db(str(db_path), okx_gaps_min=[1.0] * 250)
    report = dg.run_cadence_diagnostic(
        db_path,
        {"liquidation_okx": 3600.0},
        now_ms=NOW,
        recent_hours=48.0,
        warn_fraction=0.5,
        imminent_fraction=0.9,
        min_samples=10,
        gap_history=4000,
    )
    conn = sqlite3.connect(str(db_path))
    ts = [
        int(r[0])
        for r in conn.execute(
            "SELECT timestamp_ms FROM liquidation_events "
            "WHERE source='okx' ORDER BY timestamp_ms ASC"
        )
    ]
    conn.close()
    expected = dg.analyze_feed(
        "liquidation_okx", ts, now_ms=NOW,
        recent_ms=int(48.0 * 3600_000), min_history=50,
    )
    got = report["feeds"]["liquidation_okx"]
    for key, value in expected.items():
        assert got[key] == value, f"{key}: {got[key]!r} != {value!r}"
    expected_live = dg.live_snapshot_equivalent(
        ts, now_ms=NOW, max_silence_sec=3600.0, warn_fraction=0.5,
        imminent_fraction=0.9, min_samples=10, gap_history=4000,
    )
    assert got["live_snapshot"] == expected_live


# ── Task 5: true streaming analysis (bounded memory, identical verdict) ──
#
# The fetchmany streaming fixed row materialization but still retained one
# ``(end_ms, gap)`` tuple per sane gap — ~1.9M tuples / ~190MB traced for the
# real DB, then RSS stayed at ~2GB for hours (arena fragmentation, same
# mechanism as 81dc3e0). The analysis only needs percentiles of the
# historical/recent windows plus the recent tail, and every gap is an integer
# number of milliseconds — a per-ms histogram yields EXACT percentiles with
# memory proportional to the number of distinct gap values.

REAL_DB = ROOT / "data" / "live" / "bot.db"
# Bound for the streaming path: the chunk buffer (~0.5MB at 8192 rows), the
# recent-window list (time-bounded), the per-ms gap histograms and the small
# report dict. Anything rematerializing the full gap series blows far past
# this on a populated DB (the list path peaked ~190MB traced per feed).
PEAK_BYTES_SYNTHETIC = 8 * 1024 * 1024
PEAK_BYTES_REAL_DB = 32 * 1024 * 1024
# Whole-panel bound: measured 5.0MB traced on the real DB (2026-09-22,
# ~1.9M events across feeds) — the bound keeps ~12x headroom for window
# growth while a regression to materializing the gap series (~78MB
# standalone, ~400MB inside the bot process) trips it immediately.
PEAK_BYTES_PAYLOAD = 64 * 1024 * 1024


def _copy_db_snapshot(src: Path, dst_dir: Path) -> Path:
    """Copy a live SQLite DB (main + WAL/SHM sidecars) so the test reads a
    frozen image while the bot keeps writing. Both implementations under
    comparison read the same copy, so copy-time skew cannot break parity."""
    import shutil

    dst = dst_dir / "bot.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, Path(str(dst) + suffix))
    return dst


def _feed_ts(conn: sqlite3.Connection, query: str) -> list[int]:
    try:
        return [int(r[0]) for r in conn.execute(query)]
    except sqlite3.OperationalError:
        return []  # table missing — feed has no persisted events


def _assert_feed_parity(
    report: dict,
    feed: str,
    ts: list[int],
    *,
    now_ms: int,
    recent_ms: int,
    min_history: int,
    max_sil: float,
    warn_fraction: float,
    imminent_fraction: float,
    min_samples: int,
    gap_history: int,
) -> None:
    """Field-by-field equality of the streaming report vs the reference
    list-based computation (``analyze_feed`` + ``live_snapshot_equivalent``)."""
    from scripts.research import feed_cadence_diagnostic as dg

    expected = dg.analyze_feed(
        feed, ts, now_ms=now_ms, recent_ms=recent_ms, min_history=min_history,
    )
    got = report["feeds"][feed]
    for key, value in expected.items():
        assert got[key] == value, f"{feed}.{key}: {got[key]!r} != {value!r}"
    exp_live = dg.live_snapshot_equivalent(
        ts, now_ms=now_ms, max_silence_sec=max_sil,
        warn_fraction=warn_fraction, imminent_fraction=imminent_fraction,
        min_samples=min_samples, gap_history=gap_history,
    )
    assert got["live_snapshot"] == exp_live
    assert got["cross"] == dg.cross_verdict(expected["status"], exp_live)


def _make_edge_db(path: str) -> None:
    """DB exercising every window/fallback branch of the gap analysis."""
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

    def ins(source: str, start: int, step: int, n: int) -> None:
        t = start
        for _ in range(n):
            db.execute(
                "INSERT INTO liquidation_events VALUES ('BTC', ?, 1e6, 'long', ?)",
                (t, source),
            )
            t += step

    # okx: everything older than the 48h window -> recent falls back to the
    # min_history tail of the series.
    ins("okx", NOW - 400 * MIN, MIN, 300)
    # bybit: every gap inside the recent window -> history falls back to the
    # whole series.
    ins("bybit", NOW - 100 * MIN, MIN, 120)
    # binance: mixed — 200 historical 1m gaps, an outage-sized jump, then 100
    # recent 30s gaps (also covers the max_gap_sec filter).
    ins("binance", NOW - 300 * MIN, MIN, 200)
    ins("binance", NOW - 80 * MIN, 30_000, 100)
    for i in range(150):
        db.execute("INSERT INTO funding_history VALUES ('BTC', 0.001, 0.001, ?)",
                   (NOW - (150 - i) * 10 * MIN,))
        db.execute("INSERT INTO candles_1m VALUES ('BTC', ?, 1,2,0,1,10,0,100,0,5,5,100)",
                   (NOW - (150 - i) * MIN,))
        db.execute("INSERT INTO binance_perp_prices VALUES ('BTC', ?, 50000)",
                   (NOW - (150 - i) * 2 * MIN,))
    db.commit()
    db.close()


def test_streaming_parity_all_window_branches(tmp_path) -> None:
    """Synthetic parity: tail fallback, empty-history fallback, max_gap
    filter, shared funding series and a missing-table feed — all must produce
    the identical verdict dict as the list path."""
    from scripts.research import feed_cadence_diagnostic as dg

    db_path = tmp_path / "bot.db"
    _make_edge_db(str(db_path))
    contracts = {f: 3600.0 for f in dg._FEED_QUERIES}
    contracts["unknown_feed"] = 60.0  # no query -> empty series -> no_data
    recent_ms = int(48.0 * 3600_000)
    report = dg.run_cadence_diagnostic(
        db_path, contracts, now_ms=NOW, recent_hours=48.0,
        warn_fraction=0.5, imminent_fraction=0.9,
        min_samples=10, gap_history=4000,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        for feed, query in dg._FEED_QUERIES.items():
            ts = _feed_ts(conn, query)
            _assert_feed_parity(
                report, feed, ts, now_ms=NOW, recent_ms=recent_ms,
                min_history=50, max_sil=3600.0, warn_fraction=0.5,
                imminent_fraction=0.9, min_samples=10, gap_history=4000,
            )
    finally:
        conn.close()
    uf = report["feeds"]["unknown_feed"]
    assert uf["status"] == "no_data" and uf["events"] == 0
    assert uf["live_snapshot"] is None and uf["cross"] == "no_live_data"
    # sanity: the branch coverage actually happened on this fixture
    statuses = {f: report["feeds"][f]["status"] for f in dg._FEED_QUERIES}
    assert "insufficient" not in statuses.values()


@pytest.mark.skipif(not REAL_DB.exists(), reason="data/live/bot.db not present")
def test_streaming_parity_on_real_db(tmp_path) -> None:
    """Golden test: run the streaming report on a snapshot of the real
    ``bot.db`` and compare every feed verdict field-by-field against the
    reference list-based implementation over the materialized timestamps."""
    from scripts.research import feed_cadence_diagnostic as dg

    db_path = _copy_db_snapshot(REAL_DB, tmp_path)
    contracts = {f: 3600.0 for f in dg._FEED_QUERIES}
    contracts["unknown_feed"] = 60.0
    recent_ms = int(48.0 * 3600_000)
    report = dg.run_cadence_diagnostic(
        db_path, contracts, now_ms=NOW, recent_hours=48.0,
        warn_fraction=0.5, imminent_fraction=0.9,
        min_samples=10, gap_history=4000,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        for feed, query in dg._FEED_QUERIES.items():
            _assert_feed_parity(
                report, feed, _feed_ts(conn, query),
                now_ms=NOW, recent_ms=recent_ms, min_history=50,
                max_sil=3600.0, warn_fraction=0.5, imminent_fraction=0.9,
                min_samples=10, gap_history=4000,
            )
    finally:
        conn.close()


def test_run_cadence_streaming_peak_memory_bounded(tmp_path) -> None:
    """The analysis must not retain per-event/per-gap structures.

    200k events at 1m spacing: the list-based path allocates ~200k
    ``(end_ms, gap)`` tuples (~18MB traced); the streaming path must stay
    under 8MiB (chunk buffer + recent-window list + the per-ms histogram —
    1m gaps collapse to a single histogram key)."""
    import tracemalloc

    from scripts.research import feed_cadence_diagnostic as dg

    db_path = tmp_path / "bot.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE liquidation_events (symbol TEXT, timestamp_ms INTEGER, "
               "notional_usd REAL, side TEXT, source TEXT)")
    n = 200_000
    t0 = NOW - n * MIN
    db.executemany(
        "INSERT INTO liquidation_events VALUES ('BTC', ?, 1e6, 'long', 'okx')",
        ((t0 + i * MIN,) for i in range(n)),
    )
    db.commit()
    db.close()

    tracemalloc.start(5)
    try:
        report = dg.run_cadence_diagnostic(
            db_path, {"liquidation_okx": 3600.0}, now_ms=NOW,
            recent_hours=48.0, warn_fraction=0.5, imminent_fraction=0.9,
            min_samples=10, gap_history=4000,
        )
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report["feeds"]["liquidation_okx"]["events"] == n
    assert peak < PEAK_BYTES_SYNTHETIC, f"traced peak {peak/1e6:.1f}MB"


@pytest.mark.skipif(not REAL_DB.exists(), reason="data/live/bot.db not present")
def test_run_cadence_peak_memory_bounded_real_db(tmp_path) -> None:
    """Same bound against the real DB (~1.9M events across feeds): the
    streaming run must stay under 32MiB traced — vs ~400MB peak measured for
    the gap-list implementation on this data."""
    import tracemalloc

    from scripts.research import feed_cadence_diagnostic as dg

    db_path = _copy_db_snapshot(REAL_DB, tmp_path)
    contracts = {f: 3600.0 for f in dg._FEED_QUERIES}
    tracemalloc.start(5)
    try:
        report = dg.run_cadence_diagnostic(
            db_path, contracts, now_ms=NOW, recent_hours=48.0,
            warn_fraction=0.5, imminent_fraction=0.9,
            min_samples=10, gap_history=4000,
        )
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert report["feeds"], "expected per-feed verdicts on the real DB"
    assert peak < PEAK_BYTES_REAL_DB, f"traced peak {peak/1e6:.1f}MB"


@pytest.mark.skipif(not REAL_DB.exists(), reason="data/live/bot.db not present")
def test_research_watchdogs_payload_peak_memory_bounded() -> None:
    """The dashboard panel build (``/api/research_watchdogs``) runs inside
    the trading process — its traced peak must stay bounded regardless of
    history depth."""
    import tracemalloc

    from src.research.research_watchdog_status import (
        build_research_watchdogs_payload,
    )

    tracemalloc.start(5)
    try:
        payload = build_research_watchdogs_payload()
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert payload["watchdogs"]
    assert peak < PEAK_BYTES_PAYLOAD, f"traced peak {peak/1e6:.1f}MB"
