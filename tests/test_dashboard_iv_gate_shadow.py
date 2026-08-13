"""Tests for the dashboard IV gate shadow sample panel (``/api/iv_gate_shadow``).

Shows the growing shadow sample: n per class (high_iv / low_iv / unknown),
closed vs open, and the average recorded IV percentile per class. The endpoint
reuses the exact join/slices from ``scripts/iv_gate_shadow_vs_pnl.py`` (the
single source of truth) so the dashboard and the recheck watchdog can never
disagree about what counts as a matched IV decision.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_research_db(path: str) -> None:
    """Minimal research DB with iv_gate_shadow rows (metadata iv_class/pct)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, symbol TEXT, "
               "strategy TEXT, variant TEXT, side TEXT, would_enter INTEGER, "
               "reason TEXT, timestamp_ms INTEGER, snapshot_json TEXT, "
               "ingested_at_ms INTEGER)")
    rows = [
        # 2 high_iv, 3 low_iv — decision ts 100ms before entry_time (join window).
        ("high_iv", 80.0, 1_786_600_000_000),
        ("high_iv", 72.0, 1_786_600_060_000),
        ("low_iv", 40.0, 1_786_600_120_000),
        ("low_iv", 55.0, 1_786_600_180_000),
        ("low_iv", 30.0, 1_786_600_240_000),
    ]
    for i, (cls, pct, ts) in enumerate(rows, start=1):
        snap = json.dumps({"metadata": {"iv_class": cls, "iv_percentile": pct,
                                        "iv_threshold": 66.7, "iv_currency": "BTC"}})
        db.execute(
            "INSERT INTO shadow_decisions VALUES (?, 'BTC', 'VWAPDeviation', "
            "'iv_gate_shadow', 'long', 1, ?, ?, ?, 0)",
            (i, f"iv_gate:{cls}", ts, snap),
        )
    db.commit()
    db.close()


def _make_live_db(path: str, n: int = 6) -> None:
    """Live DB with closed trades at entry_time = decision ts (join resolves)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, "
               "entry_time INTEGER, exit_time INTEGER, pnl_usd REAL, pnl_pct REAL, "
               "strategy TEXT, status TEXT, exit_reason TEXT)")
    for i in range(n):
        ts = 1_786_600_000_000 + i * 60_000
        db.execute(
            "INSERT INTO trades VALUES (?, 'BTC', 'long', ?, ?, 5.0, 0.01, "
            "'VWAPDeviation', 'closed', 'tp')",
            (i + 1, ts, ts + 3_600_000),
        )
    db.commit()
    db.close()


class TestIvGateShadowEndpoint:
    pytestmark = pytest.mark.integration_offline

    def setup_method(self) -> None:
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self._tmp = tempfile.TemporaryDirectory()
        self._research = os.path.join(self._tmp.name, "research.db")
        self._live = os.path.join(self._tmp.name, "bot.db")
        _make_research_db(self._research)
        _make_live_db(self._live, n=6)
        self._app, self._sio, _ = web.create_app({"mode": "paper"})
        self._client = self._app.test_client()

    def teardown_method(self) -> None:
        self._web._engine = self._orig_engine
        self._tmp.cleanup()

    def _get(self):
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(self._research)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            return self._client.get("/api/iv_gate_shadow")

    def test_shape_and_class_distribution(self) -> None:
        r = self._get()
        assert r.status_code == 200
        d = r.get_json()
        assert "error" not in d
        by = d["by_class"]
        assert by["high_iv"]["n"] == 2
        assert by["high_iv"]["n_closed"] == 2
        assert by["low_iv"]["n"] == 3
        assert by["low_iv"]["n_closed"] == 3
        assert by["unknown"]["n"] == 1  # 6 trades - 5 matched
        # avg percentile per class from the recorded decisions
        assert by["high_iv"]["avg_pct"] == pytest.approx(76.0)
        assert by["low_iv"]["avg_pct"] == pytest.approx(41.67, abs=0.01)
        assert d["threshold"] == 66.7
        assert d["target_closed"] == 30
        assert d["trades_with_decision"] == 5
        assert d["total"] == 6
        assert d["n_decisions"] == 5
        assert d["matched"] == 5

    def test_gate_progress_fields(self) -> None:
        d = self._get().get_json()
        # closed with decision = 2 high + 3 low = 5, target 30
        assert d["target_closed"] == 30

    def test_empty_research_db(self) -> None:
        empty = os.path.join(self._tmp.name, "empty.db")
        sqlite3.connect(empty).close()
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(empty)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/iv_gate_shadow")
        assert r.status_code == 200
        d = r.get_json()
        assert "error" not in d
        # No decisions: high/low empty, all trades fall into unknown.
        assert d["by_class"]["high_iv"]["n"] == 0
        assert d["by_class"]["low_iv"]["n"] == 0
        assert d["by_class"]["unknown"]["n"] == 6
        assert d["trades_with_decision"] == 0
        assert d["n_decisions"] == 0


class TestIvGateShadowTemplate:
    """The panel markup + JS wiring are present in the dashboard template."""

    pytestmark = pytest.mark.unit

    def test_panel_markup_present(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="ivshadow-tbody"' in html
        assert 'id="ivshadow-bar-fill"' in html
        assert "Distribuição high/low-IV dos trades shadow" in html
        assert "iv_gate_shadow" in html

    def test_render_function_and_poll_wiring(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "function renderIvGateShadow" in html
        assert '"/api/iv_gate_shadow"' in html
        assert "renderIvGateShadow(d)" in html

    def test_trades_table_has_iv_columns(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "IV pct" in html
        assert "iv_percentile" in html
        assert "ivBadge(t.iv_class)" in html
        assert 'function ivBadge' in html
        assert 'colspan="14"' in html


class TestTradesEndpointIvEnrichment:
    """/api/trades enriches executed trades with the shadow IV decision."""

    pytestmark = pytest.mark.integration_offline

    def setup_method(self) -> None:
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        self._tmp = tempfile.TemporaryDirectory()
        self._research = os.path.join(self._tmp.name, "research.db")
        self._live = os.path.join(self._tmp.name, "bot.db")
        _make_research_db(self._research)
        _make_live_db(self._live, n=6)
        self._app, self._sio, _ = web.create_app({"mode": "paper"})
        self._client = self._app.test_client()

    def teardown_method(self) -> None:
        self._web._engine = self._orig_engine
        if getattr(self, "_db", None) is not None:
            self._db.close()
        self._tmp.cleanup()

    def test_trades_carry_iv_class_and_percentile(self) -> None:
        from src.data.database import Database

        self._db = Database(self._live)
        self._web._engine = type("E", (), {"_db": self._db})()
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(self._research)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/trades")
        assert r.status_code == 200
        rows = r.get_json()
        assert len(rows) == 6
        # 5 trades matched a decision (2 high_iv + 3 low_iv), 1 unmatched
        # (join marks unmatched as "unknown", no percentile).
        high = [t for t in rows if t["iv_class"] == "high_iv"]
        low = [t for t in rows if t["iv_class"] == "low_iv"]
        unknown = [t for t in rows if t["iv_class"] == "unknown"]
        assert len(high) == 2
        assert len(low) == 3
        assert len(unknown) == 1
        assert all(t["iv_percentile"] is not None for t in high + low)
        assert high[0]["iv_percentile"] in (72.0, 80.0)
        assert unknown[0].get("iv_percentile") is None

    def test_enrichment_best_effort_no_crash(self) -> None:
        """A broken research DB must not take the trades list down."""
        from src.data.database import Database

        self._db = Database(self._live)
        self._web._engine = type("E", (), {"_db": self._db})()
        empty = os.path.join(self._tmp.name, "empty2.db")
        sqlite3.connect(empty).close()
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(empty)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/trades")
        assert r.status_code == 200
        rows = r.get_json()
        assert len(rows) == 6  # still served, just unenriched
        assert all(t.get("iv_class") in (None, "unknown") for t in rows)
