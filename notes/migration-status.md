# Migration status — Mac → bots-01

Single source of truth for the Linux deployment state. Updated at each
gate. Mac remains production until Pedro approves cutover explicitly.

## Current state (2026-11-11)

- Host repo (`/srv/hl/app/repo`): `0b33923` (`feat/post-cutover`, merged into `main` via PR #14 `e951943`)
- Image: `hyperliquid-bot:local` = `93664545eb96` (built at `0b33923`); pre-deploy rollback tag `hyperliquid-bot:pre-0b33923` (and `pre-jev-kill`) kept on host
- `config_hash` = `9e9a6c37d02a1331` (re-registered 2026-11-11 via `scripts/ops/reregister_jev_kill_20261111.py`, experiment_id `460f7253-d1f2-42c9-9922-a8b436469498`)
- **JevJudge KILLED (2026-11-11):** final preregistered readout (`PREREGISTER_JEV_OOS_KILL_2026-10-08.md`) reached `indep_n = 325 >= 100`, `net PF = 0.8373 <= 1.0` (`-$679.35 USD`). `strategy.jev_judge.enabled = false`, `execution_strategies = []`, `hl-jev-judge` stopped/removed and gated under `profiles: ["killed"]`.
- **`unlock_shadow.db` WAL + zlib compaction (2026-11-11):** compacted `2.5 GB -> 205 MB` (`PRAGMA integrity_check = ok`), cryptographic readout verified **`1724/1724` snapshots with `0` `sha256` mismatches**. **Disclosure / correction:** `unlock_shadow.db` was **not** in Restic prior to `2026-11-11` (`hl-backup.sh` previously staged only `bot.db` daily and `hyperliquid.db` weekly), and the temporary `.pre-zlib` file was deleted at the end of the compaction script — so the uncompressed pre-zlib copy was lost; the live DB is verified intact and all 5 shadow DBs (`unlock_shadow`, `carry_shadow`, `boros_shadow`, `resmom_shadow`, `latency_probe`) are now included in daily Restic `VACUUM INTO` backups (`hl-backup.sh`).
- Host hardening applied: `/etc/audit/rules.d/oficina.rules` narrowed from `-w /srv -p wa` to `/srv/hl/secrets` + `/etc/hyperliquid` (load average `1.97 -> 0.20`), `hl-db-vacuum.service` set to `User=101000`, `hl-backup.sh` excludes `/etc/hyperliquid/bot.env`, `hl-watchdogs` report dirs fixed.
- Mainnet & `src/multileg/` status: **inert and frozen** — zero live executors, zero HL keys on `bots-01`, Mainnet blocked pending a formal forward preregistered `PASS`.

### Prior state (2026-10-31)

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

## Cutover Mac → bots-01 (2026-11-01) — EXECUTED, Pedro-approved

**Write-stopped window: 00:13:49 → 01:29:56 UTC = 1h16m07s** — exceeds the
<30 min meta → **record for Amendment 1 of the Jev v2 amendment** (see
timeline below for where the time went).

### Sequence

| t (UTC) | Step |
|---------|------|
| 00:12:21 | Pre-flight bots-01: 10/10 up, hash `4077927fec6a880c`, disk 43%, repo `bf06b2d`, image `be1a1e2b5b6c` |
| 00:13:49 | **pm2 stop** (audited wrapper) — hyperliquid, jev-judge, outcome-eval, carry-shadow, unlock-shadow, boros-shadow + crons watchdogs/overnight/backup-monthly; `pm2 save` persisted; lsof = **0 writers** — **WINDOW OPEN** |
| 00:14–00:21 | `VACUUM INTO` on Mac: `bot.db` 6s (430,075,904 B), `hyperliquid.db` 5m26s (9,407,008,768 B — source was 14.7GB, VACUUM compacted ~5GB free pages) |
| 00:21–00:34 | sha256 both + `gzip -1` research → 3,039,251,065 B |
| 00:34–00:45 | Transfer Mac→Windows→bots-01 relay: bot.db.gz 108MB + research split 4× (~4.6MB/s parallel vs ~1MB/s single — home upload bound, direct tailscale pong 263ms) |
| 00:45–00:50 | Reassemble, decompress, **sha256 match both**: bot.db `53769c10…`, hyperliquid.db `07fdcbb5…` |
| 00:50–01:09 | `PRAGMA integrity_check` on received files: bot.db **ok** ~1min, hyperliquid.db **ok** ~19min (normal scheduling) |
| 01:09–01:29 | Table counts verified equal to Mac (research: candles_1m 1,493,009 / jev 3,589 / shadow_decisions 169,391 / scoreboards 3,424 / trades 0 / decision_audit 0; live: trades 681 / candles_1m 824,301) → docker stop 9 writers → swap: current → `*-pre-cutover`, Mac files in, `chown 101000:101000` |
| 01:29:47 | `docker start` 9 writers |
| 01:29:56 | **First write** (`bot.db-wal` created) — **WINDOW CLOSED** |

### Post-swap verification — PASS

- `config_hash` = `4077927fec6a880c` (unchanged)
- Candles fresh <2min on all 4 symbols continuously
- `jev_decisions` advanced on new DB: 3,589 → 3,593 rows, latest 01:29:52
- `shadow_decisions` 169,391 → 169,398
- Healthchecks **5/5 up**
- **0 ERROR/Traceback** across all 10 containers, 01:50→02:05 UTC
- All containers r=0 post-restart

### Preflight incident (designed behavior, resolved)

`hl-paper` refused to boot post-swap — preflight feed check FATAL on stale
candles (expected: the cutover gap is by definition stale data). Resolution:
one boot with `--skip-preflight` (the documented escape hatch), then
re-created a second time after the first 15m candle closed (01:44:59) —
needed because last closed 15m was Mac's 00:00 (>30min stale). Third boot
with normal command at 01:47:33: **"Preflight feed check PASSED — all
contracted feeds fresh"**, compose diff clean (0 leftover edits).

### Where the 76 min went (Amendment 1 evidence)

- ~7min snapshot (VACUUM both DBs)
- ~15min transfer (3.1GB compressed; home-upload-bound ~4.6MB/s with 4
  parallel streams — single stream ~1MB/s would have been ~50min)
- ~19min integrity_check on 9.4GB received research DB (required pre-swap)
- ~20min stop+swap+start cycle (container stop grace dominates)
- ~15min preflight resolution (not planned-for in the meta)

### Rollback state

- Mac: all 9 pm2 targets `stopped` (dump saved), processes intact, files
  untouched — **frozen for 72h** rollback. Rollback = stop bots-01 writers +
  `pm2 start` on Mac from frozen state.
- bots-01: `bot.db-pre-cutover` (454,766,592 B) +
  `hyperliquid.db-pre-cutover` (9,408,016,384 B) preserved at
  `/srv/hl/data/{live,research}/`; `.gz` copies of the Mac DBs kept in
  `/srv/hl/data/cutover-incoming/` (3.1GB).
- Disk post-cutover: 74% (raised by preserved DBs — revisit cleanup after
  the 72h freeze).

## Gate state

- [x] Soak #1 infra validation
- [x] Soak #2 24h clean run — **PASS**
- [x] Post-PASS package deployed (`bf06b2d`)
- [x] 1h smoke — **PASS**
- [x] Manual backup + full restore + integrity — **PASS**
- [x] **Cutover Mac → bots-01 — EXECUTED 2026-11-01** (window 76min > 30min meta → Amendment 1 record)
- [x] Post-cutover soak #3 (24h+) & 72h freeze concluído (`2026-11-04`; 7d continuous run verified `2026-11-11`, `restarts=0`, `OOMKilled=false`)
- [x] PR #6 merge — **MERGED** (`2026-10-27`)
- [x] **Deploy `0b33923` + JevJudge KILL (`config_hash = 9e9a6c37d02a1331`) + `unlock_shadow.db` sha256 `1724/1724` verified (`2026-11-11`)**
- [ ] **Parte C do Mac (`hl-cutover-stage`, `_test_tape_*`, `pm2 delete`) — TRANCADA** até instrução explícita `"OK limpeza"` (zero ficheiros ou processos tocados no Mac)

## Audit Log — Ficheiros Removidos em `bots-01` (`2026-11-11`)

Registo integral e exacto do que foi apagado no host `bots-01` a `2026-11-11` (levando `/dev/sda1` de `45 GB / 47%` para `19 GB / 18.8%`):

| Timestamp UTC | Caminho exacto em `bots-01` | Tamanho | Nota de auditoria |
|---|---|---:|---|
| `2026-11-11 ~13:42` | `/srv/hl/data/research/hyperliquid.db-pre-cutover` | `8.8 GB` | Artefacto de rollback do cutover de `2026-11-01` |
| `2026-11-11 ~13:42` | `/srv/hl/data/live/bot.db-pre-cutover` | `434 MB` | Artefacto de rollback do cutover de `2026-11-01` |
| `2026-11-11 ~13:42` | `/srv/hl/data/cutover-incoming/{bot.db.gz,hyperliquid.db.gz}` | `3.0 GB` | Pacotes `.gz` da transferência de `2026-11-01` |
| `2026-11-11 ~13:42` | `/srv/hl/data/backups/runs/2026-11-02T133145Z_monthly` + `2026-11-04T161621Z_monthly` | `13.0 GB` | **Fora da lista pré-aprovada:** cópias mensais locais geradas no arranque de `hl-backup-monthly` antes de `910874b` |
| `2026-11-11 ~13:42` | `/srv/hl/incoming/bot-smoke.db` | `420 MB` | **Fora da lista pré-aprovada:** cópia do smoke de `2026-10-28` |
| `2026-11-11 ~15:05` | `/srv/hl/app/repo/deploy/linux/compose.smoke.yaml` | `<4 KB` | **Fora da lista pré-aprovada:** ficheiro untracked no checkout |
| `2026-11-11 ~15:11` | `/srv/hl/data/research/unlock_shadow.db.pre-zlib` | `2.5 GB` | **Fora da lista pré-aprovada:** cópia temporária criada às `15:05` antes do `compact_snapshots(vacuum=True)` e apagada automaticamente após `PRAGMA integrity_check == ok` (`1724/1724` `sha256` verificados) |

**Regra em vigor:** A **Parte C do Mac (`hl-cutover-stage`, `_test_tape_*`, `pm2 delete`) permanece estritamente TRANCADA** até ordem explícita `"OK limpeza"`. Nada mais pode ser apagado em nenhum host sem esse `"OK limpeza"` explícito.

## Post-mortem — cutover 2026-11-01 (window 1h16m07s vs meta 30 min)

A meta de 30 min falhou por três causas medidas no timeline:

1. **Transferência** (~15 min): 3.1 GB comprimidos por upload caseiro
   (~4.6 MB/s com 4 streams em paralelo — single-stream ~1 MB/s teria
   custado ~50 min). Não havia ligação directa rápida Mac→bots-01
   (tailscale pong 263 ms).
2. **`PRAGMA integrity_check`** (~19 min): correu sequencialmente sobre a
   DB de research recebida (9.4 GB) antes do swap — passo obrigatório mas
   não orçamentado na meta.
3. **Preflight stale** (~15 min): `hl-paper` recusou boot pós-swap — a
   janela do cutover deixou as candles stale por definição, e a última
   15m fechada era a do Mac às 00:00 (>30 min). O escape hatch
   documentado `--skip-preflight` foi usado **uma vez** para bootear, e
   revertido logo a seguir — o boot final às 01:47:33 correu o comando
   normal com «Preflight feed check PASSED» (compose diff limpo, 0
   edições residuais).

**Lição:** num futuro cutover, o `integrity_check` da DB recebida corre
**em paralelo com a transferência dos restantes artefactos** (ou depois
do start, com o rollback ainda de pé) — é o maior bloco serializável que
não precisa da janela fechada. Snapshot+sha+transfer ficam ~20 min; o
resto da meta era realista.
