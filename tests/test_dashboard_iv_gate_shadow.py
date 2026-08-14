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

    def test_pnl_fields_per_class(self) -> None:
        """The endpoint exposes the accumulated PnL of each IV slice — the
        live-vs-backtest evidence the gate will be decided on."""
        d = self._get().get_json()
        hi = d["by_class"]["high_iv"]
        lo = d["by_class"]["low_iv"]
        assert hi["net_pnl_usd"] is not None
        assert lo["net_pnl_usd"] is not None
        assert 0 <= hi["win_rate"] <= 1
        assert hi["avg_pnl_usd"] is not None
        assert hi["median_pnl_usd"] is not None
        assert hi["best_usd"] is not None
        assert hi["worst_usd"] is not None
        # All 5 matched trades are closed in this fixture — every slice PnL is
        # a real number, and the high/low sum covers exactly the 5 matched.
        assert hi["n_closed"] == 2
        assert lo["n_closed"] == 3
        # Join coverage: 5/5 decisions matched, 5/6 trades carry a class.
        assert d["join_coverage_pct"] == pytest.approx(100.0)
        assert d["trade_coverage_pct"] == pytest.approx(100.0 * 5 / 6, abs=0.1)
        assert d["verdict"]["status"] in (
            "INCONCLUSIVE", "CONSISTENTE", "CONSISTENTE (parcial)",
            "CONTRADIZ", "EMPATE",
        )

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

    def test_distribution_by_strategy_and_symbol(self, tmp_path) -> None:
        """The endpoint exposes where the sample lives: per strategy and per
        symbol, with the per-class mix and aggregate n/closed."""
        from scripts import iv_gate_shadow_vs_pnl as pnl

        live = os.path.join(tmp_path, "live.db")
        research = os.path.join(tmp_path, "research.db")

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
        # BTC + VWAPDeviation (high_iv) and ETH + VolatilityBreakout (low_iv).
        rows = [
            ("BTC", "VWAPDeviation", "high_iv", 80.0, t0, 5.0),
            ("ETH", "VolatilityBreakout", "low_iv", 40.0, t0 + 60_000, -3.0),
        ]
        for i, (sym, strat, cls, pct, ts, pnl_) in enumerate(rows, start=1):
            db.execute(
                "INSERT INTO trades VALUES (?, ?, 'long', ?, ?, ?, 0.01, "
                "?, 'closed', 'tp')",
                (i, sym, ts, ts + 3_600_000, pnl_, strat),
            )
            snap = json.dumps({"metadata": {"iv_class": cls, "iv_percentile": pct,
                                            "iv_threshold": 66.7, "iv_currency": sym}})
            rdb.execute(
                "INSERT INTO shadow_decisions VALUES (?, ?, ?, "
                "'iv_gate_shadow', 'long', 1, ?, ?, ?, 0)",
                (i, sym, strat, f"iv_gate:{cls}", ts, snap),
            )
        db.commit(); db.close()
        rdb.commit(); rdb.close()

        def _resolve(live_db=None, research_db=None):
            return Path(live), Path(research)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/iv_gate_shadow")
        d = r.get_json()

        by_strat = d["by_strategy"]
        assert set(by_strat) == {"VWAPDeviation", "VolatilityBreakout"}
        vw = by_strat["VWAPDeviation"]
        assert vw["n"] == 1 and vw["n_closed"] == 1
        assert vw["classes"]["high_iv"]["net_pnl_usd"] == 5.0
        assert vw["classes"]["low_iv"]["n"] == 0
        vb = by_strat["VolatilityBreakout"]
        assert vb["classes"]["low_iv"]["net_pnl_usd"] == -3.0

        by_sym = d["by_symbol"]
        assert set(by_sym) == {"BTC", "ETH"}
        assert by_sym["BTC"]["classes"]["high_iv"]["n"] == 1
        assert by_sym["ETH"]["classes"]["low_iv"]["n"] == 1
        assert by_sym["BTC"]["n_closed"] == 1
        assert by_sym["ETH"]["n_closed"] == 1


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

    def test_iv_gate_is_on_slow_research_poll_not_fast_poll(self) -> None:
        """IV/watchdog/DVOL joins must not ride the 5s ops poll."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        start = html.find("pollAll = function pollAll()")
        mid = html.find("pollResearch = function pollResearch()")
        assert start != -1 and mid != -1 and mid > start
        poll_all = html[start:mid]
        assert "/api/iv_gate_shadow" not in poll_all
        assert "/api/research_watchdogs" not in poll_all
        assert "/api/dvol" not in poll_all
        assert "/api/top_traders" not in poll_all
        assert "/api/shadow_panel" not in poll_all
        assert "/api/market_data_health" in poll_all
        assert "setInterval(pollResearch, 60000)" in html
        assert 'id="tt-bias-tbody"' in html
        assert "Top Traders (aggregate)" in html

    def test_panel_shows_pnl_and_join_coverage(self) -> None:
        """The panel renders the accumulated PnL columns (Net PnL / WR / Avg)
        and the join-coverage bars (decisões matched / trades com decisão)."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "Net PnL" in html
        assert "WR" in html
        assert "Cobertura do join" in html
        assert "join_coverage_pct" in html
        assert "trade_coverage_pct" in html
        assert "verdict" in html
        assert 'id="ivshadow-coverage"' in html

    def test_panel_renders_decision_rate_sparkline(self) -> None:
        """The panel shows the decision-count-per-day sparkline (sample
        accumulation rate) fed by decisions_per_day from the endpoint."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="ivshadow-rate-svg"' in html
        assert 'id="ivshadow-rate-sum"' in html
        assert "Decisões por dia" in html
        assert "decisions_per_day" in html
        assert "taxa de acumulação" in html

    def test_panel_renders_strategy_and_symbol_distribution(self) -> None:
        """The panel shows where the sample lives: per-strategy and per-symbol
        tables with the high/low/unknown mix and net PnL."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="ivshadow-strat-tbody"' in html
        assert 'id="ivshadow-sym-tbody"' in html
        assert "Por estratégia" in html
        assert "Por símbolo" in html
        assert "high/low/unk" in html
        assert "payload.by_strategy" in html
        assert "payload.by_symbol" in html

    def test_trades_table_has_iv_columns(self) -> None:
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "IV pct" in html
        assert "iv_percentile" in html
        assert "ivBadge(t.iv_class)" in html
        assert 'function ivBadge' in html
        assert 'colspan="14"' in html

    def test_trades_tooltip_shows_exact_percentile_and_threshold(self) -> None:
        """Each trade row's tooltip carries the exact IV percentile and the
        classification threshold (66.7), not just the class badge."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert "ivTip" in html
        # The exact-percentile + threshold formatting now lives in the shared
        # ivTipTxt helper (used by trades AND positions rows).
        assert "IV ${pct.toFixed(1)}% (threshold ${thr})" in html
        assert 'const thr = threshold != null ? threshold : 66.7;' in html
        assert '${cls || "unknown"}' in html
        assert "ivTipTxt(t.iv_percentile, t.iv_threshold, t.iv_class)" in html
        assert 'title="Funding paid: $${fund.toFixed(4)} · ${ivTip}"' in html

    def test_trades_log_has_iv_class_filter(self) -> None:
        """The trades log can isolate high_iv / low_iv / undecided rows."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        assert 'id="trades-iv-filter"' in html
        assert 'value="high_iv"' in html
        assert 'value="low_iv"' in html
        assert 'value="unknown"' in html
        assert 'onchange="setTradesIvFilter(this.value)"' in html
        assert "_tradesIvFilter" in html
        assert '(t.iv_class || "unknown") === _tradesIvFilter' in html

    def test_positions_table_has_iv_columns(self) -> None:
        """The open-positions panel shows the same IV columns (percentile +
        class) as the trades log."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Positions header + render use the same IV cells as trades.
        assert 'id="positions-tbody"' in html
        assert "p.iv_percentile != null" in html
        assert "ivBadge(p.iv_class)" in html
        assert 'colspan="12"' in html
        assert html.count("IV pct") >= 2  # trades + positions headers

    def test_iv_tooltip_shows_delta_to_threshold(self) -> None:
        """Trade and position tooltips show the classification slack: the
        percentile's distance to the threshold (e.g. +5.3 acima de 66.7) so
        the operator sees at a glance how close the class is to flipping."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Shared helper: delta = pct - threshold, signed, with direction.
        assert 'function ivDeltaTxt(pct, threshold)' in html
        assert 'const d = pct - threshold;' in html
        assert 'd >= 0 ? "acima de" : "abaixo de"' in html
        assert '"Δ " + (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(1)' in html
        # Shared tooltip builder used by BOTH trades and positions rows.
        assert 'function ivTipTxt(pct, threshold, cls)' in html
        assert 'ivTipTxt(t.iv_percentile, t.iv_threshold, t.iv_class)' in html
        assert 'ivTipTxt(p.iv_percentile, p.iv_threshold, p.iv_class)' in html
        # Positions rows now carry the tooltip (they had none before).
        assert '<tr title="${ivTip}">' in html

    def test_trades_panel_has_liquidation_stop_out_counter_and_badge(self) -> None:
        """The trades panel surfaces liquidation stop-outs: a counter in the
        header and a distinct badge on the exit_reason cell."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Counter element in the panel header.
        assert 'id="trades-liq-count"' in html
        # Counter updated on every render (socket + REST share renderTrades).
        assert 't.exit_reason === "liquidation_stop_out"' in html
        assert '⛔ ${liqCount} liquidation stop-out' in html
        # Distinct badge on the Reason cell for stop-out trades.
        assert 'class="pill pill-liq"' in html
        assert 'LIQ STOP-OUT' in html
        assert '${liqBadge}' in html

    def test_trades_panel_header_shows_active_filter_n_and_net(self) -> None:
        """The Trade History header shows n + net PnL of the ACTIVE IV filter
        (all / high_iv / low_iv / sem decisão) — performance by class without
        opening the DB."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Element in the header, next to the liquidation counter.
        assert 'id="trades-filter-stats"' in html
        assert 'n e net PnL dos trades do filtro IV activo (fechados)' in html
        # Stats computed from the same filtered array the table renders —
        # closed-only net, open count shown as a suffix.
        assert 'filtered.filter(t => t.status === "closed" && typeof t.pnl_usd === "number")' in html
        assert 'closed.reduce((a, t) => a + t.pnl_usd, 0)' in html
        assert '${label}:</span>' in html
        assert '${filtered.length}</b> trades' in html
        assert 'net <b class="${cls}">' in html
        assert '(${filtered.length - closed.length} open)' in html
        # The filter label is humanised (high_iv -> high iv).
        assert '_tradesIvFilter.replace("_", " ")' in html

    def test_positions_panel_mirrors_iv_class_filter(self) -> None:
        """The open-positions panel (already carrying the IV columns) mirrors
        the same IV-class filter: one shared state, both selects in sync,
        and renderPositions filters with the same rule as renderTrades."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Select in the Open Positions header, wired to its own setter.
        assert 'id="positions-iv-filter"' in html
        assert 'onchange="setPositionsIvFilter(this.value)"' in html
        assert 'value="high_iv"' in html
        assert 'value="low_iv"' in html
        # Shared state + mirror: changing positions updates the trades select.
        assert "let _positionsAll = [];" in html
        assert 'function setPositionsIvFilter(v)' in html
        assert 'document.getElementById("trades-iv-filter")' in html
        # renderPositions filters with the SAME rule as renderTrades.
        assert '(p.iv_class || "unknown") === _tradesIvFilter' in html
        assert 'No ${_tradesIvFilter.replace("_", " ")} positions' in html
        # Reverse mirror: trades filter + gate toggle sync the positions select.
        assert 'document.getElementById("positions-iv-filter")' in html
        assert 'posSel.value = v' in html
        assert 'posSel2.value = cls' in html

    def test_trades_gate_toggle_links_filter_to_projection(self) -> None:
        """The gate toggle pre-selects the IV class the projected decision
        condemns: PROMOTE → low_iv (enforcement would block it), REJECT →
        high_iv (the slice that fails to confirm). Live-follows the
        projection on every watchdog refresh; N/A refuses to engage."""
        html = (ROOT / "src" / "dashboard" / "templates" / "index.html").read_text(
            encoding="utf-8"
        )
        # Toggle in the Trade History header, wired to the filter.
        assert 'id="trades-gate-toggle"' in html
        assert 'onchange="setGateFilter(this.checked)"' in html
        assert "gate\n" in html or "> gate<" in html
        # Projection state + mapping (PROMOTE → low_iv, REJECT → high_iv).
        assert "let _ivProjection = null;" in html
        assert '_ivProjection === "REJECT" ? "high_iv" : "low_iv"' in html
        # Manual filter overrides the toggle (checkbox turns off).
        assert 'if (gate && gate.checked && v !== "all") gate.checked = false;' in html
        # N/A projection refuses the toggle (no class to pre-select).
        assert '_ivProjection !== "PROMOTE" && _ivProjection !== "REJECT"' in html
        # Live follow on watchdog refresh + reset to all when projection N/A.
        assert 'gateChk && gateChk.checked' in html
        assert '_ivProjection = (w.projected && w.projected.status !== "N/A") ? w.projected.status : null;' in html


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
        # The recorded decision threshold rides along on matched rows.
        assert all(t["iv_threshold"] == 66.7 for t in high + low)
        assert unknown[0].get("iv_threshold") is None

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


class TestPositionsEndpointIvEnrichment:
    """/api/positions enriches open positions with the shadow IV decision —
    same join and columns as the trades log."""

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

    def test_positions_carry_iv_class_and_percentile(self) -> None:
        """A live position (same key as its opening trade) joins the shadow
        decision and carries iv_class / iv_percentile like the trades log."""
        from src.data.database import Database

        self._db = Database(self._live)
        pos = type(
            "Pos",
            (),
            {
                "entry_price": 80_000.0,
                "size": 0.1,
                "side": "long",
                "entry_time_ms": 1_786_600_000_000,
                "stop_loss_price": None,
                "take_profit_price": None,
                "metadata": {"strategy": "VWAPDeviation"},
            },
        )()
        port = type(
            "Port",
            (),
            {"get_dashboard_snapshot_sync": staticmethod(
                lambda: type("Snap", (), {"positions": {"BTC": pos}})()
            )},
        )()
        self._web._engine = type("E", (), {"_db": self._db, "portfolio": port,
                                          "get_mark_prices_sync": staticmethod(lambda: {"BTC": 81_000.0})})()
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(self._research)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/positions")
        assert r.status_code == 200
        rows = r.get_json()
        assert len(rows) == 1
        p = rows[0]
        assert p["symbol"] == "BTC"
        assert p["strategy"] == "VWAPDeviation"
        # Entry at t0+0s = the first decision (high_iv, 80.0) — joined within
        # the 60s tolerance, exactly like the trades log.
        assert p["iv_class"] == "high_iv"
        assert p["iv_percentile"] == 80.0

    def test_positions_enrichment_best_effort_no_crash(self) -> None:
        """A broken research DB leaves positions served, just unenriched."""
        from src.data.database import Database

        self._db = Database(self._live)
        pos = type(
            "Pos",
            (),
            {
                "entry_price": 80_000.0,
                "size": 0.1,
                "side": "long",
                "entry_time_ms": 1_786_600_000_000,
                "stop_loss_price": None,
                "take_profit_price": None,
                "metadata": {"strategy": "VWAPDeviation"},
            },
        )()
        port = type(
            "Port",
            (),
            {"get_dashboard_snapshot_sync": staticmethod(
                lambda: type("Snap", (), {"positions": {"BTC": pos}})()
            )},
        )()
        self._web._engine = type("E", (), {"_db": self._db, "portfolio": port,
                                          "get_mark_prices_sync": staticmethod(lambda: {"BTC": 81_000.0})})()
        empty = os.path.join(self._tmp.name, "empty3.db")
        sqlite3.connect(empty).close()
        from scripts import iv_gate_shadow_vs_pnl as pnl

        def _resolve(live_db=None, research_db=None):
            return Path(self._live), Path(empty)

        with patch.object(pnl, "resolve_db_paths", _resolve):
            r = self._client.get("/api/positions")
        assert r.status_code == 200
        rows = r.get_json()
        assert len(rows) == 1  # still served
        assert rows[0].get("iv_class") in (None, "unknown")
        assert rows[0].get("iv_percentile") is None
