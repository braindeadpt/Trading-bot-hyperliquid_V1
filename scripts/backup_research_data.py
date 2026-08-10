#!/usr/bin/env python3
"""Verified monthly backup of irrecoverable research + ops SQLite + L2 books.

Copies (never moves / never deletes sources):

* ``data/research/hyperliquid.db`` — consistent snapshot via ``sqlite3`` backup API
* ``data/live/bot.db`` — same (paper trade history is irrecoverable)
* ``data/research/l2_books`` — incremental copy of **closed** daily
  ``*.jsonl.gz`` only (today's file is still being written)

Verification (fail loudly; never prune a failed run or replace a good prior):

* SQLite: ``PRAGMA integrity_check`` on the copy + row counts for key tables
* gzip: zlib member walk (``gzip -t`` equivalent) on each newly copied ``.gz``
* Manifest JSON written only after checks pass

Retention: keep the last **3 successful monthly** runs + **1 successful annual**.
Never delete the newest successful run of a tag, and never delete a run whose
manifest marks ``ok: false``.

Usage (manual)::

    python scripts/backup_research_data.py --tag monthly
    python scripts/backup_research_data.py --tag annual
    python scripts/backup_research_data.py --tag monthly --dry-run

Windows Task Scheduler proposal is documented in ``docs/DATA_ARCHITECTURE.md``
— do **not** create the scheduled task from this script.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("backup_research_data")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_BACKUP_ROOT = Path("data/backups/hyperliquid")
DEFAULT_L2_SRC = Path("data/research/l2_books")
DEFAULT_RESEARCH_DB = PROJECT_ROOT / "data" / "research" / "hyperliquid.db"
DEFAULT_LIVE_DB = PROJECT_ROOT / "data" / "live" / "bot.db"

# Tables that must exist and match row counts when present on the source.
RESEARCH_COUNT_TABLES = (
    "trade_tape",
    "l2_snapshots",
    "candles_1m",
    "candles_5m",
    "candles_15m",
    "candles_1h",
    "shadow_decisions",
    "feed_health_snapshots",
)
LIVE_COUNT_TABLES = (
    "trades",
    "candles_1m",
    "candles_15m",
    "decision_audit",
    "signals",
    "portfolio_snapshots",
)

MONTHLY_KEEP = 3
ANNUAL_KEEP = 1


@dataclass
class FileRecord:
    path: str
    size: int
    sha256: str
    verified: bool
    action: str  # copied | skipped_exists | skipped_today


@dataclass
class DbRecord:
    name: str
    dest: str
    size: int
    sha256: str
    integrity_check: str
    row_counts: Dict[str, int]
    counts_match: bool


@dataclass
class Manifest:
    schema_version: int = 1
    tag: str = "monthly"
    started_at_utc: str = ""
    finished_at_utc: str = ""
    ok: bool = False
    error: Optional[str] = None
    backup_root: str = ""
    run_dir: str = ""
    sources: Dict[str, str] = field(default_factory=dict)
    databases: List[Dict[str, Any]] = field(default_factory=list)
    l2_files: List[Dict[str, Any]] = field(default_factory=list)
    l2_copied: int = 0
    l2_skipped: int = 0
    notes: List[str] = field(default_factory=list)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def count_tables(conn: sqlite3.Connection, names: Sequence[str]) -> Dict[str, int]:
    present = table_names(conn)
    out: Dict[str, int] = {}
    for name in names:
        if name not in present:
            continue
        out[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return out


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0]) if row else "empty"


def snapshot_sqlite_consistent(src: Path, dest: Path) -> None:
    """Consistent online snapshot via the SQLite backup API (WAL-aware).

    Source is opened read-only so this never takes a write lock that would
    stall the paper bot. Destination is created fresh under ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    for side in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        side.unlink(missing_ok=True)
    src_conn = open_readonly(src)
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
        dest_conn.commit()
    finally:
        dest_conn.close()
        src_conn.close()


def verify_sqlite_copy(
    src: Path,
    dest: Path,
    count_names: Sequence[str],
    *,
    counts_before: Optional[Dict[str, int]] = None,
) -> Tuple[bool, str, Dict[str, int], Dict[str, int], Dict[str, int]]:
    """Verify dest integrity; compare counts against a live source safely.

    A live research/ops DB keeps receiving inserts while backup runs. Comparing
    the copy to a *post*-backup source count alone is a false failure.

    Pass when:
      * ``PRAGMA integrity_check`` on dest is ``ok``
      * for every counted table: ``before[t] <= dest[t] <= after[t]``

    Returns ``(ok, integrity_msg, before, dest_counts, after)``.
    """
    src_conn = open_readonly(src)
    dest_conn = open_readonly(dest)
    try:
        msg = integrity_check(dest_conn)
        before = (
            dict(counts_before)
            if counts_before is not None
            else count_tables(src_conn, count_names)
        )
        dest_counts = count_tables(dest_conn, count_names)
        after = count_tables(src_conn, count_names)
    finally:
        dest_conn.close()
        src_conn.close()
    if msg != "ok":
        return False, msg, before, dest_counts, after
    for table, d_n in dest_counts.items():
        b_n = int(before.get(table, d_n))
        a_n = int(after.get(table, d_n))
        if not (b_n <= d_n <= a_n):
            return False, "row_count_window_mismatch", before, dest_counts, after
    return True, msg, before, dest_counts, after


def gzip_integrity_ok(path: Path) -> bool:
    """Equivalent to ``gzip -t`` — decompress fully, discard output."""
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
        return False


def parse_l2_day(path: Path) -> Optional[date]:
    """Parse ``YYYY-MM-DD`` from ``SYM/YYYY-MM-DD.jsonl.gz``."""
    stem = path.name
    if stem.endswith(".jsonl.gz"):
        day_s = stem[: -len(".jsonl.gz")]
    elif stem.endswith(".gz"):
        day_s = path.stem  # strips .gz → YYYY-MM-DD.jsonl
        if day_s.endswith(".jsonl"):
            day_s = day_s[: -len(".jsonl")]
    else:
        return None
    try:
        return date.fromisoformat(day_s)
    except ValueError:
        return None


def iter_closed_l2_files(src_root: Path, *, today: date) -> List[Path]:
    if not src_root.is_dir():
        return []
    out: List[Path] = []
    for path in sorted(src_root.rglob("*.jsonl.gz")):
        day = parse_l2_day(path)
        if day is None:
            continue
        if day >= today:
            continue  # today's file still open for append
        out.append(path)
    return out


def incremental_copy_l2(
    src_root: Path,
    dest_root: Path,
    *,
    today: Optional[date] = None,
    dry_run: bool = False,
) -> Tuple[List[FileRecord], List[str]]:
    """Copy closed-day gz files that are missing (or size-mismatched) at dest.

    Returns (records, errors). On any gzip verify failure after copy, the bad
    dest file is removed and listed in errors — sources are never touched.
    """
    today = today or datetime.now(timezone.utc).date()
    records: List[FileRecord] = []
    errors: List[str] = []
    dest_root.mkdir(parents=True, exist_ok=True)

    for src in iter_closed_l2_files(src_root, today=today):
        rel = src.relative_to(src_root)
        dest = dest_root / rel
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            records.append(
                FileRecord(
                    path=rel.as_posix(),
                    size=dest.stat().st_size,
                    sha256="",  # deferred — skip rehash on unchanged
                    verified=True,
                    action="skipped_exists",
                )
            )
            continue
        if dry_run:
            records.append(
                FileRecord(
                    path=rel.as_posix(),
                    size=src.stat().st_size,
                    sha256="",
                    verified=False,
                    action="would_copy",
                )
            )
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            shutil.copy2(src, tmp)
            if not gzip_integrity_ok(tmp):
                tmp.unlink(missing_ok=True)
                errors.append(f"gzip_verify_failed:{rel.as_posix()}")
                continue
            digest = sha256_file(tmp)
            tmp.replace(dest)
            records.append(
                FileRecord(
                    path=rel.as_posix(),
                    size=dest.stat().st_size,
                    sha256=digest,
                    verified=True,
                    action="copied",
                )
            )
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            errors.append(f"copy_failed:{rel.as_posix()}:{exc}")

    return records, errors


def run_dir_name(tag: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{when.strftime('%Y-%m-%dT%H%M%SZ')}_{tag}"


def list_run_dirs(runs_root: Path) -> List[Path]:
    if not runs_root.is_dir():
        return []
    return sorted(
        (p for p in runs_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )


def load_manifest(run_dir: Path) -> Optional[Dict[str, Any]]:
    path = run_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def prune_retention(
    runs_root: Path,
    *,
    monthly_keep: int = MONTHLY_KEEP,
    annual_keep: int = ANNUAL_KEEP,
    dry_run: bool = False,
) -> List[str]:
    """Delete old **successful** run dirs beyond keep counts.

    Never deletes:
    * a run with missing/failed manifest (``ok`` is not true)
    * the newest successful run of each tag (even if keep=0)
    """
    actions: List[str] = []
    by_tag: Dict[str, List[Path]] = {"monthly": [], "annual": []}
    for run in list_run_dirs(runs_root):
        man = load_manifest(run)
        if not man or not man.get("ok"):
            actions.append(f"keep_unverified_or_failed:{run.name}")
            continue
        tag = str(man.get("tag") or "")
        if tag not in by_tag:
            actions.append(f"keep_unknown_tag:{run.name}")
            continue
        by_tag[tag].append(run)

    for tag, keep in (("monthly", monthly_keep), ("annual", annual_keep)):
        runs = by_tag[tag]  # already sorted by name (= utc stamp)
        if not runs:
            continue
        # Newest last; always retain at least the newest successful run.
        n_keep = max(1, keep)
        victims = runs[:-n_keep]
        for victim in victims:
            if dry_run:
                actions.append(f"would_prune:{victim.name}")
            else:
                shutil.rmtree(victim)
                actions.append(f"pruned:{victim.name}")
    return actions


def backup_one_db(
    *,
    label: str,
    src: Path,
    dest: Path,
    count_names: Sequence[str],
    dry_run: bool,
) -> Tuple[Optional[DbRecord], Optional[str]]:
    if not src.exists():
        return None, f"missing_source:{src}"
    if dry_run:
        return (
            DbRecord(
                name=label,
                dest=str(dest),
                size=src.stat().st_size,
                sha256="",
                integrity_check="dry_run",
                row_counts={},
                counts_match=True,
            ),
            None,
        )
    src_conn = open_readonly(src)
    try:
        counts_before = count_tables(src_conn, count_names)
    finally:
        src_conn.close()

    snapshot_sqlite_consistent(src, dest)
    ok, msg, before, dest_counts, after = verify_sqlite_copy(
        src, dest, count_names, counts_before=counts_before
    )
    digest = sha256_file(dest)
    rec = DbRecord(
        name=label,
        dest=str(dest),
        size=dest.stat().st_size,
        sha256=digest,
        integrity_check=msg if ok or msg != "row_count_window_mismatch" else "ok",
        row_counts=dest_counts,
        counts_match=ok,
    )
    if not ok:
        detail = {
            "check": msg,
            "counts_before": before,
            "dest_counts": dest_counts,
            "counts_after": after,
        }
        if msg == "row_count_window_mismatch":
            dest_conn = open_readonly(dest)
            try:
                real_integrity = integrity_check(dest_conn)
            finally:
                dest_conn.close()
            detail["integrity_check"] = real_integrity
            rec = DbRecord(
                name=label,
                dest=str(dest),
                size=dest.stat().st_size,
                sha256=digest,
                integrity_check=real_integrity,
                row_counts=dest_counts,
                counts_match=False,
            )
        return rec, f"sqlite_verify_failed:{label}:{json.dumps(detail)}"
    return rec, None


def write_manifest(path: Path, manifest: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_backup(
    *,
    backup_root: Path,
    research_db: Path,
    live_db: Path,
    l2_src: Path,
    tag: str,
    dry_run: bool = False,
    skip_prune: bool = False,
) -> Manifest:
    started = utc_now_iso()
    runs_root = backup_root / "runs"
    l2_dest = backup_root / "l2_books"
    run_dir = runs_root / run_dir_name(tag)
    man = Manifest(
        tag=tag,
        started_at_utc=started,
        backup_root=str(backup_root),
        run_dir=str(run_dir),
        sources={
            "hyperliquid.db": str(research_db),
            "bot.db": str(live_db),
            "l2_books": str(l2_src),
        },
    )

    errors: List[str] = []
    if not dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)

    # --- SQLite snapshots (into the dated run dir) ---
    for label, src, names in (
        ("hyperliquid.db", research_db, RESEARCH_COUNT_TABLES),
        ("bot.db", live_db, LIVE_COUNT_TABLES),
    ):
        dest = run_dir / label
        rec, err = backup_one_db(
            label=label, src=src, dest=dest, count_names=names, dry_run=dry_run
        )
        if rec is not None:
            man.databases.append(asdict(rec))
            man.notes.append(
                f"{label}: integrity={rec.integrity_check} "
                f"counts_match={rec.counts_match} size={rec.size}"
            )
        if err:
            errors.append(err)

    # --- Incremental L2 (shared mirror under backup_root) ---
    if not l2_src.is_dir():
        man.notes.append(f"l2_src_missing:{l2_src}")
    else:
        l2_records, l2_errors = incremental_copy_l2(
            l2_src, l2_dest, dry_run=dry_run
        )
        errors.extend(l2_errors)
        man.l2_files = [asdict(r) for r in l2_records]
        man.l2_copied = sum(1 for r in l2_records if r.action in ("copied", "would_copy"))
        man.l2_skipped = sum(1 for r in l2_records if r.action == "skipped_exists")
        man.notes.append(
            f"l2: copied={man.l2_copied} skipped_exists={man.l2_skipped} "
            f"errors={len(l2_errors)}"
        )

    man.ok = len(errors) == 0
    man.error = "; ".join(errors) if errors else None
    man.finished_at_utc = utc_now_iso()

    if dry_run:
        man.notes.append("dry_run=true — no files written")
        return man

    # Write manifest only after verification outcome is known.
    # Failed runs still get a manifest with ok=false so retention never
    # treats them as successful — and we do NOT prune on failure.
    write_manifest(run_dir / "manifest.json", man)

    if man.ok and not skip_prune:
        prune_actions = prune_retention(runs_root, dry_run=False)
        man.notes.extend(prune_actions)
        # Refresh manifest with prune notes
        write_manifest(run_dir / "manifest.json", man)
    elif not man.ok:
        man.notes.append(
            "verification_failed — prior successful backups retained; "
            "this run dir left for forensics"
        )
        write_manifest(run_dir / "manifest.json", man)

    return man


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--tag",
        choices=("monthly", "annual"),
        default="monthly",
        help="Retention bucket for this run (default: monthly)",
    )
    p.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help=f"Destination root (default: {DEFAULT_BACKUP_ROOT})",
    )
    p.add_argument(
        "--research-db",
        type=Path,
        default=DEFAULT_RESEARCH_DB,
        help="Path to hyperliquid.db",
    )
    p.add_argument(
        "--live-db",
        type=Path,
        default=DEFAULT_LIVE_DB,
        help="Path to bot.db",
    )
    p.add_argument(
        "--l2-src",
        type=Path,
        default=DEFAULT_L2_SRC,
        help="Source L2 books root",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — no writes, no prune",
    )
    p.add_argument(
        "--skip-prune",
        action="store_true",
        help="Skip retention prune even on success",
    )
    p.add_argument(
        "--prune-only",
        action="store_true",
        help="Only run retention prune on existing runs",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.prune_only:
        actions = prune_retention(
            args.backup_root / "runs", dry_run=args.dry_run
        )
        for a in actions:
            print(a)
        return 0

    man = run_backup(
        backup_root=args.backup_root,
        research_db=args.research_db,
        live_db=args.live_db,
        l2_src=args.l2_src,
        tag=args.tag,
        dry_run=args.dry_run,
        skip_prune=args.skip_prune,
    )
    print(json.dumps(asdict(man), indent=2, sort_keys=True))
    if not man.ok:
        logger.error("BACKUP FAILED: %s", man.error)
        return 1
    logger.info(
        "BACKUP OK tag=%s run=%s dbs=%d l2_copied=%d",
        man.tag,
        man.run_dir,
        len(man.databases),
        man.l2_copied,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
