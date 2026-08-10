"""Unit tests for scripts/backup_research_data.py (offline)."""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import backup_research_data as br


def _make_db(path: Path, *, table: str, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, v TEXT)')
        con.executemany(
            f'INSERT INTO "{table}" (v) VALUES (?)',
            [(f"r{i}",) for i in range(rows)],
        )
        con.commit()
    finally:
        con.close()


def _write_gz(path: Path, payload: bytes = b'{"ok":true}\n') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(payload)


@pytest.mark.unit
def test_snapshot_sqlite_consistent_and_verify(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, table="trade_tape", rows=7)
    before = {"trade_tape": 7}
    br.snapshot_sqlite_consistent(src, dest)
    ok, msg, b, d, a = br.verify_sqlite_copy(
        src, dest, ["trade_tape"], counts_before=before
    )
    assert ok and msg == "ok"
    assert b == d == a == {"trade_tape": 7}


@pytest.mark.unit
def test_verify_allows_live_growth_after_snapshot(tmp_path: Path) -> None:
    """Source may gain rows after the snapshot; dest stays inside [before, after]."""
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, table="trade_tape", rows=5)
    before = {"trade_tape": 5}
    br.snapshot_sqlite_consistent(src, dest)
    # Live writer continues
    con = sqlite3.connect(str(src))
    con.execute('INSERT INTO "trade_tape" (v) VALUES (?)', ("extra",))
    con.commit()
    con.close()
    ok, msg, b, d, a = br.verify_sqlite_copy(
        src, dest, ["trade_tape"], counts_before=before
    )
    assert ok and msg == "ok"
    assert d["trade_tape"] == 5
    assert a["trade_tape"] == 6


@pytest.mark.unit
def test_verify_detects_dest_below_before(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, table="trades", rows=5)
    _make_db(dest, table="trades", rows=2)  # truncated copy
    ok, msg, _, _, _ = br.verify_sqlite_copy(
        src, dest, ["trades"], counts_before={"trades": 5}
    )
    assert not ok
    assert msg == "row_count_window_mismatch"


@pytest.mark.unit
def test_gzip_integrity_ok_and_bad(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl.gz"
    bad = tmp_path / "bad.jsonl.gz"
    _write_gz(good)
    bad.write_bytes(b"not-gzip")
    assert br.gzip_integrity_ok(good) is True
    assert br.gzip_integrity_ok(bad) is False


@pytest.mark.unit
def test_incremental_l2_skips_today_and_existing(tmp_path: Path) -> None:
    src = tmp_path / "l2_src"
    dest = tmp_path / "l2_dest"
    today = date(2026, 8, 10)
    closed = src / "BTC" / "2026-08-09.jsonl.gz"
    open_today = src / "BTC" / "2026-08-10.jsonl.gz"
    _write_gz(closed, b'{"d":9}\n')
    _write_gz(open_today, b'{"d":10}\n')

    recs, errs = br.incremental_copy_l2(src, dest, today=today)
    assert errs == []
    assert len(recs) == 1
    assert recs[0].action == "copied"
    assert (dest / "BTC" / "2026-08-09.jsonl.gz").is_file()
    assert not (dest / "BTC" / "2026-08-10.jsonl.gz").exists()

    # Second pass: size match → skip
    recs2, errs2 = br.incremental_copy_l2(src, dest, today=today)
    assert errs2 == []
    assert recs2[0].action == "skipped_exists"


@pytest.mark.unit
def test_prune_keeps_failed_and_newest(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()

    def _run(name: str, tag: str, ok: bool) -> Path:
        d = runs / name
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"ok": ok, "tag": tag}), encoding="utf-8"
        )
        (d / "marker.txt").write_text(name, encoding="utf-8")
        return d

    # 4 successful monthly + 1 failed + 2 annual
    m1 = _run("2026-01-01T000000Z_monthly", "monthly", True)
    m2 = _run("2026-02-01T000000Z_monthly", "monthly", True)
    m3 = _run("2026-03-01T000000Z_monthly", "monthly", True)
    m4 = _run("2026-04-01T000000Z_monthly", "monthly", True)
    failed = _run("2026-03-15T000000Z_monthly", "monthly", False)
    a1 = _run("2025-12-31T000000Z_annual", "annual", True)
    a2 = _run("2026-12-31T000000Z_annual", "annual", True)

    actions = br.prune_retention(runs, monthly_keep=3, annual_keep=1)
    assert m1.exists() is False  # pruned (oldest monthly beyond keep=3)
    assert m2.exists() and m3.exists() and m4.exists()
    assert failed.exists()  # failed never pruned
    assert a1.exists() is False
    assert a2.exists()
    assert any(a.startswith("pruned:") for a in actions)


@pytest.mark.unit
def test_run_backup_end_to_end(tmp_path: Path) -> None:
    research = tmp_path / "hyperliquid.db"
    live = tmp_path / "bot.db"
    l2_src = tmp_path / "l2_src"
    backup_root = tmp_path / "backup"

    _make_db(research, table="trade_tape", rows=5)
    # also create empty expected tables so count set is non-empty for live
    con = sqlite3.connect(str(research))
    con.execute(
        "CREATE TABLE l2_snapshots (symbol TEXT, timestamp_ms INT, PRIMARY KEY(symbol, timestamp_ms))"
    )
    con.commit()
    con.close()

    _make_db(live, table="trades", rows=4)
    _write_gz(l2_src / "ETH" / "2026-08-01.jsonl.gz", b'{"x":1}\n')
    _write_gz(
        l2_src / "ETH" / f"{datetime.now(timezone.utc).date().isoformat()}.jsonl.gz",
        b'{"x":today}\n',
    )

    man = br.run_backup(
        backup_root=backup_root,
        research_db=research,
        live_db=live,
        l2_src=l2_src,
        tag="monthly",
        dry_run=False,
        skip_prune=True,
    )
    assert man.ok is True
    assert man.error is None
    run = Path(man.run_dir)
    assert (run / "hyperliquid.db").is_file()
    assert (run / "bot.db").is_file()
    assert (run / "manifest.json").is_file()
    assert (backup_root / "l2_books" / "ETH" / "2026-08-01.jsonl.gz").is_file()
    # today skipped
    today_name = f"{datetime.now(timezone.utc).date().isoformat()}.jsonl.gz"
    assert not (backup_root / "l2_books" / "ETH" / today_name).exists()
    loaded = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["ok"] is True
    assert loaded["l2_copied"] == 1
