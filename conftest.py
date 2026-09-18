"""Test-environment hermeticity.

The bot's ``load_config`` intentionally loads the operator's ``.env`` into
``os.environ`` (only filling vars that are not already set). Locally that is
correct for the bot, but it leaks deployment switches into the test process
depending on suite order: e.g. ``DASHBOARD_AUTH_ENABLED=true`` + a
``BOT_DASHBOARD_TOKEN`` in the operator's ``.env`` would silently activate
auth for every dashboard test that assumes it is off.

Scrub those keys before every test and restore them afterwards. Tests that
need them set them explicitly (``patch.dict`` / ``create_app`` config), which
runs after this fixture and restores before it tears down.

The scrub sets each key to ``""`` rather than deleting it: ``load_config``
loads ``.env`` into ``os.environ`` but only for keys *not already set*, so a
popped key would be silently re-injected mid-test the first time a test
touches ``load_config``. An empty string blocks the refill and resolves as
absent everywhere downstream (``str.strip()`` → falsy).
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import pytest

# Deployment switches that must never leak from the operator's .env into tests.
_DEPLOYMENT_ENV_KEYS = (
    "DASHBOARD_AUTH_ENABLED",
    "DASHBOARD_RATE_LIMIT_PER_MIN",
    "BOT_DASHBOARD_TOKEN",
    "DASHBOARD_TOKEN",
    "DASHBOARD_CORS_ORIGINS",
)


@pytest.fixture(autouse=True)
def _scrub_deployment_env() -> None:
    saved: Dict[str, Optional[str]] = {}
    for key in _DEPLOYMENT_ENV_KEYS:
        saved[key] = os.environ.get(key)
        os.environ[key] = ""
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session", autouse=True)
def _scrub_deployment_env_session() -> None:
    """Same scrub for module/session-scoped fixtures.

    Higher-scoped fixtures instantiate *before* function-scoped ones, so a
    module-scoped server fixture (e.g. ``test_dashboard_smoke.dash_server``)
    would still see leaked ``.env`` values loaded earlier in the suite.
    Blank the keys for the whole session; the per-test fixture above keeps
    re-asserting them.
    """
    saved: Dict[str, Optional[str]] = {}
    for key in _DEPLOYMENT_ENV_KEYS:
        saved[key] = os.environ.get(key)
        os.environ[key] = ""
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
