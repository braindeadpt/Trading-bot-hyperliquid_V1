// pm2 process definitions for the Hyperliquid bot host.
//
// Before this file existed the pm2 config lived only in ~/.pm2/dump.pm2, which
// is not in git and cannot be reproduced on another machine. Apply with:
//
//   pm2 start ecosystem.config.js            # all apps
//   pm2 start ecosystem.config.js --only hyperliquid
//   pm2 save                                 # persist for `pm2 resurrect`
//
// Every app runs a run_*.sh wrapper that sources .env and execs the venv
// python, so secrets never appear here. Paths are relative to `cwd`. The
// wrappers are versioned in deploy/macos/ — copy them to the app dir on a
// fresh host.
//
// Only `hyperliquid` is a long-running service. Everything else is a cron job:
// autorestart false + cron_restart, so pm2 fires it on schedule and leaves it
// `stopped` in between — a high restart count on those rows is the number of
// scheduled fires, not failures.

const CWD = __dirname;

// Log paths are pinned explicitly AND merge_logs is on. Without merge_logs pm2
// appends the app id to the filename even in fork mode and even when out_file
// is given, so a delete/re-create silently moves the log aside
// (hyperliquid-out.log -> hyperliquid-out-26.log, then -27, observed
// 2026-10-04), breaking log continuity and every grep that targets the
// canonical name.
const LOGS = `${process.env.HOME}/.pm2/logs`;

const cron = (name, script, schedule) => ({
  name,
  script,
  cwd: CWD,
  interpreter: 'bash',
  exec_mode: 'fork',
  instances: 1,
  autorestart: false,
  cron_restart: schedule,
  merge_logs: true,
  out_file: `${LOGS}/${name}-out.log`,
  error_file: `${LOGS}/${name}-error.log`,
});

module.exports = {
  apps: [
    {
      name: 'hyperliquid',
      script: 'run_paper.sh',
      cwd: CWD,
      interpreter: 'bash',
      exec_mode: 'fork',
      instances: 1,
      autorestart: true,
      max_memory_restart: '3G',
      merge_logs: true,
      out_file: `${LOGS}/hyperliquid-out.log`,
      error_file: `${LOGS}/hyperliquid-error.log`,

      // Engine graceful shutdown budgets 10s internally; pm2's default
      // kill_timeout (1.6s) SIGKILLs it mid-flush. 15s lets it finish.
      kill_timeout: 15000,

      // Incident 2026-09-29 05:07: preflight failed on cold candles and pm2
      // restarted roughly once per second until 05:20 (~69 restarts logged).
      // It was self-healing — the failure count fell 8 -> 7 -> 5 -> 4 -> 3 -> 1
      // as auto_backfill_on_start repopulated candles — but with no delay pm2
      // hammered the check instead of giving the backfill time to finish. 60s
      // turns that episode into ~6 spaced attempts.
      restart_delay: 60000,

      // max_restarts is deliberately NOT capped. Giving up would leave the bot
      // silently down, and silent downtime is the more expensive failure: the
      // bot sat stopped for 15 days in August 2026 and the L2 recording for
      // that window is gone for good (no API serves it retroactively). Better
      // to keep retrying slowly and let the feed-silence alerts surface it.
      // The alert path is the `watchdogs` cron job below — it builds its own
      // notifier from config+env (independent of this process) and already
      // alerts on a dead/frozen trading process, so an uncapped retry loop is
      // not silent either. If capped restarts are ever wanted, pair them with
      // a shorter watchdog interval first — today it fires every 6h.
      //
      // Do NOT add --skip-preflight to run_paper.sh to stop a restart loop.
      // That flag was removed on 2026-09-29 precisely because it masked stale
      // candles and dead feeds for days. Read the report instead:
      //   ./.venv/bin/python -X utf8 scripts/ops/preflight_feed_check.py
    },

    // Jev verdict feeder — the macOS equivalent of the Windows scheduled task
    // Hyperliquid-Jev-Shadow. Refreshes data/live/jev_latest.json, a contracted
    // feed (jev_verdicts) on the FeedSilenceMonitor: if this stops, the bot
    // alerts. The script asks Jev at most 1x/hour/symbol, so most 5-minute
    // fires are no-ops by design.
    cron('jev-judge', 'run_jev_judge.sh', '*/5 * * * *'),

    cron('wallet-fills', 'run_wallet_fills.sh', '7 * * * *'),
    cron('outcome-eval', 'run_outcome_eval.sh', '20 */3 * * *'),
    cron('watchdogs', 'run_watchdogs.sh', '45 */6 * * *'),
    cron('overnight', 'run_overnight.sh', '0 5 * * *'),
    cron('backup-monthly', 'run_backup_monthly.sh', '0 4 1 * *'),
  ],
};
