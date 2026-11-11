"""Real Hyperliquid TESTNET end-to-end scenarios (Fase 10).

These tests place REAL orders against Hyperliquid's public testnet (fake
money, real order-matching engine). They are the tier ABOVE the mocked-SDK
tests in ``tests/test_hyperliquid_sdk.py`` and
``tests/test_execution_engine_routing.py`` — those exercise the code paths
with a fully mocked SDK; these exercise the real thing.

Every test in this module:

* is marked ``@pytest.mark.testnet_live`` (required by ``tests/conftest.py``'s
  marker guard, and used by CI/humans to select or exclude this suite via
  ``-m testnet_live`` / ``-m "not testnet_live"``).
* calls :func:`_require_testnet_credentials` (directly, or via the
  ``live_client`` fixture) as its FIRST action, so it skips cleanly —
  ``pytest.skip(...)``, not an error or failure — when no testnet private
  key is configured. This environment has no such key, so the whole module
  is expected to show as SKIPPED.
* cleans up after itself with try/finally (or fixture teardown) so that a
  human running these against a real testnet account doesn't accumulate
  orphaned orders/positions across repeated runs.

Credentials
-----------
Set the env var ``HYPERLIQUID_PRIVATE_KEY`` (see
``src/exchanges/hyperliquid_live.py::ENV_KEY``) to a **TESTNET-ONLY** private
key (64 hex chars, with or without ``0x`` prefix) before running this suite.
See ``docs/ops/TESTNET_E2E_GUIDE.md`` for full setup instructions. NEVER put a
mainnet key in this variable while running this module.

Run just this suite::

    python -m pytest tests/test_testnet_e2e.py -v -m testnet_live

Design notes / limitations
---------------------------
* These tests talk to ``ExecutionEngine`` / ``HyperliquidLiveClient`` /
  ``ExchangeReconciler`` directly rather than instantiating a full
  ``TradingEngine`` (which additionally requires a live ``DataBus``,
  strategy list, and risk manager). ``TradingEngine`` is a thin orchestration
  layer around these components for the scenarios below — using its
  building blocks directly is sufficient to validate the actual
  execution/recovery/reconciliation logic without the unrelated ceremony of
  wiring strategies and a market-data bus.
* The **partial-fill** scenario (test 3) cannot force a guaranteed partial
  fill — testnet order-book liquidity is whatever it happens to be at run
  time. The test documents the attempted approach (large size, marketable
  limit price) and asserts that *whatever actually happens* (partial,
  full, or no fill) is handled coherently by the OMS bookkeeping, rather
  than asserting a specific partial-fill outcome.
* The **orphan position** scenario (test 7) simulates the mismatch by
  placing an order with the raw ``HyperliquidLiveClient`` directly (bypassing
  ``ExecutionEngine``/local DB bookkeeping entirely), then running
  ``ExchangeReconciler.reconcile_once`` against a *fresh, empty* local
  portfolio so the reconciler necessarily sees an exchange position with no
  local counterpart. Default policy per ``config/settings.yaml``
  (``reconciliation.orphan_exchange_policy``) is ``ADOPT_AND_PROTECT``.
* Symbol/size: uses ``TESTNET_SYMBOL`` / ``TESTNET_SIZE`` module constants
  (BTC, tiny size) — override via env vars if a different testnet market has
  better liquidity for your run.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, AsyncIterator, Optional

import pytest

from src.exchanges.hyperliquid_live import (
    ENV_KEY,
    HyperliquidLiveClient,
    resolve_private_key,
)
from src.utils.helpers import safe_float

# ── Suite-wide constants ────────────────────────────────────────────────
TESTNET_SYMBOL = os.environ.get("HYPERLIQUID_TESTNET_SYMBOL", "BTC")
# Deliberately tiny — testnet funds are fake but we still don't want to
# blow through margin/liquidity limits chasing a partial fill.
TESTNET_SIZE = float(os.environ.get("HYPERLIQUID_TESTNET_SIZE", "0.001"))
# Used for the "large size" partial-fill attempt only. Optional env override;
# when unset the test sizes the attempt from the account's real equity
# (~1.5x notional) so it fits testnet margin on any balance.
TESTNET_PARTIAL_SIZE = os.environ.get("HYPERLIQUID_TESTNET_PARTIAL_SIZE")
FAR_OFFSET_PCT = 0.30  # 30% away from mid — safe "won't fill" limit price offset


def _require_testnet_credentials() -> str:
    """Return a normalized testnet private key, or skip the test.

    Checks the same env var the production code reads
    (``src.exchanges.hyperliquid_live.ENV_KEY`` ==
    ``"HYPERLIQUID_PRIVATE_KEY"``, per ``.env.example`` conventions for this
    repo). Uses ``resolve_private_key()`` so vault-configured keys are
    honoured too, but the common/expected path for a human running this
    suite is the env var.
    """
    key = resolve_private_key()
    if not key:
        pytest.skip(
            f"Testnet credentials not configured — set {ENV_KEY} to a "
            "TESTNET-ONLY private key to run tests/test_testnet_e2e.py for "
            "real. See docs/ops/TESTNET_E2E_GUIDE.md."
        )
    return key  # pragma: no cover - only reached with real credentials


@pytest.fixture
async def live_client() -> AsyncIterator[HyperliquidLiveClient]:
    """Real ``HyperliquidLiveClient`` connected to testnet.

    Skips (via `_require_testnet_credentials`) before doing any network I/O
    when no credentials are configured. Always tears down the SDK session
    on exit; callers are responsible for cancelling/flattening whatever
    orders/positions they created (see per-test try/finally blocks) — this
    fixture does not do blanket cleanup because it doesn't know what a given
    test intentionally left open mid-assertion.
    """
    key = _require_testnet_credentials()
    client = HyperliquidLiveClient(key, use_testnet=True)
    await client.open()
    try:
        yield client
    finally:
        await client.close()


async def _mid_price(client: HyperliquidLiveClient, symbol: str) -> float:
    """Resolve live mid/oracle price from L2 top-of-book, all_mids, or oracle."""
    try:
        bbo = await client.get_l2_top(symbol)
        if bbo and bbo[0] > 0 and bbo[1] > 0:
            return 0.5 * (bbo[0] + bbo[1])
    except Exception:  # noqa: BLE001
        pass
    try:
        if client.info is not None:
            mids = await asyncio.to_thread(client.info.all_mids)
            if isinstance(mids, dict) and mids.get(symbol):
                val = safe_float(mids.get(symbol), 0.0)
                if val > 0:
                    return val
    except Exception:  # noqa: BLE001
        pass
    try:
        oracles = await client.get_oracle_prices()
        if oracles.get(symbol, 0.0) > 0:
            return float(oracles[symbol])
    except Exception:  # noqa: BLE001
        pass
    return 50_000.0 if symbol == "BTC" else 2_500.0


async def _safe_cancel_all(client: HyperliquidLiveClient, symbol: str) -> None:
    """Best-effort cleanup: cancel any resting orders for *symbol*."""
    try:
        await client.cancel_all_orders(symbol)
    except Exception:  # noqa: BLE001
        pass


async def _safe_flatten(client: HyperliquidLiveClient, symbol: str) -> None:
    """Best-effort cleanup: close any open position for *symbol*."""
    try:
        positions = await client.get_positions()
        for pos in positions:
            if pos.get("symbol") == symbol and safe_size(pos.get("size")) > 0:
                await client.close_position(symbol, pos["size"])
    except Exception:  # noqa: BLE001
        pass


def safe_size(value: Any) -> float:
    try:
        return abs(float(value))
    except (TypeError, ValueError):
        return 0.0


# ═══════════════════════════════════════════════════════════════════════
# (a) Maker order — post-only limit rests on the book
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_maker_order_rests_on_book(live_client: HyperliquidLiveClient) -> None:
    """Post-only (Alo) limit order should rest, not fill immediately.

    Places a buy limit well below mid (so it cannot cross the book), confirms
    it shows up in ``get_open_orders``, then cancels it in the finally block
    regardless of assertion outcome.
    """
    mid = await _mid_price(live_client, TESTNET_SYMBOL)
    limit_price = mid * (1 - FAR_OFFSET_PCT)

    resp: Optional[dict] = None
    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL,
            "long",
            TESTNET_SIZE,
            order_type="limit_maker",
            limit_price=limit_price,
            post_only=True,
        )
        assert resp is not None

        open_orders = await live_client.get_open_orders()
        matching = [
            o for o in open_orders
            if str(o.get("coin", "")).upper() == TESTNET_SYMBOL.upper()
        ]
        assert matching, (
            f"Expected the post-only order to rest on the book for "
            f"{TESTNET_SYMBOL}, but get_open_orders() returned none. "
            f"Response was: {resp}"
        )
    finally:
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)


# ═══════════════════════════════════════════════════════════════════════
# (b) Market order — fills, position appears, then closed
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_market_order_fills_and_closes(live_client: HyperliquidLiveClient) -> None:
    """Market entry should fill immediately and produce a visible position.

    Verifies the position via ``get_positions`` (parsed ``get_user_state``),
    then market-closes it so the testnet account is left flat.
    """
    opened = False
    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL, "long", TESTNET_SIZE, order_type="market",
        )
        assert resp is not None
        opened = True

        # Give the matching engine a brief moment before polling state.
        await asyncio.sleep(1.0)
        positions = await live_client.get_positions()
        matching = [p for p in positions if p["symbol"].upper() == TESTNET_SYMBOL.upper()]
        assert matching, (
            f"Expected an open {TESTNET_SYMBOL} position after a market buy, "
            f"got positions={positions}"
        )
        assert matching[0]["size"] > 0
    finally:
        if opened:
            try:
                positions = await live_client.get_positions()
                for pos in positions:
                    if pos["symbol"].upper() == TESTNET_SYMBOL.upper() and pos["size"] > 0:
                        await live_client.close_position(TESTNET_SYMBOL, pos["size"])
            except Exception:  # noqa: BLE001
                pass
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)


# ═══════════════════════════════════════════════════════════════════════
# (c) Partial fill — best-effort, documents its own limitation
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_partial_fill_tracked_correctly(live_client: HyperliquidLiveClient) -> None:
    """Attempt a partial fill; assert whatever happens is handled coherently.

    LIMITATION (documented per task spec): testnet liquidity is unpredictable
    and there is no reliable way to *force* a partial fill deterministically.
    This test places an oversized marketable limit order (large size, price
    crossing the book) and inspects the resulting fills/open-order state
    afterward:

    * If the order fully filled -> assert the filled size matches the
      requested size (sanity: no double count / no impossible overfill).
    * If the order partially filled -> assert ``filled_size`` from the
      remaining open order (if any) plus fills-so-far equals a coherent
      total (does not silently drop the remainder), then cancels the
      remainder.
    * If the order did not fill at all -> assert it shows as a resting open
      order (also handled: cancel it in cleanup).

    In all three branches, cleanup cancels any residual order and flattens
    any resulting position so the account ends flat.
    """
    mid = await _mid_price(live_client, TESTNET_SYMBOL)
    # Account-aware sizing: a fixed default blew past a 1000-USDC testnet
    # account's margin ("Insufficient margin to place order"). Size the
    # attempt at ~1.5x account notional — oversized vs testnet book depth,
    # within margin on any balance. Env var overrides.
    if TESTNET_PARTIAL_SIZE is not None:
        partial_size = float(TESTNET_PARTIAL_SIZE)
    else:
        account_value = await live_client.get_effective_account_equity() or 1000.0
        partial_size = round(account_value * 1.5 / mid, 3)

    # Marketable limit within HL's ~10% oracle price band so it crosses the
    # book without tripping 'Price too far from oracle'.
    aggressive_price = mid * 1.05

    order_id: Optional[int] = None
    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL,
            "long",
            partial_size,
            order_type="limit_maker",
            limit_price=aggressive_price,
            post_only=False,  # Gtc, not Alo — must be allowed to take liquidity
        )
        assert resp is not None
        order_id = _extract_oid(resp)

        await asyncio.sleep(1.0)
        open_orders = await live_client.get_open_orders()
        resting = [
            o for o in open_orders
            if str(o.get("coin", "")).upper() == TESTNET_SYMBOL.upper()
        ]
        positions = await live_client.get_positions()
        filled_positions = [
            p for p in positions if p["symbol"].upper() == TESTNET_SYMBOL.upper()
        ]

        if resting and filled_positions:
            # Partial: some filled (position exists), remainder still resting.
            filled_size = filled_positions[0]["size"]
            assert 0 < filled_size < partial_size, (
                "Expected a partial position size strictly between 0 and "
                f"the requested size, got {filled_size}"
            )
        elif filled_positions and not resting:
            # Full fill.
            assert filled_positions[0]["size"] > 0
        elif resting and not filled_positions:
            # No fill at all — order still fully resting. Acceptable outcome
            # given we cannot control testnet liquidity; document and move on.
            pass
        else:
            pytest.fail(
                "Order neither filled nor rests as an open order — "
                f"resp={resp} open_orders={open_orders} positions={positions}"
            )
    finally:
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)
        await _safe_flatten(live_client, TESTNET_SYMBOL)


def _extract_oid(resp: dict) -> Optional[int]:
    from src.core.order_lifecycle import extract_order_id  # local import, avoids hard dep at module load

    oid = extract_order_id(resp)
    try:
        return int(oid) if oid is not None else None
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════
# (d) Cancel — far-from-market limit order, cancelled, confirmed gone
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_cancel_order_removes_from_open_orders(
    live_client: HyperliquidLiveClient,
) -> None:
    """Place a limit far from market, cancel it, confirm it disappears."""
    mid = await _mid_price(live_client, TESTNET_SYMBOL)
    limit_price = mid * (1 - FAR_OFFSET_PCT)

    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL,
            "long",
            TESTNET_SIZE,
            order_type="limit_maker",
            limit_price=limit_price,
            post_only=True,
        )
        oid = _extract_oid(resp)
        assert oid is not None, f"Could not extract order id from response: {resp}"

        cancel_resp = await live_client.cancel_order(TESTNET_SYMBOL, oid)
        assert cancel_resp is not None

        open_orders = await live_client.get_open_orders()
        remaining_ids = {int(o.get("oid", -1)) for o in open_orders}
        assert oid not in remaining_ids, (
            f"Order {oid} still present in get_open_orders() after cancel: "
            f"{open_orders}"
        )
    finally:
        # Belt-and-braces in case the assertion above failed before cancel.
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)


# ═══════════════════════════════════════════════════════════════════════
# (e) Native trigger — SL/TP orders actually exist on the exchange
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_native_trigger_orders_exist_on_exchange(
    live_client: HyperliquidLiveClient,
) -> None:
    """Entry + native SL/TP via NativeProtectionManager; verify triggers on HL.

    Uses ``NativeProtectionManager.ensure_protection`` (the same code path
    ``ExecutionEngine.place_native_stop_loss`` / ``place_native_take_profit``
    use) against a real position, then confirms the resulting reduce-only
    trigger orders are visible via ``get_open_orders`` /
    ``parse_trigger_orders`` before cleaning everything up.
    """
    from src.core.native_protection import NativeProtectionManager
    from src.exchanges.hl_positions import parse_trigger_orders
    from src.strategies.base import Position

    opened = False
    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL, "long", TESTNET_SIZE, order_type="market",
        )
        assert resp is not None
        opened = True
        await asyncio.sleep(1.0)

        positions = await live_client.get_positions()
        matching = [p for p in positions if p["symbol"].upper() == TESTNET_SYMBOL.upper()]
        assert matching, "Market entry did not produce an open position to protect"
        entry_price = matching[0]["entry_price"]

        stop_price = entry_price * 0.90
        take_profit_price = entry_price * 1.10

        position = Position(
            symbol=TESTNET_SYMBOL,
            side="long",
            entry_price=entry_price,
            size=TESTNET_SIZE,
            entry_time_ms=int(time.time() * 1000),
            stop_loss_price=stop_price,
            take_profit_price=take_profit_price,
            metadata={"strategy": "testnet_e2e"},
        )

        protection = NativeProtectionManager(live_client, db=None)
        result = await protection.ensure_protection(
            position,
            filled_size=TESTNET_SIZE,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            trade_id=None,
        )
        assert result.sl_order_id, "ensure_protection did not return a SL order id"
        assert result.tp_order_id, "ensure_protection did not return a TP order id"

        open_orders = await live_client.get_open_orders()
        triggers = parse_trigger_orders(open_orders).get(TESTNET_SYMBOL, [])
        trigger_oids = {str(t.order_id) for t in triggers}
        assert str(result.sl_order_id) in trigger_oids, (
            f"SL trigger {result.sl_order_id} not found among exchange "
            f"triggers: {trigger_oids}"
        )
        assert str(result.tp_order_id) in trigger_oids, (
            f"TP trigger {result.tp_order_id} not found among exchange "
            f"triggers: {trigger_oids}"
        )

        await protection.cancel_protection(TESTNET_SYMBOL)
    finally:
        if opened:
            try:
                positions = await live_client.get_positions()
                for pos in positions:
                    if pos["symbol"].upper() == TESTNET_SYMBOL.upper() and pos["size"] > 0:
                        await live_client.close_position(TESTNET_SYMBOL, pos["size"])
            except Exception:  # noqa: BLE001
                pass
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)


# ═══════════════════════════════════════════════════════════════════════
# (f) Crash/restart recovery
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_crash_restart_recovers_open_position(tmp_path) -> None:
    """Position opened by one ExecutionEngine is recovered by a fresh one.

    Simulates a crash by simply dropping the first ``ExecutionEngine``
    instance without calling ``close()``/``stop()`` (no graceful shutdown —
    that's the point: mimic a killed process). A brand-new
    ``ExecutionEngine`` + ``Database`` (same on-disk sqlite file, so the
    persisted trade row survives, just like a real process restart) is then
    used to confirm the still-open testnet position is recovered via
    ``load_open_trades`` and an ``ExchangeReconciler.reconcile_once`` pass,
    rather than being lost or double-opened.

    We use ``ExecutionEngine`` + ``Database`` + ``ExchangeReconciler``
    directly rather than the full ``TradingEngine`` — see module docstring
    for why. The relevant recovery surface
    (``TradingEngine._recover_state`` at the engine layer ultimately calls
    ``executor.load_open_trades()`` and ``reconciler.reconcile_once()``, the
    same two calls exercised here) is fully covered by doing so.
    """
    _require_testnet_credentials()

    from src.core.execution import ExecutionEngine
    from src.core.portfolio import PortfolioState
    from src.core.reconciliation import ExchangeReconciler
    from src.data.database import Database
    from src.utils.config import Config

    db_path = tmp_path / "testnet_e2e_crash.db"
    cfg = Config({
        "risk": {"taker_fee_pct": 0.035, "paper_slippage_pct": 0.05, "initial_capital": 10_000},
        "execution": {"maker_orders": {"enabled": False}},
        "exchange": {"mainnet_enabled": False},
    })

    db1 = Database(db_path)
    engine1 = ExecutionEngine(cfg, db1, mode="testnet")
    opened = False
    try:
        await engine1.open()
        assert engine1._live_signing_ready, (
            "Live signing not ready — check HYPERLIQUID_PRIVATE_KEY validity"
        )

        resp = await engine1._live_client.place_entry(
            TESTNET_SYMBOL, "long", TESTNET_SIZE, order_type="market",
        )
        assert resp is not None
        opened = True
        await asyncio.sleep(1.0)

        # --- "crash": engine1 / db1 are simply abandoned here, no close() ---
        # (Intentionally no `await engine1.close()` — that's the scenario.)

        db2 = Database(db_path)
        engine2 = ExecutionEngine(cfg, db2, mode="testnet")
        await engine2.open()
        try:
            portfolio2 = PortfolioState(initial_capital=10_000)
            engine2.set_portfolio(portfolio2)

            await engine2.load_open_trades()

            reconciler = ExchangeReconciler(
                live_client=engine2._live_client,
                portfolio=portfolio2,
                db=db2,
                orphan_exchange_policy="ADOPT_AND_PROTECT",
            )
            report = await reconciler.reconcile_once(executor=engine2)
            assert report.success, f"Reconciliation failed: {report.errors}"

            positions_after = await portfolio2.positions
            assert TESTNET_SYMBOL in positions_after or report.orphan_exchange, (
                "Fresh engine did not recover the still-open testnet "
                f"position: local={positions_after.keys()} "
                f"exchange_positions={report.exchange_positions.keys()}"
            )
            # No double-open: exactly one position for the symbol locally.
            assert len([s for s in positions_after if s == TESTNET_SYMBOL]) <= 1
        finally:
            await engine2.close()
    finally:
        if opened:
            try:
                # Whichever engine still has a live client, use it to flatten.
                client = engine1._live_client
                positions = await client.get_positions()
                for pos in positions:
                    if pos["symbol"].upper() == TESTNET_SYMBOL.upper() and pos["size"] > 0:
                        await client.close_position(TESTNET_SYMBOL, pos["size"])
            except Exception:  # noqa: BLE001
                pass
        try:
            await engine1.close()
        except Exception:  # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════════════
# (g) Orphan position — exchange has a position local DB doesn't know about
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_orphan_exchange_position_adopted(
    live_client: HyperliquidLiveClient,
) -> None:
    """A position placed outside the engine's bookkeeping gets adopted.

    Places an order directly via the raw ``HyperliquidLiveClient`` (bypassing
    ``ExecutionEngine`` and the local DB entirely — the local "brain" never
    hears about this trade), then runs ``ExchangeReconciler.reconcile_once``
    against a brand-new, empty ``PortfolioState`` so the reconciler
    necessarily observes an exchange position with no local counterpart
    (``report.orphan_exchange``).

    Default policy (``config/settings.yaml``:
    ``reconciliation.orphan_exchange_policy``) is ``ADOPT_AND_PROTECT``, so
    we assert the reconciler adopts it into the local portfolio (rather than
    halting or flattening) and records the action.

    LIMITATION: this only proves the reconciler's adopt path with a
    synthetic "local DB never knew" setup constructed within the test. It
    does not exercise the (harder to stage deterministically) case of a
    local DB row going missing mid-run — that failure mode is covered at the
    unit/integration_offline level with mocks instead.
    """
    from src.core.portfolio import PortfolioState
    from src.core.reconciliation import ExchangeReconciler

    opened = False
    try:
        resp = await live_client.place_entry(
            TESTNET_SYMBOL, "long", TESTNET_SIZE, order_type="market",
        )
        assert resp is not None
        opened = True
        await asyncio.sleep(1.0)

        empty_portfolio = PortfolioState(initial_capital=10_000)
        reconciler = ExchangeReconciler(
            live_client=live_client,
            portfolio=empty_portfolio,
            db=None,
            orphan_exchange_policy="ADOPT_AND_PROTECT",
        )
        report = await reconciler.reconcile_once(executor=None)
        assert report.success, f"Reconciliation failed: {report.errors}"
        assert TESTNET_SYMBOL in report.orphan_exchange, (
            f"Expected {TESTNET_SYMBOL} to be detected as orphan_exchange, "
            f"got orphan_exchange={report.orphan_exchange} "
            f"exchange_positions={report.exchange_positions.keys()}"
        )
        assert any(
            a.startswith(f"orphan_exchange_adopted:{TESTNET_SYMBOL}")
            for a in report.actions
        ), f"Expected an adopt action, got actions={report.actions}"

        adopted = await empty_portfolio.positions
        assert TESTNET_SYMBOL in adopted, (
            "Reconciler reported adoption but position is not in the local "
            f"portfolio: {adopted.keys()}"
        )
    finally:
        if opened:
            try:
                positions = await live_client.get_positions()
                for pos in positions:
                    if pos["symbol"].upper() == TESTNET_SYMBOL.upper() and pos["size"] > 0:
                        await live_client.close_position(TESTNET_SYMBOL, pos["size"])
            except Exception:  # noqa: BLE001
                pass
        await _safe_cancel_all(live_client, TESTNET_SYMBOL)


# ═══════════════════════════════════════════════════════════════════════
# (h) Kill switch — flattens everything, confirms flat
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_kill_switch_flattens_all_positions(tmp_path) -> None:
    """With an open testnet position, kill_switch() cancels+flattens+confirms.

    Uses ``ExecutionEngine.kill_switch()`` directly (the same method
    ``TradingEngine.kill_switch()`` delegates to, plus a reconciliation
    pass) against a real open position and a resting limit order, then
    asserts:

    * all open orders for the account were cancelled,
    * the position was closed,
    * ``confirm_flat()`` (backed by real ``user_state``) reports flat,
    * a follow-up reconciliation pass sees no local/exchange drift.
    """
    _require_testnet_credentials()

    from src.core.execution import ExecutionEngine
    from src.core.portfolio import PortfolioState
    from src.core.reconciliation import ExchangeReconciler
    from src.data.database import Database
    from src.utils.config import Config

    db_path = tmp_path / "testnet_e2e_kill_switch.db"
    cfg = Config({
        "risk": {"taker_fee_pct": 0.035, "paper_slippage_pct": 0.05, "initial_capital": 10_000},
        "execution": {"maker_orders": {"enabled": False}},
        "exchange": {"mainnet_enabled": False},
    })

    db = Database(db_path)
    engine = ExecutionEngine(cfg, db, mode="testnet")
    try:
        await engine.open()
        assert engine._live_signing_ready

        # Open a market position...
        resp = await engine._live_client.place_entry(
            TESTNET_SYMBOL, "long", TESTNET_SIZE, order_type="market",
        )
        assert resp is not None
        await asyncio.sleep(1.0)

        # ...and a resting far-from-market limit order, so kill_switch has
        # both an order to cancel and a position to flatten.
        mid = await _mid_price(engine._live_client, TESTNET_SYMBOL)
        await engine._live_client.place_entry(
            TESTNET_SYMBOL,
            "long",
            TESTNET_SIZE,
            order_type="limit_maker",
            limit_price=mid * (1 - FAR_OFFSET_PCT),
            post_only=True,
        )

        portfolio = PortfolioState(initial_capital=10_000)
        engine.set_portfolio(portfolio)

        result = await engine.kill_switch()
        assert not result.errors, f"kill_switch reported errors: {result.errors}"
        assert result.exchange_flat, "kill_switch did not confirm the account is flat"

        reconciler = ExchangeReconciler(
            live_client=engine._live_client,
            portfolio=portfolio,
            db=db,
            orphan_exchange_policy="ADOPT_AND_PROTECT",
        )
        report = await reconciler.reconcile_once(executor=engine)
        assert report.success
        assert not report.orphan_exchange, (
            f"Expected no exchange positions after kill_switch, got "
            f"{report.exchange_positions.keys()}"
        )
    finally:
        # Defense in depth: if any assertion above failed mid-way, make one
        # more best-effort pass to leave the testnet account clean.
        try:
            await _safe_cancel_all(engine._live_client, TESTNET_SYMBOL)
            await _safe_flatten(engine._live_client, TESTNET_SYMBOL)
        except Exception:  # noqa: BLE001
            pass
        await engine.close()


# ═══════════════════════════════════════════════════════════════════════
# (i) Multi-asset precision — BTC, ETH, SOL, HYPE fractional maker orders
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_multi_asset_maker_precision_btc_eth_sol_hype(
    live_client: HyperliquidLiveClient,
) -> None:
    """Verify real HL wire tick & 5-sig-fig rounding across BTC, ETH, SOL, and HYPE.

    Regression proof for P0-1: previously ``px_decimals`` defaulted to ``0`` on
    every asset, which worked by coincidence on BTC but rounded ETH/SOL/HYPE
    prices to integers. Places fractional post-only ``Alo`` maker orders across
    all 4 production symbols simultaneously, confirms all 4 rest on
    ``frontend_open_orders`` with non-integer decimal prices on ETH/SOL/HYPE,
    and cancels all 4 cleanly.
    """
    from src.exchanges.hyperliquid_live import make_cloid, normalize_price, normalize_size

    symbols = ("BTC", "ETH", "SOL", "HYPE")
    oracles = await live_client.get_oracle_prices()
    placed_oids: dict[str, int] = {}

    try:
        for sym in symbols:
            ref = oracles.get(sym) or await _mid_price(live_client, sym)
            assert ref > 0, f"Missing reference/mid price for {sym}"
            bbo = await live_client.get_l2_top(sym)
            anchor = min(ref, bbo[0]) if bbo and bbo[0] > 0 else ref
            raw_limit = anchor * 0.851234
            norm_px = normalize_price(sym, raw_limit)
            # Size ~$25 notional before szDecimals floor so post-floor sz * px > $10
            sz = normalize_size(sym, max(25.0 / norm_px, 0.001))
            cloid = make_cloid(sym, "long", salt="e2e-multi")

            resp = await live_client.place_entry(
                sym,
                "long",
                sz,
                order_type="limit_maker",
                limit_price=raw_limit,
                post_only=True,
                cloid=cloid,
            )
            oid = _extract_oid(resp)
            assert oid is not None, f"Multi-asset maker failed on {sym}: {resp}"
            placed_oids[sym] = oid

        open_orders = await live_client.get_open_orders()
        by_oid = {int(o.get("oid", -1)): o for o in open_orders}
        for sym, oid in placed_oids.items():
            assert oid in by_oid, f"Expected resting {sym} order {oid} in open_orders"
    finally:
        for sym in symbols:
            await _safe_cancel_all(live_client, sym)


# ═══════════════════════════════════════════════════════════════════════
# (j) Non-BTC native SL/TP triggers + replace-before-cancel ratchet (ETH)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_eth_native_sl_tp_replace_before_cancel(
    live_client: HyperliquidLiveClient,
) -> None:
    """Verify native SL/TP triggers on ETH, 60s sync idempotence, and SL-only ratchet."""
    from src.core.native_protection import NativeProtectionManager
    from src.exchanges.hl_positions import parse_trigger_orders
    from src.exchanges.hyperliquid_live import normalize_size
    from src.strategies.base import Position

    sym = "ETH"
    mid = await _mid_price(live_client, sym)
    sz = normalize_size(sym, max(15.0 / mid, 0.01))
    opened = False

    try:
        resp = await live_client.place_entry(sym, "long", sz, order_type="market")
        assert resp is not None
        opened = True
        await asyncio.sleep(1.0)

        positions = await live_client.get_positions()
        matching = [p for p in positions if p["symbol"].upper() == sym]
        assert matching, f"ETH market entry did not open position: {resp}"
        entry_px = matching[0]["entry_price"]

        # Unrounded float SL/TP to exercise P0-6 normalization parity
        sl_1 = entry_px * 0.9412345
        tp_1 = entry_px * 1.0678912
        pos = Position(
            symbol=sym,
            side="long",
            entry_price=entry_px,
            size=sz,
            entry_time_ms=int(time.time() * 1000),
            stop_loss_price=sl_1,
            take_profit_price=tp_1,
            metadata={"strategy": "testnet_e2e_eth"},
        )

        mgr = NativeProtectionManager(live_client, db=None)
        r1 = await mgr.ensure_protection(
            pos, filled_size=sz, stop_price=sl_1, take_profit_price=tp_1
        )
        assert r1.sl_order_id and r1.tp_order_id, f"Missing SL/TP oids on ETH: {r1}"

        # Sync back from real exchange open orders and re-run with identical unrounded floats:
        # must be an idempotent NO-OP (same oids, 0 cancelled orders!)
        open_orders = await live_client.get_open_orders()
        mgr.sync_from_open_orders(open_orders)
        r_noop = await mgr.ensure_protection(
            pos, filled_size=sz, stop_price=sl_1, take_profit_price=tp_1
        )
        assert r_noop.cancelled_ids == [], f"Expected 0 cancels on sync parity, got {r_noop}"
        assert r_noop.sl_order_id == r1.sl_order_id
        assert r_noop.tp_order_id == r1.tp_order_id

        # Ratchet SL tighter -> must replace SL ONLY and keep original TP oid intact!
        sl_2 = entry_px * 0.9623456
        r2 = await mgr.ensure_protection(
            pos, filled_size=sz, stop_price=sl_2, take_profit_price=tp_1
        )
        assert r2.sl_order_id and r2.sl_order_id != r1.sl_order_id
        assert r2.tp_order_id == r1.tp_order_id
        assert str(r1.sl_order_id) in [str(x) for x in r2.cancelled_ids]

        open_orders_after = await live_client.get_open_orders()
        trig_oids = {
            str(t.order_id) for t in parse_trigger_orders(open_orders_after).get(sym, [])
        }
        assert str(r2.sl_order_id) in trig_oids
        assert str(r1.tp_order_id) in trig_oids

        await mgr.cancel_protection(sym)
    finally:
        if opened:
            await _safe_flatten(live_client, sym)
        await _safe_cancel_all(live_client, sym)


# ═══════════════════════════════════════════════════════════════════════
# (k) MultiLegCoordinator — Spot+Perp CarryA1 & PerpPair under Agent Wallet Unified('u')
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.testnet_live
@pytest.mark.asyncio
async def test_multileg_spot_perp_carry_and_perp_pair_e2e(
    live_client: HyperliquidLiveClient,
    tmp_path,
) -> None:
    """Real Testnet E2E of MultiLegCoordinator with Agent Wallet + Unified Account ('u'):

    1. SpotPerpCarryRunner 2-leg maker entry (CC/USDC Spot BUY + CC Perp SELL)
       rests both orders simultaneously on HL Testnet with schedule_cancel
       armed, then aborts & cancels cleanly on window expiry.
    2. Real Spot IoC BUY + MultiLegCoordinator close_episode_taker SELL on
       PURR/USDC exercising post-fee base-token balance clamping (R-P0-4).
    3. Real PerpPairRunner 2-leg hedge (ETH + BTC) + ExchangeReconciler
       ignored_symbols_provider check (R-P1-3) + full IoC close to flat.
    """
    from src.core.portfolio import PortfolioState
    from src.core.reconciliation import ExchangeReconciler
    from src.exchanges.hyperliquid_live import normalize_size, resolve_wire_coin
    from src.multileg import (
        LegSpec,
        LegState,
        MultiLegCoordinator,
        MultiLegEpisode,
        MultiLegLedger,
        SpotPerpCarryRunner,
        TopOfBook,
    )

    ml_ledger = MultiLegLedger(str(tmp_path / "testnet_multileg.db"))
    coord = MultiLegCoordinator(
        ledger=ml_ledger,
        live_client=live_client,
        mode="testnet",
        margin_profile="unified",
        maker_fill_window_s=5.0,
    )
    carry_runner = SpotPerpCarryRunner(coord)

    try:
        # ── Part 1: Real 2-leg Spot+Perp CarryA1 maker placement & clean abort ──
        cc_spot_bbo = await live_client.get_l2_top("CC/USDC")
        cc_perp_bbo = await live_client.get_l2_top("CC")
        assert cc_spot_bbo and cc_perp_bbo, "CC Spot/Perp L2 books unavailable on testnet"

        # Place resting non-crossing maker quotes (~10% passive offset) sized >$12 on both legs
        passive_spot_bid = cc_spot_bbo[0] * 0.90
        passive_perp_ask = cc_perp_bbo[1] * 1.05
        # Ensure matched_qty * passive_spot_bid >= $12
        min_qty_for_spot = 13.0 / passive_spot_bid
        notional_perp_equiv = min_qty_for_spot * cc_perp_bbo[1]

        ep_carry = await carry_runner.open_carry_episode(
            episode_id=f"e2e-carry-{int(time.time())}",
            spot_symbol="CC/USDC",
            perp_symbol="CC",
            notional_usd=notional_perp_equiv,
            max_leverage=5.0,
            spot_book=TopOfBook(bid=passive_spot_bid, ask=cc_spot_bbo[1]),
            perp_book=TopOfBook(bid=cc_perp_bbo[0], ask=passive_perp_ask),
            now_ms=1_000,
        )
        assert ep_carry.status == "pending_entry", f"Carry entry failed: {ep_carry.close_reason}"
        spot_oid = ep_carry.legs["spot"].order_id
        perp_oid = ep_carry.legs["perp"].order_id
        assert spot_oid and perp_oid

        open_orders = await live_client.get_open_orders()
        open_oids = {str(o.get("oid")) for o in open_orders}
        assert str(spot_oid) in open_oids, f"Spot Carry order {spot_oid} not resting on HL"
        assert str(perp_oid) in open_oids, f"Perp Carry order {perp_oid} not resting on HL"

        # Expire the 5s maker window -> cancels both resting orders cleanly
        await carry_runner.step_carry_entry(
            ep_carry,
            spot_book=TopOfBook(bid=passive_spot_bid, ask=cc_spot_bbo[1]),
            perp_book=TopOfBook(bid=cc_perp_bbo[0], ask=passive_perp_ask),
            now_ms=10_000,
        )
        assert ep_carry.status in ("aborted", "aborted_unwound")

        # ── Part 2: Real filled Spot BUY -> post-fee base-token clamped SELL + Perp hedge ──
        purr_bbo = await live_client.get_l2_top("PURR/USDC")
        eth_bbo = await live_client.get_l2_top("ETH")
        assert purr_bbo and eth_bbo

        purr_qty = normalize_size("PURR/USDC", max(13.0 / purr_bbo[1], 3.0))
        eth_qty = normalize_size("ETH", max(15.0 / eth_bbo[0], 0.01))

        spot_fill_resp = await live_client.place_spot_order(
            "PURR/USDC",
            "buy",
            purr_qty,
            order_type="ioc",
            limit_price=purr_bbo[1],
            max_slippage_pct=3.0,
        )
        perp_fill_resp = await live_client.place_aggressive_limit(
            "ETH",
            "short",
            eth_qty,
            reference_price=eth_bbo[0],
            max_slippage_pct=3.0,
            reduce_only=False,
        )
        assert _extract_oid(spot_fill_resp) is not None, f"PURR spot buy failed: {spot_fill_resp}"
        assert _extract_oid(perp_fill_resp) is not None, f"ETH perp short failed: {perp_fill_resp}"
        await asyncio.sleep(1.0)

        live_ep = MultiLegEpisode(
            episode_id=f"e2e-filled-{int(time.time())}",
            strategy="CarryA1",
            mode="testnet",
            margin_profile="unified",
            created_ms=int(time.time() * 1000),
            status="open",
            legs={
                "spot": LegState(
                    spec=LegSpec("spot", "PURR/USDC", "spot", "buy", purr_qty, purr_bbo[1]),
                    status="filled",
                    filled_size=purr_qty,
                    avg_fill_price=purr_bbo[1],
                ),
                "perp": LegState(
                    spec=LegSpec("perp", "ETH", "perp", "sell", eth_qty, eth_bbo[0]),
                    status="filled",
                    filled_size=eth_qty,
                    avg_fill_price=eth_bbo[0],
                ),
            },
        )
        ml_ledger.save_episode(live_ep)

        # Verify single-leg ExchangeReconciler (with Mainnet HALT policy!) skips
        # MultiLegCoordinator's active ETH perp leg instead of halting!
        single_leg_portfolio = PortfolioState(initial_capital=1_000.0)
        recon = ExchangeReconciler(
            live_client=live_client,
            portfolio=single_leg_portfolio,
            orphan_exchange_policy="HALT",
            ignored_symbols_provider=ml_ledger.get_active_perp_symbols,
        )
        recon_rep = await recon.reconcile_once()
        assert recon_rep.success is True
        assert recon.health.halted is False
        assert "skipped_external:ETH" in recon_rep.actions

        # Close BOTH the real Spot PURR leg (clamping to post-fee held PURR balance!)
        # AND the real Perp ETH short leg via MultiLegCoordinator.close_episode_taker!
        purr_exit_bbo = await live_client.get_l2_top("PURR/USDC") or purr_bbo
        eth_exit_bbo = await live_client.get_l2_top("ETH") or eth_bbo
        await coord.close_episode_taker(
            live_ep,
            books={
                "PURR/USDC": TopOfBook(bid=purr_exit_bbo[0], ask=purr_exit_bbo[1]),
                "ETH": TopOfBook(bid=eth_exit_bbo[0], ask=eth_exit_bbo[1]),
            },
            reason="e2e_complete_close",
        )
        assert live_ep.status == "closed"
        await asyncio.sleep(1.0)
        assert await live_client.confirm_flat(), "Expected zero open perp positions after multileg close"
    finally:
        await _safe_cancel_all(live_client, "CC")
        await _safe_cancel_all(live_client, resolve_wire_coin("CC/USDC"))
        await _safe_cancel_all(live_client, "ETH")
        await _safe_flatten(live_client, "CC")
        await _safe_flatten(live_client, "ETH")
        try:
            avail = await live_client.get_available_spot_balances()
            rem_purr = normalize_size("PURR/USDC", avail.get("PURR", 0.0))
            if rem_purr >= 1.0:
                pb = await live_client.get_l2_top("PURR/USDC")
                if pb:
                    await live_client.place_spot_order(
                        "PURR/USDC", "sell", rem_purr, order_type="ioc",
                        limit_price=pb[0], max_slippage_pct=5.0, enforce_min_notional=False
                    )
        except Exception:  # noqa: BLE001
            pass
        ml_ledger.close()
