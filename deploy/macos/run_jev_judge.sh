#!/bin/bash
# Jev verdict feeder — long-running loop, supervised by pm2 (autorestart).
#
# Replaces pm2 cron_restart "*/5 * * * *" + autorestart false: the pm2 cron
# LOST THE TIMER twice (2026-10-03 23:25, 2026-10-05 16:25 — pm2.log shows
# "exited with code [1] via signal [SIGINT]" + "stale exit event ignored"
# at fire time, then nothing; app stayed stopped until manual restart,
# freezing data/live/jev_latest.json for 27h). Cadence now lives in this
# loop (pass -> sleep 300 -> pass); pm2 only supervises the process.
#
# Invariants:
#   * singleton — a second copy exits immediately (lock file below), so two
#     passes can never overlap even across a pm2 restart handoff.
#   * bounded pass — a pass that hangs (HTTP stall, DB lock, DNS) is
#     SIGKILLed at PASS_TIMEOUT_S and the loop moves on. The HTTP call
#     itself has its own 30s timeout inside the script.
#   * token budget stays pinned at 4M (counter is at ~2.1M — never raise).
set -euo pipefail
# lives under deploy/macos/ (not copied to root — single source of truth)
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research data/live

SLEEP_S="${JEV_LOOP_SLEEP_S:-300}"
PASS_TIMEOUT_S="${JEV_PASS_TIMEOUT_S:-240}"
LOCK="data/live/jev-judge.lock"

# ── singleton lock (pure bash — macOS has no flock(1)) ─────────────────
# noclobber '>' is atomic O_EXCL create; the file carries the holder pid so
# a lock orphaned by SIGKILL is reclaimed via kill -0.
for _ in 1 2; do
    if ( set -o noclobber; echo $$ >"$LOCK" ) 2>/dev/null; then
        break
    fi
    holder="$(cat "$LOCK" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        echo "jev-judge: another instance alive (pid $holder) — exiting"
        exit 0
    fi
    rm -f "$LOCK"   # stale lock, holder pid is dead — take over
done
[[ -f $LOCK && "$(cat "$LOCK" 2>/dev/null)" == "$$" ]] || {
    echo "jev-judge: could not acquire $LOCK" >&2
    exit 1
}

CHILD_PID=""
cleanup() {
    [[ -n "$CHILD_PID" ]] && kill "$CHILD_PID" 2>/dev/null || true
    rm -f "$LOCK"
}
trap 'exit 0' INT TERM
trap cleanup EXIT

echo "jev-judge: loop start pid=$$ sleep=${SLEEP_S}s pass-timeout=${PASS_TIMEOUT_S}s"
while :; do
    echo "jev-judge: pass start $(date -u +%H:%M:%SZ)"
    ./.venv/bin/python -u -X utf8 scripts/research/jev_shadow_judge.py \
        --once --max-tokens 4000000 &
    CHILD_PID=$!
    # bounded wait: watcher kills a wedged pass so the loop never stalls
    ( sleep "$PASS_TIMEOUT_S"; kill -KILL "$CHILD_PID" 2>/dev/null ) &
    WATCHER=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""
    kill "$WATCHER" 2>/dev/null || true; wait "$WATCHER" 2>/dev/null || true
    # Align each pass to the next wall-clock multiple of SLEEP_S (boundary
    # math in src/utils/cron_loop.py — tested in tests/test_cron_loop.py);
    # flat `sleep 300` would let the cadence drift by the pass runtime.
    # Backgrounded + waited so INT/TERM interrupts it via `wait`, not after
    # the sleep completes.
    echo "jev-judge: pass done, sleeping to next ${SLEEP_S}s boundary"
    ( ./.venv/bin/python -u -m src.utils.cron_loop "$SLEEP_S" \
        >> logs/jev_loop.log 2>&1 || sleep 60 ) &
    CHILD_PID=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""
done
