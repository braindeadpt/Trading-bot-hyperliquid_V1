"""deploy/linux/backup.sh staging — VACUUM INTO must clear a stale destination.

The 2026-10-30 incident chain: sqlite .backup starved on the hot WAL source
(switched to VACUUM INTO), and VACUUM INTO *fails outright* when the target
file already exists — a killed run can leave staged .db files behind because
the EXIT trap only fires on normal exit. The fix removes the destination
before each VACUUM; these tests pin that behaviour end-to-end against a real
script invocation (bash + sqlite3 CLI required).

Note: the script's EXIT trap deletes staged *.db files, so assertions run
against what the stub `restic` captured — the staging dir contents at the
moment `restic backup` would have uploaded them.
"""
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "linux" / "backup.sh"

_NEEDS = ("bash", "sqlite3", "df", "du", "date", "cp")
pytestmark = [
    pytest.mark.integration_offline,
    pytest.mark.skipif(
        any(shutil.which(t) is None for t in _NEEDS),
        reason="host lacks bash/sqlite3/coreutils needed to exec backup.sh",
    ),
]

# restic stub: captures $2 (the staging dir, first arg of `restic backup`)
# into $SNAP_DIR. The -d guard means a missing/empty arg copies nothing.
_RESTIC_STUB = """\
#!/bin/sh
if [ "$1" = "backup" ] && [ -d "$2" ]; then
    mkdir -p "$SNAP_DIR"
    cp -r "$2"/. "$SNAP_DIR"/
fi
exit 0
"""


def _mk_db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    con.commit()
    con.close()


def _env(tmp_path: Path) -> dict:
    """Minimal env: real tools from PATH plus a stub `restic` that exits 0."""
    stub = tmp_path / "bin"
    stub.mkdir()
    restic = stub / "restic"
    restic.write_text(_RESTIC_STUB)
    restic.chmod(0o755)
    data = tmp_path / "data"
    (data / "live").mkdir(parents=True)
    (data / "research").mkdir(parents=True)
    env = dict(os.environ)
    env["PATH"] = f"{stub}:{env['PATH']}"
    env["HL_DATA"] = str(data)
    env["LIVE_DB"] = str(data / "live" / "bot.db")
    env["STAGING"] = str(tmp_path / "staging")
    env["SNAP_DIR"] = str(tmp_path / "snapshot")
    env["BACKUP_RESEARCH_WEEKDAY"] = str(datetime.now().isoweekday())
    env.pop("HL_BACKUP_HC_URL", None)
    return env, data


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_vacuum_overwrites_stale_staging(tmp_path):
    """A leftover staged .db from a killed run must not fail the backup."""
    env, data = _env(tmp_path)
    _mk_db(data / "live" / "bot.db")
    _mk_db(data / "research" / "hyperliquid.db")
    staging = Path(env["STAGING"])
    staging.mkdir()
    # simulate a previous killed run: garbage where VACUUM INTO will target
    (staging / "bot.db").write_text("killed-run leftover — not sqlite")
    (staging / "hyperliquid.db").write_text("killed-run leftover — not sqlite")

    res = _run(env)
    assert res.returncode == 0, res.stderr + res.stdout
    snap = Path(env["SNAP_DIR"])
    for name in ("bot.db", "hyperliquid.db"):
        staged = snap / name
        assert staged.exists(), f"{name} missing from captured staging"
        con = sqlite3.connect(str(staged))
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
        con.close()


def test_daily_run_skips_research_when_not_weekday(tmp_path):
    """bot.db still stages + snapshots when today is not the research day."""
    env, data = _env(tmp_path)
    _mk_db(data / "live" / "bot.db")
    # research db absent entirely on non-weekly runs — it must not be required
    env["BACKUP_RESEARCH_WEEKDAY"] = str((datetime.now().isoweekday() % 7) + 1)

    res = _run(env)
    assert res.returncode == 0, res.stderr + res.stdout
    assert "daily run" in res.stdout
    snap = Path(env["SNAP_DIR"])
    assert (snap / "bot.db").exists()
    assert not (snap / "hyperliquid.db").exists()


def test_missing_live_db_fails_closed(tmp_path):
    """A missing expected source is a hard failure, never a silent skip."""
    env, _data = _env(tmp_path)
    # no DBs created at all
    res = _run(env)
    assert res.returncode != 0
    assert "missing expected source" in res.stderr
