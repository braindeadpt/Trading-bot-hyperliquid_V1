# Testnet End-to-End Test Suite (Fase 10)

`tests/test_testnet_e2e.py` places **real orders** against Hyperliquid's
public **testnet** (fake money, real order-matching engine). This is the
final validation gate before any mainnet canary is considered, and it is the
tier *above* the mocked-SDK tests (`tests/test_hyperliquid_sdk.py`,
`tests/test_execution_engine_routing.py`), which exercise the same code
paths with a fully mocked SDK and never touch the network.

## What you need

- A **funded Hyperliquid testnet account** (testnet has no real value — get
  test funds via Hyperliquid's testnet faucet/UI).
- The account's private key, set as the environment variable
  `HYPERLIQUID_PRIVATE_KEY` (64 hex chars, with or without a `0x` prefix —
  see `src/exchanges/hyperliquid_live.py::normalize_private_key`).

  **This must be a TESTNET-ONLY key. Never use a mainnet key here.** The
  suite places real orders and, in the kill-switch/crash-recovery scenarios,
  deliberately flattens whatever positions it finds on the account. Using a
  mainnet key would mean real financial exposure and possible unintended
  liquidation of unrelated mainnet positions.
- Optionally override the market/sizes used (defaults are conservative):
  - `HYPERLIQUID_TESTNET_SYMBOL` (default `BTC`)
  - `HYPERLIQUID_TESTNET_SIZE` (default `0.001`)
  - `HYPERLIQUID_TESTNET_PARTIAL_SIZE` (default `5.0`, used only by the
    partial-fill scenario to increase the odds of crossing the book)

The suite reads credentials the same way production code does
(`resolve_private_key()` — env var first, then the encrypted vault). It does
**not** read or touch `.env` directly, and it never logs the key value.

## Running it

Run just this suite:

```bash
python -m pytest tests/test_testnet_e2e.py -v -m testnet_live
```

Run everything except this suite (the default for CI / day-to-day dev — no
testnet credentials required, and none of these tests will even attempt a
network call):

```bash
python -m pytest -m "not network and not testnet_live" -q
```

Without `HYPERLIQUID_PRIVATE_KEY` configured, every test in this module
**skips** (not errors, not fails) with a message pointing back at this file.
That skip path is exercised and confirmed as part of the change that
introduced this suite — see the PR/commit description for the exact
`pytest -v` output.

## Scenarios covered

1. **Maker order** — post-only (`Alo`) limit far from mid, confirms it rests
   on the book via `get_open_orders`, cancels it.
2. **Market order** — market entry, confirms the position appears via
   `get_user_state`/`get_positions`, market-closes it.
3. **Partial fill** — best effort only (see Limitations below).
4. **Cancel** — limit far from market, cancel, confirm it's gone from
   `get_open_orders`.
5. **Native trigger** — market entry + `NativeProtectionManager.ensure_protection`
   (the same path `ExecutionEngine.place_native_stop_loss` /
   `place_native_take_profit` use), confirms the SL/TP reduce-only trigger
   orders are visible on the exchange, then cancels them.
6. **Crash/restart recovery** — one `ExecutionEngine` opens a position and is
   abandoned mid-flight (no graceful `close()`, simulating a killed
   process); a fresh `ExecutionEngine` + `Database` (same sqlite file) then
   recovers via `load_open_trades()` + `ExchangeReconciler.reconcile_once()`
   and the test asserts the position is neither lost nor double-opened.
7. **Orphan position** — an order is placed directly through the raw
   `HyperliquidLiveClient`, bypassing `ExecutionEngine`/the local DB
   entirely, then `ExchangeReconciler.reconcile_once()` is run against an
   empty local portfolio. Confirms the reconciler flags it as
   `orphan_exchange` and, per the default policy
   (`reconciliation.orphan_exchange_policy: ADOPT_AND_PROTECT` in
   `config/settings.yaml`), adopts it into the local portfolio rather than
   halting or flattening it.
8. **Kill switch** — with an open position and a resting limit order,
   `ExecutionEngine.kill_switch()` is called and the test asserts all orders
   are cancelled, the position is flattened, `confirm_flat()` reports flat,
   and a follow-up reconciliation pass sees no drift.

Each scenario is a stand-alone test function marked `@pytest.mark.testnet_live`.

## Cleanup discipline ("safe to run repeatedly")

Testnet accounts accumulate cruft (stray orders, open positions) if tests
leave things behind. Every test here follows try/finally cleanup:

- Any order placed is cancelled in a `finally` block, even if an assertion
  above it failed.
- Any position opened is market-closed in a `finally` block.
- Helper functions `_safe_cancel_all` / `_safe_flatten` swallow exceptions
  during cleanup itself (a cleanup step failing must never mask the real
  test failure, and must never crash the test run) — but they are still
  attempted unconditionally.
- The crash/restart and kill-switch tests additionally re-fetch positions
  in `finally` before closing the client, since the "current" position/order
  state may have changed during the test body.

This means the suite is idempotent to run repeatedly: a clean pass ends with
the account flat and order-free, and even a failing run makes a best-effort
attempt to leave the account flat.

## Known limitations

- **Partial fill (`test_partial_fill_tracked_correctly`)**: there is no
  reliable way to *force* a partial fill on a real exchange — it depends on
  whatever liquidity exists in the testnet order book at the moment the test
  runs. The test places an oversized, aggressively-priced limit order (large
  size, price crossing the book) to make a partial fill *likely*, then
  asserts that whichever of the three possible outcomes actually
  happened (full fill / partial fill / no fill at all) is handled
  coherently, rather than asserting a specific partial-fill outcome as a
  hard requirement. Treat a "no fill" or "full fill" result as an
  environment/liquidity artifact, not a bug, unless the OMS bookkeeping
  itself looks wrong.
- **Orphan position (`test_orphan_exchange_position_adopted`)**: the mismatch
  is manufactured by using a brand-new, empty `PortfolioState` rather than by
  making a real local DB entry actually go missing mid-run. This proves the
  reconciler's `ADOPT_AND_PROTECT` code path end-to-end against a real
  exchange response, but does not cover a local DB row disappearing out from
  under a *live*, populated portfolio — that case is covered at the
  unit/integration_offline level with mocks (`tests/test_reconcile.py`).
- **Mid-price estimation**: there's no dedicated L2-orderbook helper on
  `HyperliquidLiveClient`, so the "far from market" / "marketable" price
  helpers in this suite (`_mid_price`) fall back to a hardcoded rough
  estimate if `get_user_state()` doesn't have a usable reference price
  (e.g., no existing position for that asset). This is fine for "clearly
  won't fill" vs. "clearly aggressive" limit prices but is not a precise
  mid-price feed.
- **No dedicated market data fixture**: the suite talks to
  `HyperliquidLiveClient` / `ExecutionEngine` / `ExchangeReconciler`
  directly rather than booting a full `TradingEngine` (which additionally
  needs a live `DataBus`, strategy list, and risk manager). Those inputs are
  execution/recovery plumbing unrelated to what these scenarios are
  validating; `TradingEngine`'s own recovery methods
  (`_recover_state`, `kill_switch`) are thin wrappers around the same
  `ExecutionEngine`/`ExchangeReconciler` calls exercised directly here.
- **Timing**: a few tests use a fixed `asyncio.sleep(1.0)` after a market
  order to let the exchange settle before polling state. On a slow or
  congested testnet this could theoretically be too short; if you see
  intermittent "position not found" failures, that's the first thing to
  check.
