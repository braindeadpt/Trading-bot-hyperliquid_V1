#!/usr/bin/env python3
"""Merge legacy ghost research DB (data/research/hyperliquid.db) into configured E: path.

After commit 7d6ef6b some components still wrote to the default C: path while
others used research.database.path. This script copies rows from the ghost DB
into the configured destination without duplicating natural keys.

Usage:
  python scripts/merge_ghost_research_db.py --dry-run
  python scripts/merge_ghost_research_db.py --confirm
  python scripts/merge_ghost_research_db.py --confirm --remove-source

Does NOT modify strategy/risk settings. Does not touch .env.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.research_database import ResearchDatabase, ghost_research_db_path
from src.utils.config import load_config

# Tables known to have landed in the ghost DB (Phase08 shadow / top-trader / feed silence).
MERGE_SPECS: Dict[str, Dict[str, Any]] = {
    "shadow_decisions": {
        "columns": [
            "symbol", "strategy", "variant", "side", "would_enter", "reason",
            "timestamp_ms", "snapshot_json", "ingested_at_ms",
        ],
        "natural_key": ["symbol", "strategy", "variant", "timestamp_ms", "reason"],
    },
    "top_trader_bias_samples": {
        "columns": [
            "timestamp_ms", "symbol", "n_long", "n_short", "long_notional",
            "short_notional", "net_bias", "long_frac", "ingested_at_ms",
        ],
        "natural_key": ["symbol", "timestamp_ms", "net_bias", "long_frac"],
    },
    "top_trader_collapse_events": {
        "columns": [
            "ts_ms", "wallet", "symbol", "side", "from_notional", "to_notional",
            "drop_pct", "ingested_at_ms",
        ],
        "natural_key": ["ts_ms", "wallet", "symbol", "from_notional", "to_notional"],
    },
    "feed_silence_alerts": {
        "columns": ["feed", "alert_type", "fired_ms", "message", "ingested_at_ms"],
        "natural_key": ["feed", "alert_type", "fired_ms"],
    },
    "top_trader_virtual_trades": {
        "columns": [
            "symbol", "side", "entry_price", "entry_ts_ms", "exit_price", "exit_ts_ms",
            "exit_reason", "stop_loss_pct", "take_profit_pct", "size_pct", "entry_bias",
            "exit_bias", "pnl_pct", "status", "ingested_at_ms",
        ],
        "natural_key": ["symbol", "side", "entry_ts_ms", "entry_price", "status"],
    },
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def merge_databases(source: Path, dest: Path, *, dry_run: bool) -> Dict[str, Dict[str, int]]:
    """Merge ghost → dest. Returns per-table stats."""
    if not source.exists():
        raise FileNotFoundError(f"Ghost source DB not found: {source}")
    if source.resolve() == dest.resolve():
        raise ValueError("Source and destination are the same path — aborting")

    stats: Dict[str, Dict[str, int]] = {}
    dest.parent.mkdir(parents=True, exist_ok=True)

    dest_conn = sqlite3.connect(dest)
    dest_conn.execute("PRAGMA journal_mode=WAL")
    try:
        dest_conn.execute("ATTACH DATABASE ? AS ghost", (str(source),))
        for table, spec in MERGE_SPECS.items():
            try:
                ghost_n = int(
                    dest_conn.execute(f"SELECT COUNT(*) FROM ghost.{table}").fetchone()[0]
                )
            except sqlite3.OperationalError:
                stats[table] = {
                    "source_rows": 0,
                    "dest_before": _count(dest_conn, table),
                    "inserted": 0,
                    "dest_after": _count(dest_conn, table),
                }
                continue

            before = _count(dest_conn, table)
            if ghost_n == 0:
                stats[table] = {"source_rows": 0, "dest_before": before, "inserted": 0, "dest_after": before}
                continue

            if not _table_exists(dest_conn, table):
                ddl = dest_conn.execute(
                    "SELECT sql FROM ghost.sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if ddl and ddl[0]:
                    if not dry_run:
                        dest_conn.execute(ddl[0])
                        dest_conn.commit()

            columns = spec["columns"]
            natural_key = spec["natural_key"]
            cols = ", ".join(columns)
            key_clause = " AND ".join(f"d.{k} = g.{k}" for k in natural_key)
            count_sql = f"""
                SELECT COUNT(*) FROM ghost.{table} AS g
                WHERE NOT EXISTS (
                    SELECT 1 FROM main.{table} AS d WHERE {key_clause}
                )
            """
            would_insert = int(dest_conn.execute(count_sql).fetchone()[0])

            if dry_run:
                stats[table] = {
                    "source_rows": ghost_n,
                    "dest_before": before,
                    "would_insert": would_insert,
                }
                continue

            insert_sql = f"""
                INSERT INTO main.{table} ({cols})
                SELECT {cols} FROM ghost.{table} AS g
                WHERE NOT EXISTS (
                    SELECT 1 FROM main.{table} AS d WHERE {key_clause}
                )
            """
            cur = dest_conn.execute(insert_sql)
            dest_conn.commit()
            after = _count(dest_conn, table)
            stats[table] = {
                "source_rows": ghost_n,
                "dest_before": before,
                "inserted": int(cur.rowcount),
                "dest_after": after,
            }
        dest_conn.execute("DETACH DATABASE ghost")
    finally:
        dest_conn.close()
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge ghost research DB into configured path")
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Ghost DB path (default: data/research/hyperliquid.db under project root)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Destination DB (default: research.database.path from config)",
    )
    parser.add_argument("--config", default="config/settings.yaml", help="Config YAML path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts only; do not write",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Perform merge (required to write)",
    )
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="Delete source file after successful merge (requires --confirm)",
    )
    args = parser.parse_args()

    if args.remove_source and not args.confirm:
        print("ERROR: --remove-source requires --confirm", file=sys.stderr)
        return 2
    if not args.dry_run and not args.confirm:
        print("ERROR: pass --dry-run to inspect or --confirm to merge", file=sys.stderr)
        return 2

    cfg = load_config(ROOT / args.config)
    source = args.source or ghost_research_db_path()
    dest = args.dest or ResearchDatabase.resolve_path(cfg)

    print(f"Source (ghost): {source}")
    print(f"Dest (config):  {dest}")
    print(f"Mode: {'dry-run' if args.dry_run else 'merge'}")

    try:
        stats = merge_databases(source, dest, dry_run=args.dry_run)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    total_src = 0
    total_ins = 0
    for table, row in stats.items():
        print(f"\n{table}:")
        for k, v in row.items():
            print(f"  {k}: {v}")
        total_src += int(row.get("source_rows", 0))
        total_ins += int(row.get("inserted", row.get("would_insert", 0)))

    print(f"\nTotal source rows (listed tables): {total_src}")
    if not args.dry_run:
        print(f"Total inserted: {total_ins}")
        if args.remove_source and source.exists():
            source.unlink()
            print(f"Removed ghost source: {source}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
