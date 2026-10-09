"""Tests for the 2026-10 dashboard redesign.

Main page answers one question — "are we closer to a positive-net-PnL
strategy?" — and a separate ``/ops`` page hosts the operational panels.
Covers:

* ``/api/hypotheses`` — sealed confirmation boards expose counts only;
  the endpoint itself must never emit metric fields for a sealed board.
* ``/api/execution_pnl`` — gross/fees/slippage/funding/net decomposition
  over real fills + funding.
* ``/api/decision_feed`` — signal → risk decision → execution on one row.
* ``/api/execution_divergence`` — real paper PnL vs shadow-evaluator sim.
* ``/ops`` — operational page renders and keeps auth.
* ``/api/shadow_panel`` — pruned strategies never surface (config ∩ live).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.dashboard.web as web  # noqa: E402

pytestmark = pytest.mark.integration_offline

_T0 = 1_796_000_000_000  # ~2026-11-04


# ── fixtures ─────────────────────────────────────────────────────────────

def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _make_live_db(path: str) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE signals (timestamp INTEGER, symbol TEXT, side TEXT, "
        "strategy TEXT, confidence REAL, price REAL, reason TEXT)"
    )
    db.execute(
        "CREATE TABLE decision_audit (id INTEGER PRIMARY KEY, "
        "timestamp INTEGER, decision_type TEXT, symbol TEXT, side TEXT, "
        "strategy TEXT, signal_confidence REAL, result TEXT, reason TEXT, "
        "metadata TEXT)"
    )
    db.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, "
        "side TEXT, strategy TEXT, status TEXT, entry_time INTEGER, "
        "exit_time INTEGER, entry_price REAL, exit_price REAL, size REAL, "
        "pnl_usd REAL, pnl_pct REAL, entry_fee REAL, funding_paid REAL, "
        "signal_metadata TEXT, exit_reason TEXT)"
    )
    # ── signal executed → trade ──
    db.execute(
        "INSERT INTO signals VALUES (?, 'BTC', 'long', 'JevJudge', 0.8, "
        "100.0, 'conf=0.8')",
        (_T0,),
    )
    db.execute(
        "INSERT INTO decision_audit (timestamp, decision_type, symbol, side, "
        "strategy, signal_confidence, result, reason, metadata) VALUES "
        "(?, 'execution', 'BTC', 'long', 'JevJudge', 0.8, 'executed', "
        "'size=0.100 @ 100.00', '{\"trade_id\": 1}')",
        (_T0,),
    )
    db.execute(
        "INSERT INTO trades (id, symbol, side, strategy, status, "
        "entry_time, exit_time, entry_price, exit_price, size, pnl_usd, "
        "pnl_pct, entry_fee, funding_paid, signal_metadata, exit_reason) "
        "VALUES (1, 'BTC', 'long', 'JevJudge', 'closed', ?, ?, 100.0, "
        "101.0, 0.1, 0.05, 0.0005, 0.0045, 0.01, "
        "'{\"entry_slippage_pct\": 0.0001, \"exit_slippage_pct\": 0.0001,"
        " \"exit_fee_pct\": 0.00045}', 'tp')",
        (_T0, _T0 + 3_600_000),
    )
    # ── signal rejected by a risk gate ──
    db.execute(
        "INSERT INTO signals VALUES (?, 'ETH', 'short', 'JevJudge', 0.7, "
        "200.0, 'conf=0.7')",
        (_T0 + 60_000,),
    )
    db.execute(
        "INSERT INTO decision_audit (timestamp, decision_type, symbol, side, "
        "strategy, signal_confidence, result, reason, metadata) VALUES "
        "(?, 'risk_gate', 'ETH', 'short', 'JevJudge', 0.7, 'rejected', "
        "'max_positions', '{}')",
        (_T0 + 60_000,),
    )
    # ── bare signal, no downstream rows ──
    db.execute(
        "INSERT INTO signals VALUES (?, 'SOL', 'long', 'JevJudge', 0.6, "
        "50.0, 'conf=0.6')",
        (_T0 + 120_000,),
    )
    db.commit()
    db.close()


def _make_research_db(path: str) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, symbol TEXT, "
        "strategy TEXT, variant TEXT, side TEXT, would_enter INTEGER, "
        "reason TEXT, timestamp_ms INTEGER, snapshot_json TEXT, "
        "ingested_at_ms INTEGER)"
    )
    db.execute(
        "CREATE TABLE shadow_outcome_scoreboards (id INTEGER PRIMARY KEY, "
        "evaluated_at_ms INTEGER, strategy TEXT, since_ms INTEGER, "
        "until_ms INTEGER, candle_source TEXT, metrics_json TEXT, "
        "disclaimer TEXT)"
    )
    db.commit()
    db.close()


class _StubDB:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def _conn(self) -> sqlite3.Connection:
        return self._c


def _make_engine(live_db_path: str):
    return SimpleNamespace(
        _db=_StubDB(_conn(live_db_path)),
        _config=None,
        _strategies=[SimpleNamespace(name="JevJudge")],
        _shadow_strategies=[
            SimpleNamespace(name="VWAPDeviation"),
            SimpleNamespace(name="VolatilityBreakout"),
        ],
    )


class _DashCase:
    """Shared app/engine/DB fixture."""

    def setup_method(self) -> None:
        self._orig_engine = web._engine
        self._tmp = tempfile.TemporaryDirectory()
        self._live = os.path.join(self._tmp.name, "bot.db")
        self._research = os.path.join(self._tmp.name, "research.db")
        _make_live_db(self._live)
        _make_research_db(self._research)
        self._env = patch.dict(
            os.environ, {"BOT_RESEARCH_DATABASE_PATH": self._research}
        )
        self._env.start()
        web._engine = _make_engine(self._live)
        web._ttl_clear()
        self.app, self.sio, _ = web.create_app({"mode": "paper"})
        self.client = self.app.test_client()

    def teardown_method(self) -> None:
        # Close the stub's sqlite conn first — Windows can't unlink a file
        # with an open handle.
        try:
            eng = web._engine
            if eng is not None and getattr(eng, "_db", None) is not None:
                eng._db._c.close()
        except Exception:
            pass
        web._engine = self._orig_engine
        web._ttl_clear()
        self._env.stop()
        self._tmp.cleanup()


# ── /ops page ────────────────────────────────────────────────────────────

class TestOpsPage(_DashCase):
    def test_ops_renders(self) -> None:
        r = self.client.get("/ops")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        for frag in (
            'id="logs-container"',
            'id="feed-silence-tbody"',
            'id="live-data-tbody"',
            'id="eng-state"',
            'id="eng-mem"',
            'id="eng-error"',
            'id="eng-exposure"',
            'id="strategies-panel"',
            'id="jev-list"',
            'id="aux-jobs-panel"',
            "renderAuxJobs",
        ):
            assert frag in html, frag
        # Auth flag still injected
        assert "AUTH_REQUIRED" in html

    def test_aux_jobs_in_market_data_health(self) -> None:
        """The sys-strip aux jobs must ride the market_data_health payload —
        jev feed, outcome-eval persist heartbeat, watchdogs run heartbeat."""
        r = self.client.get("/api/market_data_health")
        assert r.status_code == 200
        aux = r.get_json().get("aux_jobs") or {}
        for key in ("jev_feed", "outcome_eval", "watchdogs"):
            assert key in aux, key
            assert "degraded" in aux[key]
            assert "action" in aux[key]
        # Fixture has no scoreboards and no cron logs -> eval is degraded,
        # and the action must name the corrective step.
        assert aux["outcome_eval"]["degraded"] is True
        assert "outcome-eval" in aux["outcome_eval"]["action"]

    def test_ops_auth_same_as_index(self) -> None:
        """/ops and / must carry the same auth_required contract."""
        idx = self.client.get("/").get_data(as_text=True)
        ops = self.client.get("/ops").get_data(as_text=True)
        assert ("auth_required" in idx) == ("auth_required" in ops)

    def test_index_no_longer_ships_ops_panels(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        for frag in (
            'id="live-data-tbody"',
            'id="feed-silence-tbody"',
            'id="logs-container"',
            'id="tt-bias-tbody"',
            'id="ivshadow-tbody"',
            'id="signals-list"',
            'id="decisions-list"',
        ):
            assert frag not in html, frag


# ── decision feed ────────────────────────────────────────────────────────

class TestDecisionFeed(_DashCase):
    def test_joins_signal_gate_execution(self) -> None:
        rows = web.build_decision_feed(limit=10)
        by_stage = {r["stage"]: r for r in rows}
        assert set(by_stage) == {"executed", "rejected", "emitted"}
        ex = by_stage["executed"]
        assert ex["symbol"] == "BTC"
        assert ex["trade_id"] == 1
        assert ex["trade_pnl_usd"] == pytest.approx(0.05)
        rej = by_stage["rejected"]
        assert "max_positions" in rej["stage_detail"]
        em = by_stage["emitted"]
        assert em["symbol"] == "SOL"

    def test_endpoint_shape(self) -> None:
        r = self.client.get("/api/decision_feed")
        assert r.status_code == 200
        d = r.get_json()
        assert len(d["rows"]) == 3

    def test_no_engine_returns_empty(self) -> None:
        web._engine._db._c.close()
        web._engine = None
        try:
            r = self.client.get("/api/decision_feed")
            assert r.status_code == 200
            assert r.get_json()["rows"] == []
        finally:
            web._engine = _make_engine(self._live)


# ── execution PnL ────────────────────────────────────────────────────────

class TestExecutionPnl(_DashCase):
    def test_decomposition(self) -> None:
        r = self.client.get("/api/execution_pnl")
        assert r.status_code == 200
        d = r.get_json()
        assert d["executing"] == ["JevJudge"]
        assert len(d["rows"]) == 1
        row = d["rows"][0]
        assert row["strategy"] == "JevJudge"
        assert row["trades"] == 1
        # gross = stored pnl + fees = 0.05 + (0.0045 + 101*0.1*0.00045)
        assert row["gross"] == pytest.approx(0.05 + 0.0045 + 0.004545, abs=1e-4)
        assert row["fees"] == pytest.approx(0.0045 + 0.004545, abs=1e-4)
        # slip = (0.0001 + 0.0001) * 100*0.1 notional = 0.002
        assert row["slippage"] == pytest.approx(0.002, abs=1e-6)
        assert row["funding"] == pytest.approx(0.01)
        # net = pnl + funding = 0.06
        assert row["net"] == pytest.approx(0.06, abs=1e-6)

    def test_no_engine(self) -> None:
        web._engine._db._c.close()
        web._engine = None
        try:
            r = self.client.get("/api/execution_pnl")
            assert r.status_code == 200
            assert r.get_json()["rows"] == []
        finally:
            web._engine = _make_engine(self._live)


# ── hypotheses ───────────────────────────────────────────────────────────

class TestHypotheses(_DashCase):
    def test_jev_row_present(self) -> None:
        r = self.client.get("/api/hypotheses")
        assert r.status_code == 200
        d = r.get_json()
        ids = {h["id"] for h in d["hypotheses"]}
        assert "JevJudge::oos_kill" in ids
        # VWAP confirmation preregistration is always listed
        assert any("VWAPDeviation" in i for i in ids)

    def test_jev_counts_post_boundary_only(self) -> None:
        """JevJudge indep_n = closed trades post-2026-10-04 geometry
        boundary, deduped one-open-position-per-symbol. The fixture has
        exactly one such trade => 1/100."""
        r = self.client.get("/api/hypotheses")
        d = r.get_json()
        jev = next(h for h in d["hypotheses"] if h["id"] == "JevJudge::oos_kill")
        assert jev["indep_n"] == 1
        assert jev["target"] == 100
        assert jev["n_clustered"] == 1
        # the frontier stays visible for tooltips
        assert jev["cutoff_ms"] == 1791124780000
        assert jev["stale_verdict_excluded"] == 0

    def test_jev_stale_verdict_excluded_from_kill(self) -> None:
        """A trade opened on a >2h verdict stays in the DB but is excluded
        from the kill count — the row is flagged stale_verdict=1, never
        deleted, and reported via stale_verdict_excluded."""
        conn = _conn(self._live)
        boundary_plus = 1791124780000 + 86_400_000
        conn.execute(
            "INSERT INTO trades (id, symbol, side, strategy, status, "
            "entry_time, exit_time, entry_price, exit_price, size, "
            "pnl_usd, pnl_pct, signal_metadata) VALUES "
            "(9, 'SOL', 'long', 'JevJudge', 'closed', ?, ?, 50.0, 51.0, "
            "0.1, 0.05, 0.002, ?)",
            (
                boundary_plus,
                boundary_plus + 3_600_000,
                json.dumps({
                    # verdict issued >2h before the trade opened
                    "jev_decision_ts_ms": boundary_plus - (3 * 3_600_000),
                }),
            ),
        )
        conn.commit()
        conn.close()
        web._ttl_clear()
        r = self.client.get("/api/hypotheses")
        jev = next(
            h for h in r.get_json()["hypotheses"]
            if h["id"] == "JevJudge::oos_kill"
        )
        assert jev["indep_n"] == 1          # stale trade did NOT count
        assert jev["stale_verdict_excluded"] == 1

    def test_jev_duplicate_verdict_excluded_from_kill(self) -> None:
        """Two trades on the same jev_decision_ts_ms (restart re-fire)
        count once — later rows are excluded via duplicate_verdict."""
        conn = _conn(self._live)
        boundary_plus = 1791124780000 + 86_400_000
        verdict_ts = boundary_plus                       # same verdict, twice
        # both entries within 2h of the verdict (non-stale) and sequential
        # (no overlap) — the second can only be excluded by the dup rule
        for tid, et in ((9, boundary_plus + 600_000),
                        (10, boundary_plus + 4_800_000)):
            conn.execute(
                "INSERT INTO trades (id, symbol, side, strategy, status, "
                "entry_time, exit_time, entry_price, exit_price, size, "
                "pnl_usd, pnl_pct, signal_metadata) VALUES "
                "(?, 'SOL', 'long', 'JevJudge', 'closed', ?, ?, 50.0, 51.0, "
                "0.1, 0.05, 0.002, ?)",
                (tid, et, et + 1_800_000,
                 json.dumps({"jev_decision_ts_ms": verdict_ts})),
            )
        conn.commit()
        conn.close()
        web._ttl_clear()
        r = self.client.get("/api/hypotheses")
        jev = next(
            h for h in r.get_json()["hypotheses"]
            if h["id"] == "JevJudge::oos_kill"
        )
        # baseline BTC trade + first of the dup pair count; the second
        # trade on the same verdict ts is excluded
        assert jev["indep_n"] == 2
        assert jev["duplicate_verdict_excluded"] == 1

    def test_sealed_counter_zero_without_board(self) -> None:
        """No persisted confirm board + 0 post-cutoff decisions => the
        public counter reads 0/60 (seal hides metrics, not counts)."""
        r = self.client.get("/api/hypotheses")
        d = r.get_json()
        vw = next(h for h in d["hypotheses"] if "VWAPDeviation" in h["id"])
        assert vw["sealed"] is True
        assert vw["indep_n"] == 0
        assert vw["n_clustered"] == 0

    def test_sealed_counter_pending_when_decisions_unscored(self) -> None:
        """Post-cutoff decisions exist but the evaluator has not persisted
        a board yet => indep stays unknown, never a fabricated 0."""
        conn = _conn(self._research)
        cutoff = None
        from src.research.shadow_outcome_evaluator import (
            PREREGISTERED_CONFIRMATIONS,
        )

        for (strategy, variant), (c, _t, _e) in PREREGISTERED_CONFIRMATIONS.items():
            if strategy == "VWAPDeviation":
                cutoff = c
        conn.execute(
            "INSERT INTO shadow_decisions (symbol, strategy, variant, side, "
            "would_enter, reason, timestamp_ms, ingested_at_ms) VALUES "
            "('BTC', 'VWAPDeviation', 'iv_gate_shadow', 'long', 1, "
            "'routed', ?, ?)",
            (cutoff + 60_000, cutoff + 60_000),
        )
        conn.commit()
        conn.close()
        web._ttl_clear()
        r = self.client.get("/api/hypotheses")
        d = r.get_json()
        vw = next(h for h in d["hypotheses"] if "VWAPDeviation" in h["id"])
        assert vw["sealed"] is True
        assert vw["indep_n"] is None

    def test_sealed_board_never_leaks_metrics(self) -> None:
        """A sealed confirmation board may carry metrics in storage (defence
        in depth) — the endpoint must not forward them."""
        conn = _conn(self._research)
        conn.execute(
            "INSERT INTO shadow_outcome_scoreboards "
            "(evaluated_at_ms, strategy, since_ms, until_ms, candle_source, "
            "metrics_json, disclaimer) VALUES (?, 'VWAPDeviation', 0, 0, 'hl', "
            "?, 'x')",
            (
                _T0,
                json.dumps({
                    "strategy": "VWAPDeviation",
                    "variant": "iv_gate_shadow#confirm",
                    "sealed": True,
                    "n_independent": 7,
                    "n_clustered": 4,
                    "confirmation_min_indep": 60,
                    # forbidden while sealed — must not reach the payload
                    "net_profit_factor": 9.9,
                    "win_rate": 0.99,
                    "independent_outcomes": [[1, 2, "BTC", 0.5, 0.5]],
                }),
            ),
        )
        conn.commit()
        conn.close()
        web._ttl_clear()
        r = self.client.get("/api/hypotheses")
        d = r.get_json()
        vw = next(
            h for h in d["hypotheses"] if "VWAPDeviation" in h["id"]
        )
        assert vw["sealed"] is True
        assert vw["indep_n"] == 7
        assert vw["n_clustered"] == 4
        for leaked in (
            "net_profit_factor", "win_rate", "expectancy_r",
            "independent_outcomes", "profit_factor", "pnl",
        ):
            assert leaked not in vw, leaked

    def test_hypothesis_row_shape(self) -> None:
        r = self.client.get("/api/hypotheses")
        d = r.get_json()
        for h in d["hypotheses"]:
            for key in (
                "name", "state", "indep_n", "target", "n_clustered",
                "rate_per_day", "eta_ms", "expiry_ms", "sealed",
            ):
                assert key in h, key


# ── execution divergence ────────────────────────────────────────────────

class TestExecutionDivergence(_DashCase):
    def _insert_jev_board(self, outcomes) -> None:
        conn = _conn(self._research)
        conn.execute(
            "INSERT INTO shadow_outcome_scoreboards "
            "(evaluated_at_ms, strategy, since_ms, until_ms, candle_source, "
            "metrics_json, disclaimer) VALUES (?, 'JevJudge', 0, 0, 'hl', "
            "?, 'x')",
            (
                _T0,
                json.dumps({
                    "strategy": "JevJudge",
                    "variant": "iv_gate_shadow",
                    "n_independent": len(outcomes),
                    "independent_outcomes": outcomes,
                }),
            ),
        )
        conn.commit()
        conn.close()

    def test_pairs_and_delta(self) -> None:
        # sim net +10 bps vs real pnl_pct 0.0005 (5 bps) + funding 0.01/10
        # notional (+10 bps) → real 15 bps → delta +5 bps
        self._insert_jev_board([[_T0, _T0 + 3_600_000, "BTC", 0.001, 0.2]])
        web._ttl_clear()
        r = self.client.get("/api/execution_divergence")
        assert r.status_code == 200
        d = r.get_json()
        assert d["n_pairs"] == 1
        assert d["pairs"][0]["trade_id"] == 1
        assert d["pairs"][0]["delta_bps"] == pytest.approx(5.0, abs=0.5)
        assert d["alert_threshold_bps"] == 25.0
        # below min_pairs → no alert
        assert d["alert"] is False

    def test_alert_fires_above_threshold(self) -> None:
        outs = [
            [_T0 + i * 60_000, _T0 + i * 60_000 + 3_600_000, "BTC", 0.01, 1.0]
            for i in range(5)
        ]
        # add trades matching each sim outcome (real=0 → delta ~ -100bps)
        conn = _conn(self._live)
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (id, symbol, side, strategy, status, "
                "entry_time, exit_time, entry_price, exit_price, size, "
                "pnl_usd, pnl_pct, entry_fee, funding_paid, exit_reason) "
                "VALUES (?, 'BTC', 'long', 'JevJudge', 'closed', ?, ?, "
                "100.0, 100.0, 0.1, 0.0, 0.0, 0.0045, 0.0, 'tp')",
                (100 + i, _T0 + i * 60_000, _T0 + i * 60_000 + 3_600_000),
            )
        conn.commit()
        conn.close()
        self._insert_jev_board(outs)
        web._ttl_clear()
        d = self.client.get("/api/execution_divergence").get_json()
        assert d["n_pairs"] >= 5
        assert d["alert"] is True
        assert d["mean_abs_delta_bps"] > 25.0


# ── shadow panel pruning + bootstrap CI ──────────────────────────────────

class TestShadowPanelFilter(_DashCase):
    def test_only_live_strategies_surface(self) -> None:
        """Config lists 6 shadow strategies; only the 2 instantiated ones
        may reach the payload builder."""
        captured = {}

        def fake_payload(*, shadow_names, config, evaluate):
            captured["names"] = list(shadow_names)
            return {"rows": [], "shadow_names": list(shadow_names)}

        with patch(
            "src.research.shadow_panel.build_shadow_panel_payload",
            side_effect=fake_payload,
        ):
            # patch target is imported inside the route — patch the module attr
            import src.research.shadow_panel as sp

            with patch.object(sp, "build_shadow_panel_payload", fake_payload):
                r = self.client.get("/api/shadow_panel?evaluate=0")
        assert r.status_code == 200
        assert set(captured["names"]) == {
            "VWAPDeviation", "VolatilityBreakout",
        }

    def test_bootstrap_ci_hidden_below_30(self) -> None:
        from src.research.shadow_panel import bootstrap_profit_factor_ci

        assert bootstrap_profit_factor_ci([0.1, -0.05] * 14) is None  # n=28
        # n=32 mixed → CI tuple
        ci = bootstrap_profit_factor_ci(
            [0.2, 0.15, -0.1, -0.05] * 8, n_runs=1000
        )
        assert ci is not None
        lo, hi = ci
        assert lo <= hi

    def test_bootstrap_ci_deterministic(self) -> None:
        from src.research.shadow_panel import bootstrap_profit_factor_ci

        vals = [0.2, 0.15, -0.1, -0.05, 0.3, -0.2] * 6
        a = bootstrap_profit_factor_ci(vals, n_runs=1000)
        b = bootstrap_profit_factor_ci(vals, n_runs=1000)
        assert a == b

    def test_sealed_board_persists_no_outcomes(self) -> None:
        """Scoreboard.to_dict on a sealed board must not emit the compact
        outcome rows (a resolvable return list would defeat no-peeking)."""
        from src.research.shadow_outcome_evaluator import StrategyScoreboard

        b = StrategyScoreboard(strategy="X")
        b.sealed = True
        b.independent_outcome_rows = [SimpleNamespace(
            entry_ts_ms=1, exit_ts_ms=2, symbol="BTC",
            net_pnl_pct=0.1, net_r_multiple=0.5,
        )]
        d = b.to_dict()
        assert "independent_outcomes" not in d
        b.sealed = False
        d2 = b.to_dict()
        assert len(d2["independent_outcomes"]) == 1


# ── removed endpoints stay gone ──────────────────────────────────────────

class TestRemovedEndpoints(_DashCase):
    def test_top_traders_gone(self) -> None:
        assert self.client.get("/api/top_traders").status_code == 404

    def test_iv_gate_shadow_gone(self) -> None:
        assert self.client.get("/api/iv_gate_shadow").status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
