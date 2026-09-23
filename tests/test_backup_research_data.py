"""Unit tests for scripts/ops/backup_research_data.py (offline)."""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts.ops import backup_research_data as br


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
def test_verify_stages_copy_and_cleans_up(tmp_path: Path) -> None:
    """Dest bytes are verified via a staged copy; the staging dir is removed."""
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    staging_parent = tmp_path / "stage"
    _make_db(src, table="trade_tape", rows=9)
    br.snapshot_sqlite_consistent(src, dest)
    ok, msg, _, d, _ = br.verify_sqlite_copy(
        src,
        dest,
        ["trade_tape"],
        counts_before={"trade_tape": 9},
        staging_parent=staging_parent,
    )
    assert ok and msg == "ok" and d == {"trade_tape": 9}
    assert not list(staging_parent.iterdir())


@pytest.mark.unit
def test_verify_staging_detects_corrupt_dest(tmp_path: Path) -> None:
    """Corruption on dest must still fail when verified through staging."""
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, table="trades", rows=5)
    _make_db(dest, table="trades", rows=2)
    ok, msg, _, _, _ = br.verify_sqlite_copy(
        src,
        dest,
        ["trades"],
        counts_before={"trades": 5},
        staging_parent=tmp_path / "stage",
    )
    assert not ok
    assert msg == "row_count_window_mismatch"


@pytest.mark.unit
def test_verify_falls_back_when_staging_fails(tmp_path: Path) -> None:
    """An unusable staging parent falls back to in-place verification."""
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    blocker = tmp_path / "blocker"  # a file, not a dir -> mkdtemp fails
    blocker.write_bytes(b"x")
    _make_db(src, table="trades", rows=4)
    br.snapshot_sqlite_consistent(src, dest)
    ok, msg, _, d, _ = br.verify_sqlite_copy(
        src,
        dest,
        ["trades"],
        counts_before={"trades": 4},
        staging_parent=blocker,
    )
    assert ok and msg == "ok" and d == {"trades": 4}


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
    # also create expected evidence/count tables
    con = sqlite3.connect(str(research))
    con.execute(
        "CREATE TABLE l2_snapshots (symbol TEXT, timestamp_ms INT, PRIMARY KEY(symbol, timestamp_ms))"
    )
    con.execute("CREATE TABLE shadow_decisions (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute('INSERT INTO shadow_decisions (v) VALUES ("s1")')
    con.execute("CREATE TABLE jev_decisions (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute('INSERT INTO jev_decisions (v) VALUES ("j1")')
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
        min_research_bytes=1,  # test DB is a few KB — guard tested separately
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
    research_rec = next(
        d for d in loaded["databases"] if d["name"] == "hyperliquid.db"
    )
    assert research_rec["row_counts"]["shadow_decisions"] == 1
    assert research_rec["row_counts"]["jev_decisions"] == 1
    assert research_rec["counts_match"] is True


# ── Ghost-source guard (Task 6 / A2) ────────────────────────────────────────

@pytest.mark.unit
def test_ghost_db_refused_and_no_ok_manifest(tmp_path: Path) -> None:
    """A 240KB DB with 0 shadow_decisions must fail loudly — never ok."""
    ghost = tmp_path / "hyperliquid.db"
    live = tmp_path / "bot.db"
    _make_db(ghost, table="shadow_decisions", rows=0)  # schema exists, 0 rows
    _make_db(live, table="trades", rows=2)
    backup_root = tmp_path / "backup"

    man = br.run_backup(
        backup_root=backup_root,
        research_db=ghost,
        live_db=live,
        l2_src=tmp_path / "no_l2",
        tag="monthly",
        dry_run=False,
        skip_prune=True,
        min_research_bytes=1,  # size gate passes; zero-evidence gate fires
    )
    assert man.ok is False
    assert man.error and "suspicious_source" in man.error
    assert str(ghost) in man.error  # message names the path that was read
    run = Path(man.run_dir)
    # Ghost DB was NOT propagated into the backup tree
    assert not (run / "hyperliquid.db").exists()
    # Live DB still backed up — partial value preserved
    assert (run / "bot.db").is_file()
    loaded = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["ok"] is False


@pytest.mark.unit
def test_tiny_source_file_refused_by_size_gate(tmp_path: Path) -> None:
    small = tmp_path / "hyperliquid.db"
    _make_db(small, table="shadow_decisions", rows=9)
    err = br.guard_research_source(small, min_bytes=64 * 1024 * 1024)
    assert err is not None
    assert "suspicious_source" in err and str(small) in err
    # Same file passes when the size gate is satisfied
    assert br.guard_research_source(small, min_bytes=1) is None


@pytest.mark.unit
def test_missing_research_db_refused(tmp_path: Path) -> None:
    err = br.guard_research_source(tmp_path / "nope.db")
    assert err is not None and "missing_source" in err


@pytest.mark.unit
def test_default_paths_come_from_config_not_hardcoded(
    tmp_path: Path, monkeypatch
) -> None:
    """research/l2/live defaults must follow config values — the old fixed
    default silently backed up a 245KB ghost for six weeks."""
    from src.utils.config import Config
    import src.utils.config as cfg_mod

    research = tmp_path / "elsewhere" / "hyperliquid.db"
    live = tmp_path / "live" / "bot.db"
    l2 = tmp_path / "l2_elsewhere"
    fake = Config(
        {
            "research": {"database": {"path": str(research)}},
            "database": {"path": str(live)},
            "market_data": {"l2_recording": {"path": str(l2)}},
        }
    )
    monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: fake)
    r, l, x = br.resolve_default_paths()
    assert str(r) == str(research)
    assert l == live
    assert x == l2
    # And none of them is the retired repo ghost path.
    assert "data" not in r.parts or "research" not in r.parts


# ── Task 7: --only scoped backups + manifestless-run prune safety ─────────


@pytest.mark.unit
def test_run_backup_only_live_skips_research_and_l2(tmp_path: Path) -> None:
    """--only live must back up just bot.db — no research DB copy, no L2
    pass, no ghost-source guard (which would scan E: for nothing). But a
    manifest without a verified research DB is NEVER ok:true — retention
    must not treat a partial run as a successful backup (see
    test_ok_requires_verified_research_db)."""
    research = tmp_path / "hyperliquid.db"
    live = tmp_path / "bot.db"
    _make_db(live, table="trades", rows=6)
    # research path deliberately nonexistent — an --only live run must not
    # even look at it.
    man = br.run_backup(
        backup_root=tmp_path / "backup",
        research_db=research,
        live_db=live,
        l2_src=tmp_path / "no_l2",
        tag="monthly",
        dry_run=False,
        skip_prune=True,
        only="live",
    )
    run = Path(man.run_dir)
    assert (run / "bot.db").is_file()
    assert not (run / "hyperliquid.db").exists()
    names = [d["name"] for d in man.databases]
    assert names == ["bot.db"]
    # Partial run: work was done but the manifest is honestly not-ok.
    assert man.ok is False
    assert "research_db" in (man.error or "")
    loaded = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["ok"] is False


@pytest.mark.unit
def test_run_backup_only_research_skips_live(tmp_path: Path) -> None:
    research = tmp_path / "hyperliquid.db"
    live = tmp_path / "bot.db"  # deliberately absent
    _make_db(research, table="shadow_decisions", rows=3)
    man = br.run_backup(
        backup_root=tmp_path / "backup",
        research_db=research,
        live_db=live,
        l2_src=tmp_path / "no_l2",
        tag="monthly",
        dry_run=False,
        skip_prune=True,
        only="research",
        min_research_bytes=1,
    )
    assert man.ok is True, man.error
    run = Path(man.run_dir)
    assert (run / "hyperliquid.db").is_file()
    assert not (run / "bot.db").exists()
    assert [d["name"] for d in man.databases] == ["hyperliquid.db"]


@pytest.mark.unit
def test_prune_never_deletes_run_without_manifest(tmp_path: Path) -> None:
    """A run dir with NO manifest.json is unknown state — a possibly-good
    copy that was never verified. It must never be pruned automatically."""
    runs = tmp_path / "runs"
    runs.mkdir()

    def _run(name: str, ok: bool) -> Path:
        d = runs / name
        d.mkdir()
        (d / "manifest.json").write_text(
            json.dumps({"ok": ok, "tag": "monthly"}), encoding="utf-8"
        )
        return d

    # 4 successful monthly + 1 manifestless run (the Sept failure mode:
    # 9GB copied, verification never finished, no manifest)
    _run("2026-01-01T000000Z_monthly", True)
    _run("2026-02-01T000000Z_monthly", True)
    _run("2026-03-01T000000Z_monthly", True)
    _run("2026-04-01T000000Z_monthly", True)
    orphan = runs / "2026-03-15T000000Z_monthly"
    orphan.mkdir()
    (orphan / "hyperliquid.db").write_bytes(b"partial copy")

    actions = br.prune_retention(runs, monthly_keep=3)
    assert orphan.exists(), "manifestless run dir must never be pruned"
    assert (orphan / "hyperliquid.db").exists()
    assert any(
        "keep" in a and orphan.name in a for a in actions
    ), f"expected an explicit keep action for {orphan.name}: {actions}"


@pytest.mark.unit
def test_backup_logs_phase_progress(tmp_path: Path, caplog) -> None:
    """An interrupted run must leave a trail: each phase logs start+done."""
    import logging

    live = tmp_path / "bot.db"
    _make_db(live, table="trades", rows=3)
    with caplog.at_level(logging.INFO, logger="backup_research_data"):
        man = br.run_backup(
            backup_root=tmp_path / "backup",
            research_db=tmp_path / "absent.db",
            live_db=live,
            l2_src=tmp_path / "no_l2",
            tag="monthly",
            dry_run=False,
            skip_prune=True,
            only="live",
        )
    assert man.ok is False  # scoped run without research — never ok
    text = "\n".join(r.getMessage() for r in caplog.records)
    # at minimum: per-db snapshot start/done and manifest write markers
    assert "snapshot" in text and "bot.db" in text
    assert "manifest" in text


# ── ok requires a verified research DB (regression: 2026-09-23T000131Z) ────


@pytest.mark.unit
def test_ok_requires_verified_research_db(tmp_path: Path) -> None:
    """Regression for the 2026-09-23T000131Z defect: that run wrote
    ok:true while holding ONLY bot.db — a manifest retention could trust
    enough to prune a real verified backup. From now on, ok requires the
    research DB to be present with integrity_check ok + counts_match +
    sha256; anything less is ok:false and never prunable as 'successful'."""
    live = tmp_path / "bot.db"
    _make_db(live, table="trades", rows=6)

    # 1) A scoped --only live run: bot.db backed up fine, manifest NOT ok.
    man = br.run_backup(
        backup_root=tmp_path / "backup",
        research_db=tmp_path / "absent.db",
        live_db=live,
        l2_src=tmp_path / "no_l2",
        tag="monthly",
        dry_run=False,
        skip_prune=True,
        only="live",
    )
    assert man.ok is False
    assert "research_db_missing_or_unverified" in (man.error or "")
    run = Path(man.run_dir)
    loaded = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert loaded["ok"] is False
    # Retention must refuse to classify it as a successful monthly run.
    actions = br.prune_retention(run.parent, monthly_keep=1)
    assert run.exists(), "ok:false run dirs are never pruned"
    assert any("keep_unverified_or_failed" in a for a in actions)

    # 2) A full run with a verified research DB is the only ok:true path.
    research = tmp_path / "hyperliquid.db"
    _make_db(research, table="shadow_decisions", rows=3)
    man2 = br.run_backup(
        backup_root=tmp_path / "backup2",
        research_db=research,
        live_db=live,
        l2_src=tmp_path / "no_l2",
        tag="monthly",
        dry_run=False,
        skip_prune=True,
        min_research_bytes=1,
    )
    assert man2.ok is True, man2.error
    rec = next(d for d in man2.databases if d["name"] == "hyperliquid.db")
    assert rec["integrity_check"] == "ok" and rec["sha256"]
