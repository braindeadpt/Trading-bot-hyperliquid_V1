"""Tests for the per-symbol funding interval normalization (v3.1.21)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.exchanges.funding_normalize import (
    DEFAULT_CEX_FUNDING_INTERVAL_HOURS,
    DEFAULT_HL_FUNDING_INTERVAL_HOURS,
    EXCHANGE_FUNDING_INTERVAL_HOURS,
    SYMBOL_FUNDING_INTERVAL_HOURS,
    TARGET_FUNDING_INTERVAL_HOURS,
    interval_for,
    normalize_funding_for,
    normalize_funding_to_8h,
    parse_optional_rate,
    register_symbol_interval,
)


FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── interval_for — per-symbol overrides ───────────────────────────


def test_interval_for_hl_default_1h() -> None:
    _pass("interval_for_hl_default_1h",
          interval_for("hyperliquid", "BTC") == 1.0
          and interval_for("hl", "BTC") == 1.0)


def test_interval_for_cex_default_8h() -> None:
    _pass("interval_for_cex_default_8h",
          interval_for("bybit", "BTC") == 8.0
          and interval_for("okx", "BTC") == 8.0)


def test_interval_for_binance_btc_is_4h() -> None:
    _pass("interval_for_binance_btc_is_4h", interval_for("binance", "BTC") == 4.0)


def test_interval_for_binance_eth_is_4h() -> None:
    _pass("interval_for_binance_eth_is_4h", interval_for("binance", "ETH") == 4.0)


def test_interval_for_binance_unknown_symbol_falls_back_to_8h() -> None:
    """A symbol not in the per-symbol map falls back to the
    exchange-level default (Binance = 8h)."""
    _pass("interval_for_binance_unknown_symbol_falls_back_to_8h",
          interval_for("binance", "XYZ") == 8.0)


def test_interval_for_unknown_exchange_uses_cex_default() -> None:
    _pass("interval_for_unknown_exchange_uses_cex_default",
          interval_for("uniswap", "BTC") == DEFAULT_CEX_FUNDING_INTERVAL_HOURS)


def test_interval_for_uppercases_symbol() -> None:
    _pass("interval_for_uppercases_symbol", interval_for("binance", "btc") == 4.0)


# ── register_symbol_interval ────────────────────────────────────────


def test_register_symbol_interval_overrides_default() -> None:
    register_symbol_interval("bybit", "BTC", 2.0)
    try:
        _pass("register_symbol_interval_overrides_default",
              interval_for("bybit", "BTC") == 2.0)
    finally:
        # Clean up
        SYMBOL_FUNDING_INTERVAL_HOURS.pop(("bybit", "BTC"), None)


def test_register_symbol_interval_ignores_nonpositive() -> None:
    register_symbol_interval("bybit", "ETH", -1.0)
    register_symbol_interval("bybit", "SOL", 0.0)
    _pass("register_symbol_interval_ignores_nonpositive",
          ("bybit", "ETH") not in SYMBOL_FUNDING_INTERVAL_HOURS
          and ("bybit", "SOL") not in SYMBOL_FUNDING_INTERVAL_HOURS)


# ── normalize_funding_for ────────────────────────────────────────────


def test_normalize_funding_for_binance_4h() -> None:
    """A 4h funding rate × 2 = 8h equivalent rate."""
    rate_4h = 0.0001
    expected_8h = rate_4h * (8.0 / 4.0)  # 0.0002
    actual = normalize_funding_for(rate_4h, "binance", "BTC")
    _pass("normalize_funding_for_binance_4h", abs(actual - expected_8h) < 1e-9)


def test_normalize_funding_for_hl_1h() -> None:
    """A 1h HL funding rate × 8 = 8h equivalent rate."""
    rate_1h = 0.0001
    actual = normalize_funding_for(rate_1h, "hyperliquid", "BTC")
    _pass("normalize_funding_for_hl_1h", abs(actual - 0.0008) < 1e-9)


def test_normalize_funding_for_8h_passthrough() -> None:
    """An 8h rate on a default-8h venue stays the same."""
    _pass("normalize_funding_for_8h_passthrough",
          abs(normalize_funding_for(0.0001, "bybit", "BTC") - 0.0001) < 1e-9)


# ── Backwards compat with the old API ───────────────────────────────


def test_normalize_funding_to_8h_passthrough() -> None:
    _pass("normalize_funding_to_8h_passthrough",
          abs(normalize_funding_to_8h(0.0001, 8.0) - 0.0001) < 1e-9)


def test_normalize_funding_to_8h_scales_correctly() -> None:
    _pass("normalize_funding_to_8h_scales_correctly",
          abs(normalize_funding_to_8h(0.0001, 4.0) - 0.0002) < 1e-9)


def test_normalize_funding_to_8h_invalid_interval_uses_default() -> None:
    """An interval <= 0 falls back to DEFAULT_CEX_FUNDING_INTERVAL_HOURS."""
    _pass("normalize_funding_to_8h_invalid_interval_uses_default",
          abs(normalize_funding_to_8h(0.0001, 0.0) - 0.0001) < 1e-9)


# ── parse_optional_rate ────────────────────────────────────────────


def test_parse_optional_rate_valid() -> None:
    _pass("parse_optional_rate_valid", parse_optional_rate("0.0001") == 0.0001)


def test_parse_optional_rate_none() -> None:
    _pass("parse_optional_rate_none", parse_optional_rate(None) is None)


def test_parse_optional_rate_empty_string() -> None:
    _pass("parse_optional_rate_empty_string", parse_optional_rate("") is None)


def test_parse_optional_rate_garbage() -> None:
    _pass("parse_optional_rate_garbage", parse_optional_rate("nope") is None)


def test_parse_optional_rate_nan() -> None:
    import math
    result = parse_optional_rate(math.nan)
    _pass("parse_optional_rate_nan", result is None)


def test_parse_optional_rate_inf() -> None:
    import math
    result = parse_optional_rate(math.inf)
    _pass("parse_optional_rate_inf", result is None)


# ── Default constants present ─────────────────────────────────────


def test_default_constants_present() -> None:
    _pass("default_constants_present",
          DEFAULT_CEX_FUNDING_INTERVAL_HOURS == 8.0
          and DEFAULT_HL_FUNDING_INTERVAL_HOURS == 1.0
          and TARGET_FUNDING_INTERVAL_HOURS == 8.0
          and isinstance(EXCHANGE_FUNDING_INTERVAL_HOURS, dict))


def test_symbol_map_has_btc_eth_sol_binance() -> None:
    keys = {(ex, sym) for ex, sym in SYMBOL_FUNDING_INTERVAL_HOURS}
    _pass("symbol_map_has_btc_eth_sol_binance",
          ("binance", "BTC") in keys
          and ("binance", "ETH") in keys
          and ("binance", "SOL") in keys)


def main() -> int:
    print("=" * 70)
    print("Funding interval normalization tests (v3.1.21)")
    print("=" * 70)
    tests = [
        test_interval_for_hl_default_1h,
        test_interval_for_cex_default_8h,
        test_interval_for_binance_btc_is_4h,
        test_interval_for_binance_eth_is_4h,
        test_interval_for_binance_unknown_symbol_falls_back_to_8h,
        test_interval_for_unknown_exchange_uses_cex_default,
        test_interval_for_uppercases_symbol,
        test_register_symbol_interval_overrides_default,
        test_register_symbol_interval_ignores_nonpositive,
        test_normalize_funding_for_binance_4h,
        test_normalize_funding_for_hl_1h,
        test_normalize_funding_for_8h_passthrough,
        test_normalize_funding_to_8h_passthrough,
        test_normalize_funding_to_8h_scales_correctly,
        test_normalize_funding_to_8h_invalid_interval_uses_default,
        test_parse_optional_rate_valid,
        test_parse_optional_rate_none,
        test_parse_optional_rate_empty_string,
        test_parse_optional_rate_garbage,
        test_parse_optional_rate_nan,
        test_parse_optional_rate_inf,
        test_default_constants_present,
        test_symbol_map_has_btc_eth_sol_binance,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            _pass(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            _pass(t.__name__, False, f"{type(e).__name__}: {e}")
    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
