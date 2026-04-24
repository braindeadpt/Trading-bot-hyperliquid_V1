"""Tests for strategy.py — signal generation, SMA, volume spike, OI change, regime detection."""
import pytest
from collections import deque

from strategy import MomentumStrategy


# =============================================================================
# 1. SMA Calculation (via PaperTrader)
# =============================================================================
class TestSMACalculation:
    def test_sma_basic(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = [100, 110, 120, 130, 140]
        sma = trader._calculate_sma(prices, 3)
        assert sma == pytest.approx((120 + 130 + 140) / 3)

    def test_sma_full_period(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = list(range(1, 101))
        sma = trader._calculate_sma(prices, 10)
        assert sma == pytest.approx(sum(range(91, 101)) / 10)

    def test_sma_insufficient_data_returns_average_of_available(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = [100, 200]
        sma = trader._calculate_sma(prices, 10)
        assert sma == pytest.approx(150.0)

    def test_sma_single_price(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        sma = trader._calculate_sma([50000], 10)
        assert sma == 50000

    def test_sma_empty_list(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        # Edge case: empty list division by zero protection
        sma = trader._calculate_sma([], 10)
        assert sma == 0.0  # max(1, 0) denominator -> 0/1


# =============================================================================
# 2. Volume Spike Detection
# =============================================================================
class TestVolumeSpikeDetection:
    def test_volume_spike_detected(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        spike_volume = base_volume * strategy.volume_threshold * 1.5
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': spike_volume,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'

    def test_volume_spike_below_threshold_no_signal(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        below_threshold = base_volume * (strategy.volume_threshold * 0.5)
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': below_threshold,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_volume_spike_exactly_at_threshold(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        exact_threshold = base_volume * strategy.volume_threshold
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': exact_threshold,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        # > threshold, not >=, so exact threshold may or may not trigger
        # Looking at code: volume_ratio > self.volume_threshold
        assert signal is None  # exact threshold does not trigger

    def test_volume_spike_just_above_threshold(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        # Analise() adiciona volume_total ao histórico, então preenchemos até lookback-1
        for _ in range(strategy.volume_lookback - 1):
            strategy.volume_history.append(base_volume)

        # Volume 3x a média (3.0 > 2.5 threshold)
        spike_volume = base_volume * 3.0
        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': spike_volume,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'

    def test_insufficient_volume_history_returns_none(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        # Analise() adiciona 1 ao histórico, então preenchemos até (lookback//2 - 2)
        # para garantir que mesmo com o adicionado, ainda fica abaixo do limite
        target = strategy.volume_lookback // 2 - 2
        for _ in range(target):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 10_000_000,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_volume_history_accumulates(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for i in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000 + i * 1000)
        assert len(strategy.volume_history) == strategy.volume_lookback

    def test_volume_ratio_one_means_no_spike(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume,  # exactly average
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None


# =============================================================================
# 3. OI Change Detection
# =============================================================================
class TestOIChangeDetection:
    def test_oi_change_positive_triggers(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': mock_config['strategy']['oi_change_threshold'] + 0.005,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'

    def test_oi_change_below_threshold_no_signal(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': mock_config['strategy']['oi_change_threshold'] * 0.5,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_oi_change_negative_does_not_trigger_long(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': -0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_oi_change_exactly_at_threshold(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': mock_config['strategy']['oi_change_threshold'],
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.005,
        }
        # oi_change > threshold, not >=
        signal = strategy.analyze(data, 85000.0)
        assert signal is None


# =============================================================================
# 4. Funding Rate Extremes
# =============================================================================
class TestFundingRateExtremes:
    def test_funding_above_max_blocks_long(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': mock_config['strategy']['max_funding_rate'] + 0.001,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_funding_below_min_blocks_long(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': mock_config['strategy']['min_funding_rate'] - 0.001,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_funding_at_boundary_allowed(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': mock_config['strategy']['max_funding_rate'] - 0.0001,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'

    def test_funding_zero_allowed(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.0,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'


# =============================================================================
# 5. Position state management
# =============================================================================
class TestPositionState:
    def test_in_position_blocks_new_entry(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = True
        strategy.position_direction = 'long'
        strategy.entry_price = 80000

        base_volume = 1_000_000
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(base_volume)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': base_volume * strategy.volume_threshold * 2,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None  # Already in position, no new entry

    def test_reset_position_clears_state(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = True
        strategy.position_direction = 'long'
        strategy.entry_price = 80000
        strategy._reset_position()
        assert strategy.in_position is False
        assert strategy.position_direction is None
        assert strategy.entry_price == 0


# =============================================================================
# 6. Exit signals
# =============================================================================
class TestExitSignals:
    def test_should_exit_oi_fading_long(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = True
        strategy.position_direction = 'long'
        strategy.entry_price = 80000

        data = {'oi_change_pct': -0.01}
        signal = strategy.should_exit(85000, data)
        assert signal == 'CLOSE_LONG'

    def test_should_exit_oi_not_fading(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = True
        strategy.position_direction = 'long'
        strategy.entry_price = 80000

        data = {'oi_change_pct': 0.001}
        signal = strategy.should_exit(85000, data)
        assert signal is None

    def test_should_exit_no_position(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = False
        data = {'oi_change_pct': -0.01}
        signal = strategy.should_exit(85000, data)
        assert signal is None

    def test_should_exit_short_oi_rising(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        strategy.in_position = True
        strategy.position_direction = 'short'
        strategy.entry_price = 90000

        data = {'oi_change_pct': 0.01}
        signal = strategy.should_exit(85000, data)
        # OI a subir em short = possível squeeze, fecha a posição
        assert signal == 'CLOSE_SHORT'


# =============================================================================
# 7. Regime Detection (via PaperTrader's _detect_market_regime)
# =============================================================================
class TestRegimeDetection:
    def test_bull_regime(self, mock_config):
        # Use PaperTrader's regime detection logic by importing it
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = list(range(1, 201))  # Strong uptrend
        regime = trader._detect_market_regime(prices)
        assert regime == 'bull'

    def test_bear_regime(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = list(range(200, 0, -1))  # Strong downtrend
        regime = trader._detect_market_regime(prices)
        assert regime == 'bear'

    def test_ranging_regime(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        # Oscillate around a flat line
        prices = [100 + (i % 20 - 10) * 2 for i in range(250)]
        regime = trader._detect_market_regime(prices)
        assert regime == 'ranging'

    def test_bull_with_200_prices(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = [1000 + i * 10 for i in range(250)]  # 10x stronger uptrend
        regime = trader._detect_market_regime(prices)
        assert regime == 'bull'

    def test_bear_with_200_prices(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = [10000 - i * 10 for i in range(250)]  # Strong downtrend
        regime = trader._detect_market_regime(prices)
        assert regime == 'bear'

    def test_insufficient_data_falls_back_to_sma60(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        prices = [100 + i * 0.5 for i in range(50)]  # Only 50 prices
        regime = trader._detect_market_regime(prices)
        assert regime == 'bull'


# =============================================================================
# 8. Combined signal generation
# =============================================================================
class TestSignalGeneration:
    def test_all_conditions_met_long(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,  # 3x threshold=2.5
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal == 'LONG'

    def test_no_signal_when_volume_missing(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 0,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_no_signal_when_oi_missing(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 0,
            'oi_change_pct': 0,
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        signal = strategy.analyze(data, 85000.0)
        # OI change 0 is below threshold
        assert signal is None

    def test_signal_with_various_prices(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        for price in [1000, 50000, 100000]:
            signal = strategy.analyze(data, price)
            assert signal == 'LONG', f"Failed at price {price}"
            strategy._reset_position()  # Reset for next iteration

    def test_signal_with_extreme_funding_blocked(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,
            'funding_avg': 0.02,  # Extreme
        }
        signal = strategy.analyze(data, 85000.0)
        assert signal is None

    def test_multiple_signals_only_first_triggers(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        signal1 = strategy.analyze(data, 85000.0)
        assert signal1 == 'LONG'
        # Second call should not open another position
        signal2 = strategy.analyze(data, 86000.0)
        assert signal2 is None

    def test_exit_signal_after_entry(self, mock_config):
        strategy = MomentumStrategy(mock_config)
        for _ in range(strategy.volume_lookback):
            strategy.volume_history.append(1_000_000)

        data_entry = {
            'oi_total': 1_000_000_000,
            'oi_change_pct': 0.02,
            'volume_total': 3_000_000,
            'funding_avg': 0.005,
        }
        strategy.analyze(data_entry, 85000.0)
        assert strategy.in_position is True

        data_exit = {'oi_change_pct': -0.01}
        exit_signal = strategy.should_exit(90000, data_exit)
        assert exit_signal == 'CLOSE_LONG'
        assert strategy.in_position is False
