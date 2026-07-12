"""Unit tests for the DataBus per-topic rate-limit override (QW4).

Run:  python tests/test_databus_per_topic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.exchanges.hyperliquid_ws import DataBus  # noqa: E402
import pytest

pytestmark = pytest.mark.unit

FAILED = 0


def print_test(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}{(' - ' + detail) if detail else ''}")
    global FAILED
    if not ok:
        FAILED += 1


def test_default_limit_used_when_no_override() -> None:
    bus = DataBus(rate_limit_hz=200)
    ok = bus._limit_for("trade:BTC") == 200
    print_test("default_limit_used_when_no_override", ok)


def test_per_topic_override_applied() -> None:
    bus = DataBus(rate_limit_hz=200, topic_rate_limits={"trade:": 2000})
    ok = bus._limit_for("trade:BTC") == 2000
    print_test("per_topic_override_applied", ok, f"got {bus._limit_for('trade:BTC')}")


def test_per_topic_does_not_affect_other_topics() -> None:
    bus = DataBus(rate_limit_hz=200, topic_rate_limits={"trade:": 2000})
    ok = bus._limit_for("price:BTC") == 200 and bus._limit_for("orderbook:BTC") == 200
    print_test("per_topic_does_not_affect_other_topics", ok)


def test_most_specific_prefix_wins() -> None:
    bus = DataBus(
        rate_limit_hz=200,
        topic_rate_limits={"trade:": 1000, "trade:BTC": 5000},
    )
    ok_btc = bus._limit_for("trade:BTC") == 5000
    ok_eth = bus._limit_for("trade:ETH") == 1000
    print_test("most_specific_prefix_wins", ok_btc and ok_eth,
               f"BTC={bus._limit_for('trade:BTC')} ETH={bus._limit_for('trade:ETH')}")


def test_trade_topic_partial_drop_at_higher_cap() -> None:
    """With trade_rate_limit_hz=2000, sending 2500 should drop ~500, not 2300."""
    bus = DataBus(rate_limit_hz=200, topic_rate_limits={"trade:": 2000})
    for _ in range(2500):
        bus.publish("trade:BTC", None)
    drops = bus._dropped_total.get("trade:BTC", 0)
    ok = 400 <= drops <= 600
    print_test("trade_topic_partial_drop_at_higher_cap", ok, f"sent 2500, dropped {drops}")


def test_trade_topic_drops_most_at_default_cap() -> None:
    """Without override, trade:BTC hits the 200 cap and drops most sends."""
    bus = DataBus(rate_limit_hz=200)
    for _ in range(2500):
        bus.publish("trade:BTC", None)
    drops = bus._dropped_total.get("trade:BTC", 0)
    ok = drops >= 2200
    print_test("trade_topic_drops_most_at_default_cap", ok, f"sent 2500, dropped {drops}")


def test_rate_limit_zero_disables_globally() -> None:
    bus = DataBus(rate_limit_hz=0)
    for _ in range(5000):
        bus.publish("trade:BTC", None)
    drops = sum(bus._dropped_total.values())
    ok = drops == 0
    print_test("rate_limit_zero_disables_globally", ok, f"drops={drops}")


def test_rate_window_uses_deque() -> None:
    """v3.1.21: the per-topic rate window is a collections.deque
    (O(1) popleft) instead of a list (O(n) pop(0))."""
    import collections
    bus = DataBus(rate_limit_hz=200)
    bus.publish("trade:BTC", None)
    assert isinstance(bus._rate_window["trade:BTC"], collections.deque)
    print_test("rate_window_uses_deque", True)


def test_rate_window_keeps_correct_size() -> None:
    """Under heavy publish, the deque trims to ``effective_limit`` and
    no message goes out the back door un-counted."""
    bus = DataBus(rate_limit_hz=10, topic_rate_limits={"trade:": 50})
    for _ in range(100):
        bus.publish("trade:BTC", None)
    window = bus._rate_window["trade:BTC"]
    assert len(window) <= 50
    print_test("rate_window_keeps_correct_size", len(window) <= 50,
               f"len={len(window)}")


def test_listener_iteration_is_safe() -> None:
    """v3.1.21: a callback that mutates the listener list during
    iteration must not break publish (we copy the list first)."""
    bus = DataBus(rate_limit_hz=0)
    removed = []

    def remover(_):
        removed.append(1)

    bus._listeners["topic:x"] = [remover]

    def interceptor(payload):
        # Mutate the listener list while we are iterating.
        bus._listeners["topic:x"] = []

    bus._listeners["topic:x"].append(interceptor)
    # This must not raise (the publish path copies the list first).
    bus.publish("topic:x", None)
    print_test("listener_iteration_is_safe", len(removed) == 1)


def main() -> int:
    print("=" * 70)
    print("QW4 (DataBus per-topic rate limits) tests")
    print("=" * 70)

    tests = [
        test_default_limit_used_when_no_override,
        test_per_topic_override_applied,
        test_per_topic_does_not_affect_other_topics,
        test_most_specific_prefix_wins,
        test_trade_topic_partial_drop_at_higher_cap,
        test_trade_topic_drops_most_at_default_cap,
        test_rate_limit_zero_disables_globally,
        test_rate_window_uses_deque,
        test_rate_window_keeps_correct_size,
        test_listener_iteration_is_safe,
    ]
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print_test(t.__name__, False, f"AssertionError: {e}")
        except Exception as e:  # noqa: BLE001
            print_test(t.__name__, False, f"{type(e).__name__}: {e}")

    print("=" * 70)
    if FAILED == 0:
        print(f"ALL TESTS PASSED ({len(tests)}/{len(tests)})")
        return 0
    print(f"FAILED: {FAILED}/{len(tests)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
