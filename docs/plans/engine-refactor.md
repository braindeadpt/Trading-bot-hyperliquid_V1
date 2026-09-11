# TradingEngine refactor — extraction plan

> **Status:** planned, NOT started. **Gate: do not execute while the Fase-10
> evidence window is live** — any regression mid-window contaminates or kills
> the sample we are collecting. Execute after the window closes or during a
> deliberate maintenance break, phase by phase, with `pre-push` between phases.

**Goal:** shrink `src/core/engine.py` (4665 lines, ~125 methods) into focused
collaborators without changing a single behavior — pure moves + delegation,
verified by the existing suite plus new seam tests.

**Constraints (non-negotiable):**
- Zero behavior change per phase — parity proven by tests + one full replay
  (`scripts/research/profile_backtest.py --days 14`) whose trade set must be
  byte-identical to pre-refactor.
- Never touch `config/settings.yaml`, preregister manifests, or the frozen
  config hash as a side effect.
- `scripts/ops/run_pre_push_gate.py` between phases; a red gate stops the plan.
- Live `TradingEngine` keeps its public API (`start/stop`, dashboard emitters,
  `positions`, `portfolio_snapshot_sync`, …) — `main.py`, `web.py`, tests must
  not need changes.

## Seam inventory (measured 2026-09-11)

| Cluster | Methods | ~Lines | Extraction target |
|---|---|---|---|
| Feed subscription wiring | `_make_price_callback`, `_make_binance_price_callback`, `_make_binance_perp_price_callback`, `_make_ctx_callback`, `_make_orderbook_callback`, `_make_candle_callback`, `_make_liquidation_callback`, `_make_ls_ratio_callback`, `_record_feed_silence_alert`, `_refresh_market_data_health` | ~700 | `core/feed_wiring.py` |
| Liquidation tracking | `_record_liquidation`, `_get_liquidation_source/stats`, `_refresh_liquidation_provenance`, `_accumulate_liquidation_proxy`, `_accepts_liquidation_source` | ~350 | `core/liquidation_tracker.py` |
| Cooldown | `_cooldown_key`, `_is_in_cooldown` (the 75-liner), `_update_cooldown_on_entry/exit` | ~110 | finish move into `signal_pipeline`'s cooldown component (on_entry/on_exit already delegate) |
| Exit pipeline | `_process_exit_signal`, `_maybe_liquidation_stop_out`, `_check_hard_stops`, `_maybe_update_trailing_stop`, `_sync_native_stop`, `_trailing_excluded_for_position`, `_execute_exit`, `_flatten_all_positions*` | ~600 | `core/exit_manager.py` |
| Recovery/persistence | `_recover_state`, `_sync_open_trades_to_portfolio`, `_restore_candles_from_db`, `_inject_candles`, `_persist_runtime_state`, `_save_portfolio_snapshot`, `_maybe_save_snapshot`, `_seed_kelly_from_db` | ~500 | `core/state_recovery.py` |
| Shadow/diagnostic | `_ensure_shadow_recorder`, `_evaluate_shadow_strategies`, `_record_router_blocked_signals`, `_record_iv_gate_shadow`, `_router_block_throttle_ms` | ~400 | `core/shadow_recorder.py` |
| Entry pipeline | `_process_entry_signal` + gates (`_governor_blocks_signal`, `_entry_feed_block_reason`, `_check_chase_filter`, `_price_signal`, `_estimate_fill_ratio`, `_estimate_slippage`) | ~800 | stays in engine last — highest blast radius |

## Phases (each independently testable, each ends in a commit)

1. **Cooldown** — smallest, existing delegation pattern proves the seam.
   Move `_is_in_cooldown` into the pipeline cooldown component; engine keeps a
   3-line delegate. Test: `tests/test_cascade_simulation.py` cooldown cases +
   a new direct unit test on the component.
2. **Feed wiring** — biggest line win, mostly mechanical (callback factories
   close over `self`; move to a `FeedWiring` class holding engine refs).
   Test: `test_engine_boot_integration.py` must pass unchanged.
3. **Liquidation tracker** — cohesive, few entry points.
4. **Shadow recorder** — diagnostic-only, low risk.
5. **State recovery** — moderate risk (boot path); run boot integration test.
6. **Exit pipeline** — highest-risk cluster; needs the full
   `test_critical_fixes.py` + `test_cascade_simulation.py` + replay parity.
7. **Entry pipeline** — last; only if 1–6 landed clean.

## Already-landed perf work (independent of this plan)

- `2a7d007` ADX memoized per 15m close (backtest): −93% adx calls.
- `3c55b58` VWAP stats memoized per 1h close: 53.1M → 17.7M calls total
  (−67%), ~62s → ~45s wall on a 14d replay, identical trade output.
- Reusable profiler: `scripts/research/profile_backtest.py`.
- Remaining hot spots (profile-driven, optional): `_to_indicator_candle`
  (457k calls, 5.2s), `_row_to_candle` (378k, 7.5s — SQLite row→dataclass),
  `_build_market_event` frame overhead (17.9s cumulative — mostly shared).
