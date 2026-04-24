"""Tests for paper_trading.py — position open/close, PnL, equity tracking, thread safety."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from paper_trading import PaperTrader, AutoTuner


# =============================================================================
# 1. Initialization
# =============================================================================
class TestPaperTraderInit:
    def test_init_loads_config(self, mock_config):
        trader = PaperTrader(mock_config)
        assert trader.capital == mock_config['risk']['initial_capital']
        assert trader.initial_capital == mock_config['risk']['initial_capital']
        assert trader.current_position is None
        assert trader.trade_count == 0
        assert trader.daily_trades == 0

    def test_init_creates_db_table(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            instance = MagicMock()
            MockDB.return_value = instance
            PaperTrader(mock_config)
            instance._get_conn.assert_called()

    def test_init_uses_correct_timeframes(self, mock_config):
        trader = PaperTrader(mock_config)
        assert trader.primary_tf == mock_config['timeframes']['primary']
        assert trader.secondary_tf == mock_config['timeframes']['secondary']

    def test_init_threading_lock_exists(self, mock_config):
        trader = PaperTrader(mock_config)
        # threading.Lock() retorna um _thread.lock ou _RLock — verificamos pelo tipo concreto
        assert type(trader._lock).__name__ in ('lock', 'Lock', 'RLock')


# =============================================================================
# 2. Position Opening
# =============================================================================
class TestPositionOpening:
    def test_enter_long_position(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')

            assert trader.current_position == 'long'
            assert trader.entry_price == 85000.0
            assert trader.position_size > 0
            assert trader.max_price == 85000.0
            assert trader.min_price == 85000.0
            assert trader.trailing_active is False

    def test_enter_short_position(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': -0.01, 'funding': 0}
            trader._enter_position('BTC', 'short', 85000.0, candle, 'bear')

            assert trader.current_position == 'short'
            assert trader.entry_price == 85000.0
            assert trader.position_size > 0
            assert trader.max_price == 85000.0
            assert trader.min_price == 85000.0

    def test_enter_position_increments_trade_count(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            initial_count = trader.trade_count
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            assert trader.trade_count == initial_count + 1

    def test_enter_position_sets_trailing_stop_long(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            expected_sl = 85000.0 * (1 - mock_config['risk']['stop_loss_pct'])
            assert trader.trailing_stop == pytest.approx(expected_sl)

    def test_enter_position_sets_trailing_stop_short(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': -0.01, 'funding': 0}
            trader._enter_position('BTC', 'short', 85000.0, candle, 'bear')
            expected_sl = 85000.0 * (1 + mock_config['risk']['short_stop_loss_pct'])
            assert trader.trailing_stop == pytest.approx(expected_sl)


# =============================================================================
# 3. Position Closing / PnL
# =============================================================================
class TestPositionClosing:
    def test_exit_long_position_profit(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            trader = PaperTrader(mock_config)
            initial_capital = trader.capital
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
            trader._exit_position('BTC', 85000.0, 'TRAILING_STOP', {'close': 85000.0})

            assert trader.current_position is None
            assert trader.capital > initial_capital
            assert trader.entry_price == 0

    def test_exit_long_position_loss(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            trader = PaperTrader(mock_config)
            initial_capital = trader.capital
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            trader._exit_position('BTC', 80000.0, 'STOP_LOSS', {'close': 80000.0})

            assert trader.current_position is None
            assert trader.capital < initial_capital

    def test_exit_short_position_profit(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            trader = PaperTrader(mock_config)
            initial_capital = trader.capital
            candle = {'volume': 1000000, 'oi_change': -0.01, 'funding': 0}
            trader._enter_position('BTC', 'short', 85000.0, candle, 'bear')
            trader._exit_position('BTC', 80000.0, 'TRAILING_STOP', {'close': 80000.0})

            assert trader.current_position is None
            assert trader.capital > initial_capital

    def test_exit_short_position_loss(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            trader = PaperTrader(mock_config)
            initial_capital = trader.capital
            candle = {'volume': 1000000, 'oi_change': -0.01, 'funding': 0}
            trader._enter_position('BTC', 'short', 80000.0, candle, 'bear')
            trader._exit_position('BTC', 85000.0, 'STOP_LOSS', {'close': 85000.0})

            assert trader.current_position is None
            assert trader.capital < initial_capital

    def test_exit_position_applies_fees(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            trader = PaperTrader(mock_config)
            initial_capital = trader.capital
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            position_size = trader.max_position_usd
            expected_fee = position_size * trader.fee_pct * 2

            trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
            # Exit at breakeven — should lose fees
            trader._exit_position('BTC', 80000.0, 'STOP_LOSS', {'close': 80000.0})

            expected_capital = initial_capital - expected_fee
            assert trader.capital == pytest.approx(expected_capital, abs=0.01)

    def test_exit_position_updates_db(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            mock_instance = MagicMock()
            mock_cursor = MagicMock()
            mock_instance._get_conn.return_value.__enter__ = MagicMock(return_value=mock_cursor)
            mock_instance._get_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn = MagicMock()
            mock_cursor.cursor.return_value = mock_cursor
            mock_instance._get_conn.return_value = mock_conn
            mock_conn.cursor.return_value = mock_cursor
            MockDB.return_value = mock_instance

            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
            trader._exit_position('BTC', 85000.0, 'TRAILING_STOP', {'close': 85000.0})

            mock_conn.commit.assert_called()


# =============================================================================
# 4. Equity Tracking
# =============================================================================
class TestEquityTracking:
    def test_capital_never_negative(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}

            # Execute many losing trades
            for _ in range(50):
                trader._enter_position('BTC', 'long', 100000.0, candle, 'bull')
                trader._exit_position('BTC', 50000.0, 'STOP_LOSS', {'close': 50000.0})

            # Capital should not be negative (though it may be very low)
            assert trader.capital >= 0

    def test_pnl_calculation_long(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            entry = 80000.0
            exit_price = 88000.0  # +10%
            pnl_pct = (exit_price - entry) / entry
            assert pnl_pct == pytest.approx(0.10)

            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', entry, candle, 'bull')
            position_size = trader.position_size
            expected_pnl_usd = position_size * pnl_pct

            initial = trader.capital
            trader._exit_position('BTC', exit_price, 'TRAILING_STOP', {'close': exit_price})
            actual_pnl = trader.capital - initial + (position_size * trader.fee_pct * 2)
            assert actual_pnl == pytest.approx(expected_pnl_usd, abs=0.01)

    def test_pnl_calculation_short(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            entry = 80000.0
            exit_price = 72000.0  # -10% -> profit for short
            pnl_pct = (entry - exit_price) / entry
            assert pnl_pct == pytest.approx(0.10)

            candle = {'volume': 1000000, 'oi_change': -0.01, 'funding': 0}
            trader._enter_position('BTC', 'short', entry, candle, 'bear')
            position_size = trader.position_size
            expected_pnl_usd = position_size * pnl_pct

            initial = trader.capital
            trader._exit_position('BTC', exit_price, 'TRAILING_STOP', {'close': exit_price})
            actual_pnl = trader.capital - initial + (position_size * trader.fee_pct * 2)
            assert actual_pnl == pytest.approx(expected_pnl_usd, abs=0.01)


# =============================================================================
# 5. Thread Safety
# =============================================================================
class TestThreadSafety:
    def test_concurrent_position_access(self, mock_config):
        """Mock concurrent access to position state."""
        with patch('paper_trading.BotDatabase') as MockDB:
            MockDB.return_value.get_open_trade.return_value = None  # ⚡ Sem trades abertos
            
            trader = PaperTrader(mock_config)
            errors = []
            results = []

            def enter_and_exit():
                try:
                    candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
                    with trader._lock:
                        if trader.current_position is None:
                            trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
                            results.append('entered')
                        else:
                            results.append('already_in')
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=enter_and_exit) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert len(errors) == 0, f"Thread errors: {errors}"
            # Only one thread should have entered
            enter_count = results.count('entered')
            assert enter_count == 1

    def test_concurrent_exit_during_price_check(self, mock_config):
        """Ensure _fast_price_check and manual exit don't race."""
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')

            errors = []

            def fast_check_loop():
                for _ in range(100):
                    try:
                        with trader._lock:
                            if trader.current_position == 'long':
                                # Simulate reading position state
                                _ = trader.entry_price
                                _ = trader.max_price
                    except Exception as e:
                        errors.append(str(e))

            def exit_loop():
                for _ in range(50):
                    try:
                        with trader._lock:
                            if trader.current_position == 'long':
                                trader._exit_position('BTC', 85000.0, 'TRAILING_STOP', {'close': 85000.0})
                                # Re-enter for next iteration
                                trader._enter_position('BTC', 'long', 80000.0, candle, 'bull')
                    except Exception as e:
                        errors.append(str(e))

            t1 = threading.Thread(target=fast_check_loop)
            t2 = threading.Thread(target=exit_loop)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert len(errors) == 0, f"Race condition errors: {errors}"

    def test_monitor_thread_starts(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            with patch.object(trader, '_monitor_loop'):
                with patch.object(trader, '_mtf_loop'):
                    trader._start_monitor_thread('BTC')
                    assert trader._monitor_running is True
                    assert trader._monitor_thread is not None
                    assert trader._monitor_thread.daemon is True
                    trader._monitor_running = False
                    trader._mtf_running = False


# =============================================================================
# 6. SMA Calculation (PaperTrader)
# =============================================================================
class TestPaperTraderSMA:
    def test_calculate_sma_basic(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            prices = [100, 110, 120, 130, 140]
            sma = trader._calculate_sma(prices, 3)
            assert sma == pytest.approx((120 + 130 + 140) / 3)

    def test_calculate_sma_full_list(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            prices = list(range(1, 101))
            sma = trader._calculate_sma(prices, 10)
            assert sma == pytest.approx(sum(range(91, 101)) / 10)

    def test_calculate_sma_short_list(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            prices = [100, 200]
            sma = trader._calculate_sma(prices, 10)
            assert sma == pytest.approx(150.0)


# =============================================================================
# 7. Thresholds for Regime
# =============================================================================
class TestThresholdsForRegime:
    def test_bull_long_thresholds(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            thresholds = trader._get_thresholds_for_regime('bull', 'long')
            assert thresholds['volume'] == pytest.approx(trader.tuner.volume_threshold)
            assert thresholds['oi'] == pytest.approx(trader.tuner.oi_threshold)

    def test_bull_short_thresholds_higher(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            thresholds = trader._get_thresholds_for_regime('bull', 'short')
            assert thresholds['volume'] > trader.tuner.volume_threshold
            assert thresholds['oi'] > trader.tuner.oi_threshold

    def test_bear_short_thresholds(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            thresholds = trader._get_thresholds_for_regime('bear', 'short')
            assert thresholds['volume'] == pytest.approx(trader.tuner.volume_threshold)
            assert thresholds['oi'] == pytest.approx(trader.tuner.oi_threshold)

    def test_bear_long_thresholds_higher(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            thresholds = trader._get_thresholds_for_regime('bear', 'long')
            assert thresholds['volume'] > trader.tuner.volume_threshold
            assert thresholds['oi'] > trader.tuner.oi_threshold

    def test_ranging_thresholds(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            thresholds = trader._get_thresholds_for_regime('ranging', 'long')
            assert thresholds['volume'] == pytest.approx(trader.tuner.volume_threshold * 1.1)


# =============================================================================
# 8. AutoTuner
# =============================================================================
class TestAutoTuner:
    def test_tuner_initial_state(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            db = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            conn = MagicMock()
            conn.cursor.return_value = cursor
            db._get_conn.return_value = conn
            MockDB.return_value = db

            tuner = AutoTuner(mock_config, db)
            assert tuner.volume_threshold == mock_config['strategy']['volume_spike_threshold']
            assert tuner.oi_threshold == mock_config['strategy']['oi_change_threshold']

    def test_record_trade_adds_to_history(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            db = MagicMock()
            tuner = AutoTuner(mock_config, db)
            tuner.record_trade('long', 0.05, 'TRAILING_STOP')
            assert len(tuner.trade_history) == 1
            assert tuner.trade_history[0]['pnl_pct'] == 0.05

    def test_analyze_with_few_trades_returns_not_tuned(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            db = MagicMock()
            tuner = AutoTuner(mock_config, db)
            result = tuner.analyze_and_tune()
            assert result['tuned'] is False

    def test_analyze_low_win_rate_increases_volume_threshold(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            db = MagicMock()
            tuner = AutoTuner(mock_config, db)
            initial_vol = tuner.volume_threshold

            # Add many losing trades
            for _ in range(20):
                tuner.record_trade('long', -0.01, 'STOP_LOSS')

            result = tuner.analyze_and_tune()
            assert result['tuned'] is True
            assert result['volume_threshold'] > initial_vol

    def test_analyze_high_win_rate_decreases_volume_threshold(self, mock_config):
        with patch('paper_trading.BotDatabase') as MockDB:
            db = MagicMock()
            tuner = AutoTuner(mock_config, db)
            initial_vol = tuner.volume_threshold

            # Add many winning trades
            for _ in range(20):
                tuner.record_trade('long', 0.05, 'TRAILING_STOP')

            result = tuner.analyze_and_tune()
            assert result['tuned'] is True
            assert result['volume_threshold'] < initial_vol
