"""Tests for the dashboard DVOL panel endpoint (``/api/dvol``).

Read-only research data: daily DVOL series + trailing-30d percentile per
symbol, with BTC/ETH native and SOL/HYPE classified against the BTC proxy.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase  # noqa: E402

DAY = 86_400_000


class TestDashboardDvolEndpoint:
    pytestmark = pytest.mark.integration_offline

    def setup_method(self) -> None:
        import src.dashboard.web as web

        self._web = web
        self._orig_engine = web._engine
        web._engine = None
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmp.name, "research.db")

        # Seed 39 daily closes: BTC rising (last = max -> high_iv), ETH
        # falling (last = min -> low_iv). SOL/HYPE classify via BTC proxy.
        rdb = ResearchDatabase(self._db_path)
        now = int(time.time() * 1000)
        start = now - 39 * DAY
        for i in range(1, 40):
            rdb.save_dvol_daily([("BTC", start + i * DAY, 40.0 + i)])
            rdb.save_dvol_daily([("ETH", start + i * DAY, 60.0 - 0.2 * i)])
        rdb.close()

        self._app, self._sio, _ = web.create_app({"mode": "paper"})
        self._client = self._app.test_client()

    def teardown_method(self) -> None:
        self._web._engine = self._orig_engine
        self._tmp.cleanup()

    def _get(self):
        import src.dashboard.web as web_mod

        rdb = ResearchDatabase(self._db_path)

        with patch.object(web_mod, "_open_research_db", lambda: rdb):
            return self._client.get("/api/dvol")

    def test_shape_and_current_classification(self) -> None:
        r = self._get()
        assert r.status_code == 200
        d = r.get_json()
        assert "error" not in d
        assert d["threshold"] == 66.7
        cur = d["current"]
        assert cur["BTC"]["cls"] == "high_iv"
        assert cur["ETH"]["cls"] == "low_iv"
        # SOL/HYPE use the BTC proxy, so they share BTC's classification
        assert cur["SOL"]["currency"] == "BTC"
        assert cur["HYPE"]["currency"] == "BTC"
        assert cur["SOL"]["cls"] == cur["BTC"]["cls"]
        assert cur["HYPE"]["cls"] == cur["BTC"]["cls"]

    def test_series_has_daily_entries_with_percentile(self) -> None:
        r = self._get()
        d = r.get_json()
        btc = d["series"]["BTC"]
        assert len(btc) >= 30
        # every close is a number; the trailing percentile is populated for
        # days beyond the 20-close warmup
        assert all(isinstance(row["close"], (int, float)) for row in btc)
        populated = [row for row in btc if row["pct"] is not None]
        assert len(populated) >= 10
        assert all(0.0 <= row["pct"] <= 100.0 for row in populated)

    def test_empty_db_yields_unknown(self) -> None:
        empty_path = os.path.join(self._tmp.name, "empty.db")
        import src.dashboard.web as web_mod

        rdb = ResearchDatabase(empty_path)

        with patch.object(web_mod, "_open_research_db", lambda: rdb):
            r = self._client.get("/api/dvol")
        assert r.status_code == 200
        d = r.get_json()
        for sym in ("BTC", "ETH", "SOL", "HYPE"):
            assert d["current"][sym]["cls"] == "unknown"
            assert d["current"][sym]["pct"] is None
        assert d["series"] == {"BTC": [], "ETH": []}
