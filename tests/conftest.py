"""Shared pytest configuration for the tests/ package."""
import pytest

_SUITE_MARKERS = {"unit", "integration_offline", "network", "testnet_live"}


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Fail collection if any test item lacks a required suite marker.

    CI filters tests via ``-m "unit or integration_offline"``. A test file that
    forgets to set one of the four suite markers (unit / integration_offline /
    network / testnet_live) would be silently skipped by that filter instead of
    running or failing loudly. Catch that here at collection time.
    """
    unmarked = [
        item.nodeid
        for item in items
        if not _SUITE_MARKERS.intersection(m.name for m in item.iter_markers())
    ]
    if unmarked:
        listing = "\n".join(f"  - {nodeid}" for nodeid in unmarked)
        raise pytest.UsageError(
            "The following test items are missing a required suite marker "
            f"({', '.join(sorted(_SUITE_MARKERS))}):\n{listing}"
        )
