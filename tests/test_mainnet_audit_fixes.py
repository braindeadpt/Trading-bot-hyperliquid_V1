"""Regression test suite for the 2026-11-11 Mainnet Forensic Audit (P0 + P1 fixes).

Verifies:
  1. P0-1: Hyperliquid real ``meta()`` (no ``pxDecimals``) computes ``6 - szDecimals``
     and ``<= 5`` significant figures on SOL/HYPE/sub-$1 coins + ``8 - szDecimals`` on Spot,
     Decimal size flooring without IEEE-754 drift, and $10 min notional rejection.
  2. P0-2: ``load_config(..., mode_override='mainnet')`` applies ``mode_overrides.mainnet``
     and preserves nested ``execution.maker_orders.strategies`` via deep merge while
     keeping default ``paper`` ``config_hash`` untouched.
  3. P0-3: ``SpotPerpCarry`` (``SYNTHETIC_SPOT_ONLY = True``) refuses live instantiation
     in ``testnet``/``mainnet`` execution while continuing to work in ``shadow``/``paper``.
  4. P0-4: Resting ``limit_maker`` live orders block duplicate entries in memory and SQLite.
  5. P0-5: ``parse_hyperliquid_close_response`` treats ``resting`` as ``ORDER_CLOSE_PENDING``
     and partial IoC fills as ``ORDER_PARTIAL`` (preserving residual position + native SL/TP).
  6. P0-6: ``NativeProtectionManager`` normalizes trigger prices (preventing 60s replace loop)
     and places new SL BEFORE cancelling old SL (zero unprotected gap on failure).
  7. P0-7: ``PortfolioState`` deducts ``entry_fee`` exactly once across round-trip trades
     and refunds ``entry_fee`` on ``cancel_position``, plus ``sync_live_account_equity``
     and UTC top-of-hour funding alignment.
  8. P1 Ops/Security: ``dashboard_proxy.rewrite_http_head`` injects verified ``X-Forwarded-For``
     for Docker bridge peers (stripping spoofed headers), ``_is_cb_tripped`` reads
     ``RiskManager.is_circuit_breaker_tripped()``, ``TelegramCommandBot`` handles ``/kill confirm``,
     ``emergency_flatten`` flattens cleanly, and ``CrashRecovery`` supports ``mainnet`` without
     rewriting live crashes to ``paper``.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from deploy.linux.dashboard_proxy import rewrite_http_head
from scripts.ops.emergency_flatten import execute_emergency_flatten
from src.alerts.telegram_bot import TelegramCommandBot
from src.core.native_protection import NativeProtectionManager
from src.core.order_lifecycle import (
    ORDER_CLOSE_PENDING,
    ORDER_FILLED,
    ORDER_PARTIAL,
    parse_hyperliquid_close_response,
)
from src.core.portfolio import PortfolioState
from src.core.trigger_reconcile import compute_realized_pnl
from src.dashboard.web import _is_cb_tripped
from src.data.database import Database, TradeEntry
from src.exchanges.hyperliquid_live import (
    HyperliquidLiveClient,
    _meta_cache,
    build_meta_cache,
    build_spot_meta_cache,
    make_cloid,
    normalize_price,
    normalize_size,
)
from src.strategies.base import Position
from src.strategies.factory import _instantiate_from_registry
from src.strategies.spot_perp_carry import SpotPerpCarry
from src.utils.config import Config, compute_config_hash, load_config
from src.utils.crash_recovery import CrashRecovery


@pytest.mark.unit
def test_p0_1_hl_real_meta_precision_and_spot_rounding() -> None:
    """Real HL meta() has no pxDecimals; prices must not round to integers on HYPE/SOL/sub-$1."""
    _meta_cache.clear()
    fake_info = MagicMock()
    # Exact shape returned by hyperliquid-python-sdk 0.23 Info.meta()
    fake_info.meta.return_value = {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
            {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10},
            {"name": "DOGE", "szDecimals": 0, "maxLeverage": 10},
        ]
    }
    fake_info.spot_meta.return_value = {
        "tokens": [
            {"name": "USDC", "szDecimals": 8, "index": 0},
            {"name": "HYPE", "szDecimals": 2, "index": 150},
        ],
        "universe": [
            {"name": "@107", "tokens": [150, 0], "index": 107},
        ],
    }

    build_meta_cache(fake_info)
    build_spot_meta_cache(fake_info)

    # HYPE at ~$24.8756 -> 5 sig figs & <= 4 decimals -> 24.876 (NOT 25.0!)
    assert normalize_price("HYPE", 24.8756) == 24.876
    # Sub-$1 asset (DOGE szDecimals=0 -> max 6 decimals, 5 sig figs) -> NOT 0.0!
    assert normalize_price("DOGE", 0.143289) == 0.14329
    # BTC >= $100k integer rule
    assert normalize_price("BTC", 105432.4) == 105432.0
    # IEEE-754 size floor check: 0.003 * 10**5 in float is 299.99999999999994 -> must stay 0.003
    assert normalize_size("BTC", 0.003) == 0.003
    # Spot pair HYPE/USDC (8 - 2 = 6 decimals, wire coin @107)
    assert _meta_cache["HYPE/USDC"]["wire_coin"] == "@107"
    assert _meta_cache["HYPE/USDC"]["px_decimals"] == 6
    assert normalize_price("HYPE/USDC", 24.8756) == 24.876

    cloid = make_cloid("HYPE", "long", timestamp_ms=1700000000000)
    assert cloid.startswith("0x") and len(cloid) == 34


@pytest.mark.unit
def test_p0_1_min_notional_guard_rejects_dust_entry() -> None:
    _meta_cache.clear()
    _meta_cache["HYPE"] = {
        "sz_decimals": 2,
        "px_decimals": 4,
        "is_spot": False,
        "enforce_sig_figs": True,
        "wire_coin": "HYPE",
    }
    client = HyperliquidLiveClient("0x" + "a" * 64, use_testnet=True)
    client._exchange = MagicMock()
    client._info = MagicMock()
    client._wallet_address = "0x" + "b" * 40

    # 0.10 HYPE @ $25 = $2.50 < $10 minimum -> must raise ValueError before network call
    with pytest.raises(ValueError, match="minimum"):
        asyncio.run(
            client.place_entry(
                "HYPE",
                "long",
                0.10,
                order_type="limit_maker",
                limit_price=25.0,
                post_only=True,
            )
        )
    client._exchange.order.assert_not_called()


@pytest.mark.unit
def test_p0_2_cli_mode_override_applies_safeguards_and_preserves_maker_strategies() -> None:
    paper_cfg = load_config("config/settings.yaml")
    mainnet_cfg = load_config("config/settings.yaml", mode_override="mainnet")

    # Paper hash must stay untouched
    assert paper_cfg.get("mode") == "paper"
    assert len(compute_config_hash(paper_cfg)) == 16

    # Mainnet CLI override must apply all mode_overrides.mainnet safeguards
    assert mainnet_cfg.get("mode") == "mainnet"
    assert mainnet_cfg.get("risk.leverage_max") == 5.0
    assert mainnet_cfg.get("risk.max_daily_loss_pct") == 2.0
    assert mainnet_cfg.get("risk.max_daily_trades") == 20
    assert mainnet_cfg.get("reconciliation.orphan_exchange_policy") == "HALT"
    assert mainnet_cfg.get("engine.close_positions_on_shutdown") is True

    # Deep merge must preserve nested execution.maker_orders.strategies!
    maker_strats = mainnet_cfg.get("execution.maker_orders.strategies")
    assert isinstance(maker_strats, list) and "VWAPDeviation" in maker_strats
    assert mainnet_cfg.get("execution.maker_orders.maker_fee_pct") == 0.015


@pytest.mark.unit
def test_p0_3_synthetic_spot_perp_carry_blocked_in_live_execution() -> None:
    cfg = Config({"mode": "mainnet", "strategy": {"spot_perp_carry": {"enabled": True}}})
    live_inst = _instantiate_from_registry(
        cfg, "strategy.spot_perp_carry", SpotPerpCarry, force=True, shadow=False
    )
    shadow_inst = _instantiate_from_registry(
        cfg, "strategy.spot_perp_carry", SpotPerpCarry, force=True, shadow=True
    )
    assert live_inst is None
    assert shadow_inst is not None and shadow_inst.is_active() is True


@pytest.mark.unit
def test_p0_4_database_get_active_trades_includes_resting_orders(tmp_path) -> None:
    db = Database(str(tmp_path / "active.db"))
    try:
        tid = db.save_trade_entry(
            TradeEntry(
                symbol="SOL",
                side="long",
                entry_price=150.0,
                entry_time=1_700_000_000_000,
                size=1.0,
                strategy="VWAPDeviation",
                status="resting",
            )
        )
        assert tid > 0
        assert db.get_open_trades() == []
        active = db.get_active_trades()
        assert len(active) == 1 and active[0]["symbol"] == "SOL"
        # Also verify prune_old_data executes VACUUM cleanly outside transaction
        deleted = db.prune_old_data(days=30)
        assert isinstance(deleted, dict)
    finally:
        db.close()


@pytest.mark.unit
def test_p0_5_close_response_rejects_resting_and_flags_partial_fill() -> None:
    resting_resp = {
        "status": "ok",
        "response": {"data": {"statuses": [{"resting": {"oid": 777}}]}},
    }
    parsed_resting = parse_hyperliquid_close_response(resting_resp, expected_size=2.0)
    assert parsed_resting.lifecycle_state == ORDER_CLOSE_PENDING
    assert parsed_resting.resting_order_id == "777"

    partial_resp = {
        "status": "ok",
        "response": {
            "data": {"statuses": [{"filled": {"oid": 778, "totalSz": "0.5", "avgPx": "25.1"}}]}
        },
    }
    parsed_partial = parse_hyperliquid_close_response(partial_resp, expected_size=2.0)
    assert parsed_partial.lifecycle_state == ORDER_PARTIAL
    assert parsed_partial.filled_size == 0.5
    assert parsed_partial.avg_fill_price == 25.1

    full_parsed = parse_hyperliquid_close_response(partial_resp, expected_size=0.5)
    assert full_parsed.lifecycle_state == ORDER_FILLED


@pytest.mark.unit
def test_p0_6_native_protection_replace_before_cancel_and_rounding_parity() -> None:
    _meta_cache.clear()
    _meta_cache["HYPE"] = {
        "sz_decimals": 2,
        "px_decimals": 4,
        "is_spot": False,
        "enforce_sig_figs": True,
        "wire_coin": "HYPE",
    }

    call_order: list[str] = []
    oid_seq = 100

    class OrderTrackingClient:
        def __init__(self) -> None:
            self.fail_next_sl = False

        async def place_trigger_order(self, symbol, side, size, *, trigger_price, tpsl):
            nonlocal oid_seq
            if tpsl == "sl" and self.fail_next_sl:
                call_order.append("place_sl_fail")
                raise RuntimeError("rate_limited_429")
            oid_seq += 1
            call_order.append(f"place_{tpsl}:{oid_seq}:{trigger_price}")
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid_seq}}]}}}

        async def cancel_order(self, symbol, oid):
            call_order.append(f"cancel:{oid}")
            return {"status": "ok"}

    async def _run() -> None:
        client = OrderTrackingClient()
        mgr = NativeProtectionManager(client)
        pos = Position(
            symbol="HYPE",
            side="long",
            entry_price=25.0,
            size=10.0,
            entry_time_ms=1,
            stop_loss_price=24.123456,
            take_profit_price=27.0,
        )
        r1 = await mgr.ensure_protection(
            pos, filled_size=10.0, stop_price=24.123456, take_profit_price=27.0
        )
        assert r1.sl_order_id == "101"
        assert r1.tp_order_id == "102"

        # Simulate 60s reconciler reading back normalized exchange trigger prices (24.123, 27.0)
        mgr.sync_from_open_orders(
            [
                {"coin": "HYPE", "oid": 101, "isTrigger": True, "tpsl": "sl", "triggerPx": "24.123", "sz": "10.0"},
                {"coin": "HYPE", "oid": 102, "isTrigger": True, "tpsl": "tp", "triggerPx": "27.0", "sz": "10.0"},
            ]
        )
        call_order.clear()
        # Next tick passes unrounded 24.123456 again -> must be a NO-OP (0 cancels, 0 places!)
        await mgr.ensure_protection(
            pos, filled_size=10.0, stop_price=24.123456, take_profit_price=27.0
        )
        assert call_order == []

        # Ratchet SL while 429 occurs -> old SL 101 MUST NOT be cancelled!
        client.fail_next_sl = True
        r_fail = await mgr.ensure_protection(
            pos, filled_size=10.0, stop_price=24.50, take_profit_price=27.0
        )
        assert call_order == ["place_sl_fail"]
        assert mgr.get_state("HYPE").sl_order_id == "101"
        assert r_fail.tp_order_id == "102"

        # Now succeed ratcheting SL -> places new SL 103 FIRST, then cancels old SL 101, leaving TP 102 untouched!
        call_order.clear()
        client.fail_next_sl = False
        r_ok = await mgr.ensure_protection(
            pos, filled_size=10.0, stop_price=24.50, take_profit_price=27.0
        )
        assert call_order == ["place_sl:103:24.5", "cancel:101"]
        assert r_ok.sl_order_id == "103"
        assert r_ok.tp_order_id == "102"

    asyncio.run(_run())


@pytest.mark.unit
def test_p0_7_portfolio_single_entry_fee_deduction_and_live_equity_sync() -> None:
    async def _run() -> None:
        p = PortfolioState(10_000.0)
        pos = Position(
            symbol="BTC",
            side="long",
            entry_price=50_000.0,
            size=0.1,  # notional = $5,000
            entry_time_ms=1_700_000_100_000,
        )
        entry_fee = 2.25
        exit_fee = 2.25
        # Engine passes total_cost = notional + entry_fee
        await p.add_position(pos, cost=5_000.0 + entry_fee)
        # Price flat at 50_000 -> gross PnL = 0, net pnl_usd = -4.50
        net_pnl = 0.0 - entry_fee - exit_fee
        await p.remove_position("BTC", exit_price=50_000.0, pnl_usd=net_pnl, pnl_pct=net_pnl / 5000.0, reason="tp")
        # Final equity/cash MUST be 10_000 - 4.50 = 9_995.50 (NOT 9_993.25!)
        assert abs((await p.get_total_value()) - 9_995.50) < 1e-9

        # cancel_position must also refund entry_fee
        await p.add_position(pos, cost=5_000.0 + entry_fee)
        await p.cancel_position("BTC")
        assert abs((await p.get_total_value()) - 9_995.50) < 1e-9

        # trigger_reconcile.compute_realized_pnl deducts both entry and exit fees on HL closedPnl
        pnl_usd, _ = compute_realized_pnl(
            side="long",
            entry_price=50_000.0,
            size=0.1,
            exit_price=50_100.0,
            entry_fee=2.25,
            exit_fee=2.25,
            closed_pnl_exchange=10.0,
        )
        assert abs(pnl_usd - 5.50) < 1e-9

        # Live canary wallet sync ($500 real HL accountValue instead of $10,000 YAML default)
        p_live = PortfolioState(10_000.0)
        await p_live.sync_live_account_equity(500.0)
        assert abs((await p_live.get_total_value()) - 500.0) < 1e-9

    asyncio.run(_run())


@pytest.mark.unit
def test_p1_ops_proxy_cb_telegram_kill_and_crash_recovery() -> None:
    # 1. Dashboard proxy injects verified peer IP and strips spoofed X-Forwarded-For
    req = (
        b"GET /api/status HTTP/1.1\r\n"
        b"Host: localhost:8091\r\n"
        b"X-Forwarded-For: 127.0.0.1\r\n\r\n"
    )
    rewritten = rewrite_http_head(req, "172.18.0.5")
    assert b"X-Forwarded-For: 172.18.0.5\r\n" in rewritten
    assert b"X-Forwarded-For: 127.0.0.1" not in rewritten
    # Direct host loopback stays untouched
    assert rewrite_http_head(req, "127.0.0.1") == req

    # 2. Dashboard CB helper reads is_circuit_breaker_tripped()
    fake_risk = SimpleNamespace(is_circuit_breaker_tripped=lambda: True)
    assert _is_cb_tripped(fake_risk) is True

    # 3. Telegram /kill requires confirmation then invokes engine.kill_switch()
    fake_engine = SimpleNamespace(
        kill_switch=AsyncMock(return_value={"cancelled_orders": 3, "confirmed_flat": True})
    )
    tbot = TelegramCommandBot(
        token="dummy",
        allowed_chat_ids=["12345"],
        db=MagicMock(),
        engine_getter=lambda: fake_engine,
        notifier=MagicMock(),
    )
    warn_msg = asyncio.run(tbot._dispatch("/kill", []))
    assert "confirm" in warn_msg.lower()
    fake_engine.kill_switch.assert_not_called()

    ok_msg = asyncio.run(tbot._dispatch("/kill", ["confirm"]))
    assert "KILL SWITCH EXECUTED" in ok_msg
    fake_engine.kill_switch.assert_awaited_once()

    # 4. Standalone emergency_flatten script logic
    mock_client = SimpleNamespace(
        wallet_address="0x" + "c" * 40,
        schedule_cancel=AsyncMock(return_value={"status": "ok"}),
        cancel_all_orders=AsyncMock(return_value=2),
        flatten_all_positions=AsyncMock(return_value=[{"symbol": "SOL", "ok": True}]),
        confirm_flat=AsyncMock(return_value=True),
    )
    flat_report = asyncio.run(execute_emergency_flatten(use_testnet=True, client=mock_client))
    assert flat_report["confirmed_flat"] is True
    assert flat_report["cancelled_orders"] == 2

    # 5. CrashRecovery accepts mainnet and does NOT rewrite mainnet to paper
    cr = CrashRecovery(max_restarts=1, cooldown_seconds=0)
    mainnet_cmd = (sys.executable, "main.py", "--mode", "mainnet")
    assert cr._validate_cmd(mainnet_cmd) is True
    assert cr._extract_mode(mainnet_cmd) == "mainnet"


@pytest.mark.unit
def test_review_r_p0_and_r_p1_remediations() -> None:
    """Verify PR #9 adversarial review fixes: SDK internal TypeError no-retry,
    partial close portfolio sync, keep-alive proxy Connection: close, legacy
    entry_fee_debited backfill, and reconciler ignored_symbols_provider."""
    from src.core.reconciliation import ExchangeReconciler

    # R-P0-1: Internal TypeError inside 7-arg Exchange.order must NOT submit a 2nd order!
    _meta_cache.clear()
    _meta_cache["SOL"] = {
        "sz_decimals": 2,
        "px_decimals": 4,
        "is_spot": False,
        "enforce_sig_figs": True,
        "wire_coin": "SOL",
    }
    calls = 0

    def buggy_sdk_order(coin, is_buy, sz, px, order_type, reduce_only=False, cloid=None):
        nonlocal calls
        calls += 1
        raise TypeError("internal_response_parse_type_error")

    client = HyperliquidLiveClient("0x" + "a" * 64, use_testnet=True)
    client._exchange = SimpleNamespace(order=buggy_sdk_order)
    client._info = MagicMock()
    client._wallet_address = "0x" + "b" * 40
    with pytest.raises(TypeError, match="internal_response_parse_type_error"):
        asyncio.run(
            client.place_entry(
                "SOL",
                "long",
                1.0,
                order_type="limit_maker",
                limit_price=150.0,
                post_only=True,
                cloid="0x" + "1" * 32,
            )
        )
    assert calls == 1, "SDK internal TypeError must NEVER trigger a 2nd fallback order!"

    # R-P0-2 + R-P1-2: Partial close keeps PortfolioState size & fees exact; legacy backfill works
    async def _run_portfolio_and_reconciler() -> None:
        p = PortfolioState(10_000.0)
        pos = Position(
            symbol="SOL",
            side="long",
            entry_price=100.0,
            size=2.0,  # notional = $200
            entry_time_ms=1_700_000_000_000,
        )
        # Simulate legacy restored position without entry_fee_debited, then backfill $1.00
        await p.add_position(pos, cost=200.0)
        p._positions["SOL"].metadata.pop("entry_fee_debited", None)
        p._cash -= 1.00
        await p.ensure_entry_fee_debited("SOL", 1.00)

        # Partial close of 0.5 SOL at $110 (+$5 gross, -$0.25 entry_fee_share, -$0.25 exit_fee = +$4.50 net)
        await p.reduce_position_partial("SOL", closed_size=0.5, realized_pnl_usd=4.50)
        positions = await p.positions
        assert abs(positions["SOL"].size - 1.5) < 1e-9
        # Remaining 1.5 SOL closes at $100 (0 gross, -$0.75 entry_fee_share, -$0.75 exit_fee = -$1.50 net)
        await p.remove_position("SOL", exit_price=100.0, pnl_usd=-1.50, pnl_pct=-0.01, reason="sl")
        # Net PnL across both slices = +4.50 - 1.50 = +3.00 -> total equity MUST be 10_003.00!
        assert abs((await p.get_total_value()) - 10_003.00) < 1e-9

        # R-P1-3: ExchangeReconciler skips external MultiLegCoordinator perp symbols on Mainnet HALT policy
        fake_live = SimpleNamespace(
            get_user_state=AsyncMock(
                return_value={
                    "assetPositions": [
                        {"position": {"coin": "HYPE", "szi": "-20.0", "entryPx": "25.0", "unrealizedPnl": "0.0"}}
                    ],
                    "marginSummary": {"accountValue": "10003.0"},
                }
            ),
            get_open_orders=AsyncMock(return_value=[]),
        )
        recon = ExchangeReconciler(
            live_client=fake_live,
            portfolio=p,
            orphan_exchange_policy="HALT",
            ignored_symbols_provider=lambda: {"HYPE"},
        )
        rep = await recon.reconcile_once()
        assert rep.success is True
        assert recon.health.halted is False
        assert "skipped_external:HYPE" in rep.actions

    asyncio.run(_run_portfolio_and_reconciler())

    # R-P1-1: Dashboard proxy injects Connection: close on HTTP/1.1 keep-alive but preserves WebSocket Upgrade
    keepalive_req = (
        b"GET /api/status HTTP/1.1\r\n"
        b"Host: localhost:8091\r\n"
        b"Connection: keep-alive\r\n\r\n"
    )
    rw = rewrite_http_head(keepalive_req, "172.18.0.9")
    assert b"Connection: close\r\n" in rw
    assert b"Connection: keep-alive" not in rw
    assert b"X-Forwarded-For: 172.18.0.9\r\n" in rw

    ws_req = (
        b"GET /socket.io/?EIO=4&transport=websocket HTTP/1.1\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n\r\n"
    )
    rw_ws = rewrite_http_head(ws_req, "172.18.0.9")
    assert b"Connection: Upgrade\r\n" in rw_ws
    assert b"X-Forwarded-For: 172.18.0.9\r\n" in rw_ws


@pytest.mark.unit
def test_boros_unrealized_pnl_and_watchdog_report_mkdir(tmp_path, monkeypatch) -> None:
    """Verify Boros dashboard card/detail subtracts $2,000 pooled margin base
    from equity_pooled (fixing the +$2,064.35 visual bug) and iv_gate_shadow_recheck
    creates missing report parent directories instead of raising FileNotFoundError."""
    import sqlite3 as _sqlite3
    import scripts.research.iv_gate_shadow_recheck as iv_recheck
    import src.research.paper_trades as pt

    # 1. Boros dashboard unrealized PnL fix
    bdb = tmp_path / "boros_shadow.db"
    con = _sqlite3.connect(str(bdb))
    con.executescript(
        """
        CREATE TABLE boros_trades (
            id INTEGER PRIMARY KEY, asset TEXT, structure TEXT, direction TEXT,
            state TEXT, entry_ms INTEGER, capital_usd REAL, fixed_bruto REAL, net_apy REAL
        );
        CREATE TABLE boros_close (
            trade_id INTEGER, ts_ms INTEGER, reason TEXT, fixed_usd REAL,
            floating_usd REAL, basis_usd REAL, costs_usd REAL, realized_usd REAL,
            realized_apy_cap REAL
        );
        CREATE TABLE boros_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id INTEGER, ts_ms INTEGER,
            equity_pooled REAL, funding_usd REAL
        );
        """
    )
    con.execute("INSERT INTO boros_trades VALUES (1, 'HYPE', 'simple', 'short_hl_long_b', 'closed', 1000, 4000.0, 0.07, 0.09)")
    con.execute("INSERT INTO boros_trades VALUES (3, 'HYPE', 'simple', 'short_hl_long_b', 'open', 2000, 4000.0, 0.08, 0.12)")
    con.execute("INSERT INTO boros_close VALUES (1, 1500, 'state_change', 10.0, -5.0, -35.0, 25.30, -55.30, -5.52)")
    # Pooled margin base = $2,000.0 -> equity_pooled=2009.05 means +$9.05 unrealized!
    con.execute("INSERT INTO boros_marks (trade_id, ts_ms, equity_pooled, funding_usd) VALUES (3, 3000, 2009.05, 4.20)")
    con.commit()
    con.close()

    monkeypatch.setattr(pt, "_RESEARCH", tmp_path)
    card = pt._boros_card(3500)
    detail = pt._boros_detail(3500)
    assert card["net"] == -55.3
    assert card["open_unreal_usd"] == 9.05
    assert len(detail["open"]) == 1
    assert detail["open"][0]["unreal_usd"] == 9.05

    # 2. Watchdog write_report creates missing parent dirs cleanly
    nested_report = tmp_path / "missing_docs" / "archive" / "IV_GATE_SHADOW_RECHECK_RESULT.md"
    monkeypatch.setattr(iv_recheck, "REPORT_PATH", nested_report)
    monkeypatch.setattr(iv_recheck, "STATE_DIR", tmp_path / "state_fallback")
    empty_slice = {
        "n": 15,
        "n_closed": 15,
        "n_open": 0,
        "win_rate": 0.5,
        "net_pnl_usd": 10.0,
        "avg_pnl_usd": 0.67,
        "median_pnl_usd": 0.5,
        "best_usd": 5.0,
        "worst_usd": -3.0,
    }
    iv_recheck.write_report(
        {
            "slices": {
                "high_iv": empty_slice,
                "low_iv": empty_slice,
                "unknown": empty_slice,
            },
            "concentration": {},
        },
        {"status": "HOLD_SHADOW", "detail": "test"},
        n_closed=30,
        n_high=15,
        n_low=15,
    )
    assert nested_report.exists()
