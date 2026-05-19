"""Archive logs before cleanup — preserves bot history.

Usage:
    python scripts/archive_logs.py                    # archive all .log files
    python scripts/archive_logs.py --days 7           # archive logs older than 7 days
    python scripts/archive_logs.py --restore          # list what's in archive

Archived logs go to logs/archive/YYYY-MM/ for organisation.
"""

import argparse
import gzip
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive old bot logs")
    parser.add_argument("--days", type=int, default=0, help="Archive logs older than N days (0=all)")
    parser.add_argument("--restore", action="store_true", help="List archived logs")
    args = parser.parse_args()

    logs_dir = Path(__file__).resolve().parent.parent / "logs"
    archive_root = logs_dir / "archive"

    if args.restore:
        if not archive_root.exists():
            print("No archive directory found.")
            return
        for f in sorted(archive_root.rglob("*")):
            if f.is_file():
                age = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                size = f.stat().st_size
                print(f"{f.relative_to(archive_root)}  ({size//1024} KB, {age.date()})")
        return

    # Collect log files
    log_files = list(logs_dir.glob("*.log")) + list(logs_dir.glob("*.log.*"))
    # Exclude already-rotated files (bot.log.N) — they're handled by RotatingFileHandler
    to_archive = [f for f in log_files if f.is_file() and f.name != "bot.log"]

    if args.days > 0:
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - args.days * 86400
        to_archive = [f for f in to_archive if f.stat().st_mtime < cutoff]

    if not to_archive:
        print("No files to archive.")
        return

    archive_root.mkdir(parents=True, exist_ok=True)
    month_dir = archive_root / datetime.now(timezone.utc).strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    for f in to_archive:
        gz_path = month_dir / (f.name + ".gz")
        with f.open("rb") as f_in:
            with gzip.open(str(gz_path), "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        f.unlink()
        print(f"Archived {f.name} -> {gz_path}")

    print(f"\nDone. {len(to_archive)} files archived to {archive_root}")


if __name__ == "__main__":
    main()
