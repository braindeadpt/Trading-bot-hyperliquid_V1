"""Shared pytest fixtures for the trading bot test suite."""
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import time

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def mock_config():
    """Standard mock config for all tests."""
    return {
        'bot': {
            'name': 'Test Bot',
            'version': '0.1.0',
            'paper_trading': True,
        },
        'assets': ['BTC', 'ETH'],
        'timeframes': {
            'primary': '15m',
            'secondary': '5m',
        },
        'data_sources': {
            'binance': {
                'enabled': True,
                'base_url': 'https://fapi.binance.com',
                'weight': 0.4,
            },
            'bybit': {
                'enabled': True,
                'base_url': 'https://api.bybit.com',
                'weight': 0.3,
            },
            'okx': {
                'enabled': True,
                'base_url': 'https://www.okx.com',
                'weight': 0.2,
            },
            'hyperliquid': {
                'enabled': True,
                'base_url': 'https://api.hyperliquid.xyz',
                'weight': 0.1,
            },
        },
        'strategy': {
            'volume_spike_threshold': 2.5,
            'oi_change_threshold': 0.015,
            'max_funding_rate': 0.01,
            'min_funding_rate': -0.01,
            'volume_lookback': 100,
            'price_sma_period': 100,
            'min_bullish_candles': 1,
            'min_bearish_candles': 2,
            'short_enabled': True,
            'short_volume_threshold': 4.0,
            'short_min_bearish_candles': 2,
            'market_regime_enabled': True,
        },
        'risk': {
            'max_position_size_usd': 100,
            'max_leverage': 2,
            'stop_loss_pct': 0.02,
            'short_stop_loss_pct': 0.025,
            'trailing_activation_pct': 0.015,
            'trailing_stop_pct': 0.015,
            'max_daily_trades': 5,
            'initial_capital': 10000.0,
        },
        'polling': {
            'oi_interval': 30,
            'price_interval': 5,
        },
        'logging': {
            'level': 'INFO',
            'file': 'logs/test_bot.log',
        },
    }


@pytest.fixture
def mock_hyperliquid_allmids_response():
    """Mock response for Hyperliquid allMids endpoint."""
    return {
        'BTC': 85432.50,
        'ETH': 4521.30,
        'SOL': 198.45,
    }


@pytest.fixture
def mock_hyperliquid_meta_ctxs_response():
    """Mock response for Hyperliquid metaAndAssetCtxs endpoint."""
    return [
        {
            'universe': [
                {'name': 'BTC', 'maxLeverage': 50},
                {'name': 'ETH', 'maxLeverage': 50},
                {'name': 'SOL', 'maxLeverage': 40},
            ]
        },
        [
            {'midPx': '85432.50', 'markPx': '85430.00', 'oraclePx': '85435.00'},
            {'midPx': '4521.30', 'markPx': '4520.00', 'oraclePx': '4525.00'},
            {'midPx': '198.45', 'markPx': '198.40', 'oraclePx': '198.50'},
        ]
    ]


@pytest.fixture
def mock_binance_oi_response():
    """Mock Binance openInterest response."""
    return {'openInterest': '15000.5', 'symbol': 'BTCUSDT'}


@pytest.fixture
def mock_binance_price_response():
    """Mock Binance premiumIndex (mark price) response."""
    return {'markPrice': '85432.50', 'symbol': 'BTCUSDT'}


@pytest.fixture
def mock_binance_funding_response():
    """Mock Binance fundingRate response."""
    return [{'fundingRate': '0.0001', 'symbol': 'BTCUSDT', 'fundingTime': 1234567890000}]


@pytest.fixture
def mock_binance_ticker_response():
    """Mock Binance 24hr ticker response."""
    return {'volume': '50000.0', 'symbol': 'BTCUSDT', 'quoteVolume': '4271625000'}


@pytest.fixture
def mock_bybit_ticker_response():
    """Mock Bybit tickers response."""
    return {
        'retCode': 0,
        'result': {
            'list': [
                {
                    'symbol': 'BTCUSDT',
                    'lastPrice': '85432.50',
                    'openInterest': '12000.0',
                    'fundingRate': '0.0001',
                    'volume24h': '45000.0',
                }
            ]
        }
    }


@pytest.fixture
def mock_okx_oi_response():
    """Mock OKX open-interest response."""
    return {'code': '0', 'data': [{'oi': '13000.0', 'instId': 'BTC-USDT-SWAP'}]}


@pytest.fixture
def mock_okx_mark_price_response():
    """Mock OKX mark-price response."""
    return {'code': '0', 'data': [{'markPx': '85432.50', 'instId': 'BTC-USDT-SWAP'}]}


@pytest.fixture
def mock_okx_funding_response():
    """Mock OKX funding-rate response."""
    return {'code': '0', 'data': [{'fundingRate': '0.0001', 'instId': 'BTC-USDT-SWAP'}]}


@pytest.fixture
def mock_okx_ticker_response():
    """Mock OKX tickers response."""
    return {'code': '0', 'data': [{'volCcy24h': '48000.0', 'instId': 'BTC-USDT-SWAP'}]}


@pytest.fixture
def mock_aggregated_data():
    """Mock fully aggregated data dict as returned by fetch_all_data."""
    return {
        'oi_total': 2_500_000_000,
        'oi_change_pct': 0.02,
        'volume_total': 150_000_000,
        'funding_avg': 0.0005,
        'exchanges_data': {
            'binance': {
                'oi_usd': 1_200_000_000,
                'funding_rate': 0.0004,
                'volume_24h': 80_000_000,
                'mark_price': 85432.50,
            },
            'bybit': {
                'oi_usd': 800_000_000,
                'funding_rate': 0.0006,
                'volume_24h': 50_000_000,
                'mark_price': 85431.00,
            },
            'hyperliquid': {
                'oi_usd': 0,
                'funding_rate': None,
                'volume_24h': 0,
                'mark_price': 85432.50,
            },
        },
        'timestamp': time.time(),
    }


@pytest.fixture
def sample_candles():
    """Generate a list of sample candles for testing."""
    candles = []
    base_price = 85000
    for i in range(250):
        price = base_price + (i * 10) + (i % 7 - 3) * 50  # Slight upward trend with noise
        candle = {
            'timestamp': 1700000000000 + i * 900000,
            'open': price - 20,
            'high': price + 30,
            'low': price - 40,
            'close': price,
            'volume': 1000000 + (i % 5) * 500000,
            'oi': 1000000000 + i * 1000000,
            'funding': 0.0001,
            'oi_change': 0.001,
        }
        candles.append(candle)
    return candles


@pytest.fixture
def bearish_candles():
    """Generate bearish candles for regime detection testing."""
    candles = []
    base_price = 90000
    for i in range(250):
        price = base_price - (i * 15) + (i % 7 - 3) * 30  # Downward trend
        candle = {
            'timestamp': 1700000000000 + i * 900000,
            'open': price + 20,
            'high': price + 40,
            'low': price - 30,
            'close': price,
            'volume': 1000000 + (i % 5) * 500000,
            'oi': 1000000000 - i * 500000,
            'funding': 0.0001,
            'oi_change': -0.001,
        }
        candles.append(candle)
    return candles


@pytest.fixture
def ranging_candles():
    """Generate ranging candles for regime detection testing."""
    candles = []
    for i in range(250):
        price = 85000 + (i % 20 - 10) * 100  # Oscillates around 85000
        candle = {
            'timestamp': 1700000000000 + i * 900000,
            'open': price - 15,
            'high': price + 25,
            'low': price - 35,
            'close': price,
            'volume': 1000000 + (i % 5) * 200000,
            'oi': 1000000000,
            'funding': 0.0001,
            'oi_change': 0.0,
        }
        candles.append(candle)
    return candles
