"""wallet_fills_collector incremental-cursor tests.

The collector pages userFillsByTime forward from a per-wallet cursor
(wallet_fills_cursor.max_time_ms) committed in the same transaction as the
page's inserts — a pm2 SIGINT mid-pass must leave the DB resumable without
loss or duplication. The pre-cursor design refetched every wallet's full
~2000-row recent window hourly (~12 insert attempts per new row, 8.5M
rowids/day burned, and passes that never finished inside the cron hour).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "wallet_fills_collector", ROOT / "scripts" / "research" / "wallet_fills_collector.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _fill(wallet: str, tid: int, t: int, coin: str = "ETH") -> dict:
    return {
        "coin": coin, "side": "B", "px": "100.0", "sz": "1.0",
        "time": t, "crossed": True, "dir": "Open Long",
        "closedPnl": "0", "fee": "0.1", "oid": tid, "tid": tid,
        "hash": f"0x{tid:064x}", "twapId": None,
    }


def _page(wallet: str, tid_base: int, t_start: int, n: int) -> list:
    return [_fill(wallet, tid_base + i, t_start + i * 1000) for i in range(n)]


def _cursor(conn, wallet: str):
    row = conn.execute(
        f"SELECT max_time_ms FROM {m.CURSOR_TABLE} WHERE wallet=?", (wallet,)
    ).fetchone()
    return row[0] if row else None


def _run(conn, fetch):
    return m.one_pass(
        conn,
        tracked_only=True,
        fetch_fn=fetch,
        sleep_fn=lambda _s: None,
    )


@pytest.fixture
def wallets(monkeypatch):
    def _set(ws):
        monkeypatch.setattr(m, "_universe", lambda tracked_only: list(ws))
    return _set


def test_resume_after_sigint_mid_pass(tmp_path, wallets):
    """pm2 kills the pass mid-wallet: committed pages + cursors survive,
    the killed wallet resumes from its cursor — no loss, no dupes."""
    conn = m._open_db(tmp_path / "t.db")
    wallets(["w1", "w2"])

    calls = []

    def fetch_kill(w, start):
        calls.append((w, start))
        if w == "w2":
            raise KeyboardInterrupt  # pm2 SIGINT
        return _page(w, 1, 1000, 3)

    with pytest.raises(KeyboardInterrupt):
        _run(conn, fetch_kill)

    assert _cursor(conn, "w1") == 3000          # committed before the kill
    assert _cursor(conn, "w2") is None          # nothing committed for w2
    n = conn.execute(f"SELECT COUNT(*) FROM {m.TABLE}").fetchone()[0]
    assert n == 3

    # Second pass: w2 (cursor 0) sorts first and backfills from startTime=0.
    def fetch_ok(w, start):
        calls.append((w, start))
        return _page(w, 10, 4000, 2)

    stats = _run(conn, fetch_ok)
    assert stats["errors"] == 0
    w2_calls = [s for w, s in calls if w == "w2"]
    assert w2_calls[-1] == 0                     # no cursor -> full backfill
    assert _cursor(conn, "w2") == 5000
    assert _cursor(conn, "w1") == 5000


def test_overlap_refetch_creates_no_duplicates(tmp_path, wallets):
    """The 60s overlap window refetches boundary rows; UNIQUE dedups and
    the row count is stable across passes."""
    conn = m._open_db(tmp_path / "t.db")
    wallets(["w1"])

    # Rows 60s apart: after pass 1 the cursor sits at 121000, so pass 2
    # refetches [61000..] — two of the three rows land inside the overlap.
    all_rows = [
        _fill("w1", 1, 1000),
        _fill("w1", 2, 61000),
        _fill("w1", 3, 121000),
    ]

    def fetch(w, start):
        return [f for f in all_rows if f["time"] >= start]

    _run(conn, fetch)
    n1 = conn.execute(f"SELECT COUNT(*) FROM {m.TABLE}").fetchone()[0]
    _run(conn, fetch)  # pass 2: start = cursor - 60s overlap
    n2 = conn.execute(f"SELECT COUNT(*) FROM {m.TABLE}").fetchone()[0]
    assert n1 == n2 == 3


def test_new_wallet_without_cursor_full_backfill(tmp_path, wallets):
    """cursor=0 -> startTime=0 -> pages forward through full history until
    a short (<cap) page ends the walk."""
    conn = m._open_db(tmp_path / "t.db")
    wallets(["w9"])

    calls = []
    pages = [
        _page("w9", 1, 1000, m.PAGE_CAP),           # full page -> keep paging
        _page("w9", 5000, 3_000_000, 7),            # short page -> done
    ]

    def fetch(w, start):
        calls.append(start)
        return pages.pop(0) if pages else []

    stats = _run(conn, fetch)
    assert calls[0] == 0                          # backfill from the start
    assert stats["pages"] == 2
    n = conn.execute(
        f"SELECT COUNT(*) FROM {m.TABLE} WHERE wallet='w9'"
    ).fetchone()[0]
    assert n == m.PAGE_CAP + 7
    assert _cursor(conn, "w9") == 3_006_000


def test_cursor_never_regresses(tmp_path, wallets):
    """A page whose newest row predates the cursor must not move it back."""
    conn = m._open_db(tmp_path / "t.db")
    conn.execute(
        f"INSERT INTO {m.CURSOR_TABLE} (wallet, max_time_ms, updated_at_ms)"
        " VALUES ('w1', 5000, 0)"
    )
    conn.commit()
    wallets(["w1"])

    def fetch(w, start):
        return _page(w, 1, start, 2)  # rows behind the cursor

    _run(conn, fetch)
    assert _cursor(conn, "w1") == 5000


def test_schema_has_cursor_table_and_no_wallet_index(tmp_path):
    """idx_ttf_wallet_time must not return (1.33 GB measured 2026-10-06,
    dropped deliberately); the tiny side table replaces it."""
    conn = m._open_db(tmp_path / "t.db")
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )
    }
    assert m.CURSOR_TABLE in names
    assert "idx_ttf_wallet_time" not in names


def test_page_budget_bounds_backfill_per_pass(tmp_path, wallets):
    """A backfilling wallet is capped at MAX_PAGES_PER_WALLET pages/pass so
    it cannot starve the rest of the universe; the cursor resumes next pass."""
    conn = m._open_db(tmp_path / "t.db")
    wallets(["w8"])

    def fetch(w, start):
        # infinite history: always a full page
        return _page(w, int(start / 1000) + 1, int(start) + 1000, m.PAGE_CAP)

    stats = _run(conn, fetch)
    assert stats["pages"] == m.MAX_PAGES_PER_WALLET
    cur = _cursor(conn, "w8")
    assert cur is not None and cur > 0            # progress committed
