# Entry Debounce — Live / Backtest Parity

## Purpose

Prevents rapid-fire duplicate entries on the same symbol when multiple strategy
signals or bar events arrive within a short window. Debounce is **deterministic**
and **shared** between live and backtest via `SignalPipeline` / `EntryDebounce`.

## Configuration

| Key | Default | Scope |
|-----|---------|-------|
| `engine.entry_signal_debounce_ms` | `5000` | Engine-level gate (shared) |
| `execution.entry_debounce_ms` | varies | Executor-only second layer (live) |

## Gate order

`entry_debounce` runs in `GATE_ORDER` **after** `feed_health` / `replay_data_quality`
and **before** `cooldown`:

```
feed_health → entry_debounce → cooldown → chase → correlation → risk → tca → routing
```

## Behaviour

1. On each **approved entry**, `SignalPipeline.record_trade_opened()` stores
   `ctx.last_entry_ms[symbol] = event.timestamp_ms`.
2. On subsequent signals for the same symbol, `EntryDebounce.is_blocked()` returns
   `True` if `event.timestamp_ms - last_entry_ms < debounce_ms`.
3. Rejection gate: `entry_debounce` with reason
   `entry_debounce {remaining}ms remaining`.

## Live-only second layer

`ExecutionEngine` may apply an additional `execution.entry_debounce_ms` after the
engine has already approved the signal. This executor debounce is **not replayed**
in backtest (documented in `SignalPipeline.gate_manifest()` under
`intentional_exclusions.executor_debounce`).

## Reproduction

```bash
python tests/test_backtest_live_parity.py   # test_entry_debounce_blocks_rapid_reentry
python tests/test_pre_oos_consolidation.py  # gate_manifest documents debounce_ms
```

Golden test sets `ctx.last_entry_ms["BTC"]` and fires a signal 2s later with
5s debounce — expect rejection at gate `entry_debounce`.
