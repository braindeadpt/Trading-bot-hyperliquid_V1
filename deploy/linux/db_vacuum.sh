#!/bin/sh
# db_vacuum.sh — monthly VACUUM of the SQLite DBs, run right after the host
# restic backup in a fixed window (systemd OnCalendar "--02 04:30:00 UTC",
# deliberately outside the collector windows :05-:10 and 01:02-01:05 UTC).
# Requires sqlite3 on the HOST (apt install sqlite3). Read-only free-space
# check first: VACUUM needs ~1x db size of scratch space on the same fs.
set -eu

DATA_ROOT="${1:-/srv/hl/data}"
JSL="${BOT_SQLITE_JOURNAL_LIMIT_BYTES:-67108864}"

for db in "$DATA_ROOT/research/hyperliquid.db" "$DATA_ROOT/live/bot.db"; do
    [ -f "$db" ] || continue
    free_kb=$(df -Pk "$(dirname "$db")" | awk 'NR==2 {print $4}')
    size_kb=$(du -k "$db" | cut -f1)
    if [ "$free_kb" -lt "$size_kb" ]; then
        echo "db_vacuum: SKIP $db — need ${size_kb}K free, have ${free_kb}K" >&2
        continue
    fi
    echo "db_vacuum: VACUUM $db ($(numfmt --to=iec "$((size_kb*1024))"))"
    sqlite3 "$db" "PRAGMA journal_size_limit=$JSL; VACUUM;"
    echo "db_vacuum: done $db"
done
