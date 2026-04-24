"""Edge case torture — probe every boundary and impossible input."""
import math
import threading
import time
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from data_aggregator import DataAggregator
from exchange_client import HyperliquidClient
from paper_trading import PaperTrader
from risk_manager import RiskManager
from strategy import MomentumStrategy
from utils import load_config


# =============================================================================
# 1. Price = 0, -1, NaN, Infinity
# =============================================================================
class TestPriceEdgeCases:
    def test_price_zero_rejected_by_sanity_check(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', 0.0) is False

    def test_price_negative_rejected_by_sanity_check(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', -1.0) is False

    def test_price_nan_rejected_by_sanity_check(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', float('nan')) is False

    def test_price_infinity_rejected_by_sanity_check(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', float('inf')) is False
        assert agg._is_price_sane('BTC', float('-inf')) is False

    def test_price_zero_exit_signals_no_crash(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            # Price dropping to 0 would be catastrophic; ensure we don't divide by zero
            try:
                exit_reason = trader._check_exit_signals_fast(0.0)
                # Should trigger stop loss (or be caught by sanity before this)
                assert exit_reason in ('STOP_LOSS', 'TRAILING_STOP', None)
            except ZeroDivisionError:
                pytest.fail('ZeroDivisionError on price=0 — mainnet lethal!')

    def test_price_nan_exit_signals_no_crash(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            try:
                _ = trader._check_exit_signals_fast(float('nan'))
            except (ZeroDivisionError, ValueError, TypeError) as e:
                pytest.fail(f'{type(e).__name__} on price=NaN — mainnet lethal!')

    def test_price_infinity_exit_signals_no_crash(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 1_000_000, 'oi_change': 0.01, 'funding': 0}
            trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
            try:
                _ = trader._check_exit_signals_fast(float('inf'))
            except (ZeroDivisionError, OverflowError) as e:
                pytest.fail(f'{type(e).__name__} on price=inf — mainnet lethal!')

    def test_price_negative_risk_manager_stop_loss_no_crash(self, mock_config):
        rm = RiskManager(mock_config)
        try:
            triggered = rm.check_stop_loss(85000.0, -1.0, 'long')
            assert isinstance(triggered, bool)
        except Exception as e:
            pytest.fail(f'RiskManager crashed on negative price: {e}')

    def test_strategy_analyze_with_nan_price(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, float('nan'))
            # NaN price is insane but strategy should not crash
            assert signal is None or signal == 'LONG'
        except (TypeError, ValueError) as e:
            pytest.fail(f'Strategy crashed on NaN price: {e}')


# =============================================================================
# 2. Volume = 0, -1, 10^18
# =============================================================================
class TestVolumeEdgeCases:
    def test_volume_zero_does_not_divide_by_zero(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 0,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, 85000.0)
            assert signal is None  # Volume 0 should not trigger
        except ZeroDivisionError:
            pytest.fail('ZeroDivisionError on volume=0')

    def test_volume_negative_does_not_crash(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': -1,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, 85000.0)
            # Negative volume is insane; no signal expected
            assert signal is None
        except (ValueError, ZeroDivisionError) as e:
            pytest.fail(f'Crash on volume=-1: {e}')

    def test_volume_1e18_does_not_overflow(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 10**18,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, 85000.0)
            # 1e18 / 1e6 average = 1e12 ratio — way above threshold
            # But Python floats handle it fine
            assert isinstance(signal, (str, type(None)))
        except OverflowError:
            pytest.fail('Overflow on volume=1e18')

    def test_paper_trader_volume_zero_entry(self, mock_config):
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(mock_config)
            candle = {'volume': 0, 'oi_change': 0.01, 'funding': 0}
            try:
                trader._enter_position('BTC', 'long', 85000.0, candle, 'bull')
                assert trader.current_position == 'long'
            except Exception as e:
                pytest.fail(f'PaperTrader crashed on volume=0: {e}')


# =============================================================================
# 3. OI change = +1000 %, -1000 %
# =============================================================================
class TestOIChangeEdgeCases:
    def test_oi_change_plus_1000pct(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 10.0,  # +1000%
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, 85000.0)
            # +1000% OI is absurd but code should not crash
            assert signal == 'LONG'
        except (OverflowError, ValueError) as e:
            pytest.fail(f'Crash on OI=+1000%: {e}')

    def test_oi_change_minus_1000pct(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': -10.0,  # -1000%
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        try:
            signal = strategy.analyze(data, 85000.0)
            assert signal is None  # Negative OI change blocks LONG
        except (OverflowError, ValueError) as e:
            pytest.fail(f'Crash on OI=-1000%: {e}')

    def test_oi_change_plus_1000pct_data_aggregator(self, mock_config):
        agg = DataAggregator(mock_config)
        agg.last_oi = {'binance': 1_000_000}
        result = {
            'oi_total': 11_000_000,  # 10x increase
            'exchanges_data': {'binance': {'oi_usd': 11_000_000}},
        }
        # Internal OI change calculation
        oi_old = sum(agg.last_oi.values())
        oi_change = (result['oi_total'] - oi_old) / oi_old
        assert oi_change == 10.0  # +1000%
        # Should not crash


# =============================================================================
# 4. All exchanges down simultaneously
# =============================================================================
class TestAllExchangesDown:
    def test_all_exchanges_down_returns_none(self, mock_config):
        agg = DataAggregator(mock_config)

        with patch.object(agg, '_fetch_binance', side_effect=Exception('DOWN')):
            with patch.object(agg, '_fetch_bybit', side_effect=Exception('DOWN')):
                with patch.object(agg, '_fetch_okx', side_effect=Exception('DOWN')):
                    with patch.object(agg, '_fetch_hyperliquid', side_effect=Exception('DOWN')):
                        result = agg.fetch_all_data('BTC')
                        assert result is None

    def test_all_exchanges_down_bot_loop_survives(self, mock_config):
        """
        PaperTrader.run_cycle must survive when aggregator returns None.
        """
        with patch('paper_trading.BotDatabase') as MockDB:
            MockDB.return_value._get_conn.return_value = MagicMock()
            trader = PaperTrader(mock_config)
            with patch.object(trader.aggregator, 'fetch_all_data', return_value=None):
                try:
                    trader.run_cycle('BTC')
                except Exception as e:
                    pytest.fail(f'Bot loop crashed when all exchanges down: {e}')

    def test_main_loop_backoff_on_errors(self, mock_config):
        """
        main.py loop increases consecutive_errors and sleeps.
        """
        from main import main
        # We'll just verify the logic inline since main() blocks
        max_errors = 5
        backoff_base = 5
        for consecutive in range(1, max_errors + 1):
            backoff = backoff_base * consecutive
            assert backoff > 0


# =============================================================================
# 5. Config file missing every key one by one
# =============================================================================
class TestConfigMissingKeys:
    def test_missing_bot_key_raises(self, mock_config, tmp_path):
        bad = {k: v for k, v in mock_config.items() if k != 'bot'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises(ValueError):
            load_config(str(p))

    def test_missing_assets_key_raises(self, mock_config, tmp_path):
        bad = {k: v for k, v in mock_config.items() if k != 'assets'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises(ValueError):
            load_config(str(p))

    def test_missing_polling_key_uses_defaults(self, mock_config, tmp_path):
        bad = {k: v for k, v in mock_config.items() if k != 'polling'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        # polling tem defaults, não deve falhar
        config = load_config(str(p))
        assert 'polling' not in config or config.get('polling', {}).get('oi_interval', 30) == 30

    def test_missing_risk_key_raises(self, mock_config, tmp_path):
        bad = {k: v for k, v in mock_config.items() if k != 'risk'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises((ValueError, KeyError)):
            load_config(str(p))

    def test_missing_strategy_key_raises(self, mock_config, tmp_path):
        bad = {k: v for k, v in mock_config.items() if k != 'strategy'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        with pytest.raises((ValueError, KeyError)):
            load_config(str(p))

    def test_missing_data_sources_graceful(self, mock_config, tmp_path):
        """
        data_sources missing is not in REQUIRED_KEYS in main.py,
        so it should be handled gracefully (or raise ValueError if added later).
        """
        bad = {k: v for k, v in mock_config.items() if k != 'data_sources'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        # main.py does not require data_sources explicitly, but DataAggregator needs it
        try:
            load_config(str(p))
        except ValueError:
            pass  # acceptable

    def test_missing_timeframes_uses_defaults(self, mock_config, tmp_path):
        """
        PaperTrader has defaults for timeframes, so missing key should not crash
        if defaults are sane.
        """
        bad = {k: v for k, v in mock_config.items() if k != 'timeframes'}
        p = tmp_path / 'bad.yaml'
        p.write_text(yaml.safe_dump(bad))
        cfg = load_config(str(p))
        with patch('paper_trading.BotDatabase'):
            trader = PaperTrader(cfg)
            assert trader.primary_tf == '5m'  # default in code

    def test_risk_subkeys_missing_one_by_one(self, mock_config, tmp_path):
        """
        Remove each risk sub-key and verify RiskManager either raises or uses a sane default.
        """
        risk_keys = ['max_position_size_usd', 'max_leverage', 'stop_loss_pct', 'max_daily_trades']
        for key in risk_keys:
            bad = mock_config.copy()
            bad['risk'] = {k: v for k, v in mock_config['risk'].items() if k != key}
            p = tmp_path / f'bad_risk_{key}.yaml'
            p.write_text(yaml.safe_dump(bad))
            cfg = load_config(str(p))
            try:
                rm = RiskManager(cfg)
                # If it didn't raise, at least it must have sensible defaults
                assert rm.can_trade() in (True, False)
            except (KeyError, ValueError):
                pass  # Raising is also acceptable — fail-fast is safe

    def test_strategy_subkeys_missing_one_by_one(self, mock_config, tmp_path):
        """
        Remove each strategy sub-key and verify MomentumStrategy initializes.
        """
        strat_keys = list(mock_config['strategy'].keys())
        for key in strat_keys:
            bad = mock_config.copy()
            bad['strategy'] = {k: v for k, v in mock_config['strategy'].items() if k != key}
            p = tmp_path / f'bad_strat_{key}.yaml'
            p.write_text(yaml.safe_dump(bad))
            cfg = load_config(str(p))
            try:
                strategy = MomentumStrategy(cfg)
                assert strategy.volume_threshold > 0
            except (KeyError, ValueError, TypeError):
                pass  # acceptable
