"""Unified paper ledger — payload shape + schema (offline)."""
from __future__ import annotations

import sqlite3

import pytest

from src.research.paper_ledger import (
    build_paper_ledger_payload, _row, _resmom_row, _latency_row)


@pytest.mark.unit
def test_row_schema():
    r = _row("a", "b", "shadow_daemon", "sealed", n=3, n_readout=5,
             readout_ms=1, net=1.5, net_unit="usd", detail="x")
    for k in ("id", "name", "kind", "prereg_state", "n", "n_readout",
              "readout_ms", "net", "net_unit", "detail"):
        assert k in r


@pytest.mark.unit
def test_payload_rows_present():
    payload = build_paper_ledger_payload()
    rows = payload["rows"]
    ids = {r["id"] for r in rows}
    assert "JevJudge::experiment" in ids
    assert "CarryA1::shadow" in ids
    assert "UnlockShort::shadow" in ids
    assert "BorosLockedSpread::shadow" in ids
    assert "ResidualMomentum::forward" in ids
    assert "LatencyProbe::measure" in ids
    # daemon rows degrade to "—" (n None) when ledgers are absent
    lat = next(r for r in rows if r["id"] == "LatencyProbe::measure")
    assert lat["net_unit"] == "bps"


@pytest.mark.unit
def test_resmom_row_reads_ledger(tmp_path, monkeypatch):
    import src.research.paper_ledger as pl
    db = tmp_path / "resmom_shadow.db"
    con = sqlite3.connect(str(db))
    con.executescript(
        "CREATE TABLE resmom_nav(ts_ms INTEGER, arm TEXT, nav_usd REAL,"
        " gross_usd REAL, n_legs INTEGER, dd REAL);"
        "CREATE TABLE resmom_rebalances(week_key_ms INTEGER, arm TEXT);"
        "CREATE TABLE resmom_meta(k TEXT, v TEXT);")
    con.execute("INSERT INTO resmom_nav VALUES(1,'A',10100,0,0,0)")
    con.execute("INSERT INTO resmom_nav VALUES(2,'A',10200,0,0,0)")
    con.execute("INSERT INTO resmom_rebalances VALUES(1,'A')")
    con.commit()
    con.close()
    monkeypatch.setattr(pl, "_RESEARCH", tmp_path)
    row = _resmom_row(2)
    assert row["id"] == "ResidualMomentum::forward"
    assert row["n_readout"] == 26


@pytest.mark.unit
def test_endpoint_registered():
    from src.dashboard.web import create_app
    app, _s, _e = create_app({"cors_allowed_origins": ["*"]})
    client = app.test_client()
    r = client.get("/api/paper_ledger")
    assert r.status_code == 200
    body = r.get_json()
    assert "rows" in body and isinstance(body["rows"], list)
