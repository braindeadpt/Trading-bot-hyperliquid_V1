"""Tests for data_aggregator.py — data fetching, parsing, validation, caching, fallback."""
import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from data_aggregator import DataAggregator, retry_on_failure


# =============================================================================
# 1. Retry decorator
# =============================================================================
class TestRetryOnFailure:
    def test_retry_succeeds_on_second_attempt(self, mock_config):
        """Retry decorator should succeed if the function succeeds within max retries."""
        call_count = 0

        @retry_on_failure(max_retries=3, backoff_seconds=0.01)
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("simulated failure")
            return "success"

        result = flaky_func()
        assert result == "success"
        assert call_count == 2

    def test_retry_exhausts_and_raises(self, mock_config):
        """Retry decorator should raise the last exception after all retries are exhausted."""
        @retry_on_failure(max_retries=2, backoff_seconds=0.01)
        def always_fails():
            raise ValueError("permanent failure")

        with pytest.raises(ValueError, match="permanent failure"):
            always_fails()

    def test_retry_no_backoff_on_first_success(self, mock_config):
        """Retry decorator should not sleep or retry if the function succeeds on the first call."""
        @retry_on_failure(max_retries=3, backoff_seconds=0.01)
        def always_ok():
            return "ok"

        start = time.time()
        result = always_ok()
        elapsed = time.time() - start
        assert result == "ok"
        assert elapsed < 0.05  # Should be nearly instant


# =============================================================================
# 2. Initialization & API validation
# =============================================================================
class TestDataAggregatorInit:
    def test_init_loads_config(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg.sources == mock_config['data_sources']
        assert agg.last_oi == {}
        assert agg._price_cache == {}

    def test_session_headers_set(self, mock_config):
        agg = DataAggregator(mock_config)
        assert 'User-Agent' in agg.session.headers
        assert 'Mozilla' in agg.session.headers['User-Agent']


class TestValidateAPI:
    def test_validate_binance_success(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            mock_get.return_value.status_code = 200
            assert agg.validate_api('binance') is True
            mock_get.assert_called_once_with(
                "https://fapi.binance.com/fapi/v1/ping", timeout=5
            )

    def test_validate_bybit_success(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            mock_get.return_value.json.return_value = {'retCode': 0}
            assert agg.validate_api('bybit') is True

    def test_validate_bybit_failure(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            mock_get.return_value.json.return_value = {'retCode': 10001}
            assert agg.validate_api('bybit') is False

    def test_validate_okx_success(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            mock_get.return_value.json.return_value = {'code': '0'}
            assert agg.validate_api('okx') is True

    def test_validate_hyperliquid_success(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = 'some long response with tokens'
            assert agg.validate_api('hyperliquid') is True

    def test_validate_hyperliquid_short_text_fails(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.text = 'ab'  # len < 10
            assert agg.validate_api('hyperliquid') is False

    def test_validate_unknown_source(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg.validate_api('unknown_exchange') is False

    def test_validate_exception_returns_false(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get', side_effect=requests.Timeout("timeout")):
            assert agg.validate_api('binance') is False


class TestTestAllAPIs:
    def test_test_all_apis_returns_dict(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg, 'validate_api', return_value=True):
            results = agg.test_all_apis()
        assert isinstance(results, dict)
        assert set(results.keys()) == set(mock_config['data_sources'].keys())
        assert all(v is True for v in results.values())

    def test_disabled_source_marked_false(self, mock_config):
        mock_config['data_sources']['binance']['enabled'] = False
        agg = DataAggregator(mock_config)
        results = agg.test_all_apis()
        assert results['binance'] is False


# =============================================================================
# 3. Safe JSON parsing
# =============================================================================
class TestSafeJson:
    def test_safe_json_success(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{"key": "value"}'
        resp.json.return_value = {'key': 'value'}
        result = agg._safe_json(resp, 'test_source')
        assert result == {'key': 'value'}

    def test_safe_json_non_200_status(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 503
        resp.text = 'Service Unavailable'
        assert agg._safe_json(resp, 'test_source') is None

    def test_safe_json_empty_response(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ''
        assert agg._safe_json(resp, 'test_source') is None

    def test_safe_json_whitespace_only(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '   '
        assert agg._safe_json(resp, 'test_source') is None

    def test_safe_json_html_response(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '<!DOCTYPE html><html>...</html>'
        assert agg._safe_json(resp, 'test_source') is None

    def test_safe_json_invalid_json(self, mock_config):
        agg = DataAggregator(mock_config)
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'not json at all'
        resp.json.side_effect = json.JSONDecodeError("Expecting value", "not json", 0)
        assert agg._safe_json(resp, 'test_source') is None


# =============================================================================
# 4. Price sanity checks
# =============================================================================
class TestPriceSanity:
    def test_btc_sane_price(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', 85000.0) is True
        assert agg._is_price_sane('BTC', 50000.0) is True

    def test_btc_insane_price_too_high(self, mock_config, caplog):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', 250000.0) is False
        assert "PREÇO INSANO" in caplog.text

    def test_btc_insane_price_too_low(self, mock_config, caplog):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('BTC', 5000.0) is False
        assert "PREÇO INSANO" in caplog.text

    def test_eth_sane_price(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('ETH', 3000.0) is True

    def test_eth_insane_price(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('ETH', 50000.0) is False

    def test_sol_sane_price(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('SOL', 150.0) is True

    def test_unknown_asset_uses_default_range(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg._is_price_sane('DOGE', 0.15) is True
        assert agg._is_price_sane('DOGE', 200000.0) is False

    def test_zero_price_not_sane(self, mock_config):
        agg = DataAggregator(mock_config)
        # Zero price should not log the insane message but still be false
        assert agg._is_price_sane('BTC', 0) is False


# =============================================================================
# 5. Hyperliquid fetching — allMids & metaAndAssetCtxs
# =============================================================================
class TestFetchHyperliquid:
    def test_fetch_hyperliquid_allmids_success(
        self, mock_config, mock_hyperliquid_allmids_response
    ):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_hyperliquid_allmids_response
            mock_post.return_value = resp

            result = agg._fetch_hyperliquid('BTC')
            assert result is not None
            assert result['mark_price'] == 85432.50
            assert result['oi_usd'] == 0

    def test_fetch_hyperliquid_meta_ctxs_fallback(
        self, mock_config, mock_hyperliquid_meta_ctxs_response
    ):
        agg = DataAggregator(mock_config)

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            payload = kwargs.get('json', {})
            if payload.get('type') == 'allMids':
                # allMids fails: asset not present
                resp.status_code = 200
                resp.json.return_value = {'ETH': 1234.0}  # BTC missing
            elif payload.get('type') == 'metaAndAssetCtxs':
                resp.status_code = 200
                resp.json.return_value = mock_hyperliquid_meta_ctxs_response
            else:
                resp.status_code = 200
                resp.json.return_value = {}
            return resp

        with patch.object(agg.session, 'post', side_effect=side_effect):
            result = agg._fetch_hyperliquid('BTC')
            assert result is not None
            assert result['mark_price'] == 85432.50

    def test_fetch_hyperliquid_uses_midpx_first(self, mock_config):
        """metaAndAssetCtxs should prefer midPx, then markPx, then oraclePx."""
        agg = DataAggregator(mock_config)

        meta_ctxs = [
            {'universe': [{'name': 'BTC', 'maxLeverage': 50}]},
            [{'midPx': '86000.0', 'markPx': '85000.0', 'oraclePx': '85500.0'}]
        ]

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            payload = kwargs.get('json', {})
            if payload.get('type') == 'allMids':
                resp.status_code = 200
                resp.json.return_value = {}  # empty
            elif payload.get('type') == 'metaAndAssetCtxs':
                resp.status_code = 200
                resp.json.return_value = meta_ctxs
            return resp

        with patch.object(agg.session, 'post', side_effect=side_effect):
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 86000.0  # midPx preferred

    def test_fetch_hyperliquid_fallback_to_markpx(self, mock_config):
        """If midPx is missing, should fallback to markPx."""
        agg = DataAggregator(mock_config)

        meta_ctxs = [
            {'universe': [{'name': 'BTC', 'maxLeverage': 50}]},
            [{'markPx': '85000.0', 'oraclePx': '85500.0'}]  # no midPx
        ]

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            payload = kwargs.get('json', {})
            if payload.get('type') == 'allMids':
                resp.status_code = 200
                resp.json.return_value = {}
            elif payload.get('type') == 'metaAndAssetCtxs':
                resp.status_code = 200
                resp.json.return_value = meta_ctxs
            return resp

        with patch.object(agg.session, 'post', side_effect=side_effect):
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85000.0

    def test_fetch_hyperliquid_insane_price_ignored(self, mock_config):
        """If allMids returns an insane price, it should fall through to metaAndAssetCtxs."""
        agg = DataAggregator(mock_config)

        def side_effect(*args, **kwargs):
            resp = MagicMock()
            payload = kwargs.get('json', {})
            if payload.get('type') == 'allMids':
                resp.status_code = 200
                resp.json.return_value = {'BTC': 999999.0}  # Insane
            elif payload.get('type') == 'metaAndAssetCtxs':
                resp.status_code = 200
                resp.json.return_value = [
                    {'universe': [{'name': 'BTC'}]},
                    [{'midPx': '85000.0'}]
                ]
            return resp

        with patch.object(agg.session, 'post', side_effect=side_effect):
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85000.0

    def test_fetch_hyperliquid_all_methods_fail(self, mock_config):
        """If both APIs fail and no cache, should return mark_price=0."""
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            mock_post.return_value.status_code = 503
            mock_post.return_value.text = 'Service Unavailable'
            result = agg._fetch_hyperliquid('BTC')
            assert result is not None
            assert result['mark_price'] == 0

    def test_fetch_hyperliquid_uses_cache_fallback(self, mock_config):
        """If APIs fail but cache is fresh and sane, use cache."""
        agg = DataAggregator(mock_config)
        agg._price_cache['BTC'] = 85000.0
        agg._cache_timestamp = time.time()

        with patch.object(agg.session, 'post') as mock_post:
            mock_post.return_value.status_code = 503
            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85000.0


# =============================================================================
# 6. Cache logic
# =============================================================================
class TestCache:
    def test_get_cached_price_fresh(self, mock_config):
        agg = DataAggregator(mock_config)
        agg._price_cache['BTC'] = 85000.0
        agg._cache_timestamp = time.time()
        assert agg.get_cached_price('BTC', max_age_seconds=300) == 85000.0

    def test_get_cached_price_expired(self, mock_config):
        agg = DataAggregator(mock_config)
        agg._price_cache['BTC'] = 85000.0
        agg._cache_timestamp = time.time() - 400  # 400s ago
        assert agg.get_cached_price('BTC', max_age_seconds=300) == 0

    def test_get_cached_price_missing_asset(self, mock_config):
        agg = DataAggregator(mock_config)
        assert agg.get_cached_price('BTC', max_age_seconds=300) == 0

    def test_fetch_updates_cache(self, mock_config, mock_hyperliquid_allmids_response):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'post') as mock_post:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_hyperliquid_allmids_response
            mock_post.return_value = resp

            result = agg._fetch_hyperliquid('BTC')
            assert result['mark_price'] == 85432.50
            assert agg._price_cache['BTC'] == 85432.50
            assert abs(agg._cache_timestamp - time.time()) < 5


# =============================================================================
# 7. fetch_all_data aggregation
# =============================================================================
class TestFetchAllData:
    def test_fetch_all_data_aggregates_sources(
        self, mock_config,
        mock_binance_oi_response, mock_binance_price_response,
        mock_binance_funding_response, mock_binance_ticker_response,
    ):
        agg = DataAggregator(mock_config)
        # Disable everything except binance to simplify
        for name in mock_config['data_sources']:
            mock_config['data_sources'][name]['enabled'] = (name == 'binance')

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'openInterest' in url:
                resp.json.return_value = mock_binance_oi_response
            elif 'premiumIndex' in url:
                resp.json.return_value = mock_binance_price_response
            elif 'fundingRate' in url:
                resp.json.return_value = mock_binance_funding_response
            elif 'ticker/24hr' in url:
                resp.json.return_value = mock_binance_ticker_response
            else:
                resp.json.return_value = {}
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg.fetch_all_data('BTC')
            assert result is not None
            assert result['oi_total'] > 0
            assert result['volume_total'] > 0
            assert 'binance' in result['exchanges_data']

    def test_fetch_all_data_no_valid_sources(self, mock_config):
        agg = DataAggregator(mock_config)
        # Disable all sources
        for name in mock_config['data_sources']:
            mock_config['data_sources'][name]['enabled'] = False

        result = agg.fetch_all_data('BTC')
        assert result is None

    def test_fetch_all_data_oi_change_calculated(self, mock_config):
        agg = DataAggregator(mock_config)
        # Set previous OI
        agg.last_oi = {'binance': 1_000_000_000}

        for name in mock_config['data_sources']:
            mock_config['data_sources'][name]['enabled'] = (name == 'binance')

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'openInterest' in url:
                resp.json.return_value = {'openInterest': '15000.5'}
            elif 'premiumIndex' in url:
                resp.json.return_value = {'markPrice': '80000.0'}
            elif 'fundingRate' in url:
                resp.json.return_value = [{'fundingRate': '0.0001'}]
            elif 'ticker/24hr' in url:
                resp.json.return_value = {'volume': '50000.0'}
            else:
                resp.json.return_value = {}
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg.fetch_all_data('BTC')
            assert result is not None
            expected_oi = float('15000.5') * 80000.0  # 1.2B
            expected_change = (expected_oi - 1_000_000_000) / 1_000_000_000
            assert abs(result['oi_change_pct'] - expected_change) < 0.001

    def test_fetch_all_data_funding_average(self, mock_config):
        agg = DataAggregator(mock_config)
        # Enable binance and bybit
        for name in mock_config['data_sources']:
            mock_config['data_sources'][name]['enabled'] = (name in ('binance', 'bybit'))

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'fapi.binance.com' in url:
                if 'fundingRate' in url:
                    resp.json.return_value = [{'fundingRate': '0.0002'}]
                elif 'openInterest' in url:
                    resp.json.return_value = {'openInterest': '1000'}
                elif 'premiumIndex' in url:
                    resp.json.return_value = {'markPrice': '80000'}
                elif 'ticker' in url:
                    resp.json.return_value = {'volume': '1000'}
            elif 'api.bybit.com' in url:
                resp.json.return_value = {
                    'retCode': 0,
                    'result': {
                        'list': [
                            {'symbol': 'BTCUSDT', 'lastPrice': '80000', 'openInterest': '1000',
                             'fundingRate': '0.0004', 'volume24h': '1000'}
                        ]
                    }
                }
            else:
                resp.json.return_value = {}
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg.fetch_all_data('BTC')
            assert result is not None
            assert result['funding_avg'] == pytest.approx(0.0003, abs=0.00001)

    def test_fetch_all_data_price_cache_populated(self, mock_config):
        agg = DataAggregator(mock_config)
        for name in mock_config['data_sources']:
            mock_config['data_sources'][name]['enabled'] = (name == 'binance')

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'openInterest' in url:
                resp.json.return_value = {'openInterest': '1000'}
            elif 'premiumIndex' in url:
                resp.json.return_value = {'markPrice': '85000.0'}
            elif 'fundingRate' in url:
                resp.json.return_value = [{'fundingRate': '0.0001'}]
            elif 'ticker' in url:
                resp.json.return_value = {'volume': '1000'}
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg.fetch_all_data('BTC')
            assert result is not None
            assert agg.get_cached_price('BTC', max_age_seconds=300) == 85000.0


# =============================================================================
# 8. Individual exchange fetchers
# =============================================================================
class TestFetchBinance:
    def test_fetch_binance_success(
        self, mock_config,
        mock_binance_oi_response, mock_binance_price_response,
        mock_binance_funding_response, mock_binance_ticker_response
    ):
        agg = DataAggregator(mock_config)

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if 'openInterest' in url:
                resp.json.return_value = mock_binance_oi_response
            elif 'premiumIndex' in url:
                resp.json.return_value = mock_binance_price_response
            elif 'fundingRate' in url:
                resp.json.return_value = mock_binance_funding_response
            elif 'ticker/24hr' in url:
                resp.json.return_value = mock_binance_ticker_response
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg._fetch_binance('BTC')
            assert result is not None
            assert result['mark_price'] == 85432.50
            assert result['oi_usd'] == 15000.5 * 85432.50
            assert result['funding_rate'] == 0.0001

    def test_fetch_binance_bad_oi_response(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}  # No openInterest key
            mock_get.return_value = resp
            result = agg._fetch_binance('BTC')
            assert result is None  # First call (OI) returns empty -> safe_json ok but no key -> None path

    def test_fetch_binance_http_error(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            resp = MagicMock()
            resp.status_code = 503
            resp.text = 'Service Unavailable'
            mock_get.return_value = resp
            result = agg._fetch_binance('BTC')
            assert result is None


class TestFetchBybit:
    def test_fetch_bybit_success(self, mock_config, mock_bybit_ticker_response):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_bybit_ticker_response
            mock_get.return_value = resp
            result = agg._fetch_bybit('BTC')
            assert result is not None
            assert result['mark_price'] == 85432.50
            assert result['funding_rate'] == 0.0001
            assert result['volume_24h'] == 45000.0

    def test_fetch_bybit_retcode_not_zero(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {'retCode': 10001, 'retMsg': 'error'}
            mock_get.return_value = resp
            result = agg._fetch_bybit('BTC')
            assert result is None


class TestFetchOkx:
    def test_fetch_okx_success(
        self, mock_config,
        mock_okx_oi_response, mock_okx_mark_price_response,
        mock_okx_funding_response, mock_okx_ticker_response
    ):
        agg = DataAggregator(mock_config)

        call_map = {
            'open-interest': mock_okx_oi_response,
            'mark-price': mock_okx_mark_price_response,
            'funding-rate': mock_okx_funding_response,
            'tickers': mock_okx_ticker_response,
        }

        def mock_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            for key, val in call_map.items():
                if key in url:
                    resp.json.return_value = val
                    break
            else:
                resp.json.return_value = {}
            return resp

        with patch.object(agg.session, 'get', side_effect=mock_get):
            result = agg._fetch_okx('BTC')
            assert result is not None
            assert result['mark_price'] == 85432.50
            assert result['oi_usd'] == 13000.0 * 85432.50
            assert result['funding_rate'] == 0.0001
            assert result['volume_24h'] == 48000.0

    def test_fetch_okx_code_not_zero(self, mock_config):
        agg = DataAggregator(mock_config)
        with patch.object(agg.session, 'get') as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {'code': '1', 'msg': 'error'}
            mock_get.return_value = resp
            result = agg._fetch_okx('BTC')
            assert result is None


# =============================================================================
# 9. Fallback price
# =============================================================================
class TestFallbackPrice:
    def test_fallback_price_uses_cache(self, mock_config):
        agg = DataAggregator(mock_config)
        agg._price_cache['BTC'] = 85000.0
        agg._cache_timestamp = time.time()
        result = agg._fallback_price('BTC')
        assert result['mark_price'] == 85000.0

    def test_fallback_price_cache_expired(self, mock_config):
        agg = DataAggregator(mock_config)
        agg._price_cache['BTC'] = 85000.0
        agg._cache_timestamp = time.time() - 300  # older than 120s max_age in fallback
        result = agg._fallback_price('BTC')
        assert result['mark_price'] == 0

    def test_fallback_price_no_cache(self, mock_config):
        agg = DataAggregator(mock_config)
        result = agg._fallback_price('BTC')
        assert result['mark_price'] == 0
        assert result['oi_usd'] == 0
