#!/usr/bin/env python3
"""Deduplicate candle rows that were backfilled with the open_time convention.

v3.1.16 C11: Backfilled candles used ``timestamp_ms = open_time`` (Binance
k[0]) while live candles use ``close_time`` (candle_builder convention).
The result is two rows for the same bar in the same (symbol, tf) table —
one with PK = open_time and one with PK = close_time. This script picks
one row per bar (the higher timestamp = close_time) and deletes the
duplicates. Run after the timestamp-convention fix has been deployed.

Usage:
    python scripts/deduplicate_candles.py --db data/live/bot.db
    python scripts/deduplicate_candles.py --db data/live/bot.db --dry-run
    python scripts/deduplicate_candles.py --db data/live/bot.db --yes
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("deduplicate_candles")

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}

CANDLE_TABLES = ("candles_1m", "candles_5m", "candles_15m", "candles_1h")


def deduplicate_table(
    conn: sqlite3.Connection,
    table: str,
    interval_ms: int,
    dry_run: bool,
) -> int:
    """Delete duplicate candle rows, keeping the row with the higher PK.

    The candle tables are WITHOUT ROWID, so the implicit ``rowid``
    column is unavailable. We deduplicate by deleting rows whose
    (symbol, bar_index) bucket is shared with a higher timestamp_ms
    row, keeping only the largest (close_time) row per bar.
    """
    cur = conn.cursor()
    total_before = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    # Identify duplicates: rows where the same (symbol, bar_index) has
    # multiple timestamp_ms values. Keep the largest (the close_time
    # row, which now matches live candle_builder convention).
    select_dups_sql = (
        f"SELECT symbol, timestamp_ms, MAX(timestamp_ms) OVER ("
        f"  PARTITION BY symbol, (timestamp_ms / ?)"
        f") AS max_ts, "
        f"COUNT(*) OVER ("
        f"  PARTITION BY symbol, (timestamp_ms / ?)"
        f") AS dup_count "
        f"FROM {table}"
    )
    if dry_run:
        row = cur.execute(
            f"SELECT COUNT(*) FROM ("
            f"  SELECT 1 FROM {table} "
            f"  GROUP BY symbol, (timestamp_ms / ?) "
            f"  HAVING COUNT(*) > 1"
            f")",
            (interval_ms,),
        ).fetchone()
        dup_buckets = row[0]
        cur.execute(select_dups_sql, (interval_ms, interval_ms))
        to_delete = 0
        for _sym, ts, max_ts, dup_count in cur.fetchall():
            if dup_count > 1 and ts != max_ts:
                to_delete += 1
        logger.info(
            "[DRY-RUN] %s: %d dup buckets, %d rows to delete (of %d total)",
            table, dup_buckets, to_delete, total_before,
        )
        return to_delete

    # Use a temp table to mark rows for deletion, since DELETE ... IN
    # with a window-function-based subquery isn't supported on all
    # SQLite versions.
    cur.execute(
        f"CREATE TEMP TABLE _dup_target AS "
        f"SELECT symbol, timestamp_ms FROM ("
        f"  SELECT symbol, timestamp_ms, "
        f"    MAX(timestamp_ms) OVER ("
        f"      PARTITION BY symbol, (timestamp_ms / ?)"
        f"    ) AS max_ts "
        f"  FROM {table}"
        f") WHERE timestamp_ms != max_ts"
    )
    cur.execute(
        f"DELETE FROM {table} WHERE (symbol, timestamp_ms) IN ("
        f"  SELECT symbol, timestamp_ms FROM _dup_target"
        f")"
    )
    deleted = cur.rowcount
    cur.execute("DROP TABLE _dup_target")
    conn.commit()
    logger.info(
        "%s: deleted %d duplicate rows (of %d before)",
        table, deleted, total_before,
    )
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deduplicate candle rows (v3.1.16 C11).",
    )
    parser.add_argument(
        "--db",
        default="data/live/bot.db",
        help="SQLite database path (default: data/live/bot.db).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many rows would be deleted, but make no changes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 1

    if not args.dry_run and not args.yes:
        reply = input(
            f"This will delete duplicate rows in {db_path}. Continue? [y/N] "
        )
        if reply.strip().lower() != "y":
            logger.info("Aborted.")
            return 0

    conn = sqlite3.connect(str(db_path))
    total = 0
    for table in CANDLE_TABLES:
        tf = table.replace("candles_", "")
        interval_ms = TIMEFRAME_MS.get(tf)
        if interval_ms is None:
            continue
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )
        if cur.fetchone() is None:
            continue
        total += deduplicate_table(conn, table, interval_ms, args.dry_run)
    conn.close()
    logger.info("Done. Total rows removed: %d", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
