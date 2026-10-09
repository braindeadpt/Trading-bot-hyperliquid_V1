#!/bin/bash
# Shadow outcome eval — long-running loop, supervised by pm2 (autorestart).
#
# Replaces pm2 cron_restart "20 */3 * * *" + autorestart false: pm2's cron
# scheduler double-fired on 2026-10-09 00:20 and SIGKILLed the run mid-write
# (orphaned .shadow_eval.lock → 105 skipped fires, boards frozen ~14h).
# Cadence now lives in this loop: run -> sleep to the next 3h wall-clock
# boundary -> run; pm2 only supervises the process.
#
# Invariants (same skeleton as run_jev_judge.sh):
#   * singleton — a second copy exits immediately (bash lock; SIGKILL
#     orphans reclaimed via kill -0).
#   * bounded run — a wedged evaluation is SIGKILLed at PASS_TIMEOUT_S so
#     the loop can never stall silently.
#   * start/end of every run is logged here AND the evaluator appends to
#     logs/shadow_eval_cron.log (the /ops aux_jobs probe reads both).
set -euo pipefail
# lives under deploy/macos/ (not copied to root — single source of truth)
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
export PYTHONIOENCODING=utf-8
mkdir -p logs data/research

INTERVAL_S="${EVAL_LOOP_INTERVAL_S:-10800}"    # 3h, aligned to wall clock
PASS_TIMEOUT_S="${EVAL_PASS_TIMEOUT_S:-1200}"  # 20min hard kill per run
LOCK="data/research/.outcome_eval_loop.lock"

# ── singleton lock (pure bash — macOS has no flock(1)) ─────────────────
for _ in 1 2; do
    if ( set -o noclobber; echo $$ >"$LOCK" ) 2>/dev/null; then
        break
    fi
    holder="$(cat "$LOCK" 2>/dev/null || true)"
    if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        echo "outcome-eval: another instance alive (pid $holder) — exiting"
        exit 0
    fi
    rm -f "$LOCK"   # stale lock, holder pid is dead — take over
done
[[ -f $LOCK && "$(cat "$LOCK" 2>/dev/null)" == "$$" ]] || {
    echo "outcome-eval: could not acquire $LOCK" >&2
    exit 1
}

CHILD_PID=""
cleanup() {
    [[ -n "$CHILD_PID" ]] && kill "$CHILD_PID" 2>/dev/null || true
    rm -f "$LOCK"
}
trap 'exit 0' INT TERM
trap cleanup EXIT

echo "outcome-eval: loop start pid=$$ interval=${INTERVAL_S}s run-timeout=${PASS_TIMEOUT_S}s"
while :; do
    echo "outcome-eval: run start $(date -u +%FT%TZ)"
    ./.venv/bin/python -u -X utf8 scripts/research/evaluate_shadow_outcomes.py \
        --persist --since-days 14 >> logs/shadow_eval_cron.log 2>&1 &
    CHILD_PID=$!
    # bounded wait: watcher kills a wedged run so the loop never stalls
    ( sleep "$PASS_TIMEOUT_S"; kill -KILL "$CHILD_PID" 2>/dev/null ) &
    WATCHER=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""
    kill "$WATCHER" 2>/dev/null || true; wait "$WATCHER" 2>/dev/null || true
    echo "outcome-eval: run done $(date -u +%FT%TZ); sleeping to next ${INTERVAL_S}s boundary"
    ( ./.venv/bin/python -u -m src.utils.cron_loop "$INTERVAL_S" \
        >> logs/outcome_eval_loop.log 2>&1 || sleep 300 ) &
    CHILD_PID=$!
    wait "$CHILD_PID" || true
    CHILD_PID=""
done
