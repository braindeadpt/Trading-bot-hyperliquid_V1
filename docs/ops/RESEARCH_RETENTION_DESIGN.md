# Research DB Retention — Design (NOT implemented)

> Status: **approved design, awaiting implementation**. Captured 2026-09-23
> so the safety rules are pinned before any code exists.
> Implementation was explicitly deferred until after the VPS migration.

## Problem

`E:\hyperliquid_research\hyperliquid.db` grows ~130 MB/day (3.53 GB on
2026-08-09 → 9.24 GB on 2026-09-23). At this rate it reaches ~50 GB within
a year, making the monthly verified backup, the shadow-outcome evaluator,
and every research read progressively more expensive — especially on HDD.

## Non-negotiable rule: ARCHIVABLE is a closed list

The shadow-outcome evidence path is **full-history**:

- `src/research/shadow_outcome_evaluator.py:1201-1203` calls
  `recorder.load_decisions(..., window_ms=None)` — the 90-day read bound is
  explicitly opted out.
- The evaluator reads **`shadow_decisions`** and **`candles_1m`** (plus
  funding/candle joins inside `evaluate_shadow_decisions`).

Therefore the set of tables that may ever be archived is a **closed list
in code**, not a convention:

```python
# scripts/ops/archive_research_data.py (future)
ARCHIVABLE_TABLES = ("trade_tape", "l2_snapshots")
```

**Required test (must exist before implementation ships):**

```python
def test_archivable_list_never_contains_evidence_tables():
    forbidden = {
        "candles_1m", "candles_5m", "candles_15m", "candles_1h",
        "shadow_decisions", "jev_decisions",
        "feed_health_snapshots", "shadow_outcome_scoreboards",
    }
    assert not (set(ARCHIVABLE_TABLES) & forbidden)
```

A second test should assert `set(ARCHIVABLE_TABLES) == {"trade_tape",
"l2_snapshots"}` so adding a third table is a deliberate, reviewed change —
never an accident.

## Format: monthly SQLite shards (chosen over Parquet)

- **Zero new dependencies** — the repo is pure `sqlite3`; Parquet would add
  pyarrow for a cold-storage format.
- **Queryable in place** — `ATTACH 'archive/trade_tape/2026-08.db' AS aug`
  lets backtests union hot + cold without a loader framework.
- Verified the same way as the backup path: `integrity_check` + row counts
  + sha256 per shard.

Shard layout (relative to the research root so the monthly backup mirrors
it with the existing incremental mechanism):

```
<research_root>/archive/
    trade_tape/2026-08.db          # one closed month per file
    l2_snapshots/2026-08.db
    archive_manifest.json          # table, rows, min/max ts, sha256 per shard
```

## Flow (monthly, same pattern as backup_research_data)

1. For each closed month and each `ARCHIVABLE_TABLES` table:
   stream rows into `archive/<table>/<YYYY-MM>.db` — **bounded reads,
   never materialize the table** (lesson from feed_cadence).
2. Verify the shard: `integrity_check`, `COUNT(*)` equality vs source
   range-count, sha256 → append to `archive_manifest.json`.
3. **Only after a verified shard exists**: `DELETE FROM <table> WHERE
   ts < cutoff`. A failed shard means zero deletions — fail closed.
4. `VACUUM` **quarterly** only — on 9+ GB it is slow on HDD; between
   vacuums the freed pages are reusable, not lost.
5. The monthly backup gains `archive/` as an additional mirrored source
   (same incremental-copy machinery as `l2_books`).

## Hot window

**90 days** stays in the live DB — covers the deepest known read window
(the evaluator's explicit full-history contract touches
`shadow_decisions`/`candles_1m`, which are never archived anyway; 90d is
margin for the cadence diagnostics and ad-hoc research).

Before implementation ships, enumerate the actual deepest reader
(`recorder.load_decisions` callers + feed diagnostics) and record the
number here — do not guess.

## Safety contract

- No delete without a verified shard (count match + sha256 + reopen).
- `restore` subcommand re-imports a month shard if a hole is ever found.
- The archive manifest is itself evidence — backed up monthly.
- `ARCHIVABLE_TABLES` is the only authority on what may leave the live DB.
