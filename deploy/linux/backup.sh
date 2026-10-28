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

# Free space: need ~1x total DB size on the staging fs
db_kb=$(du -ck "$HL_DATA"/research/hyperliquid.db "$LIVE_DB" 2>/dev/null | tail -1 | cut -f1)
free_kb=$(df -Pk "$STAGING" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)
mkdir -p "$STAGING"
free_kb=$(df -Pk "$STAGING" | awk 'NR==2 {print $4}')
if [ "$free_kb" -lt "${db_kb:-0}" ]; then
    echo "$LOG_TAG: FAIL need ${db_kb}K free in $STAGING, have ${free_kb}K" >&2
    hc "${HL_BACKUP_HC_URL:-}/fail"; exit 1
fi

trap 'rm -f "$STAGING"/*.db "$STAGING"/*.db-*' EXIT

# Online .backup of each DB + integrity_check on the COPY (never hot-copy)
for db in "$HL_DATA/research/hyperliquid.db" "$LIVE_DB"; do
    [ -f "$db" ] || continue
    name=$(basename "$db")
    echo "$LOG_TAG: sqlite3 .backup $name"
    sqlite3 "$db" ".backup '$STAGING/$name'"
    if ! sqlite3 "$STAGING/$name" "PRAGMA integrity_check;" | grep -qx "ok"; then
        echo "$LOG_TAG: integrity_check FAILED on $name — aborting" >&2
        hc "${HL_BACKUP_HC_URL:-}/fail"; exit 1
    fi
done

restic backup "$STAGING"     "$HL_DATA/collector_pauses.log"     /srv/hl/app/repo/data/research/phase08_preregister.json     /srv/hl/app/repo/data/research/phase10     /etc/hyperliquid/bot.env     --tag daily 2>&1 | tail -3
hc "${HL_BACKUP_HC_URL:-}"
echo "$LOG_TAG: done"
