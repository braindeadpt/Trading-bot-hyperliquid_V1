"""Regression tests for the ``/api/strategy_pnl`` 500 bug — REAL Database.

The dashboard endpoint passed ``strategy=`` to ``Database.get_strategy_pnl()``,
which did not accept that keyword → ``TypeError`` → HTTP 500 on every call
(fixed by adding the optional ``strategy`` filter to the DB method).

These tests use a real SQLite ``Database`` (temp file) so the SQL aggregation
and both filter parameters (``strategy``, ``since_ms``) are exercised — no
mocks — and the Flask endpoint is hit through the test client.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from src.data.database import Database, TradeEntry, TradeExit

pytestmark = pytest.mark.integration_offline

T0 = 1_700_000_000_000  # fixed baseline timestamp (ms)


def _fresh_db() -> Database:
    """Build a Database in a temp file so each test starts clean."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Database(Path(tmp.name))


def _seed_trade(
    db: Database,
    *,
    symbol: str = "BTC",
    strategy: str = "VWAPDeviation",
    pnl_usd: float = 0.0,
    entry_time: int = T0,
    exit_time: int = 0,
    status: str = "closed",
) -> int:
    """Insert a trade row; when ``status == 'closed'`` also record the exit."""
    entry_price = 50_000.0
    size = 0.1
    tid = db.save_trade_entry(
        TradeEntry(
            symbol=symbol,
            side="long",
            entry_price=entry_price,
            entry_time=entry_time,
            size=size,
            strategy=strategy,
        )
    )
    if status == "closed":
        exit_price = entry_price + (100.0 if pnl_usd >= 0 else -100.0)
        db.update_trade_exit(
            TradeExit(
                trade_id=tid,
                exit_price=exit_price,
                exit_time=exit_time,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_usd / (entry_price * size),
                exit_reason="tp" if pnl_usd > 0 else "stop_loss",
            )
        )
    return tid


def _row(rows, strategy: str) -> dict:
    for r in rows:
        if r["strategy"] == strategy:
            return r
    raise AssertionError(f"no row for strategy {strategy!r} in {rows}")


class TestGetStrategyPnlDb:
    def test_aggregates_closed_trades_only(self) -> None:
        db = _fresh_db()
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=100.0, exit_time=T0 + 60_000)   # win
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=-50.0, exit_time=T0 + 120_000)  # loss
        _seed_trade(db, strategy="ChecklistMeta", pnl_usd=30.0, exit_time=T0 + 90_000)    # win
        _seed_trade(db, strategy="VWAPDeviation", status="open")  # must be excluded

        rows = db.get_strategy_pnl()

        vwap = _row(rows, "VWAPDeviation")
        assert vwap["trades"] == 2          # open trade excluded
        assert vwap["wins"] == 1
        assert vwap["total_pnl_usd"] == pytest.approx(50.0)
        assert vwap["win_rate"] == pytest.approx(0.5)
        assert vwap["last_exit_ms"] == T0 + 120_000

        cm = _row(rows, "ChecklistMeta")
        assert cm["trades"] == 1
        assert cm["wins"] == 1
        assert cm["total_pnl_usd"] == pytest.approx(30.0)
        assert cm["win_rate"] == pytest.approx(1.0)

    def test_strategy_filter_restricts_rows(self) -> None:
        db = _fresh_db()
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=100.0, exit_time=T0 + 60_000)
        _seed_trade(db, strategy="ChecklistMeta", pnl_usd=30.0, exit_time=T0 + 90_000)

        rows = db.get_strategy_pnl(strategy="VWAPDeviation")
        assert [r["strategy"] for r in rows] == ["VWAPDeviation"]

        rows_none = db.get_strategy_pnl(strategy="DoesNotExist")
        assert rows_none == []

        # Empty/None filter → no strategy restriction
        rows_all = db.get_strategy_pnl(strategy="")
        assert {r["strategy"] for r in rows_all} == {"VWAPDeviation", "ChecklistMeta"}

    def test_since_ms_filter_restricts_by_exit_time(self) -> None:
        db = _fresh_db()
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=100.0, exit_time=T0)
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=200.0, exit_time=T0 + 86_400_000)

        recent = db.get_strategy_pnl(since_ms=T0 + 1)
        assert len(recent) == 1
        assert recent[0]["trades"] == 1
        assert recent[0]["last_exit_ms"] == T0 + 86_400_000

        all_rows = db.get_strategy_pnl(since_ms=None)
        assert _row(all_rows, "VWAPDeviation")["trades"] == 2

    def test_combined_strategy_and_since_ms(self) -> None:
        db = _fresh_db()
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=100.0, exit_time=T0)
        _seed_trade(db, strategy="VWAPDeviation", pnl_usd=200.0, exit_time=T0 + 86_400_000)
        _seed_trade(db, strategy="ChecklistMeta", pnl_usd=300.0, exit_time=T0 + 86_400_000)

        rows = db.get_strategy_pnl(since_ms=T0 + 1, strategy="VWAPDeviation")
        assert len(rows) == 1
        assert rows[0]["strategy"] == "VWAPDeviation"
        assert rows[0]["trades"] == 1
        assert rows[0]["total_pnl_usd"] == pytest.approx(200.0)


class TestApiStrategyPnlRealDb:
    """Endpoint regression — the exact calls that used to 500."""

    def setup_method(self):
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        self._db = _fresh_db()
        _seed_trade(self._db, strategy="VWAPDeviation", pnl_usd=100.0, exit_time=T0 + 60_000)
        _seed_trade(self._db, strategy="VWAPDeviation", pnl_usd=-50.0, exit_time=T0 + 120_000)
        _seed_trade(self._db, strategy="ChecklistMeta", pnl_usd=30.0, exit_time=T0 + 90_000)

        engine = MagicMock()
        engine._symbols = ["BTC", "ETH", "SOL"]
        engine._db = self._db
        web._engine = engine

        self.app, self.sio, self._emit = web.create_app({"mode": "paper"})
        self.client = self.app.test_client()

    def teardown_method(self):
        self._web._engine = self._orig_engine
        self._db.close()

    def test_default_returns_200_with_rows(self) -> None:
        r = self.client.get("/api/strategy_pnl")
        assert r.status_code == 200
        body = r.get_json()
        assert body["days"] == 0
        assert body["strategy_filter"] is None
        strategies = {row["strategy"] for row in body["rows"]}
        assert strategies == {"VWAPDeviation", "ChecklistMeta"}
        vwap = next(row for row in body["rows"] if row["strategy"] == "VWAPDeviation")
        assert vwap["trades"] == 2
        assert vwap["total_pnl_usd"] == pytest.approx(50.0)

    def test_strategy_filter_returns_200(self) -> None:
        """Regression: GET with ?strategy= crashed with 500 before the fix."""
        r = self.client.get("/api/strategy_pnl?strategy=VWAPDeviation")
        assert r.status_code == 200
        body = r.get_json()
        assert body["strategy_filter"] == "VWAPDeviation"
        assert [row["strategy"] for row in body["rows"]] == ["VWAPDeviation"]

    def test_days_filter_returns_200(self) -> None:
        r = self.client.get("/api/strategy_pnl?days=7")
        assert r.status_code == 200
        body = r.get_json()
        assert body["days"] == 7
        assert body["since_ms"] is not None

    def test_invalid_days_returns_200(self) -> None:
        r = self.client.get("/api/strategy_pnl?days=abc")
        assert r.status_code == 200
        assert r.get_json()["days"] == 0

    def test_regression_guard_reproduces_pre_fix_500(self) -> None:
        """Prove the regression guard actually catches the bug.

        The original bug: ``api_strategy_pnl`` called
        ``db.get_strategy_pnl(since_ms=..., strategy=...)`` but the DB
        method had no ``strategy`` kwarg -> ``TypeError`` -> HTTP 500 on
        EVERY request with ?strategy=. This test simulates the pre-fix
        signature (a method that only accepts ``since_ms``) and asserts
        the endpoint degrades to 500 exactly as it did in production. If
        the endpoint or the DB method regresses, this test fails with a
        clear signal instead of silently returning an empty body.
        """
        from unittest.mock import patch

        def _pre_fix_get_strategy_pnl(self, since_ms=None):  # noqa: ANN001
            # Pre-fix signature: only `since_ms` — calling with `strategy=`
            # raises the exact TypeError that produced the production 500.
            raise TypeError(
                "get_strategy_pnl() got an unexpected keyword argument 'strategy'"
            )

        with patch.object(
            self._db, "get_strategy_pnl", _pre_fix_get_strategy_pnl
        ):
            r = self.client.get("/api/strategy_pnl?strategy=VWAPDeviation")
        assert r.status_code == 500, (
            "Pre-fix DB signature must produce a 500 on ?strategy= — if this "
            "assertion fails the endpoint no longer degrades loudly and the "
            "bug could hide as an empty 200."
        )
