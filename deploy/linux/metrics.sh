#!/bin/sh
# metrics.sh — appends one CSV line per container from docker stats.
# Runs via hl-metrics.timer every 5 min. CSV at $METRICS_CSV rotated
# when it exceeds METRICS_MAX_LINES (keeps the newest tail).
set -u

CSV="${METRICS_CSV:-/srv/hl/logs/metrics.csv}"
MAX="${METRICS_MAX_LINES:-90000}"   # ~30 days at 10 containers x 5 min
mkdir -p "$(dirname "$CSV")" 2>/dev/null || true

ts=$(date -u +%FT%TZ)
docker stats --no-stream --format \
  '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}' 2>/dev/null \
  | while IFS='|' read -r name cpu mem memp net blk pids; do
        printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$ts" "$name" "$cpu" "$mem" "$memp" "$net" "$blk" "$pids"
    done >> "$CSV"

lines=$(wc -l < "$CSV" 2>/dev/null || echo 0)
if [ "$lines" -gt "$MAX" ]; then
    tail -n "$MAX" "$CSV" > "$CSV.tmp" 2>/dev/null && mv "$CSV.tmp" "$CSV"
fi
exit 0
