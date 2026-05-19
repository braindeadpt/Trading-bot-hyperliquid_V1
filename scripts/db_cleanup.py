#!/usr/bin/env python3
"""
Database cleanup and validation script for Hyperliquid Trading Bot.

Usage:
    python db_cleanup.py --db data/live/bot.db --dry-run
    python db_cleanup.py --db data/live/bot.db --fix
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def validate_trades(conn, dry_run: bool = True):
    """Validate and optionally fix trade anomalies."""
    cursor = conn.cursor()
    issues = []

    # 1. Trades with entry_price = 0 or NULL
    cursor.execute("SELECT COUNT(*) FROM trades WHERE entry_price IS NULL OR entry_price = 0")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"CRITICAL: {count} trades with entry_price=0")
        if not dry_run:
            cursor.execute("DELETE FROM trades WHERE entry_price IS NULL OR entry_price = 0")
            print(f"  [FIXED] Deleted {cursor.rowcount} trades with invalid entry_price")

    # 2. Trades with exit_price = 0 but status='closed'
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status='closed' AND (exit_price IS NULL OR exit_price = 0)")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"CRITICAL: {count} closed trades with exit_price=0")
        if not dry_run:
            cursor.execute("UPDATE trades SET status='open', exit_price=NULL, exit_time=NULL, pnl_usd=NULL, pnl_pct=NULL, exit_reason=NULL WHERE status='closed' AND (exit_price IS NULL OR exit_price = 0)")
            print(f"  [FIXED] Reopened {cursor.rowcount} trades with invalid exit_price")

    # 3. Trades with size <= 0
    cursor.execute("SELECT COUNT(*) FROM trades WHERE size IS NULL OR size <= 0")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"CRITICAL: {count} trades with size <= 0")

    # 4. Trades where entry_time > exit_time
    cursor.execute("SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL AND entry_time > exit_time")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"CRITICAL: {count} trades where entry_time > exit_time")

    # 5. Trades with extreme PnL
    cursor.execute("SELECT COUNT(*) FROM trades WHERE pnl_pct > 1000 OR pnl_pct < -1000")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"CRITICAL: {count} trades with extreme PnL (>1000% or <-1000%)")
        if not dry_run:
            # Flag these for manual review instead of auto-fixing
            print(f"  [FLAGGED] {count} extreme PnL trades need manual review")

    # 6. Duplicate trades
    cursor.execute("""
        SELECT symbol, side, entry_price, exit_price, entry_time, exit_time,
               size, pnl_usd, strategy, exit_reason, status, COUNT(*)
        FROM trades
        GROUP BY symbol, side, entry_price, exit_price, entry_time, exit_time,
                 size, pnl_usd, strategy, exit_reason, status
        HAVING COUNT(*) > 1
    """)
    dups = cursor.fetchall()
    if dups:
        issues.append(f"WARNING: {len(dups)} groups of duplicate trades")
        if not dry_run:
            for d in dups:
                print(f"  [FLAGGED] Duplicate: {d[0]} {d[1]} x{d[-1]} — manual review needed")

    # 7. Cross-symbol price contamination (exit price wildly different from entry)
    cursor.execute("""
        SELECT id, symbol, side, entry_price, exit_price, pnl_pct
        FROM trades
        WHERE status='closed'
          AND exit_price IS NOT NULL
          AND ABS((exit_price - entry_price) / entry_price) > 10.0
    """)
    contaminated = cursor.fetchall()
    if contaminated:
        issues.append(f"CRITICAL: {len(contaminated)} trades with >1000% price move (possible cross-symbol contamination)")
        for t in contaminated:
            print(f"  [FLAGGED] ID={t[0]} {t[1]} {t[2]}: entry={t[3]:.2f} exit={t[4]:.2f} pnl_pct={t[5]:.2f}%")

    # 8. Trades closed without exit_reason
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status='closed' AND (exit_reason IS NULL OR exit_reason = '')")
    count = cursor.fetchone()[0]
    if count > 0:
        issues.append(f"WARNING: {count} closed trades without exit_reason")

    return issues


def add_constraints(conn, dry_run: bool = True):
    """Add database constraints for data integrity."""
    cursor = conn.cursor()
    
    # Check if constraints already exist
    cursor.execute("PRAGMA foreign_key_list(trades)")
    existing = cursor.fetchall()
    if existing:
        print("  Constraints already present.")
        return []

    issues = []
    
    if not dry_run:
        # Add CHECK constraints via trigger (SQLite doesn't support ALTER TABLE ADD CONSTRAINT)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS validate_trade_entry
            BEFORE INSERT ON trades
            BEGIN
                SELECT CASE
                    WHEN NEW.entry_price <= 0 THEN
                        RAISE(ABORT, 'entry_price must be > 0')
                    WHEN NEW.size <= 0 THEN
                        RAISE(ABORT, 'size must be > 0')
                    WHEN NEW.entry_time IS NULL THEN
                        RAISE(ABORT, 'entry_time is required')
                END;
            END;
        """)
        
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS validate_trade_exit
            BEFORE UPDATE ON trades
            WHEN NEW.status = 'closed'
            BEGIN
                SELECT CASE
                    WHEN NEW.exit_price IS NULL OR NEW.exit_price <= 0 THEN
                        RAISE(ABORT, 'exit_price must be > 0 for closed trades')
                    WHEN NEW.exit_time IS NOT NULL AND NEW.exit_time < NEW.entry_time THEN
                        RAISE(ABORT, 'exit_time must be >= entry_time')
                END;
            END;
        """)
        
        print("  [ADDED] Validation triggers for trades table")
    else:
        issues.append("Would add validation triggers for trades table")
    
    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate and cleanup trading bot database")
    parser.add_argument("--db", default="data/live/bot.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show issues without fixing")
    parser.add_argument("--fix", action="store_true", help="Apply fixes")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}")
        sys.exit(1)

    dry_run = not args.fix
    mode = "DRY RUN" if dry_run else "FIX MODE"
    print(f"\n{'='*60}")
    print(f"Database Cleanup Script — {mode}")
    print(f"Database: {db_path}")
    print(f"{'='*60}\n")

    conn = connect(str(db_path))

    print("[1/3] Validating trades...")
    trade_issues = validate_trades(conn, dry_run)
    if trade_issues:
        for issue in trade_issues:
            print(f"  [WARN] {issue}")
    else:
        print("  [OK] No trade anomalies found")

    print("\n[2/3] Adding constraints...")
    constraint_issues = add_constraints(conn, dry_run)
    if constraint_issues:
        for issue in constraint_issues:
            print(f"  [INFO] {issue}")
    else:
        print("  [OK] Constraints OK")

    print("\n[3/3] Summary...")
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM trades")
    total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
    open_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM trades WHERE status='closed'")
    closed_count = cursor.fetchone()[0]
    
    print(f"  Total trades: {total}")
    print(f"  Open trades: {open_count}")
    print(f"  Closed trades: {closed_count}")

    if not dry_run:
        conn.commit()
        print("\n[OK] Changes committed.")
    else:
        print("\n[NOTE] Dry run complete. Use --fix to apply changes.")

    conn.close()


if __name__ == "__main__":
    main()
