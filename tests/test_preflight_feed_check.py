"""Tests for the pre-start feed-delivery check (scripts/preflight_feed_check.py).

Pins the exit-code contract: a contracted feed with stale-or-missing
evidence fails before the bot starts (instead of waiting for the watchdog
silence threshold), a feed past warn-fraction exits 2, and fresh evidence
exits 0. Coinalyze (verify-only, never persisted) is skipped by default.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preflight_feed_check.py"

pytestmark = pytest.mark.unit

# Same symbol set get_trading_symbols() resolves for the real config.
SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")


def _make_db(path, *, liq_okx_ms, liq_bybit_ms, funding_ms, candle_ms,
             candle_15m_ms=None) -> None:
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
    # The candle check runs per symbol: every trading symbol needs fresh
    # 1m/15m evidence (0 = leave that symbol with no candles at all).
    for table, ts in (("candles_1m", candle_ms), ("candles_15m", candle_15m_ms)):
        if ts:
            for symbol in SYMBOLS:
                db.execute(f"INSERT INTO {table} VALUES (?, ?,1,2,0,1,10,0,100,0,5,5,100)",
                           (symbol, ts))
    db.commit()
    db.close()


def _make_l2_dir(tmp: str) -> str:
    """Fresh L2 recording evidence so CI (no data/research/l2_books) stays green."""
    d = Path(tmp) / "l2_books" / "BTC"
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.jsonl").write_text("{}\n", encoding="utf-8")
    return str(Path(tmp) / "l2_books")


def _run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


NOW = int(time.time() * 1000)


def _now() -> int:
    """Wall-clock ms at call time. 'Fresh' timestamps must be relative to the
    moment the test RUNS, not module collection — a long CI run (minutes
    after import) would otherwise age a 30s-old candle past the 1m warn
    threshold (150s)."""
    return int(time.time() * 1000)


def test_fresh_evidence_exits_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 9_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[PASS]" in r.stdout
        assert "candles_1m BTC" in r.stdout


def test_stale_feed_fails_before_threshold_would_alert_anyway() -> None:
    """Age > max_silence (6h) => immediate fail, exit 1."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 7 * 3600_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout
        assert "FAIL" in r.stdout


def test_missing_feed_fails_immediately() -> None:
    """A contracted feed with no persisted evidence at all => fail (exit 1),
    catching a blocked feed before the silence threshold would trip."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout


def test_warn_fraction_exits_two() -> None:
    """Age between warn-fraction and threshold => exit 2 (early warning)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        # 60% of the 6h threshold = 3.6h of silence on okx.
        _make_db(db, liq_okx_ms=NOW - int(0.6 * 6 * 3600_000),
                 liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "WARN" in r.stdout


def test_coinalyze_skipped_by_default_gated_with_flag() -> None:
    """Coinalyze is verify-only (no persisted evidence) — skipped by default,
    but --gate-coinalyze treats missing evidence as a failure."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKIPPED" in r.stdout
        assert "liquidation_coinalyze_check" in r.stdout
        r2 = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp), "--gate-coinalyze"])
        assert r2.returncode == 1, r2.stdout + r2.stderr


def test_contracts_exclude_not_contracted_feeds() -> None:
    """The check uses feed_silence_contracts: binance_perp / liquidation_binance
    are not contracted in this deployment, so no DB evidence is needed for them."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "binance_perp" not in r.stdout
        assert "liquidation_binance" not in r.stdout

# ---------------------------------------------------------------------------
# Per-symbol candle freshness / backlog
# ---------------------------------------------------------------------------


def test_stale_candle_backlog_exits_one() -> None:
    """A 1m candle older than the max age is a data backlog => exit 1."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000,
                 candle_ms=NOW - 3600_000,  # 1h old > 5 min max
                 candle_15m_ms=NOW - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "candles_1m" in r.stdout
        assert "FAIL" in r.stdout


def test_missing_symbol_candles_fail() -> None:
    """A trading symbol with no candle evidence is a backlog signal."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000,
                 candle_ms=NOW - 30_000, candle_15m_ms=NOW - 60_000)
        # Delete SOL candles -> SOL fails the check.
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM candles_1m WHERE symbol='SOL'")
        conn.execute("DELETE FROM candles_15m WHERE symbol='SOL'")
        conn.commit()
        conn.close()
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "SOL" in r.stdout


def test_candles_only_skips_feed_contracts() -> None:
    """--candles-only (backtest path) checks only candle freshness: no feed
    evidence at all must still pass with fresh candles."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=0, funding_ms=0, candle_ms=0)
        # Insert fresh candles for every symbol; feeds left empty.
        conn = sqlite3.connect(db)
        for table, ts in (("candles_1m", _now() - 30_000), ("candles_15m", _now() - 60_000)):
            for symbol in SYMBOLS:
                conn.execute(f"INSERT INTO {table} VALUES (?, ?,1,2,0,1,10,0,100,0,5,5,100)",
                             (symbol, ts))
        conn.commit()
        conn.close()
        r = _run(["--db", db, "--candles-only"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[PASS]" in r.stdout


def test_min_latest_coverage_catches_backlog() -> None:
    """Coverage mode (backtest window end): latest candle below the requested
    end-of-window timestamp => backlog => exit 1, even with feeds fresh."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        min_latest = NOW + 2 * 3600_000  # window end 2h in the future
        r = _run(["--db", db, "--candles-only", "--min-latest-ms", str(min_latest)])
        assert r.returncode == 1, r.stdout + r.stderr


def test_min_latest_coverage_passes_historical_window() -> None:
    """Coverage mode must NOT block a historical window: old candles that
    REACH the requested (past) end-of-window pass regardless of age."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        # Candles from 2024-01-31; window end = 2024-01-31T23:59.
        old_ms = int(datetime(2024, 1, 31, 23, 59).timestamp() * 1000)
        min_latest = old_ms - 60_000
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=0, funding_ms=0, candle_ms=0)
        conn = sqlite3.connect(db)
        for table in ("candles_1m", "candles_15m"):
            for symbol in SYMBOLS:
                conn.execute(f"INSERT INTO {table} VALUES (?, ?,1,2,0,1,10,0,100,0,5,5,100)",
                             (symbol, old_ms))
        conn.commit()
        conn.close()
        r = _run(["--db", db, "--candles-only", "--min-latest-ms", str(min_latest)])
        assert r.returncode == 0, r.stdout + r.stderr


def test_missing_db_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = _run(["--db", os.path.join(tmp, "nope.db")])
        assert r.returncode == 1
        assert "not found" in r.stderr
