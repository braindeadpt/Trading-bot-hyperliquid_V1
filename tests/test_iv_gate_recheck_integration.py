"""Integration test — the IV gate supervisor at the n=30 evidence gate.

Simulates the full shadow→enforcement decision path the way the watchdog
runs it in production: real SQLite DBs (synthetic but with the exact
schema the join reads), the real ``check_iv_gate`` from the supervisor,
the real join/slices from ``iv_gate_shadow_vs_pnl``, and the real
``write_report`` — only the DB paths and the report/state destinations
are redirected to a temp dir, so nothing in the repo is touched.

Pins the END-TO-END contract:

  * at n=30 closed with an IV decision (20 high_iv + 10 low_iv) the gate
    fires, the verdict is PROMOTE, the run is persisted as triggered, and
    the complete markdown report is written to ``docs/`` (here: temp)
    with the trigger line, the slices table, the decision and the
    backtest reference;
  * a sample that contradicts the backtest direction writes a REJECT
    report (never silently enforces);
  * below the gate nothing fires and no report is written.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import pytest

pytestmark = pytest.mark.integration_offline

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import iv_gate_shadow_recheck as rc  # noqa: E402
from scripts import iv_gate_shadow_vs_pnl as pnl  # noqa: E402
from scripts import research_watchdog_supervisor as sup  # noqa: E402


def _make_dbs(tmp: Path, *, n_high: int, n_low: int, pnl_high: float,
              pnl_low: float) -> tuple[Path, Path]:
    """Synthetic live + research DBs with the exact schema the join reads.

    Trades at ``T0 + i*60s``; each ``iv_gate_shadow`` decision sits 100ms
    before its trade's entry_time, so the join always resolves within the
    default 60s tolerance. Every trade is closed (real PnL).
    """
    live = tmp / "bot.db"
    research = tmp / "hyperliquid.db"

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

    def add_trade(pnl: float, cls: str) -> None:
        nonlocal i, trade_id, decision_id
        ts = t0 + i * 60_000
        i += 1
        db.execute(
            "INSERT INTO trades VALUES (?, 'BTC', 'long', ?, ?, ?, ?, "
            "'VWAPDeviation', 'closed', 'tp')",
            (trade_id, ts, ts + 3_600_000, pnl, pnl),
        )
        trade_id += 1
        meta = {"iv_class": cls, "iv_percentile": 75.0 if cls == "high_iv" else 40.0,
                "iv_threshold": 66.7, "iv_currency": "BTC"}
        rdb.execute(
            "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
            "'iv_gate_shadow', 'long', 1, ?, ?, ?, 0)",
            (decision_id, f"iv_gate:{cls}", ts - 100, json.dumps({"metadata": meta})),
        )
        decision_id += 1

    for _ in range(n_high):
        add_trade(pnl_high, "high_iv")
    for _ in range(n_low):
        add_trade(pnl_low, "low_iv")

    db.commit()
    db.close()
    rdb.commit()
    rdb.close()
    return live, research


class _Harness:
    """Point the real supervisor/recheck at temp DBs + temp report/state.

    Only the destinations are redirected; every computation (join, slices,
    verdict, report text) is the real production code path.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.docs_dir = tmp_path / "docs"
        self.docs_dir.mkdir(exist_ok=True)
        self.report = self.docs_dir / "IV_GATE_SHADOW_RECHECK_RESULT.md"
        self.state = tmp_path / "research_watchdogs_state.json"

    def __enter__(self) -> "_Harness":
        self._orig_resolve_pnl = pnl.resolve_db_paths
        self._orig_resolve_rc = rc.resolve_db_paths
        self._orig_report_rc = rc.REPORT_PATH
        self._orig_report_sup = sup.IV_REPORT_PATH
        self._orig_state = sup.STATE_PATH

        pnl.resolve_db_paths = lambda live_db=None, research_db=None: (self.live, self.research)
        rc.resolve_db_paths = lambda live_db=None, research_db=None: (self.live, self.research)
        rc.REPORT_PATH = self.report
        sup.IV_REPORT_PATH = self.report
        sup.STATE_PATH = self.state
        return self

    def __exit__(self, *exc) -> None:
        pnl.resolve_db_paths = self._orig_resolve_pnl
        rc.resolve_db_paths = self._orig_resolve_rc
        rc.REPORT_PATH = self._orig_report_rc
        sup.IV_REPORT_PATH = self._orig_report_sup
        sup.STATE_PATH = self._orig_state

    def seed(self, *, n_high: int, n_low: int, pnl_high: float, pnl_low: float) -> None:
        self.live, self.research = _make_dbs(
            self.tmp, n_high=n_high, n_low=n_low,
            pnl_high=pnl_high, pnl_low=pnl_low,
        )


def test_promote_writes_complete_report_at_gate(tmp_path) -> None:
    """n=30 closed (20 high_iv + 10 low_iv), high_iv wins: the gate fires,
    the verdict is PROMOTE and the full markdown report lands in docs/."""
    with _Harness(tmp_path) as h:
        h.seed(n_high=20, n_low=10, pnl_high=50.0, pnl_low=-30.0)
        shared = sup.fresh_state()

        ran = sup.check_iv_gate(shared, force=False)

        assert ran is True
        assert shared["iv_gate_shadow"]["triggered"] is True
        run = shared["iv_gate_shadow"]["runs"][-1]
        assert run["verdict"] == "PROMOTE"
        assert run["n_closed"] == 30
        assert run["n_high_closed"] == 20
        assert run["n_low_closed"] == 10
        assert str(h.report) in run["report_path"]

        # The report is written and complete.
        assert h.report.exists()
        text = h.report.read_text(encoding="utf-8")
        assert "# IV Gate Shadow Recheck — shadow vs enforcement" in text
        assert "Trigger: n=30 closed trades com decisão IV (gate 30)" in text
        assert "high_iv=20 · low_iv=10" in text
        assert "threshold IV = 66.7" in text
        # Slices table with the real numbers.
        assert "| high_iv | 20 | 20 | 0 | 100% | +1000.00 |" in text
        assert "| low_iv | 10 | 10 | 0 | 0% | -300.00 |" in text
        assert "| unknown | 0 | 0 | 0 |" in text
        # Decision + backtest reference.
        assert "## Decisão" in text
        assert "**PROMOTE**" in text
        assert "## Referência" in text
        assert "Backtest high_iv-only" in text
        assert "shadow-only" in text


def test_reject_writes_report_never_enforces(tmp_path) -> None:
    """n=30 closed but the sample contradicts the backtest: the gate fires
    and writes a REJECT report — the router stays shadow."""
    with _Harness(tmp_path) as h:
        h.seed(n_high=20, n_low=10, pnl_high=-20.0, pnl_low=40.0)
        shared = sup.fresh_state()

        ran = sup.check_iv_gate(shared, force=False)

        assert ran is True
        assert shared["iv_gate_shadow"]["runs"][-1]["verdict"] == "REJECT"
        assert h.report.exists()
        text = h.report.read_text(encoding="utf-8")
        assert "**REJECT**" in text
        assert "manter shadow" in text
        assert "PROMOTE" not in text.replace("**PROMOTE**", "")


def test_below_gate_fires_nothing_and_writes_no_report(tmp_path) -> None:
    """n < 30: the gate does not fire, the run is not persisted and no
    report is written — the panel keeps accumulating."""
    with _Harness(tmp_path) as h:
        h.seed(n_high=10, n_low=5, pnl_high=50.0, pnl_low=-30.0)
        shared = sup.fresh_state()

        ran = sup.check_iv_gate(shared, force=False)

        assert ran is False
        assert shared["iv_gate_shadow"]["triggered"] is False
        assert shared["iv_gate_shadow"]["runs"] == []
        assert not h.report.exists()


def test_state_roundtrip_prevents_refire_after_report(tmp_path) -> None:
    """After a PROMOTE run, a fresh supervisor load sees triggered=True and
    never re-fires (watch-only) — the report is written exactly once."""
    with _Harness(tmp_path) as h:
        h.seed(n_high=20, n_low=10, pnl_high=50.0, pnl_low=-30.0)
        shared = sup.fresh_state()
        assert sup.check_iv_gate(shared, force=False) is True

        # Simulate a restart: reload from the persisted shared state.
        reloaded = sup.load_shared_state()
        assert reloaded["iv_gate_shadow"]["triggered"] is True
        assert reloaded["iv_gate_shadow"]["runs"][-1]["verdict"] == "PROMOTE"
        # Watch-only: no new run, report unchanged (still exactly one write).
        before = h.report.read_text(encoding="utf-8")
        assert sup.check_iv_gate(reloaded, force=False) is False
        assert len(reloaded["iv_gate_shadow"]["runs"]) == 1
        assert h.report.read_text(encoding="utf-8") == before
