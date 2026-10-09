"""carry_shadow feed-silence degradation rules (src/dashboard/web.py).

Split from test_carry_shadow.py: these assert on the web-layer
degradation semantics, which ship in a separate commit from the daemon
telemetry (web.py deploys only with an authorized bot restart).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.research.carry_shadow.ledger import Ledger

pytestmark = pytest.mark.unit


def _patch_carry_db(monkeypatch, db_path) -> None:
    import src.dashboard.web as web
    monkeypatch.setattr(web, "_CARRY_SHADOW_DB", Path(db_path))


def test_ops_degrades_on_stale_books_not_liveness(tmp_path, monkeypatch) -> None:
    """Heartbeat fresh but open-episode books stale -> degraded
    (the 14:02 flap case: process alive, tape dead)."""
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.meta_set("heartbeat_ms", str(now))
    led.meta_set("subs_active", "76")
    led.meta_set("subs_expected", "76")
    led.meta_set("book_age_ms", str(90_000))          # global (informational)
    led.meta_set("book_age_open_eps_ms", str(90_000)) # open eps stale -> red
    led.meta_set("reconnects_1h", "3")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    st = web._carry_shadow_status()
    assert st["row"]["degraded"] is True
    assert any("open-episode books" in r for r in st["degraded_reasons"])


def test_ops_thin_book_stale_but_open_eps_fresh_not_degraded(
        tmp_path, monkeypatch) -> None:
    """A quiet thin book (e.g. @243 idle >60s) must NOT degrade the row
    when the books of open episodes are fresh — global max is context."""
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.meta_set("heartbeat_ms", str(now))
    led.meta_set("subs_active", "76")
    led.meta_set("subs_expected", "76")
    led.meta_set("book_age_ms", str(300_000))         # one thin book idle
    led.meta_set("book_age_med_ms", str(2_000))       # median fine
    led.meta_set("book_age_open_eps_ms", str(2_000))  # open eps fresh
    led.meta_set("ws_connected", "1")
    led.meta_set("ws_down_ms", "0")
    led.meta_set("reconnects_1h", "2")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    st = web._carry_shadow_status()
    assert st["row"]["degraded"] is False
    assert st["book_age_ms"] == 300_000               # still surfaced


def test_ops_ws_down_over_30s_degrades(tmp_path, monkeypatch) -> None:
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.meta_set("heartbeat_ms", str(now))
    led.meta_set("subs_active", "76")
    led.meta_set("subs_expected", "76")
    led.meta_set("ws_connected", "0")
    led.meta_set("ws_down_ms", "45000")
    led.meta_set("reconnects_1h", "2")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    st = web._carry_shadow_status()
    assert st["row"]["degraded"] is True
    assert any("ws down" in r for r in st["degraded_reasons"])


def test_ops_absent_freshness_keys_do_not_degrade(tmp_path, monkeypatch) -> None:
    """Pre-telemetry daemon: only heartbeat_age/dead degrade; the absent
    keys still surface as metrics_missing so the blind spot is visible."""
    led = Ledger(str(tmp_path / "cs.db"))
    led.meta_set("heartbeat_ms", str(int(time.time() * 1000)))
    led.meta_set("subs_active", "76")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    st = web._carry_shadow_status()
    assert st["row"]["degraded"] is False
    assert st["book_age_ms"] is None
    assert "metrics_missing" in st["degraded_reasons"]


def test_ops_degrades_on_subs_and_reconnect_storm(tmp_path, monkeypatch) -> None:
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.meta_set("heartbeat_ms", str(now))
    led.meta_set("subs_active", "30")
    led.meta_set("subs_expected", "76")
    led.meta_set("reconnects_1h", "9")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    st = web._carry_shadow_status()
    assert st["row"]["degraded"] is True
    assert any("subs" in r for r in st["degraded_reasons"])
    assert any("reconnects" in r for r in st["degraded_reasons"])


# ── CarryA1 hypothesis row (sealed by construction: counts + §6 only) ────

def _mk_a1_db(tmp_path) -> Path:
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.episode_open({"pair": "X/USDC", "perp": "X", "pm_branch": 1,
                      "state": "open", "entry_decision_ms": now - 3_600_000,
                      "opened_ms": now - 3_500_000})
    led.episode_open({"pair": "Y/USDC", "perp": "Y", "pm_branch": 1,
                      "state": "closed",
                      "entry_decision_ms": now - 7_200_000,
                      "opened_ms": now - 7_100_000,
                      "closed_ms": now - 100_000,
                      "realized_pnl": 0.001, "cum_f": 0.0005,
                      "cost_bps": 9.0})
    led.episode_open({"pair": "Z/USDC", "perp": "Z", "pm_branch": 1,
                      "state": "aborted",
                      "entry_decision_ms": now - 1_800_000,
                      "closed_ms": now - 900_000,
                      "close_reason": "unlegged_unwind",
                      "cost_bps": 8.0, "gap_unverified": 1})
    # entry fills for ep2 (inside its pending window) + exit fills
    led.record_fill("spot", True, 30.0, now - 7_150_000)
    led.record_fill("perp", True, 60.0, now - 7_140_000)
    led.record_fill("spot", False, None, now - 1_700_000)   # ep3 entry miss
    led.record_fill("perp", True, 40.0, now - 1_690_000)    # ep3 entry fill
    led.record_fill("spot", True, None, now - 90_000)       # ep1 exit ctx
    led.event("unlegged_unwind", {"pair": "Z/USDC"}, episode_id=3,
              ts_ms=now - 900_000)
    day = time.strftime("%Y%m%d", time.gmtime())
    led.meta_set(f"gap_s:{day}:X", "1200.0")
    led.meta_set("dead", "")
    led.close()
    return tmp_path / "cs.db"


def test_carry_a1_row_counts_and_kill_inputs(tmp_path, monkeypatch) -> None:
    _patch_carry_db(monkeypatch, _mk_a1_db(tmp_path))
    import src.dashboard.web as web
    row = web._carry_a1_hypothesis(int(time.time() * 1000))
    assert row["id"] == "CarryA1::shadow"
    assert row["state"] == "confirmação"
    assert row["indep_n"] == 1 and row["target"] == 10
    assert row["expiry_ms"] is not None and row["review_ms"] is not None
    d = row["detail"]
    # fill gate mirrored per leg over all rows (no entry/exit split)
    assert d["fill"]["spot"] == {"n": 3, "filled": 2}
    assert d["fill"]["perp"] == {"n": 2, "filled": 2}
    assert d["states"] == {"open": 1, "closed": 1, "aborted": 1}
    assert d["liquidations"] == 0 and d["deleverages"] == 0
    assert d["unlegged_unwinds"] == 1
    assert d["gap_unverified_eps"] == 1
    assert d["gap_today"][0]["over_1pct"] is True   # 1200s > 864s
    assert d["cost_check"] == "pendente (1/5)"      # <5 closed -> unarmed


def test_carry_a1_row_never_emits_pnl_or_returns(tmp_path, monkeypatch) -> None:
    """Sealed by construction: the payload carries counts and §6 inputs
    only — no net bps, ann_ret, per-episode PnL or funding fields, and no
    §12 sensitivity fields."""
    import json as _json
    _patch_carry_db(monkeypatch, _mk_a1_db(tmp_path))
    import src.dashboard.web as web
    blob = _json.dumps(web._carry_a1_hypothesis(int(time.time() * 1000)))
    for banned in ("net_bps", "ann_ret", "realized_pnl", "cum_f",
                   "pnl", "funding", "fill_frac", "crossing_size",
                   "proxy_filled", "weighted"):
        assert banned not in blob


def test_carry_a1_missing_db_degrades_to_dash_row(tmp_path, monkeypatch) -> None:
    _patch_carry_db(monkeypatch, tmp_path / "absent.db")
    import src.dashboard.web as web
    row = web._carry_a1_hypothesis(int(time.time() * 1000))
    assert row["id"] == "CarryA1::shadow"
    assert row["state"] == "—" and row["indep_n"] is None
    assert row["detail"] is None


def test_carry_a1_verdict_comes_from_daemon_dead_flag(
        tmp_path, monkeypatch) -> None:
    """The kill verdict is the daemon's alone (meta['dead'] written by
    _die). The dashboard never recomputes the §6 gates — a liquidated
    episode without the flag stays 'confirmação', the flag flips it."""
    led = Ledger(str(tmp_path / "cs.db"))
    now = int(time.time() * 1000)
    led.episode_open({"pair": "X/USDC", "perp": "X", "pm_branch": 1,
                      "state": "liquidated",
                      "entry_decision_ms": now - 3_600_000,
                      "close_reason": "gap_kill"})
    led.meta_set("dead", "gap_kill")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    row = web._carry_a1_hypothesis(now)
    assert row["state"] == "veredicto C — kill"
    assert row["dead_reason_type"] == "gap_kill"
    assert row["detail"]["liquidations"] == 1


def test_carry_a1_dead_reason_never_leaks_numbers(
        tmp_path, monkeypatch) -> None:
    """die() reasons carry live numbers — 'edge:12.3<3x cost:4.0' leaks
    the net edge. The payload must surface only the type ('edge'), and
    'fill_rate:spot' may keep the leg but never the rate."""
    import json as _json
    led = Ledger(str(tmp_path / "cs.db"))
    led.meta_set("dead", "edge:12.3<3x cost:4.0")
    led.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs.db")
    import src.dashboard.web as web
    row = web._carry_a1_hypothesis(int(time.time() * 1000))
    assert row["state"] == "veredicto C — kill"
    assert row["dead_reason_type"] == "edge"
    blob = _json.dumps(row)
    assert "12.3" not in blob and "4.0" not in blob
    # fill_rate keeps the leg, strips the rate
    led2 = Ledger(str(tmp_path / "cs2.db"))
    led2.meta_set("dead", "fill_rate:spot:0.12<0.4")
    led2.close()
    _patch_carry_db(monkeypatch, tmp_path / "cs2.db")
    row2 = web._carry_a1_hypothesis(int(time.time() * 1000))
    assert row2["dead_reason_type"] == "fill_rate:spot"
    assert "0.12" not in _json.dumps(row2)
