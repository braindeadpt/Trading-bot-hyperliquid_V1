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
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preflight_feed_check.py"

pytestmark = pytest.mark.unit


def _make_db(path, *, liq_okx_ms, liq_bybit_ms, funding_ms, candle_ms) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE liquidation_events (symbol TEXT, timestamp_ms INTEGER, "
               "notional_usd REAL, side TEXT, source TEXT)")
    db.execute("CREATE TABLE funding_history (symbol TEXT, current REAL, "
               "predicted REAL, timestamp INTEGER)")
    db.execute("CREATE TABLE candles_1m (symbol TEXT, timestamp_ms INTEGER, "
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
    if candle_ms:
        db.execute("INSERT INTO candles_1m VALUES ('BTC', ?, 1,2,0,1,10,0,100,0,5,5,100)",
                   (candle_ms,))
    db.commit()
    db.close()


def _run(args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


NOW = int(time.time() * 1000)


def test_fresh_evidence_exits_zero() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 9_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[PASS]" in r.stdout


def test_stale_feed_fails_before_threshold_would_alert_anyway() -> None:
    """Age > max_silence (6h) => immediate fail, exit 1."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 7 * 3600_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout
        assert "FAIL" in r.stdout


def test_missing_feed_fails_immediately() -> None:
    """A contracted feed with no persisted evidence at all => fail (exit 1),
    catching a blocked feed before the silence threshold would trip."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout


def test_warn_fraction_exits_two() -> None:
    """Age between warn-fraction and threshold => exit 2 (early warning)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        # 60% of the 6h threshold = 3.6h of silence on okx.
        _make_db(db, liq_okx_ms=NOW - int(0.6 * 6 * 3600_000),
                 liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "WARN" in r.stdout


def test_coinalyze_skipped_by_default_gated_with_flag() -> None:
    """Coinalyze is verify-only (no persisted evidence) — skipped by default,
    but --gate-coinalyze treats missing evidence as a failure."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SKIPPED" in r.stdout
        assert "liquidation_coinalyze_check" in r.stdout
        r2 = _run(["--db", db, "--gate-coinalyze"])
        assert r2.returncode == 1, r2.stdout + r2.stderr


def test_contracts_exclude_not_contracted_feeds() -> None:
    """The check uses feed_silence_contracts: binance_perp / liquidation_binance
    are not contracted in this deployment, so no DB evidence is needed for them."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=NOW - 30_000)
        r = _run(["--db", db])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "binance_perp" not in r.stdout
        assert "liquidation_binance" not in r.stdout


def test_missing_db_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = _run(["--db", os.path.join(tmp, "nope.db")])
        assert r.returncode == 1
        assert "not found" in r.stderr
