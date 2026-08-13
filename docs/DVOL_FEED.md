# DVOL feed — Deribit volatility index → research DB

Background feed that removes the manual-script dependency for the IV-percentile
regime gate: it fetches the Deribit volatility-index daily closes (DVOL for BTC,
the ETH vol index for ETH) and persists them to the research DB, so the gate can
read a stored history instead of re-fetching per run.

## What it is

* `src/data/dvol_feed.py` — canonical DVOL fetch + percentile math + async feed
  + the production accessor.
* Table `dvol_daily` in `data/research/hyperliquid.db`:

  | column | type | notes |
  |---|---|---|
  | `currency` | TEXT | `BTC` / `ETH` |
  | `timestamp_ms` | INTEGER | daily close timestamp |
  | `close` | REAL | index close |
  | `ingested_at_ms` | INTEGER | write time |

  Primary key `(currency, timestamp_ms)` — `INSERT OR REPLACE` upsert, so
  re-fetching is idempotent.

## Config

```yaml
research:
  dvol_feed:
    enabled: true
    interval_hours: 24.0    # fetch cadence (initial fetch backfills on start)
    lookback_days: 60       # how much history to (re-)fetch each cycle
    window_days: 30         # trailing window for the percentile
    currencies: ["BTC", "ETH"]
```

Wired into `main.py` next to the other research feeds; started/stopped with the
bot. Failure is non-fatal (logs a warning; the gate just sees `None`).

## Production accessor

```python
from src.data.dvol_feed import current_dvol_percentile
pct = current_dvol_percentile("BTC")          # None while warmup (<20 closes)
pct = current_dvol_percentile("ETH", ts_ms=…)  # or a specific timestamp
```

Returns the trailing-30d percentile of the **last completed** DVOL day (no
lookahead) — the same value the backtest evidence attaches to a trade, so the
gate in production reproduces the research numbers exactly.

## Shadow-only IV gate (production, not enforced)

The regime router now records the high/low-IV decision for every routed trade
via `TradingEngine._record_iv_gate_shadow`, **without changing execution** —
the backtest gate (`docs/IV_HIGH_ONLY_AB_SPLIT.md`, high_iv = DVOL
percentile(30d) > 66.7) is still INCONCLUSIVE (n=13), so it is observability
only. Each executed trade gets an `iv_gate_shadow` ShadowDecision with
`iv_percentile`, `iv_class` (`high_iv` / `low_iv` / `unknown`) and the
`IV_HIGH_PCT` threshold in the snapshot metadata, so live outcomes can be
compared against the backtest evidence without touching the trade path.

`SOL`/`HYPE` classify against the **BTC DVOL** global proxy (see
`dvol_currency_for`), mirroring `dvol_series_for` in backtest. The record is
skipped when `research.dvol_feed.enabled` is false, and a `None` percentile
records as `unknown` (never blocks).

## Parity guarantee

`fetch_dvol`, `build_iv_percentile`, `iv_pct_at` and `dvol_series_for` are the
canonical copies here; the offline evidence scripts
(`scripts/iv_percentile_regime_gate_test.py`, `iv_high_only_ab_split.py`,
`iv_vs_adx_disagreement.py`) import them, so production and backtest can never
drift on the percentile definition.

## Hash-neutrality (frozen Fase-10 window)

The `research.dvol_feed` subtree is **excluded from `compute_config_hash`**
(see `_sanitize_config_for_hash`) because it is a data-collection schedule, not
a trading/risk parameter — toggling it must not trip the mid-window drift
assert. Pinned by `tests/test_dvol_feed.py::TestHashNeutral`.

## Tests

* `tests/test_dvol_feed.py` — percentile math, DB upsert/load, accessor,
  fetch→persist (mocked HTTP), factory, and hash-neutrality.
* `tests/test_iv_gate_shadow.py` — shadow-only IV recording (high/low/unknown,
  SOL→BTC proxy, router/DVOL-disable skip, non-raising on recorder failure,
  hook ordering before execution).
