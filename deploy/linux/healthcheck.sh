#!/usr/bin/env bash
# healthcheck.sh — thin host-side pinger for healthchecks.io.
# Usage: healthcheck.sh <check-name> — reads the ping URL from
# /srv/hl/secrets/hc-<name>.url (0600). Pings /fail on local probe failure.
# Runs via hl-healthcheck.timer every 5 min (nothing inside containers).
set -u
name="${1:?check name required}"
URL_FILE="/srv/hl/secrets/hc-$name.url"
[ -f "$URL_FILE" ] || exit 0
URL=$(tr -d "[:space:]" < "$URL_FILE")

ok=0
case "$name" in
  heartbeat)
    # dashboard answers AND feed-health wrote in the last 10 min
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8091/ 2>/dev/null || echo 000)
    latest=$(sqlite3 /srv/hl/data/research/hyperliquid.db -readonly       "SELECT MAX(created_at_ms) FROM feed_health_snapshots;" 2>/dev/null || echo 0)
    now_ms=$(( $(date +%s) * 1000 ))
    [ "$code" != "000" ] && [ $(( now_ms - ${latest:-0} )) -lt 600000 ] && ok=1 ;;
  boros)
    # boros-shadow container alive after the :05 pass
    docker inspect -f '{{.State.Running}}' hl-boros-shadow 2>/dev/null | grep -q true && ok=1 ;;
  *)
    ok=1 ;;
esac

[ "$ok" = 1 ] && curl -fsS -m 10 --retry 2 "$URL" >/dev/null 2>&1 || curl -fsS -m 10 --retry 2 "$URL/fail" >/dev/null 2>&1 || true
exit 0
