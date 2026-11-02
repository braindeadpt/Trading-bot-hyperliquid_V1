#!/bin/sh
# disk_guard.sh — runs every 10 min (compose hl-disk-guard service or the
# hl-disk-guard systemd timer). Thresholds on the /data filesystem:
#   >= DISK_WARN_PCT (80): alert via log (+ optional healthcheck ping fail)
#   >= DISK_STOP_PCT (85): create $FS/PAUSE_COLLECTORS — research collectors
#                          stop writing; shadows/heartbeats keep running
#   <  warn:             remove the flag if present
# Never touches Meridian paths; the flag only covers the HL data root.
set -u

# Path precedence: DISK_GUARD_FS (unit override) > HL_DATA (bot.env) > target
# layout default /srv/hl/data. Inside containers the same dir is /data.
FS="${DISK_GUARD_FS:-${HL_DATA:-/srv/hl/data}}"
# ping URL: env wins; else /srv/hl/secrets/hc-hl-disk-guard.url (0600, off-repo)
if [ -z "${DISK_GUARD_HC_URL:-}" ] && [ -f /srv/hl/secrets/hc-hl-disk-guard.url ]; then
    DISK_GUARD_HC_URL=$(tr -d "[:space:]" < /srv/hl/secrets/hc-hl-disk-guard.url)
fi
TAG="hl-disk-guard"
WARN="${DISK_WARN_PCT:-80}"
STOP="${DISK_STOP_PCT:-85}"
# systemd EnvironmentFile does not strip trailing comments — a copied
# bot.env.example would deliver "85  # comment" and every -ge test would
# silently fail. Refuse loudly (and alert) instead of clearing the flag.
case "$WARN" in (*[!0-9]*|'') BAD=DISK_WARN_PCT ;; esac
case "$STOP" in (*[!0-9]*|'') BAD=DISK_STOP_PCT ;; esac
if [ -n "${BAD:-}" ]; then
    echo "$TAG: ERROR — non-numeric threshold $BAD (WARN='$WARN' STOP='$STOP'; check for inline comments in env file)" >&2
    [ -n "${DISK_GUARD_HC_URL:-}" ] && curl -fsS -m 10 --retry 2 "${DISK_GUARD_HC_URL}/fail" >/dev/null 2>&1 || true
    exit 1
fi
FLAG="$FS/PAUSE_COLLECTORS"
PAUSE_LOG="${DISK_GUARD_PAUSE_LOG:-$FS/collector_pauses.log}"

USE_PCT=$(df -P "$FS" 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$5); print $5}')
if [ -z "$USE_PCT" ]; then
    echo "$TAG: ERROR — cannot statfs $FS (missing or unreadable data dir; check DISK_GUARD_FS/HL_DATA vs deployed layout)" >&2
    [ -n "${DISK_GUARD_HC_URL:-}" ] && curl -fsS -m 10 --retry 2 "${DISK_GUARD_HC_URL}/fail" >/dev/null 2>&1 || true
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
