"""
Tests for Volume Profile module (Fase A — Roadmap Evolution)
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from volume_profile import VolumeProfile


class TestVolumeProfileCalculate:
    """Testes do cálculo de Volume Profile."""

    def test_calculate_basic(self):
        vp = VolumeProfile(lookback=10)
        candles = []
        base = 50000
        for i in range(20):
            p = base + i * 100
            candles.append({
                'high': p + 50,
                'low': p - 50,
                'close': p,
                'volume': 1000 + i * 100
            })

        result = vp.calculate(candles)
        assert result is not None
        assert 'poc' in result
        assert 'vwap' in result
        assert 'vah' in result
        assert 'val' in result
        assert 'range_pct' in result

        # VAH > VAL
        assert result['vah'] > result['val']
        # VWAP dentro do range
        assert result['val'] <= result['vwap'] <= result['vah']

    def test_calculate_insufficient_data(self):
        vp = VolumeProfile(lookback=96)
        candles = [{'high': 50000, 'low': 49900, 'close': 50000, 'volume': 100} for _ in range(5)]
        result = vp.calculate(candles)
        assert result is None

    def test_calculate_all_same_price(self):
        vp = VolumeProfile(lookback=10)
        candles = [{'high': 50000, 'low': 50000, 'close': 50000, 'volume': 1000} for _ in range(25)]
        result = vp.calculate(candles)
        assert result is not None
        assert result['poc'] == 50000
        assert result['vwap'] == 50000
        assert result['std'] == 0
        assert result['vah'] == 50000
        assert result['val'] == 50000
        assert result['range_pct'] == 0

    def test_calculate_high_volume_at_poc(self):
        vp = VolumeProfile(lookback=10)
        candles = []
        for i in range(20):
            vol = 10000 if i == 15 else 100
            candles.append({
                'high': 50000 + i * 10,
                'low': 50000 + i * 10 - 5,
                'close': 50000 + i * 10,
                'volume': vol
            })
        result = vp.calculate(candles)
        # POC deve estar perto do preço com maior volume
        assert abs(result['poc'] - (50000 + 15 * 10)) < 100


class TestVolumeProfileFilter:
    """Testes do filtro de entrada."""

    def test_filter_long_below_val(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=48000, side='long', profile=profile)
        assert allowed is True
        assert "Below VAL" in reason

    def test_filter_long_above_vah(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=52000, side='long', profile=profile)
        assert allowed is True
        assert "Above VAH" in reason

    def test_filter_long_inside_va_blocked(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=50000, side='long', profile=profile)
        assert allowed is False
        assert "Inside VA" in reason

    def test_filter_short_above_vah(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=52000, side='short', profile=profile)
        assert allowed is True
        assert "Above VAH" in reason

    def test_filter_short_below_val(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=48000, side='short', profile=profile)
        assert allowed is True
        assert "Below VAL" in reason

    def test_filter_short_inside_va_blocked(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0}
        allowed, reason = vp.filter_entry(price=50000, side='short', profile=profile)
        assert allowed is False
        assert "Inside VA" in reason

    def test_filter_tight_va_blocked(self):
        vp = VolumeProfile(lookback=10)
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 50025, 'val': 49975, 'std': 25, 'range_pct': 0.1}
        allowed, reason = vp.filter_entry(price=48000, side='long', profile=profile)
        assert allowed is False
        assert "too tight" in reason

    def test_filter_no_profile(self):
        vp = VolumeProfile(lookback=10)
        allowed, reason = vp.filter_entry(price=50000, side='long', profile=None)
        assert allowed is True
        assert "No VP" in reason


class TestVolumeProfileFormat:
    """Testes de formatação."""

    def test_format_with_data(self):
        vp = VolumeProfile()
        profile = {'poc': 50000, 'vwap': 50000, 'vah': 51000, 'val': 49000, 'std': 1000, 'range_pct': 4.0, 'lookback': 96}
        s = vp.format_profile(profile)
        assert "POC=$50,000" in s
        assert "VA=[$49,000-$51,000]" in s
        assert "(4.00%)" in s

    def test_format_none(self):
        vp = VolumeProfile()
        s = vp.format_profile(None)
        assert s == "VP: N/A"
