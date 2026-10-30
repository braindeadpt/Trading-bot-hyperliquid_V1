#!/usr/bin/env bash
# backup.sh — daily restic backup of the HL SQLite DBs + metadata to the
# Oracle Object Storage bucket (S3-compatible). Runs on the HOST via
# hl-backup.timer (04:00 UTC daily, outside the :05-:10 and 01:02-01:05
# collector windows and before the 05:00 overnight job).
#
# Env (from /etc/hyperliquid/backup.env, 0644 — sem segredos):
#   RESTIC_REPOSITORY   s3:https://<ns>.compat.objectstorage.<region>.oraclecloud.com/<bucket>
#   RESTIC_PASSWORD_FILE  /srv/hl/secrets/restic-password
#   HL_BACKUP_HC_URL  optional healthchecks.io ping URL
# Credentials: /srv/hl/secrets/oci-keys (0600) with
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (OCI Customer Secret Key)
set -euo pipefail

[ -f /srv/hl/secrets/oci-keys ] && . /srv/hl/secrets/oci-keys
[ -z "${HL_BACKUP_HC_URL:-}" ] && [ -f /srv/hl/secrets/hc-hl-backup-daily.url ] &&     HL_BACKUP_HC_URL=$(tr -d "[:space:]" < /srv/hl/secrets/hc-hl-backup-daily.url)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

HL_DATA="${HL_DATA:-/srv/hl/data}"
LIVE_DB="${LIVE_DB:-$HL_DATA/live/bot.db}"
STAGING="${STAGING:-/srv/hl/backup-staging}"
LOG_TAG="hl-backup"

hc() { [ -n "${HL_BACKUP_HC_URL:-}" ] && curl -fsS -m 10 --retry 2 "$1" >/dev/null 2>&1 || true; }

# Research DB (~9+ GB, ~95% frozen history) stages weekly only — the live
# bot.db stages every run. Rationale: sqlite .backup on a hot WAL source
# re-copies dirty pages and starved at ~160KB/s for 8h42m (2026-10-30);
# VACUUM INTO does a single sequential pass (~5 min measured) and yields a
# defragged copy.
RESEARCH_WEEKDAY="${BACKUP_RESEARCH_WEEKDAY:-7}"   # ISO weekday, 7=Sunday
TODAY=$(date -u +%u)

DBS="$LIVE_DB"
if [ "$TODAY" = "$RESEARCH_WEEKDAY" ]; then
    DBS="$HL_DATA/research/hyperliquid.db $DBS"
    echo "$LOG_TAG: weekly run — research DB included (weekday $RESEARCH_WEEKDAY)"
else
    echo "$LOG_TAG: daily run — research DB skipped (weekly on day $RESEARCH_WEEKDAY)"
fi

# Free space: need ~1x staged DB size on the staging fs
db_kb=$(du -ck $DBS 2>/dev/null | tail -1 | cut -f1)
free_kb=$(df -Pk "$STAGING" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
mkdir -p "$STAGING"
free_kb=$(df -Pk "$STAGING" | awk 'NR==2 {print $4}')
if [ "$free_kb" -lt "${db_kb:-0}" ]; then
    echo "$LOG_TAG: FAIL need ${db_kb}K free in $STAGING, have ${free_kb}K" >&2
    hc "${HL_BACKUP_HC_URL:-}/fail"; exit 1
fi

trap 'rm -f "$STAGING"/*.db "$STAGING"/*.db-*' EXIT

# Expected sources are mandatory: a missing DB is a failure, not a skip —
# a silent continue would produce a green snapshot missing a DB.
for db in $DBS; do
    if [ ! -f "$db" ]; then
        echo "$LOG_TAG: FAIL missing expected source $db" >&2
        hc "${HL_BACKUP_HC_URL:-}/fail"; exit 1
    fi
    name=$(basename "$db")
    echo "$LOG_TAG: VACUUM INTO $name"
    sqlite3 "file:$db?mode=ro" "VACUUM INTO '$STAGING/$name'"
    if ! sqlite3 -readonly "$STAGING/$name" "PRAGMA integrity_check;" | grep -qx "ok"; then
        echo "$LOG_TAG: integrity_check FAILED on $name — aborting" >&2
        hc "${HL_BACKUP_HC_URL:-}/fail"; exit 1
    fi
done

restic backup "$STAGING"     "$HL_DATA/collector_pauses.log"     /srv/hl/app/repo/data/research/phase08_preregister.json     /srv/hl/app/repo/data/research/phase10     /etc/hyperliquid/bot.env     --tag daily 2>&1 | tail -3
hc "${HL_BACKUP_HC_URL:-}"
echo "$LOG_TAG: done"
