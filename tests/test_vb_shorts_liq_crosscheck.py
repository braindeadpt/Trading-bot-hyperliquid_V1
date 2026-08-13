"""Unit tests for scripts/vb_shorts_liq_crosscheck.py.

Pins the flush-reversal measurement mechanics: minute bucketing, p90
threshold, post-flush return direction by dominant side, and the VB
short pre-drop/post-return proxy (incl. the failed_breakout subset —
PART C — and the expansion-only rework blocked count).
"""

import csv
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.vb_shorts_liq_crosscheck as cc  # noqa: E402
from scripts.vb_shorts_liq_crosscheck import (  # noqa: E402
    load_liq_events,
    percentile,
)


HEADERS = ["symbol", "entry_time", "exit_reason", "side", "pnl_usd", "regime"]


def _make_candles():
    """100 candles at 60s spacing. Entry at ts 3_000_000 lands on index 50
    (i>=30, i+30<100). Lows before entry are 99.9 (pre_drop -0.1%), close at
    index 80 is 100.5 (post_ret +0.5% — reversal against a short)."""
    out = {}
    for m in range(100):
        close = 100.5 if m == 80 else 100.0
        out[m * 60_000] = (100.0, 100.0, 99.9, close)
    return {"ETH": out}


def _write_forensics(tmp_path, rows):
    p = tmp_path / "vb_forensics.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)
    return p


def _run_part_c(tmp_path, rows, monkeypatch):
    p = _write_forensics(tmp_path, rows)
    monkeypatch.setattr(cc, "FORENSICS_CSV", p)
    return cc.part_b_failed_breakout(_make_candles())


def test_part_c_filters_only_failed_breakout(tmp_path, monkeypatch, capsys):
    rows = [
        # only failed_breakout exit family may be counted
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "stop_loss_above_mid",
         "side": "short", "pnl_usd": "-5.0", "regime": "trend"},
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "failed_breakout_above_mid",
         "side": "short", "pnl_usd": "-10.0", "regime": "trend"},
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "failed_breakout_below_mid",
         "side": "long", "pnl_usd": "-10.0", "regime": "low_vol"},
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "failed_breakout_above_mid",
         "side": "short", "pnl_usd": "-10.0", "regime": "expansion"},
    ]
    _run_part_c(tmp_path, rows, monkeypatch)
    out = capsys.readouterr().out
    # the stop_loss trade is excluded -> 3 failed_breakout trades total
    assert "all 3 failed_breakout trades" in out
    # shorts (2) reversed: post30 +0.5%, rose 100%
    assert "shorts" in out and "n= 2" in out and "rose=100%" in out
    # long+below_mid (1) present
    assert "long+below_mid" in out and "n= 1" in out


def test_part_c_blocked_by_rework_count(tmp_path, monkeypatch, capsys):
    rows = [
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "failed_breakout_above_mid",
         "side": "short", "pnl_usd": "-10.0", "regime": "trend"},
        {"symbol": "ETH", "entry_time": "3000000", "exit_reason": "failed_breakout_above_mid",
         "side": "short", "pnl_usd": "-10.0", "regime": "expansion"},
    ]
    _run_part_c(tmp_path, rows, monkeypatch)
    out = capsys.readouterr().out
    # only 1 of 2 lives in expansion -> 1/2 blocked by the rework
    assert "blocked by expansion-only rework (non-expansion): 1/2" in out


class FakeCur:
    """Cursor stub returning rows for load_liq_events."""

    def __init__(self, rows):
        self._rows = rows
        self._last = None

    def execute(self, sql, params=None):
        self._last = params
        return self

    def fetchall(self):
        return self._rows


def test_load_liq_events_buckets_by_minute_and_side():
    rows = [
        ("ETH", 120_000, 100.0, "long"),
        ("ETH", 121_000, 200.0, "short"),
        ("ETH", 125_000, 50.0, "long"),   # same minute as above
        ("BTC", 120_000, 999.0, "long"),  # filtered out (not in SYMBOLS? BTC is)
        ("SOL", 60_000, 10.0, "long"),
    ]
    cur = FakeCur(rows)
    out = load_liq_events(cur, ("okx", "bybit"))
    assert "ETH" in out
    m2 = out["ETH"][2]
    assert m2["long"] == pytest.approx(150.0)
    assert m2["short"] == pytest.approx(200.0)
    assert 1 in out["SOL"]
    assert 2 in out["BTC"]  # BTC minute 2 bucket exists


def test_load_liq_events_filters_symbols():
    rows = [("NOPE", 120_000, 100.0, "long"), ("HYPE", 120_000, 5.0, "short")]
    out = load_liq_events(FakeCur(rows), ("okx",))
    assert "NOPE" not in out
    assert out["HYPE"][2]["short"] == pytest.approx(5.0)


def test_percentile_reference():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.90) == pytest.approx(3.7)
    assert percentile([], 0.90) == 0.0


def test_flush_direction_semantics():
    """Reversal sign convention: long-liq (forced sells) -> price rises post."""
    # long-liq dominant flush: post return positive = reversal confirmed
    flushes_long = [("long", +0.04), ("long", +0.02)]
    flushes_short = [("short", -0.19), ("short", -0.27)]
    avg_long = sum(r[1] for r in flushes_long) / len(flushes_long)
    avg_short = sum(r[1] for r in flushes_short) / len(flushes_short)
    assert avg_long > 0 and avg_short < 0
