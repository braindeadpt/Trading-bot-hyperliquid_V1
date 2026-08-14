# L2 book recording (research) — design & usage

**Purpose:** accumulate reconstructible top-of-book history so a future market-making
study is possible. **Not** a market-making strategy. **Not** written to `data/live/bot.db`.

**Status (2026-08-09):** implemented as `src/data/l2_book_recorder.py`.
Default path: **`data/research/l2_books`**. Mount external storage beneath this
project directory when longer retention is required.
Existing `l2_snapshots` in `hyperliquid.db` store only aggregated mid/spread/OIR/depth
— **insufficient** for fill / adverse-selection simulation. This recorder stores
the actual K levels.

### Role split (do not duplicate)

| Writer | Destination | Payload | Good for | Not enough for |
|--------|-------------|---------|----------|----------------|
| `research_microstructure` (+ sampler/backfill) | `data/research/hyperliquid.db` → `l2_snapshots`, `trade_tape` | Metrics (mid, spread, depth USD, **OIR**) + tape with **side** | OIR gates, CVD/taker diagnostics on ~10/07–09/08 window | MM fill sim, queue position, reconstructible book |
| `l2_book_recorder` | `data/research/l2_books/*.jsonl.gz` | Top-**K levels** + sidecar metrics | Future MM / adverse selection / depth studies | — (this is the depth path) |

Both may subscribe to the same DataBus `orderbook:{symbol}` topic; they serve
different research questions. **Do not** expand `l2_snapshots` to store full
books on the SSD research DB — that is what the HDD recorder is for.

---

## Why the HDD (`E:`), not the system SSD

L2 recording is **append-only, sequential, batched gzip** (~100–160 MB/day). That
workload:

- fits HDD sequential write bandwidth easily;
- avoids continuous write wear on the OS/trading SSD;
- keeps **`data/live/bot.db`** on SSD for random-access trading I/O.

If `E:` is missing or unwritable at start (or mid-run), the recorder logs
**ERROR**, disables itself / drops batches, and **never blocks the trading loop**.
`FeedSilenceMonitor` feed `l2_book_recording` (120s) will alert if recording stops.

---

## Disk estimate

**Measured (2026-08-09, production knobs 2.0s / depth 10 / 4 symbols):**
about **~23 MB/day** gzipped — much lower than the early theoretical
100–160 MB/day (consecutive books compress extremely well).

**Proposed knobs** (await YAML confirmation — past cannot be re-sampled denser):
`interval_sec=1.0`, `depth_levels=25`, `retention_days=365`.

Scaled from the **real** 23 MB/day baseline:

| Scenario | MB/day | GB/year |
|----------|-------:|--------:|
| Low (1.5× interval × 1.4× depth gzip) | ~48 | ~18 |
| Mid (1.8 × 1.7) | ~70 | ~26 |
| High (2.0 × 2.2) | ~101 | ~37 |

E: has ~466 GB free — even the high case is comfortable for multi-year retention.
Re-measure recorder `mb_per_hour` a few hours after any change.

Early theoretical table (kept for history; **do not** use for capacity planning):

| | gzip (est., pre-measurement) |
|--|--:|
| /symbol/day | **~25–40 MB** |
| /day (4 sym) | **~100–160 MB** |
| /month | **~3–5 GB** |
| 90-day retention | **~9–15 GB** |

At `interval_sec=5`: ≈0.4×. At `interval_sec=1`: ≈2× vs the 2s baseline
**before** depth increase — combine with depth scaling as in the mid/high rows above.

Engine queue: `queue_max=5000` + `flush_interval_sec=1.0` remain adequate at
1s/25 levels (enqueue-only on the hot path; flush in a background thread).
Watch `dropped` after restart — raise `queue_max` only if drops appear.

---

## VPS / small-disk migration

On a VPS without a large volume, shrink footprint explicitly:

| knob | home (HDD) | tight VPS |
|------|------------|-----------|
| `path` | `data/research/l2_books` | project-contained research path |
| `retention_days` | **90** | **30** (~⅓ retention cap) |
| `interval_sec` | **2** | **5** (~40% of volume vs 2s) |

`retention_days=30` + `interval_sec=5` ≈ **~1.2–2 GB** steady state vs ~9–15 GB at
home defaults. Document the chosen knobs in YAML whenever you migrate.

---

## Semantics

1. **Sampling:** write if `interval_sec` elapsed **or** material change
   (best bid/ask **price** change, or mid move ≥ `min_mid_change_bps`).
   Size-only BBO flicker does **not** bypass the interval.
2. **Depth:** first `depth_levels` bids and asks from the WS book.
3. **Timestamps:** `exchange_ts_ms` + `received_ts_ms` (latency = difference).
4. **Metrics sidecar:** `spread_pct`, `oir_10`, `depth_quality` via the same
   `calculate_metrics()` as the engine, on the **recorded K levels**.
5. **Silence:** `l2_book_recording` (default max 120s) when enabled.

---

## Layout

```
data/research/l2_books/
  BTC/
    2026-08-09.jsonl.gz
  ETH/
    ...
```

Relative default in `DEFAULT_CONFIG` remains `data/research/l2_books` (CI / machines
without `E:`). Live `config/settings.yaml` must state the real path.

### External paths (research HDD volume) — opt-in, fail-high

The destination is configurable **by design** (research storage may live on
another volume). A path **outside the project root** is honoured only with the
explicit opt-in:

```yaml
market_data:
  l2_recording:
    path: "E:/hyperliquid_research/l2_books"
    allow_external_path: true   # required for destinations outside the repo
```

Without the opt-in an external path is **refused with an ERROR and recording
is disabled** — the recorder NEVER silently redirects to another destination
(2026-08-14 audit: the E: → C: silent regression that split the dataset and
wore the system SSD). Any unusable destination (missing volume, unwritable,
< 512 MB free) disables recording the same way. Trading is never affected.

---

## Config (`market_data.l2_recording`)

```yaml
market_data:
  l2_recording:
    enabled: true
    interval_sec: 1.0          # was 2.0; raised 2026-08-10
    depth_levels: 25           # was 10
    min_mid_change_bps: 1.0
    path: "data/research/l2_books"
    allow_external_path: false # set true to honour external (non-repo) paths
    retention_days: 365        # was 90
    prune_interval_sec: 3600   # start + hourly + stop — not config-only
    queue_max: 5000
    flush_interval_sec: 1.0
  feed_silence:
    l2_book_recording_max_sec: 120
```

---

## Non-intrusive write path

- Subscribes to DataBus `orderbook:{symbol}` (same topic as the engine).
- Sync callback only enqueues (`put_nowait`); never awaits disk I/O.
- Background task batches → gzip append via `asyncio.to_thread`.
- Start probes mkdir + write + free space (< 512 MB → **ERROR**, recorder stays off).
- Refused/external-without-opt-in path → **ERROR**, recorder stays off, never redirected.
- Mid-run write OSError → **ERROR**, drop batch, `disk_ok=false`; trading continues.

---

## Retention (executed, not decorative)

Prune deletes `*.jsonl.gz` whose **filename date** (UTC `YYYY-MM-DD`) is older
than `retention_days`. Runs:

1. on **start**
2. every **`prune_interval_sec`** (default 3600) in the flush loop
3. on **stop**

No touch of other research tables or `bot.db`.

---

## How to read

```python
import gzip, json
from pathlib import Path

def iter_l2(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)

from src.data.orderbook_metrics import PriceLevel, calculate_metrics
row = next(iter_l2(Path("data/research/l2_books/BTC/2026-08-09.jsonl.gz")))
bids = [PriceLevel(p, s) for p, s in row["bids"]]
asks = [PriceLevel(p, s) for p, s in row["asks"]]
m = calculate_metrics(bids, asks, row["symbol"], row["exchange_ts_ms"])
assert abs(m.spread_pct - row["spread_pct"]) < 1e-12
```

Validation CLI:

```bash
python scripts/validate_l2_book_recording.py --path data/research/l2_books
```
