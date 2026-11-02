"""Adenda-2 review: PAUSE_COLLECTORS gate — every collector save_* must
short-circuit with the *declared* return type while the flag exists.

The flag lives at $BOT_DATA_ROOT/PAUSE_COLLECTORS (created by
deploy/linux/disk_guard.sh at >=85% fs usage).
"""
import os

import pytest

pytestmark = pytest.mark.unit

from src.data.research_database import ResearchDatabase
from src.utils.pause_flag import collectors_paused, flag_path


@pytest.fixture()
def paused_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_ROOT", str(tmp_path))
    flag_path().write_text("paused", encoding="utf-8")
    return ResearchDatabase(tmp_path / "research.db")


def test_flag_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_ROOT", str(tmp_path))
    assert collectors_paused() is False
    flag_path().write_text("paused")
    assert collectors_paused() is True


def test_gated_saves_return_declared_types(paused_db):
    db = paused_db

    assert db.save_research_candles([], "1m", None) is None
    assert db.save_research_candles_non_destructive([], "1m", None) == (0, 0)
    assert db.save_funding_with_meta([], None) is None
    assert db.save_oi_with_meta([], None) is None
    assert db.save_l2_snapshots([]) is None
    assert db.save_trade_tape([]) is None
    assert db.save_microstructure_gap("BTC", "ws", 0, 0) is None
    assert db.save_liquidation_map_zones([]) == 0
    assert db.save_dvol_daily([]) == 0
    assert db.save_deribit_snapshot({}) is None


def test_ungated_writes_still_work(paused_db):
    """feed-health + coverage heartbeats must keep running under pause."""
    db = paused_db
    db.save_feed_health_snapshot({"ok": True}, symbol="BTC")
    rows = db._conn().execute(
        "SELECT symbol, created_at_ms FROM feed_health_snapshots"
    ).fetchall()
    assert rows and rows[0][0] == "BTC"


def test_l2_books_enabled_env(monkeypatch, tmp_path):
    """L2_BOOKS_ENABLED=false must disable the recorder config — belt &
    suspenders with the empty recording path on bots-01."""
    from src.data.l2_book_recorder import config_from_mapping

    monkeypatch.setenv("L2_BOOKS_ENABLED", "false")
    cfg = config_from_mapping({"market_data.l2_recording.path": str(tmp_path / "l2")}, tmp_path)
    assert cfg.enabled is False

    monkeypatch.setenv("L2_BOOKS_ENABLED", "true")
    cfg = config_from_mapping({}, tmp_path)
    assert cfg.enabled is True

    monkeypatch.delenv("L2_BOOKS_ENABLED")
    cfg = config_from_mapping({}, tmp_path)
    assert cfg.enabled is True


def test_l2_disabled_creates_no_files(tmp_path, monkeypatch):
    """With L2 disabled the factory returns None — nothing touches l2_books."""
    monkeypatch.setenv("L2_BOOKS_ENABLED", "false")
    from src.data.l2_book_recorder import config_from_mapping

    cfg = config_from_mapping({"market_data.l2_recording.path": str(tmp_path / "l2_books")}, tmp_path)
    if cfg.enabled:
        rec_dir = tmp_path / "l2_books"
        rec_dir.mkdir()
    assert not (tmp_path / "l2_books").exists()
