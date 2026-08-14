"""Unit tests for scripts/calibrate_liquidation_stopout_floor.py.

The script measures the real distribution of the dominant 5m liquidation
notional (multi-venue okx+bybit) to calibrate the stop-out floor
(``LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD``). These tests pin the pure
building blocks: the dominant-notional window math (must mirror the engine's
``_get_liquidation_stats`` exactly), the fixed-cadence walk, the nearest-rank
percentiles, and the CLI plumbing (real-events-only load, report rendering).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import calibrate_liquidation_stopout_floor as cal  # noqa: E402

SCRIPT = ROOT / "scripts" / "calibrate_liquidation_stopout_floor.py"

pytestmark = pytest.mark.unit


def _write_events(tmp: str, rows: list[tuple]) -> str:
    """Create a live DB with the given liquidation_events rows."""
    db = os.path.join(tmp, "bot.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE liquidation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "symbol TEXT, timestamp_ms INTEGER, notional_usd REAL, side TEXT, source TEXT)")
    for symbol, ts, notional, side, source in rows:
        conn.execute("INSERT INTO liquidation_events (symbol, timestamp_ms, notional_usd, side, source) "
                     "VALUES (?, ?, ?, ?, ?)", (symbol, ts, notional, side, source))
    conn.commit()
    conn.close()
    return db


def test_dominant_notional_matches_engine_semantics() -> None:
    """Dominant side by summed notional, ties -> long — the exact rule the
    engine and the backtest replay share."""
    # Longs dominate.
    assert cal.dominant_notional([(0, 3.0, "long"), (1, 1.0, "short")]) == (3.0, "long", 1)
    # Shorts dominate.
    assert cal.dominant_notional([(0, 2.0, "long"), (1, 5.0, "short")]) == (5.0, "short", 1)
    # Tie -> long (engine's >= comparison).
    assert cal.dominant_notional([(0, 2.0, "long"), (1, 2.0, "short")]) == (2.0, "long", 1)
    # Counts are per-side.
    assert cal.dominant_notional([(0, 1.0, "long"), (1, 1.0, "long"), (2, 1.0, "short")]) == (2.0, "long", 2)
    # Empty window -> None (cold start never fakes a flush).
    assert cal.dominant_notional([]) is None


def test_walk_window_series_sliding_5m_window() -> None:
    """A burst outside the 5m window must not leak into a later sample."""
    t0 = 1_000_000
    events = [
        ("BTC", t0, 4_000_000.0, "long", "okx"),
        ("BTC", t0 + 60_000, 3_000_000.0, "long", "bybit"),
        # Far later event — the old burst must have pruned out.
        ("BTC", t0 + 600_000, 1_000_000.0, "short", "okx"),
    ]
    rows = cal.walk_window_series(events, step_sec=60)
    assert rows, "expected samples"
    # Early samples: the 7M long burst dominates (first sample at t0 sees
    # only the first event; the bybit 3M joins from the next sample on).
    early = [r for r in rows if r["ts"] <= t0 + 240_000]
    assert early and all(r["dominant_side"] == "long" for r in early)
    assert any(r["dominant_notional"] == 7_000_000.0 for r in early)
    # Late sample (after the burst pruned): only the short 1M event.
    late = [r for r in rows if r["ts"] >= t0 + 540_000]
    assert late and late[0]["dominant_side"] == "short"
    assert late[0]["dominant_notional"] == 1_000_000.0


def test_walk_window_series_per_symbol_independent() -> None:
    """Windows are per-symbol: BTC and ETH bursts never mix."""
    t0 = 1_000_000
    events = [
        ("BTC", t0, 9_000_000.0, "long", "okx"),
        ("ETH", t0, 1_000_000.0, "short", "bybit"),
    ]
    rows = cal.walk_window_series(events, step_sec=60)
    btc = [r for r in rows if r["symbol"] == "BTC"]
    eth = [r for r in rows if r["symbol"] == "ETH"]
    assert btc and btc[0]["dominant_notional"] == 9_000_000.0
    assert eth and eth[0]["dominant_notional"] == 1_000_000.0
    assert eth[0]["dominant_side"] == "short"


def test_proxy_events_never_calibrate() -> None:
    """Proxy rows are synthetic estimates — they must never enter the
    calibration sample (the floor guards real money)."""
    t0 = 1_000_000
    rows = [
        ("BTC", t0, 50_000_000.0, "long", "proxy"),
        ("BTC", t0, 100_000_000.0, "long", "binance"),
    ]
    # load_real_events filters to REAL_SOURCES only.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = _write_events(tmp, rows)
        loaded = cal.load_real_events(Path(db))
        assert len(loaded) == 0  # neither proxy nor binance is contracted here


def test_load_real_events_filters_sources(tmp_path) -> None:
    db = _write_events(
        str(tmp_path),
        [("BTC", 1_000_000, 1.0, "long", "okx"),
         ("BTC", 1_000_001, 2.0, "short", "bybit"),
         ("BTC", 1_000_002, 3.0, "long", "proxy")],
    )
    loaded = cal.load_real_events(Path(db))
    assert [(r[3], r[4]) for r in loaded] == [("long", "okx"), ("short", "bybit")]


def test_nearest_rank_percentile() -> None:
    """Nearest-rank with ``int(pct * n)`` — the same rule the shared
    ``cadence_percentile`` uses, so the pooled p90 means the same thing as
    the monitor's percentiles."""
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert cal.nearest_rank_pct(vals, 50) == 60   # int(0.5*10)=5 -> 6th
    assert cal.nearest_rank_pct(vals, 90) == 100  # int(0.9*10)=9 -> last
    assert cal.nearest_rank_pct(vals, 95) == 100
    assert cal.nearest_rank_pct(vals, 0) == 10
    assert cal.nearest_rank_pct(vals, 100) == 100
    assert cal.nearest_rank_pct([], 90) is None


def test_pool_stats_and_by_symbol() -> None:
    rows = [
        {"ts": 0, "symbol": "BTC", "dominant_notional": 10.0, "dominant_side": "long", "count": 1},
        {"ts": 1, "symbol": "BTC", "dominant_notional": 20.0, "dominant_side": "long", "count": 1},
        {"ts": 2, "symbol": "ETH", "dominant_notional": 5.0, "dominant_side": "short", "count": 1},
    ]
    pooled = cal.pool_stats(rows)
    assert pooled["n"] == 3
    assert pooled["p50"] == 10.0
    assert pooled["p90"] == 20.0
    per = cal.by_symbol_stats(rows)
    assert per["BTC"]["n"] == 2
    assert per["BTC"]["p90"] == 20.0
    assert per["ETH"]["n"] == 1
    assert per["ETH"]["max"] == 5.0


def test_cli_writes_report_and_json(tmp_path) -> None:
    """End-to-end: the CLI measures a synthetic burst and writes the report."""
    t0 = 1_000_000
    rows = [
        ("BTC", t0 + i * 10_000, 4_000_000.0, "long", "okx")
        for i in range(10)
    ] + [
        ("ETH", t0 + i * 10_000, 1_000_000.0, "short", "bybit")
        for i in range(10)
    ]
    db = _write_events(str(tmp_path), rows)
    report = tmp_path / "report.md"
    jp = tmp_path / "out.json"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", db, "--step-sec", "60",
         "--report", str(report), "--json", str(jp)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert report.exists()
    md = report.read_text(encoding="utf-8")
    assert "Liquidation stop-out floor" in md
    assert "okx + bybit" in md
    assert "p90" in md
    data = json_load(jp)
    assert data["pooled"]["n"] > 0
    assert data["per_symbol"]["BTC"]["p90"] >= 4_000_000.0


def json_load(path):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_cli_no_real_events_returns_1(tmp_path) -> None:
    """Only proxy rows -> nothing to calibrate, exit 1."""
    db = _write_events(str(tmp_path), [("BTC", 1_000_000, 5.0, "long", "proxy")])
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", db],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    assert r.returncode == 1
    assert "Sem eventos reais" in (r.stdout or "")
