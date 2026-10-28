"""Latency-probe tests — offline, deterministic clock.

Covers: NY-window math incl. DST edges, the event rule (>=15bps/1s,
half-move delay, t0+500ms entry, t0+5s exit, -11bps), cooldown,
no-fill paths, and the readout gates.
"""
from __future__ import annotations

import datetime as dt
import time

import pytest

from src.latency_probe import spec, windows
from src.latency_probe.detect import EventDetector
from src.latency_probe.read import evaluate
from src.latency_probe.store import ProbeStore

UTC = dt.timezone.utc


def ms(y, m, d, h=0, mi=0, s=0):
    return int(dt.datetime(y, m, d, h, mi, s, tzinfo=UTC)
               .timestamp() * 1000)


# ── windows ──────────────────────────────────────────────────────────

@pytest.mark.unit
class TestWindows:
    def test_us_close_window_dst_summer(self):
        # 2026-07-15 is a Wednesday; EDT = UTC-4 -> 19:45-20:15 NY is
        # 23:45-00:15 UTC
        inside = ms(2026, 7, 15, 23, 50)
        w = windows.window_at(inside)
        assert w is not None and w[0] == "us-close"
        assert w[1] == "2026-07-15:us-close"
        assert w[2] == ms(2026, 7, 16, 0, 15)

    def test_us_close_window_est_winter(self):
        # 2026-01-14 Wed; EST = UTC-5 -> 19:45-20:15 NY is 00:45-01:15 UTC next day
        inside = ms(2026, 1, 15, 0, 50)
        w = windows.window_at(inside)
        assert w is not None and w[0] == "us-close"
        assert w[2] == ms(2026, 1, 15, 1, 15)

    def test_control_window(self):
        # 2026-10-28 Wed EDT -> 15:00-15:30 NY = 19:00-19:30 UTC
        w = windows.window_at(ms(2026, 10, 28, 19, 10))
        assert w is not None and w[0] == "control"

    def test_weekend_excluded(self):
        # 2026-10-31 is a Saturday
        assert windows.window_at(ms(2026, 10, 31, 23, 50)) is None

    def test_outside_window(self):
        assert windows.window_at(ms(2026, 10, 28, 12, 0)) is None

    def test_next_window_from_weekend(self):
        # Sat 2026-10-31 -> next is Mon 2026-11-02 control 15:00 EST
        # (DST ends Nov-1 2026 -> EST = UTC-5 -> 20:00 UTC)
        t_ms, name = windows.next_window_start(ms(2026, 10, 31, 12, 0))
        assert name == "control"
        assert t_ms == ms(2026, 11, 2, 20, 0)


# ── detector ─────────────────────────────────────────────────────────

def _det(rows):
    return EventDetector(rows.append)


@pytest.mark.unit
class TestDetector:
    PAIRS = {"BTCUSDT": ["xyz:MSTR"]}

    def _mk(self, rows):
        return EventDetector(rows.append, pairs=self.PAIRS,
                             move_bps=15.0, window_ms=1000)

    def test_event_lifecycle(self):
        rows = []
        d = self._mk(rows)
        t0 = ms(2026, 10, 28, 19, 10)
        # xyz book alive before the move
        d.on_xyz_book("xyz:MSTR", t0 - 100, 99.9, 100.1)
        # binance mid 100 -> 100.20 (20 bps) within 1 s
        d.on_bin_book("BTCUSDT", t0 - 1000, 99.9, 100.1, "s1")
        d.on_bin_book("BTCUSDT", t0, 100.1, 100.3, "s1")
        # xyz ticks at +300ms (half move = 10bps -> 100*1.001=100.10 mid)
        d.on_xyz_book("xyz:MSTR", t0 + 300, 100.05, 100.15)
        # entry tick at +600ms -> taker ask 100.25
        d.on_xyz_book("xyz:MSTR", t0 + 600, 100.15, 100.25)
        # exit tick at +5s -> mid 100.40
        d.on_xyz_book("xyz:MSTR", t0 + 5000, 100.35, 100.45)
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] == "closed"
        assert r["delay_ms"] == 300
        assert r["entry_px"] == 100.25
        assert r["exit_px"] == 100.40
        exp = (100.40 / 100.25 - 1.0) * 1e4 - spec.TAKER_COST_BPS
        assert r["gain_bps"] == pytest.approx(exp)
        assert r["direction"] == 1

    def test_below_threshold_no_event(self):
        rows = []
        d = self._mk(rows)
        t0 = ms(2026, 10, 28, 19, 10)
        d.on_bin_book("BTCUSDT", t0 - 1000, 99.9, 100.1, "s1")
        d.on_bin_book("BTCUSDT", t0, 100.0, 100.12, "s1")  # ~10 bps
        assert rows == []

    def test_short_direction_and_bid_entry(self):
        rows = []
        d = self._mk(rows)
        t0 = ms(2026, 10, 28, 19, 10)
        d.on_xyz_book("xyz:MSTR", t0 - 100, 99.9, 100.1)
        d.on_bin_book("BTCUSDT", t0 - 1000, 100.1, 100.3, "s1")
        d.on_bin_book("BTCUSDT", t0, 99.7, 99.9, "s1")   # -20 bps
        d.on_xyz_book("xyz:MSTR", t0 + 600, 99.6, 99.8)  # entry = bid
        d.on_xyz_book("xyz:MSTR", t0 + 5000, 99.3, 99.5)
        r = rows[0]
        assert r["direction"] == -1 and r["entry_px"] == 99.6
        exp = (-1) * (99.4 / 99.6 - 1.0) * 1e4 - spec.TAKER_COST_BPS
        assert r["gain_bps"] == pytest.approx(exp)

    def test_cooldown_one_event_per_bin(self):
        rows = []
        d = self._mk(rows)
        t0 = ms(2026, 10, 28, 19, 10)
        d.on_bin_book("BTCUSDT", t0 - 1000, 99.9, 100.1, "s1")
        d.on_bin_book("BTCUSDT", t0, 100.1, 100.3, "s1")
        d.on_bin_book("BTCUSDT", t0 + 100, 100.4, 100.6, "s1")  # still moving
        d.on_bin_book("BTCUSDT", t0 + 200, 100.7, 100.9, "s1")
        d.sweep(t0 + 20000)
        n = sum(1 for r in rows if r["bin_symbol"] == "BTCUSDT")
        assert n == len(self.PAIRS["BTCUSDT"])

    def test_no_fill_without_xyz_ticks(self):
        rows = []
        d = self._mk(rows)
        t0 = ms(2026, 10, 28, 19, 10)
        d.on_bin_book("BTCUSDT", t0 - 1000, 99.9, 100.1, "s1")
        d.on_bin_book("BTCUSDT", t0, 100.1, 100.3, "s1")
        d.sweep(t0 + 20000)     # xyz never ticked
        assert rows[0]["status"] == "no_fill"
        assert rows[0]["gain_bps"] is None

    def test_fanout_pairs(self):
        rows = []
        pairs = {"BTCUSDT": ["xyz:MSTR", "xyz:COIN"]}
        d = EventDetector(rows.append, pairs=pairs,
                          move_bps=15.0, window_ms=1000)
        t0 = ms(2026, 10, 28, 19, 10)
        d.on_bin_book("BTCUSDT", t0 - 1000, 99.9, 100.1, "s1")
        d.on_bin_book("BTCUSDT", t0, 100.1, 100.3, "s1")
        for x in pairs["BTCUSDT"]:
            d.on_xyz_book(x, t0 + 600, 100.0, 100.2)
            d.on_xyz_book(x, t0 + 5000, 100.4, 100.6)
        assert len(rows) == 2
        assert {r["xyz_symbol"] for r in rows} == set(pairs["BTCUSDT"])


# ── readout ──────────────────────────────────────────────────────────

def _seed(db, n_sessions, events_per):
    st = ProbeStore(str(db))
    base = ms(2026, 10, 19, 19, 0)
    for i in range(n_sessions):
        sid = f"2026-10-{19 + i}:us-close"
        st.session_open(sid, "us-close", sid.split(":")[0],
                        base + i * 86400_000)
        for j in range(events_per):
            st.event_insert({
                "session_id": sid, "t0_ms": base + i * 86400_000 + j,
                "bin_symbol": "BTCUSDT", "xyz_symbol": "xyz:MSTR",
                "direction": 1, "bin_move_bps": 20.0, "delay_ms": 480,
                "entry_px": 100.0, "exit_px": 100.3,
                "gain_bps": 8.0 + (i % 3), "status": "closed"})
        st.session_close(sid, base + i * 86400_000 + 30 * 60_000)
    st.close()


@pytest.mark.unit
class TestReadout:
    def test_in_progress_below_minimums(self, tmp_path):
        db = tmp_path / "p.db"
        _seed(db, 3, 10)
        res = evaluate(str(db))
        assert res["verdict"] == "IN_PROGRESS"
        assert res["sessions"] == 3

    def test_worth_studying(self, tmp_path):
        db = tmp_path / "p.db"
        _seed(db, 11, 10)          # 11 sessions x 10 = 110 scored, all +
        res = evaluate(str(db))
        assert res["verdict"] == "WORTH_STUDYING"
        assert res["checks"]["ci95>0"]
        assert res["checks"]["not_dominant"]

    def test_c_when_dominant_events(self, tmp_path):
        db = tmp_path / "p.db"
        st = ProbeStore(str(db))
        base = ms(2026, 10, 19, 19, 0)
        for i in range(11):
            sid = f"2026-10-{19 + i}:us-close"
            st.session_open(sid, "us-close", sid.split(":")[0],
                            base + i * 86400_000)
            for j in range(10):
                gain = 500.0 if (i == 0 and j < 2) else -1.0
                st.event_insert({
                    "session_id": sid, "t0_ms": base + j,
                    "bin_symbol": "BTCUSDT", "xyz_symbol": "xyz:MSTR",
                    "direction": 1, "bin_move_bps": 20.0,
                    "delay_ms": 480, "entry_px": 100.0,
                    "exit_px": 100.0, "gain_bps": gain,
                    "status": "closed"})
        st.close()
        res = evaluate(str(db))
        # 2 huge events carry everything -> dominance check fails
        assert res["checks"]["not_dominant"] is False
        assert res["verdict"] == "C"

    def test_no_ledger(self, tmp_path):
        res = evaluate(str(tmp_path / "missing.db"))
        assert res["verdict"] == "NO_LEDGER"
