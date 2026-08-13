"""Tests for the preflight feed-delivery check wired into main.py boot.

Pins the boot contract (``main._preflight_feed_check`` /
``main._run_preflight_at_boot``):

  * the check runs ``scripts/preflight_feed_check.py`` against the live DB
    with the resolved config + L2 dir before the engine starts;
  * exit 1 (a contracted feed not delivering / no evidence) BLOCKS boot with
    a clear message and exit code 1;
  * exit 2 (past the warn fraction) warns and continues (feeds refresh on
    start);
  * exit 0 passes;
  * ``--skip-preflight`` skips the check entirely;
  * the real script through the wiring returns 0 on fresh evidence and 1 on
    missing/stale evidence.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as main_mod  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _log() -> logging.Logger:
    return logging.getLogger("test_preflight_boot_wiring")


SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")


def _make_db(path: Path, *, liq_okx_ms: int, liq_bybit_ms: int,
             funding_ms: int, candle_ms: int, candle_15m_ms: int = 0) -> None:
    """bot.db with the schema preflight reads (same helper as
    tests/test_preflight_feed_check.py)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE liquidation_events (symbol TEXT, timestamp_ms INTEGER, "
               "notional_usd REAL, side TEXT, source TEXT)")
    db.execute("CREATE TABLE funding_history (symbol TEXT, current REAL, "
               "predicted REAL, timestamp INTEGER)")
    db.execute("CREATE TABLE candles_1m (symbol TEXT, timestamp_ms INTEGER, "
               "open REAL, high REAL, low REAL, close REAL, volume REAL, "
               "funding_rate REAL, oi_total REAL, oi_delta REAL, "
               "buy_volume REAL, sell_volume REAL, trade_count INTEGER)")
    db.execute("CREATE TABLE candles_15m (symbol TEXT, timestamp_ms INTEGER, "
               "open REAL, high REAL, low REAL, close REAL, volume REAL, "
               "funding_rate REAL, oi_total REAL, oi_delta REAL, "
               "buy_volume REAL, sell_volume REAL, trade_count INTEGER)")
    db.execute("CREATE TABLE binance_perp_prices (symbol TEXT, timestamp_ms INTEGER, price REAL)")
    if liq_okx_ms:
        db.execute("INSERT INTO liquidation_events VALUES ('BTC', ?, 1e6, 'long', 'okx')",
                   (liq_okx_ms,))
    if liq_bybit_ms:
        db.execute("INSERT INTO liquidation_events VALUES ('BTC', ?, 1e6, 'long', 'bybit')",
                   (liq_bybit_ms,))
    if funding_ms:
        db.execute("INSERT INTO funding_history VALUES ('BTC', 0.001, 0.001, ?)",
                   (funding_ms,))
    for table, ts in (("candles_1m", candle_ms), ("candles_15m", candle_15m_ms)):
        if ts:
            for symbol in SYMBOLS:
                db.execute(f"INSERT INTO {table} VALUES (?, ?,1,2,0,1,10,0,100,0,5,5,100)",
                           (symbol, ts))
    db.commit()
    db.close()


NOW = int(time.time() * 1000)


def _now() -> int:
    """Fresh 'now' at call time — candle freshness thresholds (150s warn)
    mean import-time NOW ages out of range in a long suite run."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Subprocess invocation + decision logic (in-process)
# ---------------------------------------------------------------------------

def test_preflight_invokes_script_with_resolved_paths(tmp_path, monkeypatch) -> None:
    db = tmp_path / "bot.db"
    cfg = tmp_path / "settings.yaml"
    l2 = tmp_path / "l2_books"
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("cwd")))
        return _FakeResult(0)

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    rc = main_mod._preflight_feed_check(db, cfg, l2_dir=l2)
    assert rc == 0
    assert len(calls) == 1
    cmd, cwd = calls[0]
    joined = " ".join(cmd)
    assert "preflight_feed_check.py" in joined
    assert "--db" in cmd and str(db) in cmd
    assert "--config" in cmd and str(cfg) in cmd
    assert "--l2-dir" in cmd and str(l2) in cmd
    assert cwd == str(main_mod.PROJECT_ROOT)


def test_preflight_candles_only_passes_flags(tmp_path, monkeypatch) -> None:
    """The backtest path requests candles-only + window-end coverage."""
    db = tmp_path / "bot.db"
    cfg = tmp_path / "settings.yaml"
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeResult(0)

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)

    rc = main_mod._preflight_feed_check(
        db, cfg, candles_only=True, min_latest_ms=1787000000000,
    )
    assert rc == 0
    cmd = calls[0]
    assert "--candles-only" in cmd
    assert "--min-latest-ms" in cmd
    assert "1787000000000" in cmd
    assert "--l2-dir" not in cmd


def test_preflight_rc1_blocks_boot(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_mod, "_preflight_feed_check", lambda *a, **k: 1)
    rc = main_mod._run_preflight_at_boot(Path("db"), Path("cfg"), logger=_log())
    assert rc == 1
    out = capsys.readouterr().err
    assert "[FATAL] Preflight feed check FAILED" in out
    assert "--skip-preflight" in out


def test_preflight_rc2_warns_and_continues(monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "_preflight_feed_check", lambda *a, **k: 2)
    assert main_mod._run_preflight_at_boot(Path("db"), Path("cfg"), logger=_log()) is None


def test_preflight_rc0_passes(monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "_preflight_feed_check", lambda *a, **k: 0)
    assert main_mod._run_preflight_at_boot(Path("db"), Path("cfg"), logger=_log()) is None


def test_preflight_skip_does_not_invoke(monkeypatch) -> None:
    called: list = []

    def fake(*a, **k):
        called.append(1)
        return 0

    monkeypatch.setattr(main_mod, "_preflight_feed_check", fake)
    assert main_mod._run_preflight_at_boot(
        Path("db"), Path("cfg"), skip=True, logger=_log(),
    ) is None
    assert not called


# ---------------------------------------------------------------------------
# Real script through the wiring (temp DB + real config)
# ---------------------------------------------------------------------------

def test_real_preflight_passes_with_fresh_evidence(tmp_path) -> None:
    db = tmp_path / "bot.db"
    _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 9_000,
             funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
             candle_15m_ms=_now() - 60_000)
    l2 = tmp_path / "l2"
    l2.mkdir()
    (l2 / "book.json").write_text("{}", encoding="utf-8")

    rc = main_mod._preflight_feed_check(db, ROOT / "config" / "settings.yaml", l2_dir=l2)
    assert rc == 0


def test_real_preflight_blocks_with_no_evidence(tmp_path) -> None:
    db = tmp_path / "bot.db"
    _make_db(db, liq_okx_ms=0, liq_bybit_ms=0, funding_ms=0, candle_ms=0)
    l2 = tmp_path / "empty_l2"
    l2.mkdir()

    rc = main_mod._preflight_feed_check(db, ROOT / "config" / "settings.yaml", l2_dir=l2)
    assert rc == 1


def test_real_preflight_backtest_candles_blocks_backlog(tmp_path) -> None:
    """Backtest path (candles-only + min-latest-ms): a window end the data
    does not reach returns 1 — the pre-backtest backlog guard."""
    db = tmp_path / "bot.db"
    _make_db(db, liq_okx_ms=0, liq_bybit_ms=0, funding_ms=0,
             candle_ms=_now() - 30_000, candle_15m_ms=_now() - 60_000)
    # Window end 2h in the future — candles don't cover it.
    rc = main_mod._preflight_feed_check(
        db, ROOT / "config" / "settings.yaml",
        candles_only=True, min_latest_ms=NOW + 2 * 3600_000,
    )
    assert rc == 1

    # Same DB, window end already covered -> passes (coverage, not freshness).
    # Use a target well below both candles (30 min back) — a min_latest too
    # close to _now() races the 15m candle (inserted at _now()-60s) ageing past
    # it during the test's own runtime.
    rc = main_mod._preflight_feed_check(
        db, ROOT / "config" / "settings.yaml",
        candles_only=True, min_latest_ms=_now() - 30 * 60_000,
    )
    assert rc == 0


def test_real_preflight_blocks_with_stale_evidence(tmp_path) -> None:
    """A contracted feed past its silence threshold blocks boot (rc 1)."""
    db = tmp_path / "bot.db"
    _make_db(db, liq_okx_ms=NOW - 7 * 3600_000, liq_bybit_ms=NOW - 5_000,
             funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
             candle_15m_ms=_now() - 60_000)
    l2 = tmp_path / "l2"
    l2.mkdir()
    (l2 / "book.json").write_text("{}", encoding="utf-8")

    rc = main_mod._preflight_feed_check(db, ROOT / "config" / "settings.yaml", l2_dir=l2)
    assert rc == 1
