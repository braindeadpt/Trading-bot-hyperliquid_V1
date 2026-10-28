#!/bin/sh
# disk_guard.sh — runs every 10 min (compose hl-disk-guard service or the
# hl-disk-guard systemd timer). Thresholds on the /data filesystem:
#   >= DISK_WARN_PCT (80): alert via log (+ optional healthcheck ping fail)
#   >= DISK_STOP_PCT (85): create $FS/PAUSE_COLLECTORS — research collectors
#                          stop writing; shadows/heartbeats keep running
#   <  warn:             remove the flag if present
# Never touches Meridian paths; the flag only covers the HL data root.
set -u

FS="${DISK_GUARD_FS:-/data}"
# ping URL: env wins; else /srv/hl/secrets/hc-hl-disk-guard.url (0600, off-repo)
if [ -z "${DISK_GUARD_HC_URL:-}" ] && [ -f /srv/hl/secrets/hc-hl-disk-guard.url ]; then
    DISK_GUARD_HC_URL=$(tr -d "[:space:]" < /srv/hl/secrets/hc-hl-disk-guard.url)
fi
WARN="${DISK_WARN_PCT:-80}"
STOP="${DISK_STOP_PCT:-85}"
FLAG="$FS/PAUSE_COLLECTORS"
PAUSE_LOG="${DISK_GUARD_PAUSE_LOG:-$FS/collector_pauses.log}"
TAG="hl-disk-guard"

USE_PCT=$(df -P "$FS" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ -z "$USE_PCT" ]; then
    echo "$TAG: cannot statfs $FS" >&2
    exit 1
fi

if [ "$USE_PCT" -ge "$STOP" ]; then
    if [ ! -f "$FLAG" ]; then
        : > "$FLAG"
        mkdir -p "$(dirname "$PAUSE_LOG")" 2>/dev/null || true
        echo "$(date -u +%FT%TZ) pause-start fs=$FS use=${USE_PCT}%" >> "$PAUSE_LOG" 2>/dev/null || true
    fi
    echo "$TAG: CRITICAL ${USE_PCT}% >= ${STOP}% on $FS — PAUSE_COLLECTORS set"
    if [ -n "${DISK_GUARD_HC_URL:-}" ]; then
        curl -fsS -m 10 --retry 2 "${DISK_GUARD_HC_URL}/fail" >/dev/null 2>&1 || true
    fi
elif [ "$USE_PCT" -ge "$WARN" ]; then
    echo "$TAG: WARN ${USE_PCT}% >= ${WARN}% on $FS"
    if [ -n "${DISK_GUARD_HC_URL:-}" ]; then
        curl -fsS -m 10 --retry 2 "${DISK_GUARD_HC_URL}/fail" >/dev/null 2>&1 || true
    fi
else
    [ -n "${DISK_GUARD_HC_URL:-}" ] && curl -fsS -m 10 --retry 2 "$DISK_GUARD_HC_URL" >/dev/null 2>&1 || true
    if [ -f "$FLAG" ]; then
        rm -f "$FLAG"
        echo "$(date -u +%FT%TZ) pause-end fs=$FS use=${USE_PCT}%" >> "$PAUSE_LOG" 2>/dev/null || true
        echo "$TAG: ${USE_PCT}% back under ${WARN}% — PAUSE_COLLECTORS removed"
    fi
fi
exit 0
