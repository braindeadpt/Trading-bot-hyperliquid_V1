#!/usr/bin/env bash
# monthly_loop.sh — sleep until the next 1st-of-month 09:00 Europe/Lisbon.
#
# Calendar-aligned replacement for `cron_loop 2592000` (fixed 30d interval
# drifted away from the 1st of the month). tzdata is installed in the image,
# so TZ=Europe/Lisbon resolves Lisbon wall-time including DST.
set -eu

ym=$(date +%Y-%m)
tgt=$(TZ=Europe/Lisbon date -d "$ym-01 09:00" +%s)
now=$(date +%s)
if [ "$tgt" -le "$now" ]; then
    ym=$(date -d "$ym-01 +1 month" +%Y-%m)
    tgt=$(TZ=Europe/Lisbon date -d "$ym-01 09:00" +%s)
fi

sleep_s=$((tgt - now))
echo "monthly_loop: next run $ym-01 09:00 Europe/Lisbon (in $((sleep_s/3600))h)"
sleep "$sleep_s"
