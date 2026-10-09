"""Tests for the executed-trade section of scripts/research/jev_eval.py.

The bracket simulator + random baseline power the preregistered JevJudge
kill read (docs/PREREGISTER_JEV_OOS_KILL_2026-10-08.md) — correctness here
is evidence correctness.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.research.jev_eval import (  # noqa: E402
    GEOMETRY_BOUNDARY_MS,
    JEV_FEE_RT,
    _bracket_from_metadata,
    _independent_trades,
    _percentile,
    random_baseline,
    simulate_bracket,
)

pytestmark = [pytest.mark.unit]


def _tr(symbol="BTC", entry=100, exit_=200, entry_px=100.0, side="long",
        pnl=0.0, meta=None):
    return {
        "symbol": symbol, "side": side, "entry_price": entry_px,
        "entry_time": entry, "exit_time": exit_, "pnl_pct": pnl,
        "signal_metadata": meta,
    }


# ── independence (same rule as the shadow evaluator) ─────────────────────


def test_independent_trades_overlap_collapses() -> None:
    trades = [
        _tr(entry=0, exit_=100),
        _tr(entry=50, exit_=150),   # overlaps first — drops
        _tr(entry=200, exit_=300),  # counts
    ]
    kept = _independent_trades(trades)
    assert len(kept) == 2
    assert kept[0]["entry_time"] == 0 and kept[1]["entry_time"] == 200


def test_independent_trades_per_symbol() -> None:
    trades = [
        _tr(symbol="BTC", entry=0, exit_=100),
        _tr(symbol="ETH", entry=50, exit_=150),   # different symbol — counts
    ]
    assert len(_independent_trades(trades)) == 2


def test_independent_trades_boundary_touch_counts() -> None:
    trades = [_tr(entry=0, exit_=100), _tr(entry=100, exit_=200)]
    assert len(_independent_trades(trades)) == 2


# ── bracket recovery from signal_metadata ────────────────────────────────


def test_bracket_recovery_uses_engine_atr_fallback() -> None:
    # engine stores atr_pct = stop_loss_pct / 2 → sl = 2*atr = 0.68%
    sl, tp = _bracket_from_metadata('{"atr_pct": 0.0034}')
    assert sl == pytest.approx(0.0068)
    assert tp == pytest.approx(0.0068)          # tp_r_mult = 1.0


def test_bracket_recovery_floors_at_half_pct() -> None:
    sl, tp = _bracket_from_metadata('{"atr_pct": 0.001}')
    assert sl == pytest.approx(0.005)
    assert tp == pytest.approx(0.005)


def test_bracket_recovery_missing_metadata() -> None:
    sl, tp = _bracket_from_metadata(None)
    assert sl == tp == pytest.approx(0.005)


# ── bracket simulator ────────────────────────────────────────────────────


def test_simulate_bracket_long_tp() -> None:
    candles = [(60_000, 101.0, 99.9, 100.8)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(0.005 - JEV_FEE_RT)


def test_simulate_bracket_long_sl() -> None:
    candles = [(60_000, 100.2, 99.0, 99.5)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_short_sl() -> None:
    candles = [(60_000, 101.0, 99.9, 100.8)]
    r = simulate_bracket(candles, 0, 100.0, "short", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_ambiguous_candle_resolves_sl() -> None:
    candles = [(60_000, 101.0, 99.0, 100.0)]  # both barriers touched
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(-0.005 - JEV_FEE_RT)


def test_simulate_bracket_timeout_exits_at_last_close() -> None:
    candles = [(60_000, 100.2, 99.9, 100.2), (120_000, 100.3, 100.1, 100.3)]
    r = simulate_bracket(candles, 0, 100.0, "long", 0.005, 0.005)
    assert r == pytest.approx(0.003 - JEV_FEE_RT)


def test_simulate_bracket_no_candles_returns_none() -> None:
    assert simulate_bracket([], 0, 100.0, "long", 0.005, 0.005) is None


# ── random baseline ──────────────────────────────────────────────────────


def test_random_baseline_deterministic_same_seed() -> None:
    candles = {
        "BTC": [(i * 60_000 + 60_000, 100.5, 99.5, 100.1) for i in range(10)]
    }
    trades = [_tr(entry=0, exit_=600_000, meta='{"atr_pct": 0.0025}',
                  pnl=0.0)]
    for t in trades:
        t["sl_pct"], t["tp_pct"] = _bracket_from_metadata(t["signal_metadata"])
    a = random_baseline(trades, candles, n_runs=10, seed=42)
    b = random_baseline(trades, candles, n_runs=10, seed=42)
    assert a == b
    assert len(a) == 10


def test_random_baseline_coin_flip_varies_outcome() -> None:
    # Only-up candles: long wins, short loses — run totals must split into
    # distinct values, proving the side is actually randomized.
    candles = {
        "BTC": [(i * 60_000 + 60_000, 101.0, 99.8, 100.5) for i in range(10)]
    }
    trades = [_tr(entry=0, exit_=600_000, meta='{"atr_pct": 0.0025}')]
    for t in trades:
        t["sl_pct"], t["tp_pct"] = _bracket_from_metadata(t["signal_metadata"])
    runs = random_baseline(trades, candles, n_runs=20, seed=1)
    assert len(set(runs)) == 2
    assert max(runs) > 0 > min(runs)


def test_percentile_basic() -> None:
    assert _percentile([1.0, 2.0, 3.0, 4.0], 2.5) == pytest.approx(50.0)
    assert _percentile([1.0, 2.0], 99.0) == pytest.approx(100.0)
    assert _percentile([], 1.0) != _percentile([], 1.0)  # NaN


def test_geometry_boundary_is_2026_10_04_commit() -> None:
    # 96f1b15 committed 2026-10-04 14:39:40 UTC
    assert GEOMETRY_BOUNDARY_MS == 1791124780000


# ── stale_verdict rule (deviation 2026-10-09) ─────────────────────────────
# A trade opened on a verdict older than 2h at entry is flagged
# stale_verdict=1 and excluded from the kill count. Rows are never deleted.

import json as _json
import sqlite3 as _sqlite3

from scripts.research.jev_eval import (  # noqa: E402
    STALE_VERDICT_MS,
    is_stale_verdict,
    mark_stale_verdicts,
    verdict_age_ms,
)


def test_stale_verdict_ms_is_two_hours() -> None:
    assert STALE_VERDICT_MS == 2 * 3_600_000


def test_verdict_age_ms_reads_decision_ts() -> None:
    meta = _json.dumps({"jev_decision_ts_ms": 1_000})
    assert verdict_age_ms(meta, 1_000 + STALE_VERDICT_MS) == STALE_VERDICT_MS
    assert verdict_age_ms(meta, 1_500) == 500
    # No recorded verdict ts -> no age -> never stale (pre-instrumentation)
    assert verdict_age_ms("{}", 1_500) is None
    assert verdict_age_ms(None, 1_500) is None
    assert verdict_age_ms("not json", 1_500) is None


def test_is_stale_verdict_flag_and_derived() -> None:
    fresh = _json.dumps({"jev_decision_ts_ms": 1_000})
    assert is_stale_verdict(fresh, 1_000 + 60_000) is False
    assert is_stale_verdict(fresh, 1_000 + STALE_VERDICT_MS + 1) is True
    # Explicit flag wins even without derivable ts
    flagged = _json.dumps({"stale_verdict": 1})
    assert is_stale_verdict(flagged, 5_000) is True
    assert is_stale_verdict(None, 5_000) is False


def test_mark_stale_verdicts_annotates_without_deleting(tmp_path) -> None:
    db = _sqlite3.connect(str(tmp_path / "bot.db"))
    db.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, "
        "entry_time INTEGER, signal_metadata TEXT)"
    )
    stale_meta = _json.dumps({"jev_decision_ts_ms": 1_000})
    fresh_meta = _json.dumps({"jev_decision_ts_ms": 9_000})
    db.execute(
        "INSERT INTO trades VALUES (1, 'JevJudge', ?, ?)",
        (1_000 + STALE_VERDICT_MS + 60_000, stale_meta),  # verdict 2h+1m old
    )
    db.execute(
        "INSERT INTO trades VALUES (2, 'JevJudge', ?, ?)",
        (10_000, fresh_meta),                             # verdict 1s old
    )
    db.execute(
        "INSERT INTO trades VALUES (3, 'JevJudge', ?, ?)",
        (10_000, '{}'),                                    # no verdict ts
    )
    db.execute(
        "INSERT INTO trades VALUES (4, 'Other', ?, ?)",
        (1_000 + STALE_VERDICT_MS + 60_000, stale_meta),  # other strategy
    )
    db.commit()

    assert mark_stale_verdicts(db) == 1
    rows = db.execute(
        "SELECT id, signal_metadata FROM trades ORDER BY id"
    ).fetchall()
    assert len(rows) == 4  # nothing deleted
    assert _json.loads(rows[0][1])["stale_verdict"] == 1
    assert "stale_verdict" not in _json.loads(rows[1][1])
    assert _json.loads(rows[3][1]).get("stale_verdict") is None  # Other untouched
    assert mark_stale_verdicts(db) == 0  # idempotent
    db.close()


# ── duplicate_verdict rule (2026-10-09) ──────────────────────────────────
# A restart clears JevJudge's in-memory edge-trigger; the same verdict can
# then produce a second trade if the first already closed. One verdict =
# one counted trade: earliest entry per jev_decision_ts_ms counts, the rest
# are flagged duplicate_verdict=1 and excluded. Rows are never deleted.

from scripts.research.jev_eval import (  # noqa: E402
    is_duplicate_verdict,
    mark_duplicate_verdicts,
    split_duplicate_verdicts,
    verdict_decision_ts,
)


def _tmeta(ts) -> str:
    return _json.dumps({"jev_decision_ts_ms": ts})


def test_verdict_decision_ts_reads_meta() -> None:
    assert verdict_decision_ts(_tmeta(12345)) == 12345
    assert verdict_decision_ts("{}") is None
    assert verdict_decision_ts(None) is None
    assert verdict_decision_ts("not json") is None


def test_split_duplicate_verdicts_counts_first_only() -> None:
    # restart re-fire: verdict ts=100 produces trade A (closed) then trade B
    trades = [
        _tr(entry=1000, exit_=2000, meta=_tmeta(100)),
        _tr(entry=3000, exit_=4000, meta=_tmeta(100)),  # same verdict → dupe
        _tr(entry=5000, exit_=6000, meta=_tmeta(200)),  # new verdict → counts
    ]
    kept, dupes = split_duplicate_verdicts(trades)
    assert [t["entry_time"] for t in kept] == [1000, 5000]
    assert [t["entry_time"] for t in dupes] == [3000]


def test_split_duplicate_verdicts_no_ts_never_dupe() -> None:
    # pre-instrumentation rows (no jev_decision_ts_ms) can never be dupes
    trades = [_tr(entry=1000, meta=None), _tr(entry=2000, meta="{}")]
    kept, dupes = split_duplicate_verdicts(trades)
    assert len(kept) == 2 and not dupes


def test_kill_count_two_trades_same_verdict_ts_counts_one() -> None:
    """The kill read: two closed trades on one jev_decision_ts_ms count
    once — the rest feed the exclusion print, never the n."""
    trades = [
        _tr(entry=1000, exit_=2000, meta=_tmeta(100)),
        _tr(entry=3000, exit_=4000, meta=_tmeta(100)),
        _tr(entry=5000, exit_=6000, meta=_tmeta(100)),
    ]
    kept, dupes = split_duplicate_verdicts(trades)
    assert len(dupes) == 2
    assert len(_independent_trades(kept)) == 1


def test_mark_duplicate_verdicts_annotates_without_deleting(tmp_path) -> None:
    db = _sqlite3.connect(str(tmp_path / "bot.db"))
    db.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy TEXT, "
        "entry_time INTEGER, signal_metadata TEXT)"
    )
    db.execute("INSERT INTO trades VALUES (1, 'JevJudge', 1000, ?)",
               (_tmeta(100),))
    db.execute("INSERT INTO trades VALUES (2, 'JevJudge', 3000, ?)",
               (_tmeta(100),))   # dupe of ts=100
    db.execute("INSERT INTO trades VALUES (3, 'JevJudge', 5000, ?)",
               (_tmeta(200),))   # different verdict
    db.execute("INSERT INTO trades VALUES (4, 'JevJudge', 6000, '{}')")
    db.execute("INSERT INTO trades VALUES (5, 'Other', 7000, ?)",
               (_tmeta(100),))   # other strategy untouched
    db.commit()

    assert mark_duplicate_verdicts(db) == 1
    rows = db.execute(
        "SELECT id, signal_metadata FROM trades ORDER BY id").fetchall()
    assert len(rows) == 5  # nothing deleted
    assert "duplicate_verdict" not in _json.loads(rows[0][1])   # earliest kept
    assert _json.loads(rows[1][1])["duplicate_verdict"] == 1    # dupe flagged
    assert "duplicate_verdict" not in _json.loads(rows[2][1])
    assert _json.loads(rows[4][1]).get("duplicate_verdict") is None
    assert is_duplicate_verdict(rows[1][1]) is True
    assert is_duplicate_verdict(rows[0][1]) is False
    assert mark_duplicate_verdicts(db) == 0  # idempotent
    db.close()
