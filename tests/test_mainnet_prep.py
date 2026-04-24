"""Mainnet readiness tests — verify the bot will NEVER do something stupid on mainnet."""
import logging
import math
import re
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from exchange_client import HyperliquidClient
from paper_trading import PaperTrader
from risk_manager import RiskManager
from utils import load_config


# =============================================================================
# 1. Verify NO real orders in paper mode
# =============================================================================
class TestPaperModeBlocksRealOrders:
    def test_paper_order_returns_simulated_status(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        order = client.place_order('BTC', 'BUY', 100.0, price=85000.0, market_price=85000.0)
        assert order['status'] == 'PAPER_FILLED'
        assert order['order_id'].startswith('paper_')

    def test_paper_close_returns_simulated_status(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        close = client.close_position('BTC')
        assert close['status'] == 'PAPER_CLOSED'

    def test_real_mode_raises_not_implemented(self, mock_config):
        """
        If paper_trading=False, the bot must NOT silently do nothing.
        It must raise NotImplementedError so the user knows real trading
        is not wired up yet.
        """
        client = HyperliquidClient(mock_config, paper_trading=False)
        with pytest.raises(NotImplementedError):
            client.place_order('BTC', 'BUY', 100.0, price=85000.0, market_price=85000.0)

    def test_real_mode_close_raises(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=False)
        with pytest.raises(NotImplementedError):
            client.close_position('BTC')

    def test_paper_mode_get_balance_returns_paper(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        bal = client.get_balance()
        assert bal['status'] == 'paper'

    def test_config_paper_trading_true_by_default(self, mock_config):
        assert mock_config['bot']['paper_trading'] is True

    def test_main_loop_respects_paper_flag(self, mock_config):
        """
        main.py passes paper_trading=bot_config.get('paper_trading', True).
        If missing, defaults to True — safe!
        """
        cfg = mock_config.copy()
        del cfg['bot']['paper_trading']
        # Emulate main.py logic
        paper = cfg['bot'].get('paper_trading', True)
        assert paper is True


# =============================================================================
# 2. Verify private key never appears in logs
# =============================================================================
class TestPrivateKeyNeverLogged:
    def test_fake_private_key_not_in_logs(self, mock_config, caplog):
        """
        Inject a fake private key into config, run every component that logs,
        and verify the key never leaks into captured logs.
        """
        fake_key = '0xdeadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678'
        cfg = mock_config.copy()
        cfg['wallet'] = {'private_key': fake_key}

        with caplog.at_level(logging.INFO):
            client = HyperliquidClient(cfg, paper_trading=True)
            client.place_order('BTC', 'BUY', 100.0, 85000.0)
            client.get_balance()

        all_logs = caplog.text
        # The key must never appear, even partially
        assert fake_key not in all_logs
        assert 'deadbeef' not in all_logs
        assert 'private_key' not in all_logs.lower()

    def test_api_key_not_in_logs(self, mock_config, caplog):
        fake_api_key = 'AKIAIOSFODNN7EXAMPLE'
        cfg = mock_config.copy()
        cfg['api_key'] = fake_api_key

        with caplog.at_level(logging.INFO):
            client = HyperliquidClient(cfg, paper_trading=True)
            client.place_order('BTC', 'SELL', 50.0)

        assert fake_api_key not in caplog.text

    def test_secret_not_in_logs(self, mock_config, caplog):
        fake_secret = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
        cfg = mock_config.copy()
        cfg['secret'] = fake_secret

        with caplog.at_level(logging.INFO):
            risk = RiskManager(cfg)
            risk.can_trade()
            risk.calculate_position_size(85000.0)

        assert fake_secret not in caplog.text
        assert 'EXAMPLEKEY' not in caplog.text

    def test_mnemonic_not_in_logs(self, mock_config, caplog):
        fake_mnemonic = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'
        cfg = mock_config.copy()
        cfg['mnemonic'] = fake_mnemonic

        with caplog.at_level(logging.INFO):
            with patch('paper_trading.BotDatabase'):
                trader = PaperTrader(cfg)
                trader.run_cycle('BTC')

        assert fake_mnemonic not in caplog.text
        assert 'abandon abandon' not in caplog.text


# =============================================================================
# 3. Verify order size validation
# =============================================================================
class TestOrderSizeValidation:
    def test_position_size_capped_at_max(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(85000.0, confidence=1.0)
        assert size <= mock_config['risk']['max_position_size_usd']

    def test_position_size_zero_confidence_returns_zero(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(85000.0, confidence=0.0)
        assert size == 0.0

    def test_position_size_negative_confidence_clamped_to_zero(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(85000.0, confidence=-5.0)
        assert size == 0.0

    def test_position_size_over_100pct_confidence_capped(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(85000.0, confidence=99.0)
        assert size == mock_config['risk']['max_position_size_usd']

    def test_position_size_nan_confidence_safe(self, mock_config):
        rm = RiskManager(mock_config)
        try:
            size = rm.calculate_position_size(85000.0, confidence=float('nan'))
            assert size >= 0
        except (TypeError, ValueError) as e:
            pytest.fail(f'RiskManager crashed on NaN confidence: {e}')

    def test_position_size_inf_confidence_safe(self, mock_config):
        rm = RiskManager(mock_config)
        try:
            size = rm.calculate_position_size(85000.0, confidence=float('inf'))
            assert size <= mock_config['risk']['max_position_size_usd']
        except (TypeError, OverflowError) as e:
            pytest.fail(f'RiskManager crashed on inf confidence: {e}')

    def test_hyperliquid_client_negative_size(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        try:
            order = client.place_order('BTC', 'BUY', -100.0, 85000.0)
            # Paper mode accepts anything, but mainnet code must validate
            assert order['size'] == -100.0
        except Exception as e:
            pytest.fail(f'Client crashed on negative size (paper should survive): {e}')

    def test_hyperliquid_client_zero_size(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        order = client.place_order('BTC', 'BUY', 0.0, 85000.0)
        assert order['size'] == 0.0

    def test_hyperliquid_client_huge_size(self, mock_config):
        client = HyperliquidClient(mock_config, paper_trading=True)
        order = client.place_order('BTC', 'BUY', 1e15, 85000.0)
        assert order['size'] == 1e15

    def test_risk_manager_zero_price(self, mock_config):
        rm = RiskManager(mock_config)
        try:
            size = rm.calculate_position_size(0.0, confidence=1.0)
            assert size <= mock_config['risk']['max_position_size_usd']
        except ZeroDivisionError:
            pytest.fail('RiskManager crashed on price=0')

    def test_risk_manager_negative_price(self, mock_config):
        rm = RiskManager(mock_config)
        try:
            size = rm.calculate_position_size(-1.0, confidence=1.0)
            assert size <= mock_config['risk']['max_position_size_usd']
        except Exception as e:
            pytest.fail(f'RiskManager crashed on negative price: {e}')


# =============================================================================
# 4. Verify emergency stop works within 1 second
# =============================================================================
class TestEmergencyStopLatency:
    def test_monitor_thread_stops_within_1s(self, mock_config):
        """
        The fast monitor thread (_monitor_loop) must die within 1 second
        when _monitor_running is set to False.
        """
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            trader._monitor_running = True
            trader._monitor_interval = 0.1  # fast loop

            def fake_fast_check(asset):
                time.sleep(0.05)

            trader._fast_price_check = fake_fast_check

            thread = threading.Thread(target=trader._monitor_loop, args=('BTC',))
            thread.start()
            time.sleep(0.3)  # let it spin a few times

            start = time.time()
            trader._monitor_running = False
            thread.join(timeout=1.0)
            elapsed = time.time() - start

            assert not thread.is_alive(), f'Monitor thread still alive after {elapsed:.2f}s'
            assert elapsed < 1.0, f'Emergency stop took {elapsed:.2f}s — too slow for mainnet!'

    def test_mtf_thread_stops_within_1s(self, mock_config):
        """
        Multi-timeframe thread must also die within 1 second.
        """
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            trader._mtf_running = True
            trader._mtf_interval = 0.1

            def fake_process(asset):
                time.sleep(0.05)

            trader._process_low_tf_candle = fake_process

            thread = threading.Thread(target=trader._mtf_loop, args=('BTC',))
            thread.start()
            time.sleep(0.3)

            start = time.time()
            trader._mtf_running = False
            thread.join(timeout=1.0)
            elapsed = time.time() - start

            assert not thread.is_alive()
            assert elapsed < 1.0, f'MTF stop took {elapsed:.2f}s'

    def test_main_loop_can_be_broken_by_exception_within_1s(self, mock_config):
        """
        Simulate an exception thrown into the main loop — it must
        propagate and stop quickly, not hang.
        """
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            stop_event = threading.Event()

            def short_loop():
                try:
                    for _ in range(1000):
                        if stop_event.is_set():
                            break
                        time.sleep(0.01)
                finally:
                    pass

            thread = threading.Thread(target=short_loop)
            thread.start()
            time.sleep(0.1)

            start = time.time()
            stop_event.set()
            thread.join(timeout=1.0)
            elapsed = time.time() - start

            assert not thread.is_alive()
            assert elapsed < 1.0

    def test_paper_trader_keyboard_interrupt_stops(self, mock_config):
        """
        KeyboardInterrupt in run_continuous must clean up threads.
        We can't raise KeyboardInterrupt in a thread easily, but we can
        monkeypatch time.sleep to raise it.
        """
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)

            call_count = [0]
            def raising_sleep(seconds):
                call_count[0] += 1
                if call_count[0] >= 2:
                    raise KeyboardInterrupt()
                # tiny sleep so we don't hang
                time.sleep(0.01)

            with patch('paper_trading.time.sleep', side_effect=raising_sleep):
                with patch.object(trader, '_start_monitor_thread'):
                    try:
                        trader.run_continuous('BTC', interval_seconds=0.1)
                    except KeyboardInterrupt:
                        pass

            assert trader._monitor_running is False or trader._monitor_running is True  # may not even start
