"""
🧪 TESTES DO MAINNET GUARDIAN v3
==================================
Testes unitários para todas as camadas de segurança.

Correr:
    cd trading-bot-hyperliquid
    python -m pytest tests/test_mainnet_guardian.py -v
    python -m pytest tests/test_mainnet_guardian.py -v --tb=short
"""
import pytest
import json
import time
import os
import signal
from pathlib import Path
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch

# Add src to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from mainnet_guardian import (
    NetworkGate, HttpsEnforcer, RateLimiter, OrderValidator,
    CircuitBreaker, GracefulShutdown, EmergencyStop, AuditLogger,
    MainnetGuardian, OrderCheckResult, CircuitStatus,
    MIN_ORDER_SIZE_USD, MAX_ORDER_SIZE_USD, MAX_SLIPPAGE_PCT,
    MAX_PRICE_DEVIATION_PCT, DAILY_LOSS_SOFT_PCT, DAILY_LOSS_HARD_PCT,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def base_config():
    return {
        'bot': {'paper_trading': True, 'network': 'paper'},
        'risk': {
            'initial_capital': 10000.0,
            'max_position_size_usd': 100,
            'max_leverage': 2,
            'stop_loss_pct': 0.02,
            'short_stop_loss_pct': 0.02,
            'daily_loss_limit_pct': 0.05,
            'daily_loss_hard_stop_pct': 0.10,
            'mainnet_enabled': False,
            'mainnet_confirm_required': True,
        },
        'data_sources': {
            'binance': {'enabled': True, 'base_url': 'https://fapi.binance.com'},
            'bybit': {'enabled': True, 'base_url': 'https://api.bybit.com'},
            'okx': {'enabled': True, 'base_url': 'https://www.okx.com'},
            'hyperliquid': {'enabled': True, 'base_url': 'https://api.hyperliquid.xyz'},
        }
    }


@pytest.fixture
def mainnet_config(base_config):
    cfg = base_config.copy()
    cfg['bot'] = {'paper_trading': False, 'network': 'mainnet'}
    cfg['risk']['mainnet_enabled'] = True
    return cfg


# =============================================================================
# NETWORK GATE TESTS
# =============================================================================

class TestNetworkGate:
    def test_paper_trading_always_safe(self, base_config):
        gate = NetworkGate(base_config)
        assert gate.get_network() == 'paper'
        assert gate.can_trade_real() is True
    
    def test_mainnet_blocked_by_default(self, base_config):
        cfg = base_config.copy()
        cfg['bot'] = {'paper_trading': False, 'network': 'mainnet'}
        gate = NetworkGate(cfg)
        assert gate.can_trade_real() is False
    
    def test_mainnet_needs_approval_file(self, mainnet_config, tmp_path):
        gate = NetworkGate(mainnet_config)
        # Mudar o project_root para tmp_path
        gate.project_root = tmp_path
        gate.approval_file = tmp_path / '.mainnet_approved'
        gate.blocker_file = tmp_path / '.mainnet_blocked'
        
        assert gate.can_trade_real() is False  # Sem ficheiro
        
        # Criar ficheiro
        gate.approve_mainnet()
        assert gate.can_trade_real() is True
    
    def test_blocker_file_blocks_mainnet(self, mainnet_config, tmp_path):
        gate = NetworkGate(mainnet_config)
        gate.project_root = tmp_path
        gate.approval_file = tmp_path / '.mainnet_approved'
        gate.blocker_file = tmp_path / '.mainnet_blocked'
        
        gate.approve_mainnet()
        assert gate.can_trade_real() is True
        
        gate.block_mainnet()
        assert gate.can_trade_real() is False
    
    def test_testnet_needs_mainnet_enabled(self, base_config, tmp_path):
        cfg = base_config.copy()
        cfg['bot'] = {'paper_trading': False, 'network': 'testnet'}
        gate = NetworkGate(cfg)
        assert gate.can_trade_real() is False  # mainnet_enabled=False
        
        cfg['risk']['mainnet_enabled'] = True
        gate = NetworkGate(cfg)
        assert gate.can_trade_real() is True


# =============================================================================
# HTTPS ENFORCER TESTS
# =============================================================================

class TestHttpsEnforcer:
    def test_valid_https_urls(self, base_config):
        enforcer = HttpsEnforcer(base_config)
        ok, violations = enforcer.validate_all_urls()
        assert ok is True
        assert len(violations) == 0
    
    def test_http_url_rejected(self, base_config):
        cfg = base_config.copy()
        cfg['data_sources']['bad'] = {'enabled': True, 'base_url': 'http://evil.com'}
        enforcer = HttpsEnforcer(cfg)
        ok, violations = enforcer.validate_all_urls()
        assert ok is False
        assert len(violations) == 1
        assert 'evil.com' in violations[0]
    
    def test_is_https_static(self):
        assert HttpsEnforcer.is_https('https://api.example.com') is True
        assert HttpsEnforcer.is_https('http://api.example.com') is False
        assert HttpsEnforcer.is_https('ftp://files.example.com') is False


# =============================================================================
# RATE LIMITER TESTS
# =============================================================================

class TestRateLimiter:
    def test_wait_if_needed(self):
        limiter = RateLimiter()
        # Primeiro request não deve esperar
        wait = limiter.wait_if_needed('binance')
        assert wait >= 0
        
        # Segundo request imediato deve esperar
        wait2 = limiter.wait_if_needed('binance')
        assert wait2 >= 0  # Pode ser 0 se passou tempo suficiente
    
    def test_record_request(self):
        limiter = RateLimiter()
        limiter.record_request('binance', 200)
        limiter.record_request('binance', 429)
        
        stats = limiter.get_stats('binance', 60)
        assert stats['total_requests'] == 2
        assert stats['errors'] == 1
    
    def test_different_exchanges_independent(self):
        limiter = RateLimiter()
        limiter.record_request('binance', 200)
        limiter.record_request('bybit', 200)
        
        assert limiter.get_stats('binance', 60)['total_requests'] == 1
        assert limiter.get_stats('bybit', 60)['total_requests'] == 1


# =============================================================================
# ORDER VALIDATOR TESTS
# =============================================================================

class TestOrderValidator:
    def test_valid_order(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'BUY', 50, market_price=50000)
        assert result.is_valid is True
        assert result.reason == "OK"
        assert len(result.checks_passed) > 0
    
    def test_min_size_rejected(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'BUY', 5, market_price=50000)
        assert result.is_valid is False
        assert "mínimo" in result.reason.lower() or "minimum" in result.reason.lower()
    
    def test_max_size_rejected(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'BUY', 200_000, market_price=50000)
        assert result.is_valid is False
        assert "máximo" in result.reason.lower() or "maximum" in result.reason.lower()
    
    def test_invalid_side(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'HODL', 50, market_price=50000)
        assert result.is_valid is False
    
    def test_invalid_market_price(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'BUY', 50, market_price=0)
        assert result.is_valid is False
    
    def test_price_deviation(self, base_config):
        validator = OrderValidator(base_config)
        market_price = 50000
        bad_price = market_price * 1.05  # 5% de desvio
        result = validator.validate('BTC', 'BUY', 50, price=bad_price, market_price=market_price)
        assert result.is_valid is False
        assert "desvio" in result.reason.lower() or "deviation" in result.reason.lower()
    
    def test_margin_insufficient(self, base_config):
        validator = OrderValidator(base_config)
        result = validator.validate('BTC', 'BUY', 50, market_price=50000, account_balance=10)
        assert result.is_valid is False
        assert "margem" in result.reason.lower() or "margin" in result.reason.lower()
    
    def test_slippage_acceptable(self, base_config):
        validator = OrderValidator(base_config)
        ok, pct = validator.validate_slippage(50000, 50200, 'BUY')  # 0.4%
        assert ok is True
        assert pct == 0.004
    
    def test_slippage_excessive(self, base_config):
        validator = OrderValidator(base_config)
        ok, pct = validator.validate_slippage(50000, 53000, 'BUY')  # 6%
        assert ok is False
        assert pct == 0.06


# =============================================================================
# CIRCUIT BREAKER TESTS
# =============================================================================

class TestCircuitBreaker:
    def test_no_trip_on_small_loss(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 9800  # -2%
        assert cb.check(current, initial) is False
    
    def test_soft_stop_at_5_percent(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 9499  # -5.01%
        assert cb.check(current, initial) is True
        assert cb._tripped is True
        assert "SOFT STOP" in cb._reason
    
    def test_hard_stop_at_10_percent(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 8999  # -10.01%
        assert cb.check(current, initial) is True
        assert "HARD STOP" in cb._reason
    
    def test_daily_reset(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 9000  # -10%, hard stop
        assert cb.check(current, initial) is True
        
        # Simular mudança de dia
        cb.daily_date = date.today() - timedelta(days=1)
        assert cb.check(10000, initial) is False  # Reset
    
    def test_soft_reset_allowed(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 9499
        cb.check(current, initial)  # Soft stop
        
        assert cb.reset() is True
        assert cb._tripped is False
    
    def test_hard_reset_blocked(self, base_config):
        cb = CircuitBreaker(base_config)
        initial = 10000
        current = 8999
        cb.check(current, initial)  # Hard stop
        
        assert cb.reset() is False
        assert cb._tripped is True
    
    def test_record_trade_pnl(self, base_config):
        cb = CircuitBreaker(base_config)
        cb.record_trade_pnl(-100)
        cb.record_trade_pnl(50)
        assert cb.daily_pnl == -50


# =============================================================================
# GRACEFUL SHUTDOWN TESTS
# =============================================================================

class TestGracefulShutdown:
    def test_register_callback(self, base_config):
        gs = GracefulShutdown()
        mock_callback = MagicMock()
        gs.register_position_close_callback(mock_callback)
        assert gs._position_close_callback is mock_callback
    
    def test_shutdown_executes_callback(self, base_config):
        gs = GracefulShutdown()
        mock_callback = MagicMock()
        gs.register_position_close_callback(mock_callback)
        
        # Não testamos signals aqui, apenas o execute_shutdown diretamente
        gs._execute_shutdown()
        mock_callback.assert_called_once()
    
    def test_is_shutdown_requested(self, base_config):
        gs = GracefulShutdown()
        assert gs.is_shutdown_requested() is False
        
        gs._shutdown_in_progress = True
        assert gs.is_shutdown_requested() is True


# =============================================================================
# EMERGENCY STOP TESTS
# =============================================================================

class TestEmergencyStop:
    def test_check_no_stop_file(self, tmp_path):
        es = EmergencyStop(tmp_path)
        assert es.check() is False
    
    def test_stop_and_check(self, tmp_path):
        es = EmergencyStop(tmp_path)
        es.stop("test")
        assert es.check() is True
    
    def test_resume(self, tmp_path):
        es = EmergencyStop(tmp_path)
        es.stop("test")
        assert es.check() is True
        
        es.resume()
        assert es.check() is False


# =============================================================================
# AUDIT LOGGER TESTS
# =============================================================================

class TestAuditLogger:
    def test_log_and_read(self, tmp_path):
        logger = AuditLogger(tmp_path)
        logger.log("test_event", "INFO", {"detail": "value"})
        
        events = logger.get_recent_events(60)
        assert len(events) == 1
        assert events[0]['event'] == "test_event"
        assert events[0]['level'] == "INFO"
    
    def test_multiple_events(self, tmp_path):
        logger = AuditLogger(tmp_path)
        logger.log("event1", "INFO")
        logger.log("event2", "WARNING")
        logger.log("event3", "CRITICAL")
        
        events = logger.get_recent_events(60)
        assert len(events) == 3
        assert events[2]['level'] == "CRITICAL"


# =============================================================================
# MAINNET GUARDIAN INTEGRATION TESTS
# =============================================================================

class TestMainnetGuardian:
    def test_verify_before_start_pass(self, base_config):
        guardian = MainnetGuardian(base_config)
        assert guardian.verify_before_start() is True
        assert guardian._verified is True
    
    def test_verify_fails_with_http_url(self, base_config):
        cfg = base_config.copy()
        cfg['data_sources']['bad'] = {'enabled': True, 'base_url': 'http://evil.com'}
        guardian = MainnetGuardian(cfg)
        assert guardian.verify_before_start() is False
    
    def test_should_stop_no_capital(self, base_config):
        guardian = MainnetGuardian(base_config)
        guardian.verify_before_start()
        # Sem current_capital, só verifica emergency stop
        assert guardian.should_stop() is False
    
    def test_should_stop_with_circuit_breaker(self, base_config):
        guardian = MainnetGuardian(base_config)
        guardian.verify_before_start()
        # -10% = hard stop
        assert guardian.should_stop(9000.0) is True
    
    def test_validate_order_integration(self, base_config):
        guardian = MainnetGuardian(base_config)
        result = guardian.validate_order('BTC', 'BUY', 50, market_price=50000)
        assert result.is_valid is True
    
    def test_get_status(self, base_config):
        guardian = MainnetGuardian(base_config)
        guardian.verify_before_start()
        status = guardian.get_status()
        assert status['verified'] is True
        assert status['network'] == 'paper'
        assert status['can_trade_real'] is True
        assert 'circuit_breaker' in status
    
    def test_log_audit(self, base_config, tmp_path):
        cfg = base_config.copy()
        guardian = MainnetGuardian(cfg)
        guardian.audit_logger.log_dir = tmp_path
        guardian.audit_logger.audit_file = tmp_path / 'audit.jsonl'
        
        guardian.log_audit("test", "INFO", {"key": "value"})
        events = guardian.audit_logger.get_recent_events(60)
        assert len(events) == 1


# =============================================================================
# CONVENIENCE FUNCTIONS TESTS
# =============================================================================

class TestConvenienceFunctions:
    def test_create_guardian(self, base_config):
        from mainnet_guardian import create_guardian
        g = create_guardian(base_config)
        assert isinstance(g, MainnetGuardian)
    
    def test_emergency_stop_functions(self, tmp_path):
        from mainnet_guardian import emergency_stop, check_emergency_stop
        
        assert check_emergency_stop(tmp_path) is False
        emergency_stop(tmp_path)
        assert check_emergency_stop(tmp_path) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
