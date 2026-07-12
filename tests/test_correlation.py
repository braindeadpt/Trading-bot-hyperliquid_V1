"""Test for CorrelationMonitor (max correlation between positions).

Verifies:
  - Rolling return accumulation
  - Pearson correlation calculation
  - would_violate() threshold logic
"""

import sys
sys.path.insert(0, r"C:\Users\Braindead\Documents\trading-bot-hyperliquid")

from src.core.correlation_monitor import CorrelationMonitor
import pytest

pytestmark = pytest.mark.unit


def test_correlation_monitor():
    print("=" * 60)
    print("TEST: CorrelationMonitor")
    print("=" * 60)

    mon = CorrelationMonitor(lookback=10)

    # Feed perfectly correlated returns for BTC and ETH
    for i in range(12):
        ret = 0.001 * (1 if i % 2 == 0 else -1)
        mon.add_return("BTC", ret)
        mon.add_return("ETH", ret)

    corr = mon.get_correlation("BTC", "ETH")
    print(f"Perfect correlation: BTC vs ETH r = {corr:.4f} (expect ~1.0)")
    assert corr is not None and corr > 0.99, f"Expected ~1.0, got {corr}"
    print("[PASS]")

    # Feed anti-correlated returns for BTC and SOL
    mon2 = CorrelationMonitor(lookback=10)
    for i in range(12):
        ret = 0.001 * (1 if i % 2 == 0 else -1)
        mon2.add_return("BTC", ret)
        mon2.add_return("SOL", -ret)

    corr2 = mon2.get_correlation("BTC", "SOL")
    print(f"Anti-correlation: BTC vs SOL r = {corr2:.4f} (expect ~-1.0)")
    assert corr2 is not None and corr2 < -0.99, f"Expected ~-1.0, got {corr2}"
    print("[PASS]")

    # would_violate() threshold
    violated, sym, val = mon.would_violate("ETH", ["BTC"], threshold=0.70)
    print(f"would_violate (threshold=0.70): violated={violated}, sym={sym}, r={val:.4f}")
    assert violated is True, "Should violate at 0.70 threshold"
    assert sym == "BTC"
    print("[PASS]")

    # would_violate() with strict threshold (corr=1.0 > 0.99 → violates)
    violated2, sym2, val2 = mon.would_violate("ETH", ["BTC"], threshold=0.99)
    print(f"would_violate (threshold=0.99): violated={violated2} (expect True, corr=1.0 > 0.99)")
    assert violated2 is True, "Should violate at 0.99 threshold (1.0 > 0.99)"
    print("[PASS]")

    # would_violate() with impossible threshold (no violation)
    violated3, sym3, val3 = mon.would_violate("ETH", ["BTC"], threshold=1.01)
    print(f"would_violate (threshold=1.01): violated={violated3} (expect False)")
    assert violated3 is False, "Should NOT violate at 1.01 threshold"
    print("[PASS]")

    # Not enough data
    mon3 = CorrelationMonitor(lookback=10)
    mon3.add_return("BTC", 0.001)
    corr3 = mon3.get_correlation("BTC", "ETH")
    print(f"No data for ETH: corr={corr3} (expect None)")
    assert corr3 is None
    print("[PASS]")

    print("\nALL CORRELATION TESTS PASSED")
    return True


if __name__ == "__main__":
    test_correlation_monitor()
