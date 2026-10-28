#!/usr/bin/env bash
# monthly_loop.sh — sleep until the next 1st-of-month 09:00 Europe/Lisbon.
#
# Calendar-aligned replacement for `cron_loop 2592000` (fixed 30d interval
# drifted away from the 1st of the month). Europe/Lisbon = WET (UTC+0)
# winter / WEST (UTC+1) summer; EU DST runs last-Sun-Mar .. last-Sun-Oct.
# Computed manually — the image carries no tzdata.
set -eu

# last Sunday of month — last day minus its day-of-week (dow%7)
_last_sun() {  # $1=year $2=month -> day-of-month
    last=$(date -d "$1-$2-01 +1 month -1 day" +%d)
    dow=$(date -d "$1-$2-$last" +%u)   # 7 = Sunday
    echo $((10#$last - dow % 7))
}

# UTC hour corresponding to 09:00 Europe/Lisbon on $1 (yyyy-mm-dd)
_lisbon_utc_hour() {
    y=${1%%-*}
    m_dst_s=$(_last_sun "$y" 3); m_dst_e=$(_last_sun "$y" 10)
    start=$(date -u -d "$y-03-$m_dst_s" +%s)
    end=$(date -u -d "$y-10-$m_dst_e" +%s)
    d=$(date -u -d "$1" +%s)
    if [ "$d" -ge "$start" ] && [ "$d" -lt "$end" ]; then
        echo 8   # WEST = UTC+1 → 09:00 local is 08:00 UTC
    else
        echo 9   # WET = UTC+0
    fi
}

now=$(date -u +%s)
y=$(date -u +%Y); m=$(date -u +%m)

# next candidate: 1st of this month, else next month
h=$(_lisbon_utc_hour "$y-$m-01")
tgt=$(date -u -d "$y-$m-01 $h:00:00" +%s)
if [ "$tgt" -le "$now" ]; then
    if [ "$m" = "12" ]; then y=$((y+1)); m=1; else m=$((10#$m+1)); fi
    h=$(_lisbon_utc_hour "$y-$(printf '%02d' "$m")-01")
    tgt=$(date -u -d "$y-$(printf '%02d' "$m")-01 $h:00:00" +%s)
fi

sleep_s=$((tgt - now))
echo "monthly_loop: next run $(date -u -d "@$tgt" '+%Y-%m-%d %H:%M UTC')" \
     "(in $((sleep_s/3600))h)"
sleep "$sleep_s"
