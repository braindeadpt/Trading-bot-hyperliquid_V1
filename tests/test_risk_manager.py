"""Tests for risk_manager.py — position size, stop loss, trailing stop, drawdown."""
import pytest

from risk_manager import RiskManager


# =============================================================================
# 1. Initialization
# =============================================================================
class TestRiskManagerInit:
    def test_init_loads_config(self, mock_config):
        rm = RiskManager(mock_config)
        assert rm.max_position == mock_config['risk']['max_position_size_usd']
        assert rm.max_leverage == mock_config['risk']['max_leverage']
        assert rm.stop_loss_pct == mock_config['risk']['stop_loss_pct']
        assert rm.max_daily_trades == mock_config['risk']['max_daily_trades']
        assert rm.daily_trades == 0
        assert rm.positions == {}


# =============================================================================
# 2. can_trade — daily trade limit
# =============================================================================
class TestCanTrade:
    def test_can_trade_initial_state(self, mock_config):
        rm = RiskManager(mock_config)
        assert rm.can_trade() is True

    def test_can_trade_at_limit_minus_one(self, mock_config):
        rm = RiskManager(mock_config)
        rm.daily_trades = mock_config['risk']['max_daily_trades'] - 1
        assert rm.can_trade() is True

    def test_can_trade_at_limit(self, mock_config):
        rm = RiskManager(mock_config)
        rm.daily_trades = mock_config['risk']['max_daily_trades']
        assert rm.can_trade() is False

    def test_can_trade_over_limit(self, mock_config):
        rm = RiskManager(mock_config)
        rm.daily_trades = mock_config['risk']['max_daily_trades'] + 5
        assert rm.can_trade() is False

    def test_can_trade_zero_limit(self, mock_config):
        mock_config['risk']['max_daily_trades'] = 0
        rm = RiskManager(mock_config)
        assert rm.can_trade() is False


# =============================================================================
# 3. Position size calculation
# =============================================================================
class TestPositionSize:
    def test_position_size_default_confidence(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(price=85000.0)
        assert size == pytest.approx(mock_config['risk']['max_position_size_usd'])

    def test_position_size_half_confidence(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(price=85000.0, confidence=0.5)
        assert size == pytest.approx(mock_config['risk']['max_position_size_usd'] * 0.5)

    def test_position_size_high_confidence_capped(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(price=85000.0, confidence=2.0)
        assert size == pytest.approx(mock_config['risk']['max_position_size_usd'])

    def test_position_size_zero_confidence(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(price=85000.0, confidence=0.0)
        assert size == 0.0

    def test_position_size_various_prices(self, mock_config):
        rm = RiskManager(mock_config)
        for price in [1.0, 1000.0, 50000.0, 100000.0]:
            size = rm.calculate_position_size(price=price, confidence=1.0)
            assert size == pytest.approx(mock_config['risk']['max_position_size_usd'])

    def test_position_size_returns_float(self, mock_config):
        rm = RiskManager(mock_config)
        size = rm.calculate_position_size(price=85000.0, confidence=0.75)
        assert isinstance(size, float)


# =============================================================================
# 4. Stop loss checks
# =============================================================================
class TestStopLoss:
    def test_stop_loss_long_not_triggered(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 99000.0  # 1% below entry
        sl_pct = mock_config['risk']['stop_loss_pct']
        assert (entry - current) / entry == pytest.approx(0.01, abs=0.0001)
        assert sl_pct == 0.02
        assert rm.check_stop_loss(entry, current, 'long') is False

    def test_stop_loss_long_triggered(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 97000.0  # 3% below entry (> 2% SL)
        assert rm.check_stop_loss(entry, current, 'long') is True

    def test_stop_loss_long_exactly_at_threshold(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 98000.0  # exactly 2% loss
        # check_stop_loss uses >=, so exactly at threshold triggers
        assert rm.check_stop_loss(entry, current, 'long') is True

    def test_stop_loss_short_not_triggered(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 101000.0  # 1% above entry
        assert rm.check_stop_loss(entry, current, 'short') is False

    def test_stop_loss_short_triggered(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 103000.0  # 3% above entry (> 2% SL)
        assert rm.check_stop_loss(entry, current, 'short') is True

    def test_stop_loss_short_exactly_at_threshold(self, mock_config):
        rm = RiskManager(mock_config)
        entry = 100000.0
        current = 102000.0  # exactly 2% loss
        assert rm.check_stop_loss(entry, current, 'short') is True

    def test_stop_loss_breakeven(self, mock_config):
        rm = RiskManager(mock_config)
        assert rm.check_stop_loss(100000.0, 100000.0, 'long') is False
        assert rm.check_stop_loss(100000.0, 100000.0, 'short') is False

    def test_stop_loss_profit_long(self, mock_config):
        rm = RiskManager(mock_config)
        assert rm.check_stop_loss(100000.0, 110000.0, 'long') is False

    def test_stop_loss_profit_short(self, mock_config):
        rm = RiskManager(mock_config)
        assert rm.check_stop_loss(100000.0, 90000.0, 'short') is False


# =============================================================================
# 5. Trailing stop logic (via PaperTrader)
# =============================================================================
class TestTrailingStop:
    def test_trailing_stop_long_activation(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'long'
        trader.entry_price = 80000.0
        trader.max_price = 80000.0
        trader.trailing_active = False
        trader.trailing_activation = 0.015
        trader.trailing_pct = 0.015
        trader.stop_loss_pct = 0.02

        # Price moves up 2% -> activate trailing
        exit_reason = trader._check_exit_signals_fast(81600.0)
        assert exit_reason is None  # No exit yet
        assert trader.trailing_active is True
        expected_stop = 81600.0 * (1 - 0.015)
        assert trader.trailing_stop == pytest.approx(expected_stop, abs=0.01)

    def test_trailing_stop_long_not_activated_below_threshold(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'long'
        trader.entry_price = 80000.0
        trader.max_price = 80000.0
        trader.trailing_active = False
        trader.trailing_activation = 0.015
        trader.stop_loss_pct = 0.02

        # Price moves up 1% only — below activation
        exit_reason = trader._check_exit_signals_fast(80800.0)
        assert exit_reason is None
        assert trader.trailing_active is False

    def test_trailing_stop_long_triggers_when_price_drops(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'long'
        trader.entry_price = 80000.0
        trader.max_price = 82000.0  # Max reached
        trader.trailing_active = True
        trader.trailing_pct = 0.015
        # 82000 * 0.985 = 80770
        trader.trailing_stop = 80770.0

        # Preço desceu para 80750, abaixo do trailing stop
        exit_reason = trader._check_exit_signals_fast(80750.0)
        assert exit_reason == 'TRAILING_STOP'

    def test_trailing_stop_long_updates_on_new_high(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'long'
        trader.entry_price = 80000.0
        trader.max_price = 82000.0
        trader.trailing_active = True
        trader.trailing_pct = 0.015
        trader.trailing_stop = 80870.0

        # New high of 83000
        exit_reason = trader._check_exit_signals_fast(83000.0)
        assert exit_reason is None
        expected_new_stop = 83000.0 * (1 - 0.015)
        assert trader.trailing_stop == pytest.approx(expected_new_stop, abs=0.01)

    def test_trailing_stop_short_activation(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'short'
        trader.entry_price = 80000.0
        trader.min_price = 80000.0
        trader.trailing_active = False
        trader.trailing_activation = 0.015
        trader.trailing_pct = 0.015
        trader.short_stop_loss = 0.025

        exit_reason = trader._check_exit_signals_fast(78400.0)  # -2%
        assert exit_reason is None
        assert trader.trailing_active is True
        expected_stop = 78400.0 * (1 + 0.015)
        assert trader.trailing_stop == pytest.approx(expected_stop, abs=0.01)

    def test_trailing_stop_short_triggers_when_price_rises(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'short'
        trader.entry_price = 80000.0
        trader.min_price = 78000.0
        trader.trailing_active = True
        trader.trailing_pct = 0.015
        trader.trailing_stop = 78000.0 * (1 + 0.015)  # 79170

        exit_reason = trader._check_exit_signals_fast(79200.0)
        assert exit_reason == 'TRAILING_STOP'

    def test_trailing_stop_short_updates_on_new_low(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'short'
        trader.entry_price = 80000.0
        trader.min_price = 78000.0
        trader.trailing_active = True
        trader.trailing_pct = 0.015
        trader.trailing_stop = 79170.0

        exit_reason = trader._check_exit_signals_fast(77000.0)
        assert exit_reason is None
        expected_new_stop = 77000.0 * (1 + 0.015)
        assert trader.trailing_stop == pytest.approx(expected_new_stop, abs=0.01)

    def test_stop_loss_long_before_trailing_active(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'long'
        trader.entry_price = 80000.0
        trader.max_price = 80000.0
        trader.trailing_active = False
        trader.stop_loss_pct = 0.02

        exit_reason = trader._check_exit_signals_fast(78400.0)  # -2%
        assert exit_reason == 'STOP_LOSS'

    def test_stop_loss_short_before_trailing_active(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = 'short'
        trader.entry_price = 80000.0
        trader.min_price = 80000.0
        trader.trailing_active = False
        trader.short_stop_loss = 0.025

        exit_reason = trader._check_exit_signals_fast(82000.0)  # +2.5%
        assert exit_reason == 'STOP_LOSS'

    def test_no_position_returns_none(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        trader.current_position = None
        assert trader._check_exit_signals_fast(85000.0) is None


# =============================================================================
# 6. Max drawdown tracking (via PaperTrader equity)
# =============================================================================
class TestMaxDrawdown:
    def test_equity_tracking_initial(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        assert trader.capital == mock_config['risk']['initial_capital']
        assert trader.initial_capital == mock_config['risk']['initial_capital']

    def test_equity_after_winning_trade(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        initial = trader.capital
        # Simulate a winning trade
        trader._enter_position('BTC', 'long', 80000.0, {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}, 'bull')
        # Exit at higher price
        trader._exit_position('BTC', 85000.0, 'TRAILING_STOP', {'close': 85000.0})
        assert trader.capital > initial

    def test_equity_after_losing_trade(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        initial = trader.capital
        trader._enter_position('BTC', 'long', 80000.0, {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}, 'bull')
        # Exit at lower price
        trader._exit_position('BTC', 75000.0, 'STOP_LOSS', {'close': 75000.0})
        assert trader.capital < initial

    def test_drawdown_calculation(self, mock_config):
        from paper_trading import PaperTrader
        trader = PaperTrader(mock_config)
        initial = trader.capital
        peak = initial

        # Simulate a sequence of trades that create drawdown
        # Position size is $100 with 2% SL + fees ~0.6%
        for _ in range(10):
            trader._enter_position('BTC', 'long', 80000.0, {'volume': 1000000, 'oi_change': 0.01, 'funding': 0}, 'bull')
            trader._exit_position('BTC', 75000.0, 'STOP_LOSS', {'close': 75000.0})

        drawdown = (peak - trader.capital) / peak
        assert drawdown > 0
        # 10 trades losing ~2.6% each (2% SL + 0.6% fees on $100 of $10k capital = 0.26% per trade)
        assert drawdown > 0.005  # At least 0.5% drawdown after 10 trades


# =============================================================================
# 7. record_trade
# =============================================================================
class TestRecordTrade:
    def test_record_trade_increments_counter(self, mock_config):
        rm = RiskManager(mock_config)
        initial = rm.daily_trades
        rm.record_trade()
        assert rm.daily_trades == initial + 1

    def test_record_trade_multiple(self, mock_config):
        rm = RiskManager(mock_config)
        for _ in range(5):
            rm.record_trade()
        assert rm.daily_trades == 5

    def test_record_trade_respects_can_trade(self, mock_config):
        rm = RiskManager(mock_config)
        max_trades = mock_config['risk']['max_daily_trades']
        for _ in range(max_trades + 2):
            rm.record_trade()
        assert rm.daily_trades == max_trades + 2
        assert rm.can_trade() is False
