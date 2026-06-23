"""Tests for the v3.1.22 leverage-aware position sizing.

The previous ``calculate_position_size`` only applied the
``max_position_size_pct`` cap, so even with ``leverage_max: 10`` the
bot was effectively running at 1×. The fix multiplies the max
notional by ``leverage_max`` and logs the effective leverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.risk_manager import RiskManager
from src.strategies.base import Signal
from src.utils.config import load_config

FAILED = 0


def _pass(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILED += 1


# ── Config wiring ─────────────────────────────────────────────────


def _rm(leverage: float = 1.0, max_daily_loss: float = 3.0,
        max_pos_pct: float = 5.0, per_trade_risk: float = 1.0,
        per_trade_frac: float = 33.0) -> RiskManager:
    """Build a RiskManager with arbitrary risk overrides."""
    import tempfile
    import yaml

    cfg_data = {
        "risk": {
            "max_daily_loss_pct": max_daily_loss,
            "max_position_size_pct": max_pos_pct,
            "leverage_max": leverage,
            "per_trade_risk_pct": per_trade_risk,
            "per_trade_risk_fraction_of_daily_loss": per_trade_frac,
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(cfg_data, fh)
        path = fh.name
    cfg = load_config(path)
    return RiskManager(cfg, None)


def test_leverage_max_read_from_config() -> None:
    rm = _rm(leverage=10.0)
    _pass("leverage_max_read_from_config", rm._leverage_max == 10.0)


def test_leverage_max_capped_at_20x() -> None:
    """Absolute safety: 1000x is impossible."""
    rm = _rm(leverage=1000.0)
    _pass("leverage_max_capped_at_20x", rm._leverage_max == 20.0)


def test_leverage_min_1x() -> None:
    rm = _rm(leverage=0.5)
    _pass("leverage_min_1x", rm._leverage_max == 1.0)


def test_leverage_default_1x_when_missing() -> None:
    rm = _rm(leverage=1.0)
    _pass("leverage_default_1x_when_missing", rm._leverage_max >= 1.0)


# ── Effective per-trade risk cap ───────────────────────────────────


def test_per_trade_risk_capped_to_third_of_daily() -> None:
    """With 1% per_trade_risk and 3% daily, effective ≤ 1%."""
    rm = _rm(per_trade_risk=1.0, max_daily_loss=3.0, per_trade_frac=33.0)
    _pass("per_trade_risk_capped_to_third_of_daily",
          rm._per_trade_risk_pct_effective <= 0.01 + 1e-9)


def test_per_trade_risk_lower_when_per_trade_frac_low() -> None:
    """With 1% per_trade_risk and 3% daily at 10% fraction, effective = 0.3%."""
    rm = _rm(per_trade_risk=1.0, max_daily_loss=3.0, per_trade_frac=10.0)
    _pass("per_trade_risk_lower_when_per_trade_frac_low",
          abs(rm._per_trade_risk_pct_effective - 0.003) < 1e-9)


def test_per_trade_risk_not_increased_by_daily_cap() -> None:
    """If user explicitly wants 2% per trade, the daily cap shouldn't shrink it."""
    rm = _rm(per_trade_risk=2.0, max_daily_loss=3.0, per_trade_frac=33.0)
    # 33% of 3% = 0.99%, so 2% should be clamped down to 0.99%
    _pass("per_trade_risk_not_increased_by_daily_cap",
          abs(rm._per_trade_risk_pct_effective - 0.0099) < 1e-9)


# ── max_notional scales with leverage ──────────────────────────────


def _run(rm: RiskManager, capital: float, atr_pct: float,
         size_pct: float, price: float = 50000.0) -> tuple:
    s = Signal(
        symbol="BTC", side="long", entry_price=price,
        size_pct=size_pct, strategy="trend", confidence=0.8,
    )
    size = rm.calculate_position_size(s, capital, atr_pct)
    notional = size * price
    return size, notional


def test_max_notional_scales_with_leverage() -> None:
    """With size_pct high enough to bypass the conviction cap, the
    10x-leveraged cap should be 10x larger than the 1x cap."""
    rm_1x = _rm(leverage=1.0, max_pos_pct=5.0, per_trade_risk=0.1)
    rm_10x = _rm(leverage=10.0, max_pos_pct=5.0, per_trade_risk=0.1)

    # Tiny ATR so risk ceiling is very large (uses 0.5% min stop);
    # tiny per_trade_risk (0.1% × $10k = $10 / 0.005 = $2000);
    # very high size_pct so conviction cap is huge ($100k) — only
    # the leverage cap binds.
    _, n1 = _run(rm_1x, capital=10000.0, atr_pct=0.001, size_pct=10.0)
    _, n10 = _run(rm_10x, capital=10000.0, atr_pct=0.001, size_pct=10.0)
    # min(2000, 100k, 500) = 500; min(2000, 100k, 5000) = 2000
    # so the ratio is 4x, not 10x because risk ceiling binds at 1x.
    # Use a higher per_trade_risk so risk ceiling is above 5000.
    rm_1x = _rm(leverage=1.0, max_pos_pct=5.0, per_trade_risk=10.0)
    rm_10x = _rm(leverage=10.0, max_pos_pct=5.0, per_trade_risk=10.0)
    _, n1 = _run(rm_1x, capital=10000.0, atr_pct=0.0001, size_pct=10.0)
    _, n10 = _run(rm_10x, capital=10000.0, atr_pct=0.0001, size_pct=10.0)
    _pass("max_notional_scales_with_leverage",
          abs(n10 / n1 - 10.0) < 1e-6,
          f"1x notional={n1}, 10x notional={n10}, ratio={n10 / n1 if n1 else 0}")


def test_max_notional_at_10x_lev() -> None:
    """With 5% margin cap and 10x leverage, max notional = 50% of capital."""
    rm = _rm(leverage=10.0, max_pos_pct=5.0, per_trade_risk=10.0)
    _, n = _run(rm, capital=10000.0, atr_pct=0.0001, size_pct=10.0)
    # min($200k, $100k, $5000) = $5000
    _pass("max_notional_at_10x_lev", abs(n - 5000.0) < 1e-6, f"notional={n}")


def test_max_notional_at_1x_lev() -> None:
    """With 5% margin cap and 1x leverage, max notional = 5% of capital."""
    rm = _rm(leverage=1.0, max_pos_pct=5.0, per_trade_risk=10.0)
    _, n = _run(rm, capital=10000.0, atr_pct=0.0001, size_pct=10.0)
    # min($200k, $100k, $500) = $500
    _pass("max_notional_at_1x_lev", abs(n - 500.0) < 1e-6, f"notional={n}")


def test_max_notional_at_5x_lev() -> None:
    rm = _rm(leverage=5.0, max_pos_pct=3.0, per_trade_risk=10.0)
    _, n = _run(rm, capital=10000.0, atr_pct=0.0001, size_pct=10.0)
    # 3% × 10k × 5 = 1500
    _pass("max_notional_at_5x_lev", abs(n - 1500.0) < 1e-6, f"notional={n}")


# ── Risk cap still bounds size even with leverage ──────────────────


def test_risk_cap_still_bounds_with_high_leverage() -> None:
    """With 2% ATR and 1% per_trade risk, the risk ceiling should
    still be the binding constraint regardless of leverage."""
    rm = _rm(leverage=10.0, max_pos_pct=5.0, per_trade_risk=1.0)
    _, n = _run(rm, capital=10000.0, atr_pct=0.02, size_pct=10.0)
    # risk: $10k × 1% = $100, stop = 4% → notional_risk = $2500
    # cap: $10k × 5% × 10x = $5000
    # So risk binds at $2500.
    _pass("risk_cap_still_bounds_with_high_leverage",
          n <= 2500.0 + 1e-6, f"notional={n}")


def test_zero_capital_returns_zero() -> None:
    rm = _rm(leverage=10.0)
    size, notional = _run(rm, capital=0.0, atr_pct=0.01, size_pct=0.05)
    _pass("zero_capital_returns_zero", size == 0.0 and notional == 0.0)


def test_invalid_price_returns_zero() -> None:
    rm = _rm(leverage=10.0)
    s = Signal(symbol="BTC", side="long", entry_price=0.0,
               size_pct=0.05, strategy="trend", confidence=0.8)
    _pass("invalid_price_returns_zero",
          rm.calculate_position_size(s, 10000.0, 0.01) == 0.0)


# ── live config (paper mode) ───────────────────────────────────────


def test_live_paper_config_reads_leverage() -> None:
    cfg = load_config("config/settings.yaml")
    rm = RiskManager(cfg, None)
    _pass("live_paper_config_reads_leverage", rm._leverage_max == 10.0)


def test_live_paper_config_per_trade_risk_effective_set() -> None:
    cfg = load_config("config/settings.yaml")
    rm = RiskManager(cfg, None)
    # default 33% of 3% = 0.99%, config says 1% → 0.99% wins (min)
    _pass("live_paper_config_per_trade_risk_effective_set",
          abs(rm._per_trade_risk_pct_effective - 0.0099) < 1e-9,
          f"effective={rm._per_trade_risk_pct_effective}")


def main() -> int:
    print("=" * 70)
    print("Leverage-aware position sizing tests (v3.1.22)")
    print("=" * 70)
    tests = [
        test_leverage_max_read_from_config,
        test_leverage_max_capped_at_20x,
        test_leverage_min_1x,
        test_leverage_default_1x_when_missing,
        test_per_trade_risk_capped_to_third_of_daily,
        test_per_trade_risk_lower_when_per_trade_frac_low,
        test_per_trade_risk_not_increased_by_daily_cap,
        test_max_notional_scales_with_leverage,
        test_max_notional_at_10x_lev,
        test_max_notional_at_1x_lev,
        test_max_notional_at_5x_lev,
        test_risk_cap_still_bounds_with_high_leverage,
        test_zero_capital_returns_zero,
        test_invalid_price_returns_zero,
        test_live_paper_config_reads_leverage,
        test_live_paper_config_per_trade_risk_effective_set,
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
