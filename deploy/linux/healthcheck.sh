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
    # dashboard answers on the serve target AND candles_1m wrote in the last 5 min.
    # candles_1m MAX(timestamp_ms) is the ground-truth feed-freshness signal —
    # a sampler writing staleness rows (feed_age_samples) does NOT count as up.
    # public access: https://bots-01.tail3cb15a.ts.net/ (tailnet only, root :443) —
    # probing the ts.net name from the host itself hairpins (000), so probe local
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8091/ 2>/dev/null || echo 000)
    DB="${HL_LIVE_DB:-${HL_DATA:-/srv/hl/data}/live/bot.db}"
    latest=$(sqlite3 "$DB" -readonly "SELECT MAX(timestamp_ms) FROM candles_1m;" 2>/dev/null || echo 0)
    now_ms=$(( $(date +%s) * 1000 ))
    [ "$code" != "000" ] && [ $(( now_ms - ${latest:-0} )) -lt 300000 ] && ok=1 ;;
  boros)
    # boros-shadow container alive after the :05 pass
    docker inspect -f '{{.State.Running}}' hl-boros-shadow 2>/dev/null | grep -q true && ok=1 ;;
  egress)
    # canary: real TCP connect from inside the main container to the HL API.
    # Catches kernel-level egress death (e.g. net.ipv4.ip_forward=0) that
    # host-side probes and container-alive checks cannot see.
    docker exec hl-paper python -c \
      "import socket; s = socket.create_connection(('api.hyperliquid.xyz', 443), 5); s.close()" \
      >/dev/null 2>&1 && ok=1 ;;
  *)
    ok=1 ;;
esac

[ "$ok" = 1 ] && curl -fsS -m 10 --retry 2 "$URL" >/dev/null 2>&1 || curl -fsS -m 10 --retry 2 "$URL/fail" >/dev/null 2>&1 || true
exit 0
