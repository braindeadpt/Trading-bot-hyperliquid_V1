"""Tests for scripts/iv_gate_shadow_vs_pnl.py — live high_iv vs low_iv comparison.

The IV gate is shadow-only (docs/IV_HIGH_ONLY_AB_SPLIT.md, n=13 INCONCLUSIVE):
the router records an ``iv_gate_shadow`` decision per routed trade (research
DB, IV class in snapshot metadata) *before* execution. This script joins those
decisions to executed trades (live DB, key strategy+symbol+side + nearest
entry_time within tolerance) and compares the high_iv / low_iv slices on
realized PnL — the live counterpart of the backtest gate.

These tests pin: the join semantics (nearest within tolerance, decision
consumed once, routed-but-unfilled decision = coverage loss), the slice
statistics (WR / net / avg on closed trades, open excluded from PnL), and the
verdict gate (n>=30 closed with an IV decision, else INCONCLUSIVE).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import iv_gate_shadow_vs_pnl as pnl  # noqa: E402

SCRIPT = ROOT / "scripts" / "iv_gate_shadow_vs_pnl.py"

pytestmark = pytest.mark.unit


def _make_dbs(tmp: str, *, n_high: int = 0, n_low: int = 0, n_unknown: int = 0,
              with_unmatched_decision: bool = False,
              with_open_trade: bool = False,
              pnl_high: float = 10.0, pnl_low: float = -5.0) -> tuple[str, str]:
    """Build synthetic live + research DBs.

    Trades are created at timestamps ``T0 + i * 60_000`` (60s apart); each
    decision sits exactly 100ms before its trade's entry_time, so the join
    always resolves within the default 60s tolerance. ``with_open_trade``
    adds one extra open (no-PnL) low_iv trade after the closed ones.
    """
    live = os.path.join(tmp, "bot.db")
    research = os.path.join(tmp, "hyperliquid.db")

    db = sqlite3.connect(live)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, "
               "entry_time INTEGER, exit_time INTEGER, pnl_usd REAL, pnl_pct REAL, "
               "strategy TEXT, status TEXT, exit_reason TEXT)")
    rdb = sqlite3.connect(research)
    rdb.execute("CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, symbol TEXT, "
                "strategy TEXT, variant TEXT, side TEXT, would_enter INTEGER, "
                "reason TEXT, timestamp_ms INTEGER, snapshot_json TEXT, "
                "ingested_at_ms INTEGER)")

    t0 = 1_786_600_000_000
    i = 0
    trade_id = 1
    decision_id = 1

    def add_trade(pnl, cls):
        nonlocal i, trade_id, decision_id
        ts = t0 + i * 60_000
        i += 1
        status = "closed" if pnl is not None else "open"
        db.execute(
            "INSERT INTO trades VALUES (?, 'BTC', 'long', ?, ?, ?, ?, 'VWAPDeviation', ?, 'tp')",
            (trade_id, ts, ts + 3_600_000, pnl, pnl, status),
        )
        trade_id += 1
        if cls != "unknown":
            meta = {"iv_class": cls, "iv_percentile": 75.0 if cls == "high_iv" else 40.0,
                    "iv_threshold": 66.7, "iv_currency": "BTC"}
            snap = json.dumps({"metadata": meta})
            rdb.execute(
                "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
                "'iv_gate_shadow', 'long', 1, ?, ?, ?, 0)",
                (decision_id, f"iv_gate:{cls}", ts - 100, snap),
            )
            decision_id += 1

    for _ in range(n_high):
        add_trade(pnl_high, "high_iv")
    for _ in range(n_low):
        add_trade(pnl_low, "low_iv")
    for _ in range(n_unknown):
        add_trade(-1.0, "unknown")
    # Optional open trade (no PnL) with a decision — counts in n, not closed PnL.
    if with_open_trade:
        add_trade(None, "low_iv")

    if with_unmatched_decision:
        # A routed decision with no trade row (risk reject / no fill): the join
        # must report it as coverage loss, not fabricate a match.
        rdb.execute(
            "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
            "'iv_gate_shadow', 'long', 1, 'iv_gate:high_iv', ?, ?, 0)",
            (decision_id, t0 - 500_000_000,
             json.dumps({"metadata": {"iv_class": "high_iv", "iv_percentile": 80.0,
                                      "iv_threshold": 66.7, "iv_currency": "BTC"}})),
        )

    db.commit()
    db.close()
    rdb.commit()
    rdb.close()
    return live, research


def _run(args: list[str], tmp: str):
    live, research = _make_dbs(tmp, **{})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--live-db", live, "--research-db", research, *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


def _make(run_args, **kw):
    """Build DBs in a fresh tempdir and run the script against them."""
    # ignore_cleanup_errors: Windows AV may briefly hold the fresh .db file
    # open after the child exits — the assertion outcome is unaffected.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_dbs(tmp, **kw)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", live,
             "--research-db", research, *run_args],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        return r


def test_join_attributes_decision_to_trade() -> None:
    r = _make([], n_low=1, with_open_trade=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "trades com decisão IV: 2/2" in r.stdout
    assert "decisões consumidas: 2/2" in r.stdout
    # Open trade counts in n but has no closed PnL.
    assert "low_iv | 2 | 1 | 1" in r.stdout


def test_missing_dbs_reports_gracefully() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", os.path.join(tmp, "no.db"),
             "--research-db", os.path.join(tmp, "no2.db")],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Sem trades" in r.stdout


def test_high_iv_and_low_iv_slices_split_pnl() -> None:
    r = _make([], n_high=2, n_low=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "high_iv | 2 | 2 | 0 | 100% | +20.00" in r.stdout
    assert "low_iv | 2 | 2 | 0 | 0% | -10.00" in r.stdout


def test_unknown_slice_for_trades_without_decision() -> None:
    r = _make([], n_unknown=3)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "unknown | 3 | 3 | 0" in r.stdout
    assert "trades com decisão IV: 0/3" in r.stdout


def test_unmatched_decision_reported_as_coverage_loss() -> None:
    """A routed decision with no trade (risk reject / no fill) is not
    fabricated into a match — coverage shows decision consumed < total."""
    r = _make([], n_low=1, with_unmatched_decision=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "decisões consumidas: 1/2" in r.stdout


def test_decisions_per_day_returns_14_days_zero_filled() -> None:
    """The per-day series spans exactly 14 days, oldest first, 0-filled for
    empty days — the dashboard sparkline reads it as a rate, not a sparse set."""
    from datetime import datetime, timezone

    # Fix "now" at UTC midday — a day-boundary-fixed now (e.g. 00:00:05)
    # would shift "yesterday minus 5s" into the day before.
    now = int(datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = [
        {"timestamp_ms": now - 1_000},                      # today
        {"timestamp_ms": now - 86_400_000 - 5_000},        # yesterday
        {"timestamp_ms": now - 13 * 86_400_000},           # 13 days ago
        {"timestamp_ms": now - 20 * 86_400_000},           # outside window
    ]
    rows = pnl.decisions_per_day(decisions, now_ms=now)
    assert len(rows) == 14
    assert rows[0]["date"] <= rows[-1]["date"]
    assert rows[-1]["n"] == 1  # today
    assert rows[-2]["n"] == 1  # yesterday
    assert rows[0]["n"] == 1   # 13 days ago (oldest in window)
    assert sum(r["n"] for r in rows) == 3  # the outside-window one excluded
    assert all(r["n"] == 0 for r in rows[1:-2])  # middle days zero-filled


def test_report_carries_decisions_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_report exposes the per-day series for the dashboard panel."""
    # Anchor the script's clock on the fixture's fixed t0 (2026-08-13) so the
    # 3 synthetic decisions always land on the newest bucket regardless of the
    # wall clock — a date-dependent test would flip at each UTC midnight.
    monkeypatch.setattr(pnl.time, "time", lambda: (1_786_600_000_000 + 3_600_000) / 1000.0)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_dbs(tmp, n_high=2, n_low=1)
        report = pnl.build_report(live_db=live, research_db=research)
        assert "decisions_per_day" in report
        assert isinstance(report["decisions_per_day"], list)
        assert len(report["decisions_per_day"]) == 14
        # The 3 synthetic decisions all land on the same (today) bucket.
        assert report["decisions_per_day"][-1]["n"] == 3


def _make_age_dbs(tmp: str, *, young: int = 0, old: int = 0) -> tuple[str, str]:
    """DBs with trades at controlled ages relative to now.

    ``young`` trades sit 2 days back, ``old`` 60 days back — so a
    ``--since-days 7`` window must keep only the young ones (plus their
    shadow decisions).
    """
    import time as _time

    live = os.path.join(tmp, "bot.db")
    research = os.path.join(tmp, "hyperliquid.db")
    db = sqlite3.connect(live)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, "
               "entry_time INTEGER, exit_time INTEGER, pnl_usd REAL, pnl_pct REAL, "
               "strategy TEXT, status TEXT, exit_reason TEXT)")
    rdb = sqlite3.connect(research)
    rdb.execute("CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, symbol TEXT, "
                "strategy TEXT, variant TEXT, side TEXT, would_enter INTEGER, "
                "reason TEXT, timestamp_ms INTEGER, snapshot_json TEXT, "
                "ingested_at_ms INTEGER)")
    now = int(_time.time() * 1000)
    tid = 1
    did = 1
    for _ in range(young):
        ts = now - 2 * 86_400_000
        db.execute("INSERT INTO trades VALUES (?, 'BTC', 'long', ?, ?, 5.0, 0.01, "
                   "'VWAPDeviation', 'closed', 'tp')", (tid, ts, ts + 3_600_000))
        rdb.execute(
            "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
            "'iv_gate_shadow', 'long', 1, 'iv_gate:high_iv', ?, ?, 0)",
            (did, ts - 100,
             json.dumps({"metadata": {"iv_class": "high_iv", "iv_percentile": 75.0,
                                      "iv_threshold": 66.7, "iv_currency": "BTC"}})),
        )
        tid += 1
        did += 1
    for _ in range(old):
        ts = now - 60 * 86_400_000
        db.execute("INSERT INTO trades VALUES (?, 'BTC', 'long', ?, ?, -3.0, -0.01, "
                   "'VWAPDeviation', 'closed', 'tp')", (tid, ts, ts + 3_600_000))
        rdb.execute(
            "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
            "'iv_gate_shadow', 'long', 1, 'iv_gate:low_iv', ?, ?, 0)",
            (did, ts - 100,
             json.dumps({"metadata": {"iv_class": "low_iv", "iv_percentile": 40.0,
                                      "iv_threshold": 66.7, "iv_currency": "BTC"}})),
        )
        tid += 1
        did += 1
    db.commit(); db.close()
    rdb.commit(); rdb.close()
    return live, research


def test_since_days_filters_to_recent_window() -> None:
    """--since-days 7 keeps only young trades+decisions; the old sample
    (already evaluated by the recheck) drops out of the window."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_age_dbs(tmp, young=2, old=3)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", live, "--research-db", research,
             "--since-days", "7"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "janela:       últimos 7 dias" in r.stdout
        # Only the 2 young trades survive the window.
        assert "decisões iv_gate_shadow: 2 | trades: 2" in r.stdout
        assert "high_iv | 2 | 2 | 0 | 100% | +10.00" in r.stdout
        assert "low_iv | 0 | 0 | 0" in r.stdout


def test_full_run_prints_window_comparison_table() -> None:
    """Without --since-days the CLI prints the 7d/30d/total window table so
    recent accumulation is distinguishable from the already-evaluated sample."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_age_dbs(tmp, young=2, old=3)
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", live, "--research-db", research],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[3b] Janelas" in r.stdout
        assert "| total |  5 |  5 |" in r.stdout
        assert "| 30d   |  2 |  2 |" in r.stdout
        assert "| 7d    |  2 |  2 |" in r.stdout


def test_build_report_carries_window_fields() -> None:
    """build_report exposes since_days + window_start_ms for JSON consumers."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_age_dbs(tmp, young=1, old=1)
        report = pnl.build_report(live_db=live, research_db=research, since_days=7)
        assert report["since_days"] == 7
        assert report["window_start_ms"] is not None
        assert report["n_trades"] == 1  # only the young trade
        full = pnl.build_report(live_db=live, research_db=research)
        assert full["since_days"] is None
        assert full["window_start_ms"] is None
        assert full["n_trades"] == 2


def test_nearest_decision_within_tolerance_wins() -> None:
    """Two decisions 10s apart on the same key: the trade joins the nearest
    (100ms) one, not the farther one."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_dbs(tmp, n_low=0, n_high=0, n_unknown=0)
        db = sqlite3.connect(live)
        rdb = sqlite3.connect(research)
        ts = 1_786_600_000_000
        db.execute(
            "INSERT INTO trades VALUES (1, 'BTC', 'long', ?, ?, 5.0, 0.01, "
            "'VWAPDeviation', 'closed', 'tp')",
            (ts, ts + 3_600_000),
        )
        # Far decision (9s before) vs near decision (100ms before).
        for d_id, off, cls in ((1, 9_000, "high_iv"), (2, 100, "low_iv")):
            rdb.execute(
                "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
                "'iv_gate_shadow', 'long', 1, ?, ?, ?, 0)",
                (d_id, f"iv_gate:{cls}", ts - off,
                 json.dumps({"metadata": {"iv_class": cls, "iv_percentile": 1.0,
                                          "iv_threshold": 66.7, "iv_currency": "BTC"}})),
            )
        db.commit(); db.close(); rdb.commit(); rdb.close()
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", live, "--research-db", research],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        # Nearest (100ms) decision is low_iv → trade joins low_iv, not high_iv.
        assert "low_iv | 1 | 1 | 0" in r.stdout
        assert "high_iv | 0 | 0 | 0" in r.stdout
        assert "decisões consumidas: 1/2" in r.stdout


def test_verdict_inconclusive_below_gate() -> None:
    r = _make([], n_high=2, n_low=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "INCONCLUSIVO" in r.stdout or "INCONCLUSIVE" in r.stdout
    assert "< 30" in r.stdout


def test_verdict_consistent_at_gate() -> None:
    """high_iv net > 0 and low_iv net < 0 with n>=30 closed => CONSISTENTE,
    the same direction as the backtest (+42.99 USD, high_iv-only)."""
    r = _make([], n_high=20, n_low=20)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CONSISTENTE" in r.stdout
    assert "high_iv positivo e low_iv não-positivo" in r.stdout


def test_verdict_contradicts_when_high_iv_loses() -> None:
    r = _make([], n_high=20, n_low=20, pnl_high=-5.0, pnl_low=10.0)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CONTRADIZ" in r.stdout


def test_json_output_written() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        live, research = _make_dbs(tmp, n_high=1, n_low=1)
        jp = os.path.join(tmp, "report.json")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--live-db", live, "--research-db", research,
             "--json", jp],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )
        assert r.returncode == 0, r.stdout + r.stderr
        data = json.loads(Path(jp).read_text(encoding="utf-8"))
        assert data["slices"]["high_iv"]["n"] == 1
        assert data["slices"]["low_iv"]["n_closed"] == 1
        assert data["n_decisions"] == 2
        assert data["verdict"]["status"] == "INCONCLUSIVE"
        assert data["backtest_evidence"]["net_high_iv_only_usd"] == 42.99



