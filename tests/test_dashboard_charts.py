"""Tests for dashboard chart API endpoints — Flask test client."""

from __future__ import annotations

import sys
import os
import json
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import src.dashboard.web as web
import pytest

pytestmark = pytest.mark.integration_offline


def _make_mock_db():
    from src.data.database import Candle
    db = MagicMock()
    all_candles = [
        Candle(
            symbol="BTC", timestamp_ms=ts * 1000,
            open=float(ts), high=float(ts) + 10, low=float(ts) - 10,
            close=float(ts) + 1, volume=100.0,
        )
        for ts in range(1700000000, 1700000050)
    ]
    db.get_candles.side_effect = (
        lambda symbol, timeframe, limit=500, start_ms=None, end_ms=None: all_candles[:limit]
    )
    return db


def _make_mock_engine():
    engine = MagicMock()
    engine._symbols = ["BTC", "ETH", "SOL"]
    engine._db = _make_mock_db()
    return engine


class TestCandlesEndpoint:
    def setup_method(self):
        self._orig_engine = web._engine
        self._engine = _make_mock_engine()
        web._engine = self._engine
        config = {"mode": "paper"}
        self.app, self.sio, self._emit = web.create_app(config)
        self.client = self.app.test_client()

    def teardown_method(self):
        web._engine = self._orig_engine

    def _get(self, path):
        return self.client.get(path)

    def test_valid(self):
        r = self._get("/api/candles?symbol=BTC&tf=5m&limit=10")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert isinstance(data, list)
        assert len(data) == 10
        for c in data:
            assert all(k in c for k in ("time", "open", "high", "low", "close", "volume"))

    def test_invalid_symbol(self):
        r = self._get("/api/candles?symbol=INVALID&tf=5m")
        assert r.status_code == 400

    def test_invalid_tf(self):
        r = self._get("/api/candles?symbol=BTC&tf=99m")
        assert r.status_code == 400

    def test_default_limit(self):
        r = self._get("/api/candles?symbol=BTC&tf=1m")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert len(data) <= 200

    def test_max_limit(self):
        r = self._get("/api/candles?symbol=BTC&tf=1h&limit=9999")
        assert r.status_code == 200

    def test_missing_symbol(self):
        r = self._get("/api/candles?tf=5m")
        assert r.status_code == 400

    def test_missing_tf(self):
        r = self._get("/api/candles?symbol=BTC")
        assert r.status_code == 400


class TestTradesChartEndpoint:
    def setup_method(self):
        self._orig_engine = web._engine
        self._engine = _make_mock_engine()
        self._mock_trades = [
            {"id": 1, "symbol": "BTC", "side": "long", "entry_price": 50000.0,
             "exit_price": 51000.0, "entry_time": 1700000000000,
             "exit_time": 1700050000000, "pnl_usd": 100.0, "pnl_pct": 0.02,
             "strategy": "test", "exit_reason": "tp",
             "status": "closed", "size": 0.1},
            {"id": 2, "symbol": "BTC", "side": "short", "entry_price": 51000.0,
             "exit_price": 50500.0, "entry_time": 1700100000000,
             "exit_time": None, "pnl_usd": None, "pnl_pct": None,
             "strategy": "test", "exit_reason": None,
             "status": "open", "size": 0.1},
        ]
        self._engine._db._conn.return_value.execute.return_value.fetchall.return_value = self._mock_trades
        web._engine = self._engine
        config = {"mode": "paper"}
        self.app, self.sio, self._emit = web.create_app(config)
        self.client = self.app.test_client()

    def teardown_method(self):
        web._engine = self._orig_engine

    def test_valid_symbol(self):
        r = self.client.get("/api/trades_chart?symbol=BTC&limit=10")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert isinstance(data, list)
        assert len(data) == 2
        for t in data:
            assert "symbol" in t
            assert "side" in t

    def test_invalid_symbol(self):
        r = self.client.get("/api/trades_chart?symbol=INVALID")
        assert r.status_code == 400

    def test_all_symbols(self):
        r = self.client.get("/api/trades_chart?limit=10")
        assert r.status_code == 200


class TestStrategyPnlEndpoint:
    def setup_method(self):
        self._orig_engine = web._engine
        self._engine = _make_mock_engine()
        self._rows = [
            {"strategy": "VWAPDeviation", "trades": 5, "wins": 3,
             "total_pnl_usd": 120.0, "win_rate": 0.6},
        ]
        self._engine._db.get_strategy_pnl.return_value = self._rows
        web._engine = self._engine
        config = {"mode": "paper"}
        self.app, self.sio, self._emit = web.create_app(config)
        self.client = self.app.test_client()

    def teardown_method(self):
        web._engine = self._orig_engine

    def test_default(self):
        r = self.client.get("/api/strategy_pnl")
        assert r.status_code == 200
        data = json.loads(r.data)
        assert data["rows"] == self._rows
        self._engine._db.get_strategy_pnl.assert_called_once_with(
            since_ms=None, strategy=None
        )

    def test_strategy_filter(self):
        r = self.client.get("/api/strategy_pnl?strategy=VWAPDeviation")
        assert r.status_code == 200
        self._engine._db.get_strategy_pnl.assert_called_once_with(
            since_ms=None, strategy="VWAPDeviation"
        )

    def test_days_filter(self):
        r = self.client.get("/api/strategy_pnl?days=7")
        assert r.status_code == 200
        args, kwargs = self._engine._db.get_strategy_pnl.call_args
        assert kwargs["since_ms"] is not None
        assert kwargs["strategy"] is None


class TestLiveDataPredictedAndBook:
    """REST live tape should use HL predicted + L2 bid/ask, not mids zeros."""

    def setup_method(self):
        self._orig = web._engine

    def teardown_method(self):
        web._engine = self._orig

    def test_predicted_falls_back_to_hl_8h(self):
        snap = type("S", (), {"predicted_funding_hl_8h": 0.00012})()
        web._engine = type("E", (), {
            "_hl_predicted": {"BTC": snap},
            "_latest_agg_funding": {},
        })()
        ctx = type("C", (), {"predicted_funding": None})()
        assert web._predicted_funding_for("BTC", ctx) == 0.00012

    def test_api_live_data_uses_orderbook_when_mids_bid_zero(self):
        price = type("P", (), {"mid": 100.0, "bid": 0.0, "ask": 0.0})()
        ob = type("O", (), {"best_bid": 99.9, "best_ask": 100.1})()
        web._engine = type("E", (), {
            "_symbols": ["BTC"],
            "_latest_price": {"BTC": price},
            "_latest_ctx": {"BTC": type("C", (), {
                "funding_rate": 0.0001,
                "predicted_funding": None,
                "open_interest": 1.0,
            })()},
            "_latest_agg_funding": {},
            "_hl_predicted": {"BTC": type("S", (), {"predicted_funding_hl_8h": 0.0002})()},
            "_market_data_health": {},
            "_latest_orderbook": {"BTC": ob},
            "_last_market_events": {},
        })()
        app, _, _ = web.create_app({"mode": "paper"})
        client = app.test_client()
        r = client.get("/api/live_data")
        assert r.status_code == 200
        row = r.get_json()[0]
        assert row["bid"] == 99.9
        assert row["ask"] == 100.1
        assert row["predicted"] == 0.0002


class TestDashboardLayout:
    def test_unique_chart_ids_and_no_junk_panels(self):
        html = (
            Path(__file__).resolve().parents[1]
            / "src" / "dashboard" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        assert html.count('id="chart-container"') == 1
        assert html.count('id="chart-symbol"') == 1
        assert 'id="vol-indicators-tbody"' not in html
        assert ">Candle Watch" not in html
        assert "portfolio.daily_trades ||" not in html
        assert "Risk limit" not in html
        assert 'id="kpi-daily-total"' in html
        assert "Top Traders (aggregate)" in html
        assert 'id="ivshadow-tbody"' in html
        assert 'class="research-fold"' not in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
