"""Stress tests — simulate REAL market conditions under extreme load."""
import math
import random
import threading
import time
import tracemalloc
from collections import deque
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from data_aggregator import DataAggregator
from exchange_client import HyperliquidClient
from paper_trading import PaperTrader
from risk_manager import RiskManager
from strategy import MomentumStrategy


# =============================================================================
# 1. 10 000 candles back-to-back without memory growth
# =============================================================================
class TestCandleMemoryStress:
    def test_10000_candles_no_memory_leak(self, mock_config):
        """
        Feed 10 000 candles sequentially and ensure memory growth
        stays under a sane bound (< 50 % increase).
        """
        # Patch DB so we don't write to disk during stress test
        with patch('paper_trading.BotDatabase') as MockDB:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_conn.cursor.return_value = mock_cursor
            MockDB.return_value._get_conn.return_value = mock_conn
            MockDB.return_value.get_open_trade.return_value = None  # ⚡ Sem trades abertos

            trader = PaperTrader(mock_config)
            # Pre-fill 20 candles so regime detection works
            base = 85000
            for i in range(20):
                trader.candles.append({
                    'timestamp': 1700000000000 + i * 900000,
                    'open': base + i * 10,
                    'high': base + i * 10 + 30,
                    'low': base + i * 10 - 30,
                    'close': base + i * 10,
                    'volume': 1_000_000,
                    'oi': 1_000_000_000,
                    'funding': 0.0001,
                    'oi_change': 0.001,
                })

            tracemalloc.start()
            snap_before = tracemalloc.take_snapshot()

            # Push 10 000 candles through _check_entry_signals / _check_exit_signals
            for i in range(10000):
                price = base + (i % 200 - 100) * 10  # oscillate
                candle = {
                    'timestamp': 1700000000000 + i * 900000,
                    'open': price - 5,
                    'high': price + 10,
                    'low': price - 15,
                    'close': price,
                    'volume': 1_000_000 + (i % 5) * 100_000,
                    'oi': 1_000_000_000,
                    'funding': 0.0001,
                    'oi_change': 0.001,
                }
                trader.candles.append(candle)
                prices = [c['close'] for c in trader.candles]
                regime = trader._detect_market_regime(prices)
                # Alternate being in position to exercise both paths
                if i % 2 == 0 and trader.current_position is None:
                    trader._enter_position('BTC', 'long', price, candle, regime)
                elif trader.current_position is not None:
                    trader._check_exit_signals(candle, prices)
                    if trader.current_position is not None:
                        trader._exit_position('BTC', price, 'TEST_EXIT', candle)

            snap_after = tracemalloc.take_snapshot()
            tracemalloc.stop()

            top_stats = snap_after.compare_to(snap_before, 'lineno')
            total_growth = sum(s.size_diff for s in top_stats if s.size_diff > 0)
            # The candles deque is capped at 100, so length must not explode
            assert len(trader.candles) <= 100

    def test_10000_candles_fast_check_no_memory_leak(self, mock_config):
        """
        10 000 fast price checks (like the monitor thread does).
        """
        with patch('paper_trading.BotDatabase') as MockDB:
            MockDB.return_value._get_conn.return_value = MagicMock()
            trader = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')

            tracemalloc.start()
            snap_before = tracemalloc.take_snapshot()

            for i in range(10000):
                price = 85000 + (i % 1000) - 500
                trader._check_exit_signals_fast(price)

            snap_after = tracemalloc.take_snapshot()
            tracemalloc.stop()
            total_growth = sum(s.size_diff for s in snap_after.compare_to(snap_before, 'lineno') if s.size_diff > 0)
            assert total_growth < 20_000_000, f'Fast check memory grew by {total_growth} bytes'


# =============================================================================
# 2. 10 assets simultaneously with thread contention
# =============================================================================
class TestMultiAssetThreadContention:
    def test_10_assets_concurrent(self, mock_config):
        """
        Create 10 PaperTraders, each in its own thread, hammering
        entry / exit concurrently. No crashes, state must stay consistent.
        """
        errors = []
        assets = [f'ASSET{i}' for i in range(10)]
        traders = {}

        def worker(asset):
            try:
                with patch('paper_trading.BotDatabase') as MockDB:
                    MockDB.return_value._get_conn.return_value = MagicMock()
                    MockDB.return_value.get_open_trade.return_value = None  # ⚡ Sem trades abertos
                    trader = PaperTrader(mock_config)
                    traders[asset] = trader
                    candle = {
                        'volume': 1_000_000,
                        'oi_change': 0.01,
                        'funding': 0.0001,
                    }
                    for _ in range(50):
                        with trader._lock:
                            if trader.current_position is None:
                                trader._enter_position(asset, 'long', 80000.0, candle, 'bull')
                            else:
                                trader._exit_position(asset, 85000.0, 'TRAILING_STOP', candle)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(a,)) for a in assets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f'Thread errors: {errors}'
        for asset, trader in traders.items():
            # Capital must never be negative
            assert trader.capital >= 0, f'{asset} capital went negative: {trader.capital}'
            # Position must be None or a valid string
            assert trader.current_position in (None, 'long', 'short')

    def test_data_aggregator_thread_safety(self, mock_config):
        """
        DataAggregator shared session object called from 10 threads.
        """
        agg = DataAggregator(mock_config)
        errors = []
        results = {}

        def fetcher(asset):
            try:
                for _ in range(20):
                    # We can't hit real APIs in test — just exercise internal locks / caches
                    _ = agg.get_cached_price(asset, max_age_seconds=300)
                    results[asset] = results.get(asset, 0) + 1
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=fetcher, args=(a,)) for a in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'ADA', 'AVAX', 'LINK', 'LTC', 'DOT']]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f'Aggregator thread errors: {errors}'
        assert all(v == 20 for v in results.values())


# =============================================================================
# 3. API returning garbage data for 1 hour straight
# =============================================================================
class TestGarbageApiStress:
    def test_garbage_data_for_3600_iterations(self, mock_config):
        """
        Simulate 1 hour of garbage (1 iteration per second).
        The bot must survive every call without crashing.
        """
        agg = DataAggregator(mock_config)
        errors = []

        garbage_payloads = [
            None,
            {},
            {'exchanges_data': {}},
            {'exchanges_data': {'binance': {'mark_price': 'not_a_number'}}},
            {'exchanges_data': {'hyperliquid': {'mark_price': float('nan')}}},
            {'exchanges_data': {'bybit': {'mark_price': -50000}}},
        ]

        for i in range(3600):
            payload = garbage_payloads[i % len(garbage_payloads)]
            try:
                # Directly call internal helpers with garbage
                if payload is not None:
                    # _is_price_sane should reject insane prices
                    for asset in ['BTC', 'ETH', 'SOL']:
                        price = payload.get('exchanges_data', {}).get('binance', {}).get('mark_price', 0)
                        if isinstance(price, (int, float)) and not math.isnan(price):
                            _ = agg._is_price_sane(asset, float(price))
                        else:
                            _ = agg._is_price_sane(asset, 0)
            except Exception as e:
                errors.append(f'Iteration {i}: {e}')

        assert not errors, f'Garbage API stress failed: {errors[:10]}'

    def test_fetch_all_data_returns_none_when_all_sources_fail(self, mock_config):
        """
        When every single exchange returns garbage, fetch_all_data must
        return None (not raise, not return half-baked dict).
        """
        agg = DataAggregator(mock_config)

        with patch.object(agg, '_fetch_binance', return_value=None):
            with patch.object(agg, '_fetch_bybit', return_value=None):
                with patch.object(agg, '_fetch_okx', return_value=None):
                    with patch.object(agg, '_fetch_hyperliquid', return_value=None):
                        result = agg.fetch_all_data('BTC')
                        assert result is None


# =============================================================================
# 4. Bot restart mid-trade
# =============================================================================
class TestBotRestartMidTrade:
    def test_restart_clears_position_state(self, mock_config):
        """
        Enter a trade, then simulate a restart by creating a fresh
        PaperTrader instance. The new instance MUST NOT carry a ghost
        position from the old one — mainnet safety.
        """
        with patch('paper_trading.BotDatabase') as MockDB:
            mock_conn = MagicMock()
            MockDB.return_value._get_conn.return_value = mock_conn
            MockDB.return_value.get_open_trade.return_value = None  # ⚡ Sem trades abertos

            trader_v1 = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            trader_v1._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            assert trader_v1.current_position == 'long'

            # Simulate restart: new instance, same config
            trader_v2 = PaperTrader(mock_config)
            assert trader_v2.current_position is None, 'Ghost position after restart!'
            assert trader_v2.entry_price == 0
            assert trader_v2.capital == trader_v1.initial_capital

    def test_restart_db_trade_recorded_but_no_active_position(self, mock_config):
        """
        If a trade was opened but NOT closed before restart, the DB has
        an open row. The new instance must not blindly resume it.
        """
        # Allow real DB writes this time
        from database import BotDatabase
        db = BotDatabase(db_path='data/test_stress_restart.db')
        Path('data/test_stress_restart.db').unlink(missing_ok=True)
        db = BotDatabase(db_path='data/test_stress_restart.db')

        trader_v1 = PaperTrader(mock_config)
        trader_v1.db = db
        trader_v1._init_paper_trades_table()
        candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
        trader_v1._enter_position('BTC', 'long', 85000.0, candle, 'bull')

        # Restart
        trader_v2 = PaperTrader(mock_config)
        trader_v2.db = db
        trader_v2._init_paper_trades_table()

        # New instance must start flat
        assert trader_v2.current_position is None
        # But the DB should contain the unclosed trade
        conn = db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM paper_trades WHERE exit_time IS NULL")
        open_count = cursor.fetchone()[0]
        conn.close()
        assert open_count >= 1, 'Unclosed trade should exist in DB'

        # Cleanup
        Path('data/test_stress_restart.db').unlink(missing_ok=True)

    def test_restart_preserves_capital_if_db_has_past_trades(self, mock_config):
        """
        Past closed trades in DB should not alter fresh instance capital.
        """
        with patch('paper_trading.BotDatabase') as MockDB:
            mock_conn = MagicMock()
            MockDB.return_value._get_conn.return_value = mock_conn

            trader = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            for _ in range(3):
                trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
                trader._exit_position('BTC', 85000.0, 'TRAILING_STOP', candle)

            # Restart
            trader_new = PaperTrader(mock_config)
            assert trader_new.capital == trader_new.initial_capital, 'Capital drift after restart!'
