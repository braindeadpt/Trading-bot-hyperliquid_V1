// PM2 process specification for the Hyperliquid bot on the Mac host
// (noder@192.168.1.118, app dir /Users/noder/hyperliquid/app).
//
// Replaces ad-hoc `pm2 start ... --cron-restart/--no-autorestart` flags so
// the supervision policy is versioned and reproducible. The run_*.sh
// wrappers this references live in deploy/macos/ and must be copied to
// APP_DIR on a fresh host.
//
// Apply (affects only the apps listed here; meridian-* is untouched):
//   pm2 startOrReload ecosystem.config.js             # all apps below
//   pm2 restart ecosystem.config.js --only hyperliquid
//   pm2 save                                          # persist for resurrect
//
// Policy notes for `hyperliquid`:
// - restart_delay 60s: preflight exits fast on stale candles while
//   auto_backfill_on_start repopulates them over successive attempts.
//   Without a delay pm2 hammered preflight ~1x/s (incident 2026-09-29:
//   69 restarts in 13 min). 60s lets each backfill attempt make progress.
// - min_uptime 120s + max_restarts 15: a process that dies before 2 min
//   (failed preflight, early crash loop) counts as an unstable restart.
//   After 15 unstable restarts (~20-25 min of retry budget) pm2 gives up
//   and leaves the process stopped, so a genuinely broken preflight
//   surfaces as an alert instead of looping forever in silence. A bot
//   that runs >2 min and then crashes is a normal restart and does not
//   consume this budget. Observed auto-backfill recovery needed ~6-8
//   attempts — 15 is deliberate headroom, not a guess.
// - kill_timeout 15s: the engine's graceful shutdown budgets 10s; pm2's
//   default 1.6s SIGKILL would cut it off mid-flush.
// - --skip-preflight must NOT be re-added to run_paper.sh. The goal here
//   is spacing retries, not disabling the guard.
//
// The cron jobs (autorestart:false + cron_restart) are one-shot scripts
// that pm2 re-fires on schedule; they must never autorestart on exit.

const APP_DIR = '/Users/noder/hyperliquid/app';

const cron = (name, script, schedule) => ({
  name,
  script: `${APP_DIR}/${script}`,
  cwd: APP_DIR,
  interpreter: 'bash',
  autorestart: false,
  cron_restart: schedule,
});

module.exports = {
  apps: [
    {
      name: 'hyperliquid',
      script: `${APP_DIR}/run_paper.sh`,
      cwd: APP_DIR,
      interpreter: 'bash',
      autorestart: true,
      restart_delay: 60000,
      min_uptime: 120000,
      max_restarts: 15,
      kill_timeout: 15000,
    },
    cron('jev-judge', 'run_jev_judge.sh', '*/5 * * * *'),
    cron('wallet-fills', 'run_wallet_fills.sh', '7 * * * *'),
    cron('outcome-eval', 'run_outcome_eval.sh', '20 */3 * * *'),
    cron('watchdogs', 'run_watchdogs.sh', '45 */6 * * *'),
    cron('overnight', 'run_overnight.sh', '0 5 * * *'),
    cron('backup-monthly', 'run_backup_monthly.sh', '0 4 1 * *'),
  ],
};
