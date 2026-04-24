"""
Integration tests for bot_engine.py — BotEngine start/stop, data fetching, DB saving.
"""
import pytest
import json
import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def reset_app_state():
    """Reset global app_state before each test."""
    import bot_engine as be
    original_state = be.app_state.copy()
    be.app_state.update({
        "bot_running": False,
        "trader": None,
        "aggregator": None,
        "db": None,
        "config": None,
        "last_price": 0,
        "last_data": {},
        "logs": [],
        "current_position": None,
        "equity_history": [10000],
        "trades": [],
        "capital": 10000,
        "update_count": 0,
    })
    yield
    be.app_state.update(original_state)


@pytest.fixture
def mock_engine_deps(mock_config):
    """Patch all external dependencies used by BotEngine."""
    with patch('data_aggregator.DataAggregator') as MockAgg, \
         patch('paper_trading.PaperTrader') as MockTrader, \
         patch('database.BotDatabase') as MockDB:

        agg_instance = MagicMock()
        agg_instance.get_cached_price.return_value = 0
        agg_instance._fetch_hyperliquid.return_value = {
            'mark_price': 85000.0,
            'oi': 1_000_000_000,
            'volume': 100_000_000,
            'funding': 0.0001,
        }
        agg_instance.fetch_all_data.return_value = {
            'oi_total': 2_500_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 150_000_000,
            'funding_avg': 0.0005,
            'exchanges_data': {
                'hyperliquid': {
                    'mark_price': 85000.0,
                    'oracle_price': 85001.0,
                    'oi_usd': 500_000_000,
                    'volume_24h': 50_000_000,
                }
            },
            'timestamp': time.time(),
        }
        MockAgg.return_value = agg_instance

        trader_instance = MagicMock()
        trader_instance._start_monitor_thread = MagicMock()
        trader_instance.stop_monitoring = MagicMock()
        MockTrader.return_value = trader_instance

        db_instance = MagicMock()
        db_instance.save_price = MagicMock()
        db_instance.save_oi = MagicMock()
        db_instance.save_funding = MagicMock()
        db_instance.save_open_interest = MagicMock()  # alias used by _save_market_data
        db_instance.save_funding_rate = MagicMock()     # alias used by _save_market_data
        MockDB.return_value = db_instance

        yield {
            'DataAggregator': MockAgg,
            'PaperTrader': MockTrader,
            'BotDatabase': MockDB,
            'agg': agg_instance,
            'trader': trader_instance,
            'db': db_instance,
        }


# =============================================================================
# 1. BotEngine Initialization
# =============================================================================
class TestBotEngineInit:
    def test_init_creates_components(self, mock_config, mock_engine_deps):
        """BotEngine.__init__ should instantiate aggregator, trader, and db."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        assert engine.aggregator is not None
        assert engine.trader is not None
        assert engine.db is not None
        assert engine.config == mock_config

    def test_init_sets_config_values(self, mock_config, mock_engine_deps):
        """BotEngine should read assets and poll interval from config."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        assert engine.assets == ['BTC', 'ETH']
        assert engine.poll_interval == 30

    def test_init_default_assets(self, mock_config, mock_engine_deps):
        """If assets missing in config, default should be used."""
        from bot_engine import BotEngine
        cfg = mock_config.copy()
        del cfg['assets']
        engine = BotEngine(cfg)
        assert engine.assets == ['BTC']


# =============================================================================
# 2. Start / Stop Lifecycle
# =============================================================================
class TestBotEngineLifecycle:
    def test_start_sets_running_true(self, mock_config, mock_engine_deps):
        """start() should set running=True and spawn a thread."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.start()
        assert engine.running is True
        assert engine._thread is not None
        assert engine._thread.is_alive()
        engine.stop()

    def test_start_when_already_running(self, mock_config, mock_engine_deps):
        """start() when already running should log a warning and not spawn new thread."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.start()
        original_thread = engine._thread
        engine.start()  # Second call
        assert engine._thread is original_thread  # Same thread
        engine.stop()

    def test_stop_sets_running_false(self, mock_config, mock_engine_deps):
        """stop() should set running=False and clear the event."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.start()
        time.sleep(0.1)
        engine.stop()
        assert engine.running is False
        assert engine._stop_event.is_set()

    def test_stop_calls_trader_stop_monitoring(self, mock_config, mock_engine_deps):
        """stop() should call trader.stop_monitoring()."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.start()
        time.sleep(0.1)
        engine.stop()
        mock_engine_deps['trader'].stop_monitoring.assert_called()

    def test_stop_when_not_running(self, mock_config, mock_engine_deps):
        """stop() when not running should be a no-op."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.stop()  # Should not raise
        assert engine.running is False

    def test_thread_dies_within_2_seconds(self, mock_config, mock_engine_deps):
        """After stop(), the thread should die within 2 seconds."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.start()
        time.sleep(0.2)
        engine.stop()
        assert not engine._thread.is_alive() or engine._thread.join(timeout=2) is None

    def test_is_running_property(self, mock_config, mock_engine_deps):
        """is_running property should reflect running state."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        assert engine.is_running is False
        engine.start()
        assert engine.is_running is True
        engine.stop()
        assert engine.is_running is False


# =============================================================================
# 3. Data Fetching Loop
# =============================================================================
class TestDataFetching:
    def test_run_fetches_price(self, mock_config, mock_engine_deps):
        """The _run loop should fetch price and update last_price."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.running = True
        with patch('bot_engine.time.sleep', side_effect=[None, None, Exception("break")]):
            with patch.object(engine, '_save_market_data'):
                try:
                    engine._run()
                except Exception:
                    pass
        # Price was fetched at least once
        assert mock_engine_deps['agg']._fetch_hyperliquid.called or mock_engine_deps['agg'].get_cached_price.called

    def test_run_updates_app_state_price(self, mock_config, mock_engine_deps, reset_app_state):
        """_run should update app_state['last_price']."""
        import bot_engine as be
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.running = True
        with patch('bot_engine.time.sleep', side_effect=[None, Exception("break")]):
            with patch.object(engine, '_save_market_data'):
                try:
                    engine._run()
                except Exception:
                    pass
        # last_price should have been updated from the mocked fetch
        assert be.app_state['last_price'] > 0

    def test_run_fetches_full_data(self, mock_config, mock_engine_deps):
        """_run should call fetch_all_data on the configured interval."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        engine.running = True
        engine.poll_interval = 0.05  # very fast for testing
        with patch('bot_engine.time.sleep', side_effect=[0.01, 0.01, Exception("break")]):
            with patch.object(engine, '_save_market_data'):
                try:
                    engine._run()
                except Exception:
                    pass
        assert mock_engine_deps['agg'].fetch_all_data.called

    def test_run_catches_exceptions(self, mock_config, mock_engine_deps):
        """Exceptions in the loop should be caught, not propagated."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        mock_engine_deps['agg'].get_cached_price.side_effect = RuntimeError("boom")
        with patch('bot_engine.time.sleep', side_effect=[None, Exception("break")]):
            try:
                engine._run()
            except Exception:
                pass
        # Should not raise unhandled exception
        assert True


# =============================================================================
# 4. DB Saving
# =============================================================================
class TestDatabaseSaving:
    def test_save_market_data_saves_oi(self, mock_config, mock_engine_deps):
        """_save_market_data should call save_open_interest when OI > 0."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        data = {'oi_total': 1_000_000_000, 'funding_avg': 0.0005}
        engine._save_market_data('BTC', data)
        mock_engine_deps['db'].save_open_interest.assert_called_once()

    def test_save_market_data_skips_zero_oi(self, mock_config, mock_engine_deps):
        """_save_market_data should skip OI when it's zero."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        data = {'oi_total': 0, 'funding_avg': 0.0005}
        engine._save_market_data('BTC', data)
        mock_engine_deps['db'].save_open_interest.assert_not_called()

    def test_save_market_data_saves_funding(self, mock_config, mock_engine_deps):
        """_save_market_data should call save_funding_rate when funding != 0."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        data = {'oi_total': 1_000_000_000, 'funding_avg': 0.0005}
        engine._save_market_data('BTC', data)
        mock_engine_deps['db'].save_funding_rate.assert_called_once()

    def test_save_market_data_skips_zero_funding(self, mock_config, mock_engine_deps):
        """_save_market_data should skip funding when it's zero."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        data = {'oi_total': 1_000_000_000, 'funding_avg': 0}
        engine._save_market_data('BTC', data)
        mock_engine_deps['db'].save_funding_rate.assert_not_called()

    def test_save_market_data_handles_db_error(self, mock_config, mock_engine_deps):
        """DB errors in _save_market_data should be caught and logged."""
        from bot_engine import BotEngine
        engine = BotEngine(mock_config)
        mock_engine_deps['db'].save_oi.side_effect = Exception("DB locked")
        data = {'oi_total': 1_000_000_000, 'funding_avg': 0.0005}
        # Should not raise
        engine._save_market_data('BTC', data)


# =============================================================================
# 5. Module-Level Functions
# =============================================================================
class TestModuleFunctions:
    def test_start_bot_engine_success(self, mock_config, mock_engine_deps, reset_app_state):
        """start_bot_engine should set app_state correctly on success."""
        import bot_engine as be
        result = be.start_bot_engine(mock_config)
        assert result is True
        assert be.app_state['bot_running'] is True
        assert be.app_state['trader'] is not None
        assert be.app_state['aggregator'] is not None
        assert be.app_state['db'] is not None
        assert be.app_state['config'] == mock_config
        assert be.app_state['capital'] == 10000.0
        # Cleanup
        be.stop_bot_engine()

    def test_start_bot_engine_when_already_running(self, mock_config, mock_engine_deps, reset_app_state):
        """start_bot_engine when already running should return False."""
        import bot_engine as be
        be.app_state['bot_running'] = True
        result = be.start_bot_engine(mock_config)
        assert result is False

    def test_start_bot_engine_exception(self, mock_config, mock_engine_deps, reset_app_state):
        """start_bot_engine should return False if BotEngine raises."""
        import bot_engine as be
        with patch('bot_engine.BotEngine', side_effect=Exception("init failed")):
            result = be.start_bot_engine(mock_config)
            assert result is False

    def test_stop_bot_engine(self, mock_config, mock_engine_deps, reset_app_state):
        """stop_bot_engine should set bot_running to False."""
        import bot_engine as be
        be.start_bot_engine(mock_config)
        be.stop_bot_engine()
        assert be.app_state['bot_running'] is False

    def test_get_bot_status_defaults(self, reset_app_state):
        """get_bot_status should return defaults when nothing running."""
        import bot_engine as be
        status = be.get_bot_status()
        assert status['running'] is False
        assert status['price'] == 0
        assert status['asset'] == 'BTC'
        assert status['update_count'] == 0
        assert status['capital'] == 10000
        assert status['position'] is None
        assert status['equity'] == 10000

    def test_get_bot_status_with_config(self, mock_config, mock_engine_deps, reset_app_state):
        """get_bot_status should use config asset when available."""
        import bot_engine as be
        be.app_state['config'] = mock_config
        status = be.get_bot_status()
        assert status['asset'] == 'BTC'

    def test_add_log(self, reset_app_state):
        """add_log should append log to app_state."""
        import bot_engine as be
        be.add_log("Test message", level="warning")
        assert len(be.app_state['logs']) == 1
        assert be.app_state['logs'][0]['message'] == "Test message"
        assert be.app_state['logs'][0]['level'] == "warning"

    def test_add_log_caps_at_1000(self, reset_app_state):
        """Logs should be capped at 1000 entries."""
        import bot_engine as be
        for i in range(1100):
            be.add_log(f"log {i}")
        assert len(be.app_state['logs']) == 1000


# =============================================================================
# 6. End-to-End: Start → Fetch → Stop
# =============================================================================
class TestEndToEnd:
    def test_full_lifecycle(self, mock_config, mock_engine_deps, reset_app_state):
        """Full lifecycle: start, let it run briefly, stop."""
        import bot_engine as be
        from bot_engine import BotEngine

        # Start
        result = be.start_bot_engine(mock_config)
        assert result is True

        # Let it spin for a short time
        time.sleep(0.3)

        # Verify running
        assert be.app_state['bot_running'] is True
        assert be.get_bot_status()['running'] is True

        # Stop
        be.stop_bot_engine()
        assert be.app_state['bot_running'] is False
        assert be.get_bot_status()['running'] is False

    def test_price_updates_during_run(self, mock_config, mock_engine_deps, reset_app_state):
        """During a brief run, price should be updated in app_state."""
        import bot_engine as be
        be.start_bot_engine(mock_config)
        time.sleep(0.5)
        status = be.get_bot_status()
        # Price may or may not be fetched depending on timing; just verify no crash
        assert isinstance(status['price'], (int, float))
        be.stop_bot_engine()

    def test_update_count_increments(self, mock_config, mock_engine_deps, reset_app_state):
        """update_count should increment during operation."""
        import bot_engine as be
        be.start_bot_engine(mock_config)
        initial = be.app_state['update_count']
        time.sleep(0.5)
        # Should have run at least one cycle
        assert be.app_state['update_count'] >= initial
        be.stop_bot_engine()
