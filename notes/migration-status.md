# Migration status — Mac → bots-01

Single source of truth for the Linux deployment state. Updated at each
gate. Mac remains production until Pedro approves cutover explicitly.

## Current state (2026-10-31)

- Host repo: `bf06b2d` (deploy package `eb2ee09 + 42a7161 + bf06b2d`)
- Installed host scripts match repo:
  `hl-backup.sh` sha `de3fd1f6445ad4a2`,
  `hl-disk-guard.sh` sha `f3e39f61f6118d12`,
  `hl-disk-guard.service` sha `09649fe4930a7715`
- Image: `hyperliquid-bot:local` = `be1a1e2b5b6c` (built post-reboot at
  `bf06b2d`); pre-rebuild safety tag `hyperliquid-bot:pre-patch` =
  `03b2fda3f641` (the soak-era image, kept for rollback)
- `config_hash` = `4077927fec6a880c` (unchanged through soak, reboot, smoke)
- Rollback reference commit: `2d2546b2`

## Soak #2 — PASS

- Baseline `45a19a5`, window 2026-10-29 22:20 → 2026-10-30 22:20 UTC
- 10/10 containers: `restarts=0`, `OOMKilled=false`
- `candles_1m`: **1440/1440 per symbol** (BTC, ETH, HYPE, SOL), zero gaps —
  including zero gaps inside the daily 01:02–01:05 maintenance window
- Egress canary (TCP `api.hyperliquid.xyz:443` from hl-paper): continuous, up
- Disk guard: ran ~every 10 min, all SUCCESS; `PAUSE_COLLECTORS` never set;
  disk 33% observed, est. max ~42% (transient VACUUM scratch)
- `metrics.csv` continuous (5268 lines); jev_decisions fresh (164 in window)
- Journal errors: all auditd netlink-buffer noise; container log hits were
  external Telegram API errors (recovered), one designed stop-streak
  circuit message, and watchdog probe `PermissionError` on
  `/opt/hyperliquid/data/backtests` (see backlog)
- `hl-backup-daily` healthcheck was `down` during soak report — consequence
  of the killed 04:00 backup (approved); recovered to `up` after the manual
  run below
- Verdict: **PASS** — feeds, execution, stability and config all clean

### Soak disclosures

1. Mid-soak unit swap 2026-10-29 23:40:24 UTC (`hl-disk-guard.service` →
   `1cb2ef6`): pre-dated the no-touch rule, behavior-equivalent, containers
   untouched — does not invalidate the soak.
2. 04:00 backup run killed (approved): `.backup` API starved at ~160KB/s;
   motivated the `VACUUM INTO` redesign (`42a7161`+`bf06b2d`).
   `bot-pre-reconcile.db` deleted by EXIT trap — accepted, zero impact.
3. Agent error on Mac: malformed restic stub ran `cp -a /` (~7.1 GB to
   /tmp) before being killed; fully cleaned, zero operational impact
   (verified: 803Gi free, 0 pm2 restarts, no ENOSPC hits, candles fresh).
   Rule since: no tests/scripts on the Mac — CI is the proof; real
   bash/sqlite3 scratch lives on bots-01 outside /srv/hl.

## Docker host remediation (2026-10-31)

Symptoms: BuildKit session died (`only one connection allowed` /
`failed to read dockerfile: context canceled`) on every build; `docker pull`
completed but took minutes; daemon eventually wedged in "Initializing
buildkit".

Root causes found:
- **IPv6 blackhole**: no v6 route on host; dockerd retried v6 endpoints
  with long timeouts. `curl -6` failed instantly, `curl -4` 0.5s.
- BuildKit embedded-session bug (dockerd 29.9.2 + buildx v0.38.1) —
  survived restarts; not a version mismatch (client/server both api 1.56).

Fixes applied (persistent):
- `/etc/gai.conf`: `precedence ::ffff:0:0/96 100` (prefer IPv4)
- `/etc/docker/daemon.json`: added `"ipv6": false`; kept
  `"live-restore": true`, userns-remap, icc=false, no-new-privileges,
  json-file 10MiB×3, `userland-proxy: false`, `iptables: true`
- Temporary `/etc/hosts` v4 pins were consumed by cloud-init on reboot
  (manage_etc_hosts) — file verified clean post-boot

Versions recorded: docker CLI/server **29.9.2** (api 1.56, build a82e528),
buildx **v0.38.1** (0dbe87f36), compose **v5.6.1**, storage overlay2, runc.

## Post-reboot verification

- Reboot 19:26→19:29 WET; all 10 containers returned via `unless-stopped`
- `docker info` ~9s (previously wedged); `docker pull hello-world` **2.06s**
  (target <10s) — registry path healthy
- Image rebuild at `bf06b2d`: **<2 min, clean** (previously hung 19–45 min)
- Only affected container recreated: `hl-disk-guard` (consumer of the
  in-image script); the other 9 run code-identical Python (diff touched no
  `src/`)
- Post-boot I/O storm: `hl-backup-monthly` ran a full monthly backup on
  container start (see backlog); sqlite probes briefly D-waited, all
  recovered — no data loss, candles resumed <2 min

## Smoke (2026-10-31 19:58 → 20:59 UTC) — PASS

- 10/10 containers `restarts=0`, `OOMKilled=false` for the full hour
- Candles ≤33s fresh on all 4 symbols at every 5-min sample
- Healthchecks 4/5 up during smoke (`hl-backup-daily` still down pending
  backup — recovered at 23:47); after backup: **5/5 up**
- `hl-disk-guard.service` manual run: finished successfully
- `config_hash` = `4077927fec6a880c` throughout

## Backup #1 post-deploy — manual (2026-10-31)

Run: `systemd-run` transient unit replicating `hl-backup.service`
(EnvironmentFile, Nice=19, IOSchedulingClass=idle) with
`BACKUP_RESEARCH_WEEKDAY=6` (Saturday → weekly path incl. research DB).

| Step | Duration | Notes |
|------|----------|-------|
| `VACUUM INTO hyperliquid.db` | ~14 min | staged 9,407,569,920 B |
| `PRAGMA integrity_check` (research, staged) | **~2h29m** | ~130 GB logical reads; idle-IO strangled (see backlog) |
| `VACUUM INTO bot.db` + check | ~30 s | staged 428,462,080 B |
| `restic backup` upload | **3:23** | `processed 6 files, 9.161 GiB` — dedup effective |
| **Total** | **~2h47m** (21:00→23:47 UTC) | snapshot **`8360f26b`**, hc ping recovered `hl-backup-daily`→up |

## Restore verification (2026-10-31)

- `restic restore 8360f26b --target /tmp/hl-restore-scratch`:
  **17 files/dirs, 9.161 GiB in 2:47** (~55 MB/s)
- `PRAGMA integrity_check` on restored DBs (normal scheduling):
  - `bot.db` (428 MB): **ok** in 1m07s
  - `hyperliquid.db` (9.4 GB): **ok** in 9m47s
- Conclusion: restic repo healthy end-to-end; backups are restorable.

## Ops backlog (host/infra — not strategy work)

1. **BuildKit embedded-session bug**: `only one connection allowed` killed
   every build pre-reboot. Fixed by reboot + ipv6=false, but root defect in
   dockerd 29.9.2/buildx v0.38.1 unknown — watch for recurrence; if it
   returns, evaluate `docker-container` buildx driver (needs moby/buildkit
   image pull).
2. **IPv6 blackhole**: host has no v6 route; any client retrying v6 stalls.
   `gai.conf` precedence + `daemon.json ipv6=false` applied — audit other
   egress tools if slow-network symptoms reappear.
3. **`hl-backup-monthly` runs a full monthly backup on every container
   start**: triggered the post-boot I/O storm (~75 min of ~90% disk util);
   gate the startup pass on "this month's run dir exists".
4. **Backup verify under `IOSchedulingClass=idle` is ~15× slower**
   (2h29m vs 9m47m on 9.4 GB): nightly backup at 04:00 inherits this —
   acceptable for a nightly, but budget the window; consider
   `IOSchedulingClass=best-effort` + low priority, or moving the
   integrity_check off the live box.
5. **Research watchdog probes can't write
   `/opt/hyperliquid/data/backtests`** (PermissionError, 12 tracebacks in
   soak): mount a writable output dir or make `write_report` fail-soft.
6. **SQLite readers can starve under I/O storms**: healthcheck sqlite probe
   D-waited during the post-boot backup — probe has no timeout; consider a
   bounded reader (`timeout` wrapper) so a stuck probe can't block the
   healthcheck service.

## Gate state

- [x] Soak #1 infra validation
- [x] Soak #2 24h clean run — **PASS**
- [x] Post-PASS package deployed (`bf06b2d`)
- [x] 1h smoke — **PASS**
- [x] Manual backup + full restore + integrity — **PASS**
- [ ] **Cutover Mac → bots-01 — AWAITING PEDRO'S EXPLICIT APPROVAL**
- [ ] Post-cutover soak #3 (24h) before decommissioning Mac collectors
