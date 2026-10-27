# Linux Deploy — bots-01 (Oracle A1 arm64) — Fase A packaging

Package-only phase: no trading-logic changes, `config_hash` frozen at
`4077927fec6a880c`. Host sharing contract lives in the infra repo
(`bots-01`, A1.Flex 2 OCPU / 12 GB, userns-remap Docker). Reference only:
branch `feat/vps` (paths, `docs/VPS_MIGRATION.md`, backup scripts) — not merged.

## Layout on the host

```
/srv/hl/
├── data/
│   ├── research/hyperliquid.db      # BOT_RESEARCH_DATABASE_PATH
│   ├── live/bot.db                  # BOT_DATABASE_PATH
│   ├── l2_books/                    # unused — L2 off (see below)
│   ├── backups/                     # staging; host restic is the real backup
│   └── PAUSE_COLLECTORS             # created by disk_guard at >=85%
└── logs/                            # containerised app logs (logrotated)
/etc/hyperliquid/bot.env             # env_file for every service (0600)
/home/admin/.aws                     # mounted read-only -> /home/bot/.aws
```

userns-remap: container uid 1000 → host uid 101000 →
`sudo chown -R 101000:101000 /srv/hl` before first `up -d`.

## Services (one per pm2 process)

| compose service | pm2 equivalent | command |
|---|---|---|
| `hyperliquid` | `hyperliquid` | `main.py --mode paper` (+ dashboard :5000→127.0.0.1:8091) |
| `jev-judge` | `jev-judge` | `jev_shadow_judge.py --once` every 300s boundary |
| `carry-shadow` | `carry-shadow` | `run_carry_shadow.py` (self-loop) |
| `outcome-eval` | `outcome-eval` | `evaluate_shadow_outcomes.py` every 10800s +1200 offset |
| `unlock-shadow` | `unlock-shadow` | profile `gated` — off unless explicitly started |
| `boros-shadow` | `boros-shadow` | profile `gated` — off unless explicitly started |
| `watchdogs` | cron `45 */6` | `research_watchdog_supervisor.py --once` aligned loop |
| `overnight` | cron `0 5` | `overnight_nightly.py` aligned daily loop |
| `backup-monthly` | cron `0 4 1 *` | `backup_research_data.py --tag monthly` ~30d loop |
| `disk-guard` | — (new, Adenda 1) | `disk_guard.sh` every 10 min |

Excluded on Linux: `keep-awake` (macOS `caffeinate`), `wallet-fills`
(disabled on Mac too — commented out in ecosystem.config.js).

## Disk guards (Adenda 1)

- **Container logs**: json-file `max-size 20m max-file 5` on every service.
- **App logs** in `/srv/hl/logs`: `deploy/linux/logrotate/hyperliquid`
  (copy to `/etc/logrotate.d/`).
- **disk_guard.sh**: `df` of `/data` every 10 min — compose service
  `disk-guard` (default) or systemd `hl-disk-guard.timer`.
  - ≥80% → warn log (+ optional `$DISK_GUARD_HC_URL/fail` ping)
  - ≥85% → `PAUSE_COLLECTORS` flag; `collectors_paused()` makes
    `ResearchDatabase.save_*` collectors and the L2 writer skip writes.
    Shadows (`carry/boros/unlock` ledgers are separate DBs) and
    `save_feed_health_snapshot`/`save_coverage_report` keep running.
- **VACUUM monthly**: `hl-db-vacuum.timer` — day 2, 04:30 UTC (after the
  03:30 host restic; outside the :05–:10 and 01:02–01:05 collector windows).
  `BOT_SQLITE_JOURNAL_LIMIT_BYTES` bounds WAL growth per connection.
- **Docker prune weekly**: `hl-docker-prune.timer` — `docker image prune -f`
  + `docker builder prune -f --keep-storage=2GB`. Volume prune is forbidden.

## Retention policy

| Table | Policy |
|---|---|
| `l2_books` (files) | OFF — `BOT_MARKET_DATA_L2_RECORDING_PATH=` empty + `L2_BOOKS_RETENTION_DAYS=0` (env alias; yaml stays 90 on Mac) |
| `candles_1m` | **never purge** — Jev v2 depends on history |
| `trades` | never purge — Jev v2 |
| `trade_tape` | never purge — Jev v2 (see growth warning below) |
| rest | `ResearchDatabase.prune_old_data(days)` unchanged |

## Reservoir (Hydromancer)

Env wiring only — no code reads it this phase:
`AWS_PROFILE=reservoir`, `AWS_REGION=ap-northeast-1`,
`RESERVOIR_BUCKET=hydromancer-reservoir`. The container mounts
`/home/admin/.aws` read-only → `reservoir-ro` key stays on the host,
never in repo/image/logs. Rotate with
`/usr/local/sbin/aws-reservoir-stdin` (pipe the new pair, never paste).

## Sharing with Meridian

- Ports: HL dashboard `127.0.0.1:8091` (tailscale serve `/hl`);
  Meridian `8092` (`/meridian`) — untouched.
- Data budgets: HL ~20 GB (`/srv/hl`), Meridian ~10 GB — the disk guard
  only covers `/srv/hl/data`; a full disk stops HL collectors while the
  Meridian bot keeps writing (its own fs usage is Meridian's concern).

## Cutover (documented only — NOT executed in this phase)

1. On the Mac: `sqlite3 hyperliquid.db "VACUUM INTO '/tmp/hl-snapshot.db'"`
   — compacts ~14 GB → ~9 GB, leaves production untouched.
2. `PRAGMA integrity_check` on the snapshot; `shasum -a 256` both sides.
3. Copy to `bots-01:/srv/hl/data/research/hyperliquid.db`
   (rsync/scp over Tailscale), checksum again on arrival.
4. Keep the Mac snapshot until: checksum OK **and** 24h soak **and** the
   first restic snapshot containing `/srv/hl` exists.
5. The Mac's original DB stays intact until cutover sign-off (rollback).

## Disk budget report (measured on the Mac, 2026-10-27)

- `hyperliquid.db` file: **14 GB** (≈8.6 GB live pages, ~5 GB free pages —
  VACUUM at cutover reclaims it) + WAL
- `bot.db` (live): **418 MB**

Top tables (incl. indexes):

| table | size | growth |
|---|---|---|
| `top_trader_fills`(+idx) | 4.8 GB | fills intake |
| `trade_tape`(+idx) | 3.6 GB | **~110 MB/day → ~3.3 GB/mo** |
| `l2_snapshots`(+idx) | 0.7 GB | off on Linux (0) |
| `candles_1m` | 0.35 GB | ~40 MB/mo (1.5M rows since Jan) |
| `feed_health_snapshots` | 0.37 GB | small |

**6-month projection**: starting from the ~9 GB compacted snapshot,
`trade_tape` alone adds ~20 GB/6mo at current cadence — it will hit the
20 GB HL budget on its own. The guard (85% stop) protects the box, but a
`trade_tape` retention/rollup decision is needed in a later phase —
explicitly out of scope here (Jev v2 reads it).

## Deploy steps on bots-01 (summary)

```bash
sudo mkdir -p /srv/hl/data/{research,l2_books,live,backups} /srv/hl/logs /etc/hyperliquid
sudo cp deploy/linux/bot.env.example /etc/hyperliquid/bot.env
sudoedit /etc/hyperliquid/bot.env && sudo chmod 600 /etc/hyperliquid/bot.env
sudo chown -R 101000:101000 /srv/hl
sudo cp deploy/linux/logrotate/hyperliquid /etc/logrotate.d/hyperliquid
docker compose -f deploy/linux/compose.yaml build
docker compose -f deploy/linux/compose.yaml up -d          # gated services need --profile gated
sudo install -m755 deploy/linux/disk_guard.sh /usr/local/sbin/hl-disk-guard.sh
sudo install -m755 deploy/linux/db_vacuum.sh /usr/local/sbin/hl-db-vacuum.sh
sudo install -m644 deploy/linux/systemd/hl-*.timer /etc/systemd/system/  # optional (compose guard already runs)
sudo systemctl daemon-reload && sudo systemctl enable --now hl-db-vacuum.timer hl-docker-prune.timer
```

Weekly cron alternative for prune if systemd timers are skipped: see timer.

CI note: `docker-arm64` job builds `linux/arm64` on every PR — the same
image the host runs. Pin versions; no `latest` tags anywhere.

## trade_tape — runtime readers & windows (report for the retention decision)

Current growth **~110 MB/day (~3.3 GB/mo)** — hits the 20 GB HL budget in
~6 months alone. Retention untouched this phase; the readers and the window
each needs, for the post-migration decision:

| module | role | window needed |
|---|---|---|
| `src/backtest/data_contract.py` | feed coverage check for backtests | the full requested backtest window (arbitrary — gates on `window_start/end_ms`) |
| `src/backtest/strategy_feed_requirements.py` | declares TRADE_TAPE as required feed per strategy over replay window | the replay window being evaluated |
| `src/data/research_microstructure.py` | microstructure features from tape | sampling window (short — hours/days) |
| `src/data/research_sampler.py` | tape sampling for research feeds | sampling window (short) |
| `src/data/hl_research_backfill.py` | fills gaps, writes `save_trade_tape` | default `days=7` backfill window |

**Implication**: readers need either short sampling windows (microstructure,
sampler, backfill default 7d) or the *arbitrary backtest window*
(data_contract/requirements — which drives Jev evaluations). A time-bounded
retention (e.g. keep-N-months) would cap disk at N×3.3 GB; decide N from the
longest backtest horizon Jev v2 actually replays — revisit after migration.

