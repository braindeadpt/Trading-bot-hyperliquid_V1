# Gate Reference — live vs backtest entry-gate contract

**Parity version:** `phase05-gates-v1` (pinned in `src/core/signal_pipeline.py`, `GATE_PARITY_VERSION`)

This is the single source of truth for which entry gates run in live vs
backtest, and how the live feed-health gate is substituted by the replay
data-quality gate. The authoritative runtime copy is `SignalPipeline.gate_manifest()`
— this doc mirrors it and explains the *contract* behind each row. Tests pin
this contract: `tests/test_backtest_live_parity.py` (minimal config) and
`tests/test_production_gate_parity.py` (real `config/settings.yaml`).

---

## 1. The core idea

Live and backtest must run **the same decision chain** on the same signal.
But live has runtime state that a candle replay cannot reconstruct (WS health,
exchange reconciliation, order execution). Those gates are either:

1. **Substituted** — a backtest-only gate stands in for a live gate that has
   no replay equivalent (currently exactly one: `feed_health` → `replay_data_quality`).
2. **Live-only** — run in live, deliberately *not* replayed, and documented as
   an intentional asymmetry.
3. **Shared** — identical code path in both (canonical `GATE_ORDER`).

`gate_manifest()` returns `shared_gate_order`, `live_only_gates`,
`replay_substitutes` and `intentional_exclusions` — the tests assert those
exact values.

---

## 2. Shared gates — canonical order (`GATE_ORDER`)

Both live (`SignalPipeline(for_backtest=False)`) and backtest
(`SignalPipeline(for_backtest=True)`) run these in this exact order.
First rejection wins; the gate name is recorded in the `GateDecision`.

| # | gate | live implementation | backtest implementation | notes |
|---|------|--------------------|------------------------|-------|
| 1 | `feed_health` | `TradingEngine._entry_feed_block_reason` | **substituted** → `replay_data_quality` (see §3) | the only substituted gate |
| 2 | `entry_debounce` | `Debounce.is_blocked` | same code | same object shape |
| 3 | `cooldown` | `Cooldown.is_blocked` | same code | per-strategy, per-symbol |
| 4 | `vol_circuit` | `VolatilityCircuitBreaker` | same class | live ATR(1) from WS; backtest candle-range proxy |
| 5 | `funding_blackout` | `FundingBlackoutFilter` | same class | UTC windows |
| 6 | `chase_filter` | `ChaseFilter` | same class | runup since last close |
| 7 | `correlation` | `CorrelationMonitor` | same class | live positions / backtest simulated |
| 8 | `risk` | `RiskManager` | **same instance** drives both | `max_positions`, stop streak, daily loss, directional/sector caps, kelly |
| 9 | `tca` | strict (needs L2) | proxy (paper slippage) | see §4 |

**Order is pinned** — `tests/test_backtest_live_parity.py::test_gate_sequence_order_is_canonical`
fails if `GATE_ORDER` changes without updating the contract.

---

## 3. The substitution: `feed_health` ↔ `replay_data_quality`

### 3.1 What the live gate does

`TradingEngine._entry_feed_block_reason(symbol)` returns a rejection reason or
`None`. Blocking is toggled by config:

```yaml
market_data:
  block_entries_on_stale: true        # block entries when feed health is red/stale
  block_entries_on_ws_unhealthy: true # block entries when HL WS is unhealthy
  min_exchanges_for_green: 2          # health: yellow if fewer CEX sources
  funding_stale_max_sec: 300          # use last-good values up to 5 min
```

Reasons produced (all prefixed in the pipeline as gate `feed_health`):

| reason | meaning |
|--------|---------|
| `ws_unhealthy` | HL WS client not healthy (`block_entries_on_ws_unhealthy`) |
| `feed_health_pending` | health evaluation has not run yet this process |
| `feed_health_not_ready` | health tracker not ready |
| `feed_red:{symbol}` | this symbol's feed status is `red` |
| `feed_red:overall` | aggregate health is `red` |
| `reconciliation_stale` / `reconciliation_drift:{syms}` / `reconciliation_halted` / `reconciliation_failing` | live-only reconciliation blocks entries (`block_entries_when_stale`), **doubled** through the same `feed_block_fn` (`src/core/reconciliation.py::block_reason`) |

Feed status per symbol is computed by `MarketDataHealthTracker`
(`green` / `yellow` / `red`): both CEX + HL OK → `green`; any `red` in the
window → `red`; CEX OK but HL stale → `yellow` (entries stay allowed — the
"partial outage keeps working" contract, pinned in
`tests/test_production_gate_parity.py::test_feed_health_cex_ok_but_hl_stale_is_yellow`).

### 3.2 What the replay gate does

`ReplayDataQualityGate` (`src/backtest/replay_data_quality.py`) is the
backtest stand-in, built from the same frozen config:

```yaml
backtest:
  replay_data_quality:
    min_coverage_pct: 95.0     # used when parity_mode=false
    max_bar_gap_ms: 120000     # used when parity_mode=false
    max_funding_stale_ms: 300000
    max_oi_stale_ms: 300000
    require_funding: true
    require_oi: false
    # parity_mode: backtest-only; consumer defaults to True
```

Reasons produced (gate name in backtest is `replay_data_quality`):

| reason | meaning | enabled when |
|--------|---------|--------------|
| `replay_quality_no_audit` | no per-symbol audit available for this bar | always |
| `replay_coverage_low:{pct}%<{min}%` | window coverage below min | `parity_mode=false` only |
| `replay_bar_gap:{gap}ms>{max}ms` | bar gap above max | `parity_mode=false` only |
| `replay_funding_missing` | funding series absent | `require_funding=true` |
| `replay_funding_stale:{ms}` | funding older than `max_funding_stale_ms` | `require_funding=true` |
| `replay_oi_missing` | OI series absent | `require_oi=true` |
| `replay_oi_stale:{ms}` | OI older than `max_oi_stale_ms` | `require_oi=true` |

### 3.3 The contract, in one table

| live `feed_health` | backtest `replay_data_quality` |
|--------------------|--------------------------------|
| blocks on WS unhealthy | **not simulated** (replay has no WS) — `ws_unhealthy` has no replay twin |
| blocks on red/stale funding aggregates | blocks on `replay_funding_stale` / `replay_funding_missing` |
| **does not** block on bar gaps / window coverage (live has no such notion) | **disabled in parity mode** — `replay_coverage_low` / `replay_bar_gap` only fire with `parity_mode=false` |
| blocks on reconciliation (live-only) | **not simulated** — reconciliation is live-only (see §5) |
| per-symbol vs overall health | per-symbol audit, no "overall" concept |

The **critical parity invariant** (pinned by
`tests/test_backtest_live_parity.py::test_replay_data_quality_blocks_bar_gap`
and `tests/test_production_gate_parity.py`):

> A backtest run with `parity_mode=true` must **never** kill a signal for a
> reason live could not have killed it. Coverage/gap kills are replay-only
> diagnostics (run `parity_mode=false` for data-QC sweeps). Funding/OI
> freshness are the shared analogue of live's funding-stale blocking.

In code this is a single dispatch point —
`SignalPipeline._check_feed_or_replay_quality()`:
- backtest + `replay_quality` set → `ReplayDataQualityGate.check_entry(...)`
- otherwise → `self._feed_block_fn(symbol)` (live engine's
  `_entry_feed_block_reason`, which also carries reconciliation reasons).

The gate label recorded is `replay_data_quality` in backtest,
`feed_health` in live.

---

## 4. TCA — the strict/proxy split

`tca` is a shared gate in `GATE_ORDER`, but its *mode* differs by design:

| | live | backtest |
|--|------|----------|
| mode | `strict` (`execution.tca_enabled: true`, production) | `proxy` (`backtest.tca_mode: proxy`) |
| without L2 book | **rejects** (`tca_strict_no_l2_book`) | allows, uses paper slippage |
| with L2 book | requires edge ≥ taker fee + slippage + buffer | (not simulated) |

Production fees/buffer pinned in tests: taker 4.5 bp, slip 2 bp, buffer 5 bp.

---

## 5. Live-only gates (never replayed)

`LIVE_ONLY_GATES` in `src/core/signal_pipeline.py`:

| gate | what it does live | why not replayed |
|------|-------------------|------------------|
| `execution_block` | executor-level entry block | execution layer state |
| `fill_ratio` | fill-rate sanity gate | depends on live order routing |
| `slippage_l2` | slippage vs L2 book | L2 not reconstructable from candles |
| `reconciliation_stale` | exchange-vs-local reconciliation | reconciliation is live-only (`_init_live_reconciliation`, testnet/mainnet) |
| `executor_debounce` | second debounce layer in ExecutionEngine | `intentional_exclusions.executor_debounce` — engine-level debounce after pipeline debounce; not replayed |

`reconciliation` is additionally pinned as live-only in
`tests/test_production_gate_parity.py::test_reconciliation_is_live_only_and_unreplayed`:
`replay_substitutes` contains **only** `feed_health → replay_data_quality`.

---

## 5b. Liquidation stop-out exit — parity by construction

The liquidation stop-out (`liquidation_stop_out`) is an **exit-side** parity
invariant: when the rolling 5m liquidation window *validates the position
side* (dominant notional on the SAME side as the position — longs liquidated
under a long, shorts under a short — meaning forced unwinds run against the
open position), the position is stopped out before price stops are evaluated.

| | live | backtest |
|--|------|----------|
| decision function | `liquidation_stopout_decision` (`src/core/liquidation_stopout.py`) | **the same function** |
| window state | `TradingEngine._get_liquidation_stats` (rolling accumulator) | `_advance_liquidation_replay` → `LiquidationAccumulator.stats()` |
| floor | `LIQUIDATION_STOPOUT_MIN_NOTIONAL_USD = 5_000_000` (code, hash-neutral) | same |
| provenance | window entry gated by `liquidation_source` (proxy rejected in `real` mode) | stored label replayed verbatim; decision is provenance-agnostic |

Because both paths call the **same pure function** on the same accumulator
math, live and replay cannot diverge on window state — real or proxy
provenance, same numbers → same stop-out decision, by construction. Pinned by
`tests/test_backtest_live_parity.py::TestLiquidationStopoutParity` (pure
decision, live stats path, replay replication, all four side combos, and the
end-to-end `_process_exits` close).

---

## 6. Config values that gate the contract

| key | minimal test config | production (`config/settings.yaml`) |
|-----|---------------------|-------------------------------------|
| `market_data.liquidation_source` | — | `real` |
| `market_data.block_entries_on_stale` | off | `true` |
| `market_data.block_entries_on_ws_unhealthy` | off | `true` |
| `market_data.min_exchanges_for_green` | — | `2` |
| `market_data.funding_stale_max_sec` | — | `300` |
| `backtest.replay_data_quality.require_funding` | — | `true` |
| `backtest.replay_data_quality.require_oi` | — | `false` |
| `backtest.tca_mode` | proxy | `proxy` |
| `execution.tca_enabled` | `false` | `true` (→ strict live) |

See README §"Parity contract: minimal test config vs production config" for
the full minimal-vs-production table (risk, chase, TCA, trailing, gates).

### 6.1 Gate-key registry (machine-checked)

This is the **exhaustive** registry of gate keys — the mirror that
`tests/test_gate_key_drift.py` checks against `config/settings.yaml`
(`DOCUMENTED_GATE_KEYS`). Every leaf under these prefixes that lives in the
YAML **must** be listed here verbatim (backticked, dotted). Adding a gate key
without updating this registry + README parity table fails CI.

The registry is grouped by gate family; values shown are production
(`config/settings.yaml`) where meaningful.

| gate family | key | production value |
|-------------|-----|------------------|
| risk | `risk.max_positions` | `3` |
| risk | `risk.max_position_size_pct` | `2.0` |
| risk | `risk.taker_fee_pct` | `0.045` |
| risk | `risk.paper_slippage_pct` | `0.02` |
| risk | `risk.per_trade_risk_pct` | `1.0` |
| risk | `risk.max_daily_loss_pct` | `3.0` |
| risk | `risk.max_daily_stop_losses` | `4` |
| risk | `risk.max_slippage_pct` | `0.2` |
| risk | `risk.min_fill_ratio` | `0.8` |
| risk | `risk.circuit_breaker_drawdown_pct` | `10.0` |
| risk | `risk.circuit_breaker_recovery_pct` | `50.0` |
| risk | `risk.symbol_risk_multiplier.SOL` | `0.5` |
| risk | `risk.chase_filter.enabled` | `true` |
| risk | `risk.chase_filter.lookback_hours` | `3.0` |
| risk | `risk.chase_filter.max_runup_pct` | `0.008` |
| risk | `risk.chase_filter.exempt_strategies` | VB, Donchian |
| risk | `risk.volatility_circuit_breaker.enabled` | `true` |
| risk | `risk.volatility_circuit_breaker.multiplier` | `3.0` |
| risk | `risk.volatility_circuit_breaker.baseline_window_bars` | `168` |
| risk | `risk.volatility_circuit_breaker.block_duration_min` | `30` |
| risk | `risk.volatility_circuit_breaker.min_samples` | `24` |
| risk | `risk.funding_blackout.enabled` | `true` |
| risk | `risk.funding_blackout.minutes_before` | `5` |
| risk | `risk.funding_blackout.minutes_after` | `5` |
| risk | `risk.funding_blackout.resets_utc` | 00/08/16 UTC |
| governance | `strategy.portfolio_governance.max_directional_exposure_pct` | `50` |
| governance | `strategy.portfolio_governance.max_sector_exposure_pct` | `100` |
| governance | `strategy.portfolio_governance.max_correlation` | `0.85` |
| governance | `strategy.portfolio_governance.max_correlation_lookback` | `60` |
| governance | `strategy.portfolio_governance.daily_drawdown_circuit_pct` | `3` |
| governance | `strategy.portfolio_governance.daily_drawdown_halt_entries` | `true` |
| governance | `strategy.portfolio_governance.daily_drawdown_flatten` | `true` |
| governance | `strategy.portfolio_governance.daily_drawdown_alert` | `true` |
| liquidation | `market_data.liquidation_source` | `real` |
| liquidation | `market_data.liquidation_okx_enabled` | `true` |
| liquidation | `market_data.liquidation_bybit_enabled` | `true` |
| liquidation | `market_data.liquidation_coinalyze_check` | `true` |
| liquidation | `strategy.liquidation_catcher.require_real_liquidation_data` | `true` |
| liquidation | `strategy.liquidation_catcher.feed_warmup_events` | `1` |
| feed health | `market_data.block_entries_on_stale` | `true` |
| feed health | `market_data.block_entries_on_ws_unhealthy` | `true` |
| feed health | `market_data.block_funding_strategies_on_red` | `true` |
| feed health | `market_data.funding_stale_max_sec` | `300` |
| feed health | `market_data.min_exchanges_for_green` | `2` |
| feed health | `market_data.max_venue_spread` | `0.001` |
| execution | `execution.tca_enabled` | `true` |
| execution | `execution.tca_mode` | `strict` |
| execution | `execution.min_edge_buffer_pct` | `0.05` |
| execution | `execution.entry_debounce_ms` | `5000` |
| trailing | `execution.trailing_stop.enabled` | `true` |
| trailing | `execution.trailing_stop.activation_pct` | `0.01` |
| trailing | `execution.trailing_stop.trail_pct` | `0.008` |
| trailing | `execution.trailing_stop.exclude_strategies` | VB, VWAP, SMF, TrendPyramid |
| replay | `backtest.tca_mode` | `proxy` |
| replay | `backtest.replay_data_quality.min_coverage_pct` | `95.0` |
| replay | `backtest.replay_data_quality.max_bar_gap_ms` | `120000` |
| replay | `backtest.replay_data_quality.max_funding_stale_ms` | `300000` |
| replay | `backtest.replay_data_quality.max_oi_stale_ms` | `300000` |
| replay | `backtest.replay_data_quality.require_funding` | `true` |
| replay | `backtest.replay_data_quality.require_oi` | `false` |
| reconciliation | `reconciliation.enabled` | `true` |
| reconciliation | `reconciliation.interval_sec` | `60` |
| reconciliation | `reconciliation.stale_threshold_sec` | `120` |
| reconciliation | `reconciliation.orphan_exchange_policy` | ADOPT_AND_PROTECT |
| reconciliation | `reconciliation.mismatch_policy` | HALT |
| reconciliation | `reconciliation.block_entries_when_stale` | `true` |

---

## 7. How to verify

```bash
# Runtime manifest (authoritative)
python -c "from src.core.signal_pipeline import SignalPipeline; \
import inspect; print(inspect.getsource(SignalPipeline.gate_manifest))"

# Contract tests (minimal + production configs)
python -m pytest tests/test_backtest_live_parity.py tests/test_production_gate_parity.py -q

# Full CI
python scripts/run_ci_tests.py
```

Changing `GATE_ORDER`, `LIVE_ONLY_GATES`, `replay_substitutes`, or adding a
new gate key to `config/settings.yaml` **requires** updating this doc and the
pinning tests — that is a deliberate, reviewable act.
