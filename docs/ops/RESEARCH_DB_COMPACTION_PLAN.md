# Research DB compaction — measured plan (2026-10-06)

> **SUSPENDED 2026-10-07 — do not execute.** The size problem was solved by stopping
> the collectors nobody reads (wallet-fills stopped; WS tape/recorder off): measured growth
> ~462 MB/day -> ~4-5 MB/day, so no compaction is needed. Kept for the measurements.
>
> **Known defect if this plan is ever revived:** Phase 2 copies the DB (`cp`) *before*
> Phase 3 runs `wal_checkpoint(TRUNCATE)`. A copy taken with an unconsolidated WAL silently
> loses the latest writes, and the count check cannot catch it because it compares against
> the copy itself. The checkpoint must precede the copy and the reference count must come
> from the live DB. Variant (e) also needs a test that inserting the same fill through both
> representations (hex str vs BLOB) yields ONE row — a type mismatch makes `INSERT OR IGNORE`
> stop conflicting, i.e. dedup fails open with no error.

All measurements on **copies**, never the live DB
(`/Users/noder/hyperliquid_research/hyperliquid.db` stays untouched until the
owner approves a written plan). Measurement sources:

- Static backup `runs/2026-10-04T145227Z_monthly/hyperliquid.db` (13.96 GB, read-only)
- Byte copy at `/Users/noder/hyperliquid_research/scratch_dbsize.db` (writes OK)

## What the file looks like today

- 14.70 GB file, ~1.43 GB in free pages (result of the 2026-10-06
  `idx_ttf_wallet_time` DROP — space is inside the file, not returned)
- Growth ~444 MB/day measured across dated backups (11.48 → 12.43 → 13.96 GB)
- `top_trader_fills`: 12,312,798 rows — data 2.53 GB + UNIQUE autoindex
  (wallet,tid,hash) 1.84 GB + `idx_ttf_coin_time` 0.31 GB
- `trade_tape`: 41,847,911 rows — data 4.00 GB + `idx_tape_trade_id` 1.36 GB
  (partial UNIQUE on symbol,trade_id) + `idx_tape_symbol_ts` 0.96 GB

## Readers of `top_trader_fills` (complete list — repo grep)

| File | How it touches the table |
|---|---|
| `scripts/research/wallet_fills_collector.py` | Sole writer — `INSERT OR IGNORE` against `UNIQUE(wallet,tid,hash)`; reads `wallet_fills_cursor` only |
| `scripts/research/wallet_markout_screen.py` | Sole reader — `SELECT wallet,time_ms,side,px,sz WHERE coin=? AND crossed=1 [ORDER BY time_ms]` (lines 84, 129). Uses `wallet` as a Python dict key and prints `w[:12]`. Never selects `id`, `tid`, `hash`, `oid`, `twap_id`. |

Nothing reads the `id` AUTOINCREMENT column — it exists only to burn rowids.
`tests/test_backup_research_data.py` counts rows via `COUNT(*)`. No JOINs,
no FK references. `wallet_fills_cursor` is independent.

## T1 — is (wallet,tid) unique? NO — measured

`COUNT(*)` = 12,312,798 vs `COUNT(DISTINCT wallet||'|'||tid)` = 12,312,794.
**4 colliding pairs**, all `tid = 0` with *different* `hash` and different
coin/px/time — real fills the API emitted with `tid=0` (spot `@`-coin fills,
2024-era rows). `tid` alone is not the API's fill identity; `hash` carries it.
Also measured: zero NULL wallet/tid/hash rows — but the collector writes
`tid=None` when the API omits it, so NULLs can appear in the future (NULL
already defeats dedup today, identically under both key shapes).

**Consequence:** `UNIQUE(wallet,tid)` and `PRIMARY KEY(wallet,tid)` silently
collapse real fills. The only semantics-preserving dedup key is
`(wallet,tid,hash)` — today's key.

## T2 — alternatives, savings MEASURED on the scratch copy

Rebuilt the 12.3M-row table under each schema on the congested Mac
(rebuild wall time is itself a data point for the execution plan).

Baseline: table 2410.9 MB + autoindex 1752.2 MB + coin_time 297.9 MB = **4461.0 MB**

| Variant | dedup index | table | coin_idx | total | saving | rows kept |
|---|---|---|---|---|---|---|
| (a) rowid + `UNIQUE(wallet,tid)` | 797.4 | 2409.5 | 248.7 | **3455.6 MB** | **~1.0 GB** | 12,312,794 (4 lost) |
| (b) `WITHOUT ROWID PK(wallet,tid)` | — | 2694.4 | 812.1 | **3506.5 MB** | **~0.95 GB** | 12,312,794 (4 lost) |
| (d) `WITHOUT ROWID PK(wallet,tid,hash)` | — | 2694.1 | 1640.9 | **4335.0 MB** | **~0.13 GB** | 12,312,798 (none lost) |
| (c) `WITHOUT ROWID` + BLOB wallet(20)/hash(32) | — | 1866.2 | 946.8 | **2813.0 MB** | **~1.65 GB** | 12,312,798 (none lost) |
| (e) rowid + BLOB + `UNIQUE(wallet,tid,hash)` | 943.3 | 1706.3 | 248.7 | **2898.3 MB** | **~1.56 GB** | 12,312,798 (none lost) |

**Key measured insight — WITHOUT ROWID is a bad trade here.** A WITHOUT
ROWID secondary index stores the full PRIMARY KEY in every entry
(≈116 B/row for PK(wallet,tid,hash)): `idx_d_coin` measured **1640.9 MB**
vs 297.9 MB for the rowid equivalent — the secondary index eats the whole
autoindex saving. Variant (d), the only semantics-preserving structural
change, nets a mere ~0.13 GB. WITHOUT ROWID only wins if its PK is small
((b): ~0.95 GB — and (b) still collapses the 4 real tid=0 fills).

Measured rebuild wall-times on the congested Mac: single-statement
`INSERT INTO ... SELECT` ~30 min when the box is quiet (variant d), ~90 min
under contention (variant a); Python 5 K-row batches ~9–22 min (c, e).
The whole rebuild fits inside one maintenance window.

Reader impact: (a)/(b) change dedup semantics (fill loss on tid collisions —
fail-open); (d) keeps exact semantics but nets almost nothing (fat PK in
every secondary index entry); (c)/(e) keep exact semantics — `wallet`/`hash`
become opaque bytes: collector converts `bytes.fromhex(w[2:])` on insert and
the cursor table stores BLOB; markout's `w[:12]` print becomes `.hex()[:14]`.

## T3 — idx_tape_trade_id dedup rate: measured

`COUNT(*)` = `COUNT(DISTINCT symbol||'|'||trade_id)` = 41,847,911 (zero
duplicates retained; `SUM(trade_id IS NULL)` = 0). AUTOINCREMENT rowid gap:
MAX(id) − COUNT = **106,786 rejected attempts** over the table's life —
~0.26% of inserts. The index does real but marginal work (re-ingestion after
reconnects). Whether 1.36 GB for ~107K lifetime rejections is worth keeping
is the owner's call — this plan does not remove it.

## T4 — VACUUM: measured

`VACUUM INTO` of the 13.96 GB backup → **12.76 GB** compacted
(~1.20 GB reclaimed — the DROP-INDEX free pages), in **686 s ≈ 11.4 min**
on the congested host while other sqlite jobs ran. Production window
estimates below.

## T5 — backup + restore safety

- `MONTHLY_KEEP=3`, `ANNUAL_KEEP=1`; only `monthly`/`annual` tags exist;
  unknown tags are kept forever. `--skip-prune` flag exists.
- A pre-migration safety copy: run `backup_research_data.py --tag monthly
  --skip-prune`, then `cp -a` the run dir to `~/hyperliquid_backup/pre_migration/`
  (outside `runs/` so retention can never touch it).
- Restore proof of the current backup: `PRAGMA integrity_check` on the
  2026-10-04 run returned **ok**; `trade_tape`=41,847,911 and
  `top_trader_fills`=12,312,798 match the manifest exactly. The backup is
  restorable — opens clean, integrity ok, counts proven.

## T6 — execution protocol (runbook for a bad day)

Decisions the owner must make first: (i) which `top_trader_fills` schema
variant (see T2 table — recommendation below), (ii) whether
`idx_tape_trade_id` stays (T3), (iii) `trade_tape` retention window
(owner-pending, out of scope here), (iv) VACUUM in the same window
(recommended — the swap is the only exclusive-lock moment).

### Phase 0 — preconditions

- Mac host, ≥ 30 GB free on `/System/Volumes/Data` (scratch copy + vacuum
  target + safety copy coexist).
- `pm2 describe hyperliquid wallet-fills jev-judge` — know what you will stop.
- No `wallet-fills`/`backup-monthly`/`overnight` job in flight
  (`pm2 list`, `pgrep -f 'wallet_fills|backup_research|overnight'`).

### Phase 1 — safety copy outside retention (~20 min)

```
pm2 stop hyperliquid wallet-fills jev-judge   # stop ALL writers
pgrep -fl 'wallet_fills|jev_shadow'            # must be empty
cd ~/hyperliquid/app
./.venv/bin/python -u scripts/ops/backup_research_data.py \
    --tag monthly --skip-prune                 # ~17 min on this host
cp -a ~/hyperliquid_backup/runs/<NEW_RUN> \
      ~/hyperliquid_backup/pre_migration_dbshrink/
```
`--skip-prune` keeps the new run AND all existing runs. The `cp -a` outside
`runs/` makes it invisible to retention forever. Verify the run manifest:
`ok:true, integrity_check:ok, counts_match:true` — abort if not.

### Phase 2 — rebuild on a copy (measured: ~10–30 min quiet, ~90 min worst)

```
cp ~/hyperliquid_research/hyperliquid.db /tmp/rebuild.db
```
Run the chosen variant build on `/tmp/rebuild.db` (the exact SQL of the
approved variant — see T2). Then verify **on the copy**:

```
PRAGMA integrity_check;                         -- must print ok
SELECT COUNT(*) FROM <new_table>;               -- must equal old COUNT(*)
                                                --   (a/b: minus the 4 tid=0 rows)
SELECT COUNT(*) FROM top_trader_fills;          -- sanity vs manifest count
```

Abort criteria: integrity_check not ok, count mismatch beyond the expected
delta, disk below 20 GB at any point.

### Phase 3 — swap (exclusive lock, ~2 min)

```
# live DB quiesced since Phase 1 — nothing may be writing it
sqlite3 ~/hyperliquid_research/hyperliquid.db 'PRAGMA wal_checkpoint(TRUNCATE);'
mv ~/hyperliquid_research/hyperliquid.db \
   ~/hyperliquid_research/hyperliquid.db.premigration
mv /tmp/rebuild.db ~/hyperliquid_research/hyperliquid.db
```
The WAL must be checkpointed before `mv` — `-wal` content lives outside the
.db file. `.premigration` stays until a later verified cleanup.

### Phase 4 — VACUUM (if approved in the same window — ~12 min measured)

Either `VACUUM INTO` then swap as above, or in-place `VACUUM` on the swapped
file (~15 GB temp headroom needed; measured 686 s on this host under load).

### Phase 5 — restart + verify

```
pm2 start hyperliquid wallet-fills jev-judge  # or pm2 restart all
pm2 logs hyperliquid --lines 80                # boot + preflight
sqlite3 ... 'SELECT COUNT(*) FROM top_trader_fills; PRAGMA integrity_check;'
tail logs/wallet_fills_cron.log                # next :07 pass: errors must be 0
```

### Reversal

```
pm2 stop hyperliquid wallet-fills jev-judge
mv ~/hyperliquid_research/hyperliquid.db /tmp/rejected.db
mv ~/hyperliquid_research/hyperliquid.db.premigration \
   ~/hyperliquid_research/hyperliquid.db
pm2 start hyperliquid
```
Worst case beyond that: restore the `pre_migration_dbshrink/` copy the same
way (cp -a back, wal_checkpoint, pm2 start). Nothing in this protocol is
irreversible while `.premigration` and the safety copy exist — do NOT delete
either until the owner confirms a week of clean operation.

### Recommendation

**Variant (e): rowid + BLOB(wallet,hash) + `UNIQUE(wallet,tid,hash)` —
~1.56 GB saved with zero change to dedup semantics.** It shrinks the
autoindex ~46% (943 MB vs 1752 MB) because the key drops ~62 bytes/row,
keeps the `id` column and the slim 249 MB secondary index, and keeps
rowid-append insert order — the cheapest steady-state write path for the
hourly collector. (c) saves ~85 MB more but deletes `id`, bloats the coin
index ~700 MB, and makes inserts scatter on a 52-byte PK — worse ongoing
write path for a table that grows 700 K rows/day. (a) and (b) are dominated
(identical ballpark savings but they collapse real fills where tid repeats —
measured loss, and fail-open on any future collision). (d) is pointless.
`idx_tape_trade_id` keeps 1.36 GB for ~107 K lifetime rejections — keep
unless the owner wants the space; if removed, do it in the same rebuild
window. VACUUM (~12 min) reclaims the existing 1.43 GB of free pages and
can ride the same swap.
