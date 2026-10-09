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
