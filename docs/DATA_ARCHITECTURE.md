# Data architecture — operation vs research vs protection

**Updated:** 2026-08-10
**Principle:** investigation and operation have different lifecycles. They do
**not** need to live on the same volume. That split is what makes a future VPS
migration trivial (ship code + pruned `bot.db`; leave the research corpus at
home).

---

## Storage roles

| Location | Role | Contents | Lifecycle |
|--------|------|----------|-----------|
| Repository | **OPERAÇÃO** | Code, `config/`, `.env` / vault, **`data/live/bot.db`**, hot `logs/` | Random I/O, always online with the paper bot |
| `data/research/` | **INVESTIGAÇÃO** | L2 depth JSONL, `hyperliquid.db`, fills and backtest inputs | Append-heavy; months–years retention |
| `data/backups/` | **PROTECÇÃO** | Verified monthly/annual snapshots | Copy-only; never the write path for the bot |
| **VPS** (future) | **OPERAÇÃO only** | Code + config + **pruned** `bot.db` (~1 GB target) | Research corpus does **not** migrate |

```
                    ┌─────────────────────────┐
   market WS ──────►│  Paper bot              │
                    │  bot.db + code + logs   │
                    └───────────┬─────────────┘
                                │ DataBus orderbook
              ┌─────────────────┼─────────────────┐
              ▼                                   ▼
   research_microstructure              l2_book_recorder
   metrics → hyperliquid.db             depth → data/research/l2_books
   (OIR, tape side)                     (K levels JSONL.gz)
              │                                   │
              └────────────► backup script ◄──────┘
                             (SQLite backup API +
                              incremental closed .gz)
                                      │
                                      ▼
                            data/backups/hyperliquid/
                            (verified manifests)
```

---

## What each store is good for

| Store | Payload | Good for | Not enough for |
|-------|---------|----------|----------------|
| `hyperliquid.db` `l2_snapshots` | mid, spread, depth USD, **OIR** | Gate / OIR studies on ~10/07–09/08 | Queue position, fill sim |
| `hyperliquid.db` `trade_tape` | ticks + **true side** | CVD / taker split (real tape) | — |
| `data/research/l2_books` | top-**K** bids/asks | Future MM / adverse selection | (this **is** the depth path) |
| `bot.db` | trades, candles, portfolio | Live/paper ops + live-vs-replay | Research tape |

See also: `docs/L2_BOOK_RECORDING.md` (role split), `docs/DISK_AND_CODE_INVENTORY.md`.

---

## L2 recording knobs (resolution vs disk)

Measured baseline (2026-08-09, 4 symbols, `interval=2s`, `depth=10`):
**~23 MB/day** compressed — far below the early 100–160 MB/day estimate
(gzip loves near-duplicate consecutive books).

Proposed production knobs (**applied 2026-08-10** in `config/settings.yaml`;
**restart required** for the running process):

| knob | was | now | rationale |
|------|----:|----:|-----------|
| `interval_sec` | 2.0 | **1.0** | Past cannot be re-sampled denser |
| `depth_levels` | 10 | **25** | Queue / deeper adverse-selection studies |
| `retention_days` | 90 | **365** | Long-lived research retention; monitor disk usage |

**Volume estimate from the real 23 MB/day baseline** (not the old theory):

| Scenario | Scale (interval × depth gzip) | MB/day | GB/year |
|----------|-------------------------------|-------:|--------:|
| Low | 1.5 × 1.4 | ~48 | ~18 |
| Mid | 1.8 × 1.7 | ~70 | ~26 |
| High | 2.0 × 2.2 | ~101 | ~37 |

Re-measure `mb_per_hour` from
recorder stats a few hours after any knob change.

**Engine headroom (no YAML change required):**

- Hot path only `queue.put_nowait` — disk I/O is `asyncio.to_thread`.
- Steady floor at 1s × 4 symbols ≈ **4 rows/s**; bursts from BBO changes.
- `queue_max=5000` ≈ **minutes** of burst buffer at tens of rows/s.
- `flush_interval_sec=1.0` drains every second off-thread.
- Conclusion: **keep** `queue_max` / `flush_interval` as-is for the proposed
  resolution; watch `dropped` in recorder stats after restart.

**Requires bot restart** to take effect. Changing `settings.yaml` also
invalidates the Fase 10 frozen-window hash — re-register if that window is
still active.

---

## Backup

Script: `scripts/backup_research_data.py`

### What it does

1. **SQLite** (`hyperliquid.db`, `bot.db`): `sqlite3.Connection.backup` from a
   **read-only** URI source → consistent snapshot while the bot keeps writing
   (WAL-aware; **never** a raw file copy of a live DB).
2. **L2 books**: incremental copy into `data/backups/hyperliquid/l2_books` of
   **closed** daily `*.jsonl.gz` only (`day < today UTC`). Today's open file
   is skipped until the next run.
3. **Verify**: `PRAGMA integrity_check` on the copy; row counts must satisfy
   ``count_before ≤ count_dest ≤ count_after`` (live DBs keep growing during
   the copy — equality with a post-backup source count is a false failure).
   Gzip decompress test on each newly copied `.gz`.
4. **Manifest**: `runs/<UTC>_<tag>/manifest.json` with sizes, checksums,
   counts, `ok` flag.
5. **Retention**: keep last **3 successful monthly** + **1 successful annual**.
   Never prune a failed/`ok:false` run; never prune the newest success per tag.
   On verify failure: leave the bad run for forensics; **do not** delete prior
   good backups.

### Layout

```
data/backups/hyperliquid/
  l2_books/                 # shared incremental mirror of E:
    BTC/YYYY-MM-DD.jsonl.gz
  runs/
    2026-08-10T001500Z_monthly/
      hyperliquid.db
      bot.db
      manifest.json
    2026-12-31T120000Z_annual/
      ...
```

### Space under the retention policy (order-of-magnitude)

| Component | Estimate |
|-----------|----------|
| 3 monthly SQLite pairs | 3 × ~(3.3 + 0.21) ≈ **10.5 GB** |
| 1 annual SQLite pair | ≈ **3.5 GB** |
| L2 mirror (1y @ mid rate) | ≈ **26 GB** |
| **Total** | ≈ **40 GB**; provision the backup volume accordingly |

Origins are **never** deleted by this script (copy, not migrate).

### Manual command

```bash
python scripts/backup_research_data.py --tag monthly
python scripts/backup_research_data.py --tag annual
python scripts/backup_research_data.py --tag monthly --dry-run
```

Verified runs record their outcome in
`data/backups/hyperliquid/runs/<timestamp>_<tag>/manifest.json`. The closed-day
rule skips the current UTC L2 file until the next run; failed manifests remain
available for forensics and are not pruned.
### Windows Task Scheduler — **proposed, not created**

Confirm before creating. Example monthly task (1st of month 03:30 local):

```text
Program:  py
Arguments: scripts/backup_research_data.py --tag monthly
Start in:  <repository>
Trigger:   Monthly, day 1, 03:30
```

PowerShell register sketch (do **not** run until confirmed):

```powershell
$action = New-ScheduledTaskAction `
  -Execute "py" `
  -Argument "scripts/backup_research_data.py --tag monthly" `
  -WorkingDirectory "<repository>"
$trigger = New-ScheduledTaskTrigger -Monthly -DaysOfMonth 1 -At 3:30AM
Register-ScheduledTask -TaskName "HL_ResearchBackup_Monthly" `
  -Action $action -Trigger $trigger -Description "Verified research+ops backup"
```

Annual: same with `--tag annual` on e.g. 31 Dec 04:00.

---

## VPS migration (when ready)

Take to VPS:

- git checkout / release of code
- `config/settings.yaml` (no research HDD paths; L2 recorder off or local tiny path)
- pruned `bot.db` (~1 GB)

Leave on research/backup storage:

- `hyperliquid.db`, fills, L2 books, backtest snapshots, `D:` backups

The bot does not need the research corpus to trade. That is the point of the
split.
