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
        assert "past 50%" in r.stderr  # default warn-fraction (no env)


def test_warn_fraction_respects_env_threshold(monkeypatch) -> None:
    """FEED_SILENCE_WARN_FRACTION moves the preflight warn level: 40% of the
    threshold warns when the env says 0.3 (and prints "past 30%"), where the
    hardcoded 0.5 default would have passed."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - int(0.4 * 6 * 3600_000),
                 liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        env = dict(os.environ)
        env["FEED_SILENCE_WARN_FRACTION"] = "0.3"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", db,
             "--l2-dir", _make_l2_dir(tmp)],
            capture_output=True, text=True, cwd=str(ROOT), env=env,
        )
        assert r.returncode == 2, r.stdout + r.stderr
        assert "past 30%" in r.stderr


def test_warn_fraction_cli_override_beats_env(monkeypatch) -> None:
    """An explicit --warn-fraction still overrides the env threshold."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        # 40% of the 6h threshold: warns with env 0.3, but --warn-fraction
        # 0.7 is stricter-avoiding -> passes.
        _make_db(db, liq_okx_ms=NOW - int(0.4 * 6 * 3600_000),
                 liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        env = dict(os.environ)
        env["FEED_SILENCE_WARN_FRACTION"] = "0.3"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", db,
             "--l2-dir", _make_l2_dir(tmp), "--warn-fraction", "0.7"],
            capture_output=True, text=True, cwd=str(ROOT), env=env,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "[PASS]" in r.stdout


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


def test_self_produced_feed_never_blocks_boot() -> None:
    """l2_book_recording is SELF-PRODUCED (the bot writes it; evidence only
    exists while it runs). After any downtime its evidence is stale by
    definition — the boot gate must NOT deadlock on it: no L2 evidence at all
    still exits 0 when every external feed is fresh (2026-08-14 audit:
    restart after long downtime without --skip-preflight)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        # l2 dir that does NOT exist -> no evidence at all for the
        # self-produced feed.
        r = _run(["--db", db, "--l2-dir", os.path.join(tmp, "no_l2_books")])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "l2_book_recording" in r.stdout
        assert "SELF-PRODUCED" in r.stdout


def test_self_produced_stale_evidence_reported_but_never_gated() -> None:
    """Even a *very* stale L2 recording (10+ minutes past its 2m runtime
    threshold) is reported with its age but never counted as a failure — the
    exact case that blocked boot before (bot down > 2 min)."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        l2 = Path(tmp) / "l2_books"
        btc = l2 / "BTC"
        btc.mkdir(parents=True, exist_ok=True)
        # mtime 10 minutes ago — far past the 120s runtime threshold.
        old = btc / "2026-01-01.jsonl.gz"
        old.write_text("{}\n", encoding="utf-8")
        old_ts = time.time() - 600
        os.utime(old, (old_ts, old_ts))
        r = _run(["--db", db, "--l2-dir", str(l2)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "SELF-PRODUCED" in r.stdout
        assert "FAIL" not in r.stdout


def test_external_feed_missing_still_blocks_boot_with_self_produced_stale() -> None:
    """The self-produced exemption must NOT weaken the original protection:
    a genuinely missing EXTERNAL feed (liquidation_okx) still fails, even
    when the L2 evidence is stale too."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", os.path.join(tmp, "no_l2_books")])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout
        assert "FAIL" in r.stdout

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


# ---------------------------------------------------------------------------
# Stale vs dead — downtime-aware classification of over-threshold feeds
# (the --skip-preflight-by-default fix; preserves the fstream lesson)
# ---------------------------------------------------------------------------


def _make_old_l2_dir(tmp: str, age_ms: int) -> str:
    """L2 evidence aged like the rest of a post-downtime DB.

    The probe file's mtime IS bot-life evidence (l2_book_recording participates
    in the downtime inference) — after a real stop it is as old as everything
    else, NOT fresh.
    """
    d = Path(tmp) / "l2_books" / "BTC"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "old.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    ts = age_ms / 1000.0
    os.utime(f, (ts, ts))
    return str(Path(tmp) / "l2_books")


def test_downtime_tolerance_is_module_constant_not_config() -> None:
    """The grace must stay a module constant: a config key would drift the
    Fase-10 config_hash manifest. Pins the documented value (300s)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pfc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DOWNTIME_TOLERANCE_SEC == 300.0


def test_downtime_tolerance_single_source_of_truth() -> None:
    """DOWNTIME_TOLERANCE_SEC must be defined in exactly ONE place
    (src/data/market_data_health.py) and imported everywhere else — two
    independent constants that must agree by definition will eventually
    diverge. preflight_feed_check.py imports it rather than redefining it,
    so the two are literally the same object."""
    from src.data.market_data_health import DOWNTIME_TOLERANCE_SEC as health_value

    import importlib.util

    spec = importlib.util.spec_from_file_location("pfc2", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.DOWNTIME_TOLERANCE_SEC == health_value
    assert mod.DOWNTIME_TOLERANCE_SEC is health_value

    src = SCRIPT.read_text(encoding="utf-8")
    assert "DOWNTIME_TOLERANCE_SEC =" not in src, (
        "preflight_feed_check.py must import DOWNTIME_TOLERANCE_SEC from "
        "src.data.market_data_health, never redefine it"
    )


def test_feed_dead_while_bot_ran_still_blocks_boot() -> None:
    """fstream protection intact: liquidation_okx 7h old while OTHER evidence
    is 30s old -> the bot was alive 30s ago, so okx was ALREADY dead during
    uptime (7h >> downtime + 300s grace) -> exit 1."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 7 * 3600_000, liq_bybit_ms=NOW - 30_000,
                 funding_ms=NOW - 30_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout
        assert "FAIL" in r.stdout
        assert "STALE-SINCE-DOWNTIME" not in r.stdout


def test_all_feeds_stale_consistent_with_downtime_do_not_block() -> None:
    """Bot off for ~8h: EVERY feed's age matches the downtime -> all classified
    stale-since-downtime, zero failures -> exit 2 (warnings), boot proceeds.
    Candle max-ages are raised via their CLI flags (they are CLI-tunable on
    purpose) so the candle backlog check doesn't mask the feed semantics."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        old = NOW - 8 * 3600_000
        _make_db(db, liq_okx_ms=old - 60_000, liq_bybit_ms=old - 90_000,
                 funding_ms=old - 30_000, candle_ms=old,
                 candle_15m_ms=old - 120_000)
        r = _run(["--db", db, "--l2-dir", _make_old_l2_dir(tmp, old),
                  "--candle-1m-max-age-sec", "36000",
                  "--candle-15m-max-age-sec", "36000"])
        assert r.returncode == 2, r.stdout + r.stderr
        assert "STALE-SINCE-DOWNTIME" in r.stdout
        assert "FAIL" not in r.stdout
        assert "stale-since-downtime" in r.stderr
        assert "boot proceeds" in r.stderr


def test_mixed_case_dead_feed_blocks_while_stale_ones_warn() -> None:
    """One feed dead during uptime (bybit 30h old vs ~8h downtime) plus one
    stale-by-downtime (okx ~8h) -> the dead one fails (exit 1) and the stale
    one is still reported for visibility."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        old = NOW - 8 * 3600_000
        dead = NOW - 30 * 3600_000
        _make_db(db, liq_okx_ms=old - 60_000, liq_bybit_ms=dead,
                 funding_ms=old - 30_000, candle_ms=old,
                 candle_15m_ms=old - 120_000)
        r = _run(["--db", db, "--l2-dir", _make_old_l2_dir(tmp, old),
                  "--candle-1m-max-age-sec", "36000",
                  "--candle-15m-max-age-sec", "36000"])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_bybit" in r.stdout          # dead while running
        assert "FAIL" in r.stdout
        assert "STALE-SINCE-DOWNTIME" in r.stdout       # okx explained by off


def test_stale_since_downtime_marked_in_json_report() -> None:
    """The JSON report carries the new status + flag so the boot wiring and
    humans see WHY the feed didn't block."""
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        old = NOW - 8 * 3600_000
        _make_db(db, liq_okx_ms=old - 60_000, liq_bybit_ms=old - 90_000,
                 funding_ms=old - 30_000, candle_ms=old,
                 candle_15m_ms=old - 120_000)
        r = _run(["--db", db, "--l2-dir", _make_old_l2_dir(tmp, old), "--json",
                  "--candle-1m-max-age-sec", "36000",
                  "--candle-15m-max-age-sec", "36000"])
        assert r.returncode == 2, r.stdout + r.stderr
        report = _json.loads(r.stdout)
        feeds = report["feeds"]
        stale = [f for f, st in feeds.items()
                 if st.get("stale_since_downtime")]
        assert stale, _json.dumps(feeds, indent=2)[:800]
        assert all(st["status"] == "stale-since-downtime"
                   for f, st in feeds.items() if f in stale)
        assert all(st["status"] != "fail" for st in feeds.values())


def test_out_writes_json_report_file() -> None:
    """--out persists the same JSON report to a file — the boot wiring uses
    this so the dashboard feed panel can show the boot's verdict per feed."""
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=NOW - 5_000, liq_bybit_ms=NOW - 5_000,
                 funding_ms=NOW - 5_000, candle_ms=_now() - 30_000,
                 candle_15m_ms=_now() - 60_000)
        out = os.path.join(tmp, "preflight_last.json")
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp), "--out", out])
        assert r.returncode == 0, r.stdout + r.stderr
        report = _json.loads(Path(out).read_text(encoding="utf-8"))
        # Boot context the dashboard needs to explain the verdicts:
        assert report["downtime_sec"] >= 0
        assert report["last_alive_ms"] and report["now_ms"]
        feeds = report["feeds"]
        assert feeds["funding_hl"]["status"] == "ok"
        assert feeds["l2_book_recording"]["status"] == "self-produced"
        assert feeds["liquidation_coinalyze_check"]["status"] == "skipped"


def test_out_report_carries_stale_since_downtime_flags() -> None:
    """A post-downtime run persists per-feed stale_since_downtime + the
    inferred downtime, so the dashboard can label feeds stale@boot."""
    import json as _json

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        old = NOW - 8 * 3600_000
        _make_db(db, liq_okx_ms=old - 60_000, liq_bybit_ms=old - 90_000,
                 funding_ms=old - 30_000, candle_ms=old,
                 candle_15m_ms=old - 120_000)
        out = os.path.join(tmp, "preflight_last.json")
        r = _run(["--db", db, "--l2-dir", _make_old_l2_dir(tmp, old),
                  "--out", out,
                  "--candle-1m-max-age-sec", "36000",
                  "--candle-15m-max-age-sec", "36000"])
        assert r.returncode == 2, r.stdout + r.stderr
        report = _json.loads(Path(out).read_text(encoding="utf-8"))
        assert report["downtime_sec"] > 3600
        stale = [f for f, st in report["feeds"].items()
                 if st.get("stale_since_downtime")]
        assert stale, _json.dumps(report["feeds"], indent=2)[:800]
        assert all(report["feeds"][f]["status"] == "stale-since-downtime"
                   for f in stale)
        assert report["feeds"][stale[0]]["stale_since_downtime"] is True


def test_empty_db_no_downtime_invented_unchanged_behavior() -> None:
    """No evidence at all (first run / empty DB): the classic gate applies
    unchanged — missing feeds fail (exit 1), no downtime is invented, so a
    genuinely dead feed can never hide behind the downtime classification."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bot.db")
        _make_db(db, liq_okx_ms=0, liq_bybit_ms=0, funding_ms=0, candle_ms=0)
        r = _run(["--db", db, "--l2-dir", _make_l2_dir(tmp)])
        assert r.returncode == 1, r.stdout + r.stderr
        assert "liquidation_okx" in r.stdout
        assert "STALE-SINCE-DOWNTIME" not in r.stdout
        assert "stale-since-downtime" not in r.stderr
