"""Tests for Telegram report formatters and notifier trade alerts."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from alerts.notifier import AlertConfig, AlertNotifier
from alerts.telegram_reports import (
    HELP_TEXT,
    build_pnl_message,
    build_strategy_message,
    build_trades_message,
    rolling_days_ms,
    utc_midnight_ms,
)
from data.database import Database, TradeEntry, TradeExit
import pytest

pytestmark = pytest.mark.integration_offline


class TestTelegramReports(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmpdir.name, "test.db"))
        now = int(time.time() * 1000)
        tid1 = self.db.save_trade_entry(
            TradeEntry(
                symbol="BTC",
                side="long",
                entry_price=100_000.0,
                size=0.1,
                strategy="VWAPDeviation",
                entry_time=now - 3600_000,
            )
        )
        self.db.update_trade_exit(
            TradeExit(
                trade_id=tid1,
                exit_price=101_000.0,
                exit_time=now - 1800_000,
                pnl_usd=100.0,
                pnl_pct=0.01,
                exit_reason="take_profit",
            )
        )
        tid2 = self.db.save_trade_entry(
            TradeEntry(
                symbol="ETH",
                side="short",
                entry_price=3000.0,
                size=1.0,
                strategy="VolatilityBreakout",
                entry_time=now - 7200_000,
            )
        )
        self.db.update_trade_exit(
            TradeExit(
                trade_id=tid2,
                exit_price=3100.0,
                exit_time=now - 3600_000,
                pnl_usd=-50.0,
                pnl_pct=-0.005,
                exit_reason="stop_loss",
            )
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def test_help_text_lists_commands(self) -> None:
        self.assertIn("/status", HELP_TEXT)
        self.assertIn("/pnl week", HELP_TEXT)

    def test_pnl_message_today(self) -> None:
        msg = build_pnl_message(self.db, period="day")
        self.assertIn("PnL", msg)
        self.assertIn("Realised PnL", msg)

    def test_pnl_message_week(self) -> None:
        msg = build_pnl_message(self.db, period="week")
        self.assertIn("Last 7 Days", msg)

    def test_trades_message(self) -> None:
        msg = build_trades_message(self.db, limit=5)
        self.assertIn("BTC", msg)
        self.assertIn("take_profit", msg)

    def test_strategy_message(self) -> None:
        msg = build_strategy_message(self.db)
        self.assertIn("VWAPDeviation", msg)
        self.assertIn("VolatilityBreakout", msg)

    def test_closed_trade_stats_since(self) -> None:
        stats = self.db.get_closed_trade_stats_since(rolling_days_ms(1))
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["wins"], 1)
        self.assertAlmostEqual(stats["total_pnl_usd"], 50.0)

    def test_utc_midnight_ms_is_today(self) -> None:
        ms = utc_midnight_ms()
        self.assertLess(ms, int(time.time() * 1000))


class TestNotifierTradeAlerts(unittest.IsolatedAsyncioTestCase):
    async def test_trade_entry_bypasses_min_level(self) -> None:
        cfg = AlertConfig(
            enabled=True,
            min_level="error",
            trade_alerts=True,
            telegram_bot_token="tok",
            telegram_chat_id="123",
        )
        notifier = AlertNotifier(cfg)
        with patch.object(notifier, "_send_telegram", new_callable=AsyncMock) as mock_send:
            await notifier.trade_entry("BTC", "long", 0.1, 100_000.0, "TestStrat")
            mock_send.assert_called_once()

    async def test_trade_alerts_disabled(self) -> None:
        cfg = AlertConfig(
            enabled=True,
            trade_alerts=False,
            telegram_bot_token="tok",
            telegram_chat_id="123",
        )
        notifier = AlertNotifier(cfg)
        with patch.object(notifier, "_send_telegram", new_callable=AsyncMock) as mock_send:
            await notifier.trade_entry("BTC", "long", 0.1, 100_000.0, "TestStrat")
            mock_send.assert_not_called()

    async def test_info_blocked_without_force(self) -> None:
        cfg = AlertConfig(enabled=True, min_level="warning", telegram_bot_token="tok", telegram_chat_id="123")
        notifier = AlertNotifier(cfg)
        with patch.object(notifier, "_send_telegram", new_callable=AsyncMock) as mock_send:
            await notifier.send("hello", level="info")
            mock_send.assert_not_called()


class TestTelegramCommandBotAuth(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_chat_ignored(self) -> None:
        from alerts.telegram_bot import TelegramCommandBot

        bot = TelegramCommandBot(
            token="tok",
            allowed_chat_ids=["999"],
            db=MagicMock(),
            engine_getter=lambda: None,
            notifier=None,
        )
        with patch.object(bot, "_send_message", new_callable=AsyncMock) as mock_reply:
            await bot._handle_update(
                {"message": {"chat": {"id": 111}, "text": "/status"}}
            )
            mock_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
